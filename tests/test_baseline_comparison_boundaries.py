import random
from types import SimpleNamespace

import pytest

from giraffe.reporting import _output_tail_shifted, compare_baseline
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
    metrics = {row["metric"] for row in compared.baseline["targets"][0]["comparisons"]}
    tail = {"latency_p95_ms", "first_output_p95_ms", "max_stream_gap_p95_ms"}
    assert tail <= metrics if count == 20 else not tail & metrics
    assert "latency_ms" in metrics


def test_small_sample_baseline_does_not_gain_tail_noise():
    compared = compare_baseline(make_report(count=8), make_report("baseline", count=8))
    target = compared.baseline["targets"][0]
    assert not any(row["metric"].endswith("_p95_ms") for row in target["comparisons"])
    check = baseline_check(compared)
    assert check.metrics["excluded_metrics"] == 0
    assert "not comparable" not in check.summary
    assert compared.overall == "pass"


@pytest.mark.parametrize("field,metric", [
    ("elapsed_ms", "latency"), ("first_output_ms", "first_output"),
    ("max_stream_gap_ms", "max_stream_gap"),
])
def test_issue_scenario_fifteen_percent_slow_at_sixty_samples(field, metric):
    current, old = make_report(count=60), make_report("baseline", count=60)
    for record in current.requests[-9:]:
        setattr(record, field, getattr(record, field) * 10)
    compared = compare_baseline(current, old)
    rows = {row["metric"]: row for row in compared.baseline["targets"][0]["comparisons"]}
    assert rows[f"{metric}_ms"]["status"] == "pass"
    assert rows[f"{metric}_p95_ms"]["status"] == "fail"
    assert baseline_check(compared).status == compared.overall == "fail"


def _noisy_report(run_id, count, seed):
    report = make_report(run_id, count=count)
    rng = random.Random(seed)
    for record in report.requests:
        tokens = max(1, int(rng.lognormvariate(4, .6)))
        record.output_tokens, record.output_chars = tokens, tokens * 4
        record.elapsed_ms = 20 * tokens * rng.uniform(.9, 1.1)
        record.first_output_ms = record.elapsed_ms / 2
        record.max_stream_gap_ms = record.elapsed_ms / 10
    return report


@pytest.mark.parametrize("count", [20, 30])
def test_same_distribution_runs_do_not_fail_tail_comparison(count):
    for seed in range(5):
        compared = compare_baseline(_noisy_report("current", count, seed),
                                    _noisy_report("baseline", count, 100 + seed))
        assert compared.overall != "fail", seed
        assert not any(row["status"] == "fail"
                       for row in compared.baseline["targets"][0]["comparisons"]), seed


def _answers(rng, count, scale=1):
    return [SimpleNamespace(status="completed", valid=True, workload={},
                            output_tokens=(tokens := max(1, int(rng.lognormvariate(5, .8) * scale))),
                            output_chars=tokens * 4) for _ in range(count)]


@pytest.mark.parametrize("count,bound", [(30, .10), (60, .05)])
def test_tail_output_guard_rarely_fires_on_sampling_noise(count, bound):
    # Measured: about 6% at n=30 and 2.5% at n=60 for same-distribution runs.
    fired = sum(_output_tail_shifted(_answers(random.Random(seed), count),
                                     _answers(random.Random(10_000 + seed), count), 20)
                for seed in range(200))
    assert fired / 200 < bound


def test_tail_output_guard_fires_on_large_tail_shift():
    base = _answers(random.Random(0), 60)
    assert _output_tail_shifted(_answers(random.Random(0), 60, scale=3), base, 20)
    assert not _output_tail_shifted(base, base, 20)


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


@pytest.mark.parametrize("count", [60, 100])
def test_longer_tail_answers_are_not_reported_as_latency_regression(count):
    # 15% of answers grow 100 -> 150 tokens; time per token is unchanged.
    current, old = make_report(count=count), make_report("baseline", count=count)
    for report in (current, old):
        for record in report.requests:
            record.output_tokens, record.output_chars = 100, 400
            record.elapsed_ms = 1000
    for record in current.requests[-round(count * .15):]:
        record.output_tokens, record.output_chars = 150, 600
        record.elapsed_ms = 1500
    compared = compare_baseline(current, old)
    rows = {row["metric"]: row for row in compared.baseline["targets"][0]["comparisons"]}
    tail = rows["latency_p95_ms"]
    assert tail["status"] == "inconclusive"
    assert "output_work_changed" in {warning["code"] for warning in tail["warnings"]}
    assert compared.overall != "fail"
