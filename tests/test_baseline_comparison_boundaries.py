import pytest

from giraffe.reporting import compare_baseline
from tests.test_reporting import baseline_check, make_report


@pytest.mark.parametrize("field,metric", [
    ("elapsed_ms", "latency"), ("first_output_ms", "first_output"),
    ("max_stream_gap_ms", "max_stream_gap"),
])
def test_tail_regression_fails_when_median_is_unchanged(field, metric):
    current, old = make_report(count=100), make_report("baseline", count=100)
    for record in current.requests[-15:]:
        setattr(record, field, getattr(record, field) * 10)
    compared = compare_baseline(current, old)
    rows = {row["metric"]: row for row in compared.baseline["targets"][0]["comparisons"]}
    assert rows[f"{metric}_ms"]["status"] == "pass"
    tail = rows[f"{metric}_p95_ms"]
    assert tail["aggregation"] == "p95"
    assert tail["current"] == tail["baseline"] * 10
    assert tail["status"] == "fail"
    assert baseline_check(compared).status == compared.overall == "fail"
    from tests.test_performance import summary
    performance = summary(compared)["metrics"]["baseline_timing"]
    assert any(row["metric"] == f"{metric}_p95_ms" and row["status"] == "fail"
               for row in performance["value"])


@pytest.mark.parametrize("count", [19, 20])
def test_tail_comparison_requires_twenty_samples(count):
    compared = compare_baseline(make_report(count=count), make_report("baseline", count=count))
    tail = next(row for row in compared.baseline["targets"][0]["comparisons"]
                if row["metric"] == "latency_p95_ms")
    assert tail["status"] == ("pass" if count == 20 else "inconclusive")
    if count == 19:
        assert "at least 20" in tail["reason"]


def test_tail_work_qualification_uses_only_measured_timing_records():
    current, old = make_report(count=100), make_report("baseline", count=100)
    for report in (current, old):
        for index, record in enumerate(report.requests):
            measured = index < 50
            record.max_stream_gap_ms = 10 if measured else None
            record.output_tokens = 100 if measured == (report is current) else 10
    for record in current.requests[35:50]:
        record.max_stream_gap_ms = 100
    compared = compare_baseline(current, old)
    tail = next(row for row in compared.baseline["targets"][0]["comparisons"]
                if row["metric"] == "max_stream_gap_p95_ms")
    assert tail["current_samples"] == tail["baseline_samples"] == 50
    assert tail["evidence_ids"] == [record.id for record in current.requests[:50]]
    assert tail["output_work"]["current"]["reported_output_tokens_p95"] == 100
    assert tail["output_work"]["baseline"]["reported_output_tokens_p95"] == 10
    assert tail["status"] == "inconclusive"
    assert "output_work_changed" in {warning["code"] for warning in tail["warnings"]}


@pytest.mark.parametrize("guard", ["output_tail", "client_delay", "stream_options", "input_tail"])
def test_tail_comparison_preserves_timing_qualifications(guard):
    current, old = make_report(count=100), make_report("baseline", count=100)
    for record in current.requests[-15:]:
        record.elapsed_ms *= 10
        if guard == "output_tail":
            record.output_chars *= 10
            record.output_tokens *= 10
    if guard == "client_delay":
        current.observations["generator_max_lag_ms"] = 1000
    elif guard == "stream_options":
        for report, requested in ((current, True), (old, False)):
            for record in report.requests:
                record.stream = True
                record.requested_stream_logprobs = requested
                record.requested_stream_usage = True
    elif guard == "input_tail":
        for report in (current, old):
            for record in report.requests:
                record.input_tokens = 10
        for record in current.requests[-15:]:
            record.input_tokens = 100
    compared = compare_baseline(current, old)
    tail = next(row for row in compared.baseline["targets"][0]["comparisons"]
                if row["metric"] == "latency_p95_ms")
    expected = {"output_tail": "output_work_changed", "client_delay": "client_delayed",
                "stream_options": "stream_options_changed", "input_tail": "input_work_changed"}[guard]
    assert tail["status"] == "inconclusive"
    assert expected in {warning["code"] for warning in tail["warnings"]}


def test_lower_bound_equal_to_regression_threshold_is_material():
    compared = compare_baseline(make_report(latency=120), make_report("baseline", latency=100))

    latency = next(row for row in compared.baseline["targets"][0]["comparisons"]
                   if row["metric"] == "latency_ms")
    assert latency["worsening_interval_95"] == [20, 20]
    assert latency["threshold"] == 20
    assert latency["status"] == "fail"
    assert latency["reason"] == "Material worsening exceeds observed sample uncertainty."
    assert baseline_check(compared).status == compared.overall == "fail"


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
