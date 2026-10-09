"""Finite arrival-rate traffic with immediate admission and scored goodput.

The scheduler has no request queue. A missing slot is evidence of the local
generator's limit, not a measurement of the service's internal queue.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable

from .models import MAX_PLANNED_ARRIVALS, Limits, RequestRecord, RequestSpec, TrafficConfig


def arrival_schedule(traffic: TrafficConfig, rate_rps: float, stage_index: int = 0):
    """Return a bounded, reproducible list of (offset seconds, workload class)."""
    arrivals, choices = random.Random(traffic.seed + stage_index), random.Random(
        (traffic.seed + stage_index) ^ 0x6A09E667
    )
    weights = traffic.mix.model_dump() if hasattr(traffic.mix, "model_dump") else traffic.mix
    classes = [key for key, weight in weights.items() if weight > 0]
    cumulative, total = [], 0.0
    for key in classes:
        total += weights[key]
        cumulative.append(total)
    schedule = []
    offset = 0.0 if traffic.arrival == "steady" else arrivals.expovariate(rate_rps)
    while offset < traffic.duration_seconds:
        if len(schedule) == MAX_PLANNED_ARRIVALS:
            return schedule, True
        choice = choices.random() * total
        kind = next(key for key, bound in zip(classes, cumulative, strict=True) if choice < bound)
        schedule.append((offset, kind))
        offset = (len(schedule) / rate_rps if traffic.arrival == "steady" else
                  offset + arrivals.expovariate(rate_rps))
    return schedule, False


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


def _timing(record: RequestRecord, limits: Limits) -> tuple[bool, bool]:
    """Return (violated, missing); arrival-relative latency includes dispatch lag."""
    measured = (
        (limits.first_output_ms, record.arrival_first_output_ms, False),
        (limits.latency_ms, record.arrival_elapsed_ms, False),
        (limits.stream_gap_ms, record.max_stream_gap_ms if record.stream else None, False),
        (limits.min_output_tokens_per_second, record.generation_tokens_per_second, True),
    )
    violated = missing = False
    for limit, value, minimum in measured:
        if limit is None:
            continue
        if value is None:
            missing = True
        elif (value < limit if minimum else value > limit):
            violated = True
    return violated, missing


def _record_stats(records: list[RequestRecord], limits: Limits) -> dict:
    completed = [r for r in records if r.status == "completed" and r.valid]
    scored = [r for r in completed if r.score is not None]
    timings = [_timing(r, limits) for r in completed]
    good = sum(r.score is True and not bad and not missing
               for r, (bad, missing) in zip(completed, timings, strict=True))
    result = {
        "completed": len(completed),
        "failed": sum(r.status == "failed" or (r.status == "completed" and not r.valid)
                      for r in records),
        "timed_out": sum(r.status == "timeout" for r in records),
        "cancelled": sum(r.status == "cancelled" for r in records),
        "scored": len(scored), "correct": sum(r.score is True for r in scored),
        "correctness": sum(r.score is True for r in scored) / len(scored) if scored else None,
        "good": good, "timing_violations": sum(bad for bad, _ in timings),
        "missing_timing": sum(missing for _, missing in timings),
        "unscored_completions": len(completed) - len(scored),
    }
    for name, attribute in (
        ("dispatch_lag", "dispatch_lag_ms"),
        ("arrival_first_output", "arrival_first_output_ms"),
        ("arrival_latency", "arrival_elapsed_ms"),
        ("first_output", "first_output_ms"),
        ("latency", "elapsed_ms"),
    ):
        values = [value for r in completed if (value := getattr(r, attribute)) is not None]
        for label, fraction in (("p50", .5), ("p95", .95)):
            result[f"{name}_{label}_ms"] = _percentile(values, fraction)
    return result


async def run_stage(
    *,
    traffic: TrafficConfig,
    rate_rps: float,
    stage_index: int,
    fixtures: dict[str, list[RequestSpec]],
    execute: Callable[[RequestSpec], Awaitable[RequestRecord | None]],
    reserve_offer: Callable[[], bool],
    try_acquire: Callable[[], bool],
    release: Callable[[], None],
    stop_reason: Callable[[], str | None],
    deadline: float,
    limits: Limits,
) -> dict:
    """Run one stage; callbacks own shared run budgets and global admission slots.

    ``traffic.max_in_flight`` must already be resolved against the global bound.
    ``execute`` uses the reserved slot without waiting for another semaphore and
    must return after cancellation, as the existing HTTP client does.
    """
    if traffic.max_in_flight is None:
        raise ValueError("resolve traffic.max_in_flight against the global bound first")
    schedule, overflow = arrival_schedule(traffic, rate_rps, stage_index)
    if any(not fixtures.get(kind) for _, kind in schedule):
        raise ValueError("every scheduled traffic class needs a scored fixture")
    started_at = time.monotonic()
    stage_end = started_at + traffic.duration_seconds
    outcomes = ["not_offered"] * len(schedule)
    records: list[tuple[int, RequestRecord]] = []
    lifetimes: dict[int, tuple[float, float]] = {}
    tasks: set[asyncio.Task] = set()
    reasons = []
    peak = 0
    execution_errors = 0
    discard_through = -1.0
    interrupted = overflow
    drain_expired = False
    if overflow:
        reasons.append("The Poisson schedule exceeded the finite arrival bound.")

    def interruption() -> str | None:
        return stop_reason() or ("Global duration reached." if time.monotonic() >= deadline else None)

    async def wait_until(when: float) -> str | None:
        while time.monotonic() < when:
            if reason := interruption():
                return reason
            await asyncio.sleep(min(.05, max(0, when - time.monotonic())))
        return interruption()

    def missed_arrival(offset: float, now: float) -> bool:
        nonlocal discard_through
        if (now - started_at - offset) * 1000 > traffic.scheduler_lag_tolerance_ms:
            # Discard the entire already-due backlog after an excessive stall,
            # rather than replaying it in a catch-up burst. Close Poisson
            # arrivals within the explicit tolerance are legitimate clusters;
            # the next random gap is not a scheduler lateness threshold.
            discard_through = max(discard_through, now - started_at)
        return offset <= discard_through

    async def dispatch(index: int, kind: str, offset: float):
        nonlocal execution_errors
        dispatched = time.monotonic()
        lag_ms = max(0.0, (dispatched - started_at - offset) * 1000)
        if missed_arrival(offset, dispatched):
            outcomes[index] = "dropped_late"
            return
        outcomes[index] = "started"
        try:
            spec = fixtures[kind][index % len(fixtures[kind])].model_copy(update={
                "scenario": f"traffic_stage_{stage_index}", "check_ids": ["capacity"],
            })
            record = await execute(spec)
            if record is None:
                outcomes[index] = "not_offered"
                return
            record.traffic_stage = stage_index
            record.traffic_rate_rps = rate_rps
            record.traffic_class = kind
            record.scheduled_offset_ms = offset * 1000
            record.dispatch_lag_ms = lag_ms
            record.arrival_first_output_ms = (lag_ms + record.first_output_ms
                                               if record.first_output_ms is not None else None)
            record.arrival_elapsed_ms = lag_ms + record.elapsed_ms
            records.append((index, record))
        except asyncio.CancelledError:
            outcomes[index] = "cancelled"
        except Exception:
            # Backend normally records exceptions. Keep a truthful failure if a
            # callback unexpectedly escapes without leaking transport text.
            execution_errors += 1
        finally:
            lifetimes[index] = (dispatched - started_at, time.monotonic() - started_at)

    def finished(task: asyncio.Task):
        # A done callback also runs when cancellation happens before the task's
        # coroutine starts; a coroutine finalizer alone can leak that slot.
        release()
        tasks.discard(task)

    try:
        if not overflow:
            for index, (offset, kind) in enumerate(schedule):
                reason = await wait_until(started_at + offset)
                if reason:
                    reasons.append(reason)
                    interrupted = True
                    break
                if not reserve_offer():
                    reasons.append("Request budget stopped the planned arrivals.")
                    interrupted = True
                    break
                now = time.monotonic()
                if missed_arrival(offset, now):
                    outcomes[index] = "dropped_late"
                    continue
                active = sum(not task.done() for task in tasks)
                if active >= traffic.max_in_flight or not try_acquire():
                    outcomes[index] = "dropped_local"
                    continue
                outcomes[index] = "dispatching"
                task = asyncio.create_task(dispatch(index, kind, offset))
                tasks.add(task)
                task.add_done_callback(finished)
                peak = max(peak, active + 1)
                # Give the reserved request a chance to start before processing
                # another due arrival; this never waits for its completion.
                await asyncio.sleep(0)
            else:
                if reason := await wait_until(stage_end):
                    reasons.append(reason)
                    interrupted = True

        drain_deadline = min(deadline, time.monotonic() + traffic.drain_timeout_seconds)
        while tasks:
            if reason := interruption():
                reasons.append(reason)
                interrupted = True
                break
            remaining = drain_deadline - time.monotonic()
            if remaining <= 0:
                reasons.append("Admitted requests exceeded the drain deadline.")
                drain_expired = True
                break
            await asyncio.wait(tasks, timeout=min(.05, remaining))
    except asyncio.CancelledError:
        reasons.append("Traffic stage cancelled.")
        interrupted = True
    finally:
        # Copy: task completion callbacks remove tasks from the live set.
        pending = list(tasks)
        for task in pending:
            task.cancel()
        if pending:
            cleanup = asyncio.gather(*pending, return_exceptions=True)
            while not cleanup.done():
                try:
                    # The run monitor can cancel this stage after it has
                    # already noticed a user stop and begun draining. Preserve
                    # those partial records instead of interrupting cleanup.
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    reasons.append("Traffic stage cancelled.")
                    interrupted = True

    counts = Counter(outcomes)
    started = counts["started"] + counts["cancelled"]
    not_offered = counts["not_offered"] + counts["dispatching"]
    evidence = [record for _, record in records]
    stats = _record_stats(evidence, limits)
    stats["failed"] += execution_errors
    stats["cancelled"] += counts["cancelled"]
    error_rate = (stats["failed"] + stats["timed_out"]) / started if started else None
    generator_limited = bool(counts["dropped_local"] or counts["dropped_late"] or overflow)
    fully_offered = not (not_offered or generator_limited or interrupted)
    service_failure = error_rate is not None and error_rate > limits.max_error_rate
    answer_failure = stats["correctness"] is not None and stats["correctness"] < limits.min_correctness
    timing_failure = bool(stats["timing_violations"] and not counts["dropped_late"])
    if service_failure:
        reasons.append("Service failures exceeded the error limit.")
    if answer_failure:
        reasons.append("Known-answer correctness was below the required fraction.")
    if stats["timing_violations"]:
        reasons.append("Answers exceeded configured timing limits." if timing_failure else
                       "Timing violations were observed with excessive scheduler lateness.")
    if generator_limited:
        reasons.append("Local admission or scheduling limits prevented the configured load.")
    if not fully_offered:
        reasons.append("The full planned arrival schedule was not delivered.")
    enough_samples = stats["scored"] >= limits.min_samples
    if not enough_samples:
        reasons.append("Too few scored completions to establish capacity.")
    latency_configured = limits.first_output_ms is not None or limits.latency_ms is not None
    if not latency_configured:
        reasons.append("Set a first-output or total-latency limit to establish capacity.")
    if stats["missing_timing"] or stats["unscored_completions"]:
        reasons.append("Some completed answers lack required timing or correctness evidence.")
    if stats["cancelled"]:
        reasons.append("Some admitted requests were cancelled before completion.")
    failed = service_failure or answer_failure or timing_failure
    accepted = bool(not failed and fully_offered and enough_samples and latency_configured
                    and not drain_expired and not stats["missing_timing"]
                    and not stats["unscored_completions"] and not stats["cancelled"])
    windows = []
    for number in range(5):
        lower, upper = (traffic.duration_seconds * number / 5,
                        traffic.duration_seconds * (number + 1) / 5)
        indices = {i for i, (offset, _) in enumerate(schedule) if lower <= offset < upper}
        window_records = [record for index, record in records if index in indices]
        window_counts = Counter(outcomes[index] for index in indices)
        window_stats = _record_stats(window_records, limits)
        window_started = window_counts["started"] + window_counts["cancelled"]
        windows.append({
            "from_seconds": lower, "to_seconds": upper, "scheduled": len(indices),
            "started": window_started, "dropped_local": window_counts["dropped_local"],
            "dropped_late": window_counts["dropped_late"],
            "not_offered": window_counts["not_offered"] + window_counts["dispatching"],
            "pending_at_end": sum(begin <= upper < end for begin, end in lifetimes.values()),
            "error_rate": ((window_stats["failed"] + window_stats["timed_out"]) / window_started
                           if window_started else None),
            **window_stats,
        })
    return {
        "stage_index": stage_index, "rate_rps": rate_rps,
        "duration_seconds": traffic.duration_seconds,
        "observation_seconds": time.monotonic() - started_at,
        "scheduled": len(schedule), "started": started,
        "dropped_local": counts["dropped_local"], "dropped_late": counts["dropped_late"],
        "not_offered": not_offered, "schedule_truncated": overflow,
        "scheduled_rps": len(schedule) / traffic.duration_seconds,
        "achieved_rps": started / traffic.duration_seconds,
        "goodput_rps": stats["good"] / traffic.duration_seconds,
        "good_fraction": stats["good"] / len(schedule) if schedule else None,
        "error_rate": error_rate, "peak_in_flight": peak,
        "fully_offered": fully_offered, "generator_limited": generator_limited,
        "drain_expired": drain_expired, "accepted": accepted,
        "status": "fail" if failed else "pass" if accepted else "inconclusive",
        "reasons": list(dict.fromkeys(reasons)), "windows": windows,
        "planned_mix": dict(Counter(kind for _, kind in schedule)),
        **stats,
    }
