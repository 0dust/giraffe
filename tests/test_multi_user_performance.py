import pytest

from giraffe.models import CheckResult
from giraffe.multi_user_metrics import summarize
from giraffe.performance import build_performance
from tests.test_multi_user_reporting import user_report


@pytest.mark.parametrize("scenarios", [
    ["multi_user_2_arrivals", "multi_user_3_arrivals"],
    ["multi_user_2_recovery_control", "multi_user_2_recovery_lighter"],
    ["multi_user_2_neighbors_long_short_before_output_probe",
     "multi_user_2_neighbors_short_long_during_stream_probe"],
])
def test_scenario_measurement_labels_preserve_condition_identity(scenarios):
    from giraffe.conditions import condition_for
    from giraffe.performance import _cohort_label
    from tests.test_multi_user_metrics import record

    labels = []
    for scenario in scenarios:
        request = record("user-01", scenario=scenario)
        request.traffic_stage = 0
        request.workload.update(role=scenario.rsplit("_", 1)[-1],
                                scenario="noisy_neighbors" if "neighbors" in scenario else
                                         "recovery" if "recovery" in scenario else "arrivals",
                                population=int(scenario.split("_")[2]))
        label = _cohort_label([request], {})[1]
        assert label == condition_for(scenario, request.workload)["label"]
        labels.append(label)
    assert len(set(labels)) == len(scenarios)


def test_saved_multi_user_stage_numbers_do_not_join_capacity_baselines():
    from giraffe.reporting import compare_baseline
    from tests.test_traffic_reporting import traffic_report

    current, baseline = traffic_report("current"), traffic_report("baseline")
    user_window = "multi_user_5_arrivals"
    for report, tokens in ((current, 64), (baseline, 10)):
        for row in report.requests:
            row.output_tokens = 10
        arrivals = user_report().requests
        for row in arrivals:
            row.scenario = user_window
            row.traffic_stage = 0
            row.workload.update(window_id=user_window, scenario="arrivals", role="arrivals")
            row.output_tokens = tokens
        report.requests.extend(arrivals)

    compared = compare_baseline(current, baseline)
    stage = compared.baseline["targets"][0]["traffic_capacity"]["stages"][0]
    assert stage["evidence_ids"] == [r.id for r in current.requests if "capacity" in r.check_ids and r.traffic_stage == 0]
    assert stage["timing_comparable"]
    assert stage["output_work"]["current"] == stage["output_work"]["baseline"]

    scope = f"full:{user_window}"
    data = build_performance(compared, window=scope)["targets"][0]["summaries"][scope]
    assert data["metrics"]["baseline_capacity"]["value"] is None
    assert data["metrics"]["baseline_throughput"]["value"] is None


def test_scheduled_user_and_capacity_windows_keep_separate_offer_ledgers():
    from tests.test_multi_user_metrics import record

    report = user_report()
    user_window = "multi_user_5_arrivals"
    for request in report.requests:
        request.scenario = user_window
        request.traffic_stage = 0
        request.workload.update(window_id=user_window, scenario="arrivals", role="arrivals")
    summary_window = report.checks[0].metrics["multi_user"]["windows"][0]
    summary_window.update(id=user_window, scenario="arrivals", offer_accounting={
        "planned": 48, "offered": 47, "dispatched": 40, "dropped_local": 3,
        "dropped_late": 2, "unissued": 2, "not_offered": 1})
    capacity_request = record("user-01", identity="capacity", scenario="traffic_stage_0",
                              target="local", check_ids=["capacity"], traffic_stage=0, workload={})
    report.requests.append(capacity_request)
    report.manifest["config"]["checks"].append("capacity")
    report.checks.append(CheckResult(id="capacity", target="local", title="Capacity", status="pass",
        summary="Capacity completed.", metrics={"traffic_stages": [{
            "stage_index": 0, "rate_rps": 50, "accepted": True, "dropped_local": 25,
        }]}))

    def metrics(window):
        target = build_performance(report, window=window)["targets"][0]
        return target["summaries"][window]["metrics"]

    arrivals = metrics(f"full:{user_window}")
    drops, = arrivals["drops"]["value"]
    assert drops["dropped_local"] == 3
    assert drops["dropped_late"] == 2
    assert drops["unissued"] == 2
    assert drops["not_offered"] == 1
    assert arrivals["highest_acceptable_rate"]["value"] is None
    capacity = metrics("full:traffic:check=capacity")
    assert capacity["drops"]["value"][0]["dropped_local"] == 25
    assert capacity["highest_acceptable_rate"]["value"] == 50


@pytest.mark.parametrize("missing_users", [0, 1, 5])
def test_delivery_envelope_counts_known_zero_rates_and_missing_usage(missing_users):
    report = user_report()
    missing = {f"user-{index:02}" for index in range(1, missing_users + 1)}
    for record in report.requests:
        record.output_tokens = None if record.workload["user_id"] in missing else 0
    source = report.observations["targets"]["local"]["multi_user"]
    report.checks[0].metrics["multi_user"] = summarize(source, report.requests)

    target = build_performance(report)["targets"][0]
    delivery = target["summaries"][target["default_window"]]["metrics"]["persistent_user_delivery"]

    assert len(delivery["value"]) == 5
    assert delivery["counts"] == {"sample": 5 - missing_users, "eligible": 5, "missing": missing_users}
    assert delivery["sample_count"] == 5 - missing_users
    assert delivery["eligible_count"] == 5
    assert delivery["state"] == ("insufficient_evidence" if missing_users else "measured")
    assert bool(delivery["reason"]) == bool(missing_users)
    assert {row["user_id"] for row in delivery["value"] if row["output_tokens_per_second"] is None} == missing
