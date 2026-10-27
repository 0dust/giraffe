import json
import re
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
        last_output_ms=latency, answer_chunks=2, answer_reads=2, first_read_answer_chars=1,
        max_stream_gap_ms=latency / 10, output_tokens=10, output_chars=2, output="42",
        reasoning="computed", reasoning_tokens=1, stream_terminated=True, finish_reason="stop", valid=True,
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


def dead_endpoint_report():
    from giraffe.models import EvaluationEvidence

    report = make_report(count=8)
    report.overall = "fail"
    report.checks = [CheckResult(id="capacity", target="local", title="Capacity", status="fail",
        summary="Some incoming rates exceeded known-answer, error or timing limits.",
        metrics={"traffic_stages": [], "highest_acceptable_rate_rps": None})]
    for record in report.requests:
        record.status, record.valid, record.http_status = "failed", False, None
        record.error, record.score = "request failed (ConnectError)", None
        record.output, record.reasoning = "", ""
        record.evaluation = EvaluationEvidence(messages=[{"role": "user", "content": "context <notes> " * 2000}],
            scorer="exact", expected="42", expected_provided=True)
    return report


def test_short_baseline_summary_names_sample_gate_for_both_runs():
    result = compare_baseline(make_report(count=1), make_report("baseline", count=1))
    check = baseline_check(result)
    assert check.status == "inconclusive"
    assert "at least 5 measured samples per metric" in check.summary
    assert "both the current and baseline runs" in check.summary


def test_dead_endpoint_headline_and_bounded_html_keep_complete_json(tmp_path):
    report = dead_endpoint_report()
    before = report.model_dump_json()
    json_path, html_path = write_report(report, tmp_path, html=True)
    html = html_path.read_text()
    print(f"dead endpoint HTML bytes: {html_path.stat().st_size}")
    assert "Could not connect to http://localhost:8000 (ConnectError); no checks could run" in html
    assert "highest tested acceptable arrival rate" not in html
    assert html_path.stat().st_size < 60_000
    assert 'href="report.json"' in html
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[0].evaluation == report.requests[0].evaluation
    assert report.model_dump_json() == before


def test_html_caps_request_details_and_raw_bodies_without_losing_json(tmp_path):
    report = make_report(count=80)
    report.manifest["config"]["retention"] = "all"
    report.requests[-1].output = "<script>" + "long body " * 10000
    report.requests[-1].score = False
    json_path, html_path = write_report(report, tmp_path, html=True)
    html = html_path.read_text()
    assert "Showing 25 of 80 request details" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert html_path.stat().st_size < 120_000
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[-1].output == report.requests[-1].output


@pytest.mark.parametrize("policy,retained", [("all", True), ("none", False), ("failures", False)])
def test_html_logprob_alternatives_are_visible_despite_large_token_byte_arrays(policy, retained, tmp_path):
    from giraffe.logprobs import collect_logprobs

    report = make_report(count=1)
    report.manifest["config"]["retention"] = policy
    text, alternative = "retained chosen token " * 32, "ALT_ONLY_SECRET<script>"
    chosen = {"token": text, "bytes": list(text.encode()), "logprob": -.05}
    entry = {**chosen, "top_logprobs": [chosen.copy(), {
        "token": alternative, "bytes": list(alternative.encode()), "logprob": -2.0}]}
    record = report.requests[0]
    record.output, record.output_chars = text, len(text)
    record.logprob_evidence = collect_logprobs({"content": [entry]}, text, 2)
    before = report.model_dump_json()
    json_path, html_path = write_report(report, tmp_path, html=True)
    html, data = html_path.read_text(), json_path.read_text()
    assert ("ALT_ONLY_SECRET" in html) == retained
    assert ("ALT_ONLY_SECRET" in data) == retained
    assert "<script>" not in html
    assert ("ALT_ONLY_SECRET&lt;script&gt;" in html) == retained
    baseline = report.model_copy(deep=True)
    baseline.requests[0].logprob_evidence = None
    _, baseline_html = write_report(baseline, tmp_path / "without-logprobs", html=True)
    # Bound evidence growth independently of shared report markup. Raw byte
    # arrays must not be duplicated into the human-readable HTML.
    assert html_path.stat().st_size - baseline_html.stat().st_size < 6_000
    assert '"bytes":' not in html
    assert 'data:application/json' not in html
    saved = RunReport.model_validate_json(data)
    if retained:
        assert saved.requests[0].logprob_evidence == record.logprob_evidence
        assert 'href="report.json"' in html
    else:
        assert saved.requests[0].logprob_evidence.tokens == []
    assert report.model_dump_json() == before


@pytest.mark.parametrize("change", ["http_error", "success", "timeout", "no_requests"])
def test_connection_headline_requires_only_connection_failures(change, tmp_path):
    report = dead_endpoint_report()
    if change == "http_error":
        report.requests[0].http_status = 503
    elif change == "success":
        report.requests[0].status, report.requests[0].valid = "completed", True
    elif change == "timeout":
        report.requests[0].status, report.requests[0].error = "timeout", "request timeout (ReadTimeout)"
    else:
        report.requests = []
    _, path = write_report(report, tmp_path, html=True)
    assert "Could not connect to" not in path.read_text()
    assert "highest tested acceptable arrival rate" in path.read_text()


@pytest.mark.parametrize("policy", ["all", "failures", "none"])
def test_mismatch_excerpts_are_escaped_and_follow_retention(policy, tmp_path):
    from giraffe.fixtures import score_response
    from giraffe.models import RequestSpec

    report = make_report(count=1)
    report.manifest["config"]["retention"] = policy
    spec = RequestSpec(fixture_id="mismatch", scenario="warm", check_ids=["correctness"],
        messages=[], scorer="exact", expected='<expected "answer">')
    record = report.requests[0].model_copy(update={"output": '<received "answer">'})
    report.requests[0] = score_response(spec, record)
    check = report.checks[0]
    check.status = "fail"
    check.summary = report.requests[0].score_message
    check.metrics["scenarios"] = {"example": {"title": "Example", "status": "fail", "summary": check.summary}}
    json_path, html_path = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text())
    html = html_path.read_text()
    assert "<received" not in html
    assert ('&lt;received' in html) == (policy != "none")
    assert ("received:" in saved.requests[0].score_message) == (policy != "none")
    assert ("received:" in saved.checks[0].summary) == (policy != "none")
    assert ("received:" in saved.checks[0].metrics["scenarios"]["example"]["summary"]) == (policy != "none")


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


@pytest.mark.parametrize("verification,expected", [("required", "inconclusive"), ("off", "pass")])
def test_legacy_cancellation_grading_cannot_claim_a_server_stop_regression(verification, expected):
    current, previous = make_report(), make_report("baseline")
    for report in (current, previous):
        report.manifest["config"]["test_options"] = {"cancellation": {"backend_verification": verification}}
        report.checks.append(CheckResult(id="cancellation", target="local",
            title="Limits and cancellation", status="pass", summary="Recorded evidence"))
    current.checks[-1].metrics["cancellation_results"] = {"server_stopped": {"status": "inconclusive"}}
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == expected
    reasons = result.baseline["targets"][0]["reasons"]
    assert any("Cancellation verdict definitions differ" in reason for reason in reasons) == (verification == "required")


@pytest.mark.parametrize("change,expected", [
    ("legacy", "pass"), ("legacy_override", "inconclusive"), ("explicit_defaults", "pass"),
    ("inactive", "pass"), ("legacy_timer", "pass"),
    ("chunks", "inconclusive"), ("cleanup", "inconclusive"), ("clamped_cleanup", "pass"),
    ("unused_cleanup", "pass"), ("samples", "inconclusive"),
    ("enabled", "inconclusive"), ("required_cleanup", "inconclusive"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_cancellation_baselines_ignore_only_inactive_backend_settings(change, expected, reverse):
    current, previous = make_report(), make_report("baseline")
    for report in (current, previous):
        config = RunConfig.model_validate(report.manifest["config"])
        report.manifest["effective_checks"] = {
            name: config.effective_check(name) for name in config.checks
        }
    settings = previous.manifest["effective_checks"]["cancellation"]
    if change in {"legacy", "legacy_override"}:
        if change == "legacy_override":
            previous.manifest["config"]["test_options"] = {"cancellation": {"cancel_after_chunks": 1}}
        previous.manifest["effective_checks"]["cancellation"] = {
            name: value for name, value in settings.items()
            if name in {"samples", "cancel_after_ms", "deadline_probe_ms", "limits"}
        }
    elif change == "explicit_defaults":
        previous.manifest["config"]["test_options"] = {
            "cancellation": {"cancel_after_chunks": 5, "cleanup_seconds": 5, "backend_verification": "off"}}
    elif change == "inactive":
        settings.update(trace_file="/another/trace", server_log="/another/log", trace_wait_seconds=2)
    elif change == "legacy_timer":
        settings["cancel_after_ms"] += 1
    elif change == "chunks":
        settings["cancel_after_chunks"] += 1
    elif change == "cleanup":
        settings["cleanup_seconds"] = 1
    elif change == "clamped_cleanup":
        settings["cleanup_seconds"] = 10
    elif change == "unused_cleanup":
        for report in (current, previous):
            report.manifest["config"]["checks"].remove("recovery")
            report.manifest["effective_checks"].pop("recovery")
        settings["cleanup_seconds"] = 1
    elif change == "samples":
        settings["samples"] += 1
    elif change == "enabled":
        settings["backend_verification"] = "required"
    else:
        for report in (current, previous):
            report.manifest["effective_checks"]["cancellation"]["backend_verification"] = "required"
        settings["cleanup_seconds"] += 1
    if reverse:
        current, previous = previous, current
    before = [report.model_dump_json() for report in (current, previous)]
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == expected
    reasons = result.baseline["targets"][0]["reasons"]
    assert ("Recorded effective_checks differs or is missing." in reasons) == (expected == "inconclusive")
    assert [report.model_dump_json() for report in (current, previous)] == before


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


@pytest.mark.parametrize("change", ["route", "model", "url", "load", "fixture", "unknown", "aborted", "target_aborted"])
def test_incomparable_baseline_never_labels_regression(change):
    current, previous = make_report(latency=500), make_report("baseline")
    if change in {"route", "model", "url"}:
        previous.manifest["config"]["targets"][0][change] = "different"
    elif change == "load":
        previous.manifest["config"]["default_concurrency"] = 8
    elif change == "fixture":
        previous.manifest["fixture_pack"]["version"] = "unknown"
    elif change == "aborted":
        previous.abort_reason = "Request budget exhausted"
    elif change == "target_aborted":
        previous.observations["target_abort_reasons"] = {"local": "Endpoint local unresponsive"}
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


def test_scoped_delay_does_not_taint_other_baseline_scenarios_or_first_output():
    current, previous = make_report(latency=200, count=12), make_report("baseline", count=12)
    for report in (current, previous):
        for index, record in enumerate(report.requests):
            record.scenario = "affected" if index < 6 else "unaffected"
            record.dispatch_ms = 1000 if index < 6 else 4000
            record.completed_ms = record.dispatch_ms + record.elapsed_ms
        report.observations["client_timing_health"] = {"schema_version": 1, "windows": [],
                                                       "overflow_window": None}
    current.observations["generator_max_lag_ms"] = 500
    current.observations["client_timing_health"]["windows"] = [
        {"start_ms": 1120, "end_ms": 1180, "lag_ms": 500}]
    result = compare_baseline(current, previous)
    rows = result.baseline["targets"][0]["comparisons"]
    assert all(row["status"] == "fail" for row in rows
               if row["scenario"] == "unaffected" and row["metric"] == "latency_ms")
    affected = {row["metric"]: row for row in rows if row["scenario"] == "affected"}
    assert affected["latency_ms"]["status"] == "inconclusive"
    assert affected["first_output_ms"]["status"] == "fail"
    assert affected["latency_ms"]["current_samples"] == 6


def test_changed_receive_boundaries_require_new_timing_baseline_only():
    current, previous = make_report(), make_report("baseline")
    current.manifest["timing_measurement_version"] = 2
    rows = compare_baseline(current, previous).baseline["targets"][0]["comparisons"]
    assert all(row["status"] == "inconclusive" for row in rows if row["metric"] == "latency_ms")
    assert all(row["status"] == "pass" for row in rows if row["metric"] == "correctness_rate")


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
    json_path, html_path = write_report(report, tmp_path, html=True)
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


@pytest.mark.parametrize("policy", ["all", "failures", "none"])
@pytest.mark.parametrize("content_status", ["pass", "fail", "not_checked"])
def test_tool_evidence_retention_and_html(policy, content_status, tmp_path):
    report = make_report(count=1)
    report.manifest["config"]["retention"] = policy
    record = report.requests[0]
    expected_content = None if content_status == "not_checked" else {
        "mode": "exact", "value": "Expected <tail>", "normalization": "none",
    }
    expectation = {
        "tools": [{"type": "function", "function": {
            "name": "lookup", "description": "Look up <city>",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}],
        "expected_calls": [{"name": "lookup", "arguments": {"city": "<Delhi>"}}],
        "expected_content": expected_content,
    }
    outcomes = {
        "protocol": {"status": "pass", "message": "Completed protocol."},
        "content": {"status": content_status, "message": "Content <verdict>."},
        "calls": {"status": "pass", "message": "Matched configured calls."},
    }
    actual_calls = [{"id": "call_1", "type": "function", "function": {
        "name": "lookup", "arguments": '{"city":"<observed-secret>"}',
    }}]
    events = [
        {"sequence": 0, "kind": "content", "offset": 0, "length": 17},
        {"sequence": 1, "kind": "tool_call", "index": 0, "name_length": 6,
         "arguments_length": 28, "id_present": True, "type_present": True},
    ]
    report.requests[0] = RequestRecord.model_validate(record.model_dump() | {
        "score": content_status != "fail", "output": "<actual-response>",
        "output_chars": 17, "reasoning": "<actual-reasoning>", "tool_calls": actual_calls,
        "evaluation": {
            "messages": [{"role": "user", "content": "Check <city>"}],
            "scorer": "tool", "tool_expectation": expectation,
        },
        "tool_evaluation": outcomes,
        "response_events": events, "response_events_truncated": True,
    })
    before = report.model_dump()
    json_path, html_path = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text()).requests[0]
    retained = policy == "all" or (policy == "failures" and content_status == "fail")
    assert saved.tool_evaluation.model_dump() == outcomes | {
        "choice": None, "arguments": None, "task": None, "support": None,
    }
    assert saved.evaluation.tool_expectation.model_dump() == expectation | {"choice": None}
    assert bool(saved.output) == retained
    assert bool(saved.reasoning) == retained
    assert saved.tool_calls == (actual_calls if retained else [])
    assert saved.response_events == events
    assert saved.response_events_truncated is True
    assert saved.score == (content_status != "fail")
    assert report.model_dump() == before
    html = html_path.read_text()
    for label in ("Tool-call expectations", "Assistant content", "Tool calls", "Protocol"):
        assert label in html
    assert "Content &lt;verdict&gt;." in html
    assert "&lt;Delhi&gt;" in html
    assert "<actual-response>" not in html
    assert "<observed-secret>" not in html
    assert ("&lt;observed-secret&gt;" in html) == retained
    assert ("&lt;actual-response&gt;" in html) == retained
    assert ("Expected &lt;tail&gt;" in html) == (content_status != "not_checked")
    assert "No expected answer configured." not in html


def test_legacy_tool_report_remains_readable(tmp_path):
    report = make_report(count=1)
    report.requests[0].evaluation = None
    json_path, html_path = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[0].tool_evaluation is None
    assert "Request evidence" in html_path.read_text()


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
    json_path, html_path = write_report(report, tmp_path, html=True)
    html = html_path.read_text()
    assert payload not in html
    assert "&lt;script&gt;" in html
    assert 'href="#request-0"' in html
    assert '<script' not in html
    assert 'src="http' not in html
    assert json.loads(json_path.read_text())["requests"][0]["output"] == payload


def test_regression_evidence_bodies_are_retained_as_failures(tmp_path: Path):
    report = compare_baseline(make_report(latency=200), make_report("baseline"))
    json_path, _ = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[0].output == "42"
    assert saved.baseline["status"] == "fail"


def test_intentional_cancellation_is_not_a_retention_failure(tmp_path: Path):
    report = make_report()
    report.requests[0].scenario = "client_cancel"
    report.requests[0].status = "cancelled"
    report.requests[0].valid = False
    report.requests[0].score = None
    json_path, _ = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[0].output == ""


@pytest.mark.parametrize("scenario", ["client_deadline", "backend_cancel"])
def test_intentional_client_deadline_does_not_create_baseline_error_regression(scenario):
    current, previous = make_report(), make_report("baseline")
    for report in (current, previous):
        intentional = report.requests[0].model_copy(deep=True)
        intentional.id += "-deadline"
        intentional.scenario = scenario
        intentional.status = "timeout" if scenario == "client_deadline" else "cancelled"
        intentional.valid = False
        intentional.score = None
        report.requests.append(intentional)
    result = compare_baseline(current, previous)
    assert baseline_check(result).status == "pass"
    assert not any(row["scenario"] == scenario
                   for row in result.baseline["targets"][0]["comparisons"])


def test_unselected_traffic_configuration_does_not_invalidate_other_checks():
    current, previous = make_report(), make_report("baseline")
    for report in (current, previous):
        report.manifest["config"]["checks"] = ["correctness"]
        report.manifest["effective_traffic"] = report.manifest["config"]["traffic"].copy()
    current.manifest["config"]["traffic"]["rates"] = [1, 20]
    current.manifest["effective_traffic"]["rates"] = [1, 20]
    assert baseline_check(compare_baseline(current, previous)).status == "pass"


def test_pack_html_separates_rates_and_qualifies_quiet_failure():
    from giraffe.models import CheckResult
    from giraffe.reporting import _traffic_table

    check = CheckResult(id="capacity", target="local", title="Capacity", status="fail",
                        summary="Quiet failed", metrics={
        "request_pack": {"control_status": "fail", "all_cases_scored": False},
        "traffic_stages": [{"rate_rps": 10, "status": "pass", "goodput_rps": 3,
                            "valid_throughput_rps": 8}],
    })
    content = _traffic_table(check)
    assert "Valid throughput" in content and "8 requests/s" in content
    assert "Scored-subset correct goodput" in content and "3 requests/s" in content
    assert "Loaded result: pass; quiet failed" in content
    assert 'class="badge pass"' not in content


@pytest.mark.parametrize("policy,score,retained", [
    ("all", True, True), ("failures", False, True),
    ("failures", True, False), ("none", False, False),
])
def test_judge_rationale_follows_answer_retention_without_losing_votes(tmp_path, policy, score, retained):
    report = make_report(count=1)
    report.manifest["config"]["retention"] = policy
    vote = {"judge": "reviewer", "url": "http://localhost:9000/v1", "model": "judge-model",
            "verdict": score, "status": "completed", "reason": "<script>private rationale</script>"}
    report.requests[0] = RequestRecord.model_validate({
        **report.requests[0].model_dump(), "score": score,
        "judgement": {"status": "pass" if score else "fail", "votes": [vote]},
    })
    original = report.model_copy(deep=True)
    json_path, html_path = write_report(report, tmp_path, html=True)
    saved = json.loads(json_path.read_text())["requests"][0]
    assert saved["judgement"]["votes"][0]["verdict"] == score
    assert bool(saved["judgement"]["votes"][0]["reason"]) == retained
    content = html_path.read_text()
    assert "AI judge verdicts" in content
    assert "<script>private rationale</script>" not in content
    assert ("&lt;script&gt;private rationale&lt;/script&gt;" in content) == retained
    assert report == original


@pytest.mark.parametrize("policy", ["all", "failures", "none"])
def test_evaluation_context_and_schema_error_survive_report_retention(policy, tmp_path):
    from giraffe.models import EvaluationEvidence

    report = make_report()
    report.manifest["config"]["retention"] = policy
    record = report.requests[0]
    record.score = None
    record.evaluation_error = "schema_recursion"
    record.evaluation = EvaluationEvidence(messages=[{"role": "user", "content": "<question>"}],
                                            scorer="json", expected=None, expected_provided=True,
                                            schema={"$ref": "#"}, rubric="No <invention>")
    json_path, html_path = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text()).requests[0]
    assert saved.evaluation.messages == record.evaluation.messages
    assert saved.evaluation.schema_ == {"$ref": "#"}
    assert saved.evaluation.expected_provided and saved.evaluation.expected is None
    assert saved.evaluation_error == "schema_recursion"
    assert bool(saved.output) is (policy != "none")
    html = html_path.read_text()
    for text in ("Input messages", "Expected answer", "Answer check", "JSON schema", "&lt;question&gt;", "No &lt;invention&gt;"):
        assert text in html
    assert "<question>" not in html


@pytest.mark.parametrize("scorer", ["none", "json", "judge"])
def test_request_evidence_distinguishes_unscored_purpose_from_expected_answer(scorer):
    from giraffe.models import EvaluationEvidence
    from giraffe.reporting import _render

    report = make_report(count=1)
    record = report.requests[0]
    record.score = None
    record.evaluation = EvaluationEvidence(
        messages=[], scorer=scorer, expected={"purpose": "observe <sustained> answer generation"},
        expected_provided=True,
    )
    html = _render(report)
    assert "observe &lt;sustained&gt; answer generation" in html
    assert "<sustained>" not in html
    assert ("<h3>Purpose</h3>" in html) is (scorer == "none")
    assert ("<h3>Expected answer</h3>" in html) is (scorer != "none")
    assert record.score is None


@pytest.mark.parametrize("side", ["current", "baseline"])
def test_schema_evaluation_error_cannot_produce_passing_comparison(side):
    current, baseline = make_report(), make_report("baseline")
    affected = current if side == "current" else baseline
    affected.requests[0].score = None
    affected.requests[0].evaluation_error = "schema_recursion"
    result = compare_baseline(current, baseline)
    assert result.baseline["status"] == "inconclusive"
    assert "answer evaluation failed" in str(result.baseline)


@pytest.mark.parametrize("status", ["fail", "inconclusive", "blocked"])
@pytest.mark.parametrize("evidence_count", [3, 30])
def test_html_prioritizes_all_check_evidence_over_request_detail_cap(status, evidence_count, tmp_path):
    import re

    report = make_report(count=80)
    evidence = report.requests[-evidence_count:]
    for index, record in enumerate(evidence):
        record.score = None
        record.valid = index % 3 != 0
        record.elapsed_ms = 1000
    for record in report.requests[:30]:
        record.score = False
    report.checks = [CheckResult(id=name, target="local", title=name, status=status,
                                summary="Measured evidence", evidence_ids=[r.id for r in evidence] + ["missing"])
                     for name in ["generation", "first_output", "serving"]]
    _, path = write_report(report, tmp_path, html=True)
    html = path.read_text()
    for index, record in enumerate(evidence):
        assert f'href="#request-{index}"' in html
        assert f'<details id="request-{index}">' in html
        assert f'Request {record.id} ' in html
    links = re.findall(r'href="#(request-[^"]+)"', html)
    assert links and all(f'id="{anchor}"' in html for anchor in links)
    assert len(re.findall(r'<details id="request-', html)) == max(25, evidence_count)
    if evidence_count < 25:
        assert 'Request current-0 ' in html
    assert 'Request current-30 ' not in html


def test_html_baseline_table_uses_actual_comparison_contract():
    from giraffe.reporting import _render

    report = compare_baseline(make_report(latency=200), make_report("baseline"))
    html = _render(report)
    assert '<th>Metric</th><th>Baseline</th><th>Current</th><th>Change</th><th>Verdict</th>' in html
    assert '+100.0%' in html
    assert '>Worse</strong>' in html
    assert '>No material change</strong>' in html
    assert 'Material worsening exceeds observed sample uncertainty.' in html
    assert html.count('>Baseline comparison</h2>') == 1
    assert 'Entry 1' not in html
    assert 'vs baseline baseline:' in html


def test_html_json_export_is_relative_with_filename_note_and_no_embedded_json():
    from giraffe.reporting import _render

    report = make_report(count=1)
    for output in ('42', 'large retained answer ' * 110_000):
        report.requests[0].output = output
        html = _render(report)
        assert 'href="report.json"' in html
        assert 'report.json — keep it next to this file' in html
        assert 'data:application/json' not in html
        assert 'base64' not in html
        assert len(html.encode()) < 50_000


@pytest.mark.parametrize("current_latency, expected", [(50, "Better"), (100, "No material change"),
                                                       (200, "Worse")])
def test_html_baseline_verdicts_follow_material_comparison(current_latency, expected):
    from giraffe.reporting import _baseline_table_html

    report = compare_baseline(make_report(latency=current_latency), make_report("baseline"))
    html = _baseline_table_html(report.baseline)
    assert f'>{expected}</strong>' in html


def test_html_low_sample_comparison_is_not_comparable():
    from giraffe.reporting import _render

    report = compare_baseline(make_report(count=1), make_report("baseline", count=1))
    html = _render(report)
    assert '>Not comparable</strong>' in html
    assert 'Needs at least 5 measured samples in each run.' in html
    assert 'Entry 1' not in html


def test_html_findings_include_three_bounded_expected_received_rows():
    from giraffe.models import EvaluationEvidence
    from giraffe.reporting import _render

    report = make_report(count=5)
    report.overall = "fail"
    report.checks[0].status = "fail"
    report.checks[0].summary = "Answers did not match."
    for record in report.requests:
        record.score = False
        record.output = '<received> ' * 100
        record.evaluation = EvaluationEvidence(messages=[], scorer="exact", expected='<expected> ' * 100,
                                               expected_provided=True)
    html = _render(report)
    findings = html[html.index('<section id="findings">'):html.index('<section id="checks">')]
    assert findings.count('class="answer-finding"') == 3
    assert 'Expected: &lt;expected&gt;' in findings
    assert 'Received: &lt;received&gt;' in findings
    assert '<received>' not in findings


def test_html_traffic_only_baseline_includes_capacity_delta_and_summary():
    from giraffe.reporting import _render
    from tests.test_traffic_reporting import traffic_report

    report = compare_baseline(traffic_report(high_failure="latency"), traffic_report("baseline"))
    assert report.baseline["targets"][0]["comparisons"] == []
    html = _render(report)
    assert 'vs baseline baseline: 1 worse' in html
    table = html[html.index('<table class="baseline-table">'):]
    assert 'Highest acceptable tested arrival rate (requests/s)' in table
    assert '-75.0%' in table
    assert '>Worse</strong>' in table


@pytest.mark.parametrize('has_points', [True, False])
def test_html_load_baseline_table_names_qualification_and_missing_comparisons(has_points):
    from giraffe.reporting import _render

    report = make_report()
    point = dict(type='combinations', shape='short_short', pattern='concurrent',
                 bursts=False, level=2, rate=None, source='primary', status='pass')
    comparison = dict(status='fail' if has_points else 'inconclusive',
                      reason='Lost acceptable load.' if has_points else 'No matched load points.',
                      points=[dict(baseline=point, current={**point, 'status': 'fail'},
                                   status='fail', reason='Lost acceptable load.')] if has_points else [])
    report.baseline = dict(run_id='before', status=comparison['status'],
                           targets=[dict(target='local', load_test=comparison)])
    html = _render(report)
    table = re.search(r'<table class="baseline-table">(.*?)</table>', html, re.S).group(1)
    assert html.count('<section id="baseline">') == 1
    if has_points:
        assert 'short input' in table.lower() and 'concurrency 2' in table
        assert '<td>Pass</td>' in table and '<td>Fail</td>' in table
        assert 'Worse' in table and 'Lost acceptable load.' in table
    else:
        assert 'Not comparable' in table and 'No matched load points.' in table


def test_html_baseline_groups_each_condition_with_changed_rows_first():
    from giraffe.reporting import _baseline_table_html

    unchanged = dict(status="pass", reason="No material worsening detected in these samples.",
                     baseline=100, current=100, check="correctness", scenario="warm")
    rows = [dict(unchanged, metric="only-unchanged", scenario="quiet"),
            dict(unchanged, metric="unchanged"),
            dict(unchanged, metric="uncertain", status="inconclusive", reason="Needs more samples."),
            dict(unchanged, metric="better", current=50, worsening=-50,
                 worsening_interval_95=[-50, -50], threshold=10),
            dict(unchanged, metric="worse", current=200, status="fail", reason="Latency doubled.")]
    html = _baseline_table_html(dict(targets=[dict(target="local", comparisons=rows)]))
    assert html.index('local · correctness · warm') < html.index('local · correctness · quiet')
    assert html.index('>Worse (1)</h4>') < html.index('>Better (1)</h4>')
    assert html.index('>Better (1)</h4>') < html.index('<summary>Unchanged (1)</summary>')
    assert html.index('<summary>Unchanged (1)</summary>') < html.index('<summary>Not comparable (1)</summary>')
    assert html.count('<details class="baseline-group">') == 3
    assert '<details class="baseline-group" open' not in html
    assert 'Needs more samples.' in html and 'Latency doubled.' in html
    assert 'No material worsening detected in these samples.' not in html
    assert html.count('<th>Reason</th>') == 2
    assert 'Entry ' not in html


def test_html_baseline_no_regression_summary_includes_uncompared_count_and_focus_target():
    from giraffe.reporting import _render

    report = make_report()
    report.baseline = dict(name="before-upgrade", targets=[dict(target="local", comparisons=[
        dict(status="pass", metric=f"unchanged-{index}") for index in range(64)
    ] + [dict(status="inconclusive", metric=f"uncompared-{index}", reason="No samples.")
         for index in range(30)])])
    html = _render(report)
    assert 'vs baseline before-upgrade: no regressions (64 unchanged, 30 not comparable)' in html
    assert '<a href="#baseline-heading">vs baseline' in html
    assert '<h2 tabindex="-1" id="baseline-heading">Baseline comparison</h2>' in html
    assert '<summary>Unchanged (64)</summary>' in html
    assert '<summary>Not comparable (30)</summary>' in html


@pytest.mark.parametrize('status, verdict', [('pass', 'Better'), ('inconclusive', 'Not comparable')])
def test_html_load_improvement_requires_comparable_evidence(status, verdict):
    from giraffe.reporting import _baseline_table_html, _baseline_summary_html

    point = dict(type='combinations', shape='short_short', pattern='concurrent',
                 bursts=False, level=2, rate=None, source='primary', status='fail')
    baseline = dict(run_id='before', targets=[dict(target='local', load_test=dict(
        reason='Complete matched load evidence.' if status == 'pass' else 'Client-limited evidence.',
        points=[dict(baseline=point, current={**point, 'status': 'pass'}, status=status,
                     reason='Acceptance changed.')]))])
    html = _baseline_table_html(baseline)
    assert f'>{verdict}</strong>' in html
    assert ('Complete matched load evidence.' if status == 'pass' else 'Client-limited evidence.') in html
    assert '<td>Fail</td>' in html and '<td>Pass</td>' in html
    if status == 'pass':
        assert '1 better' in _baseline_summary_html(baseline)


@pytest.mark.parametrize('scorer, criterion, options', [
    ('json', 'JSON schema', {'schema': {'type': 'object', 'required': ['answer']}}),
    ('judge', 'Rubric', {'rubric': 'Answer must identify the failed deployment.'}),
])
def test_html_findings_include_schema_and_rubric_failures_without_expected_value(scorer, criterion, options):
    from giraffe.models import EvaluationEvidence
    from giraffe.reporting import _render

    report = make_report(count=1)
    record = report.requests[0]
    report.overall = report.checks[0].status = 'fail'
    record.score = False
    record.score_message = 'Answer failed the configured criterion.'
    record.evaluation = EvaluationEvidence(messages=[{'role': 'user', 'content': 'What failed in this deployment?'}],
                                           scorer=scorer, **options)
    html = _render(report)
    findings = html[html.index('<section id="findings">'):html.index('<section id="checks">')]
    assert '<summary>What failed in this deployment?' in findings
    assert f'<strong>{criterion}:</strong>' in findings
    assert 'Answer failed the configured criterion.' in findings
    assert 'Expected: Not retained' not in findings


def test_html_tool_only_findings_and_request_evidence_show_tool_calls():
    from giraffe.models import EvaluationEvidence
    from giraffe.reporting import _render

    report = make_report(count=1)
    record = report.requests[0]
    report.overall = report.checks[0].status = 'fail'
    record.score, record.output = False, ''
    record.evaluation = EvaluationEvidence(messages=[{'role': 'user', 'content': 'Check the weather in Paris.'}],
                                           scorer='tool')
    record.tool_calls = [{'type': 'function', 'function': {'name': 'weather', 'arguments': '{"city":"Paris"}'}}]
    html = _render(report)
    assert 'Tool calls: weather({&quot;city&quot;:&quot;Paris&quot;})' in html
    assert '<h3>Tool response</h3>' in html
    assert 'No retained response body' not in html


def test_html_coverage_includes_selected_blocked_and_skipped_reasons():
    from giraffe.reporting import _render

    report = make_report()
    report.manifest['config']['checks'] = ['correctness', 'load']
    report.checks = [
        CheckResult(id='correctness', target='local', title='Answer correctness', status='blocked',
                    summary='Model unavailable.'),
        CheckResult(id='load', target='local', title='Load test', status='skipped', required=False,
                    summary='Request budget exhausted.'),
        CheckResult(id='json', target='local', title='JSON correctness', status='skipped', required=False,
                    summary='Not selected.'),
    ]
    html = _render(report)
    coverage = html[html.index('<h2>Coverage and limitations</h2>'):html.index('<section id="checks">')]
    assert 'Answer correctness: Model unavailable.' in coverage
    assert 'Load test: Request budget exhausted.' in coverage
    assert 'JSON correctness: Not selected.' not in coverage


@pytest.mark.parametrize("reason", [
    "Timing observed; no acceptance limit configured. To conclude, set limits.first_output_ms to your service's acceptance target and rerun.",
    "Timing observed; no acceptance limit configured. Set limits.first_output_ms (--first-output-ms) to your target and rerun.",
    "Stopped by you after 16 of 20 planned requests.",
])
def test_html_preserves_backend_reason_sentences_without_rewriting(reason):
    from html import escape
    from giraffe.reporting import _render

    report = make_report(count=1)
    report.overall = report.checks[0].status = "inconclusive"
    report.checks[0].summary = reason
    html = _render(report)
    # HTML does not strip prefixes or split sentences; preserve backend copy.
    assert f"<p>{escape(reason)}</p>" in html
    assert "Stopped by you Stopped by you" not in html
    assert "--First-output-ms" not in html
