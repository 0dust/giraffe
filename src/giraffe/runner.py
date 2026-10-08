"""Bounded, shared-traffic acceptance suite for an existing inference endpoint."""

from __future__ import annotations

import asyncio
import hashlib
from giraffe.deployment import fixture_hash
import math
import platform
import re
import socket
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


from .client import LLMClient
from .fixtures import FIXTURE_VERSION, SUITE_VERSION, builtin_fixtures, load_custom_fixtures, score_response
from .models import (
    CHECK_NAMES, CORE_CHECKS, CheckResult, RequestRecord, RequestSpec, RunConfig, RunReport, Target,
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


def _stats(records: list[RequestRecord]) -> dict:
    good = [r for r in records if r.status == "completed" and r.valid]
    latencies = [r.elapsed_ms for r in good]
    output = [r.first_output_ms for r in good if r.first_output_ms is not None]
    evaluated = [r for r in records if r.status != "cancelled"]
    errors = sum(r.status in {"failed", "timeout"} or (r.status == "completed" and not r.valid) for r in evaluated)
    rates = [r.output_tokens / (r.elapsed_ms / 1000) for r in good
             if r.output_tokens is not None and r.elapsed_ms > 0]
    generation_rates = [rate for r in good if (rate := r.generation_tokens_per_second) is not None]
    result = {
        "attempted": len(records), "completed": len(good),
        "failed": sum(r.status == "failed" or (r.status == "completed" and not r.valid)
                      for r in records),
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


def _spec(spec: RequestSpec, scenario: str, checks: list[str] = ()) -> RequestSpec:
    return spec.model_copy(update={
        "scenario": scenario,
        "check_ids": list(dict.fromkeys([*spec.check_ids, *checks, "access", "serving",
                                          "first_output", "generation"])),
    })


class _Run:
    def __init__(self, config: RunConfig, progress, stop_event):
        self.config = config
        self.progress = progress
        self.stop = stop_event or asyncio.Event()
        self.start = time.monotonic()
        self.deadline = self.start + config.max_duration_seconds
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.records: list[RequestRecord] = []
        self.attempted = 0
        self.errors = 0
        self.reason: str | None = None
        self.counts = Counter()
        base, extra = divmod(config.max_requests, len(config.targets))
        self.quotas = {t.name: base + (i < extra) for i, t in enumerate(config.targets)}
        self.unfinished = defaultdict(set)
        self.observations = {t.name: {"fairness": {"pairs": 0, "overlaps": 0},
                                     "restart": None, "metrics": [], "capacity": {}}
                             for t in config.targets}
        self.active = Counter()
        self.peak = Counter()
        self.max_lag_ms = 0.0
        self.inflight = []
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
                           "max_requests": self.config.max_requests,
                           "elapsed_seconds": round(time.monotonic() - self.start, 3), **extra})

    def allowed(self, target: str) -> bool:
        if self.stop.is_set():
            self.reason = self.reason or "cancelled by user"
        if time.monotonic() >= self.deadline:
            self.reason = self.reason or "maximum run duration reached"
        if self.errors >= self.config.stop_after_errors:
            self.reason = self.reason or "error threshold reached"
        if self.attempted >= self.config.max_requests:
            self.reason = self.reason or "maximum request budget reached"
        return not self.reason and self.attempted < self.config.max_requests and (
            self.counts[target] < self.quotas[target])

    async def request(self, target, client, spec, event=None, on_start=None, timeout_override=None):
        async with self.semaphore:
            if not self.allowed(target.name):
                self.unfinished[target.name].update(spec.check_ids)
                return None
            self.attempted += 1
            self.counts[target.name] += 1
            key = (target.name, spec.scenario)
            self.active[key] += 1
            self.peak[key] = max(self.peak[key], self.active[key])
            started, started_at = time.monotonic(), _now()
            slot = {"target": target.name, "peak": 1}
            self.inflight.append(slot)
            concurrent = sum(item["target"]==target.name for item in self.inflight)
            for item in self.inflight:
                if item["target"]==target.name:
                    item["peak"] = max(item["peak"], concurrent)
            if on_start:
                on_start()
            self.emit("request_started", target.name, spec.scenario)
            try:
                remaining = self.deadline - time.monotonic()
                global_deadline = remaining <= min(self.config.request_timeout_seconds, timeout_override or self.config.request_timeout_seconds)
                timeout = max(.001, min(self.config.request_timeout_seconds,
                                        timeout_override or self.config.request_timeout_seconds,
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
                self.inflight.remove(slot)
            # A transport may catch cancellation to preserve partial streamed output.
            if time.monotonic() >= self.deadline and record.status == "cancelled":
                record.error = "maximum run duration reached"
            record.completed_at = _now()
            record.dispatch_ms = (started-self.start)*1000
            record.completed_ms = (time.monotonic()-self.start)*1000
            record.workload = dict(spec.workload)
            record.scheduled_ms = spec.workload.get("scheduled_ms")
            record.client_wait_ms = max(0, record.dispatch_ms-record.scheduled_ms) if record.scheduled_ms is not None else None
            record.overlap = slot["peak"]
            record = score_response(spec, record)
            self.records.append(record)
            intentional = (spec.cancel_after_ms is not None and record.status == "cancelled") or (
                timeout_override is not None and record.status == "timeout")
            if not intentional and record.status != "cancelled" and (record.status != "completed" or not record.valid):
                self.errors += 1
            self.emit("request_completed", target.name, spec.scenario, status=record.status)
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
                    self.unfinished[target.name].update(spec.check_ids)
                break
            result.extend(r for r in await asyncio.gather(
                *(self.request(target, client, spec) for spec in batch)) if r is not None)
        if len(result) < count:
            self.unfinished[target.name].update(checks)
        self.emit("scenario_finished", target.name, scenario)
        return result

    async def monitor(self, tasks):
        last = time.monotonic()
        while not all(task.done() for task in tasks):
            await asyncio.sleep(.05)
            now = time.monotonic()
            self.max_lag_ms = max(self.max_lag_ms, (now-last-.05)*1000)
            last = now
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


_GPU_FAMILIES = {
    "memory": ("fb_used", "memory_used", "memory_bytes", "memory_used_bytes"),
    "utilization": ("gpu_util", "gpu_utilization", "utilization_gpu"),
    "temperature": ("gpu_temp", "temperature"),
    "throttling": ("throttle", "throttling", "clocks_event_reasons"),
    "device_errors": ("xid", "device_errors", "ecc_dbe"),
}
_METRIC_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*?\})?\s+'
                          r'([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)'
                          r'(?:\s+(\d+(?:\.\d+)?))?\s*$')


def _gpu_snapshot(snapshot):
    samples = [s for s in snapshot.get("samples", []) if s["family"] in _GPU_FAMILIES]
    families = {s["family"] for s in samples if not s["stale"]}
    return {**snapshot, "samples": samples, "unavailable": sorted(set(_GPU_FAMILIES)-families)}


async def _metrics(run, target, phase):
    from giraffe.telemetry import scrape
    snapshot = await scrape(run, target, phase, force=True)
    run.observations[target.name]["metrics"].append(_gpu_snapshot(snapshot))


async def _fairness(run, target, client, fixtures):
    observation = run.observations[target.name]["fairness"]
    if not run.config.stream or run.config.concurrency < 2:
        observation["reason"] = "Requires observable streaming and concurrency of at least two"
        run.unfinished[target.name].add("fairness")
        return
    for index in range(run.config.samples):
        if not run.allowed(target.name):
            run.unfinished[target.name].add("fairness")
            break
        event = asyncio.Event()
        short = _spec(fixtures["short"][index % len(fixtures["short"])],
                      "fairness_short", ["fairness"])
        task = asyncio.create_task(run.request(target, client, short, event))
        waiter = asyncio.create_task(event.wait())
        try:
            await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
            observation["pairs"] += 1
            if event.is_set() and not task.done():
                def on_start():
                    if not task.done():
                        observation["overlaps"] += 1
                await run.request(target, client,
                                  _spec(fixtures["long"][index % len(fixtures["long"])],
                                        "fairness_long", ["fairness"]), on_start=on_start)
            else:
                run.unfinished[target.name].add("fairness")
            await task
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(waiter, task, return_exceptions=True)


async def _context_fixtures(run, target, client, fixtures, source=None):
    """Use observed token usage to size deterministic filler, never claim estimated coverage."""
    if source is None:
        long_fixture_ids = {spec.fixture_id for spec in fixtures["long"]}
        candidates = [r for r in run.records if r.target == target.name and r.valid and
                      r.fixture_id in long_fixture_ids and r.input_tokens and r.input_chars]
        if not candidates:
            candidates = await run.group(target, client, [fixtures["context"][-2]],
                                         "context_calibration", count=1, checks=["context"])
        source = next((r for r in candidates if r.valid and r.input_tokens and r.input_chars), None)
    if source is None:
        run.observations[target.name]["context_calibration"] = {"status": "unavailable", "reason": "Server input token usage unavailable"}
        return fixtures["context"]
    desired_tokens = max(1, int((run.config.context_limit - min(24, run.config.max_output_tokens)) * .85))
    character_budget = int(source.input_chars * desired_tokens / source.input_tokens)
    fixture_context_limit = max(128, min(run.config.context_limit * 16,
                                        character_budget + min(24, run.config.max_output_tokens) + 32))
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
    return builtin_fixtures(run.config.model_copy(update={"context_limit": fixture_context_limit}))["context"]


async def _target(run: _Run, target: Target, fixtures: dict):
    config, enabled = run.config, set(run.config.checks)
    telemetry_task = None
    from giraffe import deployment, telemetry, workloads
    try:
        run.observations[target.name]["deployment"] = await deployment.snapshot(target, config, run.deadline, run.stop)
        if not await _restart(run, target):
            return
        async with LLMClient(target, config) as client:
            if config.metrics:
                data = run.observations[target.name]["serving_telemetry"]
                data["snapshots"].append(await telemetry.scrape(run, target, "start"))
                telemetry_task = asyncio.create_task(telemetry.sample_during(run, target))
            elif target.deployment.collector_url:
                telemetry_task = asyncio.create_task(telemetry.sample_during(run, target))
            initial = await run.group(target, client, fixtures["short"], "initial", count=1)
            restart = run.observations[target.name]["restart"]
            if restart and restart["status"] == "hook_completed":
                while not any(r.valid for r in initial) and run.allowed(target.name):
                    await asyncio.sleep(min(.1, max(0, run.deadline-time.monotonic())))
                    initial = await run.group(target, client, fixtures["short"], "readiness", count=1)
                restart["status"] = "ready" if any(r.valid for r in initial) else "blocked"
                restart["readiness_ms"] = round((time.monotonic()-restart.pop("started_monotonic"))*1000, 3)
                if restart["status"] == "blocked":
                    restart["reason"] = "No successful inference within readiness budget"
                    return
            await run.group(target, client, fixtures["short"], "warm")
            if "generation" in enabled:
                await run.group(target, client, fixtures["generation"], "generation",
                                checks=["generation"])
            if "capacity" in enabled:
                for level in _levels(config.concurrency):
                    records = []
                    for shape in ("short", "long"):
                        records += await run.group(target, client, fixtures[shape],
                                                   f"capacity_c{level}_{shape}", concurrency=level,
                                                   checks=["capacity"])
                    run.observations[target.name]["capacity"][str(level)] = _stats(records)
            if "fairness" in enabled:
                await _fairness(run, target, client, fixtures)
            if "context" in enabled:
                context_fixtures = await _context_fixtures(run, target, client, fixtures)
                count = max(config.samples, len(fixtures["context"]))
                for attempt in range(3):
                    records = await run.group(target, client, context_fixtures, "context",
                                              count=count, checks=["context"])
                    source = max((r for r in records if r.valid and r.input_tokens
                                  and r.input_chars), key=lambda r: r.input_tokens, default=None)
                    if source is None or source.input_tokens >= config.context_limit * .8:
                        break
                    remaining = min(config.max_requests - run.attempted,
                                    run.quotas[target.name] - run.counts[target.name])
                    if attempt == 2 or len(records) < count or remaining < count:
                        break
                    # Fixed chat-template tokens make one proportional estimate undershoot.
                    # Correct from measured usage, retaining every earlier answer and score.
                    context_fixtures = await _context_fixtures(
                        run, target, client, fixtures, source=source,
                    )
            if "correctness" in enabled:
                await run.group(target, client, fixtures["correctness"],
                                f"correctness_c{config.concurrency}",
                                count=max(config.samples, len(fixtures["correctness"])),
                                concurrency=config.concurrency, checks=["correctness"])
            if config.structured_json and "json" in enabled:
                await run.group(target, client, fixtures["json"], f"json_c{config.concurrency}",
                                concurrency=config.concurrency, checks=["json"])
            if "cancellation" in enabled:
                normal = [spec for spec in fixtures["limits"] if spec.cancel_after_ms is None]
                cancel = [spec for spec in fixtures["limits"] if spec.cancel_after_ms is not None]
                await run.group(target, client, normal, "limits",
                                count=max(config.samples, len(normal)), checks=["cancellation"])
                await run.group(target, client, cancel, "client_cancel", count=len(cancel),
                                checks=["cancellation"])
                if cancel:
                    deadline_spec = _spec(cancel[0].model_copy(update={"cancel_after_ms": None}),
                                          "client_deadline", ["cancellation"])
                    probe_timeout = min(.05, config.request_timeout_seconds)
                    run.observations[target.name]["deadline_probe_ms"] = probe_timeout * 1000
                    await run.request(target, client, deadline_spec, timeout_override=probe_timeout)
                await run.group(target, client, fixtures["short"], "cancellation_probe",
                                checks=["cancellation"])
            if "recovery" in enabled:
                end = time.monotonic() + config.sustained_seconds
                batches = 0
                while time.monotonic() < end and run.allowed(target.name):
                    remaining = run.quotas[target.name] - run.counts[target.name] - config.samples
                    if remaining <= 0:
                        run.unfinished[target.name].add("recovery")
                        break
                    await run.group(target, client, fixtures["sustained"], "sustained",
                                    count=min(config.concurrency, remaining),
                                    concurrency=config.concurrency, checks=["recovery"])
                    batches += 1
                run.observations[target.name]["sustained_batches"] = batches
                run.observations[target.name]["sustained_complete"] = time.monotonic() >= end and batches > 0
                await run.group(target, client, fixtures["short"], "recovery", checks=["recovery"])
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
        if config.metrics:
            data["snapshots"].append(await telemetry.scrape(run, target, "end"))
        else:
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


def _levels(concurrency):
    result, level = [], 1
    while level < concurrency:
        result.append(level)
        level *= 2
    return [*result, concurrency]


def _timing_violations(records, limits):
    failures = []
    for record in records:
        if record.status != "completed" or not record.valid:
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
        if limits.stream_gap_ms is not None and (not record.stream or record.max_stream_gap_ms is None):
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


def _checks(run: _Run, target: Target) -> list[CheckResult]:
    config, limits = run.config, run.config.limits
    all_records = [r for r in run.records if r.target == target.name]
    ordinary = [r for r in all_records if r.scenario not in {"client_cancel", "client_deadline"}]
    observations = run.observations[target.name]
    result = []
    warm = [r for r in ordinary if r.scenario == "warm"]
    saturated = run.max_lag_ms > max(100, (_stats(warm)["first_output_p50_ms"] or 0)*.1)
    for check_id in CORE_CHECKS:
        title = CHECK_NAMES[check_id]
        optional = check_id in {"json", "gpu"}
        records = [r for r in ordinary if check_id in r.check_ids]
        if check_id == "capacity":
            records = [r for r in records if r.scenario.startswith("capacity_c")]
        if check_id == "fairness":
            records = [r for r in records if r.scenario in {"fairness_short", "fairness_long"}]
        if check_id == "cancellation":
            records = [r for r in all_records if check_id in r.check_ids]
        if check_id in {"first_output", "generation"}:
            records = [r for r in records if r.scenario not in {"initial", "readiness"}]
        if check_id == "generation":
            records = [r for r in records if r.scenario == "generation"]
        metrics, status, summary = _stats(records), "pass", "Observed requests met configured checks."
        if check_id not in config.checks or (check_id == "json" and not config.structured_json) or (check_id == "gpu" and not config.metrics):
            status, summary = "skipped", "Not selected for this run."
        elif observations["restart"] and observations["restart"]["status"] == "blocked":
            status, summary = "blocked", observations["restart"].get("reason", "Restart readiness unavailable")
        elif observations.get("client_error"):
            status, summary = "blocked", observations["client_error"]
        elif check_id == "gpu":
            snapshots = observations["metrics"]
            missing = sorted({family for snap in snapshots for family in snap["unavailable"]})
            metrics = {"attempted": len(snapshots), "completed": sum(bool(s["samples"]) for s in snapshots),
                       "snapshots": snapshots, "unavailable_families": missing}
            status = "inconclusive" if missing or not snapshots else "pass"
            summary = "Existing GPU telemetry observed; this is not device health certification."
            if missing or not snapshots:
                summary = "GPU observations are missing or stale; unavailable does not mean healthy."
        elif not records:
            status, summary = "inconclusive", "No applicable requests completed within the run budget."
        elif check_id == "access":
            denied = [r for r in records if r.http_status in {401, 403}]
            disconnected = [r for r in records if r.http_status is None and r.status in {"failed", "timeout"}]
            if denied or disconnected:
                status = "blocked" if not any(r.valid for r in records) else "fail"
                summary = "Authentication or transport continuity failed on the configured route."
            else:
                summary = "Configured route accepted requests; TLS trust is enforced for HTTPS."
            metrics["tls"] = "verified by transport" if target.url.startswith("https:") else "not applicable (HTTP)"
            metrics["tls_expiry"] = "certificate dates not separately inspected"
        elif check_id == "serving":
            if metrics["error_rate"] is not None and metrics["error_rate"] > limits.max_error_rate:
                status, summary = "fail", "Invalid completions, errors or timeouts exceeded the configured allowance."
            else:
                summary = "Valid non-empty responses and required stream termination observed."
        elif check_id in {"first_output", "generation"}:
            metrics["initial"] = _stats([r for r in ordinary if r.scenario in {"initial", "readiness"}])
            thresholds = (limits.first_output_ms is not None if check_id == "first_output" else
                          any(v is not None for v in (limits.latency_ms, limits.stream_gap_ms,
                                                      limits.min_output_tokens_per_second)))
            violations = _timing_violations(records, limits.model_copy(update={"first_output_ms": None}))
            if check_id == "first_output":
                violations = [r.id for r in records if limits.first_output_ms and r.first_output_ms is not None and r.first_output_ms > limits.first_output_ms]
            if violations and saturated:
                status, summary = "inconclusive", "Generator saturation prevents attributing observed timing violations to the endpoint."
                metrics["unattributed_timing_violations"] = violations
            elif violations:
                status, summary = "fail", "Observed request timing exceeded a configured limit."
                metrics["violations"] = violations
            elif not thresholds:
                status, summary = "inconclusive", "Timing observed; no acceptance limit configured."
            elif saturated or metrics["completed"] < limits.min_samples:
                status, summary = "inconclusive", "Timing has too few successful samples or generator saturation."
            elif check_id == "first_output" and any(r.valid and r.first_output_ms is None for r in records):
                status, summary = "inconclusive", "Useful first-output timing unavailable for some responses."
            elif check_id == "generation" and _missing_timing(records, limits.model_copy(update={"first_output_ms": None})):
                metrics["missing_timing_metrics"] = _missing_timing(records, limits.model_copy(update={"first_output_ms": None}))
                status, summary = "inconclusive", "Configured generation limits cannot be checked with available stream or token measurements."
            elif (check_id == "generation" and limits.min_output_tokens_per_second is not None
                  and metrics["generation_rate_samples"] < limits.min_samples):
                status, summary = "inconclusive", "Too few measurable streamed answers to assess generation pace."
        elif check_id == "capacity":
            levels = observations["capacity"]
            accepted = []
            for level, stats in levels.items():
                evidence = [r for r in records if r.scenario.startswith(f"capacity_c{level}_")]
                achieved = max((run.peak[(target.name, r.scenario)] for r in evidence), default=0)
                stats["achieved_concurrency"] = achieved
                stats["timing_violations"] = len(_timing_violations(evidence, limits))
                stats["missing_timing_metrics"] = _missing_timing(evidence, limits)
                stats["accuracy"] = stats["correct"] / stats["scored"] if stats["scored"] else None
                stats["unscored_completions"] = stats["completed"] - stats["scored"]
                if stats["attempted"] >= config.samples*2 and achieved >= int(level) and stats["error_rate"] is not None and stats["error_rate"] <= limits.max_error_rate and not stats["timing_violations"] and not stats["missing_timing_metrics"] and stats["accuracy"] is not None and stats["accuracy"] >= limits.min_correctness and not stats["unscored_completions"] and not saturated:
                    accepted.append(int(level))
            metrics["levels"] = levels
            metrics["highest_tested_acceptable_concurrency"] = max(accepted, default=None)
            if any((v["timing_violations"] and not saturated) or (v["error_rate"] is not None and v["error_rate"] > limits.max_error_rate) or (v["accuracy"] is not None and v["accuracy"] < limits.min_correctness) for v in levels.values()):
                status, summary = "fail", "Some tested loads exceeded correctness, error or timing limits."
            elif not accepted or saturated or any(v["missing_timing_metrics"] or v["unscored_completions"] for v in levels.values()):
                status, summary = "inconclusive", "Insufficient concurrency, timing or scored evidence, or generator saturation."
            else:
                summary = "Reported highest tested acceptable concurrency; maximum capacity is unknown."
            if not any((limits.first_output_ms, limits.latency_ms, limits.stream_gap_ms, limits.min_output_tokens_per_second)) and status == "pass":
                status, summary = "inconclusive", "Tested valid-completion load recorded; no timing acceptance limits configured."
        elif check_id == "fairness":
            baseline = _stats(warm)
            mixed_records = [r for r in records if r.scenario == "fairness_short"]
            mixed = _stats(mixed_records)
            missing_gap = _missing_timing(mixed_records, limits.model_copy(update={
                "first_output_ms": None, "latency_ms": None, "min_output_tokens_per_second": None}))
            ratio = mixed["p50_ms"] / baseline["p50_ms"] if mixed["p50_ms"] is not None and baseline["p50_ms"] else None
            accuracy = metrics["correct"] / metrics["scored"] if metrics["scored"] else None
            metrics.update({"short_only": baseline, "mixed_short": mixed, "latency_ratio": ratio,
                            "accuracy": accuracy, "unscored_completions": metrics["completed"] - metrics["scored"],
                            **observations["fairness"]})
            if any(r.status != "cancelled" and (not r.valid or r.status != "completed") for r in records):
                status, summary = "fail", "Mixed-load requests failed; short and long traffic did not both complete successfully."
            elif accuracy is not None and accuracy < limits.min_correctness:
                status, summary = "fail", "Mixed short/long traffic failed the configured known-answer correctness limit."
            elif saturated:
                status, summary = "inconclusive", "Generator saturation prevents attributing mixed-traffic timing effects to the endpoint."
            elif limits.stream_gap_ms and mixed["max_stream_gap_ms"] is not None and mixed["max_stream_gap_ms"] > limits.stream_gap_ms:
                status, summary = "fail", "Active short-stream pauses exceeded the configured stream-gap limit under mixed traffic."
            elif ratio is not None and limits.fairness_max_ratio and ratio > limits.fairness_max_ratio:
                status, summary = "fail", "Short-stream latency increased beyond the configured mixed-traffic ratio."
            elif observations["fairness"]["overlaps"] < config.samples or ratio is None or saturated:
                status, summary = "inconclusive", "Long prefill did not observably overlap every active short stream, or timing is noisy."
            elif accuracy is None or metrics["unscored_completions"]:
                status, summary = "inconclusive", "Some mixed-load completions lack a local correctness score."
            elif missing_gap:
                metrics["missing_timing_metrics"] = missing_gap
                status, summary = "inconclusive", "Configured mixed-traffic gap limit cannot be checked without visible stream-gap measurements."
            elif limits.fairness_max_ratio is None:
                status, summary = "inconclusive", "Mixed-traffic effect observed; no acceptance ratio configured."
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
            if accuracy is not None and accuracy < limits.min_correctness:
                status, summary = "fail", "Local known-answer or schema scoring failed the configured correctness limit."
            elif metrics["completed"] != metrics["attempted"] or not metrics["scored"]:
                status, summary = "inconclusive", "Some fixtures could not be scored because inference did not complete."
            elif check_id == "context" and not metrics["near_limit_coverage"]:
                status, summary = "inconclusive", "Recall fixtures passed at observed lengths; near-limit token context was not verified."
            else:
                summary = "Built-in deterministic checks passed for the recorded fixtures and load."
        elif check_id == "cancellation":
            cancelled = [r for r in all_records if r.scenario == "client_cancel"]
            deadlines = [r for r in all_records if r.scenario == "client_deadline"]
            probes = [r for r in records if r.scenario == "cancellation_probe"]
            metrics.update({"intentional_cancellations": len(cancelled),
                            "client_disconnect_observed": sum(r.status == "cancelled" for r in cancelled),
                            "backend_cancellation": "unverified", "probes": _stats(probes),
                            "deadline_probe_ms": observations.get("deadline_probe_ms"),
                            "deadline_probes": _stats(deadlines)})
            cap_usage = {r.id: r.completion_tokens if r.completion_tokens is not None else r.output_tokens for r in records}
            bad_caps = [r.id for r in records if cap_usage[r.id] is not None and r.requested_max_tokens and cap_usage[r.id] > r.requested_max_tokens]
            failed_limits = [r.id for r in records if r.scenario == "limits" and not r.valid and r.status != "cancelled"]
            if bad_caps or failed_limits or any(r.score is False for r in records) or any(not r.valid and r.status != "cancelled" for r in probes):
                status, summary = "fail", "Output/stop limits or inference after client cancellation failed."
                metrics["output_cap_violations"] = bad_caps
                metrics["failed_limit_requests"] = failed_limits
            elif not cancelled or not all(r.status == "cancelled" for r in cancelled) or not probes or not deadlines or not all(r.status == "timeout" for r in deadlines):
                status, summary = "inconclusive", "Client disconnect, request deadline or follow-up inference could not be exercised."
            elif any(cap_usage[r.id] is None for r in records if r.scenario == "limits"):
                status, summary = "inconclusive", "Output token usage missing; exact output cap compliance is unverified."
            else:
                summary = "Client cancellation, output limits and follow-up probes observed; server work cancellation is unverified."
        elif check_id == "recovery":
            sustained = [r for r in records if r.scenario == "sustained"]
            probes = [r for r in records if r.scenario == "recovery"]
            windows = [sustained[i:i+config.concurrency] for i in range(0, len(sustained), config.concurrency)]
            metrics.update({"sustained": _stats(sustained), "recovery": _stats(probes),
                            "rolling_windows": [_stats(window) for window in windows]})
            violations = _timing_violations(records, limits)
            if (metrics["error_rate"] is not None and metrics["error_rate"] > limits.max_error_rate) or (violations and not saturated) or any(r.score is False for r in records):
                status, summary = "fail", "Repeated traffic or light-load recovery failed correctness or configured limits."
            elif saturated and any((limits.first_output_ms, limits.latency_ms, limits.stream_gap_ms, limits.min_output_tokens_per_second)):
                status, summary = "inconclusive", "Generator saturation prevents attributing sustained and recovery timing to the endpoint."
            elif _missing_timing(records, limits):
                metrics["missing_timing_metrics"] = _missing_timing(records, limits)
                status, summary = "inconclusive", "Configured sustained and recovery limits cannot be checked with available timing or token measurements."
            elif not observations.get("sustained_complete") or len(probes) < config.samples:
                status, summary = "inconclusive", "Sustained duration or light-load recovery was not fully observed."
            else:
                summary = "Bounded sustained traffic and recovery completed; unobserved internal paths remain unverified."
        if status == "fail" and check_id in {"capacity", "fairness", "cancellation", "recovery"}:
            answer_failures = _answer_failures(records, warm)
            if answer_failures:
                metrics["answer_failures"] = answer_failures
                details = "; ".join(f"{row['fixture_id']} ({row['failed']}/{row['scored']})"
                                    for row in answer_failures)
                summary = f"Answer checks failed: {details}."
                if any(row["warmup_failed"] for row in answer_failures):
                    summary += " Some of these fixtures also failed during warm-up."
                ordinary_records = [r for r in records if r.scenario not in {
                    "client_cancel", "client_deadline",
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
        if status == "pass" and check_id in run.unfinished[target.name]:
            status, summary = "inconclusive", "This check was unfinished when a run budget or stop condition was reached."
        if metrics.get("completed", 0) < limits.min_samples and status not in {"skipped", "blocked"}:
            metrics["low_confidence"] = True
        result.append(CheckResult(id=check_id, target=target.name, title=title, status=status,
                                  required=not optional and check_id in config.checks,
                                  summary=summary, metrics=metrics,
                                  evidence_ids=[r.id for r in records]))
    from giraffe.workloads import results
    return result + results(run, target, _stats, _timing_violations, _missing_timing,
                            saturated=saturated)


async def run_suite(config: RunConfig, *, progress: Callable[[dict], None] | None = None,
                    stop_event: asyncio.Event | None = None) -> RunReport:
    """Run the built-in suite with one shared load budget and preserve partial evidence."""
    started_at, run_id = _now(), uuid.uuid4().hex
    run = _Run(config, progress, stop_event)
    fixtures = builtin_fixtures(config)
    fixtures = {key: [value] if isinstance(value, RequestSpec) else list(value)
                for key, value in fixtures.items()}
    custom_hash = None
    if config.custom_fixtures:
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
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    checks = [check for target in config.targets for check in _checks(run, target)]
    counts = defaultdict(dict)
    for target in config.targets:
        for scenario in sorted({r.scenario for r in run.records if r.target == target.name}):
            counts[target.name][scenario] = _stats([r for r in run.records if r.target == target.name and r.scenario == scenario])
    warnings = ["Other traffic on the target is unknown; results cover only the recorded workload.",
                "Backend/replica identity is unverified unless explicitly reported by the server; a service URL does not establish complete replica coverage."]
    if any(check.metrics.get("low_confidence") for check in checks):
        warnings.append("Some checks have too few successful samples for confident timing conclusions.")
    if run.max_lag_ms > 100:
        warnings.append("Generator scheduling lag was observed; affected timing checks are inconclusive.")
    if run.attempted >= config.max_requests:
        warnings.append("Global request budget exhausted; unfinished checks remain inconclusive.")
    observations = {"targets": run.observations, "generator_max_lag_ms": round(run.max_lag_ms, 3),
                    "replica_coverage": [{"target": t.name, "route": t.route, "parent": t.parent,
                        "configured_url_tested": any(r.target == t.name for r in run.records),
                        "observed_models": sorted({r.observed_model for r in run.records if r.target == t.name and r.observed_model}),
                        "observed_backend_ids": sorted({r.backend_id for r in run.records if r.target == t.name and r.backend_id}),
                        "other_replicas": "unverified"} for t in config.targets]}
    from giraffe.workloads import FIXTURE_SOURCE_SHA256
    config_values = config.model_dump(mode="json")
    workload_settings = {key: config_values[key] for key in (
        "arrivals", "prefix", "buckets", "sessions", "consistency", "tool_calling",
        "forced_tool_diagnostic",
    )}
    manifest = {"config": config_values,
                "workload_selection": {key: "selected" if key in config.checks else "not selected"
                                       for key in CHECK_NAMES if key not in CORE_CHECKS},
                "fixture_pack": {"version": FIXTURE_VERSION, "custom": config.custom_fixtures,
                                 "custom_sha256": custom_hash,
                                 "builtin_sha256": fixture_hash({k: [s.model_dump() for s in v] for k,v in fixtures.items()}),
                                 "workload_fixture_sha256": fixture_hash({
                                     "generator_source_sha256": FIXTURE_SOURCE_SHA256 if any(workload_settings.values()) else None,
                                     "settings": workload_settings})},
                "run_location": {"hostname": socket.gethostname(), "system": platform.system(),
                                 "machine": platform.machine(), "python": platform.python_version()},
                "load_shape": {"levels": _levels(config.concurrency), "overlap_models": config.overlap_models,
                               "target_request_quotas": run.quotas},
                "scenario_counts": dict(counts),
                "measurement_definitions": {
                    "first_output_ms": "client-observed first non-whitespace answer or parsed tool delta; not server TTFT",
                    "generation_tokens_per_second": "(answer tokens - 1) / seconds between first and last answer chunk; estimate",
                    "aggregate": "valid completions or known output tokens / seconds from first dispatch to last completion within each scenario; includes errors in window",
                    "latency_ms": "client dispatch through protocol completion",
                    "workload_sizes": "character sizing is requested shape; server token usage is observed length",
                    "warmup": "one initial request, then config.samples short requests"}}
    run.emit("run_finished", abort_reason=run.reason)
    return RunReport(run_id=run_id, suite_version=SUITE_VERSION,
                     started_at=started_at, finished_at=_now(),
                     overall=overall_status(checks, run.reason), manifest=manifest, checks=checks,
                     requests=run.records, warnings=warnings, observations=observations,
                     abort_reason=run.reason)
