"""Bounded persistent users on the runner's shared request and budget boundary."""
from __future__ import annotations

import asyncio
import random
import time

from giraffe.concurrent_load import calibrate
from giraffe.load_test import _shape_sources, build_spec
from giraffe.models import CheckResult, LoadTestSettings


WINDOW_POLICY = "completed-output-v1"


def population_plan(settings):
    """A user's assignment never depends on completion order or smaller levels."""
    population = []
    values = settings.think_time_seconds.values
    weights = settings.think_time_seconds.weights
    variable = len({value for index, value in enumerate(values) if weights is None or weights[index]}) > 1
    tasks = (settings.conversations.workloads
             if settings.conversations and settings.conversations.profile == "performance" else [])
    task_weights = [task.weight for task in tasks]
    for index in range(max(settings.levels)):
        user_id = f"user-{index + 1:02d}"
        task = None
        if settings.conversations:
            task = (random.Random(f"{settings.seed}:conversation-task:{index}").choices(
                range(len(tasks)), weights=task_weights)[0] if tasks else None)
            profile = "conversation:" + (tasks[task].name if task is not None else "exact_repeat")
        else:
            profile = random.Random(f"{settings.seed}:profile:{index}").choice(settings.shapes)
        population.append({"user_id": user_id, "profile": profile,
            "conversation_workload": task,
            "think_time_plan": {"values": values, "weights": weights,
                "seed_key": f"{settings.seed}:pause:{user_id}" if variable else None}})
    return population


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


async def run_window(run, target, client, settings, load_settings, calibration, window, *,
                     sources_by_profile=None, prepared_inputs=None):
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
    admission_closed = asyncio.Event()

    def close(reason=None):
        nonlocal closed
        if closed:
            return
        closed = True
        admission_closed.set()
        end = min(time.monotonic(), cutoff)
        window.update(end_ms=_relative(run, end), duration_seconds=max(0, end - start),
                      cut_short=end < intended_end, reason=reason)
        for user in window["users"]:
            if user["pending_ready_ms"] is not None:
                user["pending_observed_ms"] = window["end_ms"]

    async def pause(seconds):
        if closed or time.monotonic() >= cutoff:
            return False
        if seconds == 0:
            await asyncio.sleep(0)
        else:
            try:
                async with asyncio.timeout(max(0, min(seconds, cutoff - time.monotonic()))):
                    await admission_closed.wait()
            except TimeoutError:
                pass
        return not closed and time.monotonic() < cutoff

    async def user_loop(user):
        user.update(activated_ms=_relative(run), stop_reason=None)
        sequence, conversation_sequence = 0, 0
        preceding_pause = 0

        async def send(spec, finalize=None):
            nonlocal sequence
            if closed or time.monotonic() >= cutoff:
                return None
            sequence += 1
            ready = _relative(run)
            user["ready_count"] += 1
            user["pending_ready_ms"] = ready
            spec.check_ids = ["multi_user"]
            spec.load_point = None
            spec.workload.update(user_id=user["user_id"], population=window["population"],
                profile=user["profile"], scenario=window["scenario"], phase=window["phase"],
                window_id=scenario, request_sequence=sequence, ready_ms=ready,
                think_time_seconds=preceding_pause)
            if settings.conversations:
                spec.scenario = scenario
                spec.workload["conversation_sequence"] = conversation_sequence
            async with run.slot(False, cutoff) as admitted:
                if not admitted or closed or time.monotonic() >= cutoff:
                    return None
                if not run.allowed(target.name):
                    close(run.stop_reason(target.name) or "target request allowance exhausted")
                    return None

                def dispatched():
                    user["dispatched_count"] += 1
                    user["pending_ready_ms"] = None

                def finalized(record):
                    if finalize:
                        finalize(record)
                    user["request_ids"].append(record.id)

                return await run.request(target, client, spec, slot_reserved=True, admission_deadline=cutoff,
                                         on_start=dispatched, finalize_record=finalized)

        while not closed and time.monotonic() < cutoff:
            if not run.allowed(target.name):
                close(run.stop_reason(target.name) or "target request allowance exhausted")
                break
            if settings.conversations:
                from giraffe.session_sampling import conversation_turns
                from giraffe.workloads import execute_conversation
                conversation_sequence += 1
                conversation = settings.conversations
                mode = conversation.modes[(conversation_sequence - 1) % len(conversation.modes)]
                task = user["conversation_workload"]
                workload = conversation.workloads[task] if task is not None else None
                seed = conversation.sampling.seed if conversation.sampling else settings.seed
                plan = {"workload": workload, "turns": conversation_turns(conversation, workload,
                    f"{seed}:{user['user_id']}:{conversation_sequence}:{mode}")}
                try:
                    item = await execute_conversation(run, target, client, run.config, conversation, mode,
                        (conversation_sequence - 1) * 32 + int(user["user_id"].split("-")[1]),
                        items=user.setdefault("conversations", []), prepared_inputs=prepared_inputs or {},
                        plan=plan, session_id=f"{target.name}:{scenario}:{user['user_id']}:{conversation_sequence}:{mode}",
                        check="multi_user", send=send, pause=pause)
                except asyncio.CancelledError:
                    user["conversations"][-1]["stop_reason"] = "conversation interrupted"
                    raise
                if not item["turns"]:
                    user["stop_reason"] = item["stop_reason"]
                    break
                pause_sequence = conversation_sequence
            else:
                point = {"id": scenario, "type": "combinations", "workload": "combinations",
                         "pattern": "concurrent", "level": window["population"], "rate": None,
                         "bursts": False, "source": "primary", "_source_index": sequence}
                spec = build_spec(run, load_settings, point, user["profile"], calibration,
                                  frozen_sources=(sources_by_profile or {}).get(user["profile"]))
                await send(spec)
                pause_sequence = sequence
            preceding_pause = random.Random(f"{settings.seed}:pause:{user['user_id']}:{pause_sequence}").choices(
                settings.think_time_seconds.values, weights=settings.think_time_seconds.weights)[0]
            if not await pause(preceding_pause):
                break
        if user["pending_ready_ms"] is not None:
            user["pending_observed_ms"] = min(_relative(run), window["end_ms"])
        user["stop_reason"] = window["reason"] or user.get("stop_reason")

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
            for item in user.get("conversations", []):
                item["completed_turns"] = len(item["turns"])
                if item["completed_turns"] < item["planned_turns"] and not item["stop_reason"]:
                    item["stop_reason"] = window["reason"] or window["drain_reason"] or "conversation interrupted"
            if user["pending_ready_ms"] is not None:
                user["pending_observed_ms"] = window["end_ms"]
            user["stop_reason"] = window["reason"] or user.get("stop_reason")
        if window["cut_short"]:
            run.mark_unfinished(target.name, ["multi_user"])
        run.finish_condition(target.name, scenario,
                             {"population": window["population"], "phase": window["phase"]},
                             reason=window["reason"])


def _planned_observation(settings):
    population = population_plan(settings)
    windows = []
    for level in settings.levels:
        if settings.warmup_seconds:
            windows.append(_window(level, "warmup", settings.warmup_seconds, population))
        windows.append(_window(level, "measurement", settings.duration_seconds, population))
    if settings.conversations:
        for window in windows:
            window["scenario"] = "repeated_conversations"
    return {"version": 1, "window_policy": WINDOW_POLICY,
            "settings": settings.model_dump(mode="json"), "population": population,
            "windows": windows, "calibrations": []}


async def execute(run, target, client):
    settings = run.config.multi_user
    observation = _planned_observation(settings)
    population, windows = observation["population"], observation["windows"]
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
    sources = run.pack_specs or getattr(run, "load_custom_specs", [])
    # Resolve candidates once from saved inputs, never from later peer results.
    sources_by_profile = {profile: tuple(_shape_sources(load_settings, profile.split("_")[0], sources))
                          for profile in {user["profile"] for user in population}} if sources and not settings.conversations else {}
    calibration = {}
    prepared_inputs = {}
    reason = None
    try:
        if settings.conversations:
            from giraffe.session_sampling import prepare_session_input
            conversation = settings.conversations
            distribution = conversation.sampling.input_tokens if conversation.sampling else None
            if distribution:
                tokens_to_prepare = [value for index, value in enumerate(distribution.values)
                                     if distribution.weights is None or distribution.weights[index] > 0]
                turn_counts = conversation.sampling.turn_count
                max_turns = max(value for index, value in enumerate(turn_counts.values)
                    if turn_counts.weights is None or turn_counts.weights[index] > 0) if turn_counts else None
                assigned_tasks = sorted({user["conversation_workload"] for user in population})
                for index in assigned_tasks:
                    task = conversation.workloads[index]
                    for prompt in task.turns[:int(max_turns) if max_turns is not None else None]:
                        for tokens in tokens_to_prepare:
                            if not run.allowed(target.name):
                                break
                            key = prompt, tokens
                            if key not in prepared_inputs:
                                prepared_inputs[key] = await prepare_session_input(run, target, prompt, tokens)
        elif not (run.pack_specs or getattr(run, "load_custom_specs", [])):
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
            await run_window(run, target, client, settings, load_settings, calibration, window,
                             sources_by_profile=sources_by_profile, prepared_inputs=prepared_inputs)
    finally:
        from giraffe.overlap import scheduling_delay_windows
        observation["scheduling_delay_windows"] = [
            {"start_ms": start, "end_ms": end}
            for start, end in scheduling_delay_windows(run).windows]


def result(run, target):
    from giraffe.multi_user_metrics import summarize
    from giraffe.overlap import scheduling_delay_windows

    observation = run.observations[target.name].get("multi_user")
    if observation is None:
        observation = _planned_observation(run.config.multi_user)
        reason = run.stop_reason(target.name) or "Target execution did not start."
        for window in observation["windows"]:
            window["reason"] = reason
            for user in window["users"]:
                user["stop_reason"] = reason
        run.observations[target.name]["multi_user"] = observation
    observation["scheduling_delay_windows"] = [
        {"start_ms": start, "end_ms": end}
        for start, end in scheduling_delay_windows(run).windows]
    records = [r for r in run.records if r.target == target.name and "multi_user" in r.check_ids]
    health = getattr(run, "timing_health", None)
    summary = summarize(observation, records, timing_health=health.snapshot() if health else None,
                        legacy_max_lag_ms=run.max_lag_ms)
    observation["summary"] = summary
    status = summary["status"]
    if status == "measured":
        message = "Per-user delivery measured; no acceptance criteria selected."
    elif status == "fail":
        worst = summary["worst_user"]
        fairness_failures = [window["fairness"] for window in summary["windows"]
                             if window.get("fairness", {}).get("status") == "fail"]
        message = (f"{summary['failed_user_windows']} user measurements failed; "
                   f"{worst['user_id']} at {worst['population']} users: "
                   + " ".join(worst["reasons"])) if summary["failed_user_windows"] else "Observed delivery fairness failed."
        if fairness_failures:
            message += " " + " ".join(reason for result in fairness_failures for reason in result["reasons"])
    elif status == "pass":
        message = "Every measured user met the selected criteria over the common windows."
    else:
        message = "Per-user acceptance is inconclusive because required evidence is incomplete."
    return CheckResult(id="multi_user", target=target.name, title="Multi-user",
                       status="inconclusive" if status == "measured" else status,
                       summary=message, metrics={"multi_user": summary},
                       evidence_ids=[r.id for r in records])
