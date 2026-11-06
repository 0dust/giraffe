import pytest

from giraffe.reporting import _compare_metric, compare_baseline
from tests.test_reporting import baseline_check, make_report


def test_lower_bound_equal_to_regression_threshold_is_material():
    compared = compare_baseline(make_report(latency=120), make_report("baseline", latency=100))

    latency = next(row for row in compared.baseline["targets"][0]["comparisons"]
                   if row["metric"] == "latency_ms")
    assert latency["worsening_interval_95"] == [20, 20]
    assert latency["threshold"] == 20
    assert latency["status"] == "fail"
    assert latency["reason"] == "Material worsening exceeds observed sample uncertainty."
    assert baseline_check(compared).status == compared.overall == "fail"


def test_tail_latency_comparison_catches_p95_regression_with_stable_median():
    current = [50] * 85 + [500] * 15
    baseline = [50] * 100
    median = _compare_metric("latency_ms", current, baseline, 5, 20)
    tail = _compare_metric("latency_p95_ms", current, baseline, 5, 20)

    assert median["status"] == "pass"
    assert tail["aggregation"] == "p95"
    assert tail["status"] == "fail"


@pytest.mark.parametrize("current_latency,status,reason", [
    (0, "pass", "Both measurements are zero."),
    (10, "inconclusive", "A zero baseline does not support a relative timing comparison."),
], ids=["both-zero", "nonzero-current"])
def test_zero_baseline_timing_comparison(current_latency, status, reason):
    compared = compare_baseline(make_report(latency=current_latency), make_report("baseline", latency=0))

    latency = next(row for row in compared.baseline["targets"][0]["comparisons"]
                   if row["metric"] == "latency_ms")
    assert latency["current"] == current_latency
    assert latency["baseline"] == 0
    assert latency["status"] == status
    assert latency["reason"] == reason
    assert latency["observed_change"] is None
    assert "worsening_interval_95" not in latency
    if current_latency == 0:
        assert latency["worsening"] == 0
    else:
        assert "worsening" not in latency
