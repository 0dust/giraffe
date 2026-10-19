"""Capacity, arrivals and recovery verdicts must follow the evidence they claim."""

import asyncio
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from giraffe import runner, traffic
from giraffe.models import Limits, RequestRecord, TrafficConfig
from giraffe.runner import run_suite
from tests.fake_endpoint import FakeEndpoint
from tests.test_load_reliability import LoadClient
from tests.test_runner import check, config
from tests.test_workloads import config as workload_config


@pytest.fixture
def endpoint(monkeypatch):
    class Endpoint(LoadClient):
        calls = []
    monkeypatch.setattr(runner, "LLMClient", Endpoint)
    return Endpoint


# capacity

@pytest.mark.parametrize("seed", [1, 35])
def test_poisson_stage_offers_exactly_its_labelled_rate(seed):
    settings = TrafficConfig(rates=[10, 20], duration_seconds=.5, arrival="poisson", seed=seed)
    for index, rate in enumerate(settings.rates):
        schedule, overflow = traffic.arrival_schedule(settings, rate, index)
        assert not overflow and len(schedule) == round(rate * settings.duration_seconds)
        offsets = [offset for offset, _ in schedule]
        assert offsets == sorted(offsets) and all(0 <= offset < settings.duration_seconds for offset in offsets)
        assert traffic.arrival_schedule(settings, rate, index) == (schedule, overflow)
    default = TrafficConfig(arrival="poisson", seed=1)
    assert [len(traffic.arrival_schedule(default, rate, index)[0]) / default.duration_seconds
            for index, rate in enumerate(default.rates)] == default.rates


@pytest.mark.parametrize("rate", [.2, 4.4, 5.2])
def test_poisson_arrival_count_rounds_fractional_expected_counts(rate):
    settings = TrafficConfig(rates=[rate], duration_seconds=.5, arrival="poisson")
    schedule, overflow = traffic.arrival_schedule(settings, rate)
    assert len(schedule) == round(rate * settings.duration_seconds) and not overflow


def test_poisson_schedule_respects_finite_bound(monkeypatch):
    monkeypatch.setattr(traffic, "MAX_PLANNED_ARRIVALS", 3)
    schedule, overflow = traffic.arrival_schedule(TrafficConfig(arrival="poisson"), 10)
    assert len(schedule) == 3 and overflow


class QueueClient(LoadClient):
    """Single-server FIFO: .08 s service sustains 12.5 requests/second."""
    calls = []
    lock = None

    async def execute(self, spec, *, first_output_event=None):
        cls = type(self)
        cls.lock = cls.lock or asyncio.Semaphore(1)
        started = time.monotonic()
        async with cls.lock:
            await asyncio.sleep(.08 if spec.scenario.startswith("traffic_stage_") else .005)
        elapsed = (time.monotonic() - started) * 1000
        output = str(spec.expected)
        return RequestRecord(
            id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids,
            started_at=datetime.now(timezone.utc).isoformat(), status="completed", valid=True,
            http_status=200, output=output, output_chars=len(output), input_chars=spec.input_chars,
            output_tokens=max(1, len(output.split())), elapsed_ms=elapsed,
            first_output_ms=elapsed - 1, last_output_ms=elapsed - .5, answer_chunks=2, chunks=2,
            max_stream_gap_ms=.5, stream=spec.stream, stream_terminated=True, finish_reason="stop",
            requested_max_tokens=spec.max_tokens)


@pytest.mark.parametrize("arrival,seed", [("steady", 42), ("poisson", 35)])
async def test_short_stage_above_service_rate_is_not_accepted_while_queue_grows(monkeypatch, arrival, seed):
    class Client(QueueClient):
        calls = []
        lock = None
    monkeypatch.setattr(runner, "LLMClient", Client)
    report = await run_suite(config(checks=["capacity"], concurrency=16, max_requests=400, max_duration_seconds=60,
        traffic={"rates": [10, 14], "duration_seconds": .5, "arrival": arrival, "seed": seed},
        limits=Limits(latency_ms=400, min_samples=2)))
    result = check(report, "capacity")
    stages = {stage["rate_rps"]: stage for stage in result.metrics["traffic_stages"]}
    assert stages[14]["scheduled_rps"] == 14
    assert not stages[14]["accepted"] and (stages[14]["queue_growth"] or stages[14]["stability_unverified"])
    assert stages[14]["status"] == "inconclusive"
    assert "Queue grew" in " ".join(stages[14]["reasons"])
    assert result.metrics["highest_acceptable_rate_rps"] != 14 and result.status != "pass"


async def test_steady_stage_below_service_rate_still_passes(monkeypatch):
    class Client(QueueClient):
        calls = []
        lock = None
    monkeypatch.setattr(runner, "LLMClient", Client)
    report = await run_suite(config(checks=["capacity"], concurrency=16, max_requests=400, max_duration_seconds=60,
        traffic={"rates": [5, 10], "duration_seconds": .5}, limits=Limits(latency_ms=400, min_samples=2)))
    result = check(report, "capacity")
    assert result.status == "pass" and result.metrics["highest_acceptable_rate_rps"] == 10
    assert not any(stage["queue_growth"] for stage in result.metrics["traffic_stages"])


def _evidence(stalls, slow_index):
    schedule = tuple((offset, "short") for offset in (0, .01, .02, .5, .6))
    records = []
    for index, (offset, _) in enumerate(schedule):
        if index == 0:
            continue
        elapsed = 2000 if index == slow_index else 50
        records.append((index, RequestRecord(
            id=str(index), target="t", fixture_id="short", scenario="traffic_stage_0", check_ids=["capacity"],
            started_at="now", status="completed", valid=True, score=True, elapsed_ms=elapsed,
            arrival_elapsed_ms=elapsed, first_output_ms=1, arrival_first_output_ms=1, last_output_ms=2,
            answer_chunks=2, output_tokens=2, max_stream_gap_ms=1)))
    return traffic.StageEvidence(
        stage_index=0, rate_rps=5, duration_seconds=1, observation_seconds=1, schedule=schedule,
        outcomes=("dropped_late",) + ("started",) * 4, records=tuple(records),
        lifetimes=tuple((offset, offset + .05) for offset, _ in schedule[1:]), reasons=(), peak=1,
        execution_errors=0, overflow=False, interrupted=False, drain_expired=False, case_scorers=None,
        stalls=stalls)


def test_one_late_drop_only_excuses_violations_overlapping_the_stall():
    limits = Limits(latency_ms=1000, min_samples=2)
    stall = ((0.0, .6),)
    assert traffic.finalize_stage(_evidence(((0.0, .03),), slow_index=4), limits)["status"] == "fail"
    excused = traffic.finalize_stage(_evidence(stall, slow_index=1), limits)
    assert excused["status"] == "inconclusive" and not excused["accepted"]
    assert "excessive scheduler lateness" in " ".join(excused["reasons"])


def test_stall_after_first_output_does_not_excuse_first_output_violation():
    evidence = _evidence(((.5, .6),), slow_index=1)
    evidence.records[0][1].arrival_first_output_ms = 100
    result = traffic.finalize_stage(evidence, Limits(first_output_ms=50, min_samples=2))
    assert result["status"] == "fail"


def test_rising_latency_with_normal_inflight_work_is_not_queue_growth():
    schedule = tuple((i / 10, "short") for i in range(10))
    records = tuple((i, _evidence((), 0).records[0][1].model_copy(update={
        "id": str(i), "elapsed_ms": latency, "arrival_elapsed_ms": latency,
    })) for i, latency in enumerate([200, 200, 240, 240, 280, 280, 320, 320, 360, 360]))
    evidence = replace(_evidence((), 0), rate_rps=10, schedule=schedule,
        outcomes=("started",) * 10, records=records,
        lifetimes=((.8, 1.2), (.9, 1.3)))
    result = traffic.finalize_stage(evidence, Limits(latency_ms=1000, min_samples=2))
    assert result["windows"][-1]["pending_at_end"] == 2
    assert result["accepted"] and not result["queue_growth"]


# arrivals

async def test_three_cancelled_arrivals_leave_eight_request_schedule_unqualified(endpoint, monkeypatch):
    original = endpoint.execute

    async def cancelled(self, spec, **kw):
        record = await original(self, spec, **kw)
        if spec.scenario == "arrival" and spec.workload["arrival_index"] >= 5:
            record.status, record.valid = "cancelled", False
        return record

    monkeypatch.setattr(endpoint, "execute", cancelled)
    report = await run_suite(config(checks=["arrivals"], concurrency=8,
        limits=Limits(latency_ms=5000, min_samples=2),
        arrivals={"requests": 8, "requests_per_second": 20}))
    result = check(report, "arrivals")
    assert result.metrics["schedule"]["issued"] == 8
    assert result.metrics["schedule"]["incomplete"] == 3
    assert not result.metrics["schedule"]["configured_rate_achieved"]
    assert result.status == "inconclusive"


@pytest.mark.parametrize("limits,status", [
    (Limits(min_samples=2), "inconclusive"),
    (Limits(latency_ms=5000, min_samples=2), "pass"),
    (Limits(first_output_ms=5000, min_samples=2), "pass"),
    (Limits(stream_gap_ms=5000, min_samples=2), "pass"),
    (Limits(min_output_tokens_per_second=.01, min_samples=2), "inconclusive"),
    (Limits(latency_ms=1, min_samples=2), "fail"),
])
async def test_arrivals_need_timing_limit_and_preserve_real_failures(endpoint, limits, status):
    report = await run_suite(config(checks=["arrivals"], concurrency=4, limits=limits,
        arrivals={"requests": 4, "requests_per_second": 20}))
    result = check(report, "arrivals")
    assert result.status == status
    if limits.min_output_tokens_per_second is not None:
        assert "generation_tokens_per_second" in result.metrics["missing_timing_metrics"]
    elif not limits.latency_ms and status == "inconclusive":
        assert result.summary.startswith("Arrivals observed; no acceptance limit configured.")
        assert "To conclude, set limits.latency_ms" in result.summary

async def test_arrivals_cancelled_at_run_deadline_are_not_a_pass():
    with FakeEndpoint(first_token_delay=.4) as server:
        report = await run_suite(workload_config(server, checks=["arrivals"], concurrency=16,
            request_timeout_seconds=10, max_duration_seconds=1.0, limits=Limits(min_samples=2, latency_ms=5000),
            arrivals={"requests": 8, "requests_per_second": 8}))
    result = check(report, "arrivals", target="local")
    schedule = result.metrics["schedule"]
    assert any(r.status == "cancelled" for r in report.requests if r.scenario == "arrival")
    assert schedule["incomplete"] > 0 and not schedule["configured_rate_achieved"]
    assert result.status == "inconclusive"


async def test_arrivals_without_timing_limit_are_inconclusive_and_pass_with_one():
    with FakeEndpoint(first_token_delay=.05) as server:
        unlimited = await run_suite(workload_config(server, checks=["arrivals"], concurrency=16,
            request_timeout_seconds=10, arrivals={"requests": 4, "requests_per_second": 20}))
        limited = await run_suite(workload_config(server, checks=["arrivals"], concurrency=16,
            request_timeout_seconds=10, limits=Limits(latency_ms=5000, min_samples=2),
            arrivals={"requests": 4, "requests_per_second": 20}))
    result = check(unlimited, "arrivals", target="local")
    assert result.status == "inconclusive"
    assert result.summary.startswith("Arrivals observed; no acceptance limit configured.")
    assert "To conclude, set limits.latency_ms" in result.summary
    assert check(limited, "arrivals", target="local").status == "pass"


# recovery

def _recovery_verdict(*, baseline=("completed", "completed"), probes=("completed", "completed"),
                      sustained=("completed", "completed"), limits=None):
    settings = config(checks=["recovery"], limits=limits or Limits(latency_ms=5000, min_samples=2))
    run = runner._Run(settings, None, None)
    target = settings.targets[0]
    run.observations[target.name]["sustained_complete"] = True
    for scenario, statuses in (("recovery_baseline", baseline), ("recovery", probes), ("sustained", sustained)):
        for status in statuses:
            completed = status == "completed"
            run.records.append(RequestRecord(
                id=str(len(run.records)), target=target.name, fixture_id="probe", scenario=scenario,
                check_ids=["recovery"], started_at="now", status=status, valid=completed,
                score=True if completed else None, elapsed_ms=20, first_output_ms=1,
                last_output_ms=19, answer_chunks=2, output_tokens=2, max_stream_gap_ms=1))
    return next(r for r in runner._checks(run, target) if r.id == "recovery")


def test_one_completed_probe_and_one_cancelled_probe_are_insufficient_without_unfinished_flag():
    result = _recovery_verdict(probes=("completed", "cancelled"))
    assert result.metrics["recovery"]["completed"] == 1
    assert result.status == "inconclusive"


def test_cancelled_sustained_records_are_not_errors_without_unfinished_flag():
    result = _recovery_verdict(sustained=("completed", "completed", "cancelled"))
    assert result.metrics["sustained_error_rate"] == 0
    assert result.status == "inconclusive"


def test_baseline_timeouts_leave_incomplete_evidence_even_with_absolute_limits():
    result = _recovery_verdict(baseline=("completed", "timeout"))
    assert result.metrics["baseline_non_timeout_error_rate"] == 0
    assert result.status == "inconclusive"


@pytest.mark.parametrize("failure", ["failed", "timeout"])
def test_real_sustained_failure_is_not_hidden_by_cancellation(failure):
    result = _recovery_verdict(sustained=("completed", failure, "cancelled"))
    assert result.metrics["sustained_error_rate"] == .5
    assert result.status == "fail"

async def test_user_stop_during_sustained_traffic_is_inconclusive_not_fail(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "delay", staticmethod(lambda spec, index: .3 if spec.scenario == "sustained" else .02))
    stop = asyncio.Event()

    async def stopper():
        while not any(call[0].scenario == "sustained" for call in endpoint.calls):
            await asyncio.sleep(.005)
        await asyncio.sleep(.05)
        stop.set()

    task = asyncio.create_task(stopper())
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=5, max_duration_seconds=30,
        limits=Limits(recovery_max_ratio=10, min_samples=2)), stop_event=stop)
    await task
    result = check(report, "recovery")
    assert [r.status for r in report.requests if r.scenario == "sustained"].count("cancelled") > 0
    assert result.metrics["sustained_error_rate"] == 0
    assert result.status == "inconclusive" and report.overall != "fail"


async def test_run_deadline_during_sustained_traffic_is_inconclusive_not_fail(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "delay", staticmethod(lambda spec, index: 1.0 if spec.scenario == "sustained" else .02))
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=.3, max_duration_seconds=.8,
        request_timeout_seconds=5, limits=Limits(recovery_max_ratio=10, min_samples=2)))
    result = check(report, "recovery")
    assert result.metrics["sustained_error_rate"] == 0 and result.status == "inconclusive"


async def test_probe_cancelled_at_deadline_does_not_count_as_a_sample(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "delay", staticmethod(lambda spec, index: .5 if spec.scenario == "recovery" else .02))
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=.15, max_duration_seconds=.95,
        request_timeout_seconds=5, limits=Limits(latency_ms=5000, recovery_max_ratio=100, min_samples=2)))
    probes = [r.status for r in report.requests if r.scenario == "recovery"]
    assert "cancelled" in probes and probes.count("completed") < 2
    assert check(report, "recovery").status == "inconclusive"


async def test_failing_light_load_baseline_fails_recovery(endpoint, monkeypatch):
    original = endpoint.execute

    async def fail_baseline(self, spec, **kw):
        result = await original(self, spec, **kw)
        if spec.scenario == "recovery_baseline":
            result.status, result.valid, result.http_status = "failed", False, 503
        return result

    monkeypatch.setattr(endpoint, "execute", fail_baseline)
    report = await runner.run_suite(config(checks=["recovery"], max_requests=40, sustained_seconds=10,
        limits=Limits(latency_ms=5000, min_samples=2)))
    result = check(report, "recovery")
    assert result.status == "fail" and result.metrics["baseline_non_timeout_error_rate"] == 1


async def test_ratio_from_single_surviving_baseline_is_not_a_pass(endpoint, monkeypatch):
    original = endpoint.execute
    seen = {"baseline": 0}

    async def mixed(self, spec, **kw):
        result = await original(self, spec, **kw)
        if spec.scenario == "recovery_baseline":
            seen["baseline"] += 1
            if seen["baseline"] > 1:
                result.status, result.valid, result.http_status = "failed", False, 503
            else:
                result.elapsed_ms, result.first_output_ms, result.last_output_ms = 900, 600, 899
        if spec.scenario == "recovery":
            result.elapsed_ms, result.first_output_ms, result.last_output_ms = 300, 200, 299
        return result

    monkeypatch.setattr(endpoint, "execute", mixed)
    report = await runner.run_suite(config(checks=["recovery"], samples=6, max_requests=60, sustained_seconds=10,
        stop_after_errors=100, limits=Limits(recovery_max_ratio=1.5, min_samples=2)))
    assert check(report, "recovery").status == "fail"


async def test_baseline_with_too_few_completions_is_inconclusive(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "delay", staticmethod(
        lambda spec, index: 1.0 if spec.scenario == "recovery_baseline" and index == 0 else .02))
    report = await runner.run_suite(config(checks=["recovery"], request_timeout_seconds=.3, stop_after_errors=100,
        limits=Limits(latency_ms=5000, recovery_max_ratio=100, min_samples=2, max_error_rate=1)))
    result = check(report, "recovery")
    assert result.metrics["baseline"]["completed"] == 1 and result.metrics["baseline"]["timed_out"] == 1
    assert result.status == "inconclusive"


async def test_baseline_is_primed_and_waits_after_cancellation(endpoint):
    report = await runner.run_suite(config(checks=["cancellation", "recovery"], max_requests=200,
        test_options={"cancellation": {"cleanup_seconds": .03}},
        limits=Limits(latency_ms=5000, min_samples=2)))
    scenarios = [call[0].scenario for call in endpoint.calls]
    prime = scenarios.index("recovery_prime")
    assert prime < scenarios.index("recovery_baseline") and scenarios.count("recovery_prime") == 1
    primed = next(r for r in report.requests if r.scenario == "recovery_prime")
    assert primed.check_ids == [] and primed.id not in check(report, "recovery").evidence_ids
    assert check(report, "recovery").metrics["baseline"]["attempted"] == 2
    previous_end = max(call[2] for call in endpoint.calls if call[0].scenario == "cancellation_probe")
    assert endpoint.calls[prime][1] - previous_end >= .03


async def test_prime_is_skipped_when_budget_is_tight(endpoint):
    report = await runner.run_suite(config(checks=["recovery"], max_requests=4,
        limits=Limits(latency_ms=5000, min_samples=2)))
    assert not any(r.scenario == "recovery_prime" for r in report.requests)


async def test_duration_elapsed_needs_time_weighted_occupancy(endpoint, monkeypatch):
    # One worker holds a long request while the other refills briefly: peak is
    # reached once, but the pool is mostly idle over the observed window.
    monkeypatch.setattr(endpoint, "delay", staticmethod(
        lambda spec, index: 1.0 if spec.scenario == "sustained" and index == 0 else .01))
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=.1,
        limits=Limits(latency_ms=5000, min_samples=2)))
    completion = report.observations["targets"]["service"]["sustained_completion"]
    assert completion["time_weighted_in_flight"] < 1.8
    assert check(report, "recovery").status == "inconclusive"


async def test_unscored_sustained_completions_block_a_recovery_pass(endpoint, monkeypatch):
    original = endpoint.execute

    async def truncated(self, spec, **kw):
        result = await original(self, spec, **kw)
        if spec.scenario == "sustained":
            result.output, result.finish_reason = str(spec.expected)[:2], "length"
        return result

    monkeypatch.setattr(endpoint, "execute", truncated)
    report = await runner.run_suite(config(checks=["recovery"], limits=Limits(latency_ms=5000, min_samples=2)))
    result = check(report, "recovery")
    assert result.metrics["unscored_completions"] > 0 and result.status == "inconclusive"
