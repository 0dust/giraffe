"""Bounded persistent users on the runner's shared request and budget boundary."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from copy import deepcopy

from giraffe.concurrent_load import OutputProgress, calibrate
from giraffe.load_test import _shape_sources, build_spec
from giraffe.models import CheckResult, LoadTestSettings


WINDOW_POLICY = "completed-output-v1"


class _UserOutputProgress(OutputProgress):
    first_token = False

    def __init__(self, changed, expire_trigger_admissions):
        super().__init__(changed)
        self.expire_trigger_admissions = expire_trigger_admissions

    def on_first_token(self):
        self.first_token = True
        self.expire_trigger_admissions()
        self.changed.set()

    def set(self):
        super().set()
        self.expire_trigger_admissions()


def _matched_work_sha256(spec, *, nonce=None):
    messages = spec.messages
    if nonce and messages:
        first = messages[0]
        content = first.get("content")
        prefix = f"Request {nonce}."
        # Remove only the generated cache-busting prefix from the identity.
        if isinstance(content, str) and content.startswith(prefix):
            messages = [{**first, "content": content[len(prefix):]}, *messages[1:]]
    return hashlib.sha256(json.dumps({"messages": messages, "options": spec.options,
        "max_tokens": spec.max_tokens, "requested_input_tokens": spec.requested_input_tokens,
        "scorer": spec.scorer, "expected": spec.expected}, sort_keys=True).encode()).hexdigest()


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
                     sources_by_profile=None, prepared_inputs=None, sequences=None):
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
    changed = asyncio.Event()
    active = {}
    trigger_waiters = {}
    sequences = sequences if sequences is not None else {}

    def trigger_peers():
        return [uid for uid, progress in active.items() if uid not in window.get("heavy_user_ids", [])
                and (not (progress.first_token or progress.first)
                     if window.get("trigger") == "before_output" else progress.chunks >= 2)]

    def cancellation_peers():
        return [uid for uid, progress in active.items() if progress.first]

    def expire_trigger_admissions():
        if not trigger_waiters or closed or time.monotonic() >= cutoff:
            return
        peers = trigger_peers() if window["role"] == "probe" else cancellation_peers()
        if not peers:
            for user in trigger_waiters.values():
                user["pending_ready_ms"] = None

    async def wait_for_trigger(eligible_peers):
        while not closed and time.monotonic() < cutoff:
            changed.clear()
            if eligible_peers():
                return True
            try:
                await asyncio.wait_for(changed.wait(), max(.0001, cutoff - time.monotonic()))
            except TimeoutError:
                break
        return False

    def close(reason=None):
        nonlocal closed
        if closed:
            return
        closed = True
        admission_closed.set()
        changed.set()
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

        async def send(spec, finalize=None, *, nonce=None):
            nonlocal sequence
            if closed or time.monotonic() >= cutoff:
                return None
            sequence += 1
            spec.check_ids = ["multi_user"]
            spec.load_point = None
            spec.workload.update(user_id=user["user_id"], population=window["population"],
                profile=user["profile"], scenario=window["scenario"], phase=window["phase"],
                window_id=scenario, request_sequence=sequence,
                think_time_seconds=preceding_pause)
            if window.get("role"):
                spec.workload.update(role=window["role"], matched_work_sequence=sequence,
                    trigger=window.get("trigger"), heavy_profile=window.get("heavy_profile"),
                    matched_work_sha256=_matched_work_sha256(spec, nonce=nonce))
                sequences[user["user_id"]] = sequences.get(user["user_id"], 0) + 1
                spec.workload["request_sequence"] = sequences[user["user_id"]]
            selected = window.get("selected_user_id") == user["user_id"] and window["phase"] == "measurement"
            cancel_first = selected and sequence == 1
            if selected:
                spec.workload["cancellation_role"] = "selected" if cancel_first else "followup"
                if cancel_first:
                    spec.cancel_after_chunks = settings.cancellation.cancel_after_chunks
            progress = _UserOutputProgress(changed, expire_trigger_admissions)
            heavy = window.get("role") == "probe" and user["user_id"] in window.get("heavy_user_ids", [])
            eligible_peers = cancellation_peers if cancel_first else trigger_peers if heavy else None
            if settings.conversations:
                spec.scenario = scenario
                spec.workload["conversation_sequence"] = conversation_sequence

            def dispatched():
                user["dispatched_count"] += 1
                user["pending_ready_ms"] = None
                trigger_waiters.pop(user["user_id"], None)
                active[user["user_id"]] = progress
                if heavy:
                    spec.workload.update(trigger=window["trigger"], trigger_ms=_relative(run),
                                         trigger_peer_ids=peers)
                changed.set()

            def finalized(record):
                if finalize:
                    finalize(record)
                user["request_ids"].append(record.id)

            while not closed and time.monotonic() < cutoff:
                if eligible_peers and not await wait_for_trigger(eligible_peers):
                    return None
                if closed or time.monotonic() >= cutoff:
                    return None
                # Scenario coordination precedes eligibility for shared admission.
                ready = _relative(run)
                user["ready_count"] += 1
                user["pending_ready_ms"] = ready
                spec.workload["ready_ms"] = ready
                if eligible_peers:
                    trigger_waiters[user["user_id"]] = user
                try:
                    async with run.slot(False, cutoff) as admitted:
                        if not admitted or closed or time.monotonic() >= cutoff:
                            return None
                        if not run.allowed(target.name):
                            close(run.stop_reason(target.name) or "target request allowance exhausted")
                            return None
                        peers = eligible_peers() if eligible_peers else []
                        if eligible_peers and (not peers or user["pending_ready_ms"] is None):
                            # An expired trigger cannot carry admission wait into a new opportunity.
                            user["pending_ready_ms"] = None
                            continue
                        try:
                            return await run.request(target, client, spec, slot_reserved=True, admission_deadline=cutoff,
                                                     event=progress, on_start=dispatched, finalize_record=finalized)
                        finally:
                            active.pop(user["user_id"], None)
                            expire_trigger_admissions()
                            changed.set()
                finally:
                    trigger_waiters.pop(user["user_id"], None)

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
                if item["completed_turns"] < item["planned_turns"] and not closed and time.monotonic() < cutoff:
                    item["stopped_before_cutoff"] = True
                if not item["turns"]:
                    user["stop_reason"] = item["stop_reason"]
                    break
                pause_sequence = conversation_sequence
            else:
                point = {"id": scenario, "type": "combinations", "workload": "combinations",
                         "pattern": "concurrent", "level": window["population"], "rate": None,
                         "bursts": False, "source": "primary", "_source_index": sequence}
                nonce = uuid.uuid4().hex if window.get("role") else None
                spec = build_spec(run, load_settings, point, user["profile"], calibration,
                                  frozen_sources=(sources_by_profile or {}).get(user["profile"]),
                                  nonce=nonce)
                record = await send(spec, nonce=nonce)
                if record is None and time.monotonic() >= cutoff:
                    break
                pause_sequence = sequence
            preceding_pause = random.Random(f"{settings.seed}:pause:{user['user_id']}:{pause_sequence}").choices(
                settings.think_time_seconds.values, weights=settings.think_time_seconds.weights)[0]
            if not await pause(preceding_pause):
                break
        if user["pending_ready_ms"] is not None:
            user["pending_observed_ms"] = min(_relative(run), window["end_ms"])
        user["stop_reason"] = window["reason"] or user.get("stop_reason")

    order = list(window["users"])
    launch_seed = f"{settings.seed}:launch:{window['population']}:{window['phase']}"
    if window.get("role"):
        # Adding heavy users must not reshuffle the matched short-user order.
        order.sort(key=lambda user: random.Random(f"{launch_seed}:{user['user_id']}").random())
    else:
        random.Random(launch_seed).shuffle(order)
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
        if "repeated_requests" in settings.scenarios:
            if settings.warmup_seconds:
                windows.append(_window(level, "warmup", settings.warmup_seconds, population))
            windows.append(_window(level, "measurement", settings.duration_seconds, population))
        if "noisy_neighbors" in settings.scenarios:
            neighbors = settings.noisy_neighbors
            heavy_ids = neighbors.heavy_user_ids or [user["user_id"] for user in population[:neighbors.heavy_user_count]]
            for profile in neighbors.heavy_profiles:
                for trigger in neighbors.triggers:
                    control_id = f"multi_user_{level}_neighbors_{profile}_{trigger}_control"
                    for role in ("control", "probe"):
                        window = _window(level, "measurement", settings.duration_seconds, population)
                        window.update(id=f"multi_user_{level}_neighbors_{profile}_{trigger}_{role}",
                                      scenario="noisy_neighbors", role=role, trigger=trigger,
                                      heavy_profile=profile, heavy_user_ids=heavy_ids, control_window_id=control_id)
                        window["users"] = [dict(user, profile=profile if user["user_id"] in heavy_ids else "short_short")
                                           for user in window["users"] if role == "probe" or user["user_id"] not in heavy_ids]
                        windows.append(window)
        if "cancellation" in settings.scenarios:
            window = _window(level, "measurement", settings.duration_seconds, population)
            window.update(id=f"multi_user_{level}_cancellation", scenario="cancellation", role="cancellation",
                          selected_user_id=settings.cancellation.user_id)
            for user in window["users"]:
                user["profile"] = "short_long"
            windows.append(window)
    if settings.conversations:
        for window in windows:
            window["scenario"] = "repeated_conversations"
    if settings.warmup_seconds:
        expanded = []
        for window in windows:
            if window.get("role"):
                warmup = deepcopy(window)
                warmup.update(id=window["id"] + "_warmup", phase="warmup",
                              planned_duration_seconds=settings.warmup_seconds)
                expanded.append(warmup)
            expanded.append(window)
        windows = expanded
    return {"version": 1, "window_policy": WINDOW_POLICY,
            "settings": settings.model_dump(mode="json"), "population": population,
            "windows": windows, "calibrations": []}


async def execute(run, target, client):
    settings = run.config.multi_user
    observation = _planned_observation(settings)
    windows = observation["windows"]
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
    profiles = {user["profile"] for window in windows for user in window["users"]}
    sources_by_profile = {profile: tuple(_shape_sources(load_settings, profile.split("_")[0], sources))
                          for profile in profiles} if sources and not settings.conversations else {}
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
                assigned_tasks = sorted({user["conversation_workload"] for window in windows for user in window["users"]})
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
            for shape in sorted(profiles):
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
        sequences = {}
        for window in windows:
            if reason or not run.allowed(target.name):
                reason = reason or run.stop_reason(target.name) or "target request allowance exhausted"
                window["reason"] = reason
                for user in window["users"]:
                    user["stop_reason"] = reason
                run.mark_unfinished(target.name, ["multi_user"])
                continue
            await run_window(run, target, client, settings, load_settings, calibration, window,
                             sources_by_profile=sources_by_profile, prepared_inputs=prepared_inputs, sequences=sequences)
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
        messages = []
        if summary["failed_user_windows"]:
            worst = summary["worst_user"]
            messages.append(f"{summary['failed_user_windows']} user measurements failed; "
                            f"{worst['user_id']} at {worst['population']} users: " + " ".join(worst["reasons"]))
        fairness_failures = [window["fairness"] for window in summary["windows"]
                             if not window.get("role") and window.get("fairness", {}).get("status") == "fail"]
        if fairness_failures:
            messages.append("Observed delivery fairness failed. " + " ".join(
                reason for evidence in fairness_failures for reason in evidence["reasons"]))
        interference_failures = [user for window in summary["windows"]
                                 for user in window.get("interference", {}).get("users", []) if user["status"] == "fail"]
        if interference_failures:
            messages.append("Observed noisy-neighbor interference failed. " + " ".join(
                f"{user['user_id']}: {' '.join(user['reasons'])}" for user in interference_failures))
        for window in summary["windows"]:
            followup = window.get("cancellation", {}).get("followup", {})
            if followup.get("status") == "fail":
                messages.append("Same-user cancellation follow-up failed for "
                                + window["cancellation"]["selected_user_id"] + ".")
        message = " ".join(messages)
    elif status == "pass":
        message = "Every measured user met the selected criteria over the common windows."
    else:
        message = "Per-user acceptance is inconclusive because required evidence is incomplete."
    return CheckResult(id="multi_user", target=target.name, title="Multi-user",
                       status="inconclusive" if status == "measured" else status,
                       summary=message, metrics={"multi_user": summary},
                       evidence_ids=[r.id for r in records])
