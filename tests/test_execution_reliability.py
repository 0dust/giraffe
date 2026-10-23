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
    spec = runner._spec(builtin_fixtures(cfg)["short"][0], "load_primary_test", ["load"])
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
    spec = runner._spec(builtin_fixtures(cfg)["short"][0], "load_primary_test", ["load"])
    record = await run.request(cfg.targets[0], Failed(), spec)
    assert record.check_ids == ["load"]
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
    ([True, False, True], 4, True), ([False, True, True], 4, True),
    ([True, True, True], 4, False), ([False, False, False], None, False),
])
def test_capacity_highest_rate_requires_contiguous_acceptance(acceptance,highest,non_monotonic):
    # Final #45 reports the highest tested passing point, not a monotonic proof.
    from giraffe import load_test
    from tests.test_concurrent_load import configuration
    from tests.test_load_executor_regressions import record
    cfg=configuration(load={'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':True},'rates':[1,2,4]}})
    run=runner._Run(cfg,None,None)
    points=load_test.test_points(cfg.load_test)
    for p,accepted in zip(points,acceptance):
        p.update(sending_start_ms=0,sending_end_ms=1000,delivered_window_seconds=1,observation_seconds=1,cut_short=False,not_run=False)
        for i in range(2):
            run.records.append(record(f'{p["rate"]}-{i}',score=accepted,arrival_elapsed_ms=35,arrival_first_output_ms=10,load_point={'point_id':p['id'],'shape':'short_short'}))
        load_test.assess_point(run,cfg.targets[0],cfg.load_test,p,[r for r in run.records if r.load_point.get('point_id') == p['id']])
    run.observations['local']['load_test']={'points':points}
    result=load_test.result(run,cfg.targets[0])
    assert [p['status']=='pass' for p in points] == acceptance
    values=result.metrics['highest_accepted']
    assert (values[0]['value'] if values else None) == highest
    if values:
        assert values[0]['qualification'] == 'highest tested, not an absolute capacity limit'


async def test_out_of_order_completions_during_refill_preserve_every_record():
    cfg = config()
    run = runner._Run(cfg, None, None)
    fixture = runner._spec(builtin_fixtures(cfg)["short"][0], "load_primary_test", ["load"])
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
        def __init__(self, *args, max_connections=None):
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
        def __init__(self, *args, max_connections=None):
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


async def test_healthy_capacity_retains_all_offers_and_following_serial_overlap(monkeypatch, local_tokenizer):
    from tests.test_concurrent_load import configuration,short_windows,Client as TokenClient
    class Healthy(TokenClient):
        def __init__(self,*args,max_connections=None): super().__init__()
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    monkeypatch.setattr(runner,'LLMClient',Healthy)
    cfg=short_windows(configuration(checks=['load','correctness'],concurrency=64,max_requests=200,test_options={'correctness':{'concurrency':1}},load={'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':True},'rates':[1000]}}),.06)
    report=await runner.run_suite(cfg)
    p=next(c for c in report.checks if c.id == 'load').metrics['points'][0]
    g=p['groups'][0]
    assert p['scheduled'] == p['dispatched'] == g['completed'] == 60
    assert g['failed'] == g['error_percent'] == 0
    assert len(report.requests) == 66 and report.abort_reason is None
    assert not any('calibration' in record.scenario for record in report.requests)
    assert all(r.overlap == 1 for r in report.requests if r.scenario == 'correctness_c1')


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
    assert run.slots.qsize() == run.slots.maxsize
