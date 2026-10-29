"""The first-output percentile needs cohort-level endpoint attribution."""

import pytest

from giraffe import runner
from giraffe.models import Limits, RunConfig
from giraffe.timing import TimingHealth
from tests.test_check_intent_round2_core import record


def assess(independent_slow, delayed_slow, *, limit=10_000, unfinished=None):
    config = RunConfig(targets=[dict(name="t", url="http://local", model="m")],
                       checks=["first_output"], limits=Limits(first_output_ms=limit))
    run = runner._Run(config, None, None)
    run.timing_health = TimingHealth(0)
    run.records = [record(str(i), check_ids=["first_output"], timing_start_ms=i * 30_000,
        completed_ms=i * 30_000 + 21_000, elapsed_ms=21_000,
        first_output_ms=20_000 if i >= 21 - independent_slow - delayed_slow else 1,
        last_output_ms=20_001, stream_end_ms=21_000) for i in range(21)]
    for i in range(21 - independent_slow - delayed_slow, 21 - independent_slow):
        run.timing_health.windows.append(dict(start_ms=i * 30_000 + 1,
            end_ms=i * 30_000 + 15_000, lag_ms=14_949))
    if unfinished is not None:
        run.records.append(record("unfinished", check_ids=["first_output"],
            status="timeout", valid=False, first_output_ms=None, elapsed_ms=20_000,
            timing_start_ms=630_000, completed_ms=650_000))
        if unfinished == "delayed":
            run.timing_health.windows.append(dict(start_ms=630_001, end_ms=645_000, lag_ms=14_949))
    before = [r.model_dump() for r in run.records]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "first_output")
    assert [r.model_dump() for r in run.records] == before
    return result


@pytest.mark.parametrize("independent,delayed,status,raw_p95,violations", [
    (0, 0, "pass", 1, []),
    (1, 0, "pass", 1, []),
    (1, 1, "inconclusive", 20_000, []),
    (0, 2, "inconclusive", 20_000, []),
    (2, 0, "fail", 20_000, ["19", "20"]),
    (2, 1, "fail", 20_000, ["19", "20"]),
])
def test_first_output_p95_requires_independent_cohort_failure(independent, delayed, status, raw_p95, violations):
    result = assess(independent, delayed)
    assert result.status == status
    assert result.metrics["first_output_p95_ms"] == raw_p95
    assert result.metrics.get("violations", []) == violations
    if status == "inconclusive":
        assert result.metrics["reason_code"] == "client_delayed"
        assert result.metrics["unattributed_timing_violations"] == ["19", "20"]


def test_first_output_qualification_keeps_delayed_samples_in_percentile_denominator():
    # Dropping the affected answer shrinks 21 samples to 20 and raises p95 of
    # the remaining 19 fast answers and one outlier from 1 ms to 1,000.95 ms.
    result = assess(1, 1, limit=500)
    assert result.status == "inconclusive"
    assert result.metrics.get("violations", []) == []


@pytest.mark.parametrize("delayed", [0, 1])
@pytest.mark.parametrize("unfinished,status", [("independent", "fail"), ("delayed", "inconclusive")])
def test_unfinished_request_lower_bound_is_qualified_separately(unfinished, status, delayed):
    result = assess(1, delayed, unfinished=unfinished)
    assert result.status == status
    assert result.metrics.get("violations", []) == (["unfinished"] if status == "fail" else [])


def test_first_output_qualification_uses_full_precision_at_the_limit():
    config = RunConfig(targets=[dict(name="t", url="http://local", model="m")],
                       checks=["first_output"], limits=Limits(first_output_ms=500.9561))
    run = runner._Run(config, None, None)
    run.timing_health = TimingHealth(0)
    run.records = [record(str(i), check_ids=["first_output"], timing_start_ms=i * 30_000,
        completed_ms=i * 30_000 + 21_000, elapsed_ms=21_000,
        first_output_ms=10_000.123 if i == 19 else 500 if i == 18 else 1,
        last_output_ms=20_001, stream_end_ms=21_000) for i in range(20)]
    run.timing_health.windows = [dict(start_ms=540_001, end_ms=540_200, lag_ms=150)]

    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "first_output")

    # Replacing the affected 500 ms answer with zero leaves a p95 of
    # 500.95615 ms. Rounding it to three decimals would hide the failure.
    assert result.status == "fail"
    assert result.metrics["first_output_p95_ms"] == pytest.approx(975.00615)
    assert result.metrics["violations"] == ["19"]
