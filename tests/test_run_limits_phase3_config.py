"""Phase 3 request-setting ownership and defaults, without endpoint traffic."""

import pytest
from pydantic import ValidationError

from giraffe.capabilities import capabilities
from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_OPTION_FIELDS, RunConfig
from giraffe.planning import request_plan
from giraffe.request_pack import pack_preview, pack_specs
from giraffe.reporting import _settings_reasons, compare_baseline
from tests.test_reporting import make_report
from tests.test_request_pack import case, config as pack_config


def config(**updates):
    return RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}], **updates)


@pytest.mark.parametrize("check", ["serving", "first_output", "generation", "context", "correctness",
                                  "json", "cancellation", "recovery", "capacity", "arrivals", "consistency", "tools"])
def test_per_check_request_settings_can_exceed_suite_defaults(check):
    configured = config(checks=[check], max_output_tokens=16, request_timeout_seconds=1,
                        test_options={check: {"max_output_tokens": 64, "request_timeout_seconds": 7}})
    effective = configured.effective_check(check)
    assert effective["max_output_tokens"] == 64
    assert effective["request_timeout_seconds"] == 7
    assert RunConfig.model_validate(configured.model_dump()).effective_check(check) == effective


@pytest.mark.parametrize("check,field", [("sessions", "concurrency"), ("sessions", "max_output_tokens"),
                                         ("arrivals", "concurrency"), ("capacity", "concurrency"),
                                         ("prefix", "max_output_tokens"), ("concurrency", "max_output_tokens")])
def test_dedicated_request_fields_have_no_second_override(check, field):
    assert field not in CHECK_OPTION_FIELDS[check]
    with pytest.raises(ValidationError, match="unsupported options"):
        config(checks=[], test_options={check: {field: 4}})


def test_endpoint_response_owns_warm_samples_and_shared_request_settings():
    configured = config(checks=["access", "serving", "first_output"], samples=2,
                        test_options={"serving": {"samples": 9, "max_output_tokens": 256},
                                      "first_output": {"max_output_tokens": 192, "request_timeout_seconds": 60}})
    effective = configured.effective_response()
    assert effective["samples"] == 9
    assert effective["max_output_tokens"] == 256
    assert effective["request_timeout_seconds"] == 60
    plan = request_plan(configured)
    assert next(row for row in plan["rows"] if row["id"] == "warm")["requests_per_target"] == 9
    assert {field: plan["request_settings"]["serving"][field] for field in effective} == effective
    first = plan["request_settings"]["first_output"]
    assert "samples" not in first
    assert first["max_output_tokens"] == 256 and first["request_timeout_seconds"] == 60


@pytest.mark.parametrize("check", ["arrivals", "prefix", "sessions", "consistency", "tools"])
def test_extended_pass_criteria_inventory_is_derived_from_actual_verdicts(check):
    expected = {"first_output_ms", "latency_ms", "stream_gap_ms",
                "min_output_tokens_per_second", "max_error_rate", "min_samples"}
    if check == "tools":
        expected.remove("min_samples")
    assert CHECK_LIMIT_FIELDS[check] == expected
    overrides = {"latency_ms": 20, **({"min_samples": 2} if check != "tools" else {})}
    configured = config(checks=[check], limits={"latency_ms": 100},
                        test_options={check: {"limits": overrides}})
    assert configured.effective_check(check)["limits"]["latency_ms"] == 20
    assert configured.effective_check(check)["limits"]["min_samples"] == (5 if check == "tools" else 2)
    with pytest.raises(ValidationError, match="unsupported options"):
        config(checks=[check], test_options={check: {"limits": {"regression_percent": 5}}})


def test_session_existing_output_budgets_are_independent_of_suite_default():
    configured = config(checks=["sessions"], max_output_tokens=8,
                        sessions={"max_tokens": 64, "profile": "performance", "modes": ["live"],
                                  "workloads": [{"name": "chat", "turns": ["Hello", "Continue"], "max_tokens": 128}]})
    assert configured.sessions.max_tokens == 64
    assert configured.sessions.workloads[0].max_tokens == 128
    assert "max_output_tokens" not in configured.effective_check("sessions")


def test_pack_correctness_has_read_only_explanation_in_preview_plan_and_capabilities():
    configured = pack_config(default_concurrency=7).model_copy(update={"checks": ["correctness"]})
    preview = pack_preview(configured)
    metadata = preview["correctness_settings"]
    assert metadata["read_only"] is True and metadata["concurrency"] == 7
    assert "default_concurrency" in metadata["explanation"]
    assert request_plan(configured)["request_settings"]["correctness"] == metadata
    check = next(item for item in capabilities(configured)["checks"] if item["id"] == "correctness")
    assert check["request_settings"] == metadata
    assert preview["inapplicable_settings"] == ["correctness.samples", "correctness.concurrency",
                                                "correctness.max_output_tokens", "correctness.request_timeout_seconds"]


def test_pack_case_cap_and_capacity_overrides_are_resolved_without_suite_clamp():
    configured = pack_config([case("own", max_tokens=96), case("default")], max_output_tokens=16,
                             request_timeout_seconds=1,
                             test_options={"capacity": {"max_output_tokens": 64, "request_timeout_seconds": 5}})
    specs = {spec.workload["case_id"]: spec for spec in pack_specs(configured)}
    assert specs["own"].max_tokens == 96
    assert specs["default"].max_tokens == 64
    assert {spec.request_timeout_seconds for spec in specs.values()} == {5}


def test_regression_threshold_is_baseline_metadata_and_not_workload_identity():
    current, old = make_report(), make_report("baseline")
    for report in (current, old):
        configured = RunConfig.model_validate(report.manifest["config"])
        report.manifest["effective_checks"] = {name: configured.effective_check(name) for name in configured.checks}
    current.manifest["config"]["limits"]["regression_percent"] = 5
    for effective in current.manifest["effective_checks"].values():
        if "limits" in effective:
            effective["limits"]["regression_percent"] = 5
    assert _settings_reasons(current, old) == []
    compared = compare_baseline(current, old)
    assert compared.baseline["regression_threshold"] == 5
    configured = config(limits={"regression_percent": 5})
    metadata = {"field": "limits.regression_percent", "regression_percent": 5}
    assert capabilities(configured)["baseline_comparison"] == metadata
    assert request_plan(configured)["baseline_comparison"] == metadata


def test_old_effective_check_manifest_acquires_only_new_request_defaults():
    from giraffe.reporting import _comparison_manifest

    configured = config(checks=["serving", "generation", "prefix"])
    old = {"config": configured.model_dump(), "effective_checks": {
        "serving": {"limits": configured.limits.model_dump()},
        "generation": {"samples": 6, "max_output_tokens": 64, "limits": configured.limits.model_dump()},
        "prefix": {"limits": configured.limits.model_dump()},
    }}
    normalized = _comparison_manifest(old)["effective_checks"]
    assert normalized["serving"]["samples"] == configured.samples
    assert normalized["generation"]["max_output_tokens"] == 64
    assert normalized["generation"]["request_timeout_seconds"] == configured.request_timeout_seconds
    assert normalized["prefix"]["context_limit"] == configured.context_limit
    # Earlier workload evidence must not be filled just because a default exists.
    old["effective_checks"]["generation"].pop("samples")
    assert "samples" not in _comparison_manifest(old)["effective_checks"]["generation"]


def test_phase2_workload_source_bridge_requires_exact_attested_generator():
    from giraffe.deployment import fixture_hash
    from giraffe.reporting import _run_limits_workload_identity, _same_run_limits_workload

    configured = config(checks=["arrivals"], arrivals={"requests": 10})
    current = {"config": configured.model_dump()}
    old = {"config": configured.model_dump()}
    for manifest, revision in ((current, "phase3"), (old, "phase2")):
        identity = _run_limits_workload_identity(manifest, revision=revision)
        manifest["fixture_pack"] = {"workload_fixture_sha256": fixture_hash(identity)}
    assert _same_run_limits_workload(current, old)
    identity["generator_source_sha256"] = "unknown-generator"
    old["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    assert not _same_run_limits_workload(current, old)


def test_prefix_plan_uses_check_context_window_and_inherited_intrinsic_output_budget():
    configured = config(checks=["prefix"], max_output_tokens=8, context_limit=4096,
                        prefix={"prefix_chars": 32, "history_turns": 2},
                        test_options={"prefix": {"context_limit": 128}})
    row = next(row for row in request_plan(configured)["rows"] if row["id"] == "prefix")
    assert row["requests_per_target"] == 2 * configured.prefix.repeats + 2
    # The same prompt cannot fit when the inherited intrinsic budget is 24.
    configured = configured.model_copy(update={"max_output_tokens": 24})
    row = next(row for row in request_plan(configured)["rows"] if row["id"] == "prefix")
    assert row["requests_per_target"] == 0


def test_shared_response_effective_metadata_preserves_each_checks_own_pass_criteria():
    configured = config(checks=["serving", "first_output"], test_options={
        "serving": {"max_output_tokens": 256, "request_timeout_seconds": 20,
                    "limits": {"max_error_rate": .1}},
        "first_output": {"max_output_tokens": 192, "request_timeout_seconds": 10,
                         "limits": {"first_output_ms": 100}},
    })
    for check in ("serving", "first_output"):
        effective = configured.effective_check(check)
        assert effective["max_output_tokens"] == 256 and effective["request_timeout_seconds"] == 20
        assert request_plan(configured)["request_settings"][check] == effective
    assert configured.effective_check("serving")["limits"]["first_output_ms"] is None
    assert configured.effective_check("first_output")["limits"]["first_output_ms"] == 100
    assert "samples" not in configured.effective_check("first_output")


def test_masked_first_answer_request_override_does_not_change_baseline_workload():
    current, old = make_report(), make_report("baseline")
    for report, masked in ((current, 192), (old, 128)):
        configured = RunConfig.model_validate(report.manifest["config"] | {"test_options": {
            "serving": {"max_output_tokens": 256}, "first_output": {"max_output_tokens": masked},
        }})
        report.manifest["config"] = configured.model_dump()
        report.manifest["effective_checks"] = {check: configured.effective_check(check) for check in configured.checks}
    assert _settings_reasons(current, old) == []


@pytest.mark.parametrize("legacy", [False, True])
def test_load_migration_preserves_current_timeout_while_noting_only_legacy_overrides(legacy):
    from giraffe.models import migrate_concurrent_load

    values = {"config_version": 2, "targets": config().model_dump()["targets"],
              "checks": ["buckets"] if legacy else ["concurrency"],
              "test_options": {"concurrency": {"request_timeout_seconds": 90, "samples": 9}}}
    if legacy:
        values["buckets"] = {"levels": [1, 2]}
    migrated, notes = migrate_concurrent_load(values)
    assert migrated["test_options"]["concurrency"] == {"request_timeout_seconds": 90}
    assert values["test_options"]["concurrency"] == {"request_timeout_seconds": 90, "samples": 9}
    assert any("test_options.concurrency.samples=9 dropped" in note for note in notes)
    assert not any("test_options.concurrency.request_timeout_seconds" in note for note in notes)
    assert RunConfig.model_validate(migrated).effective_check("concurrency")["request_timeout_seconds"] == 90


def test_load_timeout_above_suite_default_survives_cli_save_and_reload(tmp_path, monkeypatch, capsys):
    import json

    from giraffe.cli import configuration, main, parser
    from giraffe.models import RunReport

    initial = config(checks=["concurrency"], request_timeout_seconds=1,
                     test_options={"concurrency": {"request_timeout_seconds": 90}})
    source, saved = tmp_path / "input.json", tmp_path / "saved.json"
    source.write_text(initial.model_dump_json())

    async def run(configuration, **kwargs):
        assert configuration.effective_check("concurrency")["request_timeout_seconds"] == 90
        return RunReport(run_id="timeout", started_at="now", finished_at="now", overall="pass",
                         manifest={"config": configuration.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(["run", "--config", str(source), "--skip-preflight", "--save-config", str(saved),
                 "--output", str(tmp_path / "run")]) == 0
    assert "dropped" not in capsys.readouterr().err
    values = json.loads(saved.read_text())
    assert values["test_options"]["concurrency"] == {"request_timeout_seconds": 90}
    reloaded = configuration(parser().parse_args(["run", "--config", str(saved)]))
    assert reloaded.effective_check("concurrency")["request_timeout_seconds"] == 90
    assert reloaded._migration_notes == []
    assert not capsys.readouterr().err


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate", "plan"])
async def test_load_timeout_above_suite_default_survives_web_round_trip(tmp_path, endpoint):
    from tests.test_web import browser

    values = config(checks=["concurrency"], request_timeout_seconds=1,
                    test_options={"concurrency": {"request_timeout_seconds": 90}}).model_dump(mode="json")
    async with browser(tmp_path) as client:
        first = await client.post(f"/api/config/{endpoint}", json=values)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result["config"]["test_options"]["concurrency"] == {"request_timeout_seconds": 90}
        assert result["migration_notes"] == []
        assert result["request_plan"]["request_settings"]["concurrency"]["request_timeout_seconds"] == 90
        second = await client.post(f"/api/config/{endpoint}", json=result["config"])
        assert second.status_code == 200, second.text
        assert second.json()["config"] == result["config"]
        assert second.json()["migration_notes"] == []


def test_disabled_first_answer_request_draft_does_not_change_endpoint_response():
    configured = config(checks=["serving"], max_output_tokens=16, request_timeout_seconds=1,
                        test_options={"first_output": {"max_output_tokens": 1000, "request_timeout_seconds": 90}})
    assert configured.effective_response() == {"samples": 6, "max_output_tokens": 16, "request_timeout_seconds": 1}
    assert configured.effective_check("serving")["max_output_tokens"] == 16
    assert configured.model_dump()["test_options"]["first_output"]["max_output_tokens"] == 1000


def test_disabled_endpoint_response_request_draft_does_not_mask_first_answer():
    configured = config(checks=["first_output"], samples=3, test_options={
        "serving": {"samples": 9, "max_output_tokens": 1000, "request_timeout_seconds": 90},
        "first_output": {"max_output_tokens": 64, "request_timeout_seconds": 7},
    })
    assert configured.effective_response() == {"samples": 3, "max_output_tokens": 64, "request_timeout_seconds": 7}
    first = configured.effective_check("first_output")
    assert first["max_output_tokens"] == 64 and first["request_timeout_seconds"] == 7
    assert "samples" not in first
    warm = next(row for row in request_plan(configured)["rows"] if row["id"] == "warm")
    assert warm["requests_per_target"] == 3


@pytest.mark.parametrize("check,additional", [("serving", "warm"), ("generation", "generation")])
def test_response_and_generation_only_plan_includes_shared_initial_request(check, additional):
    configured = config(checks=[check], samples=3, test_options={check: {"samples": 7}})
    plan = request_plan(configured)
    counts = {row["id"]: row["requests_per_target"] for row in plan["rows"]}
    assert counts["initial"] == 1 and counts[additional] == 7
    assert plan["planned_per_target"] == 8
