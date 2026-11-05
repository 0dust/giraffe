"""Bounded persistent users on the runner's shared request and budget boundary."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
from copy import deepcopy

from giraffe.concurrent_load import OutputProgress, calibrate
from giraffe.load_test import _shape_sources, build_spec
from giraffe.models import CheckResult, LoadTestSettings, Limits, TrafficConfig
from giraffe.traffic import run_stage


WINDOW_POLICY = "completed-output-v1"


def _matched_work_sha256(spec):
    return hashlib.sha256(json.dumps({"messages": spec.messages, "options": spec.options,
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
    sequences = sequences if sequences is not None else {}

    def trigger_peers():
        return [uid for uid, progress in active.items() if uid not in window.get("heavy_user_ids", [])
                and (not progress.first if window.get("trigger") == "before_output" else progress.chunks >= 2)]

    async def wait_for_trigger():
        while not closed and time.monotonic() < cutoff:
            changed.clear()
            if trigger_peers():
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
            if window.get("role"):
                spec.workload.update(role=window["role"], matched_work_sequence=sequence,
                    trigger=window.get("trigger"), heavy_profile=window.get("heavy_profile"),
                    matched_work_sha256=_matched_work_sha256(spec))
                sequences[user["user_id"]] = sequences.get(user["user_id"], 0) + 1
                spec.workload["request_sequence"] = sequences[user["user_id"]]
            selected = window.get("selected_user_id") == user["user_id"] and window["phase"] == "measurement"
            if selected:
                spec.workload["cancellation_role"] = "selected" if sequence == 1 else "followup"
                if sequence == 1:
                    spec.cancel_after_chunks = settings.cancellation.cancel_after_chunks
            progress = OutputProgress(changed)
            if selected and sequence == 1:
                while not closed and time.monotonic() < cutoff:
                    changed.clear()
                    if any(peer.first for peer in active.values()):
                        break
                    try:
                        await asyncio.wait_for(changed.wait(), max(.0001, cutoff-time.monotonic()))
                    except TimeoutError:
                        return None
            heavy = window.get("role") == "probe" and user["user_id"] in window.get("heavy_user_ids", [])
            if heavy and not await wait_for_trigger():
                return None
            if settings.conversations:
                spec.scenario = scenario
                spec.workload["conversation_sequence"] = conversation_sequence
            async with run.slot(False, cutoff) as admitted:
                if not admitted or closed or time.monotonic() >= cutoff:
                    return None
                if not run.allowed(target.name):
                    close(run.stop_reason(target.name) or "target request allowance exhausted")
                    return None
                peers = trigger_peers() if heavy else []
                if heavy and not peers:
                    return None

                def dispatched():
                    user["dispatched_count"] += 1
                    user["pending_ready_ms"] = None
                    active[user["user_id"]] = progress
                    if heavy:
                        spec.workload.update(trigger=window["trigger"], trigger_ms=_relative(run),
                                             trigger_peer_ids=peers)
                    changed.set()

                def finalized(record):
                    if finalize:
                        finalize(record)
                    user["request_ids"].append(record.id)

                try:
                    return await run.request(target, client, spec, slot_reserved=True, admission_deadline=cutoff,
                                             event=progress, on_start=dispatched, finalize_record=finalized)
                finally:
                    active.pop(user["user_id"], None)
                    changed.set()

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
                                  frozen_sources=(sources_by_profile or {}).get(user["profile"]),
                                  nonce=hashlib.sha256(f"{settings.seed}:{window['population']}:{user['user_id']}:{sequence}".encode()).hexdigest()[:32]
                                  if window.get("role") else None)
                record = await send(spec)
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


def _steady_offer_count(duration, activation, rate):
    count = math.ceil((duration-activation)*rate)
    return count - int(count > 0 and activation+(count-1)/rate >= duration)


def _offer_plan(window, settings):
    offers = []
    recovery = window["scenario"] == "recovery"
    rate = window["requests_per_second"]
    for user in window["users"]:
        uid = user["user_id"]
        activation = 0 if recovery else settings.arrivals.activations.get(uid, 0)
        cohort = "shared" if recovery else "newcomer" if activation else "incumbent"
        user.update(cohort=cohort, activation_seconds=activation)
        for index in range(_steady_offer_count(window["planned_duration_seconds"], activation, rate)):
            offset = activation + index/rate
            if offset < window["planned_duration_seconds"]:
                offers.append({"user_id": uid, "profile": user["profile"], "cohort": cohort,
                               "offset_seconds": offset, "origin": "steady"})
    if not recovery:
        users = {u["user_id"]: u for u in window["users"]}
        for burst_index, burst in enumerate(settings.arrivals.bursts):
            for index in range(burst.requests):
                user = users[burst.user_id]
                offers.append({"user_id": burst.user_id, "profile": user["profile"], "cohort": user["cohort"],
                    "offset_seconds": burst.at_seconds + index*burst.spacing_seconds,
                    "origin": "burst", "burst_index": burst_index})
    priority = {user["user_id"]: random.Random(f"{settings.seed}:offer:{user['user_id']}").random()
                for user in window["users"]}
    offers.sort(key=lambda offer: (offer["offset_seconds"], priority[offer["user_id"]]))
    for index, offer in enumerate(offers):
        offer.update(index=index, outcome="not_offered", offered=False, dispatched=False, request_id=None)
    return offers


def _offer_counts(offers):
    return {"planned": len(offers), "offered": sum(o["offered"] for o in offers),
            **{name: sum(o["outcome"] == name for o in offers) for name in
               ("dispatched", "dropped_local", "dropped_late", "unissued", "not_offered")}}


async def run_arrival_window(run, target, client, settings, load_settings, calibration, window, *,
                             sources_by_profile, sequences):
    offers = window["offers"]
    users = {u["user_id"]: u for u in window["users"]}
    offered = 0
    work_counts = {}
    for offer in offers:
        uid = offer["user_id"]
        work_counts[uid] = work_counts.get(uid, 0) + 1
        sequences[uid] = sequences.get(uid, 0) + 1
        offer["request_sequence"] = sequences[uid]
        work_index = work_counts[uid] + window.get("work_sequence_start", 0)
        offer["work_sequence"] = work_index
        offer["matched_work_sequence"] = ((work_index-1) % window["control_work_count"] + 1
                                           if window.get("control_work_count") else work_index)

    def reserve():
        nonlocal offered
        if not run.reserve_offer(target.name):
            return False
        offer = offers[offered]
        offer["offered"] = True
        user = users[offer["user_id"]]
        user["ready_count"] += 1
        if user["activated_ms"] is None:
            user["activated_ms"] = _relative(run)
        offered += 1
        return True

    def build(index):
        offer = offers[index]
        point = {"id": window["id"], "type": "combinations", "workload": "combinations",
                 "pattern": "constant", "level": window["population"], "rate": window["requests_per_second"],
                 "bursts": offer["origin"] == "burst", "source": "primary",
                 "_source_index": offer["matched_work_sequence"]-1}
        nonce = hashlib.sha256(f"{settings.seed}:{window['population']}:{offer['user_id']}:{offer['matched_work_sequence']}".encode()).hexdigest()[:32]
        spec = build_spec(run, load_settings, point, offer["profile"], calibration,
                          frozen_sources=sources_by_profile.get(offer["profile"]), nonce=nonce)
        spec.load_point = None
        spec.workload.update({key: offer[key] for key in ("user_id", "profile", "cohort", "origin",
            "request_sequence", "work_sequence", "matched_work_sequence")})
        spec.workload.update(offer_index=index, population=window["population"], scenario=window["scenario"],
            phase=window["phase"], role=window["role"], window_id=window["id"],
            activation_seconds=users[offer["user_id"]]["activation_seconds"],
            matched_work_sha256=_matched_work_sha256(spec))
        return spec

    async def send(spec, annotate):
        offer = offers[spec.workload["offer_index"]]
        user = users[offer["user_id"]]
        spec.workload["ready_ms"] = spec.workload["scheduled_ms"]

        def started():
            offer["dispatched"] = True
            user["dispatched_count"] += 1

        def finished(record):
            annotate(record)
            offer["request_id"] = record.id
            user["request_ids"].append(record.id)

        return await run.request(target, client, spec, slot_reserved=True, budget_reserved=True,
                                 admission_deadline=run.start+spec.workload["scheduled_ms"]/1000
                                     -offer["offset_seconds"]+window["planned_duration_seconds"],
                                 on_start=started, finalize_record=finished)

    def stop():
        reason = run.stop_reason(target.name)
        return None if reason == "maximum request budget reached" else reason

    run.start_condition(target.name, window["id"], planned=len(offers))
    window.update(start_ms=_relative(run), end_ms=_relative(run)+window["planned_duration_seconds"]*1000,
                  planned_end_ms=_relative(run)+window["planned_duration_seconds"]*1000, reason=None)
    evidence = await run_stage(traffic=TrafficConfig(duration_seconds=window["planned_duration_seconds"],
        rates=[window["requests_per_second"]], max_in_flight=run.config.concurrency,
        scheduler_lag_tolerance_ms=100, drain_timeout_seconds=None), rate_rps=window["requests_per_second"]*window["population"],
        stage_index=0, fixtures={}, execute=send, reserve_offer=reserve, try_acquire=run.try_acquire_slot,
        release=run.release_slot, stop_reason=stop, deadline=run.deadline, limits=Limits(),
        schedule_override=[(o["offset_seconds"], o["profile"]) for o in offers], spec_factory=build,
        check_id="multi_user", scenario=window["id"], run_start=run.start, return_evidence=True,
        on_drop=lambda: run.drop_offer(target.name, window["id"]),
        schedule_observations=run.observations[target.name].setdefault("schedule_transitions", []),
        schedule_stage_id=window["id"], schedule_label=window["role"], schedule_pattern="constant")
    start = _relative(run, evidence.started_monotonic)
    end = start + evidence.sending_seconds*1000
    for index, offer in enumerate(offers):
        raw = evidence.outcomes[index]
        offer["outcome"] = ("dispatched" if offer["dispatched"] else raw if raw in {"dropped_local", "dropped_late"}
                            else "unissued" if offer["offered"] else "not_offered")
        offer["scheduled_ms"] = start + offer["offset_seconds"]*1000
        offer["scheduler_lag_ms"] = evidence.offer_lags_ms[index]
    window.update(start_ms=start, end_ms=end, planned_end_ms=start+window["planned_duration_seconds"]*1000,
        duration_seconds=evidence.sending_seconds, drain_seconds=max(0, evidence.observation_seconds-evidence.sending_seconds),
        cut_short=evidence.interrupted, reason="; ".join(evidence.reasons) or None,
        drain_reason="Drain interrupted" if evidence.drain_expired else None,
        offer_accounting=_offer_counts(offers), peak_in_flight=evidence.peak,
        planned_requests_per_second=len(offers)/window["planned_duration_seconds"],
        dispatched_requests_per_second=sum(o["dispatched"] for o in offers)/evidence.sending_seconds
            if evidence.sending_seconds else None,
        scheduler_stalls=[{"start_ms": start+a*1000, "end_ms": start+b*1000} for a, b in evidence.stalls])
    offers_by_user = {user["user_id"]: [] for user in window["users"]}
    for offer in offers:
        offers_by_user[offer["user_id"]].append(offer)
    for user in window["users"]:
        own = offers_by_user[user["user_id"]]
        user.update(offer_accounting=_offer_counts(own), scheduled_activation_ms=start+user["activation_seconds"]*1000,
                    stop_reason=window["reason"], active_duration_seconds=max(0, (end-user["activated_ms"])/1000)
                        if user["activated_ms"] is not None else 0)
    run.finish_condition(target.name, window["id"], reason=window["reason"])
    if evidence.interrupted:
        run.mark_unfinished(target.name, ["multi_user"])
    if asyncio.current_task().cancelling():
        raise asyncio.CancelledError


def _planned_observation(settings, *, include_offers=True):
    population = population_plan(settings)
    windows = []
    for level in settings.levels:
        if "arrivals" in settings.scenarios:
            window = _window(level, "measurement", settings.duration_seconds, population)
            window.update(id=f"multi_user_{level}_arrivals", scenario="arrivals", role="arrivals",
                          requests_per_second=settings.arrivals.requests_per_second)
            windows.append(window)
        if "recovery" in settings.scenarios:
            recovery = settings.recovery
            for role, seconds, rate in (("control", recovery.control_seconds, recovery.lighter_requests_per_second),
                    ("sustained", recovery.sustained_seconds, recovery.sustained_requests_per_second),
                    ("lighter", recovery.lighter_seconds, recovery.lighter_requests_per_second)):
                window = _window(level, "measurement", seconds, population)
                window.update(id=f"multi_user_{level}_recovery_{role}", scenario="recovery", role=role,
                    requests_per_second=rate, control_window_id=f"multi_user_{level}_recovery_control",
                    sustained_window_id=f"multi_user_{level}_recovery_sustained",
                    control_work_count=math.ceil(recovery.control_seconds*recovery.lighter_requests_per_second),
                    work_sequence_start=math.ceil(recovery.sustained_seconds*recovery.sustained_requests_per_second)
                        if role == "lighter" else 0)
                windows.append(window)
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
            if window["scenario"] in {"arrivals", "recovery"} and window["role"] in {"arrivals", "control"}:
                warmup = _window(window["population"], "warmup", settings.warmup_seconds, population)
                warmup.update(id=window["id"]+"_warmup", scenario=window["scenario"], role="warmup")
                expanded.append(warmup)
            elif window.get("role") and window["scenario"] not in {"arrivals", "recovery"}:
                warmup = deepcopy(window)
                warmup.update(id=window["id"] + "_warmup", phase="warmup",
                              planned_duration_seconds=settings.warmup_seconds)
                expanded.append(warmup)
            expanded.append(window)
        windows = expanded
    for window in windows:
        if window["scenario"] in {"arrivals", "recovery"} and window["phase"] == "measurement":
            arrivals = window["scenario"] == "arrivals"
            window["scheduled_offers"] = sum(_steady_offer_count(window["planned_duration_seconds"],
                settings.arrivals.activations.get(user["user_id"], 0) if arrivals else 0,
                window["requests_per_second"]) for user in window["users"])
            if arrivals:
                window["scheduled_offers"] += sum(burst.requests for burst in settings.arrivals.bursts)
            if include_offers:
                window["offers"] = _offer_plan(window, settings)
                window["offer_accounting"] = _offer_counts(window["offers"])
                offers_by_user = {user["user_id"]: [] for user in window["users"]}
                for offer in window["offers"]:
                    offers_by_user[offer["user_id"]].append(offer)
                for user in window["users"]:
                    user["offer_accounting"] = _offer_counts(offers_by_user[user["user_id"]])
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
            if "offers" in window:
                await run_arrival_window(run, target, client, settings, load_settings, calibration, window,
                                         sources_by_profile=sources_by_profile, sequences=sequences)
            else:
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
        worst = summary["worst_user"]
        fairness_failures = [window["fairness"] for window in summary["windows"]
                             if window.get("fairness", {}).get("status") == "fail"]
        message = (f"{summary['failed_user_windows']} user measurements failed; "
                   f"{worst['user_id']} at {worst['population']} users: "
                   + " ".join(worst["reasons"])) if summary["failed_user_windows"] else "Observed delivery fairness failed."
        if fairness_failures:
            message += " " + " ".join(reason for result in fairness_failures for reason in result["reasons"])
        interference_failures = [user for window in summary["windows"]
                                 for user in window.get("interference", {}).get("users", []) if user["status"] == "fail"]
        if interference_failures:
            detail = " ".join(f"{user['user_id']}: {' '.join(user['reasons'])}" for user in interference_failures)
            message = (message + " " if summary["failed_user_windows"] else "Observed noisy-neighbor interference failed. ") + detail
        recovery_failures = [user for window in summary["windows"]
                             for user in window.get("recovery", {}).get("users", []) if user["status"] == "fail"]
        if recovery_failures:
            message = "Observed per-user recovery failed. " + " ".join(
                f"{user['user_id']}: {' '.join(user['reasons'])}" for user in recovery_failures)
    elif status == "pass":
        message = "Every measured user met the selected criteria over the common windows."
    else:
        message = "Per-user acceptance is inconclusive because required evidence is incomplete."
    return CheckResult(id="multi_user", target=target.name, title="Multi-user",
                       status="inconclusive" if status == "measured" else status,
                       summary=message, metrics={"multi_user": summary},
                       evidence_ids=[r.id for r in records])
