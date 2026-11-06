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


def test_activation_adjusted_offer_bound_accepts_valid_plan():
    from giraffe.multi_user import _planned_observation

    cfg = settings(levels=[4], scenarios=["arrivals"], duration_seconds=1500, arrivals={
        "requests_per_second": 20, "activations": {f"user-0{i}": 1000 for i in range(2, 5)}})
    window, = _planned_observation(cfg.multi_user, include_offers=False)["windows"]
    assert window["scheduled_offers"] == 60000


def test_activation_adjusted_offer_bound_is_used_during_execution(monkeypatch):
    async def run():
        cfg = settings(levels=[2], scenarios=["arrivals"], duration_seconds=101, profile=PROFILE,
            arrivals={"requests_per_second": 1000, "activations": {"user-01": 100, "user-02": 100}}
        ).model_copy(update={"max_duration_seconds": 110})
        state = runner._Run(cfg, None, None)
        target = cfg.targets[0]
        await execute(state, target, FakeClient(target, cfg))
        window, = state.observations[target.name]["multi_user"]["windows"]
        accounting = window["offer_accounting"]
        assert accounting["planned"] == accounting["offered"] == 2000
        assert accounting["not_offered"] == 0
        assert accounting["dispatched"] == len(state.records) > 0
        assert accounting["dispatched"] + accounting["dropped_local"] == 2000
        assert {record.workload["user_id"] for record in state.records} == {"user-01", "user-02"}
        assert not window["cut_short"]

    run_virtual(monkeypatch, run())


def test_aggregate_offer_bound_rejects_large_plan_before_allocation(monkeypatch):
    from giraffe import multi_user
    from giraffe.models import RunConfig
    from pydantic import ValidationError

    monkeypatch.setattr(multi_user, "_offer_plan", lambda *args: pytest.fail("Validation allocated an offer ledger"))
    with pytest.raises(ValidationError, match="total scheduled offers"):
        RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
            checks=["multi_user"], max_requests=1, concurrency=32, multi_user={
                "scenarios": ["arrivals", "recovery"], "levels": list(range(2, 33)), "duration_seconds": 3.125,
                "arrivals": {"requests_per_second": 1000}, "recovery": {
                    "control_seconds": 6.25, "lighter_seconds": 6.25, "sustained_seconds": 3.125,
                    "lighter_requests_per_second": 500, "sustained_requests_per_second": 1000}})


def test_aggregate_offer_bound_counts_targets_and_accepts_exact_limit(monkeypatch):
    from giraffe import multi_user
    from giraffe.models import RunConfig
    from pydantic import ValidationError

    monkeypatch.setattr(multi_user, "_offer_plan", lambda *args: pytest.fail("Validation allocated an offer ledger"))
    target = {"url": "http://localhost/v1", "model": "test"}
    values = {"targets": [{**target, "name": str(i)} for i in range(2)], "checks": ["multi_user"],
        "max_requests": 1, "multi_user": {"scenarios": ["arrivals"], "levels": [2], "duration_seconds": 25,
                                         "warmup_seconds": 1, "arrivals": {"requests_per_second": 1000}}}
    cfg = RunConfig.model_validate(values)
    windows = multi_user._planned_observation(cfg.multi_user, include_offers=False)["windows"]
    assert sum(window.get("scheduled_offers", 0) for window in windows)*len(cfg.targets) == 100000
    values["targets"].append({**target, "name": "third"})
    with pytest.raises(ValidationError, match="total scheduled offers"):
        RunConfig.model_validate(values)


@pytest.mark.parametrize("case,expected", [
    ("equal", "pass"), ("unused_think_time", "pass"), ("slow", "fail"),
    ("newcomer", "inconclusive"), ("burst", "inconclusive"),
    ("unequal_local_drops", "inconclusive"), ("equal_local_drops", "inconclusive"),
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
                   arrivals=arrivals, pass_criteria=criteria,
                   think_time_seconds={"values": [0, .2] if case == "unused_think_time" else [0]})
    source = _planned_observation(cfg.multi_user)
    window, = source["windows"]
    window.update(start_ms=0, end_ms=10000, planned_end_ms=10000, duration_seconds=10,
                  cut_short=False, reason=None)
    records = []
    for offer in window["offers"]:
        uid, start = offer["user_id"], offer["offset_seconds"]*1000
        if start > 0 and (case == "equal_local_drops" or case == "unequal_local_drops" and uid == "user-02"):
            offer.update(offered=True, outcome="dropped_local")
            continue
        end = 11000 if case == "slow" and uid == "user-02" and start > 0 else start+1000
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
                    dispatched_count=sum(offer["dispatched"] for offer in own), offer_accounting=_offer_counts(own))
    summary = summarize(source, records)
    measured = summary["windows"][0]["fairness"]
    assert measured["status"] == expected
    assert measured["qualified"] == (case in {"equal", "unused_think_time", "slow", "measure_only"})
    if case == "slow":
        assert measured["ratio"] == 2.5
    if case in {"newcomer", "burst"}:
        assert any("arrival exposure or burst work differs" in reason for reason in measured["reasons"])
    if case.endswith("local_drops"):
        assert any("offer schedule" in reason for reason in measured["reasons"])
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


def test_other_check_transport_pool_does_not_raise_user_admission_limit(monkeypatch):
    from giraffe.models import RunConfig

    async def run():
        raw = settings(levels=[1], scenarios=["arrivals"], duration_seconds=.2, profile=PROFILE,
            arrivals={"requests_per_second": 1, "bursts": [
                {"user_id": "user-01", "at_seconds": 0, "requests": 8}]}).model_dump()
        raw.pop("concurrency")
        raw.update(default_concurrency=4, checks=["multi_user", "sessions"],
                   sessions={"sessions": 16, "concurrency": 16})
        cfg = RunConfig.model_validate(raw)
        state = runner._Run(cfg, None, None)
        assert state.max_connections == 16
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window, = state.observations["service"]["multi_user"]["windows"]
        assert window["peak_in_flight"] == 4
        assert window["offer_accounting"]["dispatched"] == 4
        assert window["offer_accounting"]["dropped_local"] == 5
        assert state.slots.qsize() == 16
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


def test_arrival_schedule_reports_steady_activation_stages_and_burst_offers(monkeypatch):
    from giraffe.models import RunReport
    from giraffe.performance import build_performance

    async def run():
        cfg = arrival_settings().model_copy(update={"concurrency": 8})
        state = runner._Run(cfg, None, None)
        target = cfg.targets[0]
        await execute(state, target, FakeClient(target, cfg))
        observation = state.observations[target.name]
        first, second, drain = observation["schedule_transitions"]
        assert first["end_ms"] == second["start_ms"]
        assert first["end_ms"] - first["start_ms"] == pytest.approx(100)
        assert second["end_ms"] - second["start_ms"] == pytest.approx(100)
        assert [first["intended_rate_rps"], second["intended_rate_rps"]] == [20, 40]
        assert drain["kind"] == "drain"
        assert all(row.traffic_stage is None and row.traffic_rate_rps is None for row in state.records)
        check = result(state, target)
        window, = check.metrics["multi_user"]["windows"]
        assert window["scheduled_offers"] == 9
        assert window["planned_requests_per_second"] == 45
        assert window["burst_offer_accounting"]["planned"] == 3
        assert window["burst_offer_accounting"]["dispatched"] == 3

        report = RunReport(run_id="arrivals", suite_version="test", overall=check.status,
                           started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z",
                           manifest={"config": cfg.model_dump(mode="json")}, requests=state.records,
                           checks=[check], observations={"targets": {target.name: observation}})
        view = build_performance(report)["targets"][0]
        metrics = view["summaries"][view["default_window"]]["metrics"]
        assert metrics["offered_rate"]["value"] == [20, 40]
        assert metrics["offered_rate"]["label"] == "Scheduled steady arrival rate"
        stage, *_ = [entry for entry in view["windows"] if entry["kind"] == "stage" and not entry["includes_drain"]]
        stage_metrics = build_performance(report, window=stage["id"])["targets"][0]["summaries"][stage["id"]]["metrics"]
        assert stage_metrics["drops"]["value"][0]["planned"] == 5

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


@pytest.mark.parametrize("delay,duration,stop,expected", [
    (.01, .5, None, "dispatched"), (.1, .5, None, "dispatched"),
    (.2, .5, None, "dropped_late"), (.6, .5, None, "dropped_late"),
    (.06, .05, None, "unissued"), (.2, .05, None, "unissued"),
    (.2, .5, "deadline", "unissued"),
])
def test_final_admission_rechecks_condition_setup_delay(monkeypatch, delay, duration, stop, expected):
    async def run():
        cfg = settings(levels=[1], scenarios=["arrivals"], duration_seconds=duration,
                       profile=PROFILE, arrivals={"requests_per_second": 1})
        state = runner._Run(cfg, None, None)
        if stop == "deadline":
            state.deadline = state.start + .15
        condition = state._condition
        calls = []

        class SpyClient(FakeClient):
            async def execute(self, spec, **kwargs):
                calls.append(spec.workload["offer_index"])
                return await super().execute(spec, **kwargs)

        def delayed(*args, **kwargs):
            value = condition(*args, **kwargs)
            if kwargs.get("request_parameters") is not None:
                asyncio.get_running_loop().now += delay
            return value

        monkeypatch.setattr(state, "_condition", delayed)
        await execute(state, cfg.targets[0], SpyClient(cfg.targets[0], cfg))
        await asyncio.sleep(0)  # Run the scheduler's queued slot-release callback.
        window, = state.observations["service"]["multi_user"]["windows"]
        offer, = window["offers"]
        assert offer["outcome"] == expected
        assert offer["scheduler_lag_ms"] == pytest.approx(delay*1000)
        assert len(calls) == len(state.records) == state.attempted == int(expected == "dispatched")
        assert window["offer_accounting"][expected] == 1
        assert state.slots.qsize() == state.max_connections
    run_virtual(monkeypatch, run())


def test_final_admission_stall_discards_backlog_without_catchup(monkeypatch):
    async def run():
        cfg = settings(levels=[1], scenarios=["arrivals"], duration_seconds=.5,
                       profile=PROFILE, arrivals={"requests_per_second": 20})
        state = runner._Run(cfg, None, None)
        condition = state._condition
        stalled = False

        def delayed(*args, **kwargs):
            nonlocal stalled
            value = condition(*args, **kwargs)
            if kwargs.get("request_parameters") is not None and not stalled:
                stalled = True
                asyncio.get_running_loop().now += .2
            return value

        monkeypatch.setattr(state, "_condition", delayed)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        window, = state.observations["service"]["multi_user"]["windows"]
        assert window["offer_accounting"] == {"planned": 10, "offered": 10, "dispatched": 5,
            "dropped_local": 0, "dropped_late": 5, "unissued": 0, "not_offered": 0}
        assert all(offer["outcome"] == "dropped_late" for offer in window["offers"][:5])
        assert all(record.dispatch_ms > 200 for record in state.records)
        assert window["offers"][0]["scheduler_lag_ms"] == pytest.approx(200)
        assert window["scheduler_stalls"] == [
            {"start_ms": 0, "end_ms": 200}, {"start_ms": 50, "end_ms": 200}]
    run_virtual(monkeypatch, run())


@pytest.mark.parametrize("immediate", [False, True])
def test_scheduled_request_enters_transport_before_blocking_progress(monkeypatch, immediate):
    from tests.test_multi_user_metrics import record as user_record

    async def run():
        cfg = settings(levels=[1], scenarios=["arrivals"], duration_seconds=.05,
                       profile=PROFILE, arrivals={"requests_per_second": 1})
        events, dispatches = [], []

        def progress(event):
            if event.get("user"):
                events.append(event)
                if event["event"] == "request_started":
                    asyncio.get_running_loop().now += .2

        state = runner._Run(cfg, progress, None)

        class SpyClient(FakeClient):
            async def execute(self, spec, **kwargs):
                dispatches.append((asyncio.get_running_loop().now-state.start)*1000)
                if immediate:
                    return user_record("user-01", end=0, scenario=spec.scenario, workload={})
                return await super().execute(spec, **kwargs)

        await execute(state, cfg.targets[0], SpyClient(cfg.targets[0], cfg))
        window, = state.observations["service"]["multi_user"]["windows"]
        record, = state.records
        assert dispatches == pytest.approx([record.dispatch_ms])
        assert record.dispatch_ms < window["planned_end_ms"]
        if immediate:
            assert record.completed_ms == pytest.approx(record.dispatch_ms)
        assert window["offer_accounting"]["dispatched"] == 1
        assert window["offers"][0]["request_id"] == record.id
        assert [event["event"] for event in events] == ["request_started", "request_completed"]
        assert [event["request_slot"] for event in events] == [0, 0]
        assert state.slots.qsize() == state.max_connections

    run_virtual(monkeypatch, run())


@pytest.mark.parametrize(("prepare", "delay", "duration", "global_limit", "response_delay", "expected", "status"), [
    ("hash", .2, .05, 10, 0, "unissued", None),
    ("hash", .2, .5, 10, 0, "dropped_late", None),
    ("hash", .2, .5, .15, 0, "unissued", None),
    ("build", .2, .5, 10, 0, "dropped_late", None),
    ("build", .02, .05, 10, 0, "dispatched", "completed"),
    ("build", .08, .1, 10, .02, "dispatched", "completed"),
    ("build", .08, .1, 10, .05, "dispatched", "timeout"),
    ("build", .08, .1, .095, .05, "dispatched", "cancelled"),
])
def test_scheduled_admission_checks_real_client_preparation(
    monkeypatch, prepare, delay, duration, global_limit, response_delay, expected, status,
):
    import json
    from types import SimpleNamespace

    import httpx

    from giraffe import client as client_module

    async def run():
        cfg = settings(levels=[1], scenarios=["arrivals"], duration_seconds=duration,
                       profile=PROFILE, arrivals={"requests_per_second": 1}).model_copy(
                           update={"request_timeout_seconds": .03})
        events, dispatches = [], []

        def progress(event):
            if event.get("user"):
                events.append(event)
                if event["event"] == "request_started" and not response_delay:
                    asyncio.get_running_loop().now += .2

        def hash_input(value, **kwargs):
            if prepare == "hash" and isinstance(value, dict) and value.keys() == {"messages", "options"}:
                asyncio.get_running_loop().now += delay
            return json.dumps(value, **kwargs)

        class HTTPClient(httpx.AsyncClient):
            def build_request(self, *args, **kwargs):
                request = super().build_request(*args, **kwargs)
                if prepare == "build":
                    asyncio.get_running_loop().now += delay
                return request

        state = runner._Run(cfg, progress, None)
        state.deadline = state.start + global_limit

        async def respond(request):
            dispatches.append((asyncio.get_running_loop().now-state.start)*1000)
            if response_delay:
                await asyncio.sleep(response_delay)
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"1 2"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

        monkeypatch.setattr(client_module, "json", SimpleNamespace(**(vars(json) | {"dumps": hash_input})))
        async with HTTPClient(transport=httpx.MockTransport(respond)) as transport:
            client = client_module.LLMClient(cfg.targets[0], cfg)
            client._client = transport
            await execute(state, cfg.targets[0], client)
        await asyncio.sleep(0)
        window, = state.observations["service"]["multi_user"]["windows"]
        offer, = window["offers"]
        admitted = int(expected == "dispatched")
        assert (offer["outcome"], window["offer_accounting"][expected]) == (expected, 1)
        assert offer["scheduler_lag_ms"] == pytest.approx(delay*1000)
        assert len(dispatches) == len(state.records) == state.attempted == admitted
        assert state.budget_used == 1 and state.slots.qsize() == state.max_connections
        assert [event["event"] for event in events] == ["request_started", "request_completed"] * admitted
        if admitted:
            record, = state.records
            assert record.dispatch_ms == record.timing_start_ms == pytest.approx(dispatches[0])
            assert record.status == status
            response_elapsed = min(response_delay, .03, global_limit-delay)
            assert record.completed_ms == pytest.approx(dispatches[0]+response_elapsed*1000)
            assert offer["request_id"] == record.id
        else:
            assert offer["request_id"] is None

    run_virtual(monkeypatch, run())


def test_scheduled_admission_checks_lazy_http_lane_preparation(monkeypatch):
    import httpx

    from giraffe.client import LLMClient
    from giraffe.transport import HTTPClientPool

    async def run():
        cfg = settings(levels=[2], scenarios=["arrivals"], duration_seconds=.5,
                       profile=PROFILE, arrivals={"requests_per_second": 1})
        state = runner._Run(cfg, None, None)
        requests = []
        lanes = 0

        async def respond(request):
            requests.append(request)
            await asyncio.sleep(.02)
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"1 2"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

        def factory():
            nonlocal lanes
            lanes += 1
            if lanes == 2:
                asyncio.get_running_loop().now += .2
            return httpx.AsyncClient(transport=httpx.MockTransport(respond))

        client = LLMClient(cfg.targets[0], cfg)
        pool = client._client = HTTPClientPool(factory, max_connections=2)
        try:
            await execute(state, cfg.targets[0], client)
        finally:
            await pool.aclose()
        window, = state.observations["service"]["multi_user"]["windows"]
        assert lanes == 2
        assert len(requests) == state.attempted == len(state.records) == 1
        assert [offer["outcome"] for offer in window["offers"]] == ["dispatched", "dropped_late"]
        assert state.budget_used == 2 and state.slots.qsize() == state.max_connections

    run_virtual(monkeypatch, run())
