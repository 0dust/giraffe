"""Persistent users share request admission without sharing their lifetime."""
import asyncio

import pytest
from pydantic import ValidationError

from giraffe import runner
from giraffe.models import required_concurrency
from tests.test_runner import FakeClient, config
from tests.test_review26_phase4_accuracy import run_virtual


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def settings(**changes):
    return config(checks=["multi_user"], max_requests=None, max_duration_seconds=10,
                  concurrency=5, multi_user={"levels": [20], "duration_seconds": .1,
                  "think_time_seconds": {"values": [.01]}, **changes})


def test_selection_is_opt_in_and_population_does_not_clamp_to_slots():
    cfg = settings()
    assert cfg.multi_user.levels == [20]
    assert required_concurrency(cfg)["by_check"]["multi_user"] == 20
    assert "multi_user" not in config().checks
    with pytest.raises(ValidationError):
        settings(levels=[33])
    with pytest.raises(ValidationError):
        settings(duration_seconds=0)
    with pytest.raises(ValidationError):
        settings(scenarios=["cancellation"])


def test_twenty_users_share_five_slots_and_repeat(monkeypatch):
    from giraffe.multi_user import execute

    async def scenario():
        cfg = settings()
        state = runner._Run(cfg, None, None)
        FakeClient.active = FakeClient.peak = 0
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        observation = state.observations["service"]["multi_user"]
        window = observation["windows"][0]
        assert len(window["users"]) == 20
        assert len({r.workload["user_id"] for r in state.records}) == 20
        assert all(u["dispatched_count"] >= 2 for u in window["users"])
        assert FakeClient.peak == 5
        assert state.slots.qsize() == 5
        assert all(r.dispatch_ms < window["end_ms"] for r in state.records)
        assert all(r.check_ids == ["multi_user"] for r in state.records)

    run_virtual(monkeypatch, scenario())


def test_profile_assignments_and_first_requests_are_stable_across_levels(monkeypatch):
    from giraffe.multi_user import execute, population_plan

    assert population_plan(settings(levels=[1], shapes=["short_short", "medium_long"]).multi_user) == population_plan(
        settings(levels=[1, 5, 20], shapes=["short_short", "medium_long"]).multi_user)[:1]

    async def scenario():
        cfg = settings(levels=[1, 5, 20], shapes=["short_short", "medium_long"])
        state = runner._Run(cfg, None, None)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        first = [r for r in state.records if r.workload["user_id"] == "user-01"
                 and r.workload["request_sequence"] == 1]
        assert len(first) == 3
        assert len({(r.workload["profile"], r.requested_input_tokens, r.requested_max_tokens)
                    for r in first}) == 1

    run_virtual(monkeypatch, scenario())


def test_cutoff_blocks_queued_dispatch_but_allows_admitted_request_to_drain(monkeypatch):
    from giraffe.multi_user import execute

    async def scenario():
        cfg = settings(levels=[2], duration_seconds=.05).model_copy(update={"concurrency": 1})
        state = runner._Run(cfg, None, None)
        monkeypatch.setattr(FakeClient, "delay", .1)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window = state.observations["service"]["multi_user"]["windows"][0]
        assert len(state.records) == 1
        assert state.records[0].status == "completed"
        assert state.records[0].dispatch_ms < window["end_ms"] < state.records[0].completed_ms
        pending = next(user for user in window["users"] if not user["dispatched_count"])
        assert pending["ready_count"] == 1
        assert pending["pending_ready_ms"] is not None
        assert pending["pending_observed_ms"] == window["end_ms"]
        assert state.slots.qsize() == 1
        assert not window["cut_short"]

    run_virtual(monkeypatch, scenario())


def test_warmup_fully_drains_before_shared_measurement(monkeypatch):
    from giraffe.multi_user import execute

    async def scenario():
        cfg = settings(levels=[2], warmup_seconds=.03, duration_seconds=.05)
        state = runner._Run(cfg, None, None)
        monkeypatch.setattr(FakeClient, "delay", .04)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        warmup, measurement = state.observations["service"]["multi_user"]["windows"]
        records = [r for r in state.records if r.workload["phase"] == "warmup"]
        assert max(r.completed_ms for r in records) <= measurement["start_ms"]
        assert measurement["start_ms"] > warmup["end_ms"]
        assert {r.workload["phase"] for r in state.records} == {"warmup", "measurement"}

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("budget", [1, 5])
def test_exhausted_shared_budget_preserves_every_user_and_terminal_record(monkeypatch, budget):
    from giraffe.multi_user import execute

    async def scenario():
        cfg = settings().model_copy(update={"max_requests": budget})
        persisted = []
        state = runner._Run(cfg, None, None)
        state.record_sink = persisted.append
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window = state.observations["service"]["multi_user"]["windows"][0]
        assert len(window["users"]) == 20
        assert len(state.records) == len(persisted) == budget
        assert len({r.id for r in persisted}) == budget
        assert sum(u["dispatched_count"] for u in window["users"]) == budget
        assert all(r.status == "completed" for r in state.records)
        assert state.slots.qsize() == 5
        assert window["cut_short"]

    run_virtual(monkeypatch, scenario())


def test_run_selection_planning_and_live_identity(monkeypatch):
    from giraffe.multi_user import execute  # noqa: F401  preload for virtual clock
    from giraffe.planning import request_plan

    async def scenario():
        cfg = settings(levels=[2])
        plan = request_plan(cfg)
        row = next(r for r in plan["rows"] if r["id"] == "multi_user_2")
        assert row["users"] == 2
        assert row["simultaneous_limit"] == 5
        events = []
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        report = await runner.run_suite(cfg, progress=events.append)
        assert [check.id for check in report.checks if check.status != "skipped"] == ["multi_user"]
        assert report.manifest["fixture_pack"]["workload_fixture_sha256"]
        requests = [event for event in events if event["event"] == "request_completed"]
        assert requests and {event["user"]["user_id"] for event in requests} == {"user-01", "user-02"}
        assert all(set(event["user"]) == {"user_id", "population", "profile", "window_id", "phase", "request_sequence"}
                   for event in requests)

    run_virtual(monkeypatch, scenario())


def test_actual_http_requests_share_slots_and_persist_user_identity():
    from giraffe.client import LLMClient
    from giraffe.multi_user import execute
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    async def scenario(endpoint):
        cfg = settings(duration_seconds=.4).model_copy(update={
            "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=20) as client:
            await execute(state, cfg.targets[0], client)
        assert 2 <= endpoint.peak_active <= 5
        assert len({r.workload["user_id"] for r in state.records}) == 20
        assert len({r.id for r in state.records}) == len(endpoint.requests)
        assert state.slots.qsize() == 5

    with FakeEndpoint() as endpoint:
        asyncio.run(scenario(endpoint))


def test_stop_during_drain_preserves_closed_window_and_terminal_records(monkeypatch):
    from giraffe.multi_user import execute  # noqa: F401  preload for virtual clock

    async def scenario():
        cfg = settings(levels=[2], duration_seconds=.05)
        stop = asyncio.Event()
        scheduled = False
        def progress(event):
            nonlocal scheduled
            if event["event"] == "request_started" and not scheduled:
                scheduled = True
                asyncio.get_running_loop().call_later(.08, stop.set)
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        monkeypatch.setattr(FakeClient, "delay", 1)
        report = await runner.run_suite(cfg, stop_event=stop, progress=progress)
        window = report.observations["targets"]["service"]["multi_user"]["windows"][0]
        assert window["duration_seconds"] == pytest.approx(.05)
        assert not window["cut_short"]
        assert window["drain_reason"] == "cancelled by user"
        assert len(report.requests) == len({record.id for record in report.requests}) == 2
        assert all(record.status == "cancelled" for record in report.requests)
        assert all(record.workload["user_id"] for record in report.requests)
        assert FakeClient.active == 0

    run_virtual(monkeypatch, scenario())


def test_cancelling_users_waiting_for_slots_releases_only_owned_slots(monkeypatch):
    from giraffe.multi_user import execute

    async def scenario():
        cfg = settings(levels=[20], duration_seconds=1).model_copy(update={"concurrency": 1})
        state = runner._Run(cfg, None, None)
        monkeypatch.setattr(FakeClient, "delay", 1)
        task = asyncio.create_task(execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg)))
        asyncio.get_running_loop().call_later(.05, task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await task
        window = state.observations["service"]["multi_user"]["windows"][0]
        assert len(window["users"]) == 20
        assert len(state.records) == 1
        assert state.records[0].status == "cancelled"
        assert sum(user["dispatched_count"] for user in window["users"]) == 1
        assert state.slots.qsize() == 1
        assert FakeClient.active == 0
        assert window["cut_short"]

    run_virtual(monkeypatch, scenario())


def test_preparation_interruption_preserves_unactivated_population(monkeypatch):
    from giraffe import multi_user

    async def scenario():
        cfg = settings(levels=[1, 5, 20])
        state = runner._Run(cfg, None, None)
        async def interrupted(*args, **kwargs):
            state.stop.set()
            raise asyncio.CancelledError
        monkeypatch.setattr(multi_user, "calibrate", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await multi_user.execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        windows = state.observations["service"]["multi_user"]["windows"]
        assert [len(window["users"]) for window in windows] == [1, 5, 20]
        assert all(user["activated_ms"] is None for window in windows for user in window["users"])
        assert not state.records

    run_virtual(monkeypatch, scenario())


def test_result_uses_scheduler_windows_and_keeps_all_users(monkeypatch):
    from giraffe.multi_user import execute, result

    async def scenario():
        cfg = settings()
        state = runner._Run(cfg, None, None)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        check = result(state, cfg.targets[0])
        summary = check.metrics["multi_user"]
        assert len(summary["windows"][0]["users"]) == 20
        assert summary["status"] == check.status == "pass"
        assert sum(user["outcomes"]["attempted"] for user in summary["windows"][0]["users"]) == len(state.records)
        assert state.observations["service"]["multi_user"]["summary"] == summary
        assert check.evidence_ids == [r.id for r in state.records]

    run_virtual(monkeypatch, scenario())


def test_stop_before_target_execution_still_reports_planned_users():
    async def scenario():
        stop = asyncio.Event()
        stop.set()
        report = await runner.run_suite(settings(), stop_event=stop)
        summary = next(check.metrics['multi_user'] for check in report.checks
                       if check.id == 'multi_user')
        assert len(summary['population']) == 20
        assert len(summary['windows'][0]['users']) == 20
        assert all(user['coverage']['status'] == 'unexercised'
                   for user in summary['windows'][0]['users'])
        assert not report.requests
        assert summary['status'] == 'inconclusive'
    asyncio.run(scenario())


def test_request_setup_crossing_cutoff_does_not_send_or_consume_budget(monkeypatch):
    from giraffe.multi_user import execute, result

    async def scenario():
        cfg = settings(levels=[1], duration_seconds=.05)
        state = runner._Run(cfg, None, None)
        original = state._condition
        def slow_request_setup(*args, **kwargs):
            condition = original(*args, **kwargs)
            if kwargs.get("request_parameters") is not None:
                asyncio.get_running_loop().now += .06
            return condition
        monkeypatch.setattr(state, "_condition", slow_request_setup)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        assert not state.records
        assert state.attempted == state.budget_used == 0
        assert state.counts["service"] == 0
        user = state.observations["service"]["multi_user"]["windows"][0]["users"][0]
        assert user["dispatched_count"] == 0
        assert user["pending_ready_ms"] is not None
        summary = result(state, cfg.targets[0]).metrics["multi_user"]["windows"][0]["users"][0]
        assert summary["outcomes"]["missing_terminal"] == 0
        assert state.slots.qsize() == 1

    run_virtual(monkeypatch, scenario())


def test_budget_closure_wakes_thinking_users_without_idle_drain(monkeypatch):
    from giraffe.multi_user import execute

    async def scenario():
        cfg = settings(levels=[2], duration_seconds=90, seed=42,
                       think_time_seconds={"values": [0, 60]}).model_copy(
                           update={"max_requests": 3, "max_duration_seconds": 100})
        state = runner._Run(cfg, None, None)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window = state.observations["service"]["multi_user"]["windows"][0]
        assert len(state.records) == 3
        assert all(record.status == "completed" for record in state.records)
        assert asyncio.get_running_loop().time() - state.start < .05
        assert window["drain_seconds"] < .01
        assert state.slots.qsize() == 2

    run_virtual(monkeypatch, scenario())


def test_saved_question_sequence_ignores_peer_token_counts_and_population(monkeypatch):
    from giraffe.multi_user import execute
    from giraffe.models import RequestSpec

    class MeasuredClient(FakeClient):
        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            # First candidate initially fits Short by its character estimate,
            # but actual usage is outside Short. This must not rewrite the plan.
            record.input_tokens = 281 if spec.fixture_id == "a" else 25
            return record

    async def scenario():
        cfg = settings(levels=[1, 2], duration_seconds=.015,
                       think_time_seconds={"values": [0]})
        state = runner._Run(cfg, None, None)
        state.load_custom_specs = [RequestSpec(fixture_id=name, scenario="saved", check_ids=["load"],
            messages=[{"role": "user", "content": "a" * count}], input_chars=count)
            for name, count in (("a", 921), ("b", 100))]
        await execute(state, cfg.targets[0], MeasuredClient(cfg.targets[0], cfg))
        assert {record.workload["population"] for record in state.records} == {1, 2}
        assert {record.workload["user_id"] for record in state.records} == {"user-01", "user-02"}
        assert all(record.fixture_id == ("a" if record.workload["request_sequence"] % 2 else "b")
                   for record in state.records)

    run_virtual(monkeypatch, scenario())
