from copy import deepcopy

import pytest

from giraffe.models import CheckResult, RequestRecord, RunConfig, RunReport, Target
from giraffe.reporting import compare_baseline, write_report


def traffic_report(run_id="current", *, high_failure=None):
    config = RunConfig(
        targets=[Target(name="local", url="http://localhost:8000", model="test")],
        checks=["capacity"], traffic={"rates": [1, 4], "duration_seconds": 5},
        limits={"latency_ms": 150},
    )
    records, stages = [], []
    for index, rate in enumerate(config.traffic.rates):
        count = int(rate * config.traffic.duration_seconds)
        failed = index == 1 and high_failure is not None
        latency = 200 if failed and high_failure == "latency" else 100
        correct = not (failed and high_failure == "correctness")
        stages.append({
            "rate_rps": rate, "duration_seconds": 5, "scheduled": count, "started": count,
            "completed": count, "failed": 0, "timed_out": 0, "cancelled": 0,
            "dropped_local": 0, "dropped_late": 0, "not_offered": 0,
            "achieved_rps": rate, "goodput_rps": 0 if failed else rate,
            "good": 0 if failed else count, "good_fraction": 0 if failed else 1,
            "correctness": float(correct), "error_rate": 0,
            "fully_offered": True, "generator_limited": False,
            "accepted": not failed, "status": "fail" if failed else "pass",
            "reasons": ["Configured criteria failed"] if failed else [],
            "dispatch_lag_p95_ms": 1, "latency_p95_ms": latency,
            "arrival_first_output_p95_ms": latency / 2 + 1,
            "arrival_latency_p95_ms": latency + 1,
            "windows": [{"scheduled": count}],
        })
        records.extend(RequestRecord(
            id=f"{run_id}-{index}-{number}", target="local", fixture_id="traffic-short-v1",
            scenario=f"traffic-{index}", check_ids=["capacity"], traffic_stage=index,
            started_at="2026-10-09T00:00:00Z", status="completed", http_status=200,
            elapsed_ms=latency, first_output_ms=latency / 2, output="42", output_chars=2,
            output_tokens=1, valid=True, score=correct, input_chars=30, requested_max_tokens=128,
        ) for number in range(count))
    return RunReport(
        run_id=run_id, suite_version="0.2.0", started_at="2026-10-09T00:00:00Z",
        finished_at="2026-10-09T00:00:10Z", overall="fail" if high_failure else "pass",
        manifest={"config": config.model_dump(), "fixture_pack": {"version": "0.2.0"},
                  "run_location": {"hostname": "test"},
                  "load_shape": {"arrival_rates_rps": [1, 4], "arrival": "steady"},
                  "effective_checks": {"capacity": config.effective_check("capacity")},
                  "effective_traffic": config.effective_traffic().model_dump()},
        checks=[CheckResult(
            id="capacity", target="local", title="Traffic capacity",
            status="fail" if high_failure else "pass", summary="Tested arrival rates",
            metrics={"traffic_stages": stages, "highest_acceptable_rate_rps": 1 if high_failure else 4},
        )], requests=records,
    )


def comparison(report):
    return report.baseline["targets"][0]["traffic_capacity"]


def test_matching_traffic_only_run_compares_without_generic_scenarios():
    current, old = traffic_report(), traffic_report("baseline")
    result = compare_baseline(current, old)
    assert result.baseline["status"] == "pass"
    assert comparison(result)["delta_rps"] == 0
    assert result.baseline["targets"][0]["comparisons"] == []
    assert current.baseline is None


@pytest.mark.parametrize("failure", ["latency", "correctness"])
def test_lost_qualified_rate_shows_capacity_drop_with_request_evidence(failure):
    result = compare_baseline(traffic_report(high_failure=failure), traffic_report("baseline"))
    traffic = comparison(result)
    assert result.baseline["status"] == traffic["status"] == "fail"
    assert traffic["baseline_highest_acceptable_rate_rps"] == 4
    assert traffic["current_highest_acceptable_rate_rps"] == 1
    assert traffic["delta_rps"] == -3
    assert traffic["change_percent"] == -75
    assert traffic["stages"][1]["goodput_delta_rps"] == -4
    assert next(check for check in result.checks if check.id == "baseline").evidence_ids


def test_no_acceptable_rate_does_not_invent_zero_capacity():
    current = traffic_report(high_failure="correctness")
    for stage in current.checks[0].metrics["traffic_stages"]:
        stage.update(accepted=False, status="fail", goodput_rps=0)
    current.checks[0].metrics["highest_acceptable_rate_rps"] = None
    result = compare_baseline(current, traffic_report("baseline"))
    assert comparison(result)["status"] == "fail"
    assert comparison(result)["current_highest_acceptable_rate_rps"] is None
    assert comparison(result)["delta_rps"] is None
    assert comparison(result)["change_percent"] is None


@pytest.mark.parametrize("change", ["mix", "seed", "rates", "duration", "limits", "test_options", "effective"])
def test_changed_planned_work_or_criteria_are_not_capacity_regressions(change):
    current, old = traffic_report(high_failure="latency"), traffic_report("baseline")
    config = old.manifest["config"]
    if change == "mix":
        config["traffic"]["mix"] = {"short": 1, "long_input": 0, "long_output": 0}
    elif change == "seed":
        config["traffic"]["seed"] += 1
    elif change == "rates":
        config["traffic"]["rates"] = [1, 2]
    elif change == "duration":
        config["traffic"]["duration_seconds"] = 20
    elif change == "limits":
        config["limits"]["latency_ms"] = 200
    elif change == "test_options":
        config["test_options"] = {"capacity": {"limits": {"latency_ms": 200}}}
        old.manifest["effective_checks"]["capacity"]["limits"]["latency_ms"] = 200
    else:
        old.manifest.pop("effective_checks")
    if change in {"mix", "seed", "rates", "duration"}:
        old.manifest["effective_traffic"] = deepcopy(config["traffic"])
    result = compare_baseline(current, old)
    assert result.baseline["status"] == "inconclusive"
    assert comparison(result)["status"] == "inconclusive"
    assert comparison(result)["stages"] == []
    assert comparison(result)["delta_rps"] is None


def test_disabled_customizations_and_explicit_defaults_do_not_change_executed_traffic():
    current, old = traffic_report(), traffic_report("baseline")
    current.manifest["config"]["test_options"] = {"correctness": {"samples": 99},
                                                   "capacity": {"limits": {"latency_ms": 150}}}
    current.manifest["config"]["traffic"]["max_in_flight"] = 4
    result = compare_baseline(current, old)
    assert result.baseline["status"] == "pass"
    assert comparison(result)["delta_rps"] == 0


@pytest.mark.parametrize("failure,expected", [(None, "pass"), ("latency", "fail")])
def test_disabled_custom_fixture_drafts_need_no_hash_for_traffic_comparison(failure, expected):
    current, old = traffic_report(high_failure=failure), traffic_report("baseline")
    for report, path in ((current, "/missing/current-draft.json"), (old, "/missing/old-draft.json")):
        report.manifest["config"]["custom_fixtures"] = path
        report.manifest["fixture_pack"].update(custom=path, custom_sha256=None)
    result = compare_baseline(current, old)
    assert result.baseline["status"] == expected
    assert comparison(result)["status"] == expected
    assert comparison(result)["delta_rps"] == (-3 if failure else 0)


def test_matching_partial_stage_lists_are_not_a_complete_capacity_comparison():
    current, old = traffic_report(), traffic_report("baseline")
    for report in (current, old):
        report.checks[0].metrics["traffic_stages"].pop()
        report.checks[0].metrics["highest_acceptable_rate_rps"] = 1
    result = compare_baseline(current, old)
    assert result.baseline["status"] == "inconclusive"
    assert "complete set" in comparison(result)["reason"]
    assert comparison(result)["delta_rps"] is None


@pytest.mark.parametrize("reason", ["local", "late", "not_offered", "samples"])
def test_incomplete_or_generator_limited_stages_cannot_establish_delta(reason):
    current = traffic_report()
    stage = current.checks[0].metrics["traffic_stages"][1]
    stage.update(status="inconclusive", accepted=False)
    if reason != "samples":
        stage["fully_offered"] = False
        stage["generator_limited"] = reason in {"local", "late"}
        stage[{"local": "dropped_local", "late": "dropped_late", "not_offered": "not_offered"}[reason]] = 1
    current.checks[0].metrics["highest_acceptable_rate_rps"] = 1
    result = compare_baseline(current, traffic_report("baseline"))
    assert result.baseline["status"] == "inconclusive"
    assert comparison(result)["delta_rps"] is None
    assert comparison(result)["stages"][1]["status"] == "inconclusive"


def test_observed_request_mix_does_not_hide_an_upgrade_failure():
    current = traffic_report(high_failure="correctness")
    # Different admitted/completed shapes are outcomes, not a change to the planned workload.
    for record in current.requests:
        if record.traffic_stage == 1:
            record.fixture_id = "traffic-long-output-v1"
            record.output_chars = 200
            record.output_tokens = 50
    result = compare_baseline(current, traffic_report("baseline"))
    traffic = comparison(result)
    assert traffic["status"] == "fail"
    assert traffic["delta_rps"] == -3
    assert traffic["stages"][1]["timing_comparable"] is False
    assert "output work differs" in traffic["stages"][1]["timing_comparison_reason"]


def test_legacy_concurrency_report_stays_readable_but_cannot_compare_arrival_capacity(tmp_path):
    old = traffic_report("baseline")
    old.suite_version = "0.1.3"
    old.checks[0].metrics = {"highest_tested_acceptable_concurrency": 4}
    _, html = write_report(old, tmp_path)
    assert "highest tested acceptable concurrency: 4" in html.read_text()
    result = compare_baseline(traffic_report(), old)
    assert result.baseline["status"] == "inconclusive"
    assert comparison(result)["delta_rps"] is None


def test_portable_report_explains_traffic_accounting_and_capacity_change(tmp_path):
    result = compare_baseline(traffic_report(high_failure="latency"), traffic_report("baseline"))
    original = deepcopy(result)
    _, html = write_report(result, tmp_path)
    content = html.read_text()
    assert "highest tested acceptable arrival rate: 1 requests/s" in content
    assert "Scheduled / sent" in content
    assert "Local / late / not offered" in content
    assert "Cohort goodput" in content
    assert "internal server queue time" in content
    assert "Change: -3 requests/s (-75.0%)" in content
    assert "Arrival cohort windows" in content
    assert result == original
