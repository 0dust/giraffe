"""Stored default summaries survive upgrades; alternate selections use evidence."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from giraffe import __version__
from giraffe.capabilities import capabilities
from giraffe.models import (CURRENT_SCHEMA, REPORT_MIGRATIONS, RunConfig, RunReport,
                            is_supported_schema)
from giraffe.performance import build_performance, refresh_performance
from giraffe.reporting import _portable_json_chunks, _report_json_chunks, compare_baseline
from tests.test_reporting import baseline_check, make_report

FIXTURES = Path(__file__).parent / "fixtures"


def without_series(performance):
    return {**performance, "targets": [{key: value for key, value in target.items() if key != "request_series"}
                                       for target in performance.get("targets", [])]}


def test_main_schema_three_fixture_preserves_checks_outcome_and_conditions():
    # Captured using main's unmodified serializer and loader before this patch.
    saved = RunReport.model_validate_json((FIXTURES / "main_schema3_report.json").read_text())
    expected = json.loads((FIXTURES / "main_schema3_expected.json").read_text())
    assert saved.schema_version == "4"
    assert saved.overall == expected["overall"]
    assert [check.model_dump(mode="json") for check in saved.checks] == expected["checks"]
    assert saved.conditions == expected["conditions"]
    # Main gained seven speculative-decoding rows after this fixture was
    # frozen. Project only those additive rows out; every original metric,
    # numeric value and section row must still match the untouched snapshot.
    from giraffe.performance import SPEC_DECODE_ROWS
    actual = without_series(saved.performance)
    for target, frozen_target in zip(actual["targets"], expected["performance"]["targets"], strict=True):
        for window, summary in target["summaries"].items():
            frozen = frozen_target["summaries"][window]
            added = set(summary["metrics"]) - set(frozen["metrics"])
            assert added == {key for key, _, _ in SPEC_DECODE_ROWS}
            assert all(summary["metrics"][key]["value"] is None for key in added)
            original_server_rows = len(frozen["sections"]["server"])
            assert summary["sections"]["server"][original_server_rows:] == [
                summary["metrics"][key] for key, _, _ in SPEC_DECODE_ROWS]
            summary["sections"]["server"] = summary["sections"]["server"][:original_server_rows]
            summary["metrics"] = {key: value for key, value in summary["metrics"].items() if key not in added}
    assert actual == expected["performance"]
    assert saved.observations["derived_recomputed"] is True


@pytest.mark.parametrize("chunks", [_report_json_chunks, _portable_json_chunks])
def test_saved_default_derived_values_are_used_without_recomputation(chunks, monkeypatch):
    from giraffe import conditions
    report = make_report(count=5)
    raw = json.loads("".join(chunks(report)))
    assert raw["schema_version"] == CURRENT_SCHEMA
    assert raw["performance"] == without_series(report.performance)
    assert all("request_series" not in target for target in raw["performance"]["targets"])
    # Distinct values prove loading does not silently regenerate a plausible copy.
    raw["conditions"][0]["saved_marker"] = "original aggregate"
    target = raw["performance"]["targets"][0]
    target["summaries"][target["default_window"]]["metrics"]["response_latency"]["value"] = 987654
    expected = deepcopy(raw["performance"])
    monkeypatch.setattr(conditions, "aggregate_conditions", lambda _: pytest.fail("stored conditions recomputed"))
    saved = RunReport.model_validate(raw)
    assert without_series(saved.performance) == expected
    assert saved.conditions == raw["conditions"]
    assert "derived_recomputed" not in saved.observations
    assert without_series(build_performance(saved)) == expected
    assert refresh_performance(saved).performance == saved.performance
    # Explicit selections intentionally use the retained measurement evidence.
    assert build_performance(saved, target="local")["targets"][0]["summaries"][target["default_window"]]["metrics"]["response_latency"]["value"] != 987654


@pytest.mark.parametrize("version", [2, 3, 4, "2", "3", "4"])
def test_supported_saved_schema_forms_migrate_to_current_string(version):
    raw = json.loads("".join(_report_json_chunks(make_report())))
    raw["schema_version"] = version
    assert is_supported_schema(version)
    assert RunReport.model_validate(raw).schema_version == "4"
    assert set(REPORT_MIGRATIONS) == {"2", "3"}


@pytest.mark.parametrize("version", [True, None, "unknown", "5", 5])
def test_unsupported_saved_schemas_are_rejected(version):
    raw = json.loads("".join(_report_json_chunks(make_report())))
    raw["schema_version"] = version
    assert not is_supported_schema(version)
    with pytest.raises(ValidationError, match="Unsupported report schema"):
        RunReport.model_validate(raw)


def test_unknown_saved_fields_are_ignored_but_user_config_stays_strict():
    raw = json.loads("".join(_report_json_chunks(make_report())))
    raw["future_report_field"] = 1
    raw["requests"][0]["future_request_field"] = 1
    raw["checks"][0]["future_check_field"] = 1
    raw["requests"][0]["evaluation"] = {"messages": [], "scorer": "none", "future_evidence_field": 1}
    saved = RunReport.model_validate(raw)
    assert saved.requests and saved.checks
    with pytest.raises(ValidationError, match="Extra inputs"):
        RunConfig.model_validate({"targets": [{"name": "t", "url": "http://localhost", "model": "t"}], "future_config_field": 1})


def test_fixture_and_measurement_versions_gate_comparison_not_package_version():
    current, previous = make_report(count=5), make_report("baseline", count=5)
    current.suite_version, previous.suite_version = "new-package", "old-package"
    assert baseline_check(compare_baseline(current, previous)).status == "pass"
    current.manifest["timing_measurement_version"] = 3
    previous.manifest["timing_measurement_version"] = 2
    assert baseline_check(compare_baseline(current, previous)).status == "inconclusive"
    assert capabilities()["version"] == __version__


def test_identical_five_sample_reports_pass_with_sparse_secondary_metrics():
    current, previous = make_report(count=5), make_report("baseline", count=5)
    current.requests[0].max_stream_gap_ms = None
    comparison = compare_baseline(current, previous)
    assert baseline_check(comparison).status == "pass"
    assert any(row["status"] == "inconclusive" for row in comparison.baseline["targets"][0]["comparisons"])
    assert "not comparable" in baseline_check(comparison).summary


def test_missing_selected_timing_check_names_its_required_samples():
    current, previous = make_report(count=5), make_report("baseline", count=5)
    for report in (current, previous):
        report.checks[0].id = "first_output"
        for record in report.requests:
            record.check_ids = ["first_output"]
            record.first_output_ms = None
    check = baseline_check(compare_baseline(current, previous))
    assert check.status == "inconclusive"
    assert "first_output" in check.summary and "at least 5 measured samples" in check.summary


def test_streamed_export_preserves_stored_default_summary(tmp_path):
    from giraffe.report_stream import expanded_report_chunks
    raw = json.loads("".join(_report_json_chunks(make_report(count=5))))
    raw["conditions"][0]["saved_marker"] = "original aggregate"
    target = raw["performance"]["targets"][0]
    target["summaries"][target["default_window"]]["metrics"]["response_latency"]["value"] = 987654
    path = tmp_path / "report.json"
    path.write_text(json.dumps(raw))
    exported = json.loads("".join(expanded_report_chunks(path)))
    assert without_series(exported["performance"]) == raw["performance"]
    assert exported["conditions"] == raw["conditions"]
    assert "derived_recomputed" not in exported["observations"]


@pytest.mark.parametrize("source_schema", [4, None])
def test_baseline_save_canonicalizes_integer_and_unversioned_sources(tmp_path, source_schema):
    from giraffe.reporting import save_baseline_report
    raw = json.loads("".join(_report_json_chunks(make_report(count=5))))
    if source_schema is None:
        raw.pop("schema_version")
    else:
        raw["schema_version"] = source_schema
    source = tmp_path / "report.json"
    source.write_text(json.dumps(raw))
    destination = tmp_path / "baseline.json"
    save_baseline_report(source, destination)
    assert json.loads(destination.read_text())["schema_version"] == "4"
    assert destination.stat().st_ino != source.stat().st_ino


@pytest.mark.parametrize("matched,regression,expected", [(True, False, "pass"), (False, False, "inconclusive"), (True, True, "fail")])
def test_scenario_coverage_is_per_selected_check(matched, regression, expected):
    current, previous = make_report(count=10), make_report("baseline", count=10)
    for report in (current, previous):
        for index, record in enumerate(report.requests):
            record.scenario = "matched" if matched and index < 5 else "changed_shape"
            if report is current and record.scenario == "changed_shape":
                record.fixture_id = "different-fixture"
            if report is current and record.scenario == "matched" and regression:
                record.elapsed_ms = 500
    result = compare_baseline(current, previous)
    check = baseline_check(result)
    assert check.status == expected, check.summary
    rows = result.baseline["targets"][0]["comparisons"]
    assert any(row["metric"] == "request_shapes" and row["status"] == "inconclusive" for row in rows)
    if expected == "inconclusive":
        assert "correctness" in check.summary


@pytest.mark.parametrize("schema", ["2", "3"])
def test_phase_a_performance_cache_is_migrated_for_model_cli_and_export(tmp_path, schema):
    from giraffe.cli import load_report
    from giraffe.report_stream import expanded_report_chunks
    raw = json.loads((FIXTURES / "phase_a_schema3_report.json").read_text())
    raw["schema_version"] = schema
    path = tmp_path / "report.json"
    path.write_text(json.dumps(raw))
    for loaded in (RunReport.model_validate(raw), load_report(path)):
        assert loaded.schema_version == "4"
        assert loaded.performance["performance_schema"] == 1
        assert loaded.observations["derived_recomputed"] is True
        assert loaded.conditions == raw["conditions"]
        assert loaded.performance["targets"][0]["request_series"]
    exported = json.loads("".join(expanded_report_chunks(path)))
    assert exported["performance"]["performance_schema"] == 1
    assert exported["observations"]["derived_recomputed"] is True
    assert exported["performance"]["targets"][0]["summaries"]


@pytest.mark.asyncio
async def test_phase_a_performance_cache_loads_through_saved_run_api(tmp_path):
    import httpx
    from giraffe.web import create_app
    directory = tmp_path / "phase-a-schema3"
    directory.mkdir()
    (directory / "report.json").write_bytes((FIXTURES / "phase_a_schema3_report.json").read_bytes())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://localhost") as client:
        response = await client.get("/api/runs/phase-a-schema3")
        assert response.status_code == 200
        saved = response.json()["report"]
        assert saved["performance"]["performance_schema"] == 1
        assert saved["observations"]["derived_recomputed"] is True
        download = await client.get("/api/runs/phase-a-schema3/report.json")
        assert download.status_code == 200
        assert download.json()["performance"]["performance_schema"] == 1


@pytest.mark.parametrize("identifier", ["gpu", "restart", "cold_start", "telemetry", "custom_telemetry"])
def test_checks_without_request_paths_do_not_require_baseline_samples(identifier):
    from giraffe.models import CheckResult
    current, previous = make_report(count=5), make_report("baseline", count=5)
    for report in (current, previous):
        report.manifest["config"]["checks"].append(identifier)
        report.checks.append(CheckResult(id=identifier, target="local", title="Observation",
                                        status="inconclusive", summary="No inference metric."))
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "pass"
    assert "No comparable verdict metric" not in baseline_check(result).summary


def test_optional_request_check_does_not_create_a_baseline_regression():
    current, previous = make_report(count=5), make_report("baseline", count=5)
    for report in (current, previous):
        report.checks[0].required = False
    for record in current.requests:
        record.score = False
        record.elapsed_ms = 500
    # This synthetic fixture has serving cohorts without a corresponding check;
    # those genuine request metrics retain their legacy comparison path.
    for report in (current, previous):
        for record in report.requests:
            record.check_ids = ["correctness"]
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert baseline_check(result).summary == "Nothing comparable between the runs."
    assert result.baseline["targets"][0]["comparisons"] == []
    assert "collect at least" not in baseline_check(result).summary


def test_unknown_required_check_with_requests_retains_comparison_path():
    current, previous = make_report(count=5), make_report("baseline", count=5)
    for report in (current, previous):
        report.checks[0].id = "custom_request"
        report.manifest["config"]["checks"] = ["custom_request"]
        for record in report.requests:
            record.check_ids = ["custom_request"]
    assert baseline_check(compare_baseline(current, previous)).status == "pass"


@pytest.mark.parametrize("kind", ["load", "capacity"])
def test_optional_acceptance_points_do_not_create_a_baseline_regression(kind):
    from tests.test_load_test_baseline import arrival_report
    from tests.test_traffic_reporting import traffic_report
    previous = arrival_report() if kind == "load" else traffic_report("baseline")
    current = previous.model_copy(deep=True)
    for report in (current, previous):
        report.checks[0].required = False
    field = "points" if kind == "load" else "traffic_stages"
    current.checks[0].metrics[field][-1]["status"] = "fail"
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert baseline_check(result).summary == "Nothing comparable between the runs."
    assert ("load_test" if kind == "load" else "traffic_capacity") not in result.baseline["targets"][0]
    assert "collect at least" not in baseline_check(result).summary



def test_telemetry_only_comparability_gate_does_not_suggest_request_samples():
    from giraffe.models import CheckResult
    current, previous = make_report(count=5), make_report("baseline", count=5)
    for report in (current, previous):
        report.manifest["config"]["checks"] = ["gpu"]
        report.requests = []
        report.checks = [CheckResult(id="gpu", target="local", title="GPU", status="pass", summary="Observed")]
    current.manifest["timing_measurement_version"] = 3
    previous.manifest["timing_measurement_version"] = 2
    result = baseline_check(compare_baseline(current, previous))
    assert result.status == "inconclusive"
    assert result.summary.startswith("Nothing comparable between the runs")
    assert "measurement methods differ" in result.summary
    assert "collect at least" not in result.summary


def test_identical_gpu_only_runs_have_nothing_comparable():
    from giraffe.models import CheckResult
    current, previous = make_report(count=5), make_report("baseline", count=5)
    for report in (current, previous):
        report.manifest["config"]["checks"] = ["gpu"]
        report.requests = []
        report.checks = [CheckResult(id="gpu", target="local", title="GPU", status="pass", summary="Observed")]
    check = baseline_check(compare_baseline(current, previous))
    assert check.status == "inconclusive"
    assert check.summary == "Nothing comparable between the runs."
    assert "collect at least" not in check.summary


def test_unknown_check_name_is_not_treated_as_telemetry_without_evidence():
    current, previous = make_report(count=5), make_report("baseline", count=5)
    for report in (current, previous):
        report.checks[0].id = "restart"
        report.manifest["config"]["checks"] = ["restart"]
        for record in report.requests:
            record.check_ids = ["restart"]
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "pass"
    assert result.baseline["targets"][0]["comparisons"]


def test_required_check_without_any_samples_reports_nothing_comparable():
    check = baseline_check(compare_baseline(make_report(count=0), make_report("baseline", count=0)))
    assert check.status == "inconclusive"
    assert check.summary.startswith("Nothing comparable between the runs")
    assert "correctness" in check.summary and "collect at least" in check.summary


def test_empty_comparison_summary_separates_excluded_metrics_with_a_period():
    current, previous = make_report(count=1), make_report("baseline", count=1)
    for report in (current, previous):
        report.checks[0].required = False
    result = compare_baseline(current, previous)
    check = baseline_check(result)
    assert check.status == "inconclusive"
    assert check.summary.startswith("Nothing comparable between the runs. ")
    assert "metric(s) not comparable" in check.summary
    assert result.baseline["targets"][0]["comparisons"]
