"""Match Load and generated Multi-user guards at their dispatch boundaries."""

import time
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from giraffe import concurrent_load, load_test, multi_user
from giraffe.models import MultiUser
from tests.test_workload_versions import (
    NONCE, CaptureRun, _config, _load_guard_cases, _load_test, _multi_user,
    _multi_user_cases, _multi_user_plan, _multi_user_plan_settings, _multi_user_window_plan, _requests,
)


class DispatchCaptureRun(CaptureRun):
    def __init__(self, config, *, one_request=False):
        super().__init__(config)
        self.start = time.monotonic()
        self.deadline = float("inf")
        self.one_request = one_request
        self.budget_counts = {config.targets[0].name: 0}

    def plan_condition(self, target, scenario, *args, **kwargs):
        self.planned.append(scenario)

    def allowed(self, target):
        return not self.one_request or not self.requests

    def stop_reason(self, target):
        return None

    def condition_reason(self, target):
        return "capture complete"

    def mark_unfinished(self, target, checks):
        self.unfinished[target].update(checks)

    @asynccontextmanager
    async def slot(self, *args):
        yield True

    def reserve_offer(self, *args):
        return True

    def try_acquire_slot(self, *args):
        return True

    def release_slot(self, *args):
        pass

    def drop_offer(self, *args):
        raise AssertionError("The dispatch capture must not drop an offer")

    async def request(self, target, client, spec, *, on_start=None, finalize_record=None, **kwargs):
        record = await super().request(target, client, spec, on_start=on_start)
        if finalize_record:
            finalize_record(record)
        return record


async def test_load_guard_matches_dispatch_schedule_and_requests(monkeypatch):
    # Replace the scheduler's wait/transport boundary, preserving arrival_window's
    # final schedule_override, spec_factory and execute callbacks.
    monkeypatch.setattr(concurrent_load.uuid, "uuid4", lambda: SimpleNamespace(hex=NONCE))
    settings, sources, points = _load_guard_cases()
    guard = _load_test()
    assert settings.migrated_tests
    request_index = 0
    for point_index, original in enumerate(points):
        point = deepcopy(original)
        config = _config(checks=["load"])
        run = DispatchCaptureRun(config, one_request=point["pattern"] == "concurrent")
        source = sources[point["source"]]
        calibration = {(size, variant): {"filler_chars": 400}
                       for size in ("short", "medium", "long")
                       for variant in ("short_answer", "long_output")}
        expected_by_shape = {}
        for shape in load_test.point_shapes(source, point):
            expected_by_shape[shape] = guard["requests"][request_index][0]
            request_index += 1
        offers, overflow = guard["arrivals"][point_index]["schedule"]

        async def capture_stage(**kwargs):
            assert kwargs["schedule_override"] == [
                (offer["offset_seconds"], offer["shape"]) for offer in offers
            ]
            for index, offer in enumerate(offers):
                spec = kwargs["spec_factory"](index)
                await kwargs["execute"](spec, None)
                assert _requests([run.requests[-1]]) == [expected_by_shape[offer["shape"]]]
            return SimpleNamespace(started_monotonic=run.start, outcomes=["started"] * len(offers),
                                   offer_lags_ms=[0] * len(offers), records=[], reasons=[],
                                   interrupted=False, sending_seconds=0, observation_seconds=0)

        monkeypatch.setattr(load_test, "run_stage", capture_stage)
        if point["pattern"] == "concurrent":
            # Concurrent points bypass planned_arrivals and refill slots. Capture
            # their first admitted request without spending the duration window.
            assert offers == [] and not overflow
            await load_test.concurrent_window(run, config.targets[0], None, source, point, calibration)
            assert _requests(run.requests) == [expected_by_shape[point["shape"]]]
        else:
            await load_test.arrival_window(run, config.targets[0], None, source, point, calibration)
            assert point["schedule_overflow"] == overflow
            assert len(run.requests) == len(offers)
    assert request_index == len(guard["requests"])


async def test_multi_user_guard_matches_sent_requests_for_every_shape_and_level(monkeypatch):
    monkeypatch.setattr(concurrent_load.uuid, "uuid4", lambda: SimpleNamespace(hex=NONCE))
    captured = []
    for config, load_settings, point, shape, calibration in _multi_user_cases():
        settings = MultiUser()
        run = DispatchCaptureRun(config, one_request=True)
        user = {"user_id": "user-01", "profile": shape, "conversation_workload": None}
        # Population remains the guarded default level; a single captured user
        # suffices because user IDs and timing metadata never reach the server.
        window = multi_user._window(point["level"], "measurement", 60, [user])
        await multi_user.run_window(run, config.targets[0], None, settings, load_settings, calibration, window)
        assert len(run.requests) == 1
        captured.extend(run.requests)
    assert _requests(captured) == _multi_user()["requests"]


@pytest.mark.parametrize("settings", _multi_user_plan_settings(),
                         ids=["default", "all-scenarios", "explicit-heavy-users", "recall-conversations",
                              "performance-conversations"])
async def test_multi_user_guard_matches_executed_windows_and_offer_schedule(monkeypatch, settings):
    expected = _multi_user_plan(settings)
    captured_windows, dispatched_schedules = [], []
    config = _config(checks=["multi_user"], multi_user=settings)
    run = DispatchCaptureRun(config)
    run.pack_specs = []
    run.peak = {}
    run.slots = SimpleNamespace(maxsize=32)
    arrival_executor = multi_user.run_arrival_window

    async def calibrated(*args):
        return {"status": "calibrated", "filler_chars": 400}

    async def capture_window(run, target, client, settings, load_settings, calibration, window, **kwargs):
        captured_windows.append(deepcopy(_multi_user_window_plan(window)))
        if "offers" in window:
            await arrival_executor(run, target, client, settings, load_settings, calibration, window, **kwargs)

    async def capture_stage(**kwargs):
        # Real run_arrival_window has prepared these final scheduler inputs.
        dispatched_schedules.append((kwargs["scenario"], kwargs["traffic"].duration_seconds,
                                     kwargs["schedule_override"]))
        duration = kwargs["traffic"].duration_seconds
        kwargs["schedule_observations"].extend([
            {"start_ms": 0, "end_ms": duration * 1000},
            {"start_ms": duration * 1000, "end_ms": duration * 1000},
        ])
        count = len(kwargs["schedule_override"])
        return SimpleNamespace(started_monotonic=run.start, outcomes=["not_offered"] * count,
                               offer_lags_ms=[0] * count, records=[], reasons=[], interrupted=False,
                               sending_seconds=duration, observation_seconds=duration,
                               drain_expired=False, stalls=[])

    monkeypatch.setattr(multi_user, "calibrate", calibrated)
    monkeypatch.setattr(multi_user, "run_window", capture_window)
    monkeypatch.setattr(multi_user, "run_arrival_window", capture_window)
    monkeypatch.setattr(multi_user, "run_stage", capture_stage)
    await multi_user.execute(run, config.targets[0], None)
    assert run.observations[config.targets[0].name]["multi_user"]["population"] == expected["population"]
    assert captured_windows == expected["windows"]
    assert dispatched_schedules == [
        (window["id"], window["planned_duration_seconds"],
         [(offer["offset_seconds"], offer["profile"]) for offer in window["offers"]])
        for window in expected["windows"] if "offers" in window
    ]
