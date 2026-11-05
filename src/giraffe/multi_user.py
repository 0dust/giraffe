"""Bounded persistent users on the runner's shared request and budget boundary."""
from __future__ import annotations

import asyncio
import random
import time

from giraffe.concurrent_load import calibrate
from giraffe.load_test import build_spec
from giraffe.models import CheckResult, LoadTestSettings


WINDOW_POLICY = "completed-output-v1"


def population_plan(settings):
    """A user's assignment never depends on completion order or smaller levels."""
    return [{"user_id": f"user-{index + 1:02d}",
             "profile": random.Random(f"{settings.seed}:profile:{index}").choice(settings.shapes)}
            for index in range(max(settings.levels))]


def _window(level, phase, seconds, population):
    return {"id": f"multi_user_{level}_{phase}", "scenario": "repeated_requests",
            "population": level, "phase": phase, "planned_duration_seconds": seconds,
            "start_ms": None, "end_ms": None, "planned_end_ms": None,
            "duration_seconds": 0, "drain_seconds": 0, "drain_reason": None, "cut_short": True,
            "reason": "not started", "users": [
                {**user, "activated_ms": None, "ready_count": 0, "dispatched_count": 0,
                 "request_ids": [], "pending_ready_ms": None, "pending_observed_ms": None,
                 "stop_reason": "not started"} for user in population[:level]]}


def _relative(run, now=None):
    return ((time.monotonic() if now is None else now) - run.start) * 1000


async def run_window(run, target, client, settings, load_settings, calibration, window):
    """Close admission at the common cutoff; admitted work keeps its own timeout."""
    start = time.monotonic()
    intended_end = start + window["planned_duration_seconds"]
    cutoff = min(intended_end, run.deadline)
    window.update(start_ms=_relative(run, start), planned_end_ms=_relative(run, intended_end),
                  end_ms=_relative(run, cutoff), cut_short=cutoff < intended_end, reason=None)
    scenario = window["id"]
    run.start_condition(target.name, scenario, {"population": window["population"],
                        "phase": window["phase"]}, planned=None, planned_kind="duration")
    closed = False

    def close(reason=None):
        nonlocal closed
        if closed:
            return
        closed = True
        end = min(time.monotonic(), cutoff)
        window.update(end_ms=_relative(run, end), duration_seconds=max(0, end - start),
                      cut_short=end < intended_end, reason=reason)
        for user in window["users"]:
            if user["pending_ready_ms"] is not None:
                user["pending_observed_ms"] = window["end_ms"]

    async def user_loop(user):
        user.update(activated_ms=_relative(run), stop_reason=None)
        sequence = 0
        while not closed and time.monotonic() < cutoff:
            if not run.allowed(target.name):
                close(run.stop_reason(target.name) or "target request allowance exhausted")
                break
            sequence += 1
            ready = time.monotonic()
            user["ready_count"] += 1
            user["pending_ready_ms"] = _relative(run, ready)
            point = {"id": scenario, "type": "combinations", "workload": "combinations",
                     "pattern": "concurrent", "level": window["population"], "rate": None,
                     "bursts": False, "source": "primary", "_source_index": sequence - 1}
            spec = build_spec(run, load_settings, point, user["profile"], calibration)
            spec.check_ids = ["multi_user"]
            spec.load_point = None
            spec.workload.update(user_id=user["user_id"], population=window["population"],
                                 profile=user["profile"], scenario="repeated_requests",
                                 phase=window["phase"], window_id=scenario,
                                 request_sequence=sequence, ready_ms=_relative(run, ready))
            async with run.slot(False, cutoff) as admitted:
                if not admitted or closed or time.monotonic() >= cutoff:
                    break
                if not run.allowed(target.name):
                    close(run.stop_reason(target.name) or "target request allowance exhausted")
                    break

                def dispatched():
                    user["dispatched_count"] += 1
                    user["pending_ready_ms"] = None

                def finalized(record):
                    user["request_ids"].append(record.id)

                await run.request(target, client, spec, slot_reserved=True,
                                  on_start=dispatched, finalize_record=finalized)
            if closed or time.monotonic() >= cutoff:
                break
            pause = random.Random(f"{settings.seed}:pause:{user['user_id']}:{sequence}").choices(
                settings.think_time_seconds.values, weights=settings.think_time_seconds.weights)[0]
            # Even a zero-pause, immediately completing transport yields to peers.
            await asyncio.sleep(min(pause, max(0, cutoff - time.monotonic())))
        if user["pending_ready_ms"] is not None:
            user["pending_observed_ms"] = min(_relative(run), window["end_ms"])
        user["stop_reason"] = window["reason"]

    order = list(window["users"])
    random.Random(f"{settings.seed}:launch:{window['population']}:{window['phase']}").shuffle(order)
    tasks = [asyncio.create_task(user_loop(user)) for user in order]
    try:
        pending = set(tasks)
        while pending and not closed:
            reason = run.stop_reason(target.name)
            if reason or not run.allowed(target.name):
                close(reason or run.stop_reason(target.name) or "target request allowance exhausted")
                break
            if time.monotonic() >= cutoff:
                close("maximum run duration reached" if cutoff < intended_end else None)
                break
            done, pending = await asyncio.wait(pending, timeout=min(.025, cutoff-time.monotonic()),
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        close(run.stop_reason(target.name))
        # A user Stop/error/duration ends in-flight work. A full request budget
        # closes admission only: requests already admitted still finish normally.
        reason = run.stop_reason(target.name)
        if reason and reason != "maximum request budget reached":
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks)
    except BaseException:
        if closed:
            window["drain_reason"] = run.stop_reason(target.name) or "execution cancelled"
        close(run.stop_reason(target.name) or "execution cancelled")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        window["drain_seconds"] = max(0, (_relative(run) - window["end_ms"]) / 1000)
        for user in window["users"]:
            if user["pending_ready_ms"] is not None:
                user["pending_observed_ms"] = window["end_ms"]
            user["stop_reason"] = window["reason"]
        if window["cut_short"]:
            run.mark_unfinished(target.name, ["multi_user"])
        run.finish_condition(target.name, scenario,
                             {"population": window["population"], "phase": window["phase"]},
                             reason=window["reason"])


async def execute(run, target, client):
    settings = run.config.multi_user
    population = population_plan(settings)
    windows = []
    for level in settings.levels:
        if settings.warmup_seconds:
            windows.append(_window(level, "warmup", settings.warmup_seconds, population))
        windows.append(_window(level, "measurement", settings.duration_seconds, population))
    observation = {"version": 1, "window_policy": WINDOW_POLICY,
                   "settings": settings.model_dump(mode="json"), "population": population,
                   "windows": windows, "calibrations": []}
    run.observations[target.name]["multi_user"] = observation
    for window in windows:
        run.plan_condition(target.name, window["id"],
                           {"population": window["population"], "phase": window["phase"]},
                           planned=None, planned_kind="duration")
    load_settings = LoadTestSettings(profile=settings.profile,
                                     request_timeout_seconds=run.config.request_timeout_seconds)
    if run.config.custom_fixtures:
        from giraffe.fixtures import load_custom_fixtures
        run.load_custom_specs = load_custom_fixtures(run.config.custom_fixtures,
            legacy_max_output_tokens=run.config.legacy_custom_fixture_max_output_tokens)
    calibration = {}
    reason = None
    try:
        if not (run.pack_specs or getattr(run, "load_custom_specs", [])):
            for shape in sorted({user["profile"] for user in population}):
                if not run.allowed(target.name):
                    reason = run.stop_reason(target.name) or "target request allowance exhausted"
                    break
                size, output = shape.split("_")
                key = size, "short_answer" if output == "short" else "long_output"
                if key not in calibration:
                    calibration[key] = await calibrate(run, target, client, load_settings, *key)
                    observation["calibrations"].append(calibration[key])
                if calibration[key]["status"] not in {"calibrated"}:
                    reason = calibration[key].get("reason") or "input preparation unavailable"
                    break
        for window in windows:
            if reason or not run.allowed(target.name):
                reason = reason or run.stop_reason(target.name) or "target request allowance exhausted"
                window["reason"] = reason
                for user in window["users"]:
                    user["stop_reason"] = reason
                run.mark_unfinished(target.name, ["multi_user"])
                continue
            await run_window(run, target, client, settings, load_settings, calibration, window)
    finally:
        # Preserve pending waiting intervals with the run's existing lag evidence.
        from giraffe.overlap import scheduling_delay_windows
        observation["scheduling_delay_windows"] = [
            {"start_ms": start, "end_ms": end}
            for start, end in scheduling_delay_windows(run).windows]


def result(run, target):
    observation = run.observations[target.name].get("multi_user", {})
    return CheckResult(id="multi_user", target=target.name, title="Multi-user", status="inconclusive",
                       summary="Per-user measurement windows recorded.", metrics={"multi_user": observation})
