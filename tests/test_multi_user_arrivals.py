"""Finite user offers reconcile independently from endpoint attempts."""
import asyncio

import pytest

from giraffe import runner
from giraffe.multi_user import execute, result
from tests.test_multi_user import settings
from tests.test_multi_user_scenarios import PROFILE
from tests.test_review26_phase4_accuracy import run_virtual
from tests.test_runner import FakeClient

pytestmark = pytest.mark.usefixtures("local_tokenizer")


def arrival_settings(**changes):
    return settings(levels=[2], scenarios=["arrivals"], duration_seconds=.2, profile=PROFILE,
        arrivals={"requests_per_second": 20, "activations": {"user-02": .1},
                  "bursts": [{"user_id": "user-01", "at_seconds": .05, "requests": 3}]}, **changes)


def test_preview_counts_offers_without_materializing_request_ledger(monkeypatch):
    from giraffe import multi_user
    from giraffe.planning import request_plan

    def unexpected(*args):
        pytest.fail("Setup preview must not allocate the execution offer ledger")
    monkeypatch.setattr(multi_user, "_offer_plan", unexpected)
    row = next(row for row in request_plan(arrival_settings())["rows"] if row["id"] == "multi_user_2")
    assert row["scheduled_offers"] == 9


@pytest.mark.parametrize("budget", [None, 3])
def test_user_offers_reconcile_busy_slots_and_budget(monkeypatch, budget):
    async def run():
        cfg = arrival_settings().model_copy(update={"concurrency": 1, "max_requests": budget})
        state = runner._Run(cfg, None, None)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window, = state.observations["service"]["multi_user"]["windows"]
        offers = window["offers"]
        assert len(offers) == 9
        accounting = window["offer_accounting"]
        assert accounting["planned"] == accounting["offered"] + accounting["not_offered"]
        assert accounting["offered"] == sum(accounting[k] for k in
            ("dispatched", "dropped_local", "dropped_late", "unissued"))
        assert accounting["dispatched"] == len(state.records)
        assert accounting["dropped_local"] > 0
        assert all(o["user_id"] and o["cohort"] for o in offers)
        assert all(o["offset_seconds"] >= .1 for o in offers if o["user_id"] == "user-02")
        assert state.slots.qsize() == 1
        if budget:
            assert accounting["offered"] == budget and accounting["not_offered"] == 6
        assert result(state, cfg.targets[0]).metrics["multi_user"]["windows"][0]["fairness"]["status"] != "pass"
    run_virtual(monkeypatch, run())


def test_http_single_user_burst_overlaps_under_global_cap():
    from giraffe.client import LLMClient
    from giraffe.models import Target, required_concurrency
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[1], scenarios=["arrivals"], duration_seconds=.5, profile=PROFILE,
            arrivals={"requests_per_second": 1, "bursts": [
                {"user_id": "user-01", "at_seconds": .1, "requests": 3}]}).model_copy(update={
                    "concurrency": 3, "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        assert required_concurrency(cfg)["by_check"]["multi_user"] >= 3
        state = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=3) as client:
            await execute(state, cfg.targets[0], client)
        assert 2 <= endpoint.peak_active <= 3
        window, = state.observations["service"]["multi_user"]["windows"]
        assert window["peak_in_flight"] == 3
        assert window["offer_accounting"]["planned"] == 4
        assert sum(window["offer_accounting"][key] for key in
                   ("dispatched", "dropped_local", "dropped_late", "unissued", "not_offered")) == 4
        assert {r.workload["user_id"] for r in state.records} == {"user-01"}
        assert len({r.workload["request_sequence"] for r in state.records}) == len(state.records)
        assert any(a.dispatch_ms < b.dispatch_ms < a.completed_ms for a in state.records for b in state.records)
    with FakeEndpoint(first_token_delay=.05, chunk_delay=.03) as endpoint:
        asyncio.run(run(endpoint))


def test_generator_stall_discards_due_user_offers_without_catchup(monkeypatch):
    async def run():
        cfg = settings(levels=[2], scenarios=["arrivals"], duration_seconds=.5, profile=PROFILE,
                       arrivals={"requests_per_second": 20})
        state = runner._Run(cfg, None, None)
        reserve = state.reserve_offer
        count = 0
        def delayed(target):
            nonlocal count
            count += 1
            if count == 3:
                asyncio.get_running_loop().now += .2
            return reserve(target)
        state.reserve_offer = delayed
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window, = state.observations["service"]["multi_user"]["windows"]
        assert window["offer_accounting"]["dropped_late"] == 10
        assert window["offer_accounting"]["not_offered"] == 0
        assert not any(.05 <= o["offset_seconds"] <= .25 and o["dispatched"] for o in window["offers"])
        assert window["scheduler_stalls"]
        assert all(o["user_id"] in {"user-01", "user-02"} for o in window["offers"])
    run_virtual(monkeypatch, run())


def test_reserved_offer_without_http_dispatch_is_unissued(monkeypatch):
    async def run():
        cfg = settings(levels=[1], scenarios=["arrivals"], duration_seconds=.2, profile=PROFILE,
                       arrivals={"requests_per_second": 5})
        state = runner._Run(cfg, None, None)
        async def never_sent(*args, **kwargs):
            return None
        state.request = never_sent
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window, = state.observations["service"]["multi_user"]["windows"]
        assert window["offer_accounting"] == {"planned": 1, "offered": 1, "dispatched": 0,
            "dropped_local": 0, "dropped_late": 0, "unissued": 1, "not_offered": 0}
        assert not state.records
        assert state.slots.qsize() == 5
    run_virtual(monkeypatch, run())


def test_http_interrupt_retains_partial_offers_and_future_recovery_phases():
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint
    async def run(endpoint):
        cfg = settings(levels=[2], scenarios=["arrivals", "recovery"], duration_seconds=3, profile=PROFILE,
            arrivals={"requests_per_second": 2}).model_copy(update={
                "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=5) as client:
            task = asyncio.create_task(execute(state, cfg.targets[0], client))
            async with asyncio.timeout(3):
                while len(endpoint.requests) < 2:
                    await asyncio.sleep(.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        windows = state.observations["service"]["multi_user"]["windows"]
        assert windows[0]["cut_short"] and windows[0]["offer_accounting"]["dispatched"] == 2
        assert windows[0]["offer_accounting"]["not_offered"] == 10
        assert len(state.records) == len({r.id for r in state.records}) == 2
        assert all(w["start_ms"] is None and w["offer_accounting"]["not_offered"] > 0 for w in windows[1:])
        assert state.slots.qsize() == 5
    with FakeEndpoint(first_token_delay=.5) as endpoint:
        asyncio.run(run(endpoint))


@pytest.mark.parametrize("changes", [
    {"activations": {"user-03": .1}}, {"activations": {"user-02": .2}},
    {"bursts": [{"user_id": "user-01", "at_seconds": .1, "requests": 3, "spacing_seconds": .1}]},
])
def test_invalid_user_or_out_of_window_schedule_rejected(changes):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        settings(levels=[2], scenarios=["arrivals"], duration_seconds=.2, arrivals=changes)
