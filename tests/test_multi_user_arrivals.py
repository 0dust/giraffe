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


@pytest.mark.parametrize("case,expected", [
    ("equal", "pass"), ("slow", "fail"), ("newcomer", "inconclusive"), ("burst", "inconclusive"),
    ("measure_only", "measured"),
])
def test_selected_arrival_fairness_controls_window_and_overall_verdict(case, expected):
    from giraffe.multi_user import _planned_observation, _offer_counts
    from giraffe.multi_user_metrics import summarize
    from tests.test_multi_user_metrics import record

    arrivals = {"requests_per_second": .4}
    if case == "newcomer":
        arrivals["activations"] = {"user-02": 1}
    elif case == "burst":
        arrivals["bursts"] = [{"user_id": "user-01", "at_seconds": 1, "requests": 1}]
    criteria = {"min_samples": 1, "max_throughput_ratio": 1.2}
    if case == "measure_only":
        criteria.update(max_throughput_ratio=None, max_error_percent=None)
    cfg = settings(levels=[2], scenarios=["arrivals"], duration_seconds=10, profile=PROFILE,
                   arrivals=arrivals, pass_criteria=criteria)
    source = _planned_observation(cfg.multi_user)
    window, = source["windows"]
    window.update(start_ms=0, end_ms=10000, planned_end_ms=10000, duration_seconds=10,
                  cut_short=False, reason=None)
    records = []
    for offer in window["offers"]:
        uid, start = offer["user_id"], offer["offset_seconds"]*1000
        end = 11000 if case != "equal" and uid == "user-02" and start > 0 else start+1000
        request = record(uid, identity=f'request-{offer["index"]}', dispatch=start, end=end,
            tokens=16, input_tokens=64, requested_input_tokens=64, requested_output_tokens=16,
            requested_max_tokens=16, stream=True, answer_chunks=3, answer_reads=3,
            last_output_ms=end-start-100, stream_end_ms=end-start)
        request.workload.update(window_id=window["id"], scenario="arrivals", role="arrivals",
                                cohort=offer["cohort"])
        records.append(request)
        offer.update(offered=True, dispatched=True, outcome="dispatched", request_id=request.id)
    window["offer_accounting"] = _offer_counts(window["offers"])
    for user in window["users"]:
        own = [offer for offer in window["offers"] if offer["user_id"] == user["user_id"]]
        user.update(activated_ms=user["activation_seconds"]*1000, ready_count=len(own),
                    dispatched_count=len(own), offer_accounting=_offer_counts(own))
    summary = summarize(source, records)
    measured = summary["windows"][0]["fairness"]
    assert measured["status"] == expected
    assert measured["qualified"] == (case in {"equal", "slow", "measure_only"})
    if case == "slow":
        assert measured["ratio"] == 2.5
    if case in {"newcomer", "burst"}:
        assert any("arrival exposure or burst work differs" in reason for reason in measured["reasons"])
    assert summary["windows"][0]["status"] == summary["status"] == expected


@pytest.mark.parametrize("scenario", ["arrivals", "recovery"])
@pytest.mark.parametrize("default_slots", [4, 7])
def test_scheduled_users_run_with_omitted_global_concurrency(monkeypatch, scenario, default_slots):
    from giraffe.models import RunConfig, required_concurrency

    async def run():
        raw = settings(levels=[2], scenarios=[scenario], duration_seconds=.2, profile=PROFILE,
            arrivals={"requests_per_second": 10}, recovery={"control_seconds": .2,
                "sustained_seconds": .2, "lighter_seconds": .2,
                "sustained_requests_per_second": 10, "lighter_requests_per_second": 5}).model_dump()
        raw.pop("concurrency")
        raw["default_concurrency"] = default_slots
        cfg = RunConfig.model_validate(raw)
        assert cfg.concurrency is None
        assert required_concurrency(cfg)["by_check"]["multi_user"] == default_slots
        state = runner._Run(cfg, None, None)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        windows = state.observations["service"]["multi_user"]["windows"]
        assert all(w["offer_accounting"]["dispatched"] == len(w["offers"]) for w in windows)
        assert state.records and state.slots.qsize() == default_slots
    run_virtual(monkeypatch, run())


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
        assert all(r.workload["ready_ms"] == r.workload["scheduled_ms"] for r in state.records)
        assert all(r.workload["ready_ms"] >= window["start_ms"] for r in state.records)
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
