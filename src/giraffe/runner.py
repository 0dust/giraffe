"""Bounded, shared-traffic acceptance suite for an existing inference endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import platform
import socket
import time
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


from .client import LLMClient, no_response_failure
from .conditions import condition_aggregate, condition_for, request_status
from .deployment import fixture_hash
from .fixtures import (FIXTURE_VERSION, SUITE_VERSION, builtin_fixtures, load_custom_fixtures,
                       score_response, traffic_fixtures, deterministic_options, reasoning_budget)
from .overlap import NEEDS_STREAM, max_evidenced_overlap, scheduling_delay_windows
from .request_pack import pack_identity, pack_preview, pack_specs
from .resources import PhaseSampler, SAMPLING, peak_rss_bytes
from .timing import TimingHealth, configured_metrics, exceeded_unfinished, qualified_violations, run_quality
from .models import (
    INTERNAL_CHECK_NAMES as CHECK_NAMES, CHECK_NAMES as PUBLIC_CHECK_NAMES,
    CORE_CHECKS, CheckResult, Limits, RequestRecord, RequestSpec, RunConfig, RunReport, Target,
    overall_status, required_concurrency,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower, upper = math.floor(index), math.ceil(index)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3)


def _reasoning_budget(record) -> bool:
    """No answer because reasoning used Giraffe's own output budget."""
    return reasoning_budget(record)


def _stats(records: list[RequestRecord]) -> dict:
    """attempted = completed + failed + timed_out + cancelled + reasoning_budget_exhausted.

    Reasoning-budget records are neither errors nor answers; they are excluded from error_rate.
    """
    good = [r for r in records if r.status == "completed" and r.valid]
    latencies = [r.elapsed_ms for r in good]
    output = [r.first_output_ms for r in good if r.first_output_ms is not None]
    evaluated = [r for r in records if r.status != "cancelled" and not _reasoning_budget(r)]
    errors = sum(r.status in {"failed", "timeout"} or (r.status == "completed" and not r.valid) for r in evaluated)
    rates = [r.output_tokens / (r.elapsed_ms / 1000) for r in good
             if r.output_tokens is not None and r.elapsed_ms > 0]
    generation_rates = [rate for r in good if (rate := r.generation_tokens_per_second) is not None]
    result = {
        "attempted": len(records), "completed": len(good),
        "failed": sum((r.status == "failed" or (r.status == "completed" and not r.valid))
                      and not _reasoning_budget(r) for r in records),
        "reasoning_budget_exhausted": sum(map(_reasoning_budget, records)),
        "timed_out": sum(r.status == "timeout" for r in records),
        "cancelled": sum(r.status == "cancelled" for r in records),
        "error_rate": errors / len(evaluated) if evaluated else None,
        "p50_ms": _percentile(latencies, .5), "p95_ms": _percentile(latencies, .95),
        "first_output_p50_ms": _percentile(output, .5),
        "first_output_p95_ms": _percentile(output, .95),
        "max_stream_gap_ms": max((r.max_stream_gap_ms for r in good
                                   if r.max_stream_gap_ms is not None), default=None),
        "output_tokens_per_second_p50": _percentile(rates, .5),
        "output_tokens_per_second_min": min(rates, default=None),
        "generation_tokens_per_second_p50": _percentile(generation_rates, .5),
        "generation_tokens_per_second_min": min(generation_rates, default=None),
        "generation_rate_samples": len(generation_rates),
        "output_tokens": (None if any(r.load_point and r.output_tokens is None for r in good)
                          else sum(r.output_tokens or 0 for r in good)),
        "output_chars": sum(r.output_chars for r in good),
        "scored": sum(r.score is not None for r in records),
        "correct": sum(r.score is True for r in records),
        "actual_input_tokens": [r.input_tokens for r in records if r.input_tokens is not None],
        "unknown_input_length": sum(r.input_tokens is None for r in records),
        "actual_output_tokens": [r.output_tokens for r in records if r.output_tokens is not None],
        "unknown_output_length": sum(r.output_tokens is None for r in records),
        "finish_reasons": dict(Counter(r.finish_reason or "unknown" for r in records)),
        "measurement_window_ms": max((r.completed_ms or 0 for r in records), default=0)-min((r.dispatch_ms or 0 for r in records), default=0),
    }

    window = result["measurement_window_ms"]/1000
    result["completed_requests_per_second"] = result["completed"]/window if window>0 else None
    result["aggregate_output_tokens_per_second"] = result["output_tokens"]/window if window>0 and not result["unknown_output_length"] else None
    return result


def _stress(scenario):
    return (scenario.startswith(("traffic_stage_", "context", "fairness_", "bucket_", "mixed_", "arrival", "stress_", "load_"))
            or scenario in {"sustained", "recovery", "recovery_baseline", "recovery_prime", "limits",
                            "stop_control", "client_cancel", "client_deadline", "backend_cancel",
                            "cancellation_probe", "cancellation_baseline"})


def _context_rejection(record):
    return record.http_status in {400, 413, 422} and any(word in (record.error or "").lower()
        for word in ("context length", "context_length", "maximum context", "too many tokens",
                     "context size", "context window", "max_model_len"))


def _serving_failure(record, scenario):
    if (scenario in {"sustained", "performance_evidence_probe"} or scenario.startswith("traffic_stage_") or record.status == "cancelled"
            or _reasoning_budget(record)):
        return False
    if (scenario.startswith("load_calibration_") and record.input_tokens is not None
            and record.status == "failed" and (record.http_status is None or record.http_status < 400)
            and record.error == "response contained no useful answer output"):
        return False
    if _stress(scenario) and (record.status == "timeout" or _context_rejection(record)):
        return False
    return record.status != "completed" or not record.valid


def _request_failed(record):
    if (record.intentional or record.scenario in {"client_cancel", "client_deadline", "deadline_probe"}
            or "intentional cancellation" in (record.error or "").lower()):
        return False
    return record.status in {"failed", "timeout"} or (
        record.status == "completed" and (not record.valid or record.score is False))


def _spec(spec: RequestSpec, scenario: str, checks: list[str] = ()) -> RequestSpec:
    ids = list(dict.fromkeys([*spec.check_ids, *checks]))
    if _stress(scenario):
        ids = [name for name in ids if name not in {"access", "serving", "first_output", "generation"}]
    else:
        ids = list(dict.fromkeys([*ids, "access", "serving", "first_output", "generation"]))
    return spec.model_copy(update={"scenario": scenario, "check_ids": ids})


def _scenario_checks(spec):
    if spec.scenario in {"initial", "readiness", "warm", "generation"}:
        return spec.check_ids
    return [name for name in spec.check_ids if name not in {"access", "serving", "first_output", "generation"}]


def _check_config(config: RunConfig, check_id: str) -> RunConfig:
    effective = config.effective_check(check_id)
    return config.model_copy(update={
        key: Limits.model_validate(value) if key == "limits" else value
        for key, value in effective.items() if key in RunConfig.model_fields
    })


class _Peak:
    __slots__ = ("parent", "rank", "value", "live")

    def __init__(self, value):
        self.parent = self
        self.rank = 0
        self.value = value
        self.live = 1

    def root(self):
        node = self
        while node.parent is not node:
            node.parent = node.parent.parent
            node = node.parent
        return node


class _InflightPeaks:
    """Share lifetime peaks between requests instead of updating every request.

    Before a start at N, every live peak is at least N-1. Only that one
    cohort can change. Union by rank and path compression keep end lookups
    bounded (the number of distinct live cohorts cannot exceed the admitted
    concurrency); starts do constant work, without iterating active requests.
    """

    def __init__(self):
        self.counts = Counter()
        self.peaks = {}
        self.total = 0

    def __len__(self):
        return self.total

    def start(self, target):
        self.total += 1
        count = self.counts[target] + 1
        self.counts[target] = count
        peaks = self.peaks.setdefault(target, {})
        previous = peaks.pop(count - 1, None)
        current = peaks.get(count)
        token = _Peak(count)
        if current is None:
            current = token
        else:
            token.parent = current
            current.live += 1
        if previous is not None:
            if current.rank < previous.rank:
                current, previous = previous, current
            previous.parent = current
            current.live += previous.live
            current.rank += current.rank == previous.rank
        current.value = count
        peaks[count] = current
        return token

    def finish(self, target, token):
        root = token.root()
        peak = root.value
        root.live -= 1
        if not root.live:
            del self.peaks[target][peak]
        self.total -= 1
        self.counts[target] -= 1
        if not self.counts[target]:
            del self.counts[target]
            del self.peaks[target]
        return peak


class _Run:
    def __init__(self, config: RunConfig, progress, stop_event):
        self.config = config
        self.pack_specs = pack_specs(config)
        self.stage_evidence = defaultdict(list)
        self.progress = progress
        self.stop = stop_event or asyncio.Event()
        self.start = time.monotonic()
        self.deadline = self.start + config.max_duration_seconds if config.max_duration_seconds is not None else math.inf
        peak = required_concurrency(config)
        pool_size = min(peak["peak"], config.concurrency) if config.concurrency is not None else peak["peak"]
        self.ceiling_constrains_pool = config.concurrency is not None and peak["peak"] > config.concurrency
        self.max_connections = min(peak["per_target_peak"], pool_size)
        self.connection_limits = {target.name: self.max_connections for target in config.targets}
        self.slots = asyncio.Queue(maxsize=pool_size)
        for _ in range(pool_size):
            self.slots.put_nowait(None)
        self.records: list[RequestRecord] = []
        from giraffe.logprobs import LogprobBudget
        self.logprob_budget = LogprobBudget()
        self.attempted = 0
        self.budget_used = 0
        self.dropped_offers = 0
        self.budget_counts = Counter()
        self.errors = 0
        self.reason: str | None = None
        self.limits_reached = set()
        self.memory_limit_reached = False
        self.memory_stop_reason = None
        self.memory_measured = False
        self.memory_sampling_done = False
        self.rss_at_start_bytes = None
        self.memory_interrupted_checks = set()
        self.pending_checks = {(target.name, check) for target in config.targets for check in config.checks}
        self.judging_pending = any(spec.scorer == "judge" for spec in self.pack_specs)
        self.counts = Counter()
        if config.max_requests is None:
            self.quotas = {}
        else:
            base, extra = divmod(config.max_requests, len(config.targets))
            self.quotas = {t.name: base + (i < extra) for i, t in enumerate(config.targets)}
        self.unfinished = defaultdict(set)
        self.observations = {t.name: {
                                     "restart": None, "metrics": [], "capacity": []}
                             for t in config.targets}
        self.active = Counter()
        self.peak = Counter()
        self.max_lag_ms = 0.0
        self.timing_health = TimingHealth(self.start)
        self.inflight = _InflightPeaks()
        self.conditions = {}
        from .conditions import planned_conditions
        self.planned_condition_ids = {(row["target"], row["condition_id"]) for row in planned_conditions(config)}
        from .planning import request_plan
        # Optional evidence probes must not displace finite planned work,
        # including bounded tool conversations that are excluded from "known".
        self.planned_request_reserve_per_target = sum(
            row["requests_per_target"] or row.get("known_scheduled_requests", 0)
            for row in request_plan(config)["rows"])
        self.endpoint_health = {t.name: {"first_attempt_seconds": None, "last_response_seconds": None,
            "consecutive_failures": 0, "failure_request_ids": [], "abort_reason": None} for t in config.targets}
        self.target_tasks = {}
        self.stop_notified = False
        for target, observation in self.observations.items():
            observation["endpoint_health"] = self.endpoint_health[target]
        for observation in self.observations.values():
            observation.update(conditions=[], deployment=None, deployment_history=[], phase_boundaries=[],
                serving_telemetry={"snapshots": [], "sample_limit_reached": False,
                    "sampling_interval_seconds": config.metrics_interval_seconds,
                    "timeout_seconds": config.metrics_timeout_seconds,
                    "max_samples": config.metrics_max_samples,
                    "max_series": config.metrics_max_series, "collection_gaps_seconds": []})

    def _condition(self, target, scenario, workload=None, *, request_parameters=None):
        condition = condition_for(scenario, workload or {}, self.config, request_parameters=request_parameters)
        key = (target, condition["id"])
        row = self.conditions.get(key)
        if row is None:
            if len(self.observations[target]["conditions"]) >= 1024:
                return None
            row = {"target": target, "condition": condition, "planned": None, "planned_condition": key in self.planned_condition_ids,
                   "planned_kind": "exact", "status": "waiting", "sent": 0,
                   "started": 0, "running": 0, "completed": 0, "failed": 0,
                   "not_sent": None, "reason": None, "request_statuses": {}}
            self.conditions[key] = row
            self.observations[target]["conditions"].append(row)
        else:
            previous = row["condition"]["parameters"]
            for name, value in condition["parameters"].items():
                if name in {"input_chars", "input_tokens_approx", "input_tokens_target", "output_tokens"} and name in previous:
                    values = previous[name] if isinstance(previous[name], list) else [previous[name]]
                    low, high = min(*values, value), max(*values, value)
                    previous[name] = low if low == high else [low, high]
                else:
                    previous[name] = value
            row["condition"].update(label=condition["label"], purpose=condition["purpose"])
        return row

    def plan_condition(self, target, scenario, workload=None, planned=None, planned_kind="exact"):
        row = self._condition(target, scenario, workload)
        if row is not None:
            row.update(planned=planned, planned_kind=planned_kind, planned_condition=True,
                       not_sent=max(0, planned-row["sent"]) if planned is not None else None)
            self.emit_condition("condition_planned", scenario, row)
        return row

    def start_condition(self, target, scenario, workload=None, planned=None, planned_kind="exact", *, request_parameters=None):
        row = self._condition(target, scenario, workload, request_parameters=request_parameters)
        if row is None:
            return None
        previous = row["status"]
        # An upfront plan already includes this dispatch segment. Repeated
        # unplanned groups (e.g. context calibration rounds) extend their total.
        if planned is not None and (previous != "waiting" or row["planned"] is None):
            row["planned"] = (row["planned"] or 0) + planned
        if row["planned"] is not None:
            row["not_sent"] = max(0, row["planned"] - row["sent"])
        if planned_kind in {"upper_bound", "duration"}:
            row["planned_kind"] = planned_kind
        row.update(status="running", reason=None)
        if previous != "running":
            self.emit_condition("condition_started", scenario, row)
        return row

    def emit_condition(self, event, scenario, row):
        # Keep the global completed progress counter separate from condition totals.
        aggregate = condition_aggregate(row)
        self.emit(event, row["target"], scenario, condition_summary={**row, "aggregate_status": aggregate["status"],
                  **{key: aggregate[key] for key in ("condition_id", "check_id", "name", "concurrency_description", "not_started", "reasons")}},
                  **{k: v for k, v in row.items() if k not in {"target", "completed"}})

    def condition_reason(self, target, fallback="check stopped early"):
        if self.stop.is_set() or self.reason == "cancelled by user":
            return "run stopped"
        if time.monotonic() >= self.deadline or self.reason == "maximum run duration reached":
            return "time limit reached"
        if self.remaining(target) == 0 or self.reason == "maximum request budget reached":
            return "request budget reached"
        return fallback

    def finish_condition(self, target, scenario, workload=None, reason=None, load_point_result=None):
        key = (target, condition_for(scenario, workload or {}, self.config)["id"])
        row = self.conditions.get(key)
        if row is None:
            return
        row["not_sent"] = max(0, row["planned"] - row["sent"]) if row["planned"] is not None else None
        reason = reason or row["reason"]
        row["reason"] = reason or (self.condition_reason(target) if row["not_sent"] else None)
        row["status"] = "finished"
        if load_point_result is not None:
            row["load_point_result"] = dict(load_point_result)
        self.emit_condition("condition_finished", scenario, row)

    def emit(self, event: str, target: str = "", scenario: str = "", **extra):
        if target and event in {"scenario_started", "scenario_finished"}:
            boundaries = self.observations[target]["phase_boundaries"]
            if len(boundaries)<512:
                boundaries.append({"event": event, "phase": scenario,
                                   "elapsed_seconds": time.monotonic()-self.start})
        if self.progress:
            self.progress({"event": event, "target": target, "scenario": scenario,
                           "completed": len(self.records), "attempted": self.attempted,
                           "budget_used": self.budget_used,
                           "dropped_offers": self.dropped_offers,
                           "max_requests": self.config.max_requests,
                           "elapsed_seconds": round(time.monotonic() - self.start, 3), **extra})

    def preparation(self, target, scenario, step, status, reason=None, **metadata):
        """Publish preparation independently from planned workload execution."""
        now = time.monotonic() - self.start
        previous = self.observations[target].get("preparation", {})
        continuing = previous.get("status") == "running" and previous.get("scenario") == scenario
        started = previous["started_elapsed_seconds"] if continuing else now
        value = {**metadata, "scenario": scenario, "step": step, "status": status,
                 "started_elapsed_seconds": started, "elapsed_seconds": max(0, now - started),
                 "reason": reason}
        self.observations[target]["preparation"] = value
        history = self.observations[target].setdefault("preparations", {})
        history.pop(scenario, None)
        history[scenario] = value
        while len(history) > 64:
            history.pop(next(iter(history)))
        event = ("preparation_progress" if continuing else "preparation_started") if status == "running" else "preparation_finished"
        self.emit(event, target, scenario, preparation=value)

    def mark_unfinished(self, target, checks):
        self.unfinished[target].update(check for check in checks if not self.memory_limit_reached
                                      or (target, check) in self.memory_interrupted_checks)

    def finish_checks(self, target, checks):
        for check in checks:
            if not (self.judging_pending and check in {"correctness", "capacity", "load"}):
                self.pending_checks.discard((target, check))

    def memory_sample(self, rss, unfinished=None):
        if self.memory_sampling_done:
            return
        self.memory_measured |= rss is not None
        limit = self.config.max_memory_mb * 1024**2
        if rss is None or self.rss_at_start_bytes is None or not limit or self.memory_limit_reached:
            return
        growth = max(0, rss - self.rss_at_start_bytes)
        if growth <= limit:
            return
        self.memory_limit_reached = True
        self.memory_interrupted_checks.update(self.pending_checks if unfinished is None else unfinished)
        if not self.memory_interrupted_checks:
            return
        self.stop_reason()
        if self.reason in {None, "maximum request budget reached"}:
            self.memory_stop_reason = (f"memory limit reached: RAM grew by {math.ceil(growth / 1024**2):,} MiB, "
                                       f"over the {self.config.max_memory_mb:,} MiB limit")
            self.reason = self.memory_stop_reason
        for target, check in self.memory_interrupted_checks:
            self.unfinished[target].add(check)
        self.stop_reason()

    def stop_reason(self, target: str | None = None) -> str | None:
        previous = self.reason
        # A full offer budget must not hide a real stop during the drain.
        if self.reason == "maximum request budget reached":
            self.reason = None
        if self.stop.is_set():
            self.reason = self.reason or "cancelled by user"
        if time.monotonic() >= self.deadline:
            self.limits_reached.add("max_duration_seconds")
            self.reason = self.reason or "maximum run duration reached"
        for name, health in self.endpoint_health.items():
            since = health["last_response_seconds"]
            if since is None:
                since = health["first_attempt_seconds"]
            silence = time.monotonic() - self.start - since if since is not None else 0
            if (self.config.unresponsive_after_failures and not health["abort_reason"]
                    and health["consecutive_failures"] >= self.config.unresponsive_after_failures
                    and silence >= self.config.unresponsive_for_seconds):
                health["abort_reason"] = (f"Endpoint {name} unresponsive: no response for {silence:.3g}s "
                    f"after {health['consecutive_failures']} consecutive failed requests")
                self.unfinished[name].update(self.config.checks)
                self.emit("target_stopping", name, abort_reason=health["abort_reason"])
        if all(health["abort_reason"] for health in self.endpoint_health.values()):
            self.reason = self.reason or "; ".join(health["abort_reason"] for health in self.endpoint_health.values())
        if self.errors >= self.config.stop_after_errors:
            self.reason = self.reason or "error threshold reached"
        self.reason = self.reason or previous
        if self.reason and self.reason != "maximum request budget reached" and not self.stop_notified:
            self.stop_notified = True
            self.emit("run_stopping", abort_reason=self.reason)
        return (self.endpoint_health[target]["abort_reason"] if target else None) or self.reason

    def endpoint_attempt(self, target):
        health = self.endpoint_health[target]
        if health["first_attempt_seconds"] is None:
            health["first_attempt_seconds"] = time.monotonic() - self.start

    def endpoint_response(self, target):
        health = self.endpoint_health[target]
        if health["first_attempt_seconds"] is None or health["abort_reason"]:
            return
        health.update(last_response_seconds=time.monotonic() - self.start,
                      consecutive_failures=0, failure_request_ids=[])

    def endpoint_failure(self, target, record_id=None):
        health = self.endpoint_health[target]
        if not self.config.unresponsive_after_failures or health["abort_reason"]:
            return
        health["consecutive_failures"] += 1
        if record_id is not None:
            health["failure_request_ids"].append(record_id)
        self.stop_reason()

    def remaining(self, target: str) -> int | None:
        if self.config.max_requests is None:
            return None
        return max(0, min(self.config.max_requests - self.budget_used,
                          self.quotas[target] - self.budget_counts[target]))

    def allowed(self, target: str) -> bool:
        reason = self.stop_reason(target)
        remaining = self.remaining(target)
        if remaining == 0:
            self.limits_reached.add("max_requests")
        if self.config.max_requests is not None and self.budget_used >= self.config.max_requests:
            self.reason = self.reason or "maximum request budget reached"
        return not reason and not self.reason and (remaining is None or remaining > 0)

    def reserve_offer(self, target: str) -> bool:
        if not self.allowed(target):
            return False
        self.budget_used += 1
        self.budget_counts[target] += 1
        return True

    def drop_offer(self, target: str, scenario: str):
        self.dropped_offers += 1
        self.emit("offer_dropped", target, scenario)

    def try_acquire_slot(self) -> bool:
        try:
            self.slots.get_nowait()
            return True
        except asyncio.QueueEmpty:
            if self.ceiling_constrains_pool:
                self.limits_reached.add("concurrency")
            return False

    def release_slot(self) -> None:
        self.slots.put_nowait(None)

    @asynccontextmanager
    async def slot(self, reserved: bool, deadline: float | None = None):
        if not reserved:
            if self.slots.empty() and self.ceiling_constrains_pool:
                self.limits_reached.add("concurrency")
            if deadline is None:
                await self.slots.get()
            else:
                try:
                    async with asyncio.timeout_at(deadline):
                        await self.slots.get()
                except TimeoutError:
                    yield False
                    return
        try:
            yield True
        finally:
            if not reserved:
                self.release_slot()

    async def request(self, target, client, spec, event=None, on_start=None, timeout_override=None,
                      *, slot_reserved=False, budget_reserved=False, deadline_override=None, budget_exempt=False, evaluate_record=None, request_parameters=None):
        check_ids = [name for name in spec.check_ids if name in self.config.checks
                     and (not _stress(spec.scenario) or name not in {"access", "serving", "first_output", "generation"})
                     and (name != "generation" or spec.scenario == "generation")
                     and (name != "capacity" or spec.scenario.startswith("traffic_stage_"))]
        spec = spec.model_copy(update={"check_ids": check_ids})
        readiness = spec.scenario == "readiness" or (
            spec.scenario == "initial" and self.config.restart_target == target.name)
        evidence_probe = spec.scenario == "performance_evidence_probe"
        slot_deadline = min(self.deadline, deadline_override if deadline_override is not None else self.deadline)
        async with self.slot(slot_reserved, slot_deadline) as admitted:
            # An offered arrival owns its budget even if a later arrival exhausts
            # the ceiling. Only a real stop condition can prevent its dispatch.
            reason = self.stop_reason(target.name)
            stopped = not admitted or time.monotonic() >= slot_deadline or (
                reason is not None and reason != "maximum request budget reached")
            if stopped or (not budget_reserved and not budget_exempt and not self.reserve_offer(target.name)):
                self.mark_unfinished(target.name, _scenario_checks(spec))
                return None
            self.attempted += 1
            output_cap = spec.max_tokens if spec.max_tokens is not None else self.config.max_output_tokens
            request_parameters = {**(request_parameters or {}),
                "input_chars": spec.input_chars or sum(len(str(message.get("content") or "")) for message in spec.messages),
                "request_timeout_seconds": spec.request_timeout_seconds or self.config.request_timeout_seconds,
                "output_tokens": output_cap, "requested_input_tokens": spec.requested_input_tokens}
            condition_scenario = spec.condition_id or spec.scenario
            condition = self._condition(target.name, condition_scenario, spec.workload, request_parameters=request_parameters)
            condition_id = (condition["condition"]["id"] if condition is not None else
                            condition_for(condition_scenario, spec.workload, self.config)["id"])
            if condition is None or condition["status"] != "running":
                condition = self.start_condition(target.name, condition_scenario, spec.workload, request_parameters=request_parameters)
            if condition is not None:
                condition["sent"] += 1
                condition["started"] += 1
                condition["running"] += 1
                if condition["planned"] is not None:
                    condition["not_sent"] = max(0, condition["planned"] - condition["sent"])
            request_slot = self.attempted - 1
            self.counts[target.name] += 1
            if not readiness and not evidence_probe:
                self.endpoint_attempt(target.name)
            key = (target.name, spec.scenario)
            self.active[key] += 1
            self.peak[key] = max(self.peak[key], self.active[key])
            global_deadline = False
            deadline = None
            intentional_deadline_applies = False
            started, started_at = time.monotonic(), _now()
            token = self.inflight.start(target.name)
            try:
                if on_start:
                    on_start()
                self.emit("request_started", target.name, spec.scenario,
                          request_slot=request_slot, fixture_id=spec.fixture_id,
                          input_chars=request_parameters["input_chars"], requested_max_tokens=output_cap,
                          requested_input_tokens=spec.requested_input_tokens,
                          condition_id=condition_id, condition_summary=dict(condition) if condition else None)
                request_timeout = (spec.request_timeout_seconds if spec.request_timeout_seconds is not None
                                   else self.config.request_timeout_seconds)
                if deadline_override is not None:
                    timeout_override = min(timeout_override or request_timeout,
                                           deadline_override - time.monotonic())
                intentional_deadline_applies = timeout_override is not None and timeout_override < request_timeout
                remaining = self.deadline - time.monotonic()
                global_deadline = remaining <= min(request_timeout, timeout_override or request_timeout)
                timeout = max(.001, min(request_timeout,
                                        timeout_override or request_timeout,
                                        self.deadline - time.monotonic()))
                deadline = asyncio.timeout(timeout)
                async with deadline:
                    record = await client.execute(spec, first_output_event=event)
                if deadline.expired():
                    record.status, record.valid = ("cancelled" if global_deadline else "timeout"), False
                    record.error = "maximum run duration reached" if global_deadline else (
                        "intentional request deadline" if timeout_override else "request deadline exceeded")
            except (asyncio.CancelledError, TimeoutError) as exc:
                run_deadline_expired = global_deadline and deadline is not None and deadline.expired()
                record = RequestRecord(
                    id=uuid.uuid4().hex, target=target.name, fixture_id=spec.fixture_id,
                    scenario=spec.scenario, check_ids=spec.check_ids, started_at=started_at,
                    status="cancelled" if isinstance(exc, asyncio.CancelledError) or run_deadline_expired else "timeout",
                    error="run cancelled" if isinstance(exc, asyncio.CancelledError)
                    else ("maximum run duration reached" if run_deadline_expired else "request deadline exceeded"), elapsed_ms=(time.monotonic()-started)*1000,
                    input_chars=spec.input_chars, requested_max_tokens=output_cap, stream=spec.stream,
                )
            except Exception as exc:
                # Exception text can contain credential-bearing URLs from transports.
                record = RequestRecord(
                    id=uuid.uuid4().hex, target=target.name, fixture_id=spec.fixture_id,
                    scenario=spec.scenario, check_ids=spec.check_ids, started_at=started_at,
                    status="failed", error=f"client failure ({type(exc).__name__})",
                    elapsed_ms=(time.monotonic()-started)*1000,
                    input_chars=spec.input_chars, requested_max_tokens=output_cap,
                )
            finally:
                self.active[key] -= 1
                overlap = self.inflight.finish(target.name, token)
            # A transport may catch cancellation to preserve partial streamed output.
            if time.monotonic() >= self.deadline and record.status == "cancelled":
                record.error = "maximum run duration reached"
            record.completed_at = _now()
            record.request_timeout_seconds = spec.request_timeout_seconds or self.config.request_timeout_seconds
            if record.status in {"timeout", "cancelled"}:
                reason = self.stop_reason(target.name)
                if self.stop.is_set() or reason == "cancelled by user":
                    record.termination_reason, record.termination_limit_seconds = "user_stop", None
                    record.error = "cancelled by user"
                elif time.monotonic() >= self.deadline:
                    record.termination_reason = "run_duration_limit"
                    record.termination_limit_seconds = self.config.max_duration_seconds
                    record.error = "maximum run duration reached"
                elif self.memory_limit_reached:
                    record.termination_reason, record.termination_limit_seconds = "memory_limit", None
                    record.error = self.reason
                elif reason == "error threshold reached":
                    record.termination_reason, record.termination_limit_seconds = "error_threshold", None
                    record.error = f"stopped after {self.config.stop_after_errors} request errors"
                elif self.endpoint_health[target.name]["abort_reason"]:
                    record.termination_reason, record.termination_limit_seconds = "endpoint_unresponsive", self.config.unresponsive_for_seconds
                    record.error = self.endpoint_health[target.name]["abort_reason"]
                elif intentional_deadline_applies and deadline is not None and deadline.expired():
                    record.termination_reason = "intentional_deadline"
                    record.termination_limit_seconds = timeout
                elif record.status == "timeout" and record.termination_reason in {None, "external_cancellation"}:
                    record.termination_reason = "request_timeout"
                    record.termination_limit_seconds = record.request_timeout_seconds
                elif not record.termination_reason:
                    record.termination_reason = "intentional_cancellation" if spec.cancel_after_ms or spec.cancel_after_chunks else "workload_cancelled"
                    record.termination_limit_seconds = spec.cancel_after_ms / 1000 if spec.cancel_after_ms else None
            record.condition_id = condition_id
            record.dispatch_ms = (started-self.start)*1000
            record.timing_start_ms = ((record._timing_started_monotonic - self.start) * 1000
                if record._timing_started_monotonic is not None else record.dispatch_ms)
            record.completed_ms = (time.monotonic()-self.start)*1000
            record.workload = {**spec.workload, **record.workload}
            record.load_point = dict(spec.load_point) if spec.load_point else None
            record.requested_input_tokens = spec.requested_input_tokens
            record.requested_output_tokens = spec.requested_output_tokens
            record.scheduled_ms = spec.workload.get("scheduled_ms")
            record.client_wait_ms = max(0, record.dispatch_ms-record.scheduled_ms) if record.scheduled_ms is not None else None
            record.overlap = overlap
            try:
                record = score_response(spec, record)
                if evaluate_record is not None:
                    evaluate_record(record)
            finally:
                self.records.append(record)
            intentional = ((spec.cancel_after_ms is not None or spec.cancel_after_chunks is not None)
                           and record.status == "cancelled") or (
                timeout_override is not None and record.status == "timeout")
            record.intentional = intentional
            # Real clients report body reads immediately, including SSE
            # comments/reasoning. Test/alternate clients can provide record evidence.
            if (not evidence_probe and not getattr(client, "reports_response_activity", False)
                    and (record.response_body_bytes or record.chunks or record.output or record.reasoning)):
                self.endpoint_response(target.name)
            if (not readiness and not evidence_probe and no_response_failure(record, include_timeouts=not _stress(spec.scenario))
                    and not intentional and timeout_override is None
                    and deadline_override is None and spec.cancel_after_ms is None
                    and spec.cancel_after_chunks is None and not self.stop.is_set()
                    and time.monotonic() < self.deadline):
                self.endpoint_failure(target.name, record.id)
            if record.status == "cancelled" and not intentional and timeout_override is None:
                reason = self.stop_reason(target.name)
                # Stress phases can cancel their own requests while the run continues.
                if reason or not _stress(spec.scenario):
                    if not self.endpoint_health[target.name]["abort_reason"]:
                        self.reason = reason or "maximum run duration reached"
                    else:
                        record.error = self.endpoint_health[target.name]["abort_reason"]
                    self.mark_unfinished(target.name, _scenario_checks(spec))
            budget_only = bool(set(spec.check_ids) & {"prefix", "sessions", "tools"}) and reasoning_budget(record)
            if _serving_failure(record, spec.scenario) and not readiness and not intentional and not budget_only:
                self.errors += 1
                record.check_ids = list(dict.fromkeys([*record.check_ids, *[
                    name for name in ("access", "serving", "first_output") if name in self.config.checks]]))
            if budget_exempt:
                self.observations[target.name].setdefault("readiness_requests", []).append(record.id)
            if condition is not None:
                condition["running"] -= 1
                condition["completed"] += 1
                condition["failed"] += int(_request_failed(record))
                result_status = request_status(record.model_dump(include={"status", "score", "valid"}))
                statuses = condition.setdefault("request_statuses", {})
                statuses[result_status] = statuses.get(result_status, 0) + 1
                if record.status == "cancelled" and not intentional and (
                        self.stop.is_set() or self.reason or time.monotonic() >= self.deadline):
                    condition["reason"] = self.condition_reason(target.name)
            # Live visualization gets measurements only, never retained response bodies.
            self.emit("request_completed", target.name, spec.scenario, status=record.status,
                      condition_id=condition_id, condition_summary=dict(condition) if condition else None,
                      input_chars=request_parameters["input_chars"], requested_max_tokens=output_cap,
                      requested_input_tokens=spec.requested_input_tokens,
                      request_slot=request_slot, request=record.model_dump(mode="json", include={
                          "id", "target", "fixture_id", "scenario", "condition_id", "intentional", "status", "valid", "score",
                          "elapsed_ms", "first_output_ms", "check_ids", "http_status",
                          "request_timeout_seconds", "termination_reason", "termination_limit_seconds",
                          "started_at", "dispatch_ms", "completed_ms", "timing_start_ms",
                          "timing_schema", "first_token_ms", "first_reasoning_ms", "last_output_ms",
                          "stream", "stream_end_ms", "answer_reads", "answer_chunks", "answer_chars",
                          "first_read_answer_chars", "max_stream_gap_ms", "input_tokens", "output_tokens",
                          "completion_tokens", "reasoning_tokens", "cached_prompt_tokens", "finish_reason",
                      }), intentional=intentional)
            return record

    async def group(self, target, client, specs, scenario, count=None, concurrency=1, checks=()):
        count = count if count is not None else self.config.samples
        workload = specs[0].workload if specs else {}
        request_parameters = {"concurrency": concurrency,
            "same_prompt": bool(specs) and len({json.dumps(specs[i % len(specs)].messages, sort_keys=True)
                                               for i in range(count)}) == 1}
        self.start_condition(target.name, scenario, workload, count, request_parameters=request_parameters)
        self.emit("scenario_started", target.name, scenario, planned_requests=count)
        if not specs:
            self.unfinished[target.name].update(checks)
            self.finish_condition(target.name, scenario, workload)
            return []
        try:
            result = []
            for start in range(0, count, concurrency):
                batch = [_spec(specs[i % len(specs)], scenario, checks)
                         for i in range(start, min(start + concurrency, count))]
                if not self.allowed(target.name):
                    for spec in batch:
                        self.mark_unfinished(target.name, _scenario_checks(spec))
                    break
                result.extend(r for r in await asyncio.gather(
                    *(self.request(target, client, spec, request_parameters=request_parameters) for spec in batch)) if r is not None)
            if len(result) < count:
                self.unfinished[target.name].update(checks)
            return result
        finally:
            self.emit("scenario_finished", target.name, scenario)
            self.finish_condition(target.name, scenario, workload)

    async def monitor(self, tasks):
        cancelled_targets = set()
        while not all(task.done() for task in tasks):
            await asyncio.sleep(.05)
            now = time.monotonic()
            self.timing_health.sample(now)
            self.max_lag_ms = max(self.max_lag_ms, self.timing_health.max_lag_ms)
            if all(task.done() for task in tasks):
                return
            reason = self.stop_reason()
            if reason and reason != "maximum request budget reached":
                for task in tasks:
                    if not task.done():
                        task.cancel()
                return
            for target, task in self.target_tasks.items():
                if (self.endpoint_health[target]["abort_reason"] and target not in cancelled_targets
                        and not task.done()):
                    cancelled_targets.add(target)
                    task.cancel()


async def _restart(run: _Run, target: Target) -> bool:
    if run.config.restart_target != target.name:
        return True
    observation = {"requested": True, "status": "blocked", "readiness_ms": None,
                   "coverage": "Only this configured hook; individual backend restart unverified"}
    run.observations[target.name]["restart"] = observation
    if not target.restart_command:
        observation["reason"] = "Explicit restart requires a configured restart_command"
        return False
    if not run.allowed(target.name):
        observation["reason"] = "No remaining run budget for restart and readiness"
        return False
    process = None
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *target.restart_command, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), min(run.config.request_timeout_seconds,
                                                  max(.001, run.deadline-time.monotonic())))
        observation["hook_ms"] = round((time.monotonic()-started)*1000, 3)
        if process.returncode:
            observation["reason"] = f"Restart hook exited with status {process.returncode}"
            return False
        observation["status"] = "hook_completed"
        observation["started_monotonic"] = started
        return True
    except (OSError, TimeoutError) as exc:
        observation["reason"] = f"Restart hook did not complete ({type(exc).__name__})"
        return False
    finally:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                process.kill()
                await process.wait()


_GPU_FAMILIES = {"memory", "utilization", "temperature", "throttling", "device_errors"}


def _gpu_snapshot(snapshot):
    samples = [s for s in snapshot.get("samples", []) if s["family"] in _GPU_FAMILIES]
    families = {s["family"] for s in samples if not s["stale"]}
    return {**snapshot, "samples": samples, "unavailable": sorted(set(_GPU_FAMILIES)-families)}


def _gpu_scraped(snapshot):
    return snapshot.get("status") == "available" and any(
        sample["family"] in _GPU_FAMILIES and not sample["stale"] for sample in snapshot.get("samples", []))


async def _metrics(run, target, phase):
    from giraffe.telemetry import scrape
    data = run.observations[target.name]["serving_telemetry"]
    # Reserve the last slot for the final end scrape, including GPU-only runs.
    limit = run.config.metrics_max_samples - (phase != "end")
    if len(data["snapshots"]) >= limit:
        data["sample_limit_reached"] = True
        return
    # Calling this helper explicitly requests runtime telemetry. Host-only collection
    # shares the scheduler but must not implicitly enable the runtime exporter.
    snapshot = await scrape(run, target, phase, force=True)
    data["snapshots"].append(snapshot)
    run.observations[target.name]["metrics"].append(_gpu_snapshot(snapshot))


async def _finish_gpu_observation(run, target):
    """Allow DCGM at least one refresh, bounded by the enclosing run deadline."""
    data = run.observations[target.name]["serving_telemetry"]
    first = next((snap["elapsed_seconds"] for snap in data["snapshots"] if _gpu_scraped(snap)), None)
    window = run.config.gpu_min_observation_seconds
    if first is not None and not run.stop_reason(target.name):
        delay = min(max(0, window-(time.monotonic()-run.start-first)),
                    max(0, run.deadline-time.monotonic()-.01))
        if delay:
            try:
                await asyncio.wait_for(run.stop.wait(), delay)
            except TimeoutError:
                pass
    count = len(data["snapshots"])
    await _metrics(run, target, "end")
    # Only a successful, fresh end scrape closes the window; failed end scrapes leave it open.
    end = data["snapshots"][-1] if len(data["snapshots"]) > count else None
    last = next((snap["elapsed_seconds"] for snap in reversed(data["snapshots"]) if _gpu_scraped(snap)), None)
    elapsed = last - first if first is not None and last is not None else 0
    fresh_end = end is not None and _gpu_scraped(end)
    run.observations[target.name]["gpu_observation_window"] = {
        "required_seconds": window, "elapsed_seconds": elapsed,
        "end_scrape_status": end["status"] if end else "not_collected", "end_scrape_fresh": fresh_end,
        "complete": first is not None and fresh_end and elapsed >= window}


async def _context_fixtures(run, target, client, fixtures, source=None):
    """Size recall cases locally with the target tokenizer, without inference."""
    from .fixtures import _context_request
    from .local_tokenizer import prepare_messages

    config = _check_config(run.config, "context")
    prepared, evidence = [], []
    for original in fixtures["context"]:
        _, size, position, *_ = original.fixture_id.split(".")
        output_budget = original.max_tokens or config.max_output_tokens
        desired = max(1, int((config.context_limit - output_budget) * .85))
        if size != "long":
            desired = max(1, desired // 3)

        def build(chars):
            # The fixture builder takes a character allowance; the tokenizer
            # determines that allowance using the complete rendered chat prompt.
            allowance = chars if size == "long" else chars * 3
            local = config.model_copy(update={"context_limit": max(128, allowance + output_budget + 32)})
            return _context_request(local, size, position, "context")

        result = await prepare_messages(run, target, "context_preparation", lambda chars: build(chars).messages, desired, tolerance=.01)
        evidence.append({key: value for key, value in result.items() if key != "messages"})
        if result["status"] != "calibrated":
            run.observations[target.name]["context_calibration"] = {
                "status": "unavailable", "reason": result["reason"], "method": "local_tokenizer", "preparations": evidence}
            run.mark_unfinished(target.name, {"context"})
            return []
        spec = build(result["filler_chars"])
        spec.messages = result["messages"]
        spec.requested_input_tokens = desired
        spec.workload.update(calibrated_input_tokens=result["input_tokens"], input_token_source=result["method"],
                             token_count_scope=result["token_count_scope"],
                             tokenizer=result["tokenizer"], context_group=f"{desired} target tokens")
        prepared.append(spec)
    run.observations[target.name]["context_calibration"] = {
        "status": "sized_locally", "method": "local_tokenizer", "preparations": evidence}
    return prepared


async def _target(run: _Run, target: Target, fixtures: dict):
    config, enabled = run.config, set(run.config.checks)
    telemetry_task = None
    from giraffe import deployment, telemetry, workloads
    try:
        reason = run.stop_reason(target.name)
        if reason and reason != "maximum request budget reached":
            run.unfinished[target.name].update(enabled)
            return
        run.observations[target.name]["deployment"] = await deployment.snapshot(target, config, run.deadline, run.stop)
        if not await _restart(run, target):
            run.stop_reason(target.name)
            return
        # GPU observations do not require an inference client or an unrelated
        # warm-up. An explicitly requested restart still needs readiness probes.
        if enabled == {"gpu"} and config.restart_target != target.name:
            await _metrics(run, target, "start")
            return
        async with LLMClient(target, config, max_connections=run.connection_limits[target.name]) as client:
            client.logprob_budget = run.logprob_budget
            client.on_response = lambda: run.endpoint_response(target.name)
            data = run.observations[target.name]["serving_telemetry"]
            data["host_configured"] = bool(target.host_metrics_url)
            run.preparation(target.name, "runtime_metrics_discovery", "Checking available runtime telemetry", "running")
            await _metrics(run, target, "start")
            available = data["snapshots"] and data["snapshots"][-1]["status"] in {"available", "stale_samples"}
            discovery = run.observations[target.name].get("runtime_metrics_discovery", {})
            run.preparation(target.name, "runtime_metrics_discovery", "Runtime telemetry capability", "completed" if available else "unavailable", discovery.get("reason"))
            if available or target.metrics_url or config.metrics or target.host_metrics_url or target.deployment.collector_url:
                telemetry_task = asyncio.create_task(telemetry.sample_during(run, target))
            restart = run.observations[target.name]["restart"]
            if enabled & {"access", "serving", "first_output", "generation"} or restart:
                response = config.effective_initial()
                response_config = config.model_copy(update={key: response[key] for key in ("samples", "max_output_tokens", "request_timeout_seconds")})
                initial_specs = builtin_fixtures(response_config, check_id="serving")["short"]
                if restart and restart["status"] == "hook_completed":
                    ready_deadline = min(run.deadline, time.monotonic() + config.readiness_timeout_seconds)
                    backoff = .1
                    initial = []
                    restart["health_polls"] = []
                    while time.monotonic() < ready_deadline and not run.stop_reason(target.name):
                        try:
                            async with asyncio.timeout_at(ready_deadline):
                                ready = await client.ready(ready_deadline)
                        except TimeoutError:
                            ready = False
                        restart["health_polls"].append({"elapsed_seconds": time.monotonic()-run.start,
                                                        "ready": ready})
                        if ready:
                            probe = _spec(initial_specs[0], "readiness").model_copy(update={"check_ids": []})
                            record = await run.request(target, client, probe, deadline_override=ready_deadline,
                                                       budget_exempt=True)
                            if record and record.valid:
                                record.check_ids = [name for name in ("access", "serving", "first_output")
                                                    if name in enabled]
                                initial = [record]
                            break
                        await asyncio.sleep(min(backoff, max(0, ready_deadline-time.monotonic())))
                        backoff = min(5, backoff * 2)
                    restart["status"] = "ready" if initial else "blocked"
                    restart["readiness_ms"] = round((time.monotonic()-restart.pop("started_monotonic"))*1000, 3)
                    if restart["status"] == "blocked":
                        restart["reason"] = "No successful inference within readiness budget"
                        run.stop_reason(target.name)
                        return
                else:
                    await run.group(target, client, initial_specs, "initial", count=1)
            run.finish_checks(target.name, {"access"})
            if config.stream and hasattr(client, "token_evidence_probe"):
                # A one-token inference can still have a slow first response.
                # Share one configured timeout across compatibility retries.
                probe_end = min(run.deadline, time.monotonic() + config.request_timeout_seconds)
                run.preparation(target.name, "performance_evidence_probe", "Checking streamed token and usage evidence", "running")
                client.on_response = None
                probe_count = 0
                while (run.allowed(target.name) and time.monotonic() < probe_end
                       and (run.remaining(target.name) is None or run.remaining(target.name) > max(
                           1, run.planned_request_reserve_per_target - (run.budget_counts[target.name] - probe_count)))):
                    spec = client.token_evidence_probe()
                    if spec is None:
                        break
                    spec = spec.model_copy(update={"request_timeout_seconds": min(spec.request_timeout_seconds, max(.001, probe_end - time.monotonic()))})
                    record = await run.request(target, client, spec)
                    if record is None:
                        break
                    probe_count += 1
                    run.observations[target.name]["token_evidence"] = client.observe_token_evidence_probe(record)
                client.on_response = lambda: run.endpoint_response(target.name)
                evidence = run.observations[target.name].setdefault("token_evidence", {"status": "unavailable", "reason": "No request budget or preparation time remained for a token-evidence compatibility probe."})
                run.preparation(target.name, "performance_evidence_probe", "Streamed token and usage capability", "completed" if evidence.get("status") in {"available", "observed"} else "unavailable", evidence.get("reason"))
            if enabled & {"serving", "first_output"}:
                response = config.effective_response()
                response_specs = builtin_fixtures(config.model_copy(update={key: response[key] for key in ("samples", "max_output_tokens", "request_timeout_seconds")}), check_id="serving")["short"]
                await run.group(target, client, response_specs, "warm", count=response["samples"])
            run.finish_checks(target.name, {"serving", "first_output"})
            if "generation" in enabled:
                effective = _check_config(config, "generation")
                await run.group(target, client, builtin_fixtures(effective, check_id="generation")["generation"], "generation",
                                count=effective.samples, checks=["generation"])
            run.finish_checks(target.name, {"generation"})
            if "context" in enabled:
                effective = _check_config(config, "context")
                check_fixtures = builtin_fixtures(effective, check_id="context")
                context_fixtures = await _context_fixtures(run, target, client, check_fixtures)
                count = max(effective.samples, len(check_fixtures["context"]))
                if context_fixtures:
                    await run.group(target, client, context_fixtures, "context", count=count, checks=["context"])
                else:
                    run.plan_condition(target.name, "context")
                    run.finish_condition(target.name, "context", reason=run.observations[target.name]["context_calibration"]["reason"],
                                         load_point_result={"status": "skipped"})
            run.finish_checks(target.name, {"context"})
            for check_id in ("correctness", "json"):
                if check_id in enabled:
                    if check_id == "correctness" and run.pack_specs:
                        if "capacity" not in enabled:
                            await run.group(target, client, run.pack_specs, "pack_control",
                                            count=len(run.pack_specs), checks=["correctness"])
                            effective = _check_config(config, "correctness")
                            if "load" not in enabled:
                                await run.group(target, client, run.pack_specs, "pack_loaded",
                                            count=max(len(run.pack_specs), effective.concurrency),
                                            concurrency=effective.concurrency, checks=["correctness"])
                        run.finish_checks(target.name, {check_id})
                        continue
                    effective = _check_config(config, check_id)
                    check_fixtures = builtin_fixtures(effective, check_id=check_id)
                    if check_id == "correctness":
                        # Explicit custom request settings belong to their fixture.
                        check_fixtures[check_id].extend(spec.model_copy(update={
                            "max_tokens": spec.max_tokens if spec.max_tokens is not None else effective.max_output_tokens,
                            "request_timeout_seconds": (spec.request_timeout_seconds if spec.request_timeout_seconds is not None
                                                        else effective.request_timeout_seconds)})
                            for spec in fixtures[check_id] if spec.workload.get("request_source") == "custom_fixtures")
                    specs = [spec.model_copy(update={"options": deterministic_options(effective, spec.options)})
                             for spec in check_fixtures[check_id]]
                    await run.group(target, client, specs, f"{check_id}_c{effective.concurrency}",
                                    count=max(effective.samples, len(specs)),
                                    concurrency=effective.concurrency, checks=[check_id])
                    run.finish_checks(target.name, {check_id})
            if "cancellation" in enabled:
                effective = _check_config(config, "cancellation")
                options = config.effective_check("cancellation")
                cancellation_fixtures = builtin_fixtures(effective, check_id="cancellation")
                normal = [spec.model_copy(update={"scorer": "none", "options": deterministic_options(config, spec.options)})
                                 if spec.fixture_id.startswith("limits.stop.") else spec
                                 for spec in cancellation_fixtures["limits"] if spec.cancel_after_ms is None
                                 and not spec.fixture_id.startswith("limits.stop_control")]
                cancel = [spec.model_copy(update={"cancel_after_ms": None,
                                                 "cancel_after_chunks": options["cancel_after_chunks"]})
                          for spec in cancellation_fixtures["limits"] if spec.cancel_after_ms is not None]
                await run.group(target, client, [spec for spec in cancellation_fixtures["limits"]
                                if spec.fixture_id.startswith("limits.stop_control")], "stop_control",
                                count=1, checks=["cancellation"])
                await run.group(target, client, normal, "limits",
                                count=max(effective.samples, len(normal)), checks=["cancellation"])
                if cancel:
                    # Same-fixture baseline: a follow-up queued behind leaked generation shows as delay.
                    await run.group(target, client, cancellation_fixtures["short"][:1], "cancellation_baseline",
                                    count=1, checks=["cancellation"])
                await run.group(target, client, cancel, "client_cancel", count=len(cancel),
                                checks=["cancellation"])
                # Follow-ups go immediately after the disconnect, while leaked work would still run.
                await run.group(target, client, cancellation_fixtures["short"], "cancellation_probe",
                                count=effective.samples, checks=["cancellation"])
                if cancel:
                    deadline_spec = _spec(cancel[0].model_copy(update={"cancel_after_ms": None, "cancel_after_chunks": None}),
                                          "client_deadline", ["cancellation"])
                    probe_timeout = min(options["deadline_probe_ms"] / 1000, effective.request_timeout_seconds)
                    run.observations[target.name]["deadline_probe_ms"] = probe_timeout * 1000
                    await run.request(target, client, deadline_spec, timeout_override=probe_timeout)
                if options["backend_verification"] == "required":
                    from giraffe.backend_cancellation import exercise
                    await exercise(run, target, client, cancellation_fixtures)
            run.finish_checks(target.name, {"cancellation"})
            if "recovery" in enabled:
                effective = _check_config(config, "recovery")
                probe_specs = builtin_fixtures(effective, check_id="recovery")["sustained"]
                from .planning import request_plan
                later = [name for name in config.checks if name not in CORE_CHECKS or name == "load"]
                later_plan = request_plan(config.model_copy(update={"checks": later, "restart_target": None}))
                if "cancellation" in enabled and not run.stop_reason(target.name):
                    cleanup = config.effective_check("cancellation")["cleanup_seconds"]
                    await asyncio.sleep(max(0, min(5, cleanup, run.deadline - time.monotonic())))
                remaining = run.remaining(target.name)
                if remaining is None or remaining > (
                        later_plan["minimum_reserved_per_target"] + 2 * effective.samples
                        + max(effective.limits.min_samples, effective.concurrency*2)):
                    # Uncounted warm-up so the baseline is not the probe fixture's cold first use.
                    await run.request(target, client, probe_specs[0].model_copy(update={
                        "scenario": "recovery_prime", "check_ids": []}))
                await run.group(target, client, probe_specs, "recovery_baseline",
                                count=effective.samples, checks=["recovery"])
                remaining = run.remaining(target.name)
                reserved = later_plan["minimum_reserved_per_target"] + effective.samples
                # Preserve later fixed work and probes; cap the remaining share
                # only when another selected check still needs request budget.
                allowance = (max(0, min(remaining // 2 if later else remaining, remaining-reserved))
                             if remaining is not None else None)
                run.observations[target.name]["sustained_budget"] = {
                    "allowance": allowance, "reserved_later": reserved,
                    "policy": "reserve fixed later work and probes; half quota cap only with later checks"}
                heavy = traffic_fixtures(effective, check_id="recovery")
                heavy_specs = heavy["long_input"] + heavy["long_output"]
                end = min(run.deadline, time.monotonic() + effective.sustained_seconds)
                dispatched, exhausted = 0, False
                async def worker():
                    nonlocal dispatched, exhausted
                    while time.monotonic() < end:
                        if (allowance is not None and dispatched >= allowance) or not run.allowed(target.name):
                            if allowance is not None and dispatched >= allowance:
                                run.limits_reached.add("max_requests")
                            exhausted = True
                            return
                        index = dispatched
                        dispatched += 1
                        spec = _spec(heavy_specs[index % len(heavy_specs)], "sustained", ["recovery"])
                        await run.request(target, client, spec)
                run.start_condition(target.name, "sustained", planned=allowance, planned_kind="upper_bound")
                run.emit("scenario_started", target.name, "sustained")
                await asyncio.gather(*(worker() for _ in range(effective.concurrency)))
                run.emit("scenario_finished", target.name, "sustained")
                run.finish_condition(target.name, "sustained", reason="sending window ended" if allowance is None or dispatched < allowance else None)
                run.observations[target.name]["sustained_batches"] = dispatched
                sustained = [r for r in run.records if r.target == target.name and r.scenario == "sustained"]
                window_ms = max((r.completed_ms for r in sustained), default=0) - min(
                    (r.dispatch_ms for r in sustained), default=0)
                weighted = sum(r.completed_ms-r.dispatch_ms for r in sustained) / window_ms if window_ms else 0
                enough = len(sustained) >= max(effective.limits.min_samples, effective.concurrency*2)
                # Dispatch/refill and drain edges make an exact time average of C
                # impossible. Require >=90% occupancy as well as the observed peak.
                loaded = enough and weighted >= effective.concurrency*.9 and run.peak[(target.name, "sustained")] >= effective.concurrency
                elapsed = time.monotonic() >= end and end < run.deadline
                successful = sum(r.status == "completed" and r.valid for r in sustained)
                occupied = weighted >= effective.concurrency*.9 and run.peak[(target.name, "sustained")] >= effective.concurrency
                complete = successful >= effective.limits.min_samples and ((elapsed and occupied) or (exhausted and loaded))
                run.observations[target.name]["sustained_complete"] = complete
                run.observations[target.name]["sustained_completion"] = {
                    "reason": "duration elapsed" if elapsed and complete else
                              "allowance consumed with sustained concurrency" if complete else "insufficient load evidence",
                    "time_weighted_in_flight": weighted, "minimum_occupancy_fraction": .9,
                    "requests": len(sustained), "required_requests": max(effective.limits.min_samples, effective.concurrency*2),
                    "successful_requests": successful, "required_successful_requests": effective.limits.min_samples}
                await run.group(target, client, probe_specs, "recovery",
                                count=effective.samples, checks=["recovery"])
            run.finish_checks(target.name, {"recovery"})
            await workloads.execute(run, target, client, fixtures)
            run.finish_checks(target.name, enabled - {"gpu"})
    except asyncio.CancelledError:
        run.unfinished[target.name].update(check for name, check in run.pending_checks if name == target.name)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) and str(exc).startswith((
            "Missing API key environment variable:", "Missing header environment variable:")) else type(exc).__name__
        run.observations[target.name]["client_error"] = f"Client setup failed ({detail})"
        run.unfinished[target.name].update(enabled)

    finally:
        preparation = run.observations[target.name].get("preparation", {})
        if preparation.get("status") == "running":
            reason = run.stop_reason(target.name)
            run.preparation(target.name, preparation["scenario"], preparation["step"],
                            "cancelled" if reason else "unavailable", reason or "Preparation did not complete")
        for row in run.observations[target.name]["conditions"]:
            if row["status"] != "finished":
                run.finish_condition(target.name, row["condition"]["id"])
        if telemetry_task:
            telemetry_task.cancel()
            await asyncio.gather(telemetry_task, return_exceptions=True)
        data = run.observations[target.name]["serving_telemetry"]
        if "gpu" in enabled and "gpu_observation_window" not in run.observations[target.name]:
            await _finish_gpu_observation(run, target)
        elif data["snapshots"] or target.host_metrics_url:
            await _metrics(run, target, "end")
        elif len(data["snapshots"]) < config.metrics_max_samples:
            data["snapshots"].append(await telemetry.scrape(run, target, "not collected"))
        telemetry.summarize(data, config.cache_pressure_threshold)
        run.observations[target.name]["metrics"] = [_gpu_snapshot(snap) for snap in data["snapshots"]]
        current = run.observations[target.name]["deployment"]
        if current and (target.deployment.discovery != "none" or target.deployment.collector_url):
            end = await deployment.snapshot(target, config, run.deadline, run.stop)
            run.observations[target.name]["deployment_history"].append(end)
            observations = [current, *run.observations[target.name]["deployment_history"]]
            initial_signature = deployment.observed_signature(current)
            changes = []
            for item in observations[1:]:
                signature = deployment.observed_signature(item)
                if any(signature[key] != initial_signature[key] for key in signature.keys() & initial_signature.keys()):
                    changes.append(item)
            current["continuity"] = "unstable" if changes else "no_observed_change; between samples unverified"
            current["observed_changes"] = changes
        if current:
            served = sorted({r.observed_model for r in run.records if r.target==target.name and r.observed_model})
            if len(served) > 1:
                current["continuity"] = "unstable"
                current["observed_model_identity_changes"] = True
            if served:
                row = current["fields"].setdefault("model.served", {})
                row["reported"] = deployment._fact(served, "runtime-reported", current["scope"], _now())
                if "configured" in row and row["configured"]["value"] not in (served, served[0] if len(served) == 1 else served):
                    row["status"] = "conflicting"
                current["unknown_fields"] = [key for key in current["unknown_fields"] if key != "model.served"]

        run.finish_checks(target.name, enabled)


def _timing_violations(records, limits, *, unfinished=False):
    failures = []
    for record in records:
        if record.status != "completed" or not record.valid:
            if unfinished and exceeded_unfinished(record, limits):
                failures.append(record.id)
            continue
        if limits.first_output_ms and record.first_output_ms is not None and record.first_output_ms > limits.first_output_ms:
            failures.append(record.id)
        elif limits.latency_ms and record.elapsed_ms > limits.latency_ms:
            failures.append(record.id)
        elif limits.stream_gap_ms and record.max_stream_gap_ms is not None and record.max_stream_gap_ms > limits.stream_gap_ms:
            failures.append(record.id)
        elif (limits.min_output_tokens_per_second
              and (rate := record.generation_tokens_per_second) is not None
              and rate < limits.min_output_tokens_per_second):
            failures.append(record.id)
    return failures


def _missing_timing(records, limits):
    missing = set()
    for record in records:
        if not record.valid or record.status != "completed":
            continue
        if limits.first_output_ms is not None and record.first_output_ms is None:
            missing.add("first_output_ms")
        if (limits.stream_gap_ms is not None and record.stream_gap_applicable
                and (not record.stream or record.max_stream_gap_ms is None or record.answer_reads < 2)):
            missing.add("stream_gap_ms")
        if (limits.min_output_tokens_per_second is not None
                and record.output_tokens != 1 and record.generation_tokens_per_second is None):
            missing.add("generation_tokens_per_second")
        if limits.latency_ms is not None and record.elapsed_ms <= 0:
            missing.add("latency_ms")
    if (limits.min_output_tokens_per_second is not None
            and not any(r.generation_tokens_per_second is not None for r in records)):
        missing.add("generation_tokens_per_second")
    return sorted(missing)


def _answer_failures(records: list[RequestRecord], warm: list[RequestRecord]) -> list[dict]:
    failed = Counter(r.fixture_id for r in records if r.score is False)
    scored = Counter(r.fixture_id for r in records if r.score is not None)
    warmup_failed = Counter(r.fixture_id for r in warm if r.score is False)
    warmup_scored = Counter(r.fixture_id for r in warm if r.score is not None)
    return [{"fixture_id": fixture, "failed": failed[fixture], "scored": scored[fixture],
             "warmup_failed": warmup_failed[fixture], "warmup_scored": warmup_scored[fixture]}
            for fixture in sorted(failed)]


def _pack_results(run: _Run, target: Target) -> dict:
    quiet = [record for record in run.records
             if record.target == target.name and record.scenario == "pack_control"]
    stages = run.observations[target.name]["capacity"]
    cases = []
    for spec in run.pack_specs:
        case_id = spec.workload["case_id"]
        records = [record for record in quiet if record.fixture_id == spec.fixture_id]
        valid = [record for record in records if record.status == "completed" and record.valid]
        scored = [record for record in valid if record.score is not None]
        failed = any(record.score is False or record.status in {"failed", "timeout"}
                     or (record.status == "completed" and not record.valid) for record in records)
        status = "fail" if failed else "pass" if scored else "inconclusive"
        cases.append({"case_id": case_id, "quiet": {
            "planned": 1, "offered": len(records), "started": len(records),
            "valid": len(valid), "scored": len(scored),
            "correct": sum(record.score is True for record in scored), "status": status,
        }, "stages": [{"stage_index": stage["stage_index"], "rate_rps": stage["rate_rps"],
                        **stage["case_counts"][case_id]} for stage in stages]})
    limits = _check_config(run.config, "correctness").limits
    target_records = [r for r in run.records if r.target == target.name]
    delays = scheduling_delay_windows(run)
    for case, spec in zip(cases, run.pack_specs, strict=True):
        loaded = [r for r in run.records if r.target == target.name and r.fixture_id == spec.fixture_id
                  and (r.traffic_stage is not None or r.scenario == "pack_loaded" or bool(r.load_point))]
        stats = _stats(loaded)
        accuracy = stats["correct"] / stats["scored"] if stats["scored"] else None
        failed = accuracy is not None and accuracy < limits.min_correctness
        observed = bool(loaded) and all(r.valid and r.score is not None for r in loaded)
        evidenced = max_evidenced_overlap((r for r in loaded if r.score is not None), target_records, delays)
        concurrency = run.config.effective_check("correctness")["concurrency"]
        concurrent = concurrency == 1 or evidenced >= 2
        case["loaded"] = {**stats, "accuracy": accuracy, "evidenced_overlap": evidenced, "evidenced_overlap_capped_at": 2,
                          "configured_concurrency": concurrency,
                          "concurrency_reason": None if concurrent else NEEDS_STREAM
                              if not any(r.stream for r in loaded) else "No streamed responses overlapped outside client scheduling delays.",
                          "status": "fail" if failed else "pass" if observed and concurrent else "inconclusive"}
    coverage = bool(cases) and all(case["quiet"]["scored"] >= 1 for case in cases)
    all_scored = all(spec.scorer != "none" for spec in run.pack_specs)
    control_status = ("fail" if any(case["quiet"]["status"] == "fail" for case in cases)
                      else "pass" if coverage and all_scored else "inconclusive")
    preview = pack_preview(run.config)
    loaded_status = ("fail" if any(case["loaded"]["status"] == "fail" for case in cases)
                     else "pass" if cases and all(case["loaded"]["status"] == "pass" for case in cases)
                     else "inconclusive")
    return {"control_status": control_status, "loaded_status": loaded_status, "coverage_complete": coverage,
            "all_cases_scored": all_scored, "cases": cases,
            "workload_note": "Repeated, warmed workload; quiet controls and traffic repeat requests and may benefit caches.",
            "unique_effective_requests": preview["unique_effective_requests"],
            "repeated_requests_per_target": preview["repeated_requests_per_target"]}


def _checks(run: _Run, target: Target) -> list[CheckResult]:
    config = run.config
    all_records = [r for r in run.records if r.target == target.name]
    ordinary = [r for r in all_records if r.scenario not in {"client_cancel", "client_deadline", "backend_cancel"}
                or _serving_failure(r, r.scenario)]
    observations = run.observations[target.name]
    result = []
    warm = [r for r in ordinary if r.scenario == "warm"]
    for check_id in CORE_CHECKS:
        title = CHECK_NAMES[check_id]
        if check_id == "load":
            from giraffe.load_test import result as load_result
            item = load_result(run, target) if check_id in run.config.checks else CheckResult(
                id=check_id, title=title, target=target.name, required=False, status="skipped", summary="Not selected.")
            if item.status == "inconclusive":
                item.summary += " To conclude, complete every selected sending window and collect the required valid, scored responses with complete delivery and timing evidence."
            result.append(item)
            continue
        config = _check_config(run.config, check_id)
        limits = config.limits
        optional = check_id == "gpu"
        records = [r for r in ordinary if check_id in r.check_ids]
        if check_id == "correctness" and run.pack_specs:
            records = [r for r in all_records if r.scenario in {"pack_control", "pack_loaded"}
                       or r.traffic_stage is not None or bool(r.load_point)]
            limits = limits.model_copy(update={"min_samples": 1})
        if check_id == "capacity":
            records = [r for r in records if r.traffic_stage is not None]
        if check_id == "cancellation":
            records = [r for r in all_records if check_id in r.check_ids]
        if check_id in {"first_output", "generation"}:
            # Only ordinary serial requests: loaded traffic belongs to its own checks.
            records = [r for r in records if r.scenario in {"warm", "generation"}]
        if check_id == "generation":
            records = [r for r in records if r.scenario == "generation"]
        metrics, status, summary = _stats(records), "pass", "Observed requests met configured checks."
        timing_records = records
        if check_id == "recovery":
            timing_records = [r for r in records if r.scenario in {"recovery_baseline", "recovery"}]
        quality = run_quality(run, timing_records, target.name, configured_metrics(limits, check_id))
        saturated = quality["status"] == "client_delayed"
        if check_id in {"first_output", "generation", "capacity", "recovery"}:
            metrics["timing_quality"] = quality
        if check_id not in config.checks:
            status, summary = "skipped", "Not selected for this run."
        elif observations["restart"] and observations["restart"]["status"] == "blocked":
            status, summary = "blocked", observations["restart"].get("reason", "Restart readiness unavailable")
        elif observations.get("client_error"):
            status, summary = "blocked", observations["client_error"]
        elif check_id == "gpu":
            from .telemetry import assess_gpu
            metrics, status, summary = assess_gpu(observations["metrics"], config.metrics_max_age_seconds,
                gpu_count=target.deployment.hardware.get("gpu_count"),
                gpu_max_temperature_c=limits.gpu_max_temperature_c)
            window = observations.get("gpu_observation_window")
            metrics["observation_window"] = window
            if window and not window["complete"] and status != "fail":
                status, summary = "inconclusive", ("GPU end scrape did not return fresh device samples; the observation window is incomplete."
                    if window.get("end_scrape_fresh") is False else "GPU observation window shorter than exporter refresh.")
        elif not records and check_id == "context" and observations.get("context_calibration", {}).get("status") == "unavailable":
            status, summary = "inconclusive", "Context preparation unavailable: " + observations["context_calibration"]["reason"]
            metrics.update(calibration=observations["context_calibration"], declared_context_limit=config.context_limit,
                           near_limit_coverage=False, observed_max_input_tokens=None)
        elif not records and check_id != "capacity":
            status, summary = "inconclusive", "No applicable requests completed within the run budget."
        elif check_id == "access":
            denied = [r for r in records if r.http_status in {401, 403}]
            disconnected = [r for r in records if r.http_status is None and r.status == "failed"
                            and any(name in (r.error or "") for name in
                                    ("ConnectError", "ConnectTimeout", "SSLError", "ConnectionError",
                                     "ProxyError", "UnsupportedProtocol"))]
            if denied or disconnected:
                status = "blocked" if not any(r.valid for r in records) else "fail"
                summary = "Authentication or transport continuity failed on the configured route."
            elif not any(r.valid for r in records):
                status, summary = "inconclusive", "No valid access response; timeout or inference failure observed."
            else:
                summary = "Configured route accepted requests; TLS trust is enforced for HTTPS."
            metrics["tls"] = "verified by transport" if target.url.startswith("https:") else "not applicable (HTTP)"
            metrics["tls_expiry"] = "certificate dates not separately inspected"
        elif check_id == "serving":
            if metrics["error_rate"] is not None and metrics["error_rate"] > limits.max_error_rate:
                status, summary = "fail", "Invalid completions, errors or timeouts exceeded the configured allowance."
            elif metrics["reasoning_budget_exhausted"]:
                status, summary = "inconclusive", "Reasoning exhausted the output budget before an answer."
            else:
                summary = "Valid non-empty responses and required stream termination observed."
        elif check_id in {"first_output", "generation"}:
            metrics["initial"] = _stats([r for r in ordinary if r.scenario in {"initial", "readiness"}])
            thresholds = (limits.first_output_ms is not None if check_id == "first_output" else
                          any(v is not None for v in (limits.latency_ms, limits.stream_gap_ms,
                                                      limits.min_output_tokens_per_second)))
            violations = _timing_violations(records, limits.model_copy(update={"first_output_ms": None}), unfinished=True)
            if check_id == "first_output":
                violations = [r.id for r in records if limits.first_output_ms and (
                    r.first_output_ms is not None and r.first_output_ms > limits.first_output_ms
                    or "first_output_ms" in exceeded_unfinished(r, limits))]
            assessed_limits = limits.model_copy(update={"latency_ms": None, "stream_gap_ms": None,
                "min_output_tokens_per_second": None}) if check_id == "first_output" else limits.model_copy(update={"first_output_ms": None})
            qualified = qualified_violations(records, assessed_limits, quality, unfinished=True)
            gap_samples = sum(r.valid and r.status == "completed" and r.measured_stream_gap for r in records)
            metrics["measured_gap_samples"] = gap_samples
            if qualified:
                status, summary = "fail", "Observed request timing exceeded a configured limit without overlapping client scheduling delay."
                metrics["violations"] = qualified
            elif violations and saturated:
                status, summary = "inconclusive", "Client scheduling delay prevents attributing observed timing violations to the endpoint."
                metrics["reason_code"] = "client_delayed"
                metrics["unattributed_timing_violations"] = violations
            elif violations:
                status, summary = "fail", "Observed request timing exceeded a configured limit."
                metrics["violations"] = violations
            elif not thresholds:
                status, summary = "inconclusive", "Timing observed; no acceptance limit configured."
                metrics["reason_code"] = "missing_acceptance_limit"
            elif saturated or metrics["completed"] < limits.min_samples:
                status, summary = "inconclusive", "Timing has too few successful samples or overlapping client scheduling delay."
                metrics["reason_code"] = "client_delayed" if saturated else "insufficient_timing_samples"
            elif metrics["reasoning_budget_exhausted"]:
                status, summary = "inconclusive", "Reasoning exhausted the output budget before an answer."
            elif check_id == "first_output" and any(r.valid and r.first_output_ms is None for r in records):
                status, summary = "inconclusive", "Useful first-output timing unavailable for some responses."
            elif check_id == "generation" and _missing_timing(records, limits.model_copy(update={"first_output_ms": None})):
                metrics["missing_timing_metrics"] = _missing_timing(records, limits.model_copy(update={"first_output_ms": None}))
                status, summary = "inconclusive", "Configured generation limits cannot be checked with available stream or token measurements."
            elif (check_id == "generation" and limits.min_output_tokens_per_second is not None
                  and metrics["generation_rate_samples"] < limits.min_samples):
                status, summary = "inconclusive", "Too few measurable streamed answers to assess generation pace."
            elif check_id == "generation" and limits.stream_gap_ms is not None and gap_samples < limits.min_samples:
                status, summary = "inconclusive", "Too few answers streamed over separate network reads to assess pauses."
        elif check_id == "capacity":
            stages = observations["capacity"]
            for stage in stages:
                stage_records = [r for r in records if r.traffic_stage == stage["stage_index"]]
                interval = stage.get("timing_interval_ms")
                stage_quality = run_quality(run, stage_records, target.name,
                    configured_metrics(limits), phase_intervals=(tuple(interval),) if interval else ())
                stage["timing_quality"] = stage_quality
                if stage_quality["status"] == "client_delayed":
                    stage["accepted"] = False
                    stage["generator_limited"] = True
                    # Missing judgments may still pass; preserve a correctness
                    # failure only when the full completion set cannot recover.
                    best_possible_correctness = (
                        (stage["correct"] + stage["unscored_completions"]) / stage["completed"]
                        if stage["completed"] else None
                    )
                    independent_failure = (
                        stage["error_rate"] is not None and stage["error_rate"] > limits.max_error_rate
                    ) or (best_possible_correctness is not None
                          and best_possible_correctness < limits.min_correctness) or bool(
                              qualified_violations(stage_records, limits, stage_quality, arrival=True))
                    if not independent_failure:
                        stage["status"] = "inconclusive"
                    stage["reasons"].append("Client scheduling delay overlapped this traffic stage; endpoint timing attribution is uncertain.")
            if any(stage["timing_quality"]["phase_affected"] for stage in stages):
                metrics["timing_quality"]["phase_affected"] = True
                metrics["timing_quality"]["status"] = "client_delayed"
            highest = None
            rejected = False
            non_monotonic = False
            for rate in config.traffic.rates:
                stage = next((stage for stage in stages if stage["rate_rps"] == rate), None)
                if not stage or not stage["accepted"]:
                    rejected = True
                elif rejected:
                    non_monotonic = True
                else:
                    highest = rate
            metrics["traffic_stages"] = stages
            metrics["highest_acceptable_rate_rps"] = highest
            metrics["configured_rates_rps"] = config.traffic.rates
            metrics["non_monotonic_results"] = non_monotonic
            metrics["scope"] = "Highest tested incoming rate for this scored canary mix; internal server queue time is unobserved."
            if any(stage["status"] == "fail" for stage in stages):
                status, summary = "fail", "Some incoming rates exceeded known-answer, error or timing limits."
            elif len(stages) != len(config.traffic.rates) or any(not stage["accepted"] for stage in stages):
                status, summary = "inconclusive", "The full rate plan needs more delivered, scored or timely evidence."
            else:
                summary = "Reported highest tested acceptable incoming rate; maximum deployment capacity is unknown."
        elif check_id in {"context", "correctness", "json"}:
            accuracy = metrics["correct"] / metrics["scored"] if metrics["scored"] else None
            metrics["accuracy"] = accuracy
            metrics["fixtures"] = sorted({r.fixture_id for r in records})
            if check_id == "context":
                observed_max = max((r.input_tokens for r in records if r.input_tokens is not None), default=None)
                metrics.update({"declared_context_limit": config.context_limit, "observed_max_input_tokens": observed_max,
                                "near_limit_coverage": observed_max is not None and observed_max >= config.context_limit * .8,
                                "calibration": observations.get("context_calibration")})
                coverage = {}
                for record in records:
                    if record.scenario == "context" and "position_coverage" in record.workload:
                        size = record.fixture_id.split(".")[1]
                        coverage[size] = record.workload["position_coverage"]
                metrics["position_coverage"] = coverage
                metrics["reduced_position_coverage"] = any(row["reduced"] for row in coverage.values())
                rejected = [r for r in records if _context_rejection(r)]
                disproving = sorted(tokens for r in rejected if (tokens := (
                    r.input_tokens if r.input_tokens is not None else
                    r.workload.get("calibrated_input_tokens") if r.workload.get("token_count_scope") not in {"message_content", "builtin_reference"} else None)) is not None
                    and tokens <= config.context_limit * (.85 if r.input_tokens is not None else .70))
                metrics["context_rejections"] = {"rejected": len(rejected), "calibrated_rejected_input_tokens": disproving}
                # Require every position at each observed near-limit size; probes cannot supply recall evidence.
                recall = [r for r in records if r.scenario in {"context", "context_calibration"}
                          and r.fixture_id.startswith("context.") and len(r.fixture_id.split(".")) >= 3]
                near_sizes = {(r.fixture_id.split(".")[1], r.workload.get("context_group", r.input_chars)) for r in recall
                              if r.input_tokens is not None and r.input_tokens >= config.context_limit * .8}
                near_positions, near_missed, near_unverified = {}, [], []
                for size, chars in sorted(near_sizes):
                    items = [r for r in recall if (r.fixture_id.split(".")[1], r.workload.get("context_group", r.input_chars)) == (size, chars)]
                    required = {position for r in items for position in
                                r.workload.get("position_coverage", {}).get("positions", ["beginning", "middle", "end"])}
                    positions = {}
                    for position in sorted(required):
                        samples = [r for r in items if r.fixture_id.split(".")[2] == position]
                        row = {"attempted": len(samples), "scored": sum(r.score is not None for r in samples),
                               "correct": sum(r.score is True for r in samples)}
                        positions[position] = row
                        input_label = f"{chars} chars" if isinstance(chars, int) else chars
                        label = f"{size}/{input_label}/{position}"
                        if row["scored"] > row["correct"]:
                            near_missed.append(label)
                        if (not samples or row["scored"] < len(samples) or any(
                                r.input_tokens is None or r.input_tokens < config.context_limit * .8 for r in samples)):
                            near_unverified.append(label)
                    near_positions[f"{size}:{chars}"] = positions
                metrics["near_limit_positions"] = near_positions
                metrics["near_limit_coverage"] = bool(near_sizes)
            if check_id in {"correctness", "json"} and not run.pack_specs:
                metrics["configured_concurrency"] = config.concurrency
                delays = scheduling_delay_windows(run)
                metrics["evidenced_overlap"] = max_evidenced_overlap((r for r in records if r.score is not None), all_records, delays)
                metrics["evidenced_overlap_capped_at"] = 2
            if check_id == "context" and not records and observations.get("context_calibration", {}).get("status") == "unavailable":
                status, summary = "inconclusive", "Context preparation unavailable: " + observations["context_calibration"]["reason"]
            elif check_id == "context" and any(_serving_failure(r, r.scenario) for r in records):
                status, summary = "fail", "Context prompts encountered HTTP or protocol failures."
            elif check_id == "context" and disproving:
                status, summary = "fail", (f"Server rejected a prompt sized at {disproving[0]} tokens, below the safety margin "
                                           f"for declared context_limit {config.context_limit} (70% estimated, 85% server-reported).")
            elif accuracy is not None and accuracy < limits.min_correctness:
                status, summary = "fail", "Local known-answer or schema scoring failed the configured correctness limit."
            elif check_id in {"correctness", "json"} and not run.pack_specs and config.concurrency > 1 and metrics["evidenced_overlap"] < 2:
                metrics["reason_code"] = "missing_concurrent_answers"
                status, summary = "inconclusive", ("Known-answer checks were not exercised concurrently: no scored answer's streamed response "
                    "overlapped another outside recorded client scheduling delays" + (f"; {NEEDS_STREAM}." if not config.stream else
                                                             " (client overlap alone does not show server concurrency)."))
            elif check_id == "context" and near_missed:
                status, summary = "fail", f"Near-limit recall missed the fact at position(s): {', '.join(near_missed)}."
            elif check_id == "context" and rejected:
                status, summary = "inconclusive", ("server rejected a context prompt without calibrated size evidence below the safety margin "
                                                   "(70% estimated, 85% server-reported); the declared context_limit remains unverified.")
            elif check_id == "correctness" and run.pack_specs and any(spec.scorer == "none" for spec in run.pack_specs):
                metrics["reason_code"] = "scoring_disabled"
                status, summary = "inconclusive", "Some request-pack cases have scoring disabled; answer correctness cannot be established."
            elif metrics["completed"] != metrics["attempted"] or metrics["scored"] < metrics["completed"] or not metrics["scored"]:
                status, summary = "inconclusive", "Some fixtures lack a score because inference or answer evaluation did not complete."
            elif check_id == "context" and metrics["reduced_position_coverage"]:
                status, summary = "inconclusive", "reduced fact-position coverage (context_limit too small)"
            elif check_id == "context" and not metrics["near_limit_coverage"]:
                status, summary = "inconclusive", "Recall fixtures passed at observed lengths; near-limit token context was not verified."
            elif check_id == "context" and near_unverified:
                status, summary = "inconclusive", f"Near-limit recall was not verified at position(s): {', '.join(near_unverified)}."
            else:
                summary = ("Built-in deterministic checks passed under observed concurrency."
                           if check_id in {"correctness", "json"} and config.concurrency > 1
                           else "Built-in deterministic checks passed for the recorded fixtures.")
        elif check_id == "cancellation":
            import re as _re
            cancelled = [r for r in all_records if r.scenario == "client_cancel"]
            deadlines = [r for r in all_records if r.scenario == "client_deadline"]
            probes = [r for r in records if r.scenario == "cancellation_probe"]
            baseline = next((r for r in records if r.scenario == "cancellation_baseline"), None)
            # Reasoning spent the whole output budget: the cap held, but there is no answer to judge.
            budget = {r.id for r in records if reasoning_budget(r)}
            metrics.update({"intentional_cancellations": len(cancelled),
                            "client_disconnect_observed": sum(r.status == "cancelled" for r in cancelled),
                            "backend_cancellation": observations.get("backend_cancellation", {"status": "unverified", "summary": "Backend verification was not enabled."}), "probes": _stats(probes),
                            "reasoning_budget_request_ids": sorted(budget),
                            "client_deadline_self_test": {
                                "deadline_ms": observations.get("deadline_probe_ms"), "requests": _stats(deadlines),
                                "timed_out": bool(deadlines) and all(r.status == "timeout" for r in deadlines),
                                "scope": "Exercises Giraffe's own client timer; nothing deadline-related reaches the server, so it is not endpoint evidence."}})
            cap_usage = {r.id: r.completion_tokens if r.completion_tokens is not None else r.output_tokens for r in records}
            caps = [r for r in records if r.fixture_id.startswith("limits.output_cap")]
            # Without usage, each streamed answer chunk and each fixture number is at least one token.
            cap_bound = {r.id: max(r.answer_chunks or 0, len(_re.findall(r"\d+", r.output or ""))) for r in caps}
            bad_caps = [r.id for r in caps if r.requested_max_tokens and (
                cap_bound[r.id] > r.requested_max_tokens if cap_usage[r.id] is None else
                cap_usage[r.id] > r.requested_max_tokens or (r.finish_reason == "length"
                                                              and cap_usage[r.id] != r.requested_max_tokens))]
            unverified_caps = [r.id for r in caps if cap_usage[r.id] is None and r.id not in bad_caps
                               and (r.finish_reason != "length" or not cap_bound[r.id])]
            controls = [r for r in records if r.fixture_id.startswith("limits.stop_control")]
            stops = [r for r in records if r.fixture_id.startswith("limits.stop.")]
            reached = any(r.valid and r.output.strip() == "blue [END] green" for r in controls)
            stop_failed = any("[END]" in r.output or (reached and "green" in r.output) for r in stops if r.id not in budget)
            stop_observed = reached and bool(stops) and all(
                r.valid and r.output.strip() == "blue" and r.finish_reason == "stop" for r in stops)
            metrics["stop_enforcement"] = {"control_reached_stop": reached,
                "status": "fail" if stop_failed else "pass" if stop_observed else "inconclusive"}
            metrics["output_cap_finish_reasons"] = [r.finish_reason for r in caps]
            metrics["output_cap_token_evidence"] = [{"id": r.id, "usage": cap_usage[r.id], "lower_bound": cap_bound[r.id],
                                                     "requested_max_tokens": r.requested_max_tokens} for r in caps]
            failed_limits = [r.id for r in records if r.scenario == "limits" and not r.valid
                             and r.status != "cancelled" and r.id not in budget]
            # Follow-up delay against a same-fixture baseline exposes leaked generation on shared slots.
            first_probe = next((r for r in probes if baseline and r.fixture_id == baseline.fixture_id), None)
            before = baseline.first_output_ms if baseline and baseline.valid else None
            after = first_probe.first_output_ms if first_probe and first_probe.valid else None
            threshold = max(3 * before, before + 1000) if before is not None else None
            delayed = threshold is not None and after is not None and after > threshold
            metrics["followup_delay"] = {"baseline_first_output_ms": before, "followup_first_output_ms": after,
                                         "threshold_ms": threshold, "delayed": delayed}
            if bad_caps or failed_limits or stop_failed or any(
                    r.score is False for r in records if r.scenario != "client_deadline" and r.id not in budget) or any(
                    not r.valid and r.status != "cancelled" and r.id not in budget for r in probes):
                status, summary = "fail", "Output/stop limits or inference after client cancellation failed."
                metrics["output_cap_violations"] = bad_caps
                metrics["failed_limit_requests"] = failed_limits
            elif not cancelled or not all(r.status == "cancelled" and any(
                                              value is not None and 0 <= value <= (
                                                  r.client_cancelled_ms if r.client_cancelled_ms is not None else r.elapsed_ms)
                                              for value in (r.first_output_ms, r.first_reasoning_ms))
                                              and r.client_cancelled_at_ns is not None and r.finish_reason is None
                                              and (r.generation_chunks or r.answer_chunks) >= run.config.effective_check("cancellation")["cancel_after_chunks"] for r in cancelled) or not probes:
                status, summary = "inconclusive", "Client disconnect or follow-up inference could not be exercised."
            elif any(r.id in budget for r in probes) or (baseline and baseline.id in budget):
                status, summary = "inconclusive", "Reasoning exhausted the follow-up output budget before an answer; follow-up inference is unverified."
            elif not stop_observed:
                status, summary = "inconclusive", ("Reasoning exhausted the stop fixture's output budget before an answer; stop enforcement is unverified."
                                                   if any(r.id in budget for r in [*stops, *controls]) else
                                                   "Server stop enforcement lacks a matching full-output control or stop finish reason.")
            elif unverified_caps:
                status, summary = "inconclusive", "Output token usage or a usable token bound is missing; output cap compliance is unverified."
            elif delayed:
                status, summary = "inconclusive", "follow-up delayed after cancellation; server may still be generating cancelled requests."
            elif threshold is None or after is None:
                status, summary = "inconclusive", "Follow-up first output could not be compared with a pre-cancellation baseline."
            else:
                summary = "Client cancellation, output limits and prompt follow-up probes observed; server work cancellation is unverified."
            if not config.stream:
                status, summary = "inconclusive", "client cancellation requires streaming"
            backend = metrics["backend_cancellation"]
            from giraffe.backend_cancellation import server_stop_result
            server_stop = server_stop_result(backend)
            answers = [r for r in records
                       if r.scenario not in {"client_cancel", "client_deadline", "backend_cancel"}
                       and (r.scenario == "cancellation_probe" or r.score is not None)]
            scored = [r for r in answers if r.score is not None]
            answer_status = "fail" if any(r.score is False for r in scored) else (
                "pass" if scored and len(scored) == len(answers) else "inconclusive")
            metrics["cancellation_results"] = {
                "server_stopped": server_stop,
                "answer": {"title": "Cancellation — verify answer", "status": answer_status,
                    "summary": "Completed stop/follow-up answers are scored separately; interrupted partial outputs are not graded.",
                    "correct": sum(r.score is True for r in scored), "scored": len(scored),
                    "attempted": len(answers), "evidence_ids": [r.id for r in answers]},
            }
            if backend["status"] == "fail":
                status, summary = "fail", "Backend cancellation or follow-up inference failed."
            elif status == "pass" and backend["status"] in {"inconclusive", "blocked"}:
                status, summary = backend["status"], backend["summary"]
            elif status == "pass" and backend["status"] == "pass":
                summary = "Output/stop limits, client interruption, backend capacity release and follow-ups passed."
                if server_stop["status"] != "pass":
                    status, summary = server_stop["status"], server_stop["summary"]
            if status == "pass" and answer_status == "inconclusive":
                status, summary = "inconclusive", "Some completed answer fixtures could not be scored."
        elif check_id == "recovery":
            sustained = [r for r in records if r.scenario == "sustained"]
            probes = [r for r in records if r.scenario == "recovery"]
            baseline = [r for r in records if r.scenario == "recovery_baseline"]
            before, after = _stats(baseline), _stats(probes)
            latency_ratio = after["p50_ms"] / before["p50_ms"] if after["p50_ms"] is not None and before["p50_ms"] else None
            first_ratio = after["first_output_p50_ms"] / before["first_output_p50_ms"] if after["first_output_p50_ms"] is not None and before["first_output_p50_ms"] else None
            ratio_failed = limits.recovery_max_ratio is not None and any(
                ratio is not None and ratio > limits.recovery_max_ratio for ratio in (latency_ratio, first_ratio))
            windows = [sustained[i:i+config.concurrency] for i in range(0, len(sustained), config.concurrency)]
            probe_stats = _stats(probes)
            accuracy = metrics["correct"] / metrics["scored"] if metrics["scored"] else None
            # Deadline/stop cancellations are unfinished evidence, not endpoint errors.
            evaluated = [r for r in sustained if r.status != "cancelled" and not _reasoning_budget(r)]
            sustained_errors = sum(r.status != "completed" or not r.valid for r in evaluated)
            sustained_error_rate = sustained_errors / len(evaluated) if evaluated else 0
            sustained_stats = _stats(sustained)
            # Light-load failures before any sustained traffic are endpoint failures;
            # timeouts and cancellations there only leave the baseline incomplete.
            baseline_evaluated = [r for r in baseline if r.status != "cancelled" and not _reasoning_budget(r)]
            baseline_error_rate = (sum(r.status == "failed" or (r.status == "completed" and not r.valid)
                                       for r in baseline_evaluated) / len(baseline_evaluated)
                                   if baseline_evaluated else None)
            metrics.update({"sustained": sustained_stats, "recovery": after,
                            "baseline": before, "latency_ratio": latency_ratio, "first_output_ratio": first_ratio,
                            "sustained_budget": observations.get("sustained_budget"), "accuracy": accuracy,
                            "unscored_completions": sum(r.status == "completed" and r.valid and r.score is None
                                                        for r in probes + sustained),
                            "baseline_non_timeout_error_rate": baseline_error_rate,
                            "sustained_error_rate": sustained_error_rate,
                            "sustained_non_timeout_error_rate": sum(r.status not in {"completed", "timeout", "cancelled"}
                                or (r.status == "completed" and not r.valid) for r in evaluated) / len(evaluated) if evaluated else 0,
                            "sustained_completion": observations.get("sustained_completion"),
                            "rolling_windows": [_stats(window) for window in windows]})
            if ((probe_stats["error_rate"] is not None and probe_stats["error_rate"] > limits.max_error_rate)
                    or sustained_error_rate > limits.max_error_rate
                    or (baseline_error_rate is not None and baseline_error_rate > limits.max_error_rate)
                    or (qualified_violations(probes, limits, quality) or (ratio_failed and not saturated))
                    or (accuracy is not None and accuracy < limits.min_correctness)):
                status, summary = "fail", "Repeated traffic or light-load recovery failed correctness or configured limits."
                if baseline_error_rate is not None and baseline_error_rate > limits.max_error_rate:
                    summary = "Light-load baseline requests failed before sustained traffic; repeated traffic or recovery limits were not met."
            elif metrics["reasoning_budget_exhausted"]:
                status, summary = "inconclusive", "Reasoning exhausted the output budget before an answer; recovery is unverified."
            elif metrics["cancelled"]:
                status, summary = "inconclusive", "Sustained or recovery requests were cancelled by the run deadline or a stop; recovery was not fully observed."
            elif accuracy is None or metrics["unscored_completions"]:
                status, summary = "inconclusive", "Some sustained or recovery completions lack a local correctness score."
            elif saturated and any((limits.first_output_ms, limits.latency_ms, limits.stream_gap_ms,
                                        limits.min_output_tokens_per_second, limits.recovery_max_ratio)):
                status, summary = "inconclusive", "Client scheduling delay prevents attributing sustained and recovery timing to the endpoint."
                metrics["reason_code"] = "client_delayed"
            elif _missing_timing(probes, limits):
                metrics["missing_timing_metrics"] = _missing_timing(probes, limits)
                status, summary = "inconclusive", "Configured recovery limits cannot be checked with available timing or token measurements."
            elif (not observations.get("sustained_complete") or metrics["sustained"]["completed"] < limits.min_samples
                  or after["completed"] < config.samples
                  or before["completed"] < config.samples):
                status, summary = "inconclusive", "Sustained duration or light-load recovery was not fully observed."
            elif limits.recovery_max_ratio is not None and (latency_ratio is None or first_ratio is None):
                status, summary = "inconclusive", "Recovery ratio requires matching latency and first-output baseline evidence."
            elif not any((limits.recovery_max_ratio, limits.first_output_ms, limits.latency_ms,
                          limits.stream_gap_ms, limits.min_output_tokens_per_second)):
                status, summary = "inconclusive", "Recovery observed; no acceptance limit configured."
                metrics["reason_code"] = "missing_acceptance_limit"
            else:
                summary = "Bounded sustained traffic and recovery completed; unobserved internal paths remain unverified."
                if limits.recovery_max_ratio is None:
                    summary += (f" Probe/baseline median ratios: latency {latency_ratio}, first output {first_ratio}; "
                                "no ratio limit configured.")
        if run.pack_specs and check_id in {"capacity", "correctness"} and check_id in run.config.checks:
            pack = _pack_results(run, target)
            metrics["request_pack"] = pack
            if check_id == "capacity":
                metrics["scope"] = "Highest tested incoming rate for this request pack; repeated requests may benefit caches."
                if not pack["coverage_complete"] or not pack["all_cases_scored"]:
                    metrics["highest_acceptable_rate_rps"] = None
            if pack["control_status"] == "fail" and status != "blocked":
                status, summary = "fail", "Quiet request-pack checks failed; loaded-stage outcomes are reported separately."
            elif pack["control_status"] != "pass" and status == "pass":
                status, summary = "inconclusive", "Quiet request-pack coverage is incomplete or includes unscored cases."
            elif check_id == "correctness":
                if pack["loaded_status"] == "fail":
                    status, summary = "fail", "Loaded request-pack answers failed the per-case correctness limit."
                elif pack["loaded_status"] != "pass" and status == "pass":
                    status, summary = "inconclusive", "Request-pack cases lack complete scored concurrent evidence."
                elif status == "pass":
                    summary = ("Every request-pack case passed quiet and loaded scoring."
                               if config.concurrency > 1 else "Every request-pack case passed quiet and serial scoring.")
        if status == "fail" and check_id in {"capacity", "cancellation", "recovery"}:
            answer_failures = _answer_failures(records, warm)
            if check_id == "recovery" and metrics["accuracy"] is not None and metrics["accuracy"] >= limits.min_correctness:
                answer_failures = []
            if answer_failures:
                metrics["answer_failures"] = answer_failures
                details = "; ".join(f"{row['fixture_id']} ({row['failed']}/{row['scored']})"
                                    for row in answer_failures)
                summary = f"Answer checks failed: {details}."
                if any(row["warmup_failed"] for row in answer_failures):
                    summary += " Some of these fixtures also failed during warm-up."
                ordinary_records = [r for r in records if r.scenario not in {
                    "client_cancel", "client_deadline", "backend_cancel",
                }]
                if any(not r.valid or r.status != "completed" for r in ordinary_records):
                    summary += " Request or protocol failures were also observed."
                timing_failed = False
                if check_id in {"capacity", "recovery"}:
                    timing_failed = bool(_timing_violations(ordinary_records, limits))
                if timing_failed and not saturated:
                    summary += " Configured timing limits exceeded."
                if check_id == "cancellation" and metrics.get("output_cap_violations"):
                    summary += " Output token caps were also exceeded."
        if check_id == "cancellation" and "cancellation_results" in metrics:
            outcomes = metrics["cancellation_results"]
            summary = (f"Server stopped: {outcomes['server_stopped']['status']}; "
                       f"answer check: {outcomes['answer']['status']}. {summary}")
        if status == "pass" and check_id in run.unfinished[target.name]:
            status, summary = "inconclusive", "This check was unfinished when a run budget or stop condition was reached."
        if metrics.get("completed", 0) < limits.min_samples and status not in {"skipped", "blocked"}:
            metrics["low_confidence"] = True
        result.append(CheckResult(id=check_id, target=target.name, title=title, status=status,
                                  required=not optional and check_id in config.checks,
                                  summary=summary, metrics=metrics,
                                  evidence_ids=[r.id for r in records]))
    from giraffe.workloads import results
    workload_results = results(run, target, _stats, _timing_violations, _missing_timing)
    for check in workload_results:
        if check.id == "consistency" and check.status == "inconclusive" and "logprobs" in check.metrics:
            reasons = dict.fromkeys(reason for row in check.metrics["logprobs"]["conditions"] for reason in row["coverage_reasons"])
            check.summary += "".join(" " + reason[:1].upper() + reason[1:].rstrip(".") + "."
                                     for reason in reasons if reason.lower().rstrip(".") not in check.summary.lower())
    from .reporting import connection_summary
    budget = getattr(run, "verdict_budget_guidance", None) or _budget_guidance(run.config)
    disconnected = connection_summary(all_records, target.url)
    for check in result + workload_results:
        if check.status not in {"fail", "inconclusive", "blocked"}:
            continue
        config = _check_config(run.config, check.id)
        evidence = set(check.evidence_ids)
        records = [r for r in all_records if r.id in evidence]
        if check.id not in {"load", "concurrency"}:
            check.summary = _verdict_summary(check, config, records, budget)
        if disconnected:
            check.summary = (f"{disconnected}. {check.summary} "
                             "Start the server and verify the target URL, port, proxy and TLS settings, then rerun.")
    return result + workload_results


def _budget_guidance(config, check_id=None):
    if config.max_requests is None:
        return ""
    from .planning import request_plan
    plan = request_plan(config)
    if check_id in {"load", "concurrency"}:
        return ("Allow the configured Load test sending windows, local preparation, and outstanding request timeouts; "
                "set the request limit (Stop after this many requests) using observed throughput. Duration-based requests have no fixed count.")
    planned = plan["required_request_limit"] or max(
        plan["known_total"], (plan["conservative_requests_per_target"]
                              - plan["adaptive_estimates"].get("concurrency", 0)) * len(config.targets))
    amount = f"to at least {planned}" if planned > config.max_requests else f"above {config.max_requests} if exhausted"
    return (f"Raise max_requests (or --max-requests) {amount}, "
            f"or select fewer checks. The plan needs at least {plan['known_total']} fixed requests; adaptive traffic may need more.")


def _output_budget_guidance(config, records, check_id):
    capped = [r for r in records if _reasoning_budget(r) or r.finish_reason == "length"]
    owner = "capacity" if config.request_source == "request_pack" and "capacity" in config.checks else check_id
    options = config.test_options.get(owner)
    field = f"test_options.{owner}.max_output_tokens" if options and options.max_output_tokens is not None else "max_output_tokens"
    value = config.max_output_tokens
    if check_id == "sessions":
        field, value = "sessions.max_tokens", config.sessions.max_tokens
        for index, workload in enumerate(config.sessions.workloads):
            if workload.max_tokens is not None and capped and all(
                    record.workload.get("task_type") == workload.name for record in capped):
                field, value = f"sessions.workloads.{index}.max_tokens", workload.max_tokens
                break
    if capped and all(record.workload.get("request_source") == "request_pack" for record in capped):
        cases = {case.fixture_id: (index, case) for index, case in enumerate(config.request_pack.cases)}
        explicit = {cases[record.workload["case_id"]][0] for record in capped
                    if record.workload.get("case_id") in cases
                    and cases[record.workload["case_id"]][1].max_tokens is not None}
        if explicit and len(explicit) == 1:
            index = explicit.pop()
            field, value = f"request_pack.cases.{index}.max_tokens", config.request_pack.cases[index].max_tokens
    elif capped and all(record.workload.get("request_source") == "custom_fixtures" for record in capped):
        if config.legacy_custom_fixture_max_output_tokens is not None:
            field, value = "legacy_custom_fixture_max_output_tokens", config.legacy_custom_fixture_max_output_tokens
        else:
            field, value = "custom_fixtures request max_tokens", max(record.requested_max_tokens for record in capped)
    if capped and all(r.requested_max_tokens < value for r in capped):
        caps = ", ".join(str(value) for value in sorted({r.requested_max_tokens for r in capped}))
        return (f"the affected fixtures request {caps} tokens below the configured default; increasing {field} alone will not "
                "change those requests. Configure the model to return the final fixture answer without extra reasoning within its requested cap, then rerun")
    setting = field + (" (or --max-output-tokens)" if field == "max_output_tokens" else "")
    return (f"raise {setting} above {value} for requests using that output cap, "
            "or configure the model to produce the final answer within its requested cap, then rerun")


def _verdict_summary(check, config, records, budget):

    summary, metrics, limits = check.summary, check.metrics, config.limits
    if check.id == "concurrency":
        from .concurrent_load import remedies
        guidance = remedies(check.metrics, config)
        return check.summary if not guidance or "To conclude," in check.summary else check.summary.rstrip(".") + ". To conclude, " + guidance + "."
    answer_failed = False
    reason = metrics.get("reason_code")
    text = summary.lower() if check.status != "fail" else ""
    if check.status == "fail":
        details = []
        for stage in metrics.get("traffic_stages", []):
            if stage["status"] != "fail":
                continue
            prefix = f"at {stage['rate_rps']:g} requests/s, "
            if stage.get("error_rate") is not None and stage["error_rate"] > limits.max_error_rate:
                details.append(f"{prefix}error rate {stage['error_rate']:.1%} > allowed {limits.max_error_rate:.1%}")
            if stage.get("correctness") is not None and stage["correctness"] < limits.min_correctness:
                details.append(f"{prefix}correctness {stage['correctness']:.1%} < allowed {limits.min_correctness:.1%}")
            if not details:
                details.append(prefix + "; ".join(stage.get("reasons", ["stage did not meet configured acceptance limits"])))
            break
        if check.id == "gpu":
            for alert in metrics.get("alerts", [])[:2]:
                detail = f"GPU {alert['gpu']}: {', '.join(alert['reasons'])}"
                if alert["family"] == "temperature":
                    detail += f" ({alert['value']:g} °C > allowed {limits.gpu_max_temperature_c:g} °C)"
                elif alert.get("value") is not None:
                    detail += f" (value {alert['value']})"
                details.append(detail)
        if check.id == "consistency":
            agreements = [row["agreement_rate"] for row in metrics.get("cross_level", [])
                          if row.get("agreement_rate") is not None]
            if agreements and min(agreements) < 1:
                details.append(f"answer agreement {min(agreements):.1%} < required 100.0% under strict consistency")
        rates = [("error rate", metrics.get("error_rate"), limits.max_error_rate, False),
                 ("correctness", metrics.get("accuracy", metrics.get("correctness_observed")), limits.min_correctness, True)]
        if check.id == "recovery":
            rates.extend((name, metrics.get(key), limits.max_error_rate, False) for name, key in (
                ("sustained error rate", "sustained_error_rate"),
                ("baseline error rate", "baseline_non_timeout_error_rate")))
        for name, value, allowed, minimum in rates:
            if value is not None and (value < allowed if minimum else value > allowed):
                details.append(f"{name} {value:.1%} {'<' if minimum else '>'} allowed {allowed:.1%}")
        measured = records
        if check.id == "recovery":
            measured = [r for r in records if r.scenario == "recovery"]
        quality = metrics.get("timing_quality", {})
        if quality.get("status") == "client_delayed":
            ids = qualified_violations(measured, limits, quality, unfinished=True)
            measured = [r for r in measured if r.id in ids]
        timing = [("stream gap", "max_stream_gap_ms", limits.stream_gap_ms, "ms", False),
                  ("latency", "elapsed_ms", limits.latency_ms, "ms", False),
                  ("first output", "first_output_ms", limits.first_output_ms, "ms", False),
                  ("generation pace", "generation_tokens_per_second", limits.min_output_tokens_per_second, "tokens/s", True)]
        for name, field, allowed, unit, minimum in timing:
            if allowed is None or (check.id == "first_output" and field != "first_output_ms") or (
                    check.id == "generation" and field == "first_output_ms"):
                continue
            values = [value for r in measured if (value := getattr(r, field)) is not None
                      and (r.valid or field in exceeded_unfinished(r, limits))]
            value = (min(values) if minimum else max(values)) if values else None
            if value is not None and (value < allowed if minimum else value > allowed):
                details.append(f"{name} {value:g} {unit} {'<' if minimum else '>'} allowed {allowed:g} {unit}")
        if quality.get("status") != "client_delayed":
            for key in ("latency_ratio", "first_output_ratio"):
                allowed = limits.recovery_max_ratio
                value = metrics.get(key)
                if allowed and value is not None and value > allowed:
                    name = key.replace("_", " ")
                    details.append(f"{name} {value:.3g} > allowed {allowed:g}")
        for record in records:
            if record.id in metrics.get("output_cap_violations", []):
                value = record.completion_tokens if record.completion_tokens is not None else record.output_tokens
                details.append(f"output cap violated: {value if value is not None else 'token lower bound'} tokens; requested {record.requested_max_tokens}")
                break
        if metrics.get("stop_enforcement", {}).get("status") == "fail":
            details.append("stop output contained text at or after the configured [END] stop string")
        if not details:
            failed = next((r for r in records if r.status in {"failed", "timeout"} or not r.valid), None)
            if failed:
                details.append(f"{failed.fixture_id}: {failed.error or ('HTTP ' + str(failed.http_status) if failed.http_status else 'invalid or unfinished response')}")
        if details:
            summary += " Observed: " + "; ".join(details[:4]) + "."
    if answer_failed and not metrics.get("answer_failures"):
        failures = _answer_failures(records, [])
        if failures:
            summary += " Answer checks failed: " + "; ".join(
                f"{row['fixture_id']} ({row['failed']}/{row['scored']})" for row in failures) + "."
    if check.status == "fail" or answer_failed:
        mismatch = next((r for r in records if r.score is False and r.evaluation
                         and r.evaluation.expected_provided), None)
        if mismatch:
            from .fixtures import answer_difference, final_answer
            summary += answer_difference(mismatch.evaluation.expected, final_answer(mismatch.output)[0])

    samples = max(2, config.samples, limits.min_samples)
    if check.id == "gpu":
        # GPU samples are exporter readings, never scored completion answers.
        remedy = ("configure a reachable GPU metrics exporter with fresh per-device samples "
                  f"and allow at least {config.gpu_min_observation_seconds:g} seconds for observation; "
                  "inspect reported device errors before rerunning")
    elif reason == "missing_concurrent_answers":
        summary += (" Plain words: answers could not be checked under concurrent load. The server appears to handle one request at a time, "
                    "or the recorded streaming/client timing did not expose overlapping answers."
                    if config.stream else " Plain words: streaming was disabled, so answers could not be checked under concurrent load; server parallelism is unverified.")
        remedy = ("enable parallel serving on the server (for example OLLAMA_NUM_PARALLEL=2 for Ollama, or the engine's equivalent), "
                  "restart the server to apply the configuration and rerun with streaming (stream: true, omit --nonstream); "
                  "or rerun with --concurrency 1 to check answers sequentially, which does not test concurrent load")
    elif check.status == "inconclusive" and (reason == "client_delayed" or (
            reason is None and "scheduling delay" in text and metrics.get("timing_quality", {}).get("status") == "client_delayed")):
        remedy = "rerun from an idle load-generator machine and reduce competing client work so streaming timing can be measured"
    elif check.status == "inconclusive" and metrics.get("reasoning_budget_exhausted"):
        remedy = _output_budget_guidance(config, records, check.id)
    elif reason == "insufficient_timing_samples":
        setting = "samples (or --samples)" + (" and any test_options.generation.samples override" if check.id == "generation" else "")
        remedy = (f"set {setting} to at least {limits.min_samples}, "
                  "then allow those serial requests to complete within the request and time budgets")
    elif check.status == "inconclusive" and (reason == "missing_acceptance_limit" or (check.id, summary) in {
            ("arrivals", "Arrivals observed; no acceptance limit configured.")}):
        options = {
            "first_output": ("first_output_ms",),
            "generation": ("latency_ms", "stream_gap_ms", "min_output_tokens_per_second"),
            "arrivals": ("latency_ms", "first_output_ms", "stream_gap_ms", "min_output_tokens_per_second"),
            "recovery": ("recovery_max_ratio", "first_output_ms", "latency_ms", "stream_gap_ms", "min_output_tokens_per_second"),
        }[check.id]
        option = " or ".join(f"limits.{name}" for name in options)
        remedy = f"set {option} to your service's acceptance target and rerun"
    elif reason == "scoring_disabled":
        remedy = "configure an answer scorer for each unscored request-pack case, then complete its quiet and loaded passes; performance measurements remain available without answer scoring"
    elif metrics.get("request_pack") and check.status == "inconclusive" and (
            not metrics["request_pack"]["coverage_complete"] or not metrics["request_pack"]["all_cases_scored"]):
        remedy = ("finish quiet and loaded scoring for every request-pack case and inspect any evaluator errors or unfinished answers; "
                  f"global samples does not select the case count. {budget}")
    elif check.id == "capacity" and check.status == "inconclusive":
        stages = metrics.get("traffic_stages", [])
        needed = max(limits.min_samples, 80 if any(stage.get("stability_unverified") for stage in stages) else 0)
        duration = max(config.traffic.duration_seconds, needed / min(config.traffic.rates))
        remedy = (f"allow traffic.duration_seconds of at least {duration:g} seconds per rate for at least {needed} scored completions; "
                  "set limits.first_output_ms or limits.latency_ms, enable stream/token measurements, "
                  f"and allow the whole rate plan to finish. {budget}")
    elif check.status == "inconclusive" and any(_reasoning_budget(r) for r in records):
        remedy = _output_budget_guidance(config, records, check.id)
    elif check.id == "context":
        remedy = (f"set context_limit to the server's actual token window (currently {config.context_limit}), "
                  "enable prompt token usage reporting and rerun all fact positions with enough output tokens for complete answers")
    elif check.id == "recovery" and summary in {
            "Sustained duration or light-load recovery was not fully observed.",
            "Sustained or recovery requests were cancelled by the run deadline or a stop; recovery was not fully observed."}:
        remedy = (f"allow {config.sustained_seconds:g} seconds of sustained traffic with at least {limits.min_samples} successful sustained requests "
                  f"and {config.samples} successful requests in each baseline and recovery batch; "
                  + (f"raise max_duration_seconds (or --max-duration) above {config.max_duration_seconds:g} if the deadline stops the run. "
                     if config.max_duration_seconds is not None else "") + budget)
    elif any(word in text for word in ("run budget", "run deadline", "no applicable")):
        remedy = budget + (" Allow more max_duration_seconds (or --max-duration) if the run deadline stops traffic"
                          if config.max_duration_seconds is not None else "")
    elif any(word in text for word in ("overlap", "concurrent")) and check.status == "inconclusive":
        remedy = ("enable parallel serving on the server and streaming (stream: true, omit --nonstream), "
                  "disable response buffering along the route, and rerun the same workload; raising client concurrency alone cannot make a serial server process requests in parallel")
    elif check.id == "cancellation" and not config.stream:
        remedy = "enable streaming (stream: true, omit --nonstream) so the client can observe output and interrupt the request, then rerun cancellation"
    elif any(word in text for word in ("stream", "timing", "measurable", "measured", "token measurements", "first-output baseline")) and check.status == "inconclusive":
        remedy = (f"enable streaming (stream: true, omit --nonstream), disable response buffering along the route and enable completion token usage reporting; "
                  f"collect at least {limits.min_samples} responses with the required timing measurements, then rerun the same workload")
    elif "sample" in text or "score" in text or "scor" in text:
        remedy = (f"collect at least {samples} complete scored answers per tested condition; "
                  "check the fixture's expected answer and evaluator errors, increase the output budget for truncated answers, then rerun")
    else:
        remedy = {
            "access": "verify server availability, target URL, credentials, proxy and TLS trust, then rerun",
            "serving": "inspect the failed request's HTTP/protocol error, fix the server response or timeout cause, then rerun",
            "first_output": "inspect slow requests and server load, fix the first-output delay, then rerun against the same timing limit",
            "generation": "inspect slow or buffered streams and server load, fix generation delays, then rerun against the same limits",
            "correctness": "compare the expected and received answer, check the prompt and model's answer format, fix the mismatch, then rerun the same fixtures",
            "json": "compare the expected and received JSON and the schema violation, fix the model's structured output, then rerun the same fixtures",
            "capacity": "inspect the failing traffic stage and request errors, address server overload or wrong answers, then rerun the same rate plan",
            "cancellation": "enable streaming and completion token usage, verify server stop and cancellation support, then rerun the control and follow-up probes",
            "recovery": "inspect sustained-load errors and slow recovery probes, address the server issue, then rerun the same sustained workload",
            "prefix": "enable cached-token usage reporting, check the server's prefix-cache configuration and rerun the repeat and independent controls",
            "sessions": "complete each configured session mode with at least two overlapping streamed conversations and inspect any wrong recall answers",
            "consistency": "inspect differing answers and token probabilities, verify deterministic serving settings and rerun the same shapes and concurrency levels",
            "tools": "inspect the tool protocol and argument outcomes, enable the server's tool parser and correct the configured tool contract, then rerun",
        }.get(check.id, "inspect the failed request and coverage details, correct the reported server or workload issue, then rerun the same workload")
    return summary.rstrip(".") + ". To conclude, " + remedy.rstrip(".") + "."


async def run_suite(config: RunConfig, *, progress: Callable[[dict], None] | None = None,
                    stop_event: asyncio.Event | None = None) -> RunReport:
    from .local_tokenizer import ensure_tokenizers
    await ensure_tokenizers(config)
    with PhaseSampler("traffic") as sampler:
        try:
            return await _run_suite(config, progress=progress, stop_event=stop_event, sampler=sampler)
        finally:
            sampler.detach()
            if hasattr(sampler, "run"):
                sampler.run.memory_sampling_done = True


async def _run_suite(config: RunConfig, *, progress, stop_event, sampler) -> RunReport:
    """Run the built-in suite with one shared load budget and preserve partial evidence."""
    public_config = config.model_copy(deep=True)
    config = public_config.model_copy(deep=True)
    if config.request_source == "request_pack" and "correctness" in config.checks:
        options = config.test_options.get("correctness")
        if options and options.model_fields_set & {"samples", "concurrency", "max_output_tokens", "request_timeout_seconds", "context_limit"}:
            raise ValueError("Request-pack correctness uses every case and default_concurrency; "
                             "per-check request setting overrides are unsupported.")
    started_at, run_id = _now(), uuid.uuid4().hex
    run = _Run(config, progress, stop_event)
    run.run_id = run_id
    loop = asyncio.get_running_loop()
    sampler.run = run
    run.rss_at_start_bytes = sampler.initial_rss_bytes
    samples = []
    def consume_samples():
        with sampler._lock:
            pending, samples[:] = samples[:], []
        for rss, unfinished in pending:
            run.memory_sample(rss, unfinished)
    def sampled(rss):
        samples.append((rss, tuple(run.pending_checks)))
        loop.call_soon_threadsafe(consume_samples)
    with sampler._lock:
        sampler.on_sample = sampled
    run.emit("run_started")
    sampler.sample()
    consume_samples()
    run.memory_sample(sampler.peaks.get("traffic"))
    fixtures = builtin_fixtures(config)
    fixtures = {key: [value] if isinstance(value, RequestSpec) else list(value)
                for key, value in fixtures.items()}
    custom_hash = None
    if config.custom_fixtures and "correctness" in config.checks:
        fixtures["correctness"].extend(load_custom_fixtures(config.custom_fixtures,
            legacy_max_output_tokens=config.legacy_custom_fixture_max_output_tokens))
        custom_hash = hashlib.sha256(Path(config.custom_fixtures).read_bytes()).hexdigest()
    sampler.sample()
    consume_samples()

    async def target_task(target):
        task = asyncio.create_task(_target(run, target, fixtures))
        run.target_tasks[target.name] = task
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not run.endpoint_health[target.name]["abort_reason"]:
                raise
            run.unfinished[target.name].update(config.checks)

    if config.overlap_models:
        tasks = [asyncio.create_task(target_task(target)) for target in config.targets]
    else:
        async def sequential():
            for target in config.targets:
                await target_task(target)
        tasks = [asyncio.create_task(sequential())]
    monitor = asyncio.create_task(run.monitor(tasks))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # Child cleanup can propagate the monitor's cancellation. Only a
        # cancellation of the suite itself is a new user stop.
        if not run.reason or asyncio.current_task().cancelling():
            run.reason = "cancelled by user"
            run.stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # A blocked callback may complete all requests before the monitor wakes.
        # Capture that final interval before cancelling the monitor.
        run.timing_health.sample(time.monotonic())
        run.max_lag_ms = max(run.max_lag_ms, run.timing_health.max_lag_ms)
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        if any(task.cancelled() or not task.done() for task in tasks) or any(run.unfinished.values()):
            run.stop_reason()
    inference_duration = time.monotonic() - run.start
    sampler.set_phase("scoring")
    consume_samples()
    judging_summary = None
    if any(spec.scorer == "judge" for spec in run.pack_specs) and not run.stop.is_set() and not run.memory_limit_reached:
        from .judging import judge_records
        from .traffic import finalize_stage

        # Evaluation is outside every target's lifetime, telemetry task and
        # generator monitor. Inference budgets do not shorten this phase.
        evaluation = asyncio.create_task(judge_records(
            config, run.records, run.pack_specs, stop_event=run.stop,
            progress=lambda event: run.emit(**event, phase="judging"),
        ))
        async def memory_watch():
            while not evaluation.done():
                if run.memory_limit_reached:
                    run.stop.set()
                    return
                await asyncio.sleep(.05)
        memory_monitor = asyncio.create_task(memory_watch())
        try:
            judging_summary = await asyncio.shield(evaluation)
        except asyncio.CancelledError:
            # Let the evaluator close its own workers and preserve partial votes.
            run.stop.set()
            judging_summary = await evaluation
        finally:
            memory_monitor.cancel()
            await asyncio.gather(memory_monitor, return_exceptions=True)
        if run.stop.is_set():
            run.reason = run.reason or "cancelled by user"
        limits = _check_config(config, "capacity").limits
        for target in config.targets:
            if run.stage_evidence[target.name]:
                stages = run.observations[target.name]["capacity"]
                run.observations[target.name]["capacity"] = [
                    finalize_stage(evidence, limits) | {
                        "timing_interval_ms": stages[index]["timing_interval_ms"]}
                    for index, evidence in enumerate(run.stage_evidence[target.name])]
        failures = Counter((record.target, record.condition_id) for record in run.records if _request_failed(record))
        for key, row in run.conditions.items():
            if row["failed"] != failures[key]:
                row["failed"] = failures[key]
                run.emit_condition("condition_finished", row["condition"]["id"], row)
    run.judging_pending = False
    for target in config.targets:
        run.finish_checks(target.name, config.checks)
    consume_samples()
    run.verdict_budget_guidance = _budget_guidance(config)
    checks = [check for target in config.targets for check in _checks(run, target)]
    sampler.close()
    consume_samples()
    sampler.detach()
    run.memory_sampling_done = True
    target_abort_reasons = {target: health["abort_reason"] for target, health in run.endpoint_health.items()
                           if health["abort_reason"]}
    for check in checks:
        if (check.target, check.id) in run.memory_interrupted_checks and check.status == "pass":
            check.status = "inconclusive"
            check.summary = run.reason + ". This check was interrupted."
        if check.target in target_abort_reasons and check.status == "pass":
            check.status = "inconclusive"
            check.summary = target_abort_reasons[check.target] + ". This check was interrupted."
    counts = defaultdict(dict)
    for target in config.targets:
        for scenario in sorted({r.scenario for r in run.records if r.target == target.name}):
            counts[target.name][scenario] = _stats([r for r in run.records if r.target == target.name and r.scenario == scenario])
    warnings = ["Other traffic on the target is unknown; results cover only the recorded workload.",
                "Backend/replica identity is unverified unless explicitly reported by the server; a service URL does not establish complete replica coverage."]
    if run.memory_limit_reached:
        warnings.append("Memory limit (RAM growth during this run) was exceeded." +
            (" Unfinished checks were interrupted." if run.memory_interrupted_checks else " No check was interrupted."))
    if any(check.metrics.get("low_confidence") for check in checks):
        warnings.append("Some checks have too few successful samples for confident timing conclusions.")
    if run.max_lag_ms > 100:
        warnings.append("Client scheduling delay was observed. Timing quality is qualified by overlapping request/stage intervals; raw measurements are retained. Its cause is not established.")
    if config.max_requests is not None and run.budget_used >= config.max_requests:
        run.limits_reached.add("max_requests")
        warnings.append("Global request budget exhausted; unfinished checks remain inconclusive.")
    cancellation_options = config.test_options.get("cancellation")
    if ("cancellation" in config.checks and cancellation_options
            and cancellation_options.cancel_after_ms is not None):
        warnings.append("Legacy cancellation.cancel_after_ms does not schedule the disconnect; "
                        "cancel_after_chunks controls cancellation after useful answer chunks.")
    if run.pack_specs:
        warnings.append("Repeated, warmed workload: quiet controls and traffic repeat the same requests and may benefit caches.")
        warnings.append("Request-pack correctness covers every case; global samples does not select case count. "
                        "With Load test, the selected traffic supplies the loaded pass. Otherwise one quiet pass and a concurrent pass of at least default concurrency run. "
                        "Legacy request previews omit this extra concurrent pass.")
    observations = {"resources": {
                        "sampled_peak_rss_bytes": dict(sampler.peaks),
                        "rss_at_start_bytes": run.rss_at_start_bytes,
                        "sampled_peak_growth_bytes": {phase: max(0, value - run.rss_at_start_bytes)
                            if value is not None and run.rss_at_start_bytes is not None else None
                            for phase, value in sampler.peaks.items()},
                        "process_peak_rss_bytes": peak_rss_bytes(),
                        "memory_limit_bytes": config.max_memory_mb * 1024**2,
                        "memory_limit_reached": run.memory_limit_reached,
                        "memory_limit_stopped_run": bool(run.memory_interrupted_checks),
                        "memory_interrupted_checks": [{"target": target, "id": check}
                            for target, check in sorted(run.memory_interrupted_checks)],
                        "measurement_available": run.memory_measured,
                        "guardrail_active": bool(config.max_memory_mb and run.rss_at_start_bytes is not None),
                        "memory_measurement": "sampled process RSS" if run.memory_measured else "not measured on this platform",
                        "sampling": SAMPLING},
                    "targets": run.observations, "generator_max_lag_ms": round(run.max_lag_ms, 3),
                    "target_abort_reasons": target_abort_reasons,
                    "client_timing_health": run.timing_health.snapshot(),
                    "request_budget_used": run.budget_used,
                    "inference_duration_seconds": inference_duration,
                    **({"judging": judging_summary} if judging_summary is not None else {}),
                    "replica_coverage": [{"target": t.name, "route": t.route, "parent": t.parent,
                        "configured_url_tested": any(r.target == t.name for r in run.records),
                        "observed_models": sorted({r.observed_model for r in run.records if r.target == t.name and r.observed_model}),
                        "observed_backend_ids": sorted({r.backend_id for r in run.records if r.target == t.name and r.backend_id}),
                        "other_replicas": "unverified"} for t in config.targets]}
    from .reporting import connection_summary
    connection_failures = [message for target in config.targets if (message := connection_summary(
        [r for r in run.records if r.target == target.name], target.url))]
    if connection_failures:
        observations["summary"] = "; ".join(connection_failures)
    from giraffe.workloads import FIXTURE_SOURCE_SHA256, WORKLOAD_CHECKS
    config_values = public_config.model_dump(mode="json")
    workload_settings = {key: (getattr(config, key).model_dump(mode="json")
                              if hasattr(getattr(config, key), "model_dump") else getattr(config, key))
                         for key, checks in WORKLOAD_CHECKS.items()
                         if checks.intersection(config.checks)}
    workload_identity = {
        "generator_source_sha256": FIXTURE_SOURCE_SHA256 if any(workload_settings.values()) else None,
        "settings": workload_settings,
    }
    if "load" in config.checks:
        from giraffe import load_test, concurrent_load, traffic
        workload_identity["load_test_source_sha256"] = hashlib.sha256(b"".join(Path(m.__file__).read_bytes() for m in (load_test, concurrent_load, traffic))).hexdigest()
    if "consistency" in config.checks and config.consistency and config.consistency.logprobs:
        from giraffe import logprobs, logprob_workload
        workload_identity["logprob_source_sha256"] = hashlib.sha256(
            Path(logprobs.__file__).read_bytes() + Path(logprob_workload.__file__).read_bytes()
        ).hexdigest()
    if "tools" in config.checks:
        from giraffe.tool_checks import resolved_tool_fixtures, resolved_tool_modes
        from giraffe.tool_conversations import SOURCE_SHA256
        workload_identity["resolved_tool_checks"] = {
            "fixtures": [fixture.model_dump(mode="json") for fixture in resolved_tool_fixtures(config)],
            "modes": resolved_tool_modes(config),
            **({"conversations": [fixture.model_dump(mode="json") for fixture in config.tool_checks.conversations],
                "conversation_source_sha256": SOURCE_SHA256}
               if config.tool_checks and config.tool_checks.conversations else {}),
        }
    fixture_values = {key: [spec.model_dump() for spec in specs] for key, specs in fixtures.items()}
    # An unused optional field must not invalidate existing baseline fingerprints.
    for specs in fixture_values.values():
        for spec in specs:
            if spec["request_timeout_seconds"] == config.request_timeout_seconds:
                spec["request_timeout_seconds"] = None
            for field, default in (("condition_id", None), ("cancel_after_chunks", None),
                                   ("expected_provided", False), ("rubric", None),
                                   ("tool_expectation", None), ("load_point", None),
                                   ("requested_input_tokens", None), ("requested_output_tokens", None)):
                if spec[field] == default:
                    del spec[field]
    manifest = {"config": config_values,
                "config_migration_notes": list(config._migration_notes),
                "timing_measurement_version": 2,
                **({"request_pack": pack_identity(config)} if run.pack_specs else {}),
                "workload_selection": {key: "selected" if key in public_config.checks else "not selected"
                                       for key in PUBLIC_CHECK_NAMES if key not in CORE_CHECKS},
                "effective_checks": {name: public_config.effective_check(name) for name in public_config.checks},
                "effective_initial": public_config.effective_initial(),
                "effective_traffic": (config.effective_traffic().model_dump(mode="json")
                                      if "capacity" in config.checks else None),
                "fixture_pack": {"version": FIXTURE_VERSION, "custom": config.custom_fixtures,
                                 "custom_sha256": custom_hash,
                                 "builtin_sha256": (None if run.pack_specs and set(config.checks) <= {"load", "capacity", "correctness"}
                                                    else fixture_hash(fixture_values)),
                                 "workload_fixture_sha256": fixture_hash(workload_identity)},
                "run_location": {"hostname": socket.gethostname(), "system": platform.system(),
                                 "machine": platform.machine(), "python": platform.python_version()},
                "load_shape": {**({"arrival_rates_rps": config.traffic.rates,
                                   "arrival": config.traffic.arrival} if "capacity" in config.checks else {}),
                               "overlap_models": config.overlap_models,
                               "target_request_quotas": run.quotas},
                "prerequisites": {name: scenarios for name, scenarios in {
                    "access": ["initial"], "serving": ["warm"],
                    "first_output": ["initial", "warm"],
                }.items() if name in config.checks},
                "scenario_counts": dict(counts),
                "measurement_definitions": {
                    "first_output_ms": "client-observed first non-whitespace answer or parsed tool delta; not server TTFT",
                    "generation_tokens_per_second": "(answer tokens - 1) / seconds between first and last answer chunk; estimate",
                    "aggregate": "valid completions or known output tokens / seconds from first dispatch to last completion within each scenario; includes errors in window",
                    "latency_ms": "client dispatch through protocol completion",
                    "workload_sizes": "character sizing is requested shape; server token usage is observed length",
                    "warmup": "initial and warm short requests only when required by selected checks or restart readiness; recovery uses its own matched baseline"}}
    memory_required_interrupted = any(check.required and (check.target, check.id)
                                     in run.memory_interrupted_checks for check in checks)
    verdict_reason = run.reason
    if run.memory_stop_reason and verdict_reason == run.memory_stop_reason and not memory_required_interrupted:
        verdict_reason = None
    run.emit("run_finished", abort_reason=verdict_reason, target_abort_reasons=target_abort_reasons)
    return RunReport(run_id=run_id, suite_version=SUITE_VERSION,
                     started_at=started_at, finished_at=_now(),
                     overall=overall_status(checks, verdict_reason or next(iter(target_abort_reasons.values()), None),
                                            memory_limit_interrupted=memory_required_interrupted), manifest=manifest, checks=checks,
                     requests=run.records, warnings=warnings, observations=observations,
                     abort_reason=verdict_reason, limits_reached=sorted(run.limits_reached))
