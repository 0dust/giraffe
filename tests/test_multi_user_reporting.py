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
                                         ("conversations", {"modes": ["live"]}),
                                         ("arrivals", {"requests_per_second": 2}),
                                         ("recovery", {"lighter_seconds": 9})])
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


def test_recovery_report_export_cli_and_baseline_keep_all_phase_users(tmp_path, capsys):
    from tests.test_multi_user_recovery import recovery_observation
    from giraffe.reporting import _user_measurements
    source, records = recovery_observation()
    report = make_report()
    for r in records:
        r.target = "local"
    report.requests = records
    report.manifest['config'].update(checks=['multi_user'], multi_user=source['settings'])
    summary = summarize(source, records)
    report.checks = [CheckResult(id='multi_user', target='local', title='Multi-user', status=summary['status'],
                                summary='Per-user recovery', metrics={'multi_user': summary})]
    _, path = write_report(report, tmp_path, html=True)
    html = path.read_text()
    assert 'Matched per-user recovery' in html and 'user-03' in html and 'Recovery slowdown' in html
    assert 'Scheduled and achieved traffic' in html and 'not server queue time' in html
    exported = reproduction_export(report)['multi_user']['local']
    assert [w['role'] for w in exported['windows']] == ['control', 'sustained', 'lighter']
    assert exported['windows'][2]['recovery']['users'][2]['slowdown'] == 3
    assert len(_user_measurements(report, 'local')) == 9
    _print_result(report, tmp_path/'report.json', path, tmp_path, {})
    output = capsys.readouterr().out
    assert 'Matched per-user recovery: fail' in output and 'user-03: fail' in output


def test_finite_user_request_comparisons_keep_phase_and_cohort():
    from tests.test_multi_user_recovery import recovery_observation
    from giraffe.reporting import _groups
    source, records = recovery_observation()
    for r in records:
        r.traffic_stage = 0
        r.workload['cohort'] = 'shared'
    groups = _groups(records, 'service')
    assert len(groups) == 9
    assert all(len(rows) == 1 for rows in groups.values())
    assert all('shared' in scenario for check, scenario in groups)
    assert {r.workload['role'] for rows in groups.values() for r in rows} == {'control', 'sustained', 'lighter'}


def test_matching_user_baseline_preserves_per_user_delivery_rows():
    result = compare_baseline(user_report(), user_report())
    rows = result.baseline["targets"][0]["comparisons"]
    rates = [row for row in rows if row["metric"] == "shared_window_output_tokens_per_second"]
    assert result.baseline["status"] == "pass"
    assert len(rates) == 5 and all(row["current"] == 80 for row in rates)
    assert all("worsening_interval_95" not in row for row in rates)


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
            "probe_output_median_tokens": 95, "private_prompt": "do not export"}]}
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
    assert "Missing heavy overlap" in html
    assert "Selected request cancellation" in html
    assert "Peer continuation" in html and "Same-user follow-up" in html
    assert "Backend compute and memory release: unverified" in html
    assert 'href="#request-' in html
    windows = reproduction_export(report)["multi_user"]["local"]["windows"]
    assert windows[0]["role"] == "probe"
    assert windows[0]["interference"]["users"][0]["matched_samples"] == 2
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
    for text in ("Matched noisy-neighbor comparison", "Missing heavy overlap", "Peer continuation",
                 "Same-user follow-up", "Backend compute and memory release: unverified"):
        assert text in output


def test_unqualified_scenario_cannot_pass_baseline_with_complete_user_delivery():
    report = scenario_report()
    compared = compare_baseline(report, report)
    assert compared.baseline["status"] == "inconclusive"
    assert any("scenario" in reason.lower() for reason in compared.baseline["targets"][0]["reasons"])


def test_new_interference_failure_survives_incomplete_peer_cancellation_baseline():
    current, previous = scenario_report(), scenario_report()
    for report in (current, previous):
        interference = report.checks[0].metrics["multi_user"]["windows"][0]["interference"]
        interference.update(status="pass", qualified=True, reasons=[])
        interference["users"][0].update(status="pass", qualified=True, reasons=[])
    current.checks[0].metrics["multi_user"]["windows"][0]["interference"]["users"][0].update(
        status="fail", reasons=["Short user slowdown exceeds the selected limit."])
    compared = compare_baseline(current, previous)
    assert compared.baseline["status"] == "fail"
    assert any(row["metric"] == "scenario_acceptance" and row["status"] == "fail"
               for row in compared.baseline["targets"][0]["comparisons"])
