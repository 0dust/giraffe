"""Golden samples keep every report path on one percentile convention."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from giraffe import concurrent_load, latency, load_test, performance, reporting, runner, session_metrics, stats, traffic
from giraffe.models import ConcurrentLoad, Limits, LoadTest, RequestRecord, RunReport


VALUES = [value + .000123 for value in range(1, 11)]
P50 = 5.500123
P95 = 9.550123


def records():
    return [RequestRecord(id=str(index), target="t", fixture_id="sample", scenario="generation",
        check_ids=["generation"], started_at="now", status="completed", valid=True, score=True,
        elapsed_ms=value, first_output_ms=value, last_output_ms=value, stream_end_ms=value,
        dispatch_ms=index * 100, timing_start_ms=index * 100, completed_ms=index * 100 + value,
        input_tokens=128, output_tokens=64, completion_tokens=64, requested_max_tokens=64,
        load_point={"shape": "short_short", "role": "request"})
        for index, value in enumerate(VALUES)]


def point():
    return dict(id="sample", type="combinations", workload="short_short", shape="short_short",
        pattern="concurrent", level=1, rate=None, bursts=False, source="primary",
        sending_start_ms=0, sending_end_ms=1000, delivered_window_seconds=1,
        configured_window_seconds=1, observation_seconds=1, load_qualified=True)


def run(rows):
    return SimpleNamespace(records=rows, max_lag_ms=0, config=SimpleNamespace(stream=True))


def test_shared_statistics_golden_and_empty_samples():
    assert stats.percentile(reversed(VALUES), .95) == pytest.approx(P95)
    assert stats.median(iter(VALUES)) == pytest.approx(P50)
    assert stats.distribution(VALUES) == pytest.approx(dict(n=10, mean=P50, min=1.000123,
        median=P50, p90=9.100123, p95=P95, p99=9.910123, max=10.000123))
    assert stats.percentile([], .95) is None
    assert stats.median([]) is None
    assert stats.distribution([])["n"] == 0
    assert stats.percentile([None, True, float("inf"), float("nan"), 7], .95) == 7
    with pytest.raises(ValueError):
        stats.percentile(VALUES, 1.1)


def test_only_shared_percentile_implementation_remains():
    # This gate covers product/report code under src/giraffe. The separate
    # validation_suite/verifier.py intentionally keeps an independent nearest-rank
    # implementation with a 0..100 API and its own hand-specified golden controls;
    # sharing product arithmetic would weaken that harness's independence.
    package = Path(stats.__file__).parent
    definitions = [(path.name, node.name) for path in package.glob("*.py")
                   for node in ast.walk(ast.parse(path.read_text()))
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in {"percentile", "_percentile"}]
    assert definitions == [("stats.py", "percentile")]
    for module in (concurrent_load, latency, load_test, performance, reporting, runner, session_metrics, traffic):
        assert module.percentile is stats.percentile


def test_performance_distribution_golden():
    result = performance.distribution(VALUES, label="Latency", unit="ms", source="sample",
                                      target="t", window="full")["value"]
    assert result["median"] == pytest.approx(P50)
    assert result["p95"] == pytest.approx(P95)


def test_legacy_concurrent_measurements_golden():
    rows = records()
    result = concurrent_load.measurements(run(rows), SimpleNamespace(name="t"), ConcurrentLoad(), point(), rows)
    assert result["response_p50_ms"] == pytest.approx(P50)
    assert result["response_p95_ms"] == pytest.approx(P95)
    assert result["first_output_p95_ms"] == pytest.approx(P95)


def test_load_measurements_golden_and_ten_sample_verdict():
    rows = records()
    settings = LoadTest()
    settings.pass_criteria.max_completion_ms = 9.75
    result = load_test.measurements(run(rows), SimpleNamespace(name="t"), settings, point(), rows, "short_short")
    assert result["completion_p50_ms"] == pytest.approx(P50)
    assert result["completion_p95_ms"] == pytest.approx(P95)
    assert result["first_answer_p95_ms"] == pytest.approx(P95)
    # The active load verdict enforces each response limit. An interpolated p95
    # below 9.75 does not excuse the tenth response exceeding that limit.
    result = load_test.score_group(settings, point(), result, rows)
    assert result["status"] == "fail"
    assert result["reason_code"] == "timing"



def test_load_burst_and_origin_summaries_golden():
    rows = records()
    for row in rows:
        row.scheduled_ms = row.dispatch_ms
        row.arrival_elapsed_ms = row.elapsed_ms
        row.load_point.update(origin="burst", burst_index=0)
    result = load_test.assess_point(run(rows), SimpleNamespace(name="t"), LoadTest(), point(), rows)
    assert result["burst_delivery"][0]["completion_p50_ms"] == pytest.approx(P50)
    assert result["response_by_origin"]["burst"]["completion_p50_ms"] == pytest.approx(P50)


def test_arrival_traffic_measurements_golden_without_early_rounding():
    result = traffic._record_stats(records(), Limits())
    assert result["latency_p50_ms"] == pytest.approx(P50)
    assert result["latency_p95_ms"] == pytest.approx(P95)
    assert result["first_output_p95_ms"] == pytest.approx(P95)
    assert result["latency_p95_ms"] != round(P95, 3)


def test_latency_summary_golden():
    value = RunReport(run_id="samples", overall="pass", started_at="now", finished_at="now",
        manifest={"config": {"targets": [{"name": "t", "url": "http://localhost:1/v1", "model": "test"}]}}, requests=records())
    result = latency.latency_summary(value)["targets"][0]["measurements"][0]
    assert result["p50_ms"] == pytest.approx(P50)
    assert result["p90_ms"] == pytest.approx(9.100123)
    assert result["p99_ms"] == pytest.approx(9.910123)


def test_runner_stats_golden():
    result = runner._stats(records())
    assert result["p50_ms"] == pytest.approx(P50)
    assert result["p95_ms"] == pytest.approx(P95)
    assert result["first_output_p95_ms"] == pytest.approx(P95)


def test_reporting_percentile_and_bootstrap_golden():
    assert reporting.percentile(VALUES, .5) == pytest.approx(P50)
    assert reporting.percentile(VALUES, .95) == pytest.approx(P95)
    result = reporting._compare_metric("latency_ms", [10] * 10, [10] * 10, 5, 10)
    assert result["current"] == result["baseline"] == 10
    assert result["worsening_interval_95"] == [0, 0]
    assert result["status"] == "pass"


def test_session_generation_p10_golden():
    rows = [SimpleNamespace(generation_tokens_per_second=value, finish_reason="stop") for value in VALUES]
    result = session_metrics._summary(rows, lambda records: {"completed": len(records)})
    assert result["generation_tokens_per_second_p10"] == pytest.approx(1.900123)


def test_short_poisson_stability_uses_interpolated_tail_golden():
    # Nine slowly increasing responses and one slow tail. Floor-index p90 used
    # to omit the tail entirely (1.08); interpolation measures p90 = 1.472.
    seconds = [1 + index / 100 for index in range(9)] + [5]
    rows = records()
    for row, duration in zip(rows, seconds):
        row.arrival_elapsed_ms = duration * 1000
        row.arrival_first_output_ms = row.first_output_ms
    evidence = traffic.StageEvidence(
        stage_index=0, rate_rps=10, duration_seconds=1, observation_seconds=6,
        schedule=tuple((index / 10, "short") for index in range(10)),
        outcomes=("started",) * 10, records=tuple(enumerate(rows)), lifetimes=(),
        reasons=(), peak=10, execution_errors=0, overflow=False, interrupted=False,
        drain_expired=False, case_scorers=None, arrival="poisson")
    result = traffic.finalize_stage(evidence, Limits(min_samples=2, latency_ms=6000))
    assert stats.percentile(seconds, .1) == pytest.approx(1.009)
    assert stats.percentile(seconds, .9) == pytest.approx(1.472)
    assert result["stability_unverified"] is True
    assert not result["accepted"] and result["status"] == "inconclusive"
