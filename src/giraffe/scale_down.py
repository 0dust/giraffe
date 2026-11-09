"""Evaluate one external replica removal without inferring missing backend facts."""

from __future__ import annotations

import math
import time
from bisect import bisect_right
from dataclasses import dataclass

from .caps import MAX_SANITIZE_ITEMS
from .client import REASONING_BUDGET_ERROR
from .deployment import scale_down_enabled
from .fixtures import reasoning_budget
from .models import (
    MAX_PLANNED_ARRIVALS, MAX_SCALE_DOWN_SNAPSHOTS, MAX_SIMULTANEOUS_REQUESTS,
    CheckResult, Limits, RequestRecord, ScaleDownConfig, ScaleDownEvidence,
    ScaleDownInventory, ScaleDownLifecycleFact, ScaleDownSnapshot, ScaleDownSubresult,
)
from .timing import configured_metrics, qualify, qualified_violations, threshold
from .traffic import _timing


@dataclass(frozen=True)
class _History:
    snapshots: tuple[ScaleDownSnapshot, ...]
    events: tuple[ScaleDownLifecycleFact, ...]
    inventories: tuple[ScaleDownInventory, ...]
    conflicts: tuple[str, ...]


def _history(settings: ScaleDownConfig, evidence: ScaleDownEvidence) -> _History:
    snapshots = sorted((s for s in evidence.snapshots if s.export.target == settings.target),
                       key=lambda s: s.collected_start_ms)
    if not snapshots:
        return _History((), (), (), ())
    source = (snapshots[0].export.source, snapshots[0].export.clock_id)
    selected = []
    conflicts = []
    events = {}
    inventories = {}
    inventory_sequences = {}
    invalid_inventory_ids = set()
    invalid_ids = set()
    sequences = {}
    watermark = -1
    for snapshot in snapshots:
        export = snapshot.export
        if (export.source, export.clock_id) != source:
            conflicts.append("Lifecycle source or clock identity changed.")
            continue
        if export.through_sequence < watermark:
            conflicts.append("Lifecycle sequence moved backwards.")
            continue
        watermark = export.through_sequence
        selected.append(snapshot)
        for inventory in export.inventories:
            if inventory.sequence > export.through_sequence:
                conflicts.append("Replica inventory is beyond the source watermark.")
                invalid_inventory_ids.add(inventory.event_id)
                continue
            previous = inventories.get(inventory.event_id)
            if previous is not None and previous != inventory:
                invalid_inventory_ids.add(inventory.event_id)
                conflicts.append("Conflicting replica inventory ID.")
            at_sequence = inventory_sequences.get(inventory.sequence)
            if at_sequence is not None and at_sequence.replicas != inventory.replicas:
                invalid_inventory_ids.update((inventory.event_id, at_sequence.event_id))
                conflicts.append("Conflicting replica inventory sequence.")
            inventories[inventory.event_id] = inventory
            inventory_sequences[inventory.sequence] = inventory
        for event in export.events:
            if event.sequence > export.through_sequence:
                conflicts.append("Lifecycle fact is beyond the source watermark.")
                invalid_ids.add(event.event_id)
                continue
            previous = events.get(event.event_id)
            if previous is not None and previous != event:
                invalid_ids.add(event.event_id)
                conflicts.append("Conflicting lifecycle event ID.")
            at_sequence = sequences.get(event.sequence)
            if at_sequence is not None and at_sequence != event.event_id:
                invalid_ids.update((event.event_id, at_sequence))
                conflicts.append("Conflicting lifecycle sequence.")
            events[event.event_id] = event
            sequences[event.sequence] = event.event_id
    ordered = sorted((e for key, e in events.items() if key not in invalid_ids), key=lambda e: e.sequence)
    if any(a.time_ms > b.time_ms for a, b in zip(ordered, ordered[1:])):
        conflicts.append("Lifecycle source times contradict sequence ordering.")
        ordered = []
    terminals = {}
    for event in ordered:
        if event.kind == "request_completed":
            terminals.setdefault((event.replica_id, event.request_id, event.attempt_id), []).append(event)
    for attempt in terminals.values():
        if len(attempt) > 1:
            conflicts.append("Conflicting terminal facts for one backend request attempt.")
            invalid_ids.update(event.event_id for event in attempt)
    ordered = [event for event in ordered if event.event_id not in invalid_ids]
    inventory_rows = sorted((i for key, i in inventories.items() if key not in invalid_inventory_ids),
                            key=lambda i: i.sequence)
    event_sequences = [event.sequence for event in ordered]
    inventory_times = {}
    for inventory in inventory_rows:
        low, high = inventory_times.get(inventory.sequence, (math.inf, -math.inf))
        inventory_times[inventory.sequence] = min(low, inventory.time_ms), max(high, inventory.time_ms)
    lower_max, upper_min = {}, {}
    maximum = -math.inf
    for sequence, (_, high) in inventory_times.items():
        lower_max[sequence], maximum = maximum, max(maximum, high)
    minimum = math.inf
    for sequence, (low, _) in reversed(inventory_times.items()):
        upper_min[sequence], minimum = minimum, min(minimum, low)
    for inventory in inventory_rows:
        next_index = bisect_right(event_sequences, inventory.sequence)
        if ((next_index and ordered[next_index - 1].time_ms > inventory.time_ms)
                or (next_index < len(ordered) and ordered[next_index].time_ms < inventory.time_ms)):
            invalid_inventory_ids.add(inventory.event_id)
            conflicts.append("Replica inventory source time contradicts lifecycle ordering.")
        if inventory.time_ms < lower_max[inventory.sequence] or inventory.time_ms > upper_min[inventory.sequence]:
            invalid_inventory_ids.add(inventory.event_id)
            conflicts.append("Replica inventory source times contradict sequence ordering.")
    return _History(tuple(selected), tuple(ordered),
                    tuple(i for i in inventory_rows if i.event_id not in invalid_inventory_ids),
                    tuple(dict.fromkeys(conflicts)))


def _bracket(event: ScaleDownLifecycleFact, history: _History) -> tuple[float, float] | None:
    """Bracket a new fact using source captures, never HTTP receipt timestamps."""
    before = None
    for snapshot in history.snapshots:
        if snapshot.export.through_sequence < event.sequence:
            if snapshot.capture_start_ms is not None:
                before = snapshot
            continue
        if any(e.event_id == event.event_id and e == event for e in snapshot.export.events):
            if (before is not None and snapshot.capture_end_ms is not None
                    and before.capture_end_ms <= snapshot.capture_start_ms):
                return before.capture_start_ms, snapshot.capture_end_ms
            return None
    return None


def _answer_started(record: RequestRecord, evidence: ScaleDownEvidence) -> float | None:
    observed = evidence.answer_started_ms.get(record.id)
    if (observed is None or record.timing_start_ms is None or not record.stream
            or record.answer_chars <= 0 or record.read_timestamps_truncated
            or not record.answer_read_timestamps_ms):
        return None
    # The parser may discover inline reasoning only when the closing tag arrives.
    # Its final answer reads can invalidate an earlier live arming candidate.
    return max(observed, record.timing_start_ms + record.answer_read_timestamps_ms[0])


def _budget_exhausted(record: RequestRecord) -> bool:
    return reasoning_budget(record) or (record.status == "completed" and not record.valid
        and record.finish_reason == "length" and record.error == REASONING_BUDGET_ERROR)


def _request_outcome(records: list[RequestRecord], limits: Limits, evidence: ScaleDownEvidence,
                     minimum: int, *, strict_errors: bool) -> tuple[list[str], list[str], dict]:
    violations, missing = [], []
    interrupted = [r for r in records if r.status == "cancelled"]
    measured = [r for r in records if r.status != "cancelled" and not _budget_exhausted(r)]
    errors = [r for r in measured if (r.status != "completed" or not r.valid
              or r.error or not r.stream_terminated or r.finish_reason not in {"stop", "length", "end_turn", "eos_token"})]
    if errors and (strict_errors or len(errors) / max(1, len(measured)) > limits.max_error_rate):
        violations.append("Original requests failed or did not end with a valid terminal response.")
    if interrupted:
        missing.append("Giraffe cancelled requests; those outcomes do not establish endpoint interruption.")
    if any(_budget_exhausted(r) for r in records):
        missing.append("Some responses exhausted Giraffe's output budget before an answer; they are not endpoint failures.")
    valid = [r for r in measured if r not in errors]
    if len(valid) < minimum:
        missing.append(f"Need {minimum} valid responses; observed {len(valid)}.")
    correctness = [r for r in valid if r.evaluation is None or r.evaluation.scorer != "none"]
    scored = [r for r in correctness if r.score is not None]
    unscored = len(correctness) - len(scored)
    if correctness and (sum(r.score is True for r in scored) + unscored) / len(correctness) < limits.min_correctness:
        violations.append("Configured answer correctness was not met.")
    if limits.min_correctness > 0 and len(scored) != len(correctness):
        missing.append("Some responses lack configured correctness evidence.")
    timing_records, raw_violations = [], []
    for record in valid:
        origin = record.scheduled_ms if record.scheduled_ms is not None else record.dispatch_ms
        answer = _answer_started(record, evidence)
        measured_record = record.model_copy(update={
            "first_output_ms": (answer - record.timing_start_ms if answer is not None and record.timing_start_ms is not None
                                else record.first_output_ms),
            "arrival_first_output_ms": (answer - origin if answer is not None and origin is not None
                                         else record.arrival_first_output_ms),
            "arrival_elapsed_ms": (record.completed_ms - origin if record.completed_ms is not None and origin is not None
                                    else record.arrival_elapsed_ms),
        })
        timing_records.append(measured_record)
        bad, unavailable = _timing(measured_record, limits)
        if bad:
            raw_violations.append(record.id)
        if unavailable:
            missing.append("Configured response timing lacks measurable evidence.")
    metrics = configured_metrics(limits)
    quality = qualify(timing_records, evidence.timing_health, evidence.timing_threshold_ms or 0, metrics)
    scoped = quality["scope"] == "request_intervals" and evidence.timing_threshold_ms is not None
    qualified = []
    if metrics and not scoped:
        missing.append("Scoped client scheduling evidence is unavailable for configured timing limits.")
    elif quality["affected_request_ids"]:
        missing.append("Overlapping client scheduling delay prevents qualifying some configured response timings.")
    if scoped:
        for record in timing_records:
            applicable = limits if record.stream_gap_applicable else limits.model_copy(update={"stream_gap_ms": None})
            if record.id in raw_violations and qualified_violations([record], applicable, quality, arrival=True):
                qualified.append(record.id)
    if qualified:
        violations.append("Configured response timing was not met.")
    details = {"timing_quality": quality, "qualified_timing_violations": qualified,
               "unattributed_timing_violations": [key for key in raw_violations if key not in qualified]}
    return list(dict.fromkeys(violations)), list(dict.fromkeys(missing)), details


def _result(title: str, violations: list[str], limitations: list[str], records=(), events=(), **metrics) -> ScaleDownSubresult:
    status = "fail" if violations else "inconclusive" if limitations else "pass"
    explanations = list(dict.fromkeys(violations + limitations))
    return ScaleDownSubresult(status=status, summary=" ".join(explanations) if explanations else title + " met the configured criteria.",
                             limitations=list(dict.fromkeys(limitations)), request_ids=[r.id for r in records],
                             evidence_ids=list(dict.fromkeys(e.event_id for e in events)), metrics=metrics)


def evaluate_scale_down(settings: ScaleDownConfig, evidence: ScaleDownEvidence,
                        records: list[RequestRecord], limits: Limits) -> CheckResult:
    """Return three independent verdicts; a proven failure outranks missing coverage."""
    history = _history(settings, evidence)
    shared = list(evidence.limitations)
    shared.extend(history.conflicts)
    marker = evidence.marker
    accepted_marker = (marker is not None and marker.target == settings.target and evidence.marker_accepted_ms is not None
                       and evidence.armed_ms is not None and evidence.marker_accepted_ms >= evidence.armed_ms)
    if not accepted_marker:
        shared.append("No accepted removal marker for the armed scenario.")
    initial = history.snapshots[0].export.through_sequence if history.snapshots else math.inf
    new_events = [e for e in history.events if e.sequence > initial and accepted_marker
                  and (marker.replica_id is None or e.replica_id == marker.replica_id)]
    removed_replicas = {e.replica_id for e in new_events if e.kind in {"drain_effective", "replica_terminated"}}
    replica_id = next(iter(removed_replicas)) if len(removed_replicas) == 1 else None
    selected = [e for e in history.events if e.replica_id == replica_id]
    exits = [e for e in selected if e.kind == "replica_terminated"]
    if exits:
        admitted_before_exit = {(e.request_id, e.attempt_id) for e in selected
                                if e.kind == "request_admitted" and e.sequence < exits[0].sequence}
        # Supervisors can report failed or cancelled in-flight attempts after exit.
        if any(e.sequence > exits[0].sequence and not (
                e.kind == "request_completed" and e.outcome in {"failed", "cancelled"}
                and (e.request_id, e.attempt_id) in admitted_before_exit) for e in selected):
            shared.append("Lifecycle facts follow process termination for the same replica incarnation.")
            new_events = []
    drains = [e for e in new_events if e.kind == "drain_effective" and e.replica_id == replica_id]
    drain = drains[0] if len(drains) == 1 else None
    terminations = [e for e in new_events if e.kind == "replica_terminated" and e.replica_id == replica_id]
    terminated = terminations[0] if len(terminations) == 1 else None
    # Abrupt removal need not emit a drain cutoff. Process termination still
    # establishes a transition for client continuity and surviving capacity.
    transition_fact = drain or terminated
    transition = _bracket(transition_fact, history) if transition_fact else None
    if transition is not None and evidence.armed_ms is not None and transition[1] < evidence.armed_ms:
        shared.append("The observed removal predates scenario arming and is excluded from its verdicts.")
        drain = terminated = transition_fact = replica_id = transition = None
        selected = []
    if transition_fact is None:
        shared.append("No unique new drain or process-termination fact identifies the removed replica incarnation.")
    if transition is None or evidence.armed_ms is None or transition[0] < evidence.armed_ms:
        shared.append("The confirmed transition lacks a bounded interval in the armed run.")
        transition = None
    removed_interval = _bracket(terminated, history) if terminated else None
    baseline_inventory = max((i for i in history.inventories if i.sequence < transition_fact.sequence),
                             key=lambda i: i.sequence, default=None) if transition_fact else None
    baseline_topology = (baseline_inventory is not None and len(baseline_inventory.replicas) >= 2
                         and replica_id in baseline_inventory.replicas)
    if transition_fact is not None and not baseline_topology:
        shared.append("Baseline inventory does not establish at least two replica incarnations.")
    by_id = {r.id: r for r in records if r.target == settings.target}
    baseline = [by_id[i] for i in dict.fromkeys(evidence.baseline_request_ids) if i in by_id
                and transition and by_id[i].completed_ms is not None and by_id[i].completed_ms <= transition[0]]
    baseline_violations, baseline_missing, _ = _request_outcome(baseline, limits, evidence, settings.min_baseline_samples, strict_errors=False)
    if transition is not None and (baseline_violations or baseline_missing):
        shared.append("Baseline service did not establish the configured successful samples.")
    admissions = [e for e in selected if e.kind == "request_admitted"]
    completions = {}
    for event in selected:
        if event.kind == "request_completed":
            completions.setdefault((event.request_id, event.attempt_id), []).append(event)

    def correlated(event, record):
        return event.request_id in {record.id, record.server_request_id} - {None}

    admitted_attempts = {(e.request_id, e.attempt_id) for e in admissions}
    orphan_completions = any(key not in admitted_attempts for key in completions)
    latest_coverage = history.snapshots[-1].export.coverage if history.snapshots else []
    coverage_conflict = any(c.replica_id == replica_id and c.start_sequence <= terminated.sequence
                           and (c.truncated or any(0 <= gap <= terminated.sequence for gap in c.gaps))
                           for c in latest_coverage) if terminated else False
    coverage = [c for c in latest_coverage if c.replica_id == replica_id and c.complete
                and not c.gaps and not c.truncated and c.start_sequence == 0
                and c.end_sequence >= terminated.sequence] if terminated else []
    giraffe_complete = not history.conflicts and not coverage_conflict and any(c.scope in {"all", "giraffe"} for c in coverage)
    complete = giraffe_complete and not orphan_completions and any(c.scope == "all" for c in coverage)

    def active_attempts(record):
        if transition_fact is None:
            return []
        active = []
        for admitted in admissions:
            if not correlated(admitted, record) or admitted.sequence >= transition_fact.sequence:
                continue
            done = completions.get((admitted.request_id, admitted.attempt_id), ())
            if not any(e.sequence <= transition_fact.sequence for e in done):
                active.append((admitted, done))
        return active

    def source_proves_overlap(record):
        return any(any(e.sequence > transition_fact.sequence for e in done)
                   or transition_fact == terminated and giraffe_complete for _, done in active_attempts(record))

    cohort = []
    if transition:
        for request_id in dict.fromkeys(evidence.long_request_ids):
            record = by_id.get(request_id)
            answer = _answer_started(record, evidence) if record else None
            if record is None or answer is None or answer > transition[0] or record.completed_ms is None:
                continue
            if record.completed_ms >= transition[1]:
                cohort.append(record)
            elif record.completed_ms >= transition[0] and record.status in {"failed", "timeout"} and source_proves_overlap(record):
                cohort.append(record)
    continuity_bad, continuity_missing, continuity_timing = _request_outcome(cohort, limits, evidence, settings.min_overlap_samples, strict_errors=True)
    continuity_missing = [*shared, *continuity_missing]
    if len(cohort) < settings.min_overlap_samples:
        continuity_missing.append("Too few real answer streams span the confirmed transition.")
    continuity = _result("Client-visible continuity", continuity_bad, continuity_missing, cohort, [transition_fact] if transition_fact else (),
                         transition_interval_ms=transition, **continuity_timing)

    drain_bad, drain_missing = [], list(shared)
    attribution_conflicts = set(evidence.attribution_conflict_request_ids)
    for record in cohort:
        if record.backend_id is None:
            continue
        record_admissions = [e for e in history.events if e.kind == "request_admitted" and correlated(e, record)]
        if record_admissions and not any(e.replica_id == record.backend_id for e in record_admissions):
            attribution_conflicts.add(record.id)
    attributed = [r for r in cohort if r.id not in attribution_conflicts and active_attempts(r)]
    if any(r.id in attribution_conflicts for r in cohort):
        drain_missing.append("Client and backend attribution conflict for an overlapping stream.")
    if coverage_conflict:
        drain_missing.append("Coverage claims for the removed replica conflict.")
    if orphan_completions:
        drain_missing.append("A backend completion has no matching admission on the removed incarnation; coverage is incomplete or conflicting.")
    if drain is None:
        drain_missing.append("The effective admission cutoff is unconfirmed.")
    if not attributed:
        drain_missing.append("No overlapping stream is attributed by backend admission evidence to the removed incarnation.")
    if terminated is None:
        drain_missing.append("Process termination is unconfirmed.")
    if not complete:
        drain_missing.append("Complete all-traffic admission and completion coverage through termination is unavailable.")
    if drain and any(e.sequence > drain.sequence for e in admissions):
        drain_bad.append("The removed replica admitted new work after effective draining.")
    if terminated:
        for admitted in admissions:
            done = completions.get((admitted.request_id, admitted.attempt_id), ())
            if any(admitted.sequence < e.sequence < transition_fact.sequence for e in done):
                continue
            finished = [e for e in done if admitted.sequence < e.sequence < terminated.sequence and e.outcome == "completed"]
            if not finished:
                if any(e.outcome == "cancelled" for e in done):
                    drain_missing.append("Some admitted work was cancelled by its client; drain completion is not established for it.")
                elif complete or any(e.sequence >= terminated.sequence or e.outcome == "failed" for e in done):
                    drain_bad.append("Admitted work was unfinished or unsuccessful when the replica terminated.")
                else:
                    drain_missing.append("Completion of admitted work is not established.")
        anchors = ([drain] if drain else []) if settings.grace_anchor == "drain_effective" else [
            e for e in selected if e.kind == "removal_requested" and e.sequence < transition_fact.sequence]
        if len(anchors) != 1:
            drain_missing.append("The declared grace-period anchor is unconfirmed.")
        elif terminated.time_ms - anchors[0].time_ms > settings.grace_seconds * 1000 + 1e-6:
            drain_bad.append("Replica termination exceeded the declared grace period.")
    backend = _result("Backend drain semantics", drain_bad, drain_missing, attributed, selected,
                      replica_id=replica_id, complete_coverage=complete,
                      grace_anchor=settings.grace_anchor, grace_seconds=settings.grace_seconds)

    capacity_missing = list(shared)
    survivors = any(i.sequence >= terminated.sequence and i.replicas and replica_id not in i.replicas
                    for i in history.inventories) if terminated else False
    if not survivors:
        capacity_missing.append("Surviving topology after process termination is unconfirmed.")
    if removed_interval is None:
        capacity_missing.append("Removal lacks a bounded interval in the runner clock.")
    # Include the cutoff's capture uncertainty: requests can fail after draining
    # starts but before the next poll confirms it. Stop at confirmed exit.
    draining = [by_id[i] for i in dict.fromkeys(evidence.followup_request_ids)
                if i in by_id and drain and transition and removed_interval
                and by_id[i].dispatch_ms is not None
                and transition[0] <= by_id[i].dispatch_ms < removed_interval[1]]
    drain_response_bad, drain_response_missing, drain_timing = _request_outcome(
        draining, limits, evidence, settings.min_post_samples, strict_errors=False)
    if drain is None or transition is None or removed_interval is None:
        drain_response_missing.append("The drain observation window is unconfirmed.")
    drain_window = _result("Drain-window requests", drain_response_bad, [*shared, *drain_response_missing],
                           draining, [drain, terminated] if drain and terminated else (),
                           start_ms=transition[0] if drain and transition else None,
                           end_ms=removed_interval[1] if removed_interval else None,
                           minimum_samples=settings.min_post_samples, measured_requests=len(draining), **drain_timing)
    post = [by_id[i] for i in dict.fromkeys(evidence.followup_request_ids) if i in by_id and removed_interval
            and by_id[i].dispatch_ms is not None and by_id[i].dispatch_ms >= removed_interval[1]
            and evidence.post_started_ms is not None and evidence.post_ended_ms is not None
            and evidence.post_started_ms <= by_id[i].dispatch_ms < evidence.post_ended_ms]
    capacity_bad, response_missing, capacity_timing = _request_outcome(post, limits, evidence, settings.min_post_samples, strict_errors=False)
    capacity_missing.extend(response_missing)
    expected = max(1, math.floor(settings.requests_per_second * settings.post_seconds))
    if (evidence.post_started_ms is None or evidence.post_ended_ms is None or removed_interval is None
            or evidence.post_started_ms < removed_interval[1]
            or evidence.post_ended_ms - evidence.post_started_ms + 1e-6 < settings.post_seconds * 1000
            or len(post) < expected or evidence.dropped_post_requests):
        capacity_missing.append("The configured post-removal offered load was not fully delivered.")
    post_window = _result("Post-exit requests", capacity_bad, capacity_missing, post, [terminated] if terminated else (),
                          start_ms=evidence.post_started_ms, end_ms=evidence.post_ended_ms,
                          minimum_samples=settings.min_post_samples, expected_requests=expected,
                          measured_requests=len(post), **capacity_timing)
    capacity = _result("Remaining capacity",
                       [*(f"Drain window: {value}" for value in drain_response_bad), *capacity_bad],
                       [*(f"Drain window: {value}" for value in drain_window.limitations), *capacity_missing],
                       draining + post, [e for e in (drain, terminated) if e],
                       expected_requests=expected, measured_requests=len(post), removed_interval_ms=removed_interval,
                       windows={"drain": drain_window.model_dump(mode="json"),
                                "post_removal": post_window.model_dump(mode="json")}, **capacity_timing)
    capacity.summary = (f"Drain-window requests: {drain_window.status}; post-exit requests: {post_window.status}. "
                        + capacity.summary)
    results = {"continuity": continuity, "drain": backend, "capacity": capacity}
    status = "fail" if any(r.status == "fail" for r in results.values()) else "inconclusive" if any(r.status == "inconclusive" for r in results.values()) else "pass"
    return CheckResult(id="scale_down", target=settings.target, title="Scale-down continuity", status=status,
                       summary=f"Continuity: {continuity.status}; drain: {backend.status}; capacity: {capacity.status}.",
                       metrics={"schema_version": 1, "results": {key: result.model_dump(mode="json") for key, result in results.items()}},
                       evidence_ids=list(dict.fromkeys([*(r.id for r in cohort + draining + post), *(e.event_id for e in selected)])))


class ScaleDownController:
    """Small run-loop-owned marker boundary shared by CLI and web adapters."""

    def __init__(self, run_id, target, *, run_start, stop, on_change=None):
        import uuid
        self.run_id, self.target, self.run_start = run_id, target, run_start
        self.nonce = uuid.uuid4().hex
        self.stop, self.on_change = stop, on_change
        self.phase = "baseline"
        self._ack = None

    def status(self):
        from copy import deepcopy
        return deepcopy(dict(run_id=self.run_id, target=self.target, nonce=self.nonce,
                             phase=self.phase, acknowledgment=self._ack))

    def set_phase(self, phase):
        if phase not in {"baseline", "waiting_for_output", "armed", "observing", "post_removal", "finalizing", "finished"}:
            raise ValueError("Unknown scale-down phase")
        self.phase = phase
        if self.on_change:
            self.on_change(self.status())

    def accept_marker(self, value):
        from copy import deepcopy
        from pydantic import ValidationError
        from .models import ScaleDownMarker
        received = max(0, (time.monotonic() - self.run_start) * 1000)
        ack = dict(accepted=False, event_id=None, reason=None, received_ms=received,
                   accepted_ms=None, marker=None)
        try:
            marker = ScaleDownMarker.model_validate(value).model_dump(mode="json")
        except (ValidationError, TypeError, ValueError):
            return {**ack, "reason": "Invalid scale-down marker."}
        ack.update(event_id=marker["event_id"], marker=marker)
        if self.stop.is_set() or self.phase in {"finalizing", "finished"}:
            ack["reason"] = "The scenario is stopping or finished."
        elif marker["nonce"] != self.nonce or marker["target"] != self.target:
            ack["reason"] = "Marker does not identify this active scenario."
        elif self._ack is not None:
            if self._ack["marker"] == marker:
                return deepcopy(self._ack)
            ack["reason"] = "A different marker has already been accepted."
        elif self.phase != "armed":
            ack["reason"] = "The scenario is not armed."
        else:
            ack.update(accepted=True, accepted_ms=max(received, (time.monotonic()-self.run_start)*1000))
            self._ack = deepcopy(ack)
            self.set_phase("observing")
        return deepcopy(ack)


def ingest_deployment(run, target, evidence):
    """Consume append-only collector histories once, without another poller."""
    observation = run.observations[target]
    states = getattr(run, "scale_down_ingestion", None)
    if states is None:
        states = run.scale_down_ingestion = {}
    state = states.setdefault(target, {"payloads": set(), "deployment_history": 0,
                                      "scale_down_deployment_history": 0})
    rows = [observation.get("deployment")]
    for name in ("deployment_history", "scale_down_deployment_history"):
        history = observation.get(name, [])
        rows.extend(history[state[name]:])
        state[name] = len(history)
        # The collector adds its sample-cap limitation to the final envelope.
        if history:
            rows.append(history[-1])
    changed = False

    def limit(message):
        nonlocal changed
        if message not in evidence.limitations and len(evidence.limitations) < MAX_SANITIZE_ITEMS:
            evidence.limitations.append(message)
            changed = True

    for limitation in observation.get("scale_down_limitations", []):
        limit(limitation)
    if observation.get("serving_telemetry", {}).get("sample_limit_reached"):
        limit("Lifecycle polling reached the configured sample limit.")
    for row in rows:
        if not row:
            continue
        for limitation in row.get("scale_down_limitations", []):
            limit(limitation)
        payload = row.get("scale_down_lifecycle")
        if payload and id(payload) not in state["payloads"]:
            state["payloads"].add(id(payload))
            try:
                snapshot = ScaleDownSnapshot.model_validate(payload)
            except ValueError:
                limit("Invalid collected lifecycle snapshot.")
                continue
            if len(evidence.snapshots) < MAX_SCALE_DOWN_SNAPSHOTS:
                evidence.snapshots.append(snapshot)
                changed = True
            else:
                limit("Lifecycle snapshot limit reached.")
    return changed


def resolve_fixtures(settings, fixtures):
    candidates = {spec.fixture_id: spec for values in fixtures.values() for spec in values}
    resolved = []
    for identifier in (settings.long_fixture, settings.followup_fixture):
        if identifier not in candidates:
            raise ValueError(f"scale_down fixture {identifier!r} was not found")
        spec = candidates[identifier]
        if (not spec.stream or spec.cancel_after_ms is not None or spec.cancel_after_chunks is not None
                or spec.tool_expectation is not None or spec.options.get("tools") or spec.options.get("tool_choice")):
            raise ValueError(f"scale_down fixture {identifier!r} must be an uninterrupted text stream")
        resolved.append(spec.model_copy(deep=True))
    return resolved


async def execute(run, target, client, fixtures, *, collector_task=None):
    """Hold an answer cohort while one shared arrival stage continues traffic."""
    import asyncio
    from .models import ScaleDownMarker, TrafficConfig
    from .traffic import run_stage

    settings = run.config.scale_down
    if target.name != settings.target:
        return
    long_fixture, followup_fixture = resolve_fixtures(settings, fixtures)
    evidence = ScaleDownEvidence()
    control = run.scale_down_control
    observation = run.observations[target.name]
    active_answers = set()
    backend_ids = {}
    tasks = []
    stage = None
    sending_done = False
    published_snapshots = 0
    begin = time.monotonic()
    deadline = min(run.deadline, begin + settings.max_duration_seconds)
    arm_deadline = min(deadline, begin + settings.baseline_timeout_seconds)

    def publish(status=None):
        nonlocal published_snapshots
        status = control.status() if status is None else status
        health = getattr(run, "timing_health", None)
        if health is not None:
            if status["phase"] == "finished":
                health.sample(time.monotonic())
            evidence.timing_health = health.snapshot()
            evidence.timing_threshold_ms = threshold([record for record in run.records if record.target == target.name])
        ack = status["acknowledgment"]
        if ack:
            evidence.marker = ScaleDownMarker.model_validate(ack["marker"])
            evidence.marker_accepted_ms = ack["accepted_ms"]
            evidence.marker_received_ms = ack["received_ms"]
        evidence.limitations = list(dict.fromkeys(evidence.limitations))[:MAX_SANITIZE_ITEMS]
        previous = observation.get("scale_down", {}).get("snapshots", [])
        new_snapshots = evidence.snapshots[published_snapshots:]
        observation["scale_down"] = {
            **evidence.model_dump(mode="json", exclude={"snapshots"}),
            "snapshots": [*previous, *[snapshot.model_dump(mode="json") for snapshot in new_snapshots]],
        }
        run.emit("scale_down_updated", target.name, "scale_down",
                 scale_down=evidence.model_dump(mode="json", exclude={"snapshots"}),
                 scale_down_snapshots=[snapshot.model_dump(mode="json") for snapshot in new_snapshots],
                 scale_down_control=status)
        published_snapshots = len(evidence.snapshots)

    control.on_change = publish
    def observer(role):
        def observed(event):
            if event.backend_id is not None:
                previous = backend_ids.setdefault(event.request_id, event.backend_id)
                if (previous != event.backend_id and event.request_id not in evidence.attribution_conflict_request_ids
                        and len(evidence.attribution_conflict_request_ids) < MAX_SIMULTANEOUS_REQUESTS + MAX_PLANNED_ARRIVALS):
                    evidence.attribution_conflict_request_ids.append(event.request_id)
            if event.kind == "started":
                getattr(evidence, role + "_request_ids").append(event.request_id)
            elif event.kind == "answer" and event.stream:
                evidence.answer_started_ms.setdefault(event.request_id, max(0, (event.monotonic_seconds-run.start)*1000))
                if role == "long":
                    active_answers.add(event.request_id)
            elif event.kind == "terminal":
                active_answers.discard(event.request_id)
        return observed

    def request_spec(fixture, role):
        return fixture.model_copy(deep=True, update=dict(scenario="scale_down_" + role,
            condition_id="scale_down_" + role, check_ids=["scale_down"],
            workload={**fixture.workload, "scale_down_role": role}))

    async def pause():
        if ingest_deployment(run, target.name, evidence):
            publish()
        await asyncio.sleep(.01)

    def reason():
        return run.stop_reason(target.name)

    try:
        ingest_deployment(run, target.name, evidence)
        publish()
        baseline = []
        for _ in range(settings.min_baseline_samples):
            if reason() or time.monotonic() >= arm_deadline:
                break
            record = await run.request(target, client, request_spec(followup_fixture, "baseline"),
                                       deadline_override=arm_deadline, lifecycle_observer=observer("baseline"))
            if record:
                baseline.append(record)
        failures, missing, _ = _request_outcome(baseline, run.config.limits, evidence,
                                             settings.min_baseline_samples, strict_errors=False)
        if failures or missing:
            evidence.limitations.append("Successful baseline samples were not established before the arming deadline.")
            return
        control.set_phase("waiting_for_output")
        for _ in range(settings.long_cohort_size):
            tasks.append(asyncio.create_task(run.request(target, client, request_spec(long_fixture, "long"),
                lifecycle_observer=observer("long"))))
        traffic = TrafficConfig(duration_seconds=max(.001, begin + settings.max_sending_seconds-time.monotonic()),
            rates=[settings.requests_per_second], max_in_flight=min(settings.concurrency, run.slots.maxsize)-settings.long_cohort_size,
            drain_timeout_seconds=settings.completion_seconds)

        async def send(spec, finalize_record):
            return await run.request(target, client, spec, slot_reserved=True, budget_reserved=True,
                lifecycle_observer=observer("followup"), finalize_record=finalize_record)

        def dropped():
            run.drop_offer(target.name, "scale_down_followup")
            if control.phase == "post_removal":
                evidence.dropped_post_requests += 1

        stage = asyncio.create_task(run_stage(traffic=traffic, rate_rps=settings.requests_per_second,
            stage_index=0, fixtures={}, execute=send,
            reserve_offer=lambda: not sending_done and run.reserve_offer(target.name),
            try_acquire=run.try_acquire_slot, release=run.release_slot,
            stop_reason=lambda: None if reason() == "maximum request budget reached" else reason(),
            deadline=deadline, limits=run.config.limits,
            spec_factory=lambda index: request_spec(followup_fixture, "followup"),
            check_id="scale_down", scenario="scale_down_followup", run_start=run.start,
            return_evidence=True, on_drop=dropped,
            schedule_observations=observation.setdefault("schedule_transitions", [])))
        while time.monotonic() < arm_deadline and not reason():
            if len(active_answers) >= settings.min_overlap_samples:
                evidence.armed_ms = (time.monotonic()-run.start)*1000
                control.set_phase("armed")
                break
            if all(task.done() for task in tasks):
                break
            await pause()
        if control.phase != "armed" and control.phase != "observing":
            evidence.limitations.append("No sufficient active long-answer cohort before the arming deadline.")
            return
        event_deadline = min(deadline, time.monotonic()+settings.event_timeout_seconds)
        while time.monotonic() < event_deadline and not reason() and evidence.marker is None:
            await pause()
        if evidence.marker is None:
            evidence.limitations.append("No accepted external removal marker before the event deadline.")
            return
        removal_deadline = min(deadline, time.monotonic()+settings.grace_seconds)
        checked_snapshots = -1
        while time.monotonic() < removal_deadline and not reason():
            await pause()
            if len(evidence.snapshots) == checked_snapshots:
                continue
            checked_snapshots = len(evidence.snapshots)
            history = _history(settings, evidence)
            initial = history.snapshots[0].export.through_sequence if history.snapshots else math.inf
            removed = [event for event in history.events if event.kind == "replica_terminated"
                       and event.sequence > initial and (evidence.marker.replica_id is None
                       or event.replica_id == evidence.marker.replica_id)]
            if len(removed) == 1 and not history.conflicts and _bracket(removed[0], history):
                evidence.post_started_ms = (time.monotonic()-run.start)*1000
                control.set_phase("post_removal")
                post_deadline = min(deadline, time.monotonic()+settings.post_seconds)
                while time.monotonic() < post_deadline and not reason():
                    await pause()
                evidence.post_ended_ms = (time.monotonic()-run.start)*1000
                break
        if evidence.post_started_ms is None:
            evidence.limitations.append("Replica termination was not confirmed within the observation allowance.")
    finally:
        sending_done = True
        evidence.stop_reason = reason()
        control.set_phase("finalizing")
        pending = [*tasks, *([stage] if stage else [])]
        try:
            finish = min(deadline, time.monotonic()+settings.completion_seconds)
            while any(not task.done() for task in pending) and time.monotonic() < finish:
                stop_reason = reason()
                if stop_reason and stop_reason != "maximum request budget reached":
                    break
                await pause()
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            # Completion can fall between collector polls. Give the same live
            # sampler one final observation within the existing cleanup window.
            history = observation.get("scale_down_deployment_history", [])
            collected = len(history)
            while (target.deployment.collector_url and collector_task is not None
                   and not collector_task.done() and time.monotonic() < finish
                   and not observation["serving_telemetry"].get("sample_limit_reached")
                   and collected < min(run.config.metrics_max_samples, MAX_SCALE_DOWN_SNAPSHOTS)
                   and len(observation.get("scale_down_deployment_history", [])) == collected):
                stop_reason = reason()
                if stop_reason and stop_reason != "maximum request budget reached":
                    break
                await pause()
            ingest_deployment(run, target.name, evidence)
            if any(record.status == "cancelled" and record.id in evidence.long_request_ids for record in run.records):
                evidence.limitations.append("The client completion allowance or a run limit interrupted active work.")
            control.set_phase("finished")


def result(run, target):
    settings = run.config.scale_down
    if not scale_down_enabled(target, run.config):
        return CheckResult(id="scale_down", title="Scale-down continuity", target=target.name,
                           required=False, status="skipped", summary="Not selected for this target.")
    # Execution captures source history through its final cleanup boundary.
    # Later suite work may change that history; only request scoring remains live.
    saved = run.observations[target.name].get("scale_down")
    evidence = (ScaleDownEvidence.model_validate(saved) if saved is not None else
                ScaleDownEvidence(limitations=["Scale-down traffic did not start."]))
    run.observations[target.name]["scale_down"] = evidence.model_dump(mode="json")
    return evaluate_scale_down(settings, evidence, run.records, run.config.limits)
