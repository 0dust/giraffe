"""Opt-in, same-host cancellation verification using native SGLang terminal spans."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from giraffe.models import RequestRecord
from giraffe.sglang_traces import TraceTail

CHECK = "sglang_cancellation"


def evaluate(record: RequestRecord, trace: dict, started_at_ns: int, cleanup_seconds: float) -> dict | None:
    """Only an exact, fresh server request match can affect the verdict."""
    if (trace.get("schema_version") != 1 or trace.get("source") != "sglang::request"
            or trace.get("request_id") != record.server_request_id
            or trace.get("model") != record.observed_model):
        return None
    start, end = trace.get("started_at_ns"), trace.get("finished_at_ns")
    received = trace.get("received_at_ns")
    if any(type(value) is not int for value in (start, end, received)):
        return None
    cancelled = record.client_cancelled_at_ns
    if not cancelled or not started_at_ns <= start <= cancelled <= end <= received:
        return None
    reasons = trace.get("finish_reasons")
    if reasons not in (["abort"], ["stop"], ["length"]):
        return None
    delay = (end - cancelled) / 1e9
    if delay > cleanup_seconds:
        status, reason = "fail", "Backend request remained active beyond the cleanup deadline."
    elif reasons == ["abort"]:
        status, reason = "pass", "Matching backend request terminated with abort within the deadline."
    else:
        status, reason = "inconclusive", "Natural completion raced with client cancellation."
    return {"status": status, "reason": reason, "termination_ms": delay * 1000,
            "trace": trace}


async def exercise(run, target, client, fixtures) -> None:
    options = run.config.effective_check(CHECK)
    evidence = {"status": "inconclusive", "attempted": 0, "completed": 0,
                "cleanup_seconds": options["cleanup_seconds"], "probes": [],
                "summary": "Native SGLang terminal tracing is required; client close is insufficient."}
    run.observations[target.name][CHECK] = evidence
    declared = (target.metrics_profile == "sglang" or target.deployment.discovery == "sglang"
                or target.deployment.runtime.get("engine") == "sglang")
    if not declared or urlsplit(target.url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        evidence.update(status="blocked", summary="Requires a declared SGLang target on this host (shared clock).")
        return
    if not run.config.stream or not options["trace_file"]:
        return
    try:
        tail = TraceTail(options["trace_file"])
    except (OSError, ValueError):
        evidence["summary"] = "Trace file unavailable; start giraffe sglang-traces first."
        return
    spec, followup_spec = fixtures[CHECK]
    run.emit("scenario_started", target.name, "sglang_cancel", planned_requests=options["samples"] * 2)
    for _ in range(options["samples"]):
        started = time.time_ns()
        record = await run.request(target, client, spec)
        if record is None:
            break
        evidence["attempted"] += 1
        probe = {"request_id": record.id, "server_request_id": record.server_request_id,
                 "status": "inconclusive", "reason": "Generation was not observably cancelled after answer chunks."}
        evidence["probes"].append(probe)
        if record.status == "cancelled" and record.client_cancelled_at_ns and record.server_request_id:
            probe["client_cancelled_at_ns"] = record.client_cancelled_at_ns
            probe["reason"] = "No matching terminal trace arrived within the observation window."
            deadline = min(run.deadline, time.monotonic() + options["trace_wait_seconds"])
            while time.monotonic() < deadline and not run.stop.is_set():
                try:
                    rows = tail.read()
                except (OSError, ValueError):
                    probe["reason"] = "Trace evidence became unavailable or invalid."
                    break
                match = None
                for row in rows:
                    match = evaluate(record, row, started, options["cleanup_seconds"])
                    if match:
                        break
                if match:
                    probe.update(match)
                    evidence["completed"] += 1
                    break
                await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        followup = await run.request(target, client, followup_spec)
        probe["followup_id"] = followup.id if followup else None
        probe["followup_valid"] = bool(followup and followup.valid)
        if followup and not probe["followup_valid"]:
            probe.update(status="fail", reason="Follow-up inference failed after cancellation.")
        if followup is None:
            if probe["status"] != "fail":
                probe.update(status="inconclusive", reason="Follow-up did not fit within the run budget.")
            break
    statuses = [probe["status"] for probe in evidence["probes"]]
    if "fail" in statuses:
        evidence.update(status="fail", summary="Backend cancellation or follow-up inference failed; inspect probe evidence.")
    elif len(statuses) == options["samples"] and all(status == "pass" for status in statuses):
        evidence.update(status="pass", summary="Every cancelled request has a matching native abort termination within the cleanup deadline; follow-ups succeeded.")
    else:
        evidence["summary"] = "Backend cancellation is unverified for one or more probes; inspect trace coverage."
    run.emit("scenario_completed", target.name, "sglang_cancel", status=evidence["status"])
