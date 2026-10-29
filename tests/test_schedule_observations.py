"""Scheduler intent is recorded evidence, independent of actual request delivery."""

import time

import pytest

from giraffe.models import Limits, RequestSpec, TrafficConfig
from giraffe.traffic import ScheduleObservation, run_stage


def test_schedule_intervals_use_run_origin_and_preserve_downshift_and_drain():
    rows = []
    first = ScheduleObservation(rows, start=12, origin=10, stage_id="c8",
                                check_id="load", kind="concurrency", concurrency=8)
    assert rows[0]["end_ms"] is None  # Available while the scheduler is live.
    first.begin_drain(14)
    first.finish(15)
    second = ScheduleObservation(rows, start=15, origin=10, stage_id="c4",
                                 check_id="load", kind="concurrency", concurrency=4)
    second.begin_drain(18)
    second.finish(19)
    assert [(r["start_ms"], r["end_ms"], r["intended_concurrency"]) for r in rows] == [
        (2000, 4000, 8), (4000, 5000, 0), (5000, 8000, 4), (8000, 9000, 0),
    ]
    assert rows[-1]["stage_id"] == "c4.drain"
    assert rows[-1]["kind"] == "drain"
    assert all(r["intended_rate_rps"] is None for r in rows)


@pytest.mark.parametrize("pattern,expected_rate", [("steady", 3), ("bursts", None)])
async def test_partial_traffic_stage_records_observed_bounds_not_configured_duration(pattern, expected_rate):
    rows = []
    start = time.monotonic() - 5
    fixture = RequestSpec(fixture_id="fixture", scenario="traffic", check_ids=["load"],
                          messages=[], scorer="none")

    async def never_execute(spec, finalize_record):
        raise AssertionError("Budget exhausted before dispatch")

    result = await run_stage(
        traffic=TrafficConfig(rates=[3], duration_seconds=1, max_in_flight=1),
        rate_rps=3, stage_index=2, fixtures={"short": [fixture]},
        schedule_override=[(0, "short")], execute=never_execute,
        reserve_offer=lambda: False, try_acquire=lambda: True, release=lambda: None,
        stop_reason=lambda: None, deadline=time.monotonic() + 2, limits=Limits(),
        run_start=start, schedule_observations=rows, schedule_stage_id="point2",
        schedule_pattern=pattern,
    )
    assert result["started"] == 0
    assert result["not_offered"] == 1
    sending, drain = rows
    assert sending["stage_id"] == "point2"
    assert sending["start_ms"] >= 5000
    assert 0 <= sending["end_ms"] - sending["start_ms"] < 100
    assert sending["intended_rate_rps"] == expected_rate
    assert drain["start_ms"] == sending["end_ms"]
    assert drain["end_ms"] >= drain["start_ms"]
    assert drain["intended_rate_rps"] == (0 if expected_rate is not None else None)
    assert all(r["intended_concurrency"] is None for r in rows)
