"""Cancelled requests remain terminal evidence in both load schedulers."""
import asyncio
import time

import pytest

from giraffe import load_test, traffic
from giraffe.models import Limits, RequestRecord, RequestSpec, TrafficConfig
from giraffe.runner import _Run
from tests.test_concurrent_load import configuration


class PartialClient:
    def __init__(self):
        self.started = asyncio.Event()
        self.calls = 0

    async def execute(self, spec, first_output_event=None):
        self.calls += 1
        begin = time.monotonic()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Like LLMClient, retain partial output when the transport is stopped.
            return RequestRecord(id="partial", target="local", fixture_id=spec.fixture_id,
                scenario=spec.scenario, check_ids=spec.check_ids, started_at="now",
                status="cancelled", elapsed_ms=(time.monotonic() - begin) * 1000,
                first_output_ms=1, output="K7", stream=True)


@pytest.mark.parametrize("cause", ["deadline", "user_stop"])
async def test_stopped_window_keeps_cancelled_request_and_measurements(cause):
    config = configuration()
    run, client = _Run(config, None, None), PartialClient()
    settings, target = config.load_test, config.targets[0]
    point = load_test.test_points(settings)[0]
    calibration = {("short", "short_answer"): {"filler_chars": 0}}
    task = asyncio.create_task(load_test.concurrent_window(run, target, client, settings, point, calibration))
    await asyncio.wait_for(client.started.wait(), .5)
    # Change the run deadline after request() established its longer timeout.
    # The window must cancel the in-flight task and consume its attached record.
    if cause == "deadline":
        run.deadline = time.monotonic() + .02
    else:
        run.stop.set()
    rows, window = await asyncio.wait_for(task, .5)
    assert rows == run.records
    assert len(rows) == client.calls == 1
    record = rows[0]
    assert record.status == "cancelled"
    assert window["request_ids"] == [record.id]
    assert window["window_reason"] == ("overall run deadline reached" if cause == "deadline" else "cancelled by user")
    assert record.load_point["cancellation_reason"] == record.error
    assert record.termination_reason == ("run_duration_limit" if cause == "deadline" else "user_stop")
    assert record.error == ("maximum run duration reached" if cause == "deadline" else "cancelled by user")
    measured = load_test.assess_point(run, target, settings, point, rows)
    assert measured["dispatched"] == measured["cancelled"] == 1
    assert measured["groups"][0]["cancelled"] == 1
    assert measured["groups"][0]["request_ids"] == [record.id]
    assert measured["status"] == "inconclusive"


@pytest.mark.parametrize("cause", ["deadline", "user_stop"])
async def test_arrival_cancelled_record_keeps_stage_metadata_without_double_counting(cause):
    config = configuration()
    run, client = _Run(config, None, None), PartialClient()
    target = config.targets[0]
    spec = RequestSpec(fixture_id="arrival", scenario="arrival", check_ids=["load"], messages=[])

    async def execute(value):
        return await run.request(target, client, value, slot_reserved=True, budget_reserved=True)

    task = asyncio.create_task(traffic.run_stage(
        traffic=TrafficConfig(rates=[20], duration_seconds=.2, max_in_flight=1),
        rate_rps=20, stage_index=3, fixtures={"short": [spec]},
        schedule_override=[(0, "short"), (.15, "short")], execute=execute,
        reserve_offer=lambda: run.reserve_offer(target.name),
        try_acquire=run.try_acquire_slot, release=run.release_slot,
        stop_reason=lambda: run.stop_reason(target.name), deadline=run.deadline,
        limits=Limits(), run_start=run.start, check_id="load", return_evidence=True))
    await asyncio.wait_for(client.started.wait(), .5)
    if cause == "deadline":
        run.deadline = time.monotonic() + .02
    else:
        run.stop.set()
    evidence = await asyncio.wait_for(task, .5)
    assert len(evidence.records) == len(run.records) == client.calls == 1
    index, record = evidence.records[0]
    assert index == 0 and record is run.records[0]
    assert record.status == "cancelled"
    assert record.traffic_stage == 3
    assert record.traffic_rate_rps == 20
    assert record.traffic_class == "short"
    assert record.scheduled_offset_ms == 0
    assert record.scheduled_ms is not None
    assert record.dispatch_lag_ms is not None and record.dispatch_lag_ms >= 0
    assert record.arrival_first_output_ms == pytest.approx(record.dispatch_lag_ms + 1)
    assert record.arrival_elapsed_ms == pytest.approx(record.dispatch_lag_ms + record.elapsed_ms)
    assert evidence.outcomes == ("cancelled", "not_offered")
    result = traffic.finalize_stage(evidence, Limits())
    assert result["started"] == result["cancelled"] == 1
    assert result["windows"][0]["cancelled"] == 1


@pytest.mark.parametrize("returns_record", [False, True])
async def test_arrival_other_cancellation_callbacks_still_count_once(returns_record):
    spec = RequestSpec(fixture_id="cancelled", scenario="arrival", check_ids=["capacity"], messages=[])

    async def execute(value):
        if not returns_record:
            raise asyncio.CancelledError
        return RequestRecord(id="returned", target="local", fixture_id=value.fixture_id,
            scenario=value.scenario, check_ids=value.check_ids, started_at="now", status="cancelled")

    result = await traffic.run_stage(
        traffic=TrafficConfig(rates=[10], duration_seconds=.01, max_in_flight=1), rate_rps=10,
        stage_index=0, fixtures={"short": [spec]}, schedule_override=[(0, "short")],
        execute=execute, reserve_offer=lambda: True, try_acquire=lambda: True,
        release=lambda: None, stop_reason=lambda: None, deadline=time.monotonic() + 1,
        limits=Limits())
    assert result["started"] == result["cancelled"] == 1
    assert result["windows"][0]["started"] == result["windows"][0]["cancelled"] == 1
