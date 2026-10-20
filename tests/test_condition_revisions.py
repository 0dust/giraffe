"""Review regressions for condition dispatch, lifecycle, plans and probe counts."""
import asyncio

import pytest

from giraffe import runner, workloads
from giraffe.fixtures import builtin_fixtures
from giraffe.runner import _Run, run_suite
from giraffe.workloads import spec
from tests.test_condition_tracking import Client, configuration
from tests.test_runner import FakeClient, config as runner_config, judged_pack_config, judge_summary
from tests.test_web import browser, start, finished, report


async def test_dynamic_condition_stays_running_between_requests_without_event_churn():
    events = []
    config = configuration()
    run = _Run(config, events.append, None)
    request = spec("x", "stress_calibration_example", "token_stress")
    for _ in range(3):
        await run.request(config.targets[0], Client(), request)
        row = run.observations["local"]["conditions"][0]
        assert row["status"] == "running" and row["running"] == 0
    assert [event["event"] for event in events if event["event"].startswith("condition_")] == ["condition_started"]
    run.finish_condition("local", request.scenario)
    assert row["status"] == "finished" and row["sent"] == 3


async def test_intentional_timeout_is_completed_without_failed_count_live_and_recount(monkeypatch):
    events = []
    config = configuration()
    run = _Run(config, events.append, None)
    request = spec("x", "client_deadline", "cancellation")
    record = await run.request(config.targets[0], Client(status="timeout"), request, timeout_override=.1)
    assert not runner._request_failed(record)
    row = run.observations["local"]["conditions"][0]
    assert row["completed"] == 1 and row["failed"] == 0
    assert [event for event in events if event["event"] == "request_completed"][0]["condition_summary"]["failed"] == 0

    async def evaluate(config, records, specs, **kwargs):
        records[0].scenario = "client_deadline"
        records[0].status, records[0].valid = "timeout", False
        records[0].score = False
        return judge_summary(records)

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    monkeypatch.setattr("giraffe.judging.judge_records", evaluate)
    saved = await run_suite(judged_pack_config())
    first = saved.requests[0]
    row = next(item for item in saved.observations["targets"][first.target]["conditions"] if item["condition"]["id"] == first.condition_id)
    assert row["failed"] == sum(runner._request_failed(item) for item in saved.requests if item.condition_id == first.condition_id)
    assert row["failed"] == 0


async def test_capacity_local_drop_shortfall_names_busy_slots(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    monkeypatch.setattr(FakeClient, "delay", .03)
    config = runner_config(checks=["capacity"], traffic={"rates": [1000], "duration_seconds": .005, "max_in_flight": 1})
    result = await run_suite(config)
    row = result.observations["targets"]["service"]["conditions"][0]
    stage = result.observations["targets"]["service"]["capacity"][0]
    assert stage["dropped_local"] > 0 and row["not_sent"] == stage["dropped_local"]
    assert row["reason"] == "dropped: all request slots busy"


async def test_arrival_pending_queue_shortfall_is_not_a_stopped_check():
    config = configuration(arrivals={"requests": 5, "requests_per_second": 1000, "max_pending": 1})
    config.checks = ["arrivals"]
    run = _Run(config, None, None)
    for _ in range(config.concurrency):
        run.slots.get_nowait()

    async def release():
        await asyncio.sleep(.02)
        for _ in range(config.concurrency):
            run.release_slot()

    await asyncio.gather(workloads.arrivals(run, config.targets[0], Client(), {"short": [spec("x", "short", "arrivals")]}), release())
    row = run.observations["local"]["conditions"][0]
    assert row["not_sent"] == 4 and row["sent"] == 1
    assert row["reason"] == "pending queue full"


@pytest.mark.parametrize("check", ["matrix", "mixed", "sessions", "consistency", "logprobs"])
async def test_full_known_plan_is_waiting_before_first_dispatch(check):
    events = []
    config = configuration(
        concurrent_workloads={"scenarios": ["size_matrix", "mixed_interference"], "levels": [1, 2], "samples_per_bucket": 2, "mixed_pairs": 1},
        sessions={"modes": ["fixed", "live"], "sessions": 1},
        consistency={"shapes": ["short", "long"], "levels": [2], "prompts_per_shape": 2, "repetitions": 2,
                     **({"logprobs": {}} if check == "logprobs" else {})})
    config.checks = {"matrix": ["buckets"], "mixed": ["mixed"], "sessions": ["sessions"], "consistency": ["consistency"], "logprobs": ["consistency"]}[check]
    run = _Run(config, events.append, None)
    expected = {"matrix": 18, "mixed": 5, "sessions": 2, "consistency": 4, "logprobs": 4}[check]

    snapshots = []

    class InspectClient(Client):
        async def execute(self, request, first_output_event=None):
            if not snapshots:
                snapshots.extend(dict(row) for row in run.observations["local"]["conditions"])
            return await super().execute(request, first_output_event)

    fixtures = builtin_fixtures(config)
    fixtures = {key: value if isinstance(value, list) else [value] for key, value in fixtures.items()}
    if check in {"matrix", "mixed"}:
        await workloads.buckets(run, config.targets[0], InspectClient())
    elif check == "sessions":
        await workloads.sessions(run, config.targets[0], InspectClient())
    else:
        await workloads.consistency(run, config.targets[0], InspectClient(), fixtures)
    assert len(snapshots) == expected
    assert sum(row["status"] == "waiting" for row in snapshots) == expected - 1
    assert all(row["planned"] is not None for row in snapshots)
    plans = [event for event in events if event["event"] == "condition_planned"]
    first_request = next(index for index, event in enumerate(events) if event["event"] == "request_started")
    assert len(plans) == expected
    assert all(event["status"] == "waiting" for event in plans)
    assert sum(event["event"] == "condition_planned" for event in events[:first_request]) == expected


async def test_condition_events_do_not_occupy_activity_feed(tmp_path, monkeypatch):
    async def waiting(config, *, progress, stop_event):
        run = _Run(config, progress, stop_event)
        await run.group(config.targets[0], Client(), [spec("x", "warm", "serving")], "warm", count=1)
        for index in range(110):
            run.start_condition("local", f"extra_{index}", planned=1)
            run.finish_condition("local", f"extra_{index}")
        await stop_event.wait()
        return report(config, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await asyncio.sleep(.01)
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        assert all(not event["event"].startswith("condition_") for event in detail["events"])
        assert any(event["event"] == "request_completed" for event in detail["events"])
        assert len(detail["live_conditions"]) == 111
        assert detail["progress"]["event"] == "scenario_finished"
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)


@pytest.mark.parametrize("outcome,reason", [
    ("dropped_late", "dropped: scheduler was late"),
    ("not_offered", "not offered: sending window ended"),
])
async def test_capacity_condition_preserves_stage_shortfall_facts(monkeypatch, outcome, reason):
    from giraffe import traffic
    original = traffic.run_stage

    async def stage_with_shortfall(**kwargs):
        stage = await original(**kwargs)
        # The consumer must distinguish these scheduler facts from a local drop.
        stage[outcome] += stage["dropped_local"]
        stage["dropped_local"] = 0
        stage["reasons"] = [reason]
        return stage

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    monkeypatch.setattr(FakeClient, "delay", .03)
    monkeypatch.setattr(traffic, "run_stage", stage_with_shortfall)
    result = await run_suite(runner_config(checks=["capacity"],
        traffic={"rates": [1000], "duration_seconds": .005, "max_in_flight": 1}))
    row = result.observations["targets"]["service"]["conditions"][0]
    stage = result.observations["targets"]["service"]["capacity"][0]
    assert row["not_sent"] == stage[outcome] > 0
    assert row["reason"] == reason


def test_intentional_flag_survives_saved_record_round_trip():
    from giraffe.models import RequestRecord
    record = RequestRecord(id="probe", target="local", fixture_id="probe", scenario="custom_probe",
        condition_id="custom_probe", check_ids=[], started_at="", status="timeout", intentional=True)
    restored = RequestRecord.model_validate_json(record.model_dump_json())
    assert restored.intentional and not runner._request_failed(restored)
    restored.intentional = False
    assert runner._request_failed(restored)
