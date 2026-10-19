"""Bounded, shared-traffic acceptance suite for an existing inference endpoint."""

from __future__ import annotations

import asyncio
import hashlib
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


from .client import LLMClient
from .deployment import fixture_hash
from .fixtures import (FIXTURE_VERSION, SUITE_VERSION, builtin_fixtures, load_custom_fixtures,
                       score_response, traffic_fixtures, deterministic_options, reasoning_budget)
from .overlap import NEEDS_STREAM, answer_window, max_evidenced_overlap, scheduling_delay_windows
from .request_pack import case_sequence, pack_identity, pack_preview, pack_specs
from .timing import TimingHealth, configured_metrics, exceeded_unfinished, qualified_violations, run_quality
from .models import (
    INTERNAL_CHECK_NAMES as CHECK_NAMES, CHECK_NAMES as PUBLIC_CHECK_NAMES,
    CORE_CHECKS, CheckResult, Limits, RequestRecord, RequestSpec, RunConfig, RunReport, Target,
    overall_status,
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
        "output_tokens": sum(r.output_tokens or 0 for r in good),
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
    return (scenario.startswith(("traffic_stage_", "context", "fairness_", "bucket_", "mixed_", "arrival", "stress_"))
            or scenario in {"sustained", "recovery", "recovery_baseline", "recovery_prime", "limits",
                            "stop_control", "client_cancel", "client_deadline", "backend_cancel",
                            "cancellation_probe", "cancellation_baseline"})


def _context_rejection(record):
    return record.http_status in {400, 413, 422} and any(word in (record.error or "").lower()
        for word in ("context length", "context_length", "maximum context", "too many tokens",
                     "context size", "context window", "max_model_len"))


def _serving_failure(record, scenario):
    if (scenario == "sustained" or scenario.startswith("traffic_stage_") or record.status == "cancelled"
            or _reasoning_budget(record)):
        return False
    if _stress(scenario) and (record.status == "timeout" or _context_rejection(record)):
        return False
    return record.status != "completed" or not record.valid


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


class _Run:
    def __init__(self, config: RunConfig, progress, stop_event):
        self.config = config
        self.pack_specs = pack_specs(config)
        self.stage_evidence = defaultdict(list)
        self.progress = progress
        self.stop = stop_event or asyncio.Event()
        self.start = time.monotonic()
        self.deadline = self.start + config.max_duration_seconds
        self.slots = asyncio.Queue(maxsize=config.concurrency)
        for _ in range(config.concurrency):
            self.slots.put_nowait(None)
        self.records: list[RequestRecord] = []
        from giraffe.logprobs import LogprobBudget
        self.logprob_budget = LogprobBudget()
        self.attempted = 0
        self.budget_used = 0
        self.budget_counts = Counter()
        self.errors = 0
        self.reason: str | None = None
        self.counts = Counter()
        base, extra = divmod(config.max_requests, len(config.targets))
        self.quotas = {t.name: base + (i < extra) for i, t in enumerate(config.targets)}
        self.unfinished = defaultdict(set)
        self.observations = {t.name: {"fairness": {"pairs": 0, "overlaps": 0},
                                     "restart": None, "metrics": [], "capacity": []}
                             for t in config.targets}
        self.active = Counter()
        self.peak = Counter()
        self.max_lag_ms = 0.0
        self.timing_health = TimingHealth(self.start)
        self.inflight = {}
        for observation in self.observations.values():
            observation.update(deployment=None, deployment_history=[], phase_boundaries=[],
                serving_telemetry={"snapshots": [], "sample_limit_reached": False,
                    "sampling_interval_seconds": config.metrics_interval_seconds,
                    "timeout_seconds": config.metrics_timeout_seconds,
                    "max_samples": config.metrics_max_samples,
                    "max_series": config.metrics_max_series, "collection_gaps_seconds": []})

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
                           "max_requests": self.config.max_requests,
                           "elapsed_seconds": round(time.monotonic() - self.start, 3), **extra})

    def stop_reason(self) -> str | None:
        if self.stop.is_set():
            self.reason = self.reason or "cancelled by user"
        if time.monotonic() >= self.deadline:
            self.reason = self.reason or "maximum run duration reached"
        if self.errors >= self.config.stop_after_errors:
            self.reason = self.reason or "error threshold reached"
        return self.reason

    def allowed(self, target: str) -> bool:
        self.stop_reason()
        if self.budget_used >= self.config.max_requests:
            self.reason = self.reason or "maximum request budget reached"
        return not self.reason and self.budget_used < self.config.max_requests and (
            self.budget_counts[target] < self.quotas[target])

    def reserve_offer(self, target: str) -> bool:
        if not self.allowed(target):
            return False
        self.budget_used += 1
        self.budget_counts[target] += 1
        return True

    def try_acquire_slot(self) -> bool:
        try:
            self.slots.get_nowait()
            return True
        except asyncio.QueueEmpty:
            return False

    def release_slot(self) -> None:
        self.slots.put_nowait(None)

    @asynccontextmanager
    async def slot(self, reserved: bool, deadline: float | None = None):
        if not reserved:
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
                      *, slot_reserved=False, budget_reserved=False, deadline_override=None, budget_exempt=False, evaluate_record=None):
        check_ids = [name for name in spec.check_ids if name in self.config.checks
                     and (not _stress(spec.scenario) or name not in {"access", "serving", "first_output", "generation"})
                     and (name != "generation" or spec.scenario == "generation")
                     and (name != "capacity" or spec.scenario.startswith("traffic_stage_"))]
        spec = spec.model_copy(update={"check_ids": check_ids})
        async with self.slot(slot_reserved, deadline_override) as admitted:
            # An offered arrival owns its budget even if a later arrival exhausts
            # the ceiling. Only a real stop condition can prevent its dispatch.
            stopped = not admitted or self.stop.is_set() or time.monotonic() >= min(
                self.deadline, deadline_override if deadline_override is not None else self.deadline) or (
                self.errors >= self.config.stop_after_errors)
            if stopped or (not budget_reserved and not budget_exempt and not self.reserve_offer(target.name)):
                self.unfinished[target.name].update(_scenario_checks(spec))
                return None
            self.attempted += 1
            request_slot = self.attempted - 1
            self.counts[target.name] += 1
            key = (target.name, spec.scenario)
            self.active[key] += 1
            self.peak[key] = max(self.peak[key], self.active[key])
            global_deadline = False
            started, started_at = time.monotonic(), _now()
            slot = {"target": target.name, "peak": 1}
            token = object()
            self.inflight[token] = slot
            concurrent = sum(item["target"]==target.name for item in self.inflight.values())
            for item in self.inflight.values():
                if item["target"]==target.name:
                    item["peak"] = max(item["peak"], concurrent)
            try:
                if on_start:
                    on_start()
                self.emit("request_started", target.name, spec.scenario,
                          request_slot=request_slot, fixture_id=spec.fixture_id)
                if deadline_override is not None:
                    timeout_override = min(timeout_override or self.config.request_timeout_seconds,
                                           deadline_override - time.monotonic())
                remaining = self.deadline - time.monotonic()
                request_timeout = min(self.config.request_timeout_seconds,
                                      spec.request_timeout_seconds or self.config.request_timeout_seconds)
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
                record = RequestRecord(
                    id=uuid.uuid4().hex, target=target.name, fixture_id=spec.fixture_id,
                    scenario=spec.scenario, check_ids=spec.check_ids, started_at=started_at,
                    status="cancelled" if isinstance(exc, asyncio.CancelledError) or global_deadline else "timeout",
                    error="run cancelled" if isinstance(exc, asyncio.CancelledError)
                    else ("maximum run duration reached" if global_deadline else "request deadline exceeded"), elapsed_ms=(time.monotonic()-started)*1000,
                    input_chars=spec.input_chars, requested_max_tokens=spec.max_tokens or
                    self.config.max_output_tokens, stream=spec.stream,
                )
            except Exception as exc:
                # Exception text can contain credential-bearing URLs from transports.
                record = RequestRecord(
                    id=uuid.uuid4().hex, target=target.name, fixture_id=spec.fixture_id,
                    scenario=spec.scenario, check_ids=spec.check_ids, started_at=started_at,
                    status="failed", error=f"client failure ({type(exc).__name__})",
                    elapsed_ms=(time.monotonic()-started)*1000,
                    input_chars=spec.input_chars,
                )
            finally:
                self.active[key] -= 1
                self.inflight.pop(token, None)
            # A transport may catch cancellation to preserve partial streamed output.
            if time.monotonic() >= self.deadline and record.status == "cancelled":
                record.error = "maximum run duration reached"
            record.completed_at = _now()
            record.dispatch_ms = (started-self.start)*1000
            record.timing_start_ms = ((record._timing_started_monotonic - self.start) * 1000
                if record._timing_started_monotonic is not None else record.dispatch_ms)
            record.completed_ms = (time.monotonic()-self.start)*1000
            record.workload = dict(spec.workload)
            record.scheduled_ms = spec.workload.get("scheduled_ms")
            record.client_wait_ms = max(0, record.dispatch_ms-record.scheduled_ms) if record.scheduled_ms is not None else None
            record.overlap = slot["peak"]
            try:
                record = score_response(spec, record)
                if evaluate_record is not None:
                    evaluate_record(record)
            finally:
                self.records.append(record)
            intentional = ((spec.cancel_after_ms is not None or spec.cancel_after_chunks is not None)
                           and record.status == "cancelled") or (
                timeout_override is not None and record.status == "timeout")
            if record.status == "cancelled" and not intentional and timeout_override is None:
                reason = self.stop_reason()
                # Stress phases can cancel their own requests while the run continues.
                if reason or not _stress(spec.scenario):
                    self.reason = reason or "maximum run duration reached"
                    self.unfinished[target.name].update(_scenario_checks(spec))
            readiness = spec.scenario == "readiness" or (
                spec.scenario == "initial" and self.config.restart_target == target.name)
            budget_only = bool(set(spec.check_ids) & {"prefix", "sessions", "tools"}) and reasoning_budget(record)
            if _serving_failure(record, spec.scenario) and not readiness and not intentional and not budget_only:
                self.errors += 1
                record.check_ids = list(dict.fromkeys([*record.check_ids, *[
                    name for name in ("access", "serving", "first_output") if name in self.config.checks]]))
            if budget_exempt:
                self.observations[target.name].setdefault("readiness_requests", []).append(record.id)
            # Live visualization gets measurements only, never retained response bodies.
            self.emit("request_completed", target.name, spec.scenario, status=record.status,
                      request_slot=request_slot, request=record.model_dump(mode="json", include={
                          "id", "target", "fixture_id", "scenario", "status", "valid", "score",
                          "elapsed_ms", "first_output_ms", "check_ids",
                      }), intentional=intentional)
            return record

    async def group(self, target, client, specs, scenario, count=None, concurrency=1, checks=()):
        count = count if count is not None else self.config.samples
        self.emit("scenario_started", target.name, scenario, planned_requests=count)
        if not specs:
            self.unfinished[target.name].update(checks)
            return []
        result = []
        for start in range(0, count, concurrency):
            batch = [_spec(specs[i % len(specs)], scenario, checks)
                     for i in range(start, min(start + concurrency, count))]
            if not self.allowed(target.name):
                for spec in batch:
                    self.unfinished[target.name].update(_scenario_checks(spec))
                break
            result.extend(r for r in await asyncio.gather(
                *(self.request(target, client, spec) for spec in batch)) if r is not None)
        if len(result) < count:
            self.unfinished[target.name].update(checks)
        self.emit("scenario_finished", target.name, scenario)
        return result

    async def monitor(self, tasks):
        while not all(task.done() for task in tasks):
            await asyncio.sleep(.05)
            now = time.monotonic()
            self.timing_health.sample(now)
            self.max_lag_ms = max(self.max_lag_ms, self.timing_health.max_lag_ms)
            if all(task.done() for task in tasks):
                return
            if self.stop.is_set() or now >= self.deadline or self.errors >= self.config.stop_after_errors:
                self.allowed(self.config.targets[0].name)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                return


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
    snapshot = await scrape(run, target, phase, force=True)
    data["snapshots"].append(snapshot)
    run.observations[target.name]["metrics"].append(_gpu_snapshot(snapshot))


async def _finish_gpu_observation(run, target):
    """Allow DCGM at least one refresh, bounded by the enclosing run deadline."""
    data = run.observations[target.name]["serving_telemetry"]
    first = next((snap["elapsed_seconds"] for snap in data["snapshots"] if _gpu_scraped(snap)), None)
    window = run.config.gpu_min_observation_seconds
    if first is not None and not run.stop_reason():
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


async def _fairness(run, target, client, fixtures):
    config = _check_config(run.config, "fairness")
    observation = run.observations[target.name]["fairness"]
    if not config.stream or config.concurrency < 2:
        observation["reason"] = "Requires observable streaming and concurrency of at least two"
        run.unfinished[target.name].add("fairness")
        return
    short_fixtures = [spec for spec in fixtures["generation"]
                      if spec.fixture_id.startswith("fairness.copy.")]
    needed = len(short_fixtures[0].expected.split()) + 12
    observation["needed_output_tokens"] = needed
    if config.max_output_tokens < needed:
        observation["reason"] = f"Fairness needs an output cap of at least {needed} tokens for its active short stream."
        run.unfinished[target.name].add("fairness")
        return
    for index in range(config.samples):
        if not run.allowed(target.name):
            run.unfinished[target.name].add("fairness")
            break
        await run.group(target, client, short_fixtures, "fairness_baseline",
                        count=1, checks=["fairness"])
        event = asyncio.Event()
        short = _spec(short_fixtures[0], "fairness_short", ["fairness"])
        task = asyncio.create_task(run.request(target, client, short, event))
        waiter = asyncio.create_task(event.wait())
        long_record = None
        try:
            await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
            observation["pairs"] += 1
            if event.is_set() and not task.done():
                competitor = _spec(fixtures["long"][index % len(fixtures["long"])],
                                   "fairness_long", ["fairness"])
                messages = [dict(message) for message in competitor.messages]
                messages[0]["content"] = uuid.uuid4().hex + "\n" + messages[0]["content"]
                long_record = await run.request(target, client, competitor.model_copy(update={"messages": messages}))
            record = await task
            # Server evidence: the long request's first output lands strictly inside the
            # short request's output window, so its prefill finished mid-stream.
            short_window, long_window = answer_window(record) if record else None, answer_window(long_record) if long_record else None
            if (short_window and long_window and record.answer_chunks >= 2
                    and short_window[0] < long_window[0] < short_window[1]):
                observation["overlaps"] += 1
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(waiter, task, return_exceptions=True)


async def _context_fixtures(run, target, client, fixtures, source=None):
    """Use observed token usage to size deterministic filler, never claim estimated coverage."""
    config = _check_config(run.config, "context")
    if source is None:
        long_fixture_ids = {spec.fixture_id for spec in fixtures["long"]}
        candidates = [r for r in run.records if r.target == target.name and r.valid and
                      (r.fixture_id in long_fixture_ids or r.traffic_class == "long_input")
                      and r.input_tokens and r.input_chars]
        if not candidates:
            candidates = await run.group(target, client, [fixtures["context"][-2]],
                                         "context_calibration", count=1, checks=["context"])
        source = next((r for r in candidates if r.valid and r.input_tokens and r.input_chars), None)
    if source is None:
        run.observations[target.name]["context_calibration"] = {"status": "unavailable", "reason": "Server input token usage unavailable"}
        return fixtures["context"]
    desired_tokens = max(1, int((config.context_limit - min(24, config.max_output_tokens)) * .85))
    character_budget = int(source.input_chars * desired_tokens / source.input_tokens)
    fixture_context_limit = max(128, min(config.context_limit * 16,
                                        character_budget + min(24, config.max_output_tokens) + 32))
    calibration = {
        "status": "sized_from_observed_usage", "source_request_id": source.id,
        "source_input_tokens": source.input_tokens, "source_input_chars": source.input_chars,
        "desired_input_tokens": desired_tokens, "fixture_context_limit": fixture_context_limit,
        "method": "builtin_fixtures(config with this fixture_context_limit); actual coverage uses returned usage",
    }
    previous = run.observations[target.name].get("context_calibration")
    if previous and previous["status"] == "sized_from_observed_usage":
        previous.setdefault("adjustments", []).append(calibration)
    else:
        run.observations[target.name]["context_calibration"] = calibration
    specs = builtin_fixtures(config.model_copy(update={"context_limit": fixture_context_limit}))["context"]
    for spec in specs:
        # Density is extrapolated from another fixture; rejection needs a margin.
        spec.workload["calibrated_input_tokens"] = int(spec.input_chars * source.input_tokens / source.input_chars)
    return specs


async def _target(run: _Run, target: Target, fixtures: dict):
    config, enabled = run.config, set(run.config.checks)
    telemetry_task = None
    from giraffe import deployment, telemetry, workloads
    try:
        run.observations[target.name]["deployment"] = await deployment.snapshot(target, config, run.deadline, run.stop)
        if not await _restart(run, target):
            return
        # GPU observations do not require an inference client or an unrelated
        # warm-up. An explicitly requested restart still needs readiness probes.
        if enabled == {"gpu"} and config.restart_target != target.name:
            await _metrics(run, target, "start")
            return
        async with LLMClient(target, config) as client:
            client.logprob_budget = run.logprob_budget
            if "gpu" in enabled and not config.metrics:
                await _metrics(run, target, "start")
            if config.metrics:
                data = run.observations[target.name]["serving_telemetry"]
                await _metrics(run, target, "start")
                telemetry_task = asyncio.create_task(telemetry.sample_during(run, target))
            elif target.deployment.collector_url:
                telemetry_task = asyncio.create_task(telemetry.sample_during(run, target))
            restart = run.observations[target.name]["restart"]
            if enabled & {"access", "serving", "first_output", "generation"} or restart:
                initial_timeout = (config.effective_check("access")["request_timeout_seconds"]
                                   if "access" in enabled else config.request_timeout_seconds)
                initial_specs = [spec.model_copy(update={"request_timeout_seconds":
                    initial_timeout})
                    for spec in fixtures["short"]]
                if restart and restart["status"] == "hook_completed":
                    ready_deadline = min(run.deadline, time.monotonic() + config.readiness_timeout_seconds)
                    backoff = .1
                    initial = []
                    restart["health_polls"] = []
                    while time.monotonic() < ready_deadline and not run.stop_reason():
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
                        return
                else:
                    await run.group(target, client, initial_specs, "initial", count=1)
            if enabled & {"serving", "first_output"}:
                warm_count = config.samples
                await run.group(target, client, fixtures["short"], "warm", count=warm_count)
            if "generation" in enabled:
                effective = _check_config(config, "generation")
                await run.group(target, client, builtin_fixtures(effective)["generation"], "generation",
                                count=effective.samples, checks=["generation"])
            if "capacity" in enabled:
                from .traffic import arrival_schedule, run_stage

                if run.pack_specs:
                    await run.group(target, client, run.pack_specs, "pack_control",
                                    count=len(run.pack_specs), checks=["correctness"] if "correctness" in enabled else [])

                traffic = config.effective_traffic()
                effective = _check_config(config, "capacity")
                mixed_fixtures = {} if run.pack_specs else traffic_fixtures(config)

                async def execute(spec):
                    spec = _spec(spec, spec.scenario, ["capacity"])
                    return await run.request(target, client, spec, slot_reserved=True, budget_reserved=True)

                def traffic_stop_reason():
                    reason = run.stop_reason()
                    # Exhausting new offers must not cancel already admitted work.
                    return None if reason == "maximum request budget reached" else reason

                for index, rate in enumerate(traffic.rates):
                    pack_arguments = {}
                    if run.pack_specs:
                        frozen = {spec.fixture_id: spec for spec in run.pack_specs}
                        schedule, _ = arrival_schedule(traffic, rate, index)
                        pack_arguments = {
                            "case_specs": [frozen[spec.fixture_id] for spec in
                                           case_sequence(config, len(schedule), index)],
                            "active_case_specs": run.pack_specs,
                        }
                    stage_start = (time.monotonic() - run.start) * 1000
                    stage = await run_stage(
                        traffic=traffic, rate_rps=rate, stage_index=index,
                        fixtures=mixed_fixtures, execute=execute,
                        reserve_offer=lambda: run.reserve_offer(target.name),
                        try_acquire=run.try_acquire_slot, release=run.release_slot,
                        stop_reason=traffic_stop_reason, deadline=run.deadline, limits=effective.limits,
                        capture_evidence=(run.stage_evidence[target.name].append
                                          if any(spec.scorer == "judge" for spec in run.pack_specs) else None),
                        **pack_arguments,
                    )
                    stage["timing_interval_ms"] = [stage_start, (time.monotonic() - run.start) * 1000]
                    run.observations[target.name]["capacity"].append(stage)
                    if run.reason or run.stop.is_set() or time.monotonic() >= run.deadline:
                        run.unfinished[target.name].add("capacity")
                        break
            if "fairness" in enabled:
                await _fairness(run, target, client, builtin_fixtures(_check_config(config, "fairness")))
            if "context" in enabled:
                effective = _check_config(config, "context")
                check_fixtures = builtin_fixtures(effective)
                context_fixtures = await _context_fixtures(run, target, client, check_fixtures)
                count = max(effective.samples, len(check_fixtures["context"]))
                for attempt in range(3):
                    records = await run.group(target, client, context_fixtures, "context",
                                              count=count, checks=["context"])
                    source = max((r for r in records if r.valid and r.input_tokens
                                  and r.input_chars), key=lambda r: r.input_tokens, default=None)
                    if source is None or source.input_tokens >= effective.context_limit * .8:
                        break
                    remaining = min(config.max_requests - run.budget_used,
                                    run.quotas[target.name] - run.budget_counts[target.name])
                    if attempt == 2 or len(records) < count or remaining < count:
                        break
                    context_fixtures = await _context_fixtures(
                        run, target, client, check_fixtures, source=source,
                    )
            for check_id in ("correctness", "json"):
                if check_id in enabled:
                    if check_id == "correctness" and run.pack_specs:
                        if "capacity" not in enabled:
                            await run.group(target, client, run.pack_specs, "pack_control",
                                            count=len(run.pack_specs), checks=["correctness"])
                            effective = _check_config(config, "correctness")
                            await run.group(target, client, run.pack_specs, "pack_loaded",
                                            count=max(len(run.pack_specs), effective.concurrency),
                                            concurrency=effective.concurrency, checks=["correctness"])
                        continue
                    effective = _check_config(config, check_id)
                    specs = [spec.model_copy(update={"options": deterministic_options(config, spec.options)})
                             for spec in fixtures[check_id]]
                    await run.group(target, client, specs, f"{check_id}_c{effective.concurrency}",
                                    count=max(effective.samples, len(specs)),
                                    concurrency=effective.concurrency, checks=[check_id])
            if "cancellation" in enabled:
                effective = _check_config(config, "cancellation")
                options = config.effective_check("cancellation")
                normal = [spec.model_copy(update={"scorer": "none", "options": deterministic_options(config, spec.options)})
                                 if spec.fixture_id.startswith("limits.stop.") else spec
                                 for spec in fixtures["limits"] if spec.cancel_after_ms is None
                                 and not spec.fixture_id.startswith("limits.stop_control")]
                cancel = [spec.model_copy(update={"cancel_after_ms": None,
                                                 "cancel_after_chunks": options["cancel_after_chunks"]})
                          for spec in fixtures["limits"] if spec.cancel_after_ms is not None]
                await run.group(target, client, [spec for spec in fixtures["limits"]
                                if spec.fixture_id.startswith("limits.stop_control")], "stop_control",
                                count=1, checks=["cancellation"])
                await run.group(target, client, normal, "limits",
                                count=max(effective.samples, len(normal)), checks=["cancellation"])
                if cancel:
                    # Same-fixture baseline: a follow-up queued behind leaked generation shows as delay.
                    await run.group(target, client, fixtures["short"][:1], "cancellation_baseline",
                                    count=1, checks=["cancellation"])
                await run.group(target, client, cancel, "client_cancel", count=len(cancel),
                                checks=["cancellation"])
                # Follow-ups go immediately after the disconnect, while leaked work would still run.
                await run.group(target, client, fixtures["short"], "cancellation_probe",
                                count=effective.samples, checks=["cancellation"])
                if cancel:
                    deadline_spec = _spec(cancel[0].model_copy(update={"cancel_after_ms": None, "cancel_after_chunks": None}),
                                          "client_deadline", ["cancellation"])
                    probe_timeout = min(options["deadline_probe_ms"] / 1000, config.request_timeout_seconds)
                    run.observations[target.name]["deadline_probe_ms"] = probe_timeout * 1000
                    await run.request(target, client, deadline_spec, timeout_override=probe_timeout)
                if options["backend_verification"] == "required":
                    from giraffe.backend_cancellation import exercise
                    await exercise(run, target, client, fixtures)
            if "recovery" in enabled:
                effective = _check_config(config, "recovery")
                probe_specs = fixtures["sustained"]
                from .planning import request_plan
                later = [name for name in config.checks if name not in CORE_CHECKS]
                later_plan = request_plan(config.model_copy(update={"checks": later, "restart_target": None}))
                if "cancellation" in enabled:
                    cleanup = config.effective_check("cancellation")["cleanup_seconds"]
                    await asyncio.sleep(max(0, min(5, cleanup, run.deadline - time.monotonic())))
                if min(config.max_requests-run.budget_used, run.quotas[target.name]-run.budget_counts[target.name]) > (
                        later_plan["known_per_target"] + 2 * effective.samples
                        + max(effective.limits.min_samples, effective.concurrency*2)):
                    # Uncounted warm-up so the baseline is not the probe fixture's cold first use.
                    await run.request(target, client, probe_specs[0].model_copy(update={
                        "scenario": "recovery_prime", "check_ids": []}))
                await run.group(target, client, probe_specs, "recovery_baseline",
                                count=effective.samples, checks=["recovery"])
                remaining = min(config.max_requests-run.budget_used,
                                run.quotas[target.name]-run.budget_counts[target.name])
                reserved = later_plan["known_per_target"] + effective.samples
                # Preserve later fixed work and probes; cap the remaining share
                # only when another selected check still needs request budget.
                allowance = max(0, min(remaining // 2 if later else remaining, remaining-reserved))
                run.observations[target.name]["sustained_budget"] = {
                    "allowance": allowance, "reserved_later": reserved,
                    "policy": "reserve fixed later work and probes; half quota cap only with later checks"}
                heavy = traffic_fixtures(config)
                heavy_specs = heavy["long_input"] + heavy["long_output"]
                end = min(run.deadline, time.monotonic() + effective.sustained_seconds)
                dispatched, exhausted = 0, False
                async def worker():
                    nonlocal dispatched, exhausted
                    while time.monotonic() < end:
                        if dispatched >= allowance or not run.allowed(target.name):
                            exhausted = True
                            return
                        index = dispatched
                        dispatched += 1
                        spec = _spec(heavy_specs[index % len(heavy_specs)], "sustained", ["recovery"])
                        await run.request(target, client, spec)
                run.emit("scenario_started", target.name, "sustained")
                await asyncio.gather(*(worker() for _ in range(effective.concurrency)))
                run.emit("scenario_finished", target.name, "sustained")
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
            await workloads.execute(run, target, client, fixtures)
    except asyncio.CancelledError:
        run.unfinished[target.name].update(enabled)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) and str(exc).startswith((
            "Missing API key environment variable:", "Missing header environment variable:")) else type(exc).__name__
        run.observations[target.name]["client_error"] = f"Client setup failed ({detail})"
        run.unfinished[target.name].update(enabled)

    finally:
        if telemetry_task:
            telemetry_task.cancel()
            await asyncio.gather(telemetry_task, return_exceptions=True)
        data = run.observations[target.name]["serving_telemetry"]
        if "gpu" in enabled and "gpu_observation_window" not in run.observations[target.name]:
            await _finish_gpu_observation(run, target)
        elif config.metrics:
            await _metrics(run, target, "end")
        elif len(data["snapshots"]) < config.metrics_max_samples:
            data["snapshots"].append(await telemetry.scrape(run, target, "not collected"))
        telemetry.summarize(data, config.cache_pressure_threshold)
        if config.metrics:
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
                  and (r.traffic_stage is not None or r.scenario == "pack_loaded")]
        stats = _stats(loaded)
        accuracy = stats["correct"] / stats["scored"] if stats["scored"] else None
        failed = accuracy is not None and accuracy < limits.min_correctness
        observed = bool(loaded) and all(r.valid and r.score is not None for r in loaded)
        evidenced = max_evidenced_overlap((r for r in loaded if r.score is not None), target_records, delays)
        concurrent = run.config.concurrency == 1 or evidenced >= 2
        case["loaded"] = {**stats, "accuracy": accuracy, "evidenced_overlap": evidenced, "evidenced_overlap_capped_at": 2,
                          "configured_concurrency": run.config.concurrency,
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
        config = _check_config(run.config, check_id)
        limits = config.limits
        optional = check_id == "gpu"
        records = [r for r in ordinary if check_id in r.check_ids]
        if check_id == "correctness" and run.pack_specs:
            records = [r for r in all_records if r.scenario in {"pack_control", "pack_loaded"}
                       or r.traffic_stage is not None]
            limits = limits.model_copy(update={"min_samples": 1})
        if check_id == "capacity":
            records = [r for r in records if r.traffic_stage is not None]
        if check_id == "fairness":
            records = [r for r in records if r.scenario in {"fairness_baseline", "fairness_short", "fairness_long"}]
        if check_id == "cancellation":
            records = [r for r in all_records if check_id in r.check_ids]
        if check_id in {"first_output", "generation"}:
            # Only ordinary serial requests: loaded traffic belongs to its own checks.
            records = [r for r in records if r.scenario in {"warm", "generation"}]
        if check_id == "generation":
            records = [r for r in records if r.scenario == "generation"]
        metrics, status, summary = _stats(records), "pass", "Observed requests met configured checks."
        timing_records = records
        if check_id == "fairness":
            timing_records = [r for r in records if r.scenario in {"fairness_baseline", "fairness_short"}]
        elif check_id == "recovery":
            timing_records = [r for r in records if r.scenario in {"recovery_baseline", "recovery"}]
        quality = run_quality(run, timing_records, target.name, configured_metrics(limits, check_id))
        saturated = quality["status"] == "client_delayed"
        if check_id in {"first_output", "generation", "capacity", "fairness", "recovery"}:
            metrics["timing_quality"] = quality
        if check_id not in config.checks:
            status, summary = "skipped", "Not selected for this run."
        elif observations["restart"] and observations["restart"]["status"] == "blocked":
            status, summary = "blocked", observations["restart"].get("reason", "Restart readiness unavailable")
        elif observations.get("client_error"):
            status, summary = "blocked", observations["client_error"]
        elif check_id == "fairness" and observations["fairness"].get("reason"):
            status, summary = "inconclusive", observations["fairness"]["reason"]
            if "needed_output_tokens" in observations["fairness"]:
                metrics["needed_output_tokens"] = observations["fairness"]["needed_output_tokens"]
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
        elif check_id == "fairness":
            baseline_records = [r for r in records if r.scenario == "fairness_baseline"]
            baseline = _stats(baseline_records)
            mixed_records = [r for r in records if r.scenario == "fairness_short"]
            mixed = _stats(mixed_records)
            missing_gap = _missing_timing(mixed_records, limits.model_copy(update={
                "first_output_ms": None, "latency_ms": None, "min_output_tokens_per_second": None}))
            def decode_p50(rows):
                return _percentile([r.last_output_ms-r.first_output_ms for r in rows
                    if r.valid and r.score is True and r.answer_chunks >= 2 and r.last_output_ms is not None
                    and r.first_output_ms is not None and r.last_output_ms > r.first_output_ms], .5)
            base_decode, loaded_decode = decode_p50(baseline_records), decode_p50(mixed_records)
            ratio = loaded_decode / base_decode if loaded_decode is not None and base_decode else None
            accuracy = metrics["correct"] / metrics["scored"] if metrics["scored"] else None
            metrics.update({"short_only": baseline, "mixed_short": mixed, "latency_ratio": ratio,
                            "decode_p50_ms": {"baseline": base_decode, "loaded": loaded_decode},
                            "accuracy": accuracy, "unscored_completions": metrics["completed"] - metrics["scored"],
                            **observations["fairness"]})
            if any(r.status != "cancelled" and not _reasoning_budget(r)
                   and (not r.valid or r.status != "completed") for r in records):
                status, summary = "fail", "Mixed-load requests failed; short and long traffic did not both complete successfully."
            elif accuracy is not None and accuracy < limits.min_correctness:
                status, summary = "fail", "Mixed short/long traffic failed the configured known-answer correctness limit."
            elif qualified_violations(mixed_records, limits.model_copy(update={
                    "first_output_ms": None, "latency_ms": None,
                    "min_output_tokens_per_second": None}), quality):
                status, summary = "fail", "Active short-stream pauses exceeded the configured stream-gap limit without overlapping client scheduling delay."
            elif metrics["reasoning_budget_exhausted"]:
                status, summary = "inconclusive", "Reasoning exhausted the output budget before an answer; fairness is unverified."
            elif any(r.finish_reason == "length" for r in records):
                status, summary = "inconclusive", "Fairness answers reached their output cap; complete matched stream evidence is unavailable."
            elif saturated:
                status, summary = "inconclusive", "Client scheduling delay prevents attributing mixed-traffic timing effects to the endpoint."
                metrics["reason_code"] = "client_delayed"
            elif (observations["fairness"]["overlaps"] < config.samples or ratio is None
                  or len(baseline_records) < config.samples):
                status, summary = "inconclusive", "Long prefill did not observably overlap every active short stream (long-request output inside the short output window), or timing is noisy."
            elif limits.stream_gap_ms and mixed["max_stream_gap_ms"] is not None and mixed["max_stream_gap_ms"] > limits.stream_gap_ms:
                status, summary = "fail", "Active short-stream pauses exceeded the configured stream-gap limit under mixed traffic."
            elif ratio is not None and limits.fairness_max_ratio and ratio > limits.fairness_max_ratio:
                status, summary = "fail", "Short-stream decoding time increased beyond the configured mixed-traffic ratio."
            elif accuracy is None or metrics["unscored_completions"]:
                status, summary = "inconclusive", "Some mixed-load completions lack a local correctness score."
            elif missing_gap:
                metrics["missing_timing_metrics"] = missing_gap
                status, summary = "inconclusive", "Configured mixed-traffic gap limit cannot be checked without visible stream-gap measurements."
            elif limits.fairness_max_ratio is None:
                status, summary = "inconclusive", "Mixed-traffic effect observed; no acceptance ratio configured."
                metrics["reason_code"] = "missing_acceptance_limit"
            else:
                summary = "Observed mixed-traffic effect met the limit; this does not identify a scheduler cause."
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
                    r.input_tokens if r.input_tokens is not None else r.workload.get("calibrated_input_tokens"))) is not None
                    and tokens <= config.context_limit * (.85 if r.input_tokens is not None else .70))
                metrics["context_rejections"] = {"rejected": len(rejected), "calibrated_rejected_input_tokens": disproving}
                # Require every position at each observed near-limit size; probes cannot supply recall evidence.
                recall = [r for r in records if r.scenario in {"context", "context_calibration"}
                          and r.fixture_id.startswith("context.") and len(r.fixture_id.split(".")) >= 3]
                near_sizes = {(r.fixture_id.split(".")[1], r.input_chars) for r in recall
                              if r.input_tokens is not None and r.input_tokens >= config.context_limit * .8}
                near_positions, near_missed, near_unverified = {}, [], []
                for size, chars in sorted(near_sizes):
                    items = [r for r in recall if (r.fixture_id.split(".")[1], r.input_chars) == (size, chars)]
                    required = {position for r in items for position in
                                r.workload.get("position_coverage", {}).get("positions", ["beginning", "middle", "end"])}
                    positions = {}
                    for position in sorted(required):
                        samples = [r for r in items if r.fixture_id.split(".")[2] == position]
                        row = {"attempted": len(samples), "scored": sum(r.score is not None for r in samples),
                               "correct": sum(r.score is True for r in samples)}
                        positions[position] = row
                        label = f"{size}/{chars} chars/{position}"
                        if row["scored"] > row["correct"]:
                            near_missed.append(label)
                        if not samples or row["scored"] < len(samples):
                            near_unverified.append(label)
                    near_positions[f"{size}:{chars}"] = positions
                metrics["near_limit_positions"] = near_positions
                metrics["near_limit_coverage"] = bool(near_sizes)
            if check_id in {"correctness", "json"} and not run.pack_specs:
                metrics["configured_concurrency"] = config.concurrency
                delays = scheduling_delay_windows(run)
                metrics["evidenced_overlap"] = max_evidenced_overlap((r for r in records if r.score is not None), all_records, delays)
                metrics["evidenced_overlap_capped_at"] = 2
            if check_id == "context" and any(_serving_failure(r, r.scenario) for r in records):
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
        if status == "fail" and check_id in {"capacity", "fairness", "cancellation", "recovery"}:
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
                elif check_id == "fairness":
                    mixed = metrics["mixed_short"]
                    ratio = metrics["latency_ratio"]
                    timing_failed = bool(
                        (limits.fairness_max_ratio and ratio is not None
                         and ratio > limits.fairness_max_ratio)
                        or (limits.stream_gap_ms and mixed["max_stream_gap_ms"] is not None
                            and mixed["max_stream_gap_ms"] > limits.stream_gap_ms)
                    )
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
        config = (_check_config(run.config, "concurrency" if check.id in {"buckets", "mixed"} else check.id)
                  if check.id in CORE_CHECKS or check.id in {"buckets", "mixed"} else run.config)
        evidence = set(check.evidence_ids)
        records = [r for r in all_records if r.id in evidence]
        check.summary = _verdict_summary(check, config, records, budget)
        if disconnected:
            check.summary = (f"{disconnected}. {check.summary} "
                             "Start the server and verify the target URL, port, proxy and TLS settings, then rerun.")
    return result + workload_results


def _budget_guidance(config):
    from .planning import request_plan
    plan = request_plan(config)
    planned = plan["required_request_limit"] or max(
        plan["known_total"], plan["conservative_requests_per_target"] * len(config.targets))
    amount = f"to at least {planned}" if planned > config.max_requests else f"above {config.max_requests} if exhausted"
    return (f"Raise max_requests (or --max-requests) {amount}, "
            f"or select fewer checks. The plan needs at least {plan['known_total']} fixed requests; adaptive traffic may need more.")


def _output_budget_guidance(config, records):
    capped = [r for r in records if _reasoning_budget(r) or r.finish_reason == "length"]
    if capped and all(r.requested_max_tokens < config.max_output_tokens for r in capped):
        caps = ", ".join(str(value) for value in sorted({r.requested_max_tokens for r in capped}))
        return (f"the affected fixtures request {caps} tokens below the current ceiling; increasing max_output_tokens alone will not "
                "change those requests. Configure the model to return the final fixture answer without extra reasoning within its requested cap, then rerun")
    return (f"raise max_output_tokens (or --max-output-tokens) above {config.max_output_tokens} for fixtures capped by that ceiling, "
            "or configure the model to produce the final answer within its requested cap, then rerun")


def _verdict_summary(check, config, records, budget):
    from .concurrent_workloads import remedy_conditions

    summary, metrics, limits = check.summary, check.metrics, config.limits
    answer_failed, timing_failed = remedy_conditions(check, config, records)
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
        if check.id == "fairness":
            measured = [r for r in records if r.scenario == "fairness_short"]
        elif check.id == "recovery":
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
                    check.id == "generation" and field == "first_output_ms") or (
                    check.id == "fairness" and field != "max_stream_gap_ms"):
                continue
            values = [value for r in measured if (value := getattr(r, field)) is not None
                      and (r.valid or field in exceeded_unfinished(r, limits))]
            value = (min(values) if minimum else max(values)) if values else None
            if value is not None and (value < allowed if minimum else value > allowed):
                details.append(f"{name} {value:g} {unit} {'<' if minimum else '>'} allowed {allowed:g} {unit}")
        if quality.get("status") != "client_delayed":
            for key in ("latency_ratio", "first_output_ratio"):
                allowed = limits.fairness_max_ratio if check.id == "fairness" else limits.recovery_max_ratio
                value = metrics.get(key)
                if allowed and value is not None and value > allowed:
                    name = "short-stream decoding ratio" if check.id == "fairness" else key.replace("_", " ")
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
    if answer_failed or timing_failed:
        remedies = []
        if answer_failed:
            remedies.append("check the model's answers for the listed fixtures (the received text above); "
                            "a refusal or wrong answer is a model/prompt issue, not a serving slowdown; "
                            "rerun after changing the model or its settings")
        if timing_failed:
            remedies.append({
                "fairness": "inspect short-stream pauses and long-request interference on the server, address the slowdown, then rerun the same mixed traffic",
                "mixed": "inspect light-request latency and heavy-request interference on the server, address the slowdown, then rerun the same mixed traffic",
                "buckets": "inspect slow requests in the failing size/concurrency cells and server load, address the slowdown, then rerun the same matrix",
                "token_stress": "inspect slow token-stress requests and server load, address the slowdown, then rerun the same token stress cases",
            }[check.id])
        remedy = "; then ".join(remedies)
    elif check.id == "fairness" and check.status == "inconclusive" and (config.concurrency < 2 or not config.stream):
        prerequisites = []
        if config.concurrency < 2:
            prerequisites.append("set --concurrency 2 or more")
        if not config.stream:
            prerequisites.append("enable streaming (stream: true, omit --nonstream)")
        remedy = " and ".join(prerequisites) + ", then rerun fairness"
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
        remedy = _output_budget_guidance(config, records)
    elif reason == "insufficient_timing_samples":
        setting = "samples (or --samples)" + (" and any test_options.generation.samples override" if check.id == "generation" else "")
        remedy = (f"set {setting} to at least {limits.min_samples}, "
                  "then allow those serial requests to complete within the request and time budgets")
    elif check.status == "inconclusive" and (reason == "missing_acceptance_limit" or (check.id, summary) in {
            ("mixed", "Mixed-load ratios measured; no acceptance ratio configured."),
            ("arrivals", "Arrivals observed; no acceptance limit configured.")}):
        options = {
            "fairness": ("fairness_max_ratio",), "mixed": ("fairness_max_ratio",),
            "first_output": ("first_output_ms",),
            "generation": ("latency_ms", "stream_gap_ms", "min_output_tokens_per_second"),
            "arrivals": ("latency_ms", "first_output_ms", "stream_gap_ms", "min_output_tokens_per_second"),
            "recovery": ("recovery_max_ratio", "first_output_ms", "latency_ms", "stream_gap_ms", "min_output_tokens_per_second"),
        }[check.id]
        option = " or ".join(f"limits.{name}" for name in options)
        remedy = f"set {option} to your service's acceptance target and rerun"
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
    elif check.id == "fairness" and metrics.get("needed_output_tokens", 0) > config.max_output_tokens:
        remedy = f"set max_output_tokens (or --max-output-tokens) to at least {metrics['needed_output_tokens']} and rerun the streaming trials"
    elif check.status == "inconclusive" and (any(_reasoning_budget(r) for r in records)
            or (check.id == "fairness" and any(r.finish_reason == "length" for r in records))):
        remedy = _output_budget_guidance(config, records)
    elif check.id == "context":
        remedy = (f"set context_limit to the server's actual token window (currently {config.context_limit}), "
                  "enable prompt token usage reporting and rerun all fact positions with enough output tokens for complete answers")
    elif check.id == "recovery" and summary in {
            "Sustained duration or light-load recovery was not fully observed.",
            "Sustained or recovery requests were cancelled by the run deadline or a stop; recovery was not fully observed."}:
        remedy = (f"allow {config.sustained_seconds:g} seconds of sustained traffic with at least {limits.min_samples} successful sustained requests "
                  f"and {config.samples} successful requests in each baseline and recovery batch; "
                  f"raise max_duration_seconds (or --max-duration) above {config.max_duration_seconds:g} if the deadline stops the run. {budget}")
    elif any(word in text for word in ("run budget", "run deadline", "no applicable")):
        remedy = f"{budget} Allow more max_duration_seconds (or --max-duration) if the run deadline stops traffic"
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
            "fairness": "inspect short-stream pauses and long-request interference on the server, address the slowdown, then rerun the same mixed traffic",
            "cancellation": "enable streaming and completion token usage, verify server stop and cancellation support, then rerun the control and follow-up probes",
            "recovery": "inspect sustained-load errors and slow recovery probes, address the server issue, then rerun the same sustained workload",
            "gpu": f"configure a reachable GPU metrics exporter with fresh per-device samples and allow at least {config.gpu_min_observation_seconds:g} seconds for observation; inspect reported device errors before rerunning",
            "prefix": "enable cached-token usage reporting, check the server's prefix-cache configuration and rerun the repeat and independent controls",
            "sessions": "complete each configured session mode with at least two overlapping streamed conversations and inspect any wrong recall answers",
            "consistency": "inspect differing answers and token probabilities, verify deterministic serving settings and rerun the same shapes and concurrency levels",
            "tools": "inspect the tool protocol and argument outcomes, enable the server's tool parser and correct the configured tool contract, then rerun",
        }.get(check.id, "inspect the failed request and coverage details, correct the reported server or workload issue, then rerun the same workload")
    return summary.rstrip(".") + ". To conclude, " + remedy.rstrip(".") + "."


async def run_suite(config: RunConfig, *, progress: Callable[[dict], None] | None = None,
                    stop_event: asyncio.Event | None = None) -> RunReport:
    """Run the built-in suite with one shared load budget and preserve partial evidence."""
    from .concurrent_workloads import execution_config, consolidate_checks

    public_config = config.model_copy(deep=True)
    config = execution_config(public_config)
    if config.request_source == "request_pack" and "correctness" in config.checks:
        options = config.test_options.get("correctness")
        if options and options.model_fields_set & {"samples", "concurrency"}:
            raise ValueError("Request-pack correctness uses every case and the global concurrency; "
                             "per-check samples/concurrency overrides are unsupported.")
    started_at, run_id = _now(), uuid.uuid4().hex
    run = _Run(config, progress, stop_event)
    fixtures = builtin_fixtures(config)
    fixtures = {key: [value] if isinstance(value, RequestSpec) else list(value)
                for key, value in fixtures.items()}
    custom_hash = None
    if config.custom_fixtures and "correctness" in config.checks:
        fixtures["correctness"].extend(load_custom_fixtures(config.custom_fixtures))
        custom_hash = hashlib.sha256(Path(config.custom_fixtures).read_bytes()).hexdigest()
    run.emit("run_started")
    if config.overlap_models:
        tasks = [asyncio.create_task(_target(run, target, fixtures)) for target in config.targets]
    else:
        async def sequential():
            for target in config.targets:
                await _target(run, target, fixtures)
        tasks = [asyncio.create_task(sequential())]
    monitor = asyncio.create_task(run.monitor(tasks))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
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
    judging_summary = None
    if any(spec.scorer == "judge" for spec in run.pack_specs) and not run.stop.is_set():
        from .judging import judge_records
        from .traffic import finalize_stage

        # Evaluation is outside every target's lifetime, telemetry task and
        # generator monitor. Inference budgets do not shorten this phase.
        evaluation = asyncio.create_task(judge_records(
            config, run.records, run.pack_specs, stop_event=run.stop,
            progress=lambda event: run.emit(**event, phase="judging"),
        ))
        try:
            judging_summary = await asyncio.shield(evaluation)
        except asyncio.CancelledError:
            # Let the evaluator close its own workers and preserve partial votes.
            run.stop.set()
            judging_summary = await evaluation
        if run.stop.is_set():
            run.reason = "cancelled by user"
        limits = _check_config(config, "capacity").limits
        for target in config.targets:
            if run.stage_evidence[target.name]:
                stages = run.observations[target.name]["capacity"]
                run.observations[target.name]["capacity"] = [
                    finalize_stage(evidence, limits) | {
                        "timing_interval_ms": stages[index]["timing_interval_ms"]}
                    for index, evidence in enumerate(run.stage_evidence[target.name])]
    run.verdict_budget_guidance = _budget_guidance(config)
    checks = [check for target in config.targets for check in _checks(run, target)]
    checks = consolidate_checks(run, public_config, checks)
    counts = defaultdict(dict)
    for target in config.targets:
        for scenario in sorted({r.scenario for r in run.records if r.target == target.name}):
            counts[target.name][scenario] = _stats([r for r in run.records if r.target == target.name and r.scenario == scenario])
    warnings = ["Other traffic on the target is unknown; results cover only the recorded workload.",
                "Backend/replica identity is unverified unless explicitly reported by the server; a service URL does not establish complete replica coverage."]
    if any(check.metrics.get("low_confidence") for check in checks):
        warnings.append("Some checks have too few successful samples for confident timing conclusions.")
    if run.max_lag_ms > 100:
        warnings.append("Client scheduling delay was observed. Timing quality is qualified by overlapping request/stage intervals; raw measurements are retained. Its cause is not established.")
    if run.budget_used >= config.max_requests:
        warnings.append("Global request budget exhausted; unfinished checks remain inconclusive.")
    cancellation_options = config.test_options.get("cancellation")
    if ("cancellation" in config.checks and cancellation_options
            and cancellation_options.cancel_after_ms is not None):
        warnings.append("Legacy cancellation.cancel_after_ms does not schedule the disconnect; "
                        "cancel_after_chunks controls cancellation after useful answer chunks.")
    if run.pack_specs:
        warnings.append("Repeated, warmed workload: quiet controls and traffic repeat the same requests and may benefit caches.")
        warnings.append("Request-pack correctness covers every case; global samples does not select case count. "
                        "Without capacity, one quiet pass and a concurrent pass of at least global concurrency run. "
                        "Legacy request previews omit this extra concurrent pass.")
    observations = {"targets": run.observations, "generator_max_lag_ms": round(run.max_lag_ms, 3),
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
            for field, default in (("cancel_after_chunks", None),
                                   ("expected_provided", False), ("rubric", None),
                                   ("tool_expectation", None)):
                if spec[field] == default:
                    del spec[field]
    manifest = {"config": config_values,
                "timing_measurement_version": 2,
                **({"request_pack": pack_identity(config)} if run.pack_specs else {}),
                "workload_selection": {key: "selected" if key in public_config.checks else "not selected"
                                       for key in PUBLIC_CHECK_NAMES if key not in CORE_CHECKS},
                "effective_checks": {name: public_config.effective_check(name) for name in public_config.checks},
                "effective_traffic": (config.effective_traffic().model_dump(mode="json")
                                      if "capacity" in config.checks else None),
                "fixture_pack": {"version": FIXTURE_VERSION, "custom": config.custom_fixtures,
                                 "custom_sha256": custom_hash,
                                 "builtin_sha256": (None if run.pack_specs and set(config.checks) <= {"capacity", "correctness"}
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
                    "first_output": ["initial", "warm"], "fairness": ["fairness_baseline"],
                }.items() if name in config.checks},
                "scenario_counts": dict(counts),
                "measurement_definitions": {
                    "first_output_ms": "client-observed first non-whitespace answer or parsed tool delta; not server TTFT",
                    "generation_tokens_per_second": "(answer tokens - 1) / seconds between first and last answer chunk; estimate",
                    "aggregate": "valid completions or known output tokens / seconds from first dispatch to last completion within each scenario; includes errors in window",
                    "latency_ms": "client dispatch through protocol completion",
                    "workload_sizes": "character sizing is requested shape; server token usage is observed length",
                    "warmup": "initial and warm short requests only when required by selected checks or restart readiness; fairness and recovery use their own matched baselines"}}
    run.emit("run_finished", abort_reason=run.reason)
    return RunReport(run_id=run_id, suite_version=SUITE_VERSION,
                     started_at=started_at, finished_at=_now(),
                     overall=overall_status(checks, run.reason), manifest=manifest, checks=checks,
                     requests=run.records, warnings=warnings, observations=observations,
                     abort_reason=run.reason)
