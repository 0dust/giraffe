"""Condition accounting without a network server or socket permissions."""
import asyncio
import uuid

import pytest

from giraffe.logprob_workload import refill
from giraffe.models import RequestRecord, RunConfig
from giraffe.runner import _Run, run_suite
from giraffe.workloads import sessions, spec
from tests.test_web import browser, start, finished, report


def configuration(**updates):
    return RunConfig(targets=[{"name": "local", "url": "http://localhost:11434/v1", "model": "fake"}],
        checks=["concurrency"], concurrent_load={"schedule": {"levels": [1, 2]}}, concurrency=2, max_requests=100, **updates)


class Client:
    def __init__(self, delay=0, status="completed"):
        self.delay, self.status = delay, status

    async def execute(self, request, first_output_event=None):
        await asyncio.sleep(self.delay)
        return RequestRecord(id=uuid.uuid4().hex, target="local", fixture_id=request.fixture_id,
            scenario=request.scenario, check_ids=request.check_ids, started_at="2026-10-20T00:00:00Z",
            status=self.status, valid=self.status == "completed", output="wrong", elapsed_ms=self.delay*1000)


async def test_condition_matrix_partial_window_and_live_identity(monkeypatch):
    events = []
    config = configuration()
    run = _Run(config, events.append, None)
    request = spec("label", "bucket_c2_short_short", "buckets", level=2, input_bucket="short", output_bucket="short")
    from types import SimpleNamespace
    from giraffe import logprob_workload
    clock = SimpleNamespace(now=asyncio.get_running_loop().time())
    end = clock.now + 1
    monkeypatch.setattr(logprob_workload, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class WindowClient(Client):
        async def execute(self, request, first_output_event=None):
            record = await super().execute(request, first_output_event)
            clock.now = end  # Drain the first pair after the sending window closes.
            return record

    records = await refill(run, config.targets[0], WindowClient(), [request], 8, 2, end=end)
    assert len(records) == 2
    assert {r.condition_id for r in records} == {"bucket_c2_short_short"}
    condition = run.observations["local"]["conditions"][0]
    assert condition["planned"] == 8 and condition["planned_kind"] == "upper_bound"
    assert (condition["sent"], condition["not_sent"], condition["reason"]) == (2, 6, "sending window ended")
    assert condition["completed"] == 2 and condition["running"] == 0
    started = [e for e in events if e["event"] == "condition_started"]
    assert started[0]["condition"]["id"] == records[0].condition_id
    assert started[0]["planned_kind"] == "upper_bound"
    for event in (e for e in events if e["event"].startswith("request_")):
        assert event["condition_id"] == records[0].condition_id
        if event["event"] == "request_completed":
            assert event["request"]["condition_id"] == records[0].condition_id
    assert [e for e in events if e["event"] == "condition_finished"][0]["not_sent"] == 6


async def test_condition_group_timeout_is_failed_and_request_budget_shortfall():
    config = configuration()
    run = _Run(config, None, None)
    run.quotas["local"] = 1
    request = spec("label", "json_c2", "json")
    await run.group(config.targets[0], Client(status="timeout"), [request], request.scenario, count=4)
    condition = run.observations["local"]["conditions"][0]
    assert condition["failed"] == condition["completed"] == condition["sent"] == 1
    assert condition["not_sent"] == 3 and condition["reason"] == "request budget reached"


async def test_condition_sessions_dependent_turns_skip():
    config = configuration(sessions={"sessions": 2, "concurrency": 2, "modes": ["live"]})
    config.checks = ["sessions"]
    run = _Run(config, None, None)
    await sessions(run, config.targets[0], Client(status="failed"))
    condition = run.observations["local"]["conditions"][0]
    assert condition["condition"]["id"] == "session_live"
    assert condition["planned"] == 4 and condition["planned_kind"] == "exact"
    assert condition["sent"] == 2 and condition["not_sent"] == 2
    assert condition["failed"] == 2 and condition["reason"] == "dependent turns skipped"


async def test_web_condition_summaries_survive_event_cap(tmp_path, monkeypatch):
    async def waiting(config, *, progress, stop_event):
        runner = _Run(config, progress, stop_event)
        await runner.group(config.targets[0], Client(), [spec("x", "warm", "serving")], "warm", count=120)
        await stop_event.wait()
        return report(config, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        for _ in range(300):
            detail = (await client.get(f"/api/runs/{identifier}")).json()
            if len(detail["live_requests"]) == 120 and detail["live_requests"][-1]["status"] != "running":
                break
            await asyncio.sleep(0)
        assert len(detail["events"]) == 100
        assert {r["condition_id"] for r in detail["live_requests"]} == {"warm"}
        condition = next(row for row in detail["live_conditions"] if row["condition"]["id"] == "warm")
        assert condition["completed"] == condition["planned"] == 120
        assert condition["not_sent"] == 0 and condition["reason"] is None
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)


def test_legacy_request_record_condition_defaults_to_none():
    record = RequestRecord(id="old", target="local", fixture_id="old", scenario="warm", check_ids=[], started_at="", status="completed")
    assert record.condition_id is None




async def test_consistency_plan_groups_prompt_indices():
    from giraffe.workloads import consistency
    config = configuration(consistency={"levels": [2], "shapes": ["short"], "prompts_per_shape": 2, "repetitions": 2})
    config.checks = ["consistency"]
    run = _Run(config, None, None)
    await consistency(run, config.targets[0], Client(), {})
    rows = run.observations["local"]["conditions"]
    assert {row["condition"]["id"] for row in rows} == {"consistency_c1_short", "consistency_c2_short"}
    assert all(row["planned"] == row["sent"] == 4 and row["not_sent"] == 0 for row in rows)
    assert len(run.records) == 8


@pytest.mark.parametrize("stop_kind,reason", [("user", "run stopped"), ("time", "time limit reached")])
async def test_condition_stop_reasons_with_all_requests_already_started(stop_kind, reason):
    config = configuration()
    run = _Run(config, None, None)
    request = spec("x", "correctness_c2", "correctness")
    task = asyncio.create_task(run.group(config.targets[0], Client(delay=10), [request], request.scenario, count=2, concurrency=2))
    while run.attempted < 2:
        await asyncio.sleep(0)
    if stop_kind == "user":
        run.stop.set()
    else:
        run.deadline = asyncio.get_running_loop().time() - 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    row = run.observations["local"]["conditions"][0]
    assert row["sent"] == 2 and row["not_sent"] == 0
    assert row["running"] == 0 and row["completed"] == 2 and row["failed"] == 0
    assert row["reason"] == reason


async def test_saved_condition_failures_include_post_run_judging(monkeypatch):
    from tests.test_runner import FakeClient, judged_pack_config, judge_summary
    from giraffe import runner, judging

    async def evaluate(config, records, specs, **kwargs):
        for record in records:
            record.score = record.scenario == "pack_control"
        return judge_summary(records)

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    monkeypatch.setattr(judging, "judge_records", evaluate)
    result = await run_suite(judged_pack_config())
    rows = result.observations["targets"]["service"]["conditions"]
    for row in rows:
        records = [record for record in result.requests if record.condition_id == row["condition"]["id"]]
        assert row["failed"] == sum(record.score is False for record in records)




async def test_arrivals_have_rate_condition_and_exact_plan():
    from giraffe import load_test
    from tests.test_concurrent_load import configuration as load_configuration,short_windows
    cfg=short_windows(load_configuration(load={'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':True},'rates':[1000]}}),.002)
    run=_Run(cfg,None,None)
    await load_test.execute(run,cfg.targets[0],Client())
    p=load_test.result(run,cfg.targets[0]).metrics['points'][0]
    row=next(r for r in run.observations['local']['conditions'] if r['condition']['id'] == p['id'])
    assert row['condition']['id'].endswith('_constant_r1000')
    assert row['planned'] == row['sent'] == 2 and row['not_sent'] == 0
    assert {r.condition_id for r in run.records if r.load_point.get('type') != 'calibration'} == {p['id']}


async def test_logprob_consistency_plan_includes_priming_and_measured_rounds():
    from giraffe.logprob_workload import run_logprob_consistency
    config = configuration(consistency={"levels": [2], "shapes": ["short"], "repetitions": 2,
        "logprobs": {"priming_rounds": 1}})
    config.checks = ["consistency"]
    run = _Run(config, None, None)
    fixtures = {shape: [spec("x", shape, "consistency")] for shape in ["short", "long"]}
    await run_logprob_consistency(run, config.targets[0], Client(), fixtures)
    rows = run.observations["local"]["conditions"]
    assert {row["condition"]["id"] for row in rows} == {"consistency_c1_short", "consistency_c2_short"}
    assert all(row["planned"] == row["sent"] == row["completed"] == 6 for row in rows)
    assert all(row["status"] == "finished" and row["reason"] is None for row in rows)
