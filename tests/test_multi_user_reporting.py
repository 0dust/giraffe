"""Persistent users remain identifiable across retained reports and comparisons."""
import json

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


def live_user_report():
    report = user_report()
    window = report.checks[0].metrics["multi_user"]["windows"][0]
    window["scenario"] = "repeated_conversations"
    for user in window["users"]:
        user["profile"] = "conversation:chat"
    for request in report.requests:
        turn = int(request.id.rsplit("-", 1)[1]) % 2
        request.workload.update(mode="live", profile="conversation:chat", turn_id=turn,
                                scenario="repeated_conversations")
        request.fixture_id = f"session_live_turn{turn}"
        request.request_hash = f"same-turn-{turn}"
        request.finish_reason = "stop"
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


def test_same_live_history_mix_can_report_a_real_throughput_regression():
    current, previous = live_user_report(), live_user_report()
    user = current.checks[0].metrics["multi_user"]["windows"][0]["users"][-1]
    unserved = set(user["request_ids"][4:])
    current.requests = [request for request in current.requests if request.id not in unserved]
    user["request_ids"] = user["request_ids"][:4]
    user["outcomes"]["valid_completed"] = 4
    user["output_tokens_per_second"] = 40
    result = compare_baseline(current, previous)
    row = next(row for row in result.baseline["targets"][0]["comparisons"]
               if row.get("user_id") == "user-05" and row["metric"] == "shared_window_output_tokens_per_second")
    assert row["status"] == result.baseline["status"] == "fail"
    assert row["current"] == 40 and row["baseline"] == 80


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
