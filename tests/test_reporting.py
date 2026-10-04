import json
from pathlib import Path

import pytest

from giraffe.models import CheckResult, RequestRecord, RunConfig, RunReport, Target
from giraffe.reporting import compare_baseline, write_report


def make_report(run_id: str = "current", *, latency: float = 100, count: int = 8) -> RunReport:
    config = RunConfig(targets=[Target(name="local", url="http://localhost:8000", model="test")])
    records = [RequestRecord(
        id=f"{run_id}-{index}", target="local", fixture_id="arithmetic-v1", scenario="warm",
        check_ids=["serving", "correctness"], started_at="2026-10-04T10:00:00Z",
        status="completed", http_status=200, elapsed_ms=latency, first_output_ms=latency / 2,
        last_output_ms=latency, answer_chunks=2,
        max_stream_gap_ms=latency / 10, output_tokens=10, output_chars=2, output="42",
        reasoning="computed", stream_terminated=True, finish_reason="stop", valid=True,
        score=True, score_message="Exact answer matched", input_chars=30, requested_max_tokens=128,
    ) for index in range(count)]
    return RunReport(
        run_id=run_id, started_at="2026-10-04T10:00:00Z", finished_at="2026-10-04T10:00:01Z",
        overall="pass", manifest={"config": config.model_dump(),
                                  "fixture_pack": {"version": "0.1.0", "custom": None},
                                  "run_location": {"hostname": "laptop", "system": "Darwin"},
                                  "load_shape": {"levels": [1, 2, 4]}},
        checks=[CheckResult(id="correctness", target="local", title="Known-answer correctness",
                            status="pass", summary="All answers matched")], requests=records,
    )


def baseline_check(report: RunReport) -> CheckResult:
    return next(check for check in report.checks if check.id == "baseline")


def test_same_evidence_passes_and_input_reports_are_unchanged():
    current, previous = make_report(), make_report("baseline")
    result = compare_baseline(current, previous)
    assert result.overall == "pass"
    assert baseline_check(result).status == "pass"
    assert current.baseline is None
    assert previous.baseline is None
    assert len(current.checks) == 1


def test_legacy_rate_reports_are_incomparable_with_new_generation_metrics():
    current, previous = make_report(), make_report("baseline")
    previous.schema_version = "1"
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert "Report schema versions differ." in result.baseline["targets"][0]["reasons"]


def test_generation_rate_comparison_does_not_confuse_longer_prefill_with_slower_decoding():
    current, previous = make_report(), make_report("baseline")
    for record in current.requests:
        record.first_output_ms += 5000
        record.last_output_ms += 5000
        record.elapsed_ms += 5000
    result = compare_baseline(current, previous)
    rows = {row["metric"]: row for row in result.baseline["targets"][0]["comparisons"]}
    assert rows["generation_tokens_per_second"]["status"] == "pass"
    assert rows["output_tokens_per_second"]["status"] == "fail"
    assert rows["first_output_ms"]["status"] == "fail"


@pytest.mark.parametrize("change", ["route", "model", "url", "load", "fixture", "unknown", "aborted"])
def test_incomparable_baseline_never_labels_regression(change):
    current, previous = make_report(latency=500), make_report("baseline")
    if change in {"route", "model", "url"}:
        previous.manifest["config"]["targets"][0][change] = "different"
    elif change == "load":
        previous.manifest["config"]["concurrency"] = 8
    elif change == "fixture":
        previous.manifest["fixture_pack"]["version"] = "unknown"
    elif change == "aborted":
        previous.abort_reason = "Request budget exhausted"
    else:
        previous.manifest = {}
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert result.baseline["targets"][0]["reasons"]
    assert not result.baseline["targets"][0]["comparisons"]


def test_changed_runtime_is_the_subject_of_comparison_not_an_incompatibility():
    current, previous = make_report(), make_report("baseline")
    current.manifest["config"]["targets"][0]["identity"] = {"image": "new-image"}
    previous.manifest["config"]["targets"][0]["identity"] = {"image": "old-image"}
    current.manifest["config"]["retention"] = "none"
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "pass"
    assert result.baseline["targets"][0]["identity_changes"]["image"] == {
        "baseline": "old-image", "current": "new-image",
    }


def test_seeded_timing_regression_is_detected_with_request_evidence():
    result = compare_baseline(make_report(latency=200), make_report("baseline"))
    check = baseline_check(result)
    assert check.required and check.status == "fail"
    assert result.overall == "fail"
    assert check.evidence_ids
    latency = next(row for row in result.baseline["targets"][0]["comparisons"]
                   if row["metric"] == "latency_ms")
    assert latency["status"] == "fail"
    assert latency["worsening"] == 100
    assert latency["worsening_interval_95"] == [100, 100]


@pytest.mark.parametrize("failure", ["wrong", "empty", "error"])
def test_seeded_valid_completion_correctness_and_errors(failure):
    current, previous = make_report(), make_report("baseline")
    for record in current.requests:
        if failure == "wrong":
            record.score = False
        elif failure == "empty":
            record.valid = False
            record.output = ""
            record.output_chars = 0
        else:
            record.status = "failed"
            record.valid = False
            record.error = "503 Service unavailable"
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "fail"
    expected_metric = {"wrong": "correctness_rate", "empty": "valid_completion_rate", "error": "error_rate"}[failure]
    assert any(row["metric"] == expected_metric and row["status"] == "fail"
               for row in result.baseline["targets"][0]["comparisons"])


def test_absent_samples_and_single_samples_are_inconclusive():
    for count in (0, 1):
        result = compare_baseline(make_report(count=count), make_report("baseline", count=count))
        assert baseline_check(result).status == "inconclusive"


def test_initial_single_request_does_not_prevent_comparing_sufficient_warm_samples():
    current, previous = make_report(), make_report("baseline")
    for report in (current, previous):
        initial = report.requests[0].model_copy(deep=True)
        initial.id += "-initial"
        initial.scenario = "initial"
        report.requests.append(initial)
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "pass"
    assert baseline_check(result).metrics["excluded_metrics"] > 0


def test_lost_timing_or_correctness_measurements_prevent_a_passing_comparison():
    current, previous = make_report(), make_report("baseline")
    for record in current.requests:
        record.first_output_ms = None
        record.score = None
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert "measurement coverage" in baseline_check(result).summary


def test_noise_is_inconclusive_when_it_crosses_material_threshold():
    current, previous = make_report(), make_report("baseline")
    for record, timing in zip(current.requests, [80, 90, 100, 110, 140, 180, 250, 400]):
        record.elapsed_ms = timing
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    latency = next(row for row in result.baseline["targets"][0]["comparisons"]
                   if row["metric"] == "latency_ms")
    assert latency["status"] == "inconclusive"


def test_shorter_generations_do_not_establish_performance_stability():
    current, previous = make_report(latency=70), make_report("baseline")
    for record in current.requests:
        record.output_chars = 1
        record.output_tokens = 5
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    latency = next(row for row in result.baseline["targets"][0]["comparisons"]
                   if row["metric"] == "latency_ms")
    assert latency["status"] == "inconclusive"
    assert latency["output_work"]["current"]["output_chars_p50"] == 1


def test_different_fixture_proportions_are_not_a_timing_regression():
    current, previous = make_report(), make_report("baseline")
    for report, slow_count in ((current, 7), (previous, 1)):
        for index, record in enumerate(report.requests):
            record.fixture_id = "slow" if index < slow_count else "fast"
            record.elapsed_ms = 1000 if record.fixture_id == "slow" else 100
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert "proportions" in baseline_check(result).summary


def test_absolute_failure_stays_failed_even_when_baseline_comparison_passes():
    current, previous = make_report(), make_report("baseline")
    current.checks[0].status = "fail"
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "pass"
    assert result.overall == "fail"


def test_operator_cancelled_current_run_cannot_create_a_completion_regression():
    current, previous = make_report(), make_report("baseline")
    current.abort_reason = "Operator cancelled the run"
    current.overall = "inconclusive"
    for record in current.requests:
        record.status = "cancelled"
        record.valid = False
        record.score = None
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert result.overall == "inconclusive"
    assert result.baseline["targets"][0]["comparisons"] == []
    assert "Current run was aborted" in baseline_check(result).summary


@pytest.mark.parametrize("saturated_run", ["current", "baseline"])
def test_generator_saturation_cannot_create_timing_regressions(saturated_run):
    current, previous = make_report(latency=200), make_report("baseline")
    noisy = current if saturated_run == "current" else previous
    noisy.observations["generator_max_lag_ms"] = 500
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert result.overall == "inconclusive"
    rows = result.baseline["targets"][0]["comparisons"]
    timing = {"latency_ms", "first_output_ms", "max_stream_gap_ms", "output_tokens_per_second",
              "generation_tokens_per_second"}
    assert all(row["status"] == "inconclusive" for row in rows if row["metric"] in timing)
    assert all(row["status"] == "pass" for row in rows if row["metric"] not in timing)


@pytest.mark.parametrize("failure", ["wrong", "protocol"])
def test_generator_saturation_does_not_hide_correctness_or_protocol_regressions(failure):
    current, previous = make_report(latency=200), make_report("baseline")
    current.observations["generator_max_lag_ms"] = 500
    for record in current.requests:
        if failure == "wrong":
            record.score = False
        else:
            record.valid = False
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "fail"
    assert result.overall == "fail"


def test_generator_saturation_threshold_matches_warm_first_output_scale():
    current, previous = make_report(latency=20000), make_report("baseline", latency=10000)
    current.observations["generator_max_lag_ms"] = 500
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "fail"
    assert result.baseline["targets"][0]["generator_saturated_runs"] == []


def test_custom_fixture_path_without_content_hash_is_not_comparable():
    current, previous = make_report(), make_report("baseline")
    for report in (current, previous):
        report.manifest["config"]["custom_fixtures"] = "/tmp/fixtures.json"
        report.manifest["fixture_pack"]["custom"] = "/tmp/fixtures.json"
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "inconclusive"
    assert "content hash" in baseline_check(result).summary


def test_same_custom_pack_content_can_move_between_directories():
    current, previous = make_report(), make_report("baseline")
    for report, path in ((current, "/tmp/current-fixtures.json"), (previous, "/tmp/old-fixtures.json")):
        report.manifest["config"]["custom_fixtures"] = path
        report.manifest["fixture_pack"].update(custom=path, custom_sha256="same-content-hash")
    assert baseline_check(compare_baseline(current, previous)).status == "pass"


def test_missing_scenario_does_not_hide_regression_in_another_comparable_scenario():
    current, previous = make_report(latency=200), make_report("baseline")
    missing = previous.requests[0].model_copy(deep=True)
    missing.id = "initial-baseline-only"
    missing.scenario = "initial"
    previous.requests.append(missing)
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "fail"
    assert result.baseline["targets"][0]["reasons"]


@pytest.mark.parametrize("policy", ["all", "failures", "none"])
def test_serialized_retention_preserves_measurements_scores_and_source(policy, tmp_path: Path):
    report = make_report()
    report.manifest["config"]["retention"] = policy
    report.requests[1].score = False
    report.requests[1].output = "incorrect"
    report.requests[1].output_chars = 9
    report.requests[2].status = "timeout"
    report.requests[2].valid = False
    report.requests[2].error = "Request timed out"
    json_path, html_path = write_report(report, tmp_path)
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[1].score is False
    assert saved.requests[1].output_chars == 9
    assert saved.requests[2].error == "Request timed out"
    assert saved.requests[0].elapsed_ms == report.requests[0].elapsed_ms
    assert report.requests[0].output == "42"
    assert bool(saved.requests[0].output) is (policy == "all")
    assert bool(saved.requests[1].output) is (policy != "none")
    assert bool(saved.requests[1].reasoning) is (policy != "none")
    assert html_path.exists()
    assert "Measurements by scenario" in html_path.read_text()
    assert "Errors / timeouts" in html_path.read_text()


def test_baseline_roundtrip_works_after_bodies_are_stripped(tmp_path: Path):
    previous = make_report("baseline")
    previous.manifest["config"]["retention"] = "none"
    saved, _ = write_report(previous, tmp_path)
    reloaded = RunReport.model_validate_json(saved.read_text())
    assert reloaded.requests[0].output == ""
    result = compare_baseline(make_report(latency=200), reloaded)
    assert baseline_check(result).status == "fail"


def test_html_escapes_target_response_and_request_links(tmp_path: Path):
    report = make_report()
    payload = '<script>alert("unsafe")</script>'
    report.manifest["config"]["targets"][0]["model"] = payload
    report.manifest["config"]["retention"] = "all"
    report.requests[0].output = payload
    report.requests[0].id = '" onclick="alert(1)'
    report.checks[0].evidence_ids = [report.requests[0].id]
    json_path, html_path = write_report(report, tmp_path)
    html = html_path.read_text()
    assert payload not in html
    assert "&lt;script&gt;" in html
    assert 'href="#request-0"' in html
    assert '<script' not in html
    assert 'src="http' not in html
    assert json.loads(json_path.read_text())["requests"][0]["output"] == payload


def test_regression_evidence_bodies_are_retained_as_failures(tmp_path: Path):
    report = compare_baseline(make_report(latency=200), make_report("baseline"))
    json_path, _ = write_report(report, tmp_path)
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[0].output == "42"
    assert saved.baseline["status"] == "fail"


def test_intentional_cancellation_is_not_a_retention_failure(tmp_path: Path):
    report = make_report()
    report.requests[0].scenario = "client_cancel"
    report.requests[0].status = "cancelled"
    report.requests[0].valid = False
    report.requests[0].score = None
    json_path, _ = write_report(report, tmp_path)
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[0].output == ""


def test_intentional_client_deadline_does_not_create_baseline_error_regression():
    current, previous = make_report(), make_report("baseline")
    for report in (current, previous):
        intentional = report.requests[0].model_copy(deep=True)
        intentional.id += "-deadline"
        intentional.scenario = "client_deadline"
        intentional.status = "timeout"
        intentional.valid = False
        intentional.score = None
        report.requests.append(intentional)
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "pass"
    assert not any(row["scenario"] == "client_deadline"
                   for row in result.baseline["targets"][0]["comparisons"])
