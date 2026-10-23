"""Finite arrival-rate traffic with immediate admission and scored goodput.

The scheduler has no request queue. A missing slot is evidence of the local
generator's limit, not a measurement of the service's internal queue.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from statistics import mean, median
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import MAX_PLANNED_ARRIVALS, Limits, RequestRecord, RequestSpec, TrafficConfig
from .timing import qualify, qualified_violations


class ScheduleObservation:
    """Record scheduler intent, independently of admission or actual occupancy.

    Intervals use the run's monotonic origin, as do request dispatch timestamps.
    They are appended when entered (including live/partial runs), then closed in
    place. A zero target during drain means no further work is intended, even
    though outstanding requests can remain active above that target.
    """

    def __init__(self, rows, *, start, origin, stage_id, check_id, kind,
                 concurrency=None, rate=None, label=None, pattern=None):
        self.rows = rows
        self.origin = origin
        self.sending = {
            "stage_id": stage_id, "check_id": check_id, "kind": kind,
            "label": label or stage_id, "pattern": pattern,
            "start_ms": (start - origin) * 1000, "end_ms": None,
            "intended_concurrency": concurrency, "intended_rate_rps": rate,
        }
        self.drain = None
        if rows is not None:
            rows.append(self.sending)

    def begin_drain(self, when):
        if self.drain is not None:
            return
        elapsed = max(self.sending["start_ms"], (when - self.origin) * 1000)
        self.sending["end_ms"] = elapsed
        self.drain = {
            **self.sending, "stage_id": self.sending["stage_id"] + ".drain",
            "kind": "drain", "label": "Drain", "start_ms": elapsed, "end_ms": None,
            "intended_concurrency": 0 if self.sending["intended_concurrency"] is not None else None,
            "intended_rate_rps": 0 if self.sending["intended_rate_rps"] is not None else None,
        }
        if self.rows is not None:
            self.rows.append(self.drain)

    def finish(self, when):
        self.begin_drain(when)
        self.drain["end_ms"] = max(self.drain["start_ms"], (when - self.origin) * 1000)


def arrival_schedule(traffic: TrafficConfig, rate_rps: float, stage_index: int = 0, *, poisson_unconditioned: bool = False):
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
    if traffic.arrival == "poisson" and poisson_unconditioned:
        schedule, offset = [], arrivals.expovariate(rate_rps)
        while offset < traffic.duration_seconds:
            if len(schedule) >= MAX_PLANNED_ARRIVALS:
                return schedule, True
            kind = choices.choices(classes, weights=[weights[k] for k in classes])[0]
            schedule.append((offset, kind))
            offset += arrivals.expovariate(rate_rps)
        return schedule, False
    # Condition Poisson arrivals on N so a seeded draw cannot under-offer a rate.
    count = (round(rate_rps * traffic.duration_seconds) if traffic.arrival == "poisson"
             else math.ceil(rate_rps * traffic.duration_seconds))
    if traffic.arrival == "steady":
        while count > 0 and (count - 1) / rate_rps >= traffic.duration_seconds:
            count -= 1
        while count / rate_rps < traffic.duration_seconds:
            count += 1
    overflow = count > MAX_PLANNED_ARRIVALS
    count = min(count, MAX_PLANNED_ARRIVALS)
    offsets = (sorted(arrivals.random() * traffic.duration_seconds for _ in range(count))
               if traffic.arrival == "poisson" else None)
    schedule = []
    for index in range(count):
        choice = choices.random() * total
        kind = next(key for key, bound in zip(classes, cumulative, strict=True) if choice < bound)
        schedule.append((index / rate_rps if offsets is None else offsets[index], kind))
    return schedule, overflow


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
        (limits.stream_gap_ms if record.stream_gap_applicable else None,
         record.max_stream_gap_ms if record.stream else None, False),
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


@dataclass(frozen=True)
class StageEvidence:
    """Inference-only stage facts, retained privately while scores are pending.

    Records are deliberately shared so post-run evaluation can update scores.
    All scheduler inputs and timings are captured before evaluation starts.
    """

    stage_index: int
    rate_rps: float
    duration_seconds: float
    observation_seconds: float
    schedule: tuple[tuple[float, str], ...]
    outcomes: tuple[str, ...]
    records: tuple[tuple[int, RequestRecord], ...]
    lifetimes: tuple[tuple[float, float], ...]
    reasons: tuple[str, ...]
    peak: int
    execution_errors: int
    overflow: bool
    interrupted: bool
    drain_expired: bool
    case_scorers: tuple[tuple[str, str], ...] | None
    stalls: tuple[tuple[float, float], ...] = ()
    arrival: str = "steady"
    started_monotonic: float | None = None
    sending_seconds: float | None = None
    offer_lags_ms: tuple[float | None, ...] = ()


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
    case_specs: list[RequestSpec] | None = None,
    active_case_specs: list[RequestSpec] | None = None,
    capture_evidence: Callable[[StageEvidence], None] | None = None,
    on_drop: Callable[[], None] | None = None,
    schedule_override: list[tuple[float, str]] | None = None,
    specs_override: list[RequestSpec] | None = None,
    spec_factory: Callable[[int], RequestSpec] | None = None,
    check_id: str = "capacity",
    scenario: str | None = None,
    run_start: float | None = None,
    return_evidence: bool = False,
    schedule_observations: list[dict] | None = None,
    schedule_stage_id: str | None = None,
    schedule_label: str | None = None,
    schedule_pattern: str | None = None,
) -> dict:
    """Run one stage; callbacks own shared run budgets and global admission slots.

    ``traffic.max_in_flight`` must already be resolved against the global bound.
    ``execute`` uses the reserved slot without waiting for another semaphore and
    must return after cancellation, as the existing HTTP client does.
    ``on_drop`` reports reserved offers that never dispatch, including tasks
    cancelled before starting; unreserved schedule entries are not drops.
    """
    if traffic.max_in_flight is None:
        raise ValueError("resolve traffic.max_in_flight against the global bound first")
    if schedule_override is None:
        schedule, overflow = arrival_schedule(traffic, rate_rps, stage_index)
    else:
        schedule, overflow = schedule_override, len(schedule_override) > MAX_PLANNED_ARRIVALS
    if case_specs is not None and len(case_specs) != len(schedule):
        raise ValueError("case sequence must match the finite arrival schedule")
    if case_specs is None and specs_override is None and spec_factory is None and any(not fixtures.get(kind) for _, kind in schedule):
        raise ValueError("every scheduled traffic class needs a scored fixture")
    selected_specs = (None if spec_factory is not None else specs_override if specs_override is not None else case_specs if case_specs is not None else [
        fixtures[kind][index % len(fixtures[kind])] for index, (_, kind) in enumerate(schedule)
    ])
    if case_specs is not None:
        schedule = [(offset, spec.workload["case_id"])
                    for (offset, _), spec in zip(schedule, selected_specs, strict=True)]
    started_at = time.monotonic()
    intent = ScheduleObservation(
        schedule_observations, start=started_at,
        origin=run_start if run_start is not None else started_at,
        stage_id=schedule_stage_id or scenario or f"traffic_stage_{stage_index}",
        check_id=check_id, kind="arrival_rate", label=schedule_label,
        pattern=schedule_pattern or traffic.arrival,
        # A synthetic rate used to drive a finite burst schedule is not an
        # intended continuous arrival rate.
        rate=None if schedule_pattern == "bursts" else rate_rps,
    )
    stage_end = started_at + traffic.duration_seconds
    sending_finished_at = None
    offer_lags_ms = [None] * len(schedule)
    outcomes = ["not_offered"] * len(schedule)
    dropped_indices: set[int] = set()
    records: list[tuple[int, RequestRecord]] = []
    lifetimes: dict[int, tuple[float, float]] = {}
    tasks: set[asyncio.Task] = set()
    reasons = []
    peak = 0
    execution_errors = 0
    discard_through = -1.0
    stalls: list[tuple[float, float]] = []
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
            stalls.append((offset, now - started_at))
        return offset <= discard_through

    def drop(index: int, outcome: str):
        outcomes[index] = outcome
        if index not in dropped_indices:
            dropped_indices.add(index)
            if on_drop:
                on_drop()

    async def dispatch(index: int, kind: str, offset: float):
        nonlocal execution_errors
        dispatched = time.monotonic()
        lag_ms = max(0.0, (dispatched - started_at - offset) * 1000)
        offer_lags_ms[index] = lag_ms
        if missed_arrival(offset, dispatched):
            drop(index, "dropped_late")
            return
        outcomes[index] = "started"
        try:
            spec = (spec_factory(index) if spec_factory is not None else selected_specs[index]).model_copy(update={
                "scenario": scenario or f"traffic_stage_{stage_index}", "check_ids": [check_id],
            })
            if run_start is not None:
                spec.workload = {**spec.workload, "scheduled_ms": (started_at - run_start + offset) * 1000}
            record = await execute(spec)
            if record is None:
                drop(index, "not_offered")
                return
            record.traffic_stage = stage_index
            record.traffic_rate_rps = rate_rps
            record.traffic_class = kind
            record.scheduled_offset_ms = offset * 1000
            if run_start is not None and record.scheduled_ms is not None:
                clock_start = record.timing_start_ms if record.timing_start_ms is not None else record.dispatch_ms
                if clock_start is not None:
                    lag_ms = max(lag_ms, clock_start-record.scheduled_ms)
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
                offer_lags_ms[index] = max(0, (now - started_at - offset) * 1000)
                if missed_arrival(offset, now):
                    drop(index, "dropped_late")
                    continue
                active = sum(not task.done() for task in tasks)
                if active >= traffic.max_in_flight or not try_acquire():
                    drop(index, "dropped_local")
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

        sending_finished_at = time.monotonic()
        intent.begin_drain(sending_finished_at)
        drain_deadline = (deadline if traffic.drain_timeout_seconds is None else
                          min(deadline, sending_finished_at + traffic.drain_timeout_seconds))
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
        if sending_finished_at is None:
            sending_finished_at = time.monotonic()
        intent.begin_drain(sending_finished_at)
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
        intent.finish(time.monotonic())

    # A task cancelled before its coroutine starts still consumed an offer.
    for index, outcome in enumerate(outcomes):
        if outcome == "dispatching":
            drop(index, "not_offered")

    case_scorers = None
    if case_specs is not None:
        active_cases = {spec.workload["case_id"]: spec.scorer for spec in active_case_specs or []}
        for spec in selected_specs:
            active_cases.setdefault(spec.workload["case_id"], spec.scorer)
        case_scorers = tuple(active_cases.items())
    evidence = StageEvidence(
        stage_index=stage_index, rate_rps=rate_rps,
        duration_seconds=traffic.duration_seconds,
        observation_seconds=time.monotonic() - started_at,
        schedule=tuple(schedule), outcomes=tuple(outcomes), records=tuple(records),
        lifetimes=tuple(lifetimes.values()), reasons=tuple(reasons), peak=peak,
        execution_errors=execution_errors, overflow=overflow, interrupted=interrupted,
        drain_expired=drain_expired, case_scorers=case_scorers, stalls=tuple(stalls),
        arrival=traffic.arrival,
        started_monotonic=started_at,
        sending_seconds=min(traffic.duration_seconds, sending_finished_at-started_at),
        offer_lags_ms=tuple(offer_lags_ms),
    )
    result = evidence if return_evidence else finalize_stage(evidence, limits)
    if capture_evidence is not None:
        capture_evidence(evidence)
    return result


def finalize_stage(evidence: StageEvidence, limits: Limits) -> dict:
    """Aggregate original stage facts with the records' current correctness scores."""
    schedule, outcomes, records = evidence.schedule, evidence.outcomes, evidence.records
    reasons = list(evidence.reasons)
    counts = Counter(outcomes)
    started = counts["started"] + counts["cancelled"]
    not_offered = counts["not_offered"] + counts["dispatching"]
    stats = _record_stats([record for _, record in records], limits)
    stats["failed"] += evidence.execution_errors
    stats["cancelled"] += counts["cancelled"]
    error_rate = (stats["failed"] + stats["timed_out"]) / started if started else None
    generator_limited = bool(counts["dropped_local"] or counts["dropped_late"] or evidence.overflow)
    fully_offered = not (not_offered or generator_limited or evidence.interrupted)
    service_failure = error_rate is not None and error_rate > limits.max_error_rate
    # Missing judgments are uncertainty, not wrong answers. Only fail when even
    # every remaining judgment passing could not satisfy the required fraction.
    best_possible_correctness = (
        (stats["correct"] + stats["unscored_completions"]) / stats["completed"]
        if stats["completed"] else None
    )
    answer_failure = (best_possible_correctness is not None
                      and best_possible_correctness < limits.min_correctness)
    timing_failure = bool(stats["timing_violations"])
    if timing_failure and evidence.stalls:
        # Express records and discarded windows on the same stage-relative clock.
        timing_records = [record.model_copy(update={
            "dispatch_ms": schedule[index][0] * 1000 + (record.dispatch_lag_ms or 0),
            "timing_start_ms": schedule[index][0] * 1000 + (record.dispatch_lag_ms or 0),
            "scheduled_ms": schedule[index][0] * 1000,
            "completed_ms": schedule[index][0] * 1000 + (
                record.arrival_elapsed_ms if record.arrival_elapsed_ms is not None else record.elapsed_ms),
        }) for index, record in records]
        health = {"schema_version": 1, "windows": [
            {"start_ms": low * 1000, "end_ms": high * 1000, "lag_ms": (high-low) * 1000}
            for low, high in evidence.stalls]}
        quality = qualify(timing_records, health, 0)
        for record in timing_records:
            span = (record.last_output_ms or 0) - (record.first_output_ms or 0)
            speed = record.generation_tokens_per_second
            overages = {"latency_ms": (record.arrival_elapsed_ms or 0) - (limits.latency_ms or math.inf),
                        "first_output_ms": (record.arrival_first_output_ms or 0) - (limits.first_output_ms or math.inf),
                        "max_stream_gap_ms": (record.max_stream_gap_ms or 0) - (limits.stream_gap_ms or math.inf),
                        "generation_tokens_per_second": span * (1 - speed / limits.min_output_tokens_per_second)
                            if speed is not None and limits.min_output_tokens_per_second else 0}
            for metric, overage in overages.items():
                # A brief overlapping stall cannot explain a large overage.
                if overage > 0 and record.id in quality["affected_metrics"][metric] and record.id not in qualify(
                        [record], health, max(0, overage * .5 - 1e-9), metrics=(metric,))["affected_metrics"][metric]:
                    quality["affected_metrics"][metric].remove(record.id)
        timing_failure = any(_timing(record, limits)[0] and qualified_violations(
            [record], limits if record.stream_gap_applicable else limits.model_copy(update={"stream_gap_ms": None}),
            quality, arrival=True) for record in timing_records)
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
    case_counts = {}
    coverage_complete = all_cases_scored = True
    if evidence.case_scorers is not None:
        active_cases = dict(evidence.case_scorers)
        all_cases_scored = bool(active_cases) and all(scorer != "none" for scorer in active_cases.values())
        indices_by_case = defaultdict(list)
        records_by_case = defaultdict(list)
        for index, (_, case_id) in enumerate(schedule):
            indices_by_case[case_id].append(index)
        for index, record in records:
            records_by_case[schedule[index][1]].append(record)
        for case_id in active_cases:
            indices = indices_by_case[case_id]
            case_outcomes = Counter(outcomes[index] for index in indices)
            case_records = records_by_case[case_id]
            case_stats = _record_stats(case_records, limits)
            missing = case_outcomes["not_offered"] + case_outcomes["dispatching"]
            case_counts[case_id] = {
                "planned": len(indices), "offered": len(indices) - missing,
                "started": case_outcomes["started"] + case_outcomes["cancelled"],
                "valid": case_stats["completed"], "scored": case_stats["scored"],
                "correct": case_stats["correct"],
                "dropped_local": case_outcomes["dropped_local"],
                "dropped_late": case_outcomes["dropped_late"], "not_offered": missing,
                "drops": case_outcomes["dropped_local"] + case_outcomes["dropped_late"],
            }
        coverage_complete = bool(case_counts) and all(row["scored"] >= 1 for row in case_counts.values())
        if not coverage_complete:
            reasons.append("Every active case needs a valid scored completion at this rate.")
        if not all_cases_scored:
            reasons.append("An active unscored case prevents a full-pack correct-capacity claim.")
    failed = service_failure or answer_failure or timing_failure
    passed = bool(not failed and fully_offered and enough_samples and latency_configured
                    and not evidence.drain_expired and not stats["missing_timing"]
                    and not stats["unscored_completions"] and not stats["cancelled"]
                    and coverage_complete and all_cases_scored)
    windows = []
    for number in range(5):
        lower, upper = (evidence.duration_seconds * number / 5,
                        evidence.duration_seconds * (number + 1) / 5)
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
            "pending_at_end": sum(begin <= upper < end for begin, end in evidence.lifetimes),
            "time_weighted_in_flight": sum(max(0, min(end, upper)-max(begin, lower))
                                           for begin, end in evidence.lifetimes) / (upper-lower),
            "error_rate": ((window_stats["failed"] + window_stats["timed_out"]) / window_started
                           if window_started else None),
            **window_stats,
        })
    # Theil-Sen on at most 64 evenly sampled requests bounds pairwise work.
    # Calibrated: 2,000 seeds, conditioned Poisson, FIFO 0.5 s mean service with
    # lognormal sigma=.3, 60 s stages. Slope > .08 s/s, rise > .5 * early mean,
    # end backlog/last-window occupancy > 1.3 * rate * early mean: 99.45/100%
    # rejected at 120/150% load; 0/.45% flagged at 60/80%. Below 80 Poisson
    # samples, require slope > .6 to flag growth; slope > .02 with
    # p90-p10 > max(.25*p10, .01*T) leaves stability unverified instead.
    # At 10/20 s this rejects >=95% of overload seeds while flagging <=2% of
    # healthy seeds (see regression).
    latencies = sorted((schedule[index][0], record.arrival_elapsed_ms / 1000)
                       for index, record in records if record.valid and record.status == "completed"
                       and record.arrival_elapsed_ms is not None)
    queue_growth = stability_unverified = False
    short_poisson = evidence.arrival == "poisson" and len(latencies) < 80
    if short_poisson and latencies:
        values = sorted(value for _, value in latencies)
        low, high = (values[int(q * (len(values) - 1))] for q in (.1, .9))
        stability_unverified = high > max(1.25 * low, low + .01 * evidence.duration_seconds)
    if len(latencies) >= 6:
        sampled = latencies[::max(1, math.ceil(len(latencies) / 64))]
        slopes = [(y2-y1) / (x2-x1) for i, (x1, y1) in enumerate(sampled)
                  for x2, y2 in sampled[i+1:] if x2 > x1]
        early = [value for offset, value in latencies if offset < evidence.duration_seconds / 5]
        early_mean = mean(early or [value for _, value in latencies[:max(1, len(latencies) // 5)]])
        slope = median(slopes) if slopes else 0
        stability_unverified = stability_unverified and slope > .02
        queue_growth = bool(slope > (.6 if short_poisson else .08)
                            and slope * evidence.duration_seconds > .5 * early_mean
                            and windows[-1]["pending_at_end"]
                            and max(windows[-1]["pending_at_end"], windows[-1]["time_weighted_in_flight"])
                                > 1.3 * evidence.rate_rps * early_mean)
    if queue_growth:
        reasons.append("Queue grew throughout the stage; rate not sustained.")
    if stability_unverified:
        reasons.append("Too few variable-latency Poisson samples to distinguish transient bursts from unsustainable load (need 80).")
    accepted = passed and not queue_growth and not stability_unverified
    return {
        "stage_index": evidence.stage_index, "rate_rps": evidence.rate_rps,
        "duration_seconds": evidence.duration_seconds,
        "observation_seconds": evidence.observation_seconds,
        "scheduled": len(schedule), "started": started,
        "dropped_local": counts["dropped_local"], "dropped_late": counts["dropped_late"],
        "not_offered": not_offered, "schedule_truncated": evidence.overflow,
        "scheduled_rps": len(schedule) / evidence.duration_seconds,
        "achieved_rps": started / evidence.duration_seconds,
        "goodput_rps": stats["good"] / evidence.duration_seconds,
        "valid_throughput_rps": stats["completed"] / evidence.duration_seconds,
        "scored_subset_correct_goodput_rps": stats["good"] / evidence.duration_seconds,
        **({"case_counts": case_counts, "coverage_complete": coverage_complete,
            "all_cases_scored": all_cases_scored} if evidence.case_scorers is not None else {}),
        "good_fraction": stats["good"] / len(schedule) if schedule else None,
        "error_rate": error_rate, "peak_in_flight": evidence.peak,
        "fully_offered": fully_offered, "generator_limited": generator_limited,
        "drain_expired": evidence.drain_expired, "accepted": accepted, "queue_growth": queue_growth,
        "stability_unverified": stability_unverified,
        "status": "fail" if failed else "pass" if accepted else "inconclusive",
        "reasons": list(dict.fromkeys(reasons)), "windows": windows,
        "planned_mix": dict(Counter(kind for _, kind in schedule)),
        **stats,
    }
