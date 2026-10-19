"""Backend verification within Limits and cancellation, using explicit runtime metadata."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from giraffe.models import RequestRecord
from giraffe.sglang_traces import TraceTail

CHECK = "cancellation"


def server_stop_result(backend: dict) -> dict:
    """Keep termination evidence independent of answers and follow-up health."""
    status = backend.get("status", "unverified")
    result = {"title": "Cancellation — verify server stopped", "status": "inconclusive",
              "summary": "No sufficient per-request termination evidence was recorded.",
              "verified": 0, "attempted": backend.get("attempted", 0)}
    if status in {"unverified", "blocked"}:
        return result | {"status": status, "summary": backend["summary"]}
    probes = backend.get("probes", [])
    verdicts = [probe.get("server_stop", {}) for probe in probes]
    result["verified"] = sum(item.get("status") == "pass" for item in verdicts)
    if any(item.get("status") == "fail" for item in verdicts):
        result.update(status="fail", summary="A matching backend request outlived the cleanup deadline or generated its full output budget after client cancellation.")
    elif verdicts and len(verdicts) == backend.get("planned") and result["verified"] == len(verdicts):
        result.update(status="pass", summary="Every cancelled request has a matching native terminal abort within the cleanup deadline.")
    elif backend.get("runtime") == "ollama":
        result["summary"] = "Ollama slot reuse is capacity-release evidence; a native abort reason is unavailable, so server cancellation is unconfirmed."
    return result


def evaluate(record: RequestRecord, trace: dict, started_at_ns: int, cleanup_seconds: float, runtime: str = "sglang") -> dict | None:
    """Only an exact, fresh server request match can affect the verdict."""
    if runtime not in {"sglang", "vllm"}:
        return None
    if (trace.get("schema_version") != 1 or trace.get("source") != {"sglang": "sglang::request", "vllm": "vllm::scheduler"}.get(runtime)
            or trace.get("request_id") != record.server_request_id
            or trace.get("model") != record.observed_model):
        return None
    start, end = trace.get("started_at_ns"), trace.get("finished_at_ns")
    received = trace.get("received_at_ns")
    if any(type(value) is not int for value in (start, end, received)):
        return None
    if not 0 < start <= end or received <= 0:
        return None
    cancelled = record.client_cancelled_at_ns
    if not cancelled:
        return None
    reasons = trace.get("finish_reasons")
    if reasons not in (["abort"], ["stop"], ["length"]):
        return None
    if runtime == "sglang":
        # SGLang converts perf_counter timestamps with a cached wall-clock
        # offset. Even on one host those values need not align with our clock.
        # The loopback receiver and client share the system monotonic clock;
        # receiving a terminal abort bounds when termination could have occurred.
        cancel_mono = record.client_cancelled_monotonic_ns
        receive_mono = trace.get("received_monotonic_ns")
        result = {"status": "inconclusive", "termination_ms": None, "trace": trace,
                  "evidence_kind": "terminal_abort" if reasons == ["abort"] else "natural_completion",
                  "timing_basis": "unavailable",
                  "reason": "Local monotonic receipt timing is unavailable; runtime clock alignment is unverified."}
        if cancel_mono is None or receive_mono is None:
            return result
        if (type(cancel_mono) is not int or type(receive_mono) is not int
                or not 0 < cancel_mono <= receive_mono):
            return None
        upper_bound = (receive_mono - cancel_mono) / 1e9
        result.update(timing_basis="local_monotonic_receipt",
                      termination_upper_bound_ms=upper_bound * 1000)
        if reasons != ["abort"]:
            result["reason"] = "Natural completion raced with client cancellation."
        elif upper_bound <= cleanup_seconds:
            result.update(status="pass", reason="Matching native abort was received within the cleanup deadline; receipt bounds termination.")
        else:
            result["reason"] = "Native abort observed, but trace receipt exceeded the cleanup deadline; exporter delay prevents determining termination time."
        return result
    if not started_at_ns <= start <= cancelled <= end <= received:
        return None
    delay = (end - cancelled) / 1e9
    if delay > cleanup_seconds:
        status, reason = "fail", "Backend request remained active beyond the cleanup deadline."
    elif reasons == ["abort"]:
        status, reason = "pass", "Matching backend request terminated with abort within the deadline."
    elif (type(trace.get("completion_tokens")) is int and record.requested_max_tokens
            and trace["completion_tokens"] == record.requested_max_tokens):
        status, reason = "fail", "Backend request completed its full output budget after client cancellation; work was not released early."
    else:
        status, reason = "inconclusive", "Natural completion raced with client cancellation."
    return {"status": status, "reason": reason, "termination_ms": delay * 1000,
            "trace": trace, "evidence_kind": "terminal_abort" if reasons == ["abort"] else "natural_completion"}


async def exercise(run, target, client, fixtures) -> None:
    options = run.config.effective_check(CHECK)
    evidence = {"status": "inconclusive", "attempted": 0, "completed": 0, "planned": options["samples"],
                "cleanup_seconds": options["cleanup_seconds"], "probes": [],
                "summary": "Runtime evidence is required; client close alone is insufficient."}
    run.observations[target.name]["backend_cancellation"] = evidence
    values = (target.metrics_profile, target.deployment.discovery,
              target.deployment.runtime.get("engine"))
    declarations = set()
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            declarations.add("invalid")
            continue
        value = value.lower()
        if value not in {"unknown", "none"}:
            declarations.add({"vllm-v1": "vllm", "vllm-legacy": "vllm"}.get(value, value))
    runtime = next(iter(declarations)) if len(declarations) == 1 else None
    evidence["runtime"] = runtime
    if runtime in {"llamacpp", "tensorrt-llm", "lmdeploy", "tgi"}:
        evidence.update(summary=f"Native per-request termination verification is unavailable for {runtime}; client close and follow-up health do not prove server termination.")
        return
    if runtime not in {"sglang", "vllm", "ollama"} or urlsplit(target.url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        evidence.update(status="blocked", summary="Requires unambiguous runtime metadata and a same-host target (shared clock).")
        return
    evidence["verification_scope"] = "capacity_release" if runtime == "ollama" else "terminal_abort"
    if not run.config.stream or not options["server_log" if runtime == "ollama" else "trace_file"]:
        return
    try:
        if runtime == "ollama":
            from giraffe.ollama_cancellation import OllamaLogTail
            tail = OllamaLogTail(options["server_log"], target)
        else:
            tail = TraceTail(options["trace_file"])
    except (OSError, ValueError):
        evidence["summary"] = "Backend evidence is unavailable or its runtime prerequisites were not met."
        return
    spec, followup_spec = fixtures["backend_cancellation"]
    run.emit("scenario_started", target.name, "backend_cancel", planned_requests=options["samples"] * (3 if runtime == "ollama" else 2))
    for _ in range(options["samples"]):
        control = None
        if runtime == "ollama":
            control_spec = spec.model_copy(update={"cancel_after_chunks": None,
                "fixture_id": "limits.backend_cancel.control.v1", "scenario": "backend_cancel_control"})
            control = await run.request(target, client, control_spec)
            if control is None:
                break
            if not control.valid:
                evidence["attempted"] += 1
                evidence["probes"].append({"request_id": control.id, "control_id": control.id,
                    "stage": "reference", "status": "fail",
                    "reason": "Uncancelled reference inference failed before the cancellation probe."})
                break
            try:
                tail.read()  # Discard completed reference admissions before this probe.
            except (OSError, ValueError):
                evidence["summary"] = "Ollama runner evidence became invalid."
                break
        started = time.time_ns()
        record = await run.request(target, client, spec)
        if record is None:
            break
        evidence["attempted"] += 1
        probe = {"request_id": record.id, "server_request_id": record.server_request_id,
                 "status": "inconclusive", "reason": "Generation was not observably cancelled after answer chunks."}
        if record.status in {"failed", "timeout"}:
            probe.update(status="fail", reason="Generation failed before cancellation could be verified.")
        evidence["probes"].append(probe)
        followup = None
        if control:
            probe["control_id"] = control.id
        if runtime == "ollama":
            # Admission into the only slot proves the previous request released
            # capacity; a fast HTTP response by itself is never evidence.
            followup = await run.request(target, client, followup_spec)
        if record.status == "cancelled" and record.client_cancelled_at_ns and record.server_request_id:
            probe["client_cancelled_at_ns"] = record.client_cancelled_at_ns
            probe["reason"] = "No sufficient matching runtime evidence arrived within the observation window."
            deadline = min(run.deadline, time.monotonic() + options["trace_wait_seconds"])
            native_rows = []
            while time.monotonic() < deadline and not run.stop.is_set():
                try:
                    rows = tail.read()
                except (OSError, ValueError):
                    probe["reason"] = "Trace evidence became unavailable or invalid."
                    break
                match = None
                if runtime == "ollama":
                    from giraffe.ollama_cancellation import evaluate_slot_reuse
                    native_rows.extend(rows)
                    if len(native_rows) > 2:
                        probe["reason"] = "Interleaved Ollama runner admissions make correlation ambiguous."
                        break
                    if (control and control.observed_model == record.observed_model
                            and control.output.startswith(record.output)):
                        match = evaluate_slot_reuse(native_rows, started, record.client_cancelled_at_ns, options["cleanup_seconds"], control)
                    else:
                        probe["reason"] = "Cancelled output did not match the deterministic reference prefix."
                        break
                else:
                    for row in rows:
                        match = evaluate(record, row, started, options["cleanup_seconds"], runtime)
                        if match:
                            break
                if match:
                    probe.update(match)
                    # Follow-up failure must not overwrite a proven termination.
                    probe["server_stop"] = {"status": match["status"], "summary": match["reason"]}
                    if runtime == "ollama" and match["status"] != "fail":
                        probe["server_stop"] = {"status": "inconclusive",
                            "summary": "Runner capacity was released; native abort reason is unavailable."}
                    evidence["completed"] += 1
                    break
                await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        if runtime != "ollama":
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
        summary = ("Every probe reused the exclusive Ollama runner slot within the deadline with less cache growth than its capped reference; native abort reason is unavailable."
                   if runtime == "ollama" else
                   "Every probe has a matching terminal abort within the cleanup deadline; follow-ups succeeded.")
        evidence.update(status="pass", summary=summary)
    else:
        evidence["summary"] = "Backend cancellation is unverified for one or more probes; inspect runtime evidence."
    run.emit("scenario_completed", target.name, "backend_cancel", status=evidence["status"])
