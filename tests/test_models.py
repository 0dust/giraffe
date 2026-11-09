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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("nested", [False, True])
def test_request_options_reject_nonfinite_numbers_with_their_path(value, nested):
    options = {"extra": [{"top_p": value}]} if nested else {"top_p": value}
    path = "request_options.extra.0.top_p" if nested else "request_options.top_p"
    with pytest.raises(ValidationError) as caught:
        RunConfig(targets=[Target(name="local", url="http://localhost", model="example")],
                  request_options=options)
    assert f"{path} must be a finite number" in str(caught.value)


def test_request_options_preserve_finite_numbers_and_other_json_values():
    options = {"top_p": .5, "extra": [1, -2.5, True, False, "NaN", None, {"value": 1e308}]}
    cfg = RunConfig(targets=[Target(name="local", url="http://localhost", model="example")],
                    request_options=options)
    assert cfg.request_options == options


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("source", ["request_pack", "tool_fixture", "tool_conversation"])
def test_workload_request_options_reject_nonfinite_numbers(value, source):
    from giraffe.tool_checks import default_tool_fixture
    from tests.test_tool_conversations import fixture

    options = {"extra": [{"top_p": value}]}
    if source == "request_pack":
        changes = {"request_source": "request_pack", "request_pack": {"version": 1, "cases": [{
            "fixture_id": "one", "messages": [{"role": "user", "content": "hello"}],
            "scorer": "none", "options": options}]}}
        path = "request_pack.cases.0.options.extra.0.top_p"
    elif source == "tool_fixture":
        item = default_tool_fixture().model_dump(mode="python")
        changes = {"tool_checks": {"fixtures": [{**item, "options": options}]}}
        path = "tool_checks.fixtures.0.options.extra.0.top_p"
    else:
        changes = {"tool_checks": {"conversations": [fixture(options=options)]}}
        path = "tool_checks.conversations.0.options.extra.0.top_p"
    with pytest.raises(ValidationError) as caught:
        RunConfig(targets=[Target(name="local", url="http://localhost", model="example")], **changes)
    assert f"{path} must be a finite number" in str(caught.value)


def test_explicit_checks_override_legacy_flags_and_round_trip():
    target = Target(name="local", url="http://localhost", model="example")
    default = RunConfig(targets=[target])
    assert "json" not in default.checks and "gpu" not in default.checks
    legacy = RunConfig(targets=[target], structured_json=True, metrics=True)
    assert {"json", "gpu"} <= set(legacy.checks)
    selected = RunConfig(targets=[target], checks=["json"], metrics=True)
    assert selected.structured_json and not selected.metrics
    assert RunConfig.model_validate(selected.model_dump()) == selected


def test_sparse_check_options_inherit_suite_defaults_without_request_ceilings():
    target = Target(name="local", url="http://localhost", model="example")
    config = RunConfig(targets=[target], checks=["first_output"], concurrency=2, max_output_tokens=64,
                       limits={"latency_ms": 1000, "first_output_ms": 100},
                       test_options={"generation": {"samples": 9, "max_output_tokens": 256,
                                                    "limits": {"latency_ms": None}},
                                     "correctness": {"concurrency": 8}})
    generation = config.effective_check("generation")
    assert generation["samples"] == 9 and generation["max_output_tokens"] == 256
    assert generation["limits"]["latency_ms"] is None
    assert generation["limits"]["first_output_ms"] == 100
    assert config.effective_check("correctness")["concurrency"] == 8
    assert RunConfig.model_validate(config.model_dump()).model_dump() == config.model_dump()
    assert config.model_dump()["test_options"]["generation"]["limits"] == {"latency_ms": None}


@pytest.mark.parametrize("check_id,options", [
    ("correctness", {"concurrency": 5}),
])
def test_selected_overrides_above_global_ceiling_are_rejected_but_disabled_drafts_survive(check_id, options):
    target = Target(name="local", url="http://localhost", model="example")
    with pytest.raises(ValidationError, match=r"above Simultaneous limit \(4\)"):
        RunConfig(targets=[target], checks=[check_id], concurrency=4, test_options={check_id: options})
    saved = RunConfig(targets=[target], checks=["serving"], test_options={check_id: options})
    assert saved.model_dump()["test_options"][check_id] == options


def test_selected_traffic_limit_cannot_exceed_global_concurrency():
    target = Target(name="local", url="http://localhost", model="example")
    with pytest.raises(ValidationError) as caught:
        RunConfig(targets=[target], checks=["load"], concurrency=4,
            load_test={"patterns": {"concurrent": {"levels": [5]}}})
    assert caught.value.errors()[0]["ctx"]["fields"] == ["load_test.patterns.concurrent.levels", "concurrency"]
    # Legacy local admission caps have no second control in the merged check.
    legacy = RunConfig(targets=[target], checks=["capacity"], concurrency=4, traffic={"max_in_flight": 5})
    assert legacy.checks == ["load"]
    assert legacy.concurrency == 4
    assert any("max_in_flight dropped" in note for note in legacy._migration_notes)


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
    assert not (set(default.checks) - ((set(CORE_CHECKS) - {"fairness"}) | {"load"}))
    from giraffe.cli import migrated_configuration
    implicit = migrated_configuration(dict(targets=[target], sessions={}, buckets={}, tool_calling=True))
    assert {"sessions", "load", "tools"} <= set(implicit.checks)
    assert implicit.load_test.workloads.combinations.enabled
    assert implicit.load_test.workloads.stress.enabled
    assert not implicit.load_test.workloads.mixed.enabled
    explicit = migrated_configuration(dict(targets=[target], checks=["serving"], sessions={}, buckets={},
                         tool_calling=True))
    assert explicit.checks == ["serving"]
    assert explicit.sessions is not None and explicit.load_test is not None
    assert not explicit.tool_calling
    assert RunConfig.model_validate(explicit.model_dump()).model_dump() == explicit.model_dump()
    draft = ConfigDraft.model_validate({**implicit.model_dump(), "checks": []})
    assert draft.checks == []
    assert not draft.tool_calling
    assert set(CHECK_NAMES) - set(CORE_CHECKS) == {
        "gateway_attribution", "gateway_policy", "gateway_access",
        "prefix", "sessions", "consistency", "tools", "multi_user"}


@pytest.mark.parametrize("check_id,field", [
    ("load", "load_test"), ("prefix", "prefix"), ("sessions", "sessions"), ("consistency", "consistency"),
    ("tools", "tool_calling"), ("multi_user", "multi_user"),
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


@pytest.mark.parametrize("judging", [
    {"judges": [{"name": "same"}, {"name": "same"}]},
    {"judges": [{"url": "https://user:pass@localhost/v1"}]},
    {"judges": [{"url": "https://localhost/v1?key=secret"}]},
    {"judges": [{"url": "file:///tmp/judge"}]},
    {"judges": [{}] * 9}, {"concurrency": 0}, {"request_timeout_seconds": float("inf")},
    {"max_duration_seconds": 0}, {"max_output_tokens": 0},
    {"proxy": "http://user:pass@localhost"},
])
def test_judging_rejects_invalid_or_credential_bearing_configuration(judging):
    from giraffe.models import ConfigDraft

    with pytest.raises(ValueError):
        ConfigDraft(targets=[{"name": "local", "url": "http://localhost", "model": ""}], judging=judging)


def test_judge_vote_bool_is_strict_and_capabilities_are_discoverable():
    from giraffe.capabilities import capabilities
    from giraffe.models import JudgeVote

    for verdict in ("true", 1):
        with pytest.raises(ValueError):
            JudgeVote(judge="reviewer", url="http://localhost", model="judge", status="completed", verdict=verdict)
    discovered = capabilities()
    assert "judge" in discovered["request_pack"]["scorers"]
    assert discovered["judging"]["defaults"]["concurrency"] == 2
    assert discovered["judging"]["schema"]["properties"]["judges"]["maxItems"] == 8


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('encoding', ['json', 'yaml'])
def test_serial_answer_verdict_preserves_concurrency_intent_on_round_trip(explicit, encoding):
    import yaml
    from giraffe import runner
    from tests.test_check_intent_round2_core import record

    values = {'targets': [{'name': 't', 'url': 'http://local', 'model': 'm'}],
              'checks': ['correctness']}
    if explicit:
        values['default_concurrency'] = 4
    original = RunConfig.model_validate(values)
    if encoding == 'json':
        restored = RunConfig.model_validate_json(original.model_dump_json())
    else:
        restored = RunConfig.model_validate(yaml.safe_load(yaml.safe_dump(original.model_dump(mode='json'))))
    for configured in (original, restored):
        run = runner._Run(configured, None, None)
        run.records = [record(str(index), scenario='correctness_c4', check_ids=['correctness'],
                              timing_start_ms=index * 100)
                       for index in range(6)]
        result = next(check for check in runner._checks(run, configured.targets[0]) if check.id == 'correctness')
        assert result.status == ('inconclusive' if explicit else 'pass')
        assert configured.parallelism_requested('correctness') == explicit
        assert result.metrics['evidenced_overlap'] < 2
        if not explicit:
            assert 'server answered one at a time' in result.summary


@pytest.mark.parametrize('legacy,explicit', [({}, False), ({'concurrency': None}, False),
                                            ({'concurrency': 4}, True)])
def test_legacy_migration_preserves_whether_parallelism_was_requested(legacy, explicit):
    from giraffe.cli import _migrated_values

    values, _ = _migrated_values({'targets': [{'name': 'local', 'url': 'http://local', 'model': 'm'}], **legacy})
    config = RunConfig.model_validate(values)
    assert config.default_concurrency == 4
    assert config.parallelism_requested('correctness') == explicit
    assert ('default_concurrency' in config.model_dump(mode='json')) == explicit


@pytest.mark.parametrize("changes", [
    {"concurrency": 1}, {"long_cohort_size": 3, "concurrency": 3},
    {"long_cohort_size": 1, "min_overlap_samples": 2},
    {"requests_per_second": float("nan")}, {"post_seconds": float("inf")},
    {"event_timeout_seconds": 0}, {"grace_seconds": -1},
])
def test_scale_down_config_rejects_unbounded_or_starved_scenarios(changes):
    from giraffe.models import ScaleDownConfig
    with pytest.raises(ValidationError):
        ScaleDownConfig(target="service", **changes)


def test_scale_down_settings_are_optional_and_round_trip():
    from giraffe.models import ScaleDownConfig
    target = Target(name="service", url="http://localhost", model="model")
    assert RunConfig(targets=[target]).scale_down is None
    config = RunConfig(targets=[target], checks=["serving"], scale_down=ScaleDownConfig(target="service"))
    assert RunConfig.model_validate(config.model_dump()).scale_down == config.scale_down


def test_scale_down_arrival_bound_includes_baseline_samples():
    from giraffe.models import ScaleDownConfig
    # 99,999 follow-ups plus one long request already consume the finite cap.
    with pytest.raises(ValueError, match='maximum planned arrivals'):
        ScaleDownConfig(target='service', requests_per_second=9999.9, long_cohort_size=1,
                        baseline_timeout_seconds=1, event_timeout_seconds=7,
                        grace_seconds=1, post_seconds=1)


def test_scale_down_selection_requires_a_bound_target_and_config():
    target = dict(name='service', url='http://localhost/v1', model='test')
    for settings in (None, dict(target='missing')):
        with pytest.raises(ValueError, match='scale_down'):
            RunConfig(targets=[target], checks=['scale_down'], scale_down=settings)
    # Merely retaining scenario settings does not select the external exercise.
    cfg = RunConfig(targets=[target], scale_down=dict(target='service'))
    assert 'scale_down' not in cfg.checks
