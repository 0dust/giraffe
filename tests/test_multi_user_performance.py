import pytest

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
