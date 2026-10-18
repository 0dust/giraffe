"""Regression coverage for execution evidence and intentional stress isolation."""
import asyncio
import uuid

import pytest

from giraffe import runner
from giraffe.fixtures import builtin_fixtures
from giraffe.models import RequestRecord, RunConfig


def config(**options):
    return RunConfig.model_validate({"targets": [{"name": "t", "model": "m", "url": "http://localhost:1"}],
        "checks": ["access", "serving", "capacity", "correctness"], "samples": 2,
        "concurrency": 8, "max_requests": 200, "limits": {"min_samples": 2}, **options})


class Client:
    async def execute(self, spec, *, first_output_event=None):
        await asyncio.sleep(.001)
        return RequestRecord(id=uuid.uuid4().hex, target="t", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now", status="completed",
            valid=True, output=str(spec.expected), elapsed_ms=2, first_output_ms=1,
            last_output_ms=2, answer_chunks=2, http_status=200)


async def test_equal_inflight_entries_never_lose_records_or_leak_overlap():
    cfg = config()
    run = runner._Run(cfg, None, None)
    spec = runner._spec(builtin_fixtures(cfg)["short"][0], "traffic_stage_0", ["capacity"])
    for _ in range(5):
        records = await asyncio.gather(*(run.request(cfg.targets[0], Client(), spec) for _ in range(8)))
        assert len(records) == 8
    serial = await run.request(cfg.targets[0], Client(), runner._spec(spec, "warm"))
    assert len(run.records) == 41 and serial.overlap == 1
    assert not run.inflight


async def test_stress_errors_are_scoped_and_do_not_abort_later_checks():
    cfg = config(stop_after_errors=1)
    run = runner._Run(cfg, None, None)
    class Failed(Client):
        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            record.status, record.valid, record.http_status = "timeout", False, None
            return record
    spec = runner._spec(builtin_fixtures(cfg)["short"][0], "traffic_stage_0", ["capacity"])
    record = await run.request(cfg.targets[0], Failed(), spec)
    assert record.check_ids == ["capacity"]
    assert run.errors == 0 and run.stop_reason() is None
    assert await run.request(cfg.targets[0], Client(), runner._spec(spec, "warm"))


async def test_preheader_timeout_is_not_authentication_or_transport_failure():
    cfg = config(checks=["access"])
    run = runner._Run(cfg, None, None)
    run.records = [RequestRecord(id="timeout", target="t", fixture_id="f", scenario="initial",
        check_ids=["access"], started_at="now", status="timeout", error="request deadline exceeded")]
    result = next(c for c in runner._checks(run, cfg.targets[0]) if c.id == "access")
    assert result.status == "inconclusive"
    assert "timeout" in result.summary.lower() and "Authentication" not in result.summary


@pytest.mark.parametrize("acceptance,highest,non_monotonic", [
    ([True, False, True], 1, True), ([False, True, True], None, True),
    ([True, True, True], 4, False), ([False, False, False], None, False),
])
def test_capacity_highest_rate_requires_contiguous_acceptance(acceptance, highest, non_monotonic):
    cfg = config(checks=["capacity"], traffic={"rates": [1, 2, 4]})
    run = runner._Run(cfg, None, None)
    run.observations["t"]["capacity"] = [
        {"rate_rps": rate, "accepted": accepted, "status": "pass" if accepted else "fail"}
        for rate, accepted in zip([1, 2, 4], acceptance)]
    result = next(c for c in runner._checks(run, cfg.targets[0]) if c.id == "capacity")
    assert result.metrics["highest_acceptable_rate_rps"] == highest
    assert result.metrics["non_monotonic_results"] is non_monotonic


async def test_out_of_order_completions_during_refill_preserve_every_record():
    cfg = config()
    run = runner._Run(cfg, None, None)
    fixture = runner._spec(builtin_fixtures(cfg)["short"][0], "traffic_stage_0", ["capacity"])
    gates = [asyncio.Event() for _ in range(6)]
    started = [asyncio.Event() for _ in range(6)]
    class Controlled(Client):
        async def execute(self, spec, **kwargs):
            index = spec.workload["index"]
            started[index].set()
            await gates[index].wait()
            return await super().execute(spec, **kwargs)
    def launch(index):
        return asyncio.create_task(run.request(cfg.targets[0], Controlled(),
            fixture.model_copy(update={"workload": {"index": index}})))
    tasks = [launch(index) for index in range(3)]
    await asyncio.gather(*(event.wait() for event in started[:3]))
    for index in (1, 2):
        gates[index].set()
        await tasks[index]
    tasks.extend(launch(index) for index in range(3, 6))
    await asyncio.gather(*(event.wait() for event in started[3:]))
    gates[0].set()
    try:
        await tasks[0]
    finally:
        for event in gates:
            event.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(record, RequestRecord) for record in results)
    assert len(run.records) == 6 and not run.inflight
    record = await run.request(cfg.targets[0], Client(), runner._spec(fixture, "warm"))
    assert record.overlap == 1


async def test_restart_readiness_has_separate_deadline_and_error_accounting(monkeypatch):
    cfg = config(checks=["access"], stop_after_errors=1, restart_target="t",
                 readiness_timeout_seconds=2)
    attempts = []
    class Cold(Client):
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def ready(self, deadline):
            attempts.append("GET models")
            return len(attempts) == 4
        async def execute(self, spec, **kwargs):
            attempts.append(spec.scenario)
            return await super().execute(spec, **kwargs)
    async def restart(run, target):
        run.observations["t"]["restart"] = {"status": "hook_completed", "started_monotonic": runner.time.monotonic()}
        return True
    monkeypatch.setattr(runner, "LLMClient", Cold)
    monkeypatch.setattr(runner, "_restart", restart)
    run = runner._Run(cfg, None, None)
    await runner._target(run, cfg.targets[0], builtin_fixtures(cfg))
    assert attempts == ["GET models"] * 4 + ["readiness"]
    assert run.errors == run.budget_used == 0 and run.reason is None
    assert len(run.observations["t"]["readiness_requests"]) == 1
    assert run.observations["t"]["restart"]["status"] == "ready"
    result = next(c for c in runner._checks(run, cfg.targets[0]) if c.id == "access")
    assert result.status == "pass" and result.metrics["attempted"] == 1


async def test_restart_readiness_deadline_bounds_backoff_and_inflight_request(monkeypatch):
    cfg = config(checks=["access"], restart_target="t", readiness_timeout_seconds=.03)
    class Cold(Client):
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def ready(self, deadline):
            await asyncio.sleep(1)
    async def restart(run, target):
        run.observations["t"]["restart"] = {"status": "hook_completed", "started_monotonic": runner.time.monotonic()}
        return True
    monkeypatch.setattr(runner, "LLMClient", Cold)
    monkeypatch.setattr(runner, "_restart", restart)
    run = runner._Run(cfg, None, None)
    await runner._target(run, cfg.targets[0], builtin_fixtures(cfg))
    assert run.observations["t"]["restart"]["status"] == "blocked"
    assert run.errors == 0 and runner.time.monotonic()-run.start < .2


async def test_healthy_capacity_retains_all_offers_and_following_serial_overlap(monkeypatch):
    class Healthy(Client):
        count = 0
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, spec, **kwargs):
            index = type(self).count
            type(self).count += 1
            await asyncio.sleep(.004 + (index % 7) * .002)
            return await super().execute(spec, **kwargs)
    monkeypatch.setattr(runner, "LLMClient", Healthy)
    cfg = config(checks=["capacity", "correctness"], concurrency=64,
        test_options={"correctness": {"concurrency": 1}}, stop_after_errors=1,
        traffic={"rates": [1000], "duration_seconds": .06},
        limits={"latency_ms": 200, "min_samples": 2})
    report = await runner.run_suite(cfg)
    stage = next(c for c in report.checks if c.id == "capacity").metrics["traffic_stages"][0]
    assert stage["started"] == stage["completed"] == 60
    assert stage["failed"] == stage["error_rate"] == 0
    assert len(report.requests) == 63 and report.abort_reason is None
    assert all(r.overlap == 1 for r in report.requests if r.scenario == "correctness_c1")


async def test_scorer_failure_preserves_attempt_record_and_clears_inflight(monkeypatch):
    cfg = config()
    run = runner._Run(cfg, None, None)
    def broken_scorer(spec, record):
        raise RuntimeError("scorer failed")
    monkeypatch.setattr(runner, "score_response", broken_scorer)
    spec = runner._spec(builtin_fixtures(cfg)["short"][0], "warm")
    with pytest.raises(RuntimeError, match="scorer failed"):
        await run.request(cfg.targets[0], Client(), spec)
    assert len(run.records) == run.attempted == 1
    assert not run.inflight and run.active[("t", "warm")] == 0
    assert run.slots.qsize() == cfg.concurrency
