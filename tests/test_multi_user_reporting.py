"""Persistent users remain identifiable across retained reports and comparisons."""
import json
from copy import deepcopy

import pytest

from giraffe.cli import _print_result
from giraffe.models import CheckResult, RunConfig
from giraffe.multi_user_metrics import summarize
from giraffe.reporting import compare_baseline, reproduction_export, write_report
from tests.test_multi_user_metrics import record, observation
from tests.test_reporting import make_report


def user_report(*, slow=False, missing=False, retention="none"):
    report = make_report()
    config = RunConfig(targets=[dict(name="local", url="http://localhost:8000", model="test")],
                       checks=["multi_user"], multi_user={"levels": [5], "duration_seconds": 10},
                       retention=retention)
    report.manifest["config"] = config.model_dump(mode="json")
    users = tuple(f"user-{index:02}" for index in range(1, 6))
    rows = []
    for user in users:
        if missing and user == users[-1]:
            continue
        for index in range(8):
            start = index * 1000
            duration = 500 if slow and user == users[-1] else 100
            item = record(user, identity=f"{user}-{index}", dispatch=start, end=start+duration,
                          first_output_ms=duration/2, output="suppressed answer", reasoning="suppressed reasoning")
            item.target = "local"
            rows.append(item)
    source = observation(rows, users=users)
    source["settings"] = config.multi_user.model_dump(mode="json")
    summary = summarize(source, rows)
    report.requests = rows
    report.checks = [CheckResult(id="multi_user", target="local", title="Multi-user",
        status=summary["status"], summary="Shared user measurements", metrics={"multi_user": summary})]
    report.observations = {"targets": {"local": {"multi_user": {**source, "summary": summary}}}}
    report.overall = summary["status"]
    return report


def test_html_json_and_export_retain_unserved_user_and_suppress_responses(tmp_path):
    report = user_report(missing=True)
    before = report.model_dump_json()
    json_path, html_path = write_report(report, tmp_path, html=True)
    html = html_path.read_text()
    saved = json.loads(json_path.read_text())
    assert "Per-user shared-window delivery" in html
    assert "Worst served user" in html
    assert "user-05" in html and "unexercised" in html
    assert 'href="#request-' in html
    assert "suppressed answer" not in html and "suppressed reasoning" not in html
    assert len(saved["checks"][0]["metrics"]["multi_user"]["windows"][0]["users"]) == 5
    exported = reproduction_export(report)
    user_data = exported["multi_user"]["local"]
    assert len(user_data["population"]) == 5
    assert len(user_data["windows"][0]["users"]) == 5
    assert user_data["window_policy"] == "completed-output-v1"
    assert "suppressed answer" not in json.dumps(exported)
    assert "suppressed reasoning" not in json.dumps(exported)
    assert report.model_dump_json() == before


def test_single_user_regression_is_not_pooled_with_healthy_peers():
    result = compare_baseline(user_report(slow=True), user_report())
    rows = result.baseline["targets"][0]["comparisons"]
    slow = [row for row in rows if row.get("user_id") == "user-05" and row["metric"] == "latency_ms"]
    assert len(slow) == 1 and slow[0]["status"] == "fail"
    assert result.baseline["status"] == "fail"
    assert all(row.get("user_id") for row in rows if row["check"] == "multi_user")


@pytest.mark.parametrize("field,value", [("seed", 999), ("levels", [1, 5]), ("duration_seconds", 20),
                                         ("shapes", ["long_long"]), ("fairness_tolerance_percent", 5),
                                         ("conversations", {"modes": ["live"]})])
def test_changed_user_workload_cannot_pass_baseline(field, value):
    current, previous = user_report(), user_report()
    current.manifest["config"]["multi_user"][field] = value
    result = compare_baseline(current, previous)
    assert result.baseline["status"] == "inconclusive"
    assert any("multi_user" in reason for reason in result.baseline["targets"][0]["reasons"])


def test_changed_window_policy_cannot_pass_baseline():
    current, previous = user_report(), user_report()
    current.checks[0].metrics["multi_user"]["window_policy"] = "different-policy"
    result = compare_baseline(current, previous)
    assert result.baseline["status"] == "inconclusive"
    assert any("window" in reason.lower() for reason in result.baseline["targets"][0]["reasons"])


def test_unserved_user_prevents_baseline_pass_even_when_peers_are_comparable():
    result = compare_baseline(user_report(missing=True), user_report())
    assert result.baseline["status"] == "inconclusive"


def test_cli_prints_each_user_and_common_delivery_window(tmp_path, capsys):
    report = user_report(missing=True)
    _print_result(report, tmp_path / "report.json", None, tmp_path, {})
    out = capsys.readouterr().out
    assert "Multi-user · local" in out
    assert "user-05" in out and "unexercised" in out
    assert "shared-window" in out


def test_legacy_reports_do_not_gain_persistent_users(tmp_path):
    report = make_report()
    _, html_path = write_report(report, tmp_path, html=True)
    assert "Per-user shared-window delivery" not in html_path.read_text()
    assert "multi_user" not in reproduction_export(report)


def test_matching_user_baseline_preserves_per_user_delivery_rows():
    result = compare_baseline(user_report(), user_report())
    rows = result.baseline["targets"][0]["comparisons"]
    rates = [row for row in rows if row["metric"] == "shared_window_output_tokens_per_second"]
    assert result.baseline["status"] == "pass"
    assert len(rates) == 5 and all(row["current"] == 80 for row in rates)
    assert all("worsening_interval_95" not in row for row in rates)


@pytest.mark.parametrize(("change", "expected"), [({}, "fail"), ({"input_tokens": 200}, "inconclusive"),
                                                    ({"output_tokens": None}, "inconclusive")])
def test_measured_zero_delivery_compares_when_the_same_work_drains_after_cutoff(change, expected):
    current, previous = user_report(), user_report()
    for report in (current, previous):
        settings = report.manifest["config"]["multi_user"]
        settings["pass_criteria"]["min_output_tokens_per_second"] = 1
        source = report.observations["targets"]["local"]["multi_user"]
        source["settings"] = settings
        if report is current:
            for request in report.requests:
                if request.workload["user_id"] == "user-05":
                    request.completed_ms = 10100
                    request.elapsed_ms = request.completed_ms - request.dispatch_ms
                    for field, value in change.items():
                        setattr(request, field, value)
        summary = summarize(source, report.requests)
        report.checks[0].metrics["multi_user"] = summary
        report.checks[0].status = report.overall = summary["status"]

    result = compare_baseline(current, previous)
    rows = {row["metric"]: row for row in result.baseline["targets"][0]["comparisons"]
            if row.get("user_id") == "user-05"}
    rate = rows["shared_window_output_tokens_per_second"]
    assert result.baseline["acceptance"] == {"current": "fail", "baseline": "pass"}
    assert rate["status"] == rows["user_acceptance"]["status"] == expected
    assert (rate["current"], rate["baseline"]) == (0, 80)
    assert rate["current_samples"] == 0
    if expected == "fail":
        assert result.baseline["status"] == "fail"
        assert rate["evidence_ids"] == [f"user-05-{i}" for i in range(8)]


@pytest.mark.parametrize(("limit", "new_input_tokens", "new_overlap", "expected"), [
    (1.1, 100, True, "fail"),
    (1.1, 200, True, "inconclusive"),
    (1.1, 100, False, "inconclusive"),
    (None, 100, True, "pass"),
])
def test_selected_fairness_baseline_tracks_threshold_crossings_with_comparable_work(
        limit, new_input_tokens, new_overlap, expected):
    current, previous = user_report(), user_report()
    current.requests = [request for request in current.requests if request.id != "user-05-7"]
    for report in (current, previous):
        settings = report.manifest["config"]["multi_user"]
        settings["pass_criteria"]["max_throughput_ratio"] = limit
        for request in report.requests:
            request.stream = True
            request.answer_chunks = request.answer_reads = 3 if report is previous or new_overlap else 1
            request.last_output_ms = 90
            if report is current:
                request.input_tokens = new_input_tokens
        source = report.observations["targets"]["local"]["multi_user"]
        source["settings"] = settings
        if report is current:
            source["windows"][0]["users"][-1]["dispatched_count"] = 7
        summary = summarize(source, report.requests)
        report.checks[0].metrics["multi_user"] = summary
        report.checks[0].status = report.overall = summary["status"]

    result = compare_baseline(current, previous)
    assert result.baseline["status"] == expected
    fairness = [row for row in result.baseline["targets"][0]["comparisons"] if row["metric"] == "fairness_acceptance"]
    if limit is None:
        assert not fairness
    else:
        assert len(fairness) == 1 and fairness[0]["status"] == expected
        assert fairness[0]["baseline"] == 1 and fairness[0]["current"] == pytest.approx(8 / 7)
        assert result.baseline["acceptance"]["current"] == ("fail" if new_overlap else "inconclusive")


@pytest.mark.parametrize(("drop_request", "changed_work", "expected"), [
    (True, True, "inconclusive"),
    (False, True, "inconclusive"),
    (False, False, "pass"),
])
def test_fairness_baseline_qualifies_work_even_when_both_runs_pass(drop_request, changed_work, expected):
    current, previous = user_report(), user_report()
    if drop_request:
        current.requests = [request for request in current.requests if request.id != "user-05-0"]
    old_lengths = [100]*5 + [120]*2 + [200]
    new_lengths = [100]*3 + [120]*4 + [200] if changed_work else old_lengths
    for report, lengths in ((previous, old_lengths), (current, new_lengths)):
        settings = report.manifest["config"]["multi_user"]
        settings["pass_criteria"]["max_throughput_ratio"] = 1.1
        for request in report.requests:
            request.stream = True
            request.answer_chunks = request.answer_reads = 3
            request.last_output_ms = 90
            request.input_tokens = lengths[int(request.id.rsplit("-", 1)[1])]
        source = report.observations["targets"]["local"]["multi_user"]
        source["settings"] = settings
        if report is current and drop_request:
            source["windows"][0]["users"][-1]["dispatched_count"] = 7
        summary = summarize(source, report.requests)
        report.checks[0].metrics["multi_user"] = summary
        report.checks[0].status = report.overall = summary["status"]
        fairness = summary["windows"][0]["fairness"]
        assert fairness["qualified"]
        assert [sample["input_median_tokens"] for sample in fairness["users"]] == [lengths[3]] * 5

    result = compare_baseline(current, previous)
    row = next(row for row in result.baseline["targets"][0]["comparisons"] if row["metric"] == "fairness_acceptance")
    assert row["status"] == result.baseline["status"] == expected
    assert result.baseline["acceptance"] == {"baseline": "pass", "current": "fail" if drop_request else "pass"}


def live_user_report():
    report = user_report()
    window = report.checks[0].metrics["multi_user"]["windows"][0]
    window["scenario"] = "repeated_conversations"
    for user in window["users"]:
        user["profile"] = "conversation:chat"
    for request in report.requests:
        conversation, turn = divmod(int(request.id.rsplit("-", 1)[1]), 2)
        request.workload.update(mode="live", profile="conversation:chat", turn_id=turn,
                                conversation_sequence=conversation+1, task_type="exact_repeat",
                                scenario="repeated_conversations")
        request.fixture_id = f"session_live_turn{turn}"
        request.request_hash = f"conversation-{conversation}-turn-{turn}"
        request.finish_reason = "stop"
        request.score = True
    return report


@pytest.mark.parametrize(("changed_field", "changed_value"), [
    ("request_hash", "different-live-history"),
    ("input_tokens", 400),
    ("request_hash", None),
    ("input_tokens", None),
])
def test_live_user_baseline_requires_comparable_history_and_input_usage(changed_field, changed_value):
    current, previous = live_user_report(), live_user_report()
    assert compare_baseline(previous, previous).baseline["status"] == "pass"

    for request in current.requests:
        if request.workload["user_id"] == "user-05" and request.workload["turn_id"] == 1:
            setattr(request, changed_field, changed_value)
    result = compare_baseline(current, previous)
    rates = [row for row in result.baseline["targets"][0]["comparisons"]
             if row["metric"] == "shared_window_output_tokens_per_second"]
    assert result.baseline["status"] == "inconclusive"
    assert [row["status"] for row in rates] == ["pass"] * 4 + ["inconclusive"]
    assert rates[-1]["current"] == rates[-1]["baseline"] == 80
    assert "live generated histories" in rates[-1]["reason"]


@pytest.mark.parametrize(("exact_recall", "extra_change", "expected"), [
    (True, {}, "fail"),
    (True, {"input_tokens": 400}, "inconclusive"),
    (False, {}, "fail"),
    (False, {"input_tokens": 101}, "inconclusive"),
    (False, {"request_hash": "unseen-history"}, "inconclusive"),
])
def test_live_history_repetitions_compare_when_the_extra_work_is_comparable(exact_recall, extra_change, expected):
    current, previous = live_user_report(), live_user_report()
    user = current.checks[0].metrics["multi_user"]["windows"][0]["users"][-1]
    unserved = set(user["request_ids"][4:])
    for report in (current, previous):
        for request in report.requests:
            if not exact_recall:
                request.workload["task_type"] = "chat"
                request.score = None
                request.request_hash = f"repeatable-history-{request.workload['turn_id']}"
            if report is previous and request.id in unserved:
                for field, value in extra_change.items():
                    setattr(request, field, value)
    current.requests = [request for request in current.requests if request.id not in unserved]
    user["request_ids"] = user["request_ids"][:4]
    user["outcomes"]["valid_completed"] = 4
    user["output_tokens_per_second"] = 40
    result = compare_baseline(current, previous)
    row = next(row for row in result.baseline["targets"][0]["comparisons"]
               if row.get("user_id") == "user-05" and row["metric"] == "shared_window_output_tokens_per_second")
    assert row["status"] == result.baseline["status"] == expected
    assert row["current"] == 40 and row["baseline"] == 80


@pytest.mark.parametrize(("change", "error_regression", "expected"), [
    ({"request_hash": "different-late-history", "input_tokens": 400}, False, "inconclusive"),
    ({"output_tokens": 300}, False, "inconclusive"),
    ({"completion_tokens": 300}, False, "inconclusive"),
    ({"output_tokens": 300}, True, "fail"),
    ({}, False, "fail"),
])
def test_live_acceptance_qualifies_the_work_behind_each_failure(change, error_regression, expected):
    current, previous = live_user_report(), live_user_report()
    for report in (current, previous):
        settings = report.manifest["config"]["multi_user"]["pass_criteria"]
        settings["max_completion_ms"] = 300
        source = report.observations["targets"]["local"]["multi_user"]
        source["settings"]["pass_criteria"] = settings
        source["windows"][0]["scenario"] = "repeated_conversations"
        for user in source["windows"][0]["users"]:
            user["profile"] = "conversation:chat"
        for request in report.requests:
            if request.id == "user-05-7":
                request.dispatch_ms = request.timing_start_ms = request.workload["ready_ms"] = 9900
                request.elapsed_ms = 200 if report is previous else 500
                request.completed_ms = request.dispatch_ms + request.elapsed_ms
                if report is current:
                    for field, value in change.items():
                        setattr(request, field, value)
            if report is current and error_regression and request.id == "user-05-6":
                request.score = False
        summary = summarize(source, report.requests)
        report.checks[0].metrics["multi_user"] = summary
        report.checks[0].status = report.overall = summary["status"]

    result = compare_baseline(current, previous)
    rows = result.baseline["targets"][0]["comparisons"]
    assert result.baseline["acceptance"] == {"baseline": "pass", "current": "fail"}
    assert result.baseline["status"] == expected
    acceptance = next(row for row in rows if row.get("user_id") == "user-05" and row["metric"] == "user_acceptance")
    assert acceptance["status"] == expected
    rate = next(row for row in rows if row.get("user_id") == "user-05"
                and row["metric"] == "shared_window_output_tokens_per_second")
    if not error_regression:
        assert rate["status"] == "pass"


def test_live_delivery_compares_requested_mix_of_in_window_completions():
    current, previous = live_user_report(), live_user_report()
    current.requests = [r for r in current.requests if r.id not in {f"user-05-{i}" for i in range(3, 8)}]
    late = next(request for request in current.requests if request.id == "user-05-2")
    late.dispatch_ms = late.timing_start_ms = late.workload["ready_ms"] = 9900
    late.elapsed_ms, late.completed_ms = 200, 10100
    for report in (current, previous):
        source = report.observations["targets"]["local"]["multi_user"]
        window = source["windows"][0]
        window["scenario"] = "repeated_conversations"
        for user in window["users"]:
            user["profile"] = "conversation:chat"
        if report is current:
            window["users"][-1]["dispatched_count"] = 3
        report.checks[0].metrics["multi_user"] = summarize(source, report.requests)

    result = compare_baseline(current, previous)
    rate = next(row for row in result.baseline["targets"][0]["comparisons"]
                if row.get("user_id") == "user-05" and row["metric"] == "shared_window_output_tokens_per_second")
    assert rate["status"] == result.baseline["status"] == "fail"
    assert (rate["current"], rate["baseline"]) == (20, 80)
    assert (rate["current_samples"], rate["baseline_samples"]) == (2, 8)
    assert rate["evidence_ids"] == ["user-05-0", "user-05-1"]


def test_live_timing_regression_requires_comparable_reasoning_work():
    current, previous = live_user_report(), live_user_report()
    for request in current.requests:
        if request.workload["user_id"] == "user-05":
            request.completion_tokens = 300
            request.elapsed_ms = 500
            request.completed_ms = request.dispatch_ms + request.elapsed_ms

    result = compare_baseline(current, previous)
    timing = next(row for row in result.baseline["targets"][0]["comparisons"]
                  if row.get("user_id") == "user-05" and row["metric"] == "latency_ms")
    assert timing["status"] == "inconclusive"
    assert any(warning["code"] == "output_work_changed" for warning in timing["warnings"])


@pytest.mark.parametrize(("current_long_index", "expected"), [(5, "inconclusive"), (None, "pass")])
def test_live_baseline_qualifies_only_the_completions_delivered_in_the_window(current_long_index, expected):
    current, previous = live_user_report(), live_user_report()
    for report, long_index in ((current, current_long_index), (previous, 7)):
        source = report.observations["targets"]["local"]["multi_user"]
        window = source["windows"][0]
        window["scenario"] = "repeated_conversations"
        for user in window["users"]:
            user["profile"] = "conversation:chat"
        for request in report.requests:
            if request.workload["user_id"] != "user-05":
                continue
            index = int(request.id.rsplit("-", 1)[1])
            if index == long_index:
                request.request_hash = "long-history"
                request.input_tokens = 400
            if index == 7:
                request.completed_ms = window["end_ms"] + 10
                request.elapsed_ms = request.completed_ms - request.dispatch_ms
        summary = summarize(source, report.requests)
        report.checks[0].metrics["multi_user"] = summary
        user = summary["windows"][0]["users"][-1]
        assert user["coverage"]["status"] == "complete"
        assert user["late_completed"] == 1
        assert user["output_tokens_per_second"] == 70

    result = compare_baseline(current, previous)
    row = next(row for row in result.baseline["targets"][0]["comparisons"]
               if row.get("user_id") == "user-05" and row["metric"] == "shared_window_output_tokens_per_second")
    assert row["status"] == result.baseline["status"] == expected
    assert row["current"] == row["baseline"] == 70


def test_failures_retention_keeps_truncated_conversation_output_in_a_passing_run(tmp_path):
    report = user_report(retention="failures")
    truncated = report.requests[0]
    truncated.workload.update(session_id="conversation", mode="live")
    truncated.finish_reason = "length"
    truncated.output = "partial conversation answer"
    source = report.observations["targets"]["local"]["multi_user"]
    source["settings"]["pass_criteria"]["max_error_percent"] = 20
    summary = summarize(source, report.requests)
    assert summary["status"] == "pass"
    assert summary["windows"][0]["users"][0]["outcomes"]["errors"] == 1
    report.checks[0].metrics["multi_user"] = summary

    json_path, _ = write_report(report, tmp_path, html=False)
    saved = json.loads(json_path.read_text())
    assert saved["requests"][0]["output"] == "partial conversation answer"
    assert saved["requests"][1]["output"] == ""


def test_ready_wait_regression_is_not_hidden_by_short_dispatch_latency():
    current, previous = user_report(), user_report()
    for item in current.requests:
        if item.workload["user_id"] == "user-05":
            item.dispatch_ms += 500
            item.timing_start_ms += 500
            item.completed_ms += 500
    result = compare_baseline(current, previous)
    row = next(row for row in result.baseline["targets"][0]["comparisons"]
               if row.get("user_id") == "user-05" and row["metric"] == "latency_ms")
    assert row["current"] == 600 and row["baseline"] == 100
    assert row["status"] == "fail" and row["timing_origin"] == "user_ready"


def test_known_user_failure_survives_another_users_incomplete_coverage():
    current, previous = user_report(missing=True), user_report()
    user = current.checks[0].metrics["multi_user"]["windows"][0]["users"][0]
    user["status"] = "fail"
    user["reasons"] = ["A late request failed."]
    result = compare_baseline(current, previous)
    assert result.baseline["status"] == "fail"
    assert any(row["metric"] == "user_acceptance" and row["user_id"] == "user-01"
               for row in result.baseline["targets"][0]["comparisons"])


@pytest.mark.parametrize("retention", ["none", "failures", "all"])
def test_reproduction_export_includes_only_compact_user_evidence(retention):
    report = user_report(retention=retention)
    report.manifest["config"]["targets"][0]["identity"] = {"api_key": "private-api-key-value"}
    exported = reproduction_export(report)
    text = json.dumps(exported)
    assert "private-api-key-value" not in text
    assert "suppressed answer" not in text and "suppressed reasoning" not in text
    assert len(exported["multi_user"]["local"]["windows"][0]["users"]) == 5


def test_performance_surface_keeps_all_users_in_their_common_window():
    from giraffe.performance import build_performance
    value = build_performance(user_report(missing=True))
    target = value["targets"][0]
    result = target["summaries"][target["default_window"]]["metrics"]["persistent_user_delivery"]
    assert len(result["value"]) == 5
    assert result["window_policy"] == "completed-output-v1"
    assert result["value"][-1]["coverage"]["status"] == "unexercised"


def calibrated_report(*, slow=False, input_chars=570):
    report = user_report(slow=slow)
    for item in report.requests:
        item.input_chars = input_chars
        item.workload["input_preparation"] = {
            "method": "local_tokenizer", "tokenizer": "builtin", "target_tokens": 128,
            "input_tokens": 118 if input_chars == 570 else 117,
            "chat_template": False, "count_scope": "reference_prompt_with_same_width_nonce",
            "token_count_scope": "builtin_reference", "encoding": "o200k_base"}
    return report


def test_calibrated_token_target_compares_across_prompt_character_variation():
    current, baseline = calibrated_report(slow=True, input_chars=546), calibrated_report()
    result = compare_baseline(current, baseline)
    rows = result.baseline["targets"][0]["comparisons"]
    assert not any(row["metric"] == "request_shapes" for row in rows)
    row = next(row for row in rows if row.get("user_id") == "user-05" and row["metric"] == "latency_ms")
    assert row["status"] == "fail"
    assert result.baseline["status"] == "fail"


@pytest.mark.parametrize("field,value", [("target_tokens", 256), ("tokenizer", "different-tokenizer")])
def test_changed_preparation_identity_cannot_pass_user_delivery_baseline(field, value):
    current, baseline = calibrated_report(), calibrated_report()
    for item in current.requests:
        if item.workload["user_id"] == "user-05":
            item.workload["input_preparation"][field] = value
    result = compare_baseline(current, baseline)
    assert result.baseline["status"] == "inconclusive"
    row = next(row for row in result.baseline["targets"][0]["comparisons"]
               if row.get("user_id") == "user-05" and row["metric"] == "shared_window_output_tokens_per_second")
    assert row["status"] == "inconclusive"


def scenario_report():
    report = user_report()
    summary = report.checks[0].metrics["multi_user"]
    window = summary["windows"][0]
    window.update(scenario="noisy_neighbors", role="probe", trigger="during_stream",
                  heavy_profile="long_short", heavy_user_ids=["user-05"], control_window_id="control")
    window["interference"] = {"status": "inconclusive", "qualified": False,
        "control_window_id": "control", "trigger": "during_stream", "heavy_profile": "long_short",
        "heavy_user_ids": ["user-05"], "reasons": ["Missing heavy overlap"], "private_prompt": "do not export",
        "users": [{"user_id": "user-01", "status": "inconclusive", "qualified": False,
            "reasons": ["Missing heavy overlap"], "matched_samples": 2,
            "control_request_ids": ["user-01-0"], "probe_request_ids": ["user-01-1"],
            "heavy_request_ids": ["user-05-0"], "slowdown": 1.2,
            "first_answer_change_ms": 20, "completion_change_ms": 30, "stream_gap_change_ms": 10,
            "output_tokens_change": -5, "control_input_median_tokens": 100,
            "probe_input_median_tokens": 100, "control_output_median_tokens": 100,
            "probe_output_median_tokens": 95, "heavy_input_median_tokens": 640,
            "heavy_output_median_tokens": 32, "private_prompt": "do not export"}]}
    cancellation = deepcopy(window)
    cancellation.update(id="cancellation", scenario="cancellation", role="cancellation", selected_user_id="user-02")
    cancellation.pop("interference")
    cancellation["cancellation"] = {"status": "inconclusive", "selected_user_id": "user-02",
        "cancellation": {"status": "pass", "request_ids": ["user-02-0"], "reasons": []},
        "peer_continuation": {"status": "inconclusive", "request_ids": [], "reasons": ["No peer output across cancellation"]},
        "followup": {"status": "pass", "request_ids": ["user-02-1"], "reasons": []},
        "backend_release": "unverified", "private_prompt": "do not export"}
    summary["windows"].append(cancellation)
    return report


def test_scenario_html_and_compact_export_keep_qualified_user_evidence(tmp_path):
    report = scenario_report()
    _, html_path = write_report(report, tmp_path, html=True)
    html = html_path.read_text()
    assert "Matched noisy-neighbor comparison" in html
    assert "Heavy input / output medians: 640.0 / 32.0 tokens" in html
    assert "Missing heavy overlap" in html
    assert "Selected request cancellation" in html
    assert "Peer continuation" in html and "Same-user follow-up" in html
    assert "Backend compute and memory release: unverified" in html
    assert 'href="#request-' in html
    windows = reproduction_export(report)["multi_user"]["local"]["windows"]
    assert windows[0]["role"] == "probe"
    assert windows[0]["interference"]["users"][0]["matched_samples"] == 2
    assert windows[0]["interference"]["users"][0]["heavy_input_median_tokens"] == 640
    assert windows[0]["interference"]["users"][0]["heavy_output_median_tokens"] == 32
    assert windows[1]["cancellation"]["followup"]["request_ids"] == ["user-02-1"]
    assert "do not export" not in json.dumps(windows)


def test_user_baseline_keeps_control_probe_and_heavy_profiles_separate():
    from giraffe.reporting import _user_measurements, _compare_user_delivery
    report = scenario_report()
    summary = report.checks[0].metrics["multi_user"]
    probe = summary["windows"][0]
    control = deepcopy(probe)
    control.update(id="control", role="control")
    other_profile = deepcopy(probe)
    other_profile.update(id="other-profile", heavy_profile="short_long")
    summary["windows"] = [control, probe, other_profile]
    assert len(_user_measurements(report, "local")) == 15
    rows, _ = _compare_user_delivery(report, report, "local", [], 10)
    assert len(rows) == 15
    assert {row["role"] for row in rows} == {"control", "probe"}


@pytest.mark.parametrize("field,value", [("scenarios", ["cancellation"]),
    ("noisy_neighbors", {"heavy_profiles": ["short_long"]}), ("cancellation", {"user_id": "user-02"})])
def test_changed_scenario_work_cannot_pass_baseline(field, value):
    current, previous = user_report(), user_report()
    current.manifest["config"]["multi_user"][field] = value
    assert compare_baseline(current, previous).baseline["status"] == "inconclusive"


def test_cli_preserves_scenario_qualification_and_separate_cancellation_observations(tmp_path, capsys):
    _print_result(scenario_report(), tmp_path / "report.json", None, tmp_path, {})
    output = capsys.readouterr().out
    assert "Heavy input / output medians: 640 / 32 tokens" in output
    for text in ("Matched noisy-neighbor comparison", "Missing heavy overlap", "Peer continuation",
                 "Same-user follow-up", "Backend compute and memory release: unverified"):
        assert text in output


def test_unqualified_scenario_cannot_pass_baseline_with_complete_user_delivery():
    report = scenario_report()
    compared = compare_baseline(report, report)
    assert compared.baseline["status"] == "inconclusive"
    assert any("scenario" in reason.lower() for reason in compared.baseline["targets"][0]["reasons"])


@pytest.mark.parametrize("exposure", ["missing", "partial", "complete", "late_unmatched", "changed_heavy"])
def test_probe_timing_regression_requires_heavy_overlap_in_both_runs(exposure):
    current, baseline = scenario_report(), scenario_report()
    late_unmatched = exposure == "late_unmatched"
    for report in (current, baseline):
        windows = report.checks[0].metrics["multi_user"]["windows"]
        windows.pop()  # Compare the noisy-neighbor probe on its own.
        windows[0]["users"] = windows[0]["users"][:1]
        evidence = windows[0]["interference"]
        heavy = next(item for item in report.requests if item.id == "user-05-0").model_copy(update={"check_ids": []})
        if exposure == "changed_heavy" and report is current:
            heavy.input_tokens *= 2
        report.requests = [item for item in report.requests if item.workload["user_id"] == "user-01"]
        user_requests = list(report.requests)
        if late_unmatched:
            user_requests[-1].completed_ms = windows[0]["end_ms"] + 100
            windows[0]["users"][0]["output_tokens_per_second"] = 70
        has_overlap = report is baseline or exposure != "missing"
        if has_overlap:
            evidence.update(status="pass", qualified=True, reasons=[])
            matched_requests = user_requests[:-1] if late_unmatched else user_requests
            if report is current and exposure == "partial":
                matched_requests = matched_requests[:1]
            for item in matched_requests:
                item.workload["matched_work_sha256"] = "same"
            controls = [item.model_copy(deep=True, update={"id": f"control-{item.id}", "check_ids": []})
                        for item in matched_requests]
            evidence["users"][0].update(status="pass", qualified=True, reasons=[],
                                        matched_samples=len(matched_requests),
                                        control_request_ids=[item.id for item in controls],
                                        probe_request_ids=[item.id for item in matched_requests])
            report.requests.extend([*controls, heavy])
        for item in user_requests:
            item.workload.update(scenario="noisy_neighbors", role="probe",
                                 trigger="during_stream", heavy_profile="long_short",
                                 population=windows[0]["population"])
            if report is current and item.workload["user_id"] == "user-01":
                item.completed_ms += 100
        if exposure == "changed_heavy" and report is current:
            windows[0]["users"][0].update(status="fail", criteria={"max_completion_ms": {"status": "fail", "missed": 1}})
            report.checks[0].status = report.overall = "fail"

    compared = compare_baseline(current, baseline)
    row = next(row for row in compared.baseline["targets"][0]["comparisons"]
               if row["metric"] == "latency_ms" and row.get("user_id") == "user-01")
    assert row["current"] == 200 and row["baseline"] == 100
    assert row["status"] == ("fail" if exposure == "complete" else "inconclusive")
    delivery = next(row for row in compared.baseline["targets"][0]["comparisons"]
                    if row["metric"] == "shared_window_output_tokens_per_second" and row.get("user_id") == "user-01")
    assert delivery["current"] == delivery["baseline"] == (70 if late_unmatched else 80)
    assert delivery["status"] == ("pass" if exposure in {"complete", "late_unmatched", "changed_heavy"} else "inconclusive")
    assert compared.baseline["status"] == {"complete": "fail", "late_unmatched": "pass"}.get(exposure, "inconclusive")
    if exposure == "changed_heavy":
        acceptance = next(row for row in compared.baseline["targets"][0]["comparisons"] if row["metric"] == "user_acceptance")
        assert acceptance["status"] == "inconclusive" and "heavy work differs" in acceptance["reason"]
        assert compared.baseline["acceptance"]["current"] == "fail"


@pytest.mark.parametrize(("criterion", "expected"), [
    ("max_completion_ms", "inconclusive"), ("max_error_percent", "fail"),
])
def test_partial_probe_exposure_keeps_absolute_failure_and_qualifies_relative_acceptance(criterion, expected):
    current, baseline = scenario_report(), scenario_report()
    for report in (current, baseline):
        check = report.checks[0]
        window = check.metrics["multi_user"]["windows"][0]
        check.metrics["multi_user"]["windows"] = [window]
        user = window["users"][0]
        window["users"] = [user]
        report.requests = [item for item in report.requests if item.workload["user_id"] == user["user_id"]]
        evidence = window["interference"]
        evidence.update(status="pass", qualified=True, reasons=[])
        matched = user["request_ids"][:1] if report is current else user["request_ids"]
        evidence["users"][0].update(status="pass", qualified=True, reasons=[], probe_request_ids=matched)
        if report is current:
            user.update(status="fail", criteria={criterion: {"status": "fail", "missed": 1}})
            check.status = report.overall = "fail"

    compared = compare_baseline(current, baseline)
    acceptance = next(row for row in compared.baseline["targets"][0]["comparisons"]
                      if row["metric"] == "user_acceptance")
    assert acceptance["status"] == compared.baseline["status"] == expected
    assert compared.baseline["acceptance"]["current"] == "fail"


def test_new_interference_failure_survives_incomplete_peer_cancellation_baseline():
    current, previous = scenario_report(), scenario_report()
    for report in (current, previous):
        interference = report.checks[0].metrics["multi_user"]["windows"][0]["interference"]
        interference.update(status="pass", qualified=True, reasons=[])
        user = interference["users"][0]
        user.update(status="pass", qualified=True, reasons=[], matched_samples=1)
        for item in report.requests:
            if item.id in user["control_request_ids"] + user["probe_request_ids"]:
                item.workload["matched_work_sha256"] = "same"
    current.checks[0].metrics["multi_user"]["windows"][0]["interference"]["users"][0].update(
        status="fail", reasons=["Short user slowdown exceeds the selected limit."])
    compared = compare_baseline(current, previous)
    assert compared.baseline["status"] == "fail"
    assert any(row["metric"] == "scenario_acceptance" and row["status"] == "fail"
               for row in compared.baseline["targets"][0]["comparisons"])


@pytest.mark.parametrize(("limit", "heavy_input", "retain_heavy", "slowdown", "expected"), [
    (3, 256, True, 4.05, "fail"),
    (3, 512, True, 4.05, "inconclusive"),
    (3, 512, True, 1.5, "inconclusive"),
    (3, 256, False, 4.05, "inconclusive"),
    (None, 256, True, 4.05, "pass"),
    (None, 512, True, 1.5, "inconclusive"),
    (None, 256, False, 1.5, "inconclusive"),
])
def test_interference_baseline_attributes_matched_slowdown_only_to_short_user(
    limit, heavy_input, retain_heavy, slowdown, expected,
):
    from tests.test_multi_user_scenarios import matched_observation

    current, baseline = user_report(), user_report()
    for report in (current, baseline):
        source, requests = matched_observation()
        source["settings"]["noisy_neighbors"]["max_slowdown"] = limit
        report.manifest["config"]["multi_user"].update(source["settings"])
        for item in requests:
            item.target = "local"
            window = next(window for window in source["windows"] if window["id"] == item.workload["window_id"])
            item.workload.update(scenario=window["scenario"], trigger=window["trigger"],
                                 heavy_profile=window["heavy_profile"], population=window["population"])
        if report is current:
            requests[1].completed_ms = requests[1].workload["ready_ms"] + 200 * slowdown
            requests[1].elapsed_ms = requests[1].completed_ms - requests[1].dispatch_ms
            requests[2].input_tokens = heavy_input
        summary = summarize(source, requests)
        interference = summary["windows"][1]["interference"]
        short, = interference["users"]
        assert short["slowdown"] == pytest.approx(slowdown if report is current else 1.5)
        assert short["qualified"]
        assert interference["status"] == (
            "measured" if limit is None else "fail" if report is current and slowdown > limit else "pass")
        report.requests = requests if report is baseline or retain_heavy else requests[:2]
        report.checks[0].metrics["multi_user"] = summary
        report.checks[0].status = report.overall = summary["status"]

    compared = compare_baseline(current, baseline).baseline
    target = compared["targets"][0]
    rows = target["comparisons"]
    assert compared["status"] == target["status"] == expected
    assert compared["acceptance"]["current"] == ("fail" if limit is not None and slowdown > limit else "pass")
    acceptance = [row for row in rows if row["metric"] == "scenario_acceptance"]
    measured = [row for row in rows if row["metric"] == "matched_interference_slowdown"]
    if limit is None:
        assert not acceptance
        delivery, = [row for row in rows if row["metric"] == "shared_window_output_tokens_per_second"
                     and row["role"] == "probe" and row["user_id"] == "user-01"]
        assert delivery["status"] == "pass"
        if expected == "pass":
            assert not measured
            return
        interference, = measured
        assert interference["current"] == pytest.approx(slowdown) and interference["baseline"] == 1.5
        assert interference["threshold"] is None and "worsening" not in interference
        assert "Matched heavy" in interference["reason"]
        assert interference["reason"] in target["reasons"]
    else:
        interference, = acceptance
        assert not measured
    assert interference["user_id"] == "user-01" and interference["status"] == expected
    assert interference["current_samples"] == interference["baseline_samples"] == 1
    assert interference["evidence_ids"] == [item.id for item in current.requests]



@pytest.mark.parametrize("limit", [None, 3])
@pytest.mark.parametrize("case", ["extra_exposure", "extra_pair", "legacy_missing"])
def test_baseline_compares_heavy_work_per_matched_probe(limit, case):
    from tests.test_multi_user_scenarios import matched_observation

    current, baseline = user_report(), user_report()
    extra_exposure = case == "extra_exposure"
    for report in (current, baseline):
        source, requests = matched_observation()
        source["settings"]["noisy_neighbors"]["max_slowdown"] = limit
        report.manifest["config"]["multi_user"].update(source["settings"])
        control, probe, heavy = requests

        def later(item, delay, sequence):
            clone = item.model_copy(deep=True, update={"id": f"{item.id}-later"})
            for field in ("dispatch_ms", "timing_start_ms", "request_body_sent_ms", "completed_ms"):
                setattr(clone, field, getattr(item, field) + delay)
            clone.workload.update(ready_ms=item.workload["ready_ms"] + delay,
                                  matched_work_sequence=sequence, request_sequence=sequence)
            if "trigger_ms" in item.workload:
                clone.workload["trigger_ms"] += delay
            return clone

        if report is current:
            if extra_exposure:
                # The same short response spans two sequential heavy requests.
                heavy.completed_ms = heavy.dispatch_ms + 110
                heavy.elapsed_ms = heavy.stream_end_ms = 110
                heavy.last_output_ms = 105
                requests.append(later(heavy, 120, 2))
                probe.completed_ms = probe.workload["ready_ms"] + 810
                probe.elapsed_ms = probe.completed_ms - probe.dispatch_ms
            else:
                requests.extend(later(item, 500, 2) for item in (control, probe, heavy))
        for window in source["windows"]:
            for user in window["users"]:
                cohort = [item for item in requests if item.workload["window_id"] == window["id"]
                          and item.workload["user_id"] == user["user_id"]]
                user.update(ready_count=len(cohort), dispatched_count=len(cohort), request_ids=[item.id for item in cohort])
                for item in cohort:
                    item.target = "local"
                    item.workload.update(scenario=window["scenario"], trigger=window["trigger"],
                                         heavy_profile=window["heavy_profile"], population=window["population"])
        summary = summarize(source, requests)
        short, = summary["windows"][1]["interference"]["users"]
        assert short["qualified"], short["reasons"]
        assert short["matched_samples"] == (2 if report is current and not extra_exposure else 1)
        assert len(short["heavy_request_ids"]) == (2 if report is current else 1)
        assert short["status"] == ("measured" if limit is None else "fail"
                                   if report is current and extra_exposure else "pass")
        if report is baseline or case == "legacy_missing":
            short.pop("probe_heavy_request_ids")
        report.requests = requests
        report.checks[0].metrics["multi_user"] = summary
        report.checks[0].status = report.overall = summary["status"]
        if report is current and case != "legacy_missing":
            exported = reproduction_export(report)["multi_user"]["local"]["windows"][1]["interference"]["users"][0]
            expected = {probe.id: [heavy.id, requests[-1].id]} if extra_exposure else {
                probe.id: [heavy.id], requests[-2].id: [requests[-1].id]}
            assert exported["probe_heavy_request_ids"] == expected

    compared = compare_baseline(current, baseline).baseline
    target, = compared["targets"]
    assert compared["status"] == target["status"] == ("pass" if case == "extra_pair" else "inconclusive")
    assert compared["acceptance"]["current"] == ("fail" if limit is not None and extra_exposure else "pass")
    delivery, = [row for row in target["comparisons"] if row["metric"] == "shared_window_output_tokens_per_second"
                 and row["role"] == "probe" and row["user_id"] == "user-01"]
    assert delivery["status"] == "pass"
    if case != "extra_pair":
        metric = "matched_interference_slowdown" if limit is None else "scenario_acceptance"
        comparison, = [row for row in target["comparisons"] if row["metric"] == metric]
        assert comparison["status"] == "inconclusive"
        reason = ("exposure per matched probe differs" if extra_exposure
                  else "exposure per probe is missing or incomplete")
        assert reason in comparison["reason"]
        assert set(comparison["evidence_ids"]) == {item.id for item in current.requests}



@pytest.mark.parametrize("limit", [None, 3])
@pytest.mark.parametrize("case", ["swapped_exposure", "swapped_short_work", "extra_repetitions", "missing_work"])
def test_baseline_compares_competitor_exposure_for_the_same_short_work(limit, case):
    from tests.test_multi_user_scenarios import matched_observation

    current, baseline = user_report(), user_report()
    slow_case = case in {"swapped_exposure", "swapped_short_work"}
    for report in (current, baseline):
        source, templates = matched_observation()
        source["settings"].update(duration_seconds=4)
        source["settings"]["noisy_neighbors"]["max_slowdown"] = limit
        report.manifest["config"]["multi_user"].update(source["settings"])
        control_window, probe_window = source["windows"]
        for start, window in zip((0, 4000), source["windows"]):
            window.update(start_ms=start, end_ms=start + 4000, planned_end_ms=start + 4000,
                          duration_seconds=4, planned_duration_seconds=4)
            for user in window["users"]:
                user.update(activated_ms=start, ready_count=0, dispatched_count=0, request_ids=[])

        def add(template, window, sequence, dispatch, elapsed, **fields):
            item = template.model_copy(deep=True, update={
                "id": f"{template.id}-{sequence}-{dispatch}", "target": "local",
                "dispatch_ms": dispatch, "timing_start_ms": dispatch, "request_body_sent_ms": dispatch,
                "completed_ms": dispatch + elapsed, "elapsed_ms": elapsed, "stream_end_ms": elapsed,
                "last_output_ms": elapsed - 5, **fields,
            })
            item.workload.update(scenario=window["scenario"], trigger=window["trigger"],
                                 heavy_profile=window["heavy_profile"], population=window["population"],
                                 request_sequence=sequence, matched_work_sequence=sequence, ready_ms=dispatch)
            user = next(user for user in window["users"] if user["user_id"] == item.workload["user_id"])
            user["ready_count"] += 1
            user["dispatched_count"] += 1
            user["request_ids"].append(item.id)
            requests.append(item)
            return item

        requests = []
        repeat = report is current and case == "extra_repetitions"
        for sequence, work in enumerate("BABA" if repeat else "AB", 1):
            offset = 100 + (sequence - 1) * 900
            changed_work = report is current and case == "swapped_short_work"
            tokens = 8 if (work == "A") != changed_work else 16
            elapsed = 850 if report is current and slow_case else 350
            control = add(templates[0], control_window, sequence, offset, 200,
                          fixture_id=work, output_tokens=tokens)
            probe = add(templates[1], probe_window, sequence, 4000 + offset, elapsed,
                        fixture_id=work, output_tokens=tokens)
            control.workload["matched_work_sha256"] = probe.workload["matched_work_sha256"] = work
            swapped_exposure = report is current and case == "swapped_exposure"
            heavy_count = 2 if (work == "A") != swapped_exposure else 1
            for index in range(heavy_count):
                competitor = add(templates[2], probe_window, sequence, 4070 + offset + 120 * index, 110)
                competitor.workload["trigger_ms"] = competitor.dispatch_ms

        summary = summarize(source, requests)
        short, = summary["windows"][1]["interference"]["users"]
        assert short["qualified"], short["reasons"]
        assert short["matched_samples"] == (4 if repeat else 2)
        assert len(short["heavy_request_ids"]) == (6 if repeat else 3)
        assert short["slowdown"] == (4.25 if report is current and slow_case else 1.75)
        if report is current and case == "missing_work":
            probe.workload.pop("matched_work_sha256")
        report.requests = requests
        report.checks[0].metrics["multi_user"] = summary
        report.checks[0].status = report.overall = summary["status"]

    compared = compare_baseline(current, baseline).baseline
    target, = compared["targets"]
    comparable = case == "extra_repetitions"
    assert compared["status"] == target["status"] == ("pass" if comparable else "inconclusive")
    assert compared["acceptance"]["current"] == ("fail" if limit is not None and slow_case else "pass")
    delivery, = [row for row in target["comparisons"] if row["metric"] == "shared_window_output_tokens_per_second"
                 and row["role"] == "probe" and row["user_id"] == "user-01"]
    assert delivery["status"] == "pass"
    assert delivery["timing_exposure_qualified"] == comparable
    if not comparable:
        metric = "matched_interference_slowdown" if limit is None else "scenario_acceptance"
        comparison, = [row for row in target["comparisons"] if row["metric"] == metric]
        assert comparison["status"] == "inconclusive"
        assert "logical short work" in comparison["reason"]
        assert set(comparison["evidence_ids"]) == {item.id for item in current.requests}


def test_intentional_user_cancellation_is_not_an_ordinary_baseline_sample():
    report = scenario_report()
    window = report.checks[0].metrics["multi_user"]["windows"][-1]
    report.checks[0].metrics["multi_user"]["windows"] = [window]
    window["users"] = [window["users"][1]]
    report.requests = [item for item in report.requests if item.workload["user_id"] == "user-02"][:5]
    for item in report.requests:
        item.workload.update(scenario="cancellation", role="cancellation", window_id=window["id"],
                             population=window["population"], cancellation_role="followup")
    cancelled = report.requests[0]
    cancelled.status, cancelled.valid, cancelled.intentional = "cancelled", False, True
    cancelled.termination_reason = "generation_chunk_limit"
    cancelled.workload["cancellation_role"] = "selected"
    window["users"][0].update(request_ids=[item.id for item in report.requests], output_tokens_per_second=40)

    compared = compare_baseline(report, report).baseline
    rows = {row["metric"]: row for row in compared["targets"][0]["comparisons"] if row.get("user_id") == "user-02"}
    for metric, value in (("valid_completion_rate", 1), ("error_rate", 0)):
        assert rows[metric]["current"] == rows[metric]["baseline"] == value
        assert rows[metric]["current_samples"] == rows[metric]["baseline_samples"] == 4
        assert rows[metric]["status"] == "inconclusive"
        assert cancelled.id not in rows[metric]["evidence_ids"]
    exported_window, = reproduction_export(report)["multi_user"]["local"]["windows"]
    assert exported_window["cancellation"]["cancellation"]["request_ids"] == [cancelled.id]


@pytest.mark.parametrize("qualified", [True, False])
def test_failed_followup_baseline_is_scoped_to_selected_user_with_scenario_evidence(qualified):
    current, baseline = scenario_report(), scenario_report()
    for report in (current, baseline):
        check = report.checks[0]
        window = check.metrics["multi_user"]["windows"][-1]
        check.metrics["multi_user"]["windows"] = [window]
        evidence = window["cancellation"]
        failed = report is current
        evidence["status"] = evidence["followup"]["status"] = "fail" if failed else "pass"
        if not failed or qualified:
            evidence["peer_continuation"].update(status="pass", request_ids=["user-01-0"], reasons=[])
        report.requests = [item for item in report.requests
                           if item.workload["user_id"] != "user-02" or item.id in {"user-02-0", "user-02-1"}]
        for item in report.requests:
            item.workload.update(scenario="cancellation", role="cancellation", window_id=window["id"],
                                 population=window["population"])
            if item.id == "user-02-0":
                item.status, item.valid, item.intentional = "cancelled", False, True
            elif failed and item.id == "user-02-1":
                item.status, item.valid = "failed", False
        user = window["users"][1]
        user.update(request_ids=["user-02-0", "user-02-1"], output_tokens_per_second=0 if failed else 10)
        user["outcomes"]["attempted"] = 2
        check.status = report.overall = "fail" if failed else "pass"

    compared = compare_baseline(current, baseline).baseline
    rows = compared["targets"][0]["comparisons"]
    failures = [row for row in rows if row["metric"] == "scenario_acceptance"]
    if qualified:
        failure, = failures
        assert failure["status"] == "fail" and failure["user_id"] == "user-02"
        assert failure["evidence_ids"] == ["user-02-0", "user-01-0", "user-02-1"]
        assert failure["current_samples"] == failure["baseline_samples"] == 3
    else:
        assert not failures
    assert compared["acceptance"]["current"] == "fail"
    assert any(row["user_id"] == "user-01" and row["metric"] == "shared_window_output_tokens_per_second"
               and row["status"] == "pass" for row in rows)
