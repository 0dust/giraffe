import pytest
from pydantic import ValidationError

from giraffe.models import CheckResult, RunConfig, Target, TrafficConfig, TrafficMix, overall_status


def test_bounds_and_target_names_are_checked_before_traffic():
    target = Target(name="local", url="http://localhost:11434/v1", model="example")
    for override in ({"concurrency": 0}, {"max_requests": 0}, {"context_limit": 0}):
        with pytest.raises(ValidationError):
            RunConfig(targets=[target], **override)
    with pytest.raises(ValidationError, match="unique"):
        RunConfig(targets=[target, target])


def test_missing_required_evidence_cannot_pass():
    result = CheckResult(id="serving", target="local", title="Serving", status="inconclusive", summary="No samples")
    assert overall_status([result]) == "inconclusive"
    assert overall_status([]) == "inconclusive"
    assert overall_status([result.model_copy(update={"status": "pass"})], "interrupted") == "inconclusive"


def test_optional_missing_telemetry_does_not_block_api_checks():
    serving = CheckResult(id="serving", target="local", title="Serving", status="pass", summary="Valid completion")
    gpu = CheckResult(id="gpu", target="local", title="GPU", status="inconclusive", required=False, summary="No metrics")
    assert overall_status([serving, gpu]) == "pass"


def test_request_overrides_cannot_escape_token_or_concurrency_budget():
    with pytest.raises(ValidationError, match="request_options"):
        RunConfig(targets=[Target(name="local", url="http://localhost", model="example")], request_options={"n": 100})


def test_explicit_checks_override_legacy_flags_and_round_trip():
    target = Target(name="local", url="http://localhost", model="example")
    default = RunConfig(targets=[target])
    assert "json" not in default.checks and "gpu" not in default.checks
    legacy = RunConfig(targets=[target], structured_json=True, metrics=True)
    assert {"json", "gpu"} <= set(legacy.checks)
    selected = RunConfig(targets=[target], checks=["json"], metrics=True)
    assert selected.structured_json and not selected.metrics
    assert RunConfig.model_validate(selected.model_dump()) == selected


def test_sparse_check_options_inherit_and_cannot_escape_global_ceilings():
    target = Target(name="local", url="http://localhost", model="example")
    config = RunConfig(targets=[target], checks=["first_output"], concurrency=2, max_output_tokens=64,
                       limits={"latency_ms": 1000, "first_output_ms": 100},
                       test_options={"generation": {"samples": 9, "max_output_tokens": 256,
                                                    "limits": {"latency_ms": None}},
                                     "correctness": {"concurrency": 8}})
    generation = config.effective_check("generation")
    assert generation["samples"] == 9 and generation["max_output_tokens"] == 64
    assert generation["limits"]["latency_ms"] is None
    assert generation["limits"]["first_output_ms"] == 100
    assert config.effective_check("correctness")["concurrency"] == 2
    assert RunConfig.model_validate(config.model_dump()).model_dump() == config.model_dump()
    assert config.model_dump()["test_options"]["generation"]["limits"] == {"latency_ms": None}


@pytest.mark.parametrize("check_id,options", [
    ("generation", {"max_output_tokens": 129}),
    ("correctness", {"concurrency": 5}),
    ("access", {"request_timeout_seconds": 31}),
])
def test_selected_overrides_above_global_ceiling_are_rejected_but_disabled_drafts_survive(check_id, options):
    target = Target(name="local", url="http://localhost", model="example")
    with pytest.raises(ValidationError, match="must not exceed global"):
        RunConfig(targets=[target], checks=[check_id], test_options={check_id: options})
    saved = RunConfig(targets=[target], checks=["serving"], test_options={check_id: options})
    assert saved.model_dump()["test_options"][check_id] == options


def test_selected_traffic_limit_cannot_exceed_global_concurrency():
    target = Target(name="local", url="http://localhost", model="example")
    with pytest.raises(ValidationError, match="traffic.max_in_flight"):
        RunConfig(targets=[target], checks=["capacity"], traffic={"max_in_flight": 5})
    saved = RunConfig(targets=[target], checks=["serving"], traffic={"max_in_flight": 5})
    assert saved.traffic.max_in_flight == 5


@pytest.mark.parametrize("options", [
    {"unknown": {}}, {"gpu": {"samples": 5}},
    {"serving": {"limits": {"min_correctness": .9}}},
])
def test_unsupported_check_options_are_rejected_before_traffic(options):
    with pytest.raises(ValidationError, match="unsupported"):
        RunConfig(targets=[Target(name="x", url="http://localhost", model="x")],
                  test_options=options)


@pytest.mark.parametrize("changes", [
    {"rates": [0]}, {"rates": [2, 1]}, {"rates": [1, 1]}, {"rates": [float("nan")]},
    {"rates": [10001]}, {"rates": [1000], "duration_seconds": 101},
    {"mix": {"short": 0, "long_input": 0, "long_output": 0}},
])
def test_unsafe_or_ambiguous_traffic_plans_are_rejected(changes):
    with pytest.raises(ValidationError):
        TrafficConfig(**changes)


def test_mix_normalization_is_finite_and_stable_across_reload():
    mix = TrafficMix(short=1e308, long_input=1e308, long_output=1e308)
    assert mix.short == pytest.approx(1 / 3)
    assert TrafficMix.model_validate(mix.model_dump()).model_dump() == mix.model_dump()


def test_workload_defaults_and_explicit_selection_preserve_disabled_configuration():
    from giraffe.models import CHECK_NAMES, CORE_CHECKS, ConfigDraft

    target = Target(name="local", url="http://localhost", model="example")
    default = RunConfig(targets=[target])
    assert not (set(default.checks) - set(CORE_CHECKS))
    implicit = RunConfig(targets=[target], sessions={}, buckets={}, tool_calling=True)
    assert {"sessions", "buckets", "mixed", "tools"} <= set(implicit.checks)
    explicit = RunConfig(targets=[target], checks=["serving"], sessions={}, buckets={},
                         tool_calling=True)
    assert explicit.checks == ["serving"]
    assert explicit.sessions is not None and explicit.buckets is not None
    assert not explicit.tool_calling
    assert RunConfig.model_validate(explicit.model_dump()) == explicit
    draft = ConfigDraft.model_validate({**implicit.model_dump(), "checks": []})
    assert draft.checks == []
    assert not draft.tool_calling
    assert set(CHECK_NAMES) - set(CORE_CHECKS) == {
        "arrivals", "prefix", "buckets", "mixed", "sessions", "consistency", "tools"}


@pytest.mark.parametrize("check_id,field", [
    ("arrivals", "arrivals"), ("prefix", "prefix"), ("buckets", "buckets"),
    ("mixed", "buckets"), ("sessions", "sessions"), ("consistency", "consistency"),
    ("tools", "tool_calling"),
])
def test_selected_extended_workloads_get_safe_defaults(check_id, field):
    config = RunConfig(targets=[Target(name="local", url="http://localhost", model="test")],
                       checks=[check_id], concurrency=1, max_output_tokens=16)
    assert getattr(config, field)
    assert config.checks == [check_id]
    assert RunConfig.model_validate(config.model_dump()) == config


def test_all_selectable_checks_have_discoverable_configuration():
    from giraffe.capabilities import capabilities
    from giraffe.models import CHECK_NAMES, DEFAULT_CHECKS

    discovered = capabilities()
    assert {check["id"] for check in discovered["checks"]} == set(CHECK_NAMES)
    assert {check["id"] for check in discovered["checks"] if not check["optional"]} == set(DEFAULT_CHECKS)
    assert all(check["description"] for check in discovered["checks"])
