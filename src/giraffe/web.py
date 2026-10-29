"""Small local HTTP wrapper around the same suite and saved reports as the CLI."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import queue
import re
import secrets
import shutil
import selectors
import struct
import subprocess
import sys
import socket
import tempfile
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from concurrent.futures import Future, InvalidStateError
from contextvars import copy_context
from copy import deepcopy
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Annotated, Literal
from time import perf_counter
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from anyio import CancelScope
from pydantic import Field, ValidationError, WrapValidator

from .caps import (
    MAX_BASELINE_NAME_CHARS, MAX_LIVE_CONDITIONS, MAX_LIVE_EVENTS,
    MAX_LIVE_PHASES, MAX_LIVE_REQUESTS, MAX_MIGRATION_NOTES,
    MAX_SESSION_CREDENTIALS, MAX_WEB_IDENTIFIER_CHARS,
)
from giraffe.configuration_errors import configuration_errors as _configuration_errors
from giraffe.capabilities import capabilities
from giraffe.conditions import ConditionRequestIndex, aggregate_conditions, condition_aggregate, planned_conditions
from giraffe.deployment import safe_config
from giraffe.fixtures import load_custom_fixtures
from giraffe.latency import load_observations
from giraffe.models import (ConfigDraft, CleanupCriteria, Model, RequestRecord, RunConfig, RunReport,
                            migrate_concurrent_load, migrate_load_test, migrate_run_limits)
from giraffe.planning import request_plan
from giraffe.open_files import open_file_guard
from giraffe.preflight import APIKeyInput, ConnectionTest, ModelDiscovery, PreflightError, TokenizerSelection, attach_api_key
from giraffe.preflight import check_connection as _check_connection, discover_models as _discover_models
from giraffe.reporting import (_render, compare_baseline, html_failure_warning, reproduction_export,
                               save_baseline_report, save_failure_summary, write_comparison_report, write_report)
from giraffe.report_stream import expanded_report_chunks
from giraffe.request_pack import pack_preview
from giraffe.resources import PhaseSampler, SAMPLING
from giraffe.runner import run_suite
from giraffe.runner import record_sink
from giraffe.diagnostics import configure_logging, run_logging
from giraffe.evidence import EvidenceWriter, recover_evidence
from giraffe.storage import (DEFAULT_LIMITS, apply_cleanup, automatic_cleanup, check_start, cleanup_plan,
                             is_giraffe_run, run_alive, run_size, set_kept, storage_summary,
                             Heartbeat, write_metadata, metadata_lock as storage_metadata_lock)


logger = logging.getLogger(__name__)
_EVENT_WAIT_SECONDS = 10.0
_RECOVERY_SECONDS = 5.0
_FINALIZE_SECONDS = 12.0
_PARTIAL_REPORT_SECONDS = 30.0
_DELTA_WINDOW = 16384
_REPORT_CACHE_BYTES = 256 * 1024 * 1024
_ISOLATED_REPORT_BYTES = 256 * 1024
_IN_REPORT_PROCESS = False


@contextmanager
def _measurement_gc():
    """Keep the pre-run heap out of GC scans and attribute remaining pauses."""
    thresholds = gc.get_threshold()
    already_frozen = bool(gc.get_freeze_count())
    gc.collect()
    if not already_frozen:
        gc.freeze()
    pauses = {"gc_pause_count": 0, "gc_pause_total_ms": 0.0, "gc_pause_max_ms": 0.0}
    began = None
    def collected(phase, info):
        nonlocal began
        if phase == "start":
            began = perf_counter()
        elif began is not None:
            elapsed = (perf_counter() - began) * 1000
            pauses["gc_pause_count"] += 1
            pauses["gc_pause_total_ms"] += elapsed
            pauses["gc_pause_max_ms"] = max(pauses["gc_pause_max_ms"], elapsed)
            began = None
    gc.set_threshold(thresholds[0], thresholds[1], max(thresholds[2], 1_000_000))
    gc.callbacks.append(collected)
    try:
        yield pauses
    finally:
        gc.callbacks.remove(collected)
        gc.set_threshold(*thresholds)
        if not already_frozen:
            gc.unfreeze()


def _helper_response_cost(value):
    # Transient 404/500 responses must not survive until the file changes.
    return len(value[1]) if value[0] == 200 else _REPORT_CACHE_BYTES + 1


class _ReportCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.RLock()
        self.inflight = {}
        self.generation = 0

    def get(self, key, cost, factory):
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key][1]
            generation = self.generation
            flight_key = (generation, key)
            pending = self.inflight.get(flight_key)
            owner = pending is None
            if owner:
                pending = self.inflight[flight_key] = Future()
        if not owner:
            return pending.result()
        try:
            value = factory()
            size = cost(value) if callable(cost) else cost
            with self.lock:
                if generation == self.generation and size <= _REPORT_CACHE_BYTES:
                    while self.entries and self.bytes + size > _REPORT_CACHE_BYTES:
                        _, (removed, _) = self.entries.popitem(last=False)
                        self.bytes -= removed
                    self.entries[key] = (size, value)
                    self.bytes += size
            pending.set_result(value)
            return value
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self.lock:
                self.inflight.pop(flight_key, None)

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.bytes = 0
            self.generation += 1


_report_cache = _ReportCache()


def _file_revision(path):
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_ctime_ns


def _json_response(value):
    return Response(json.dumps(value, ensure_ascii=False, allow_nan=False,
                               separators=(",", ":")).encode(), media_type="application/json")


def _cached_json_response(key, factory):
    content = _report_cache.get(key, len, lambda: _json_response(factory()).body)
    return Response(content, media_type="application/json")


def _close_report_process(process):
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    finally:
        process.stdin.close()
        process.stdout.close()


class _ReportProcess:
    """Isolate large cold parses/JSON encoding from the measurement thread's GIL."""
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.release = None

    def close(self):
        self.process = None
        if self.release is not None:
            self.release()
            self.release = None

    def read(self, root, identifier, kind, target=None, window=None):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.close()
                package_root = str(Path(__file__).resolve().parent.parent)
                env = os.environ.copy()
                env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
                self.process = subprocess.Popen([sys.executable, "-P", "-c",
                    "import os,sys; protocol=os.fdopen(os.dup(1),'wb'); "
                    "os.dup2(2,1); sys.stdout=sys.stderr; "
                    f"package_root = {package_root!r}\n"
                    # PYTHONPATH places wheel directories ahead of the stdlib;
                    # restore their normal precedence without duplicate entries.
                    "if os.path.basename(package_root) in ('site-packages', 'dist-packages'):\n"
                    "    sys.path[:] = [p for p in sys.path if p != package_root]\n"
                    "    sys.path.append(package_root)\n"
                    "elif package_root not in sys.path:\n"
                    "    sys.path.insert(0, package_root)\n"
                    "from giraffe.web import _report_process_main; _report_process_main(protocol)"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
                self.release = weakref.finalize(self, _close_report_process, self.process)
                needs_identity = True
            else:
                needs_identity = False
            process = self.process
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    size = (root / identifier / "report.json").stat().st_size
                    deadline = time.monotonic() + 30 + size / (10 * 1024**2)
                    def read_exact(size):
                        chunks = []
                        while size:
                            if not selector.select(max(0, deadline - time.monotonic())):
                                raise HTTPException(504, "Report too large to open in the UI within "
                                                    "its time limit; use giraffe report.")
                            chunk = os.read(process.stdout.fileno(), min(size, 64 * 1024))
                            if not chunk:
                                raise HTTPException(503, "Report reader stopped; restart giraffe ui "
                                                    "from the intended Giraffe installation.")
                            chunks.append(chunk)
                            size -= len(chunk)
                        return b"".join(chunks)
                    if needs_identity:
                        import giraffe
                        process.stdin.write(b'{"identity":true}\n')
                        process.stdin.flush()
                        status, length = struct.unpack("!II", read_exact(8))
                        if status != 200 or length > 16 * 1024:
                            raise HTTPException(503, "Report reader identity handshake failed. "
                                                "Restart giraffe ui from the intended installation.")
                        identity = json.loads(read_exact(length))
                        expected = {"file": str(Path(giraffe.__file__).resolve()),
                                    "version": giraffe.__version__}
                        if identity != expected:
                            raise HTTPException(503, "Report reader identity mismatch: "
                                                f"expected {expected}, received {identity}. "
                                                "Restart giraffe ui from the intended installation.")
                    process.stdin.write((json.dumps([str(root), identifier, kind, target, window,
                                                     _REPORT_CACHE_BYTES]) + "\n").encode())
                    process.stdin.flush()
                    status, length = struct.unpack("!II", read_exact(8))
                    return status, read_exact(length)
            except BaseException:
                self.close()
                raise


def _report_process_main(protocol):
    global _IN_REPORT_PROCESS, _REPORT_CACHE_BYTES
    import httpx
    _IN_REPORT_PROCESS = True
    apps = OrderedDict()
    async def read(root, identifier, kind, target, window):
        if root not in apps:
            apps[root] = create_app(Path(root))
            if len(apps) > 2:
                apps.popitem(last=False)
        path = f"/api/runs/{identifier}" + ("/performance" if kind == "performance" else "")
        params = {key: value for key, value in (("target", target), ("window", window)) if value is not None}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[root]),
                                     base_url="http://localhost",
                                     headers={"X-Giraffe-Token": apps[root].state.giraffe_token}) as client:
            response = await client.get(path, params=params)
            return response.status_code, response.content
    with asyncio.Runner() as runner:
        for line in sys.stdin.buffer:
            request = json.loads(line)
            if request == {"identity": True}:
                import giraffe
                body = json.dumps({"file": str(Path(giraffe.__file__).resolve()),
                                   "version": giraffe.__version__}).encode()
                protocol.write(struct.pack("!II", 200, len(body)))
                protocol.write(body)
                protocol.flush()
                continue
            root, identifier, kind, target, window, budget = request
            _REPORT_CACHE_BYTES = budget
            if not budget:
                _report_cache.clear()
                apps.clear()
            status, body = runner.run(read(root, identifier, kind, target, window))
            protocol.write(struct.pack("!II", status, len(body)))
            protocol.write(body)
            protocol.flush()


class _RunLoop:
    """Application-owned measurement loop, reused across sequential runs."""
    def __init__(self):
        self.jobs = queue.Queue(maxsize=1)
        self.closing = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.current = None

    def busy(self):
        with self.lock:
            if self.thread is not None and not self.thread.is_alive():
                return False
            return self.current is not None or not self.jobs.empty()

    def submit(self, worker):
        with self.lock:
            if self.closing.is_set():
                raise RuntimeError("The run loop is shutting down")
            if self.thread is not None and not self.thread.is_alive():
                logger.error("Measurement thread died; restarting it")
                if self.current is not None and not self.current.future.done():
                    self.current.future.set_exception(RuntimeError("Measurement thread died"))
                self.current = None
                self.thread = None
                while not self.jobs.empty():
                    abandoned = self.jobs.get_nowait()
                    if not abandoned.future.done():
                        abandoned.future.set_exception(RuntimeError("Measurement thread died"))
                    self.jobs.task_done()
            if self.current is not None or not self.jobs.empty():
                raise RuntimeError("Previous run still stopping")
            self.jobs.put_nowait(worker)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="giraffe-run", daemon=True)
                self.thread.start()
            worker.thread = self.thread

    def close(self):
        with self.lock:
            self.closing.set()
            current = self.current
            thread = self.thread
        if current is not None:
            current.request_stop(cancel=True)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self):
        with asyncio.Runner() as runner:
            while True:
                try:
                    worker = self.jobs.get(timeout=.1)
                except queue.Empty:
                    if self.closing.is_set():
                        return
                    continue
                with self.lock:
                    self.current = worker
                    closing = self.closing.is_set()
                try:
                    if closing:
                        worker.future.cancel()
                    else:
                        worker._run(runner)
                finally:
                    with self.lock:
                        self.current = None
                    self.jobs.task_done()
                    if worker.owns_loop:
                        self.closing.set()


class _RunThread:
    """One measured job; only thread-safe signals cross the loop boundary."""
    def __init__(self, operation, *, owner=None):
        self.future = Future()
        self.loop = None
        self.task = None
        self.stop = None
        self.stopping = threading.Event()
        self.cancelling = threading.Event()
        self.cancel_delivered = False
        self.operation = operation
        self.thread = None
        self.context = None
        self.owns_loop = owner is None
        self.owner = owner if owner is not None else _RunLoop()

    def start(self):
        self.context = copy_context()
        self.owner.submit(self)

    def request_stop(self, *, cancel=False):
        self.stopping.set()
        if cancel:
            self.cancelling.set()
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._signal)
            except RuntimeError:
                pass  # The loop finished between checking and signalling.

    def _signal(self):
        self.stop.set()
        if self.cancelling.is_set() and not self.cancel_delivered:
            self.cancel_delivered = True
            self.task.cancel()

    def _run(self, runner):
        async def run():
            self.stop = asyncio.Event()
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.create_task(self.operation(self.stop))
            if self.stopping.is_set():
                self.loop.call_soon(self._signal)
            return await self.task
        if not self.future.set_running_or_notify_cancel():
            return
        try:
            result = runner.run(run(), context=self.context)
        except BaseException as error:
            try:
                self.future.set_exception(error)
            except InvalidStateError:
                logger.exception("Run future already completed while delivering its failure")
        else:
            try:
                self.future.set_result(result)
            except InvalidStateError:
                logger.exception("Run future already completed while delivering its result")


def _completion_future(future):
    wrapped = asyncio.wrap_future(future)
    # A shielded waiter may time out or be cancelled before the worker fails.
    wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return wrapped


def _config_input(value, handler):
    """Migrate external configs before validating; constructed models are already v2."""
    if isinstance(value, RunConfig):
        return handler(value)
    value, notes = migrate_run_limits(value)
    value, load_notes = migrate_concurrent_load(value)
    value, combined_notes = migrate_load_test(value)
    config = handler(value)
    config._migration_notes = list(dict.fromkeys([*notes, *load_notes, *combined_notes, *config._migration_notes]))
    return config


RunConfigInput = Annotated[RunConfig, WrapValidator(_config_input)]
ConfigDraftInput = Annotated[ConfigDraft, WrapValidator(_config_input)]




def _track_live_request(job: dict, event: dict, max_requests: int | None) -> None:
    """Keep a bounded slot window and count completed requests evicted while running."""
    slot = event.get("request_slot")
    if (not isinstance(slot, int) or slot < 0
            or max_requests is not None and slot >= max_requests):
        return
    kind = event["event"]
    if kind not in {"request_started", "request_completed"}:
        return
    requests = job["requests"]
    summary = job["live_request_summary"]
    job["live_request_count"] = max(job["live_request_count"], slot + 1)
    if kind == "request_started":
        request = {"slot": slot, "status": "running", "target": event.get("target"),
                   "scenario": event.get("scenario"), "condition_id": event.get("condition_id"),
                   "fixture_id": event.get("fixture_id")}
    elif event.get("request"):
        request = {**event["request"], "slot": slot, "intentional": event.get("intentional", False)}
    else:
        return
    request.update({key: event.get(key) for key in
                    ("input_chars", "requested_max_tokens", "requested_input_tokens")})
    if summary["through_slot"] is not None and slot <= summary["through_slot"]:
        if kind == "request_completed" and slot in job["evicted_running"]:
            job["evicted_running"].remove(slot)
            summary["statuses"]["running"] -= 1
            status = request["status"]
            summary["statuses"][status] = summary["statuses"].get(status, 0) + 1
        return
    requests[slot] = request
    while len(requests) > MAX_LIVE_REQUESTS:
        evicted_slot, evicted = requests.popitem(last=False)
        summary["evicted"] += 1
        summary["through_slot"] = evicted_slot
        status = evicted["status"]
        summary["statuses"][status] = summary["statuses"].get(status, 0) + 1
        if status == "running":
            job["evicted_running"].add(evicted_slot)



def _track_live_progress(job: dict, event: dict, max_requests: int | None) -> None:
    """Bound live web metadata independently of the configured arrival count."""
    # Derived performance is requested separately, never copied into polling
    # progress/events (including events from third-party runner integrations).
    event = {key: value for key, value in event.items() if key != "performance"}
    if event.get("preparation"):
        preparation = {**event["preparation"], "target": event.get("target", "")}
        scenario = preparation.get("scenario") or event.get("scenario", "")
        preparation["scenario"] = scenario
        preparations = job.setdefault("live_preparations", OrderedDict())
        key = (preparation["target"], scenario)
        preparations[key] = preparation
        preparations.move_to_end(key)
        while len(preparations) > 64:
            preparations.popitem(last=False)
    if not event["event"].startswith("condition_"):
        if not event["event"].startswith("check_") and event["event"] != "run_finished":
            if "partial_report_deadline" in job:
                event.update(phase="saving_partial_report", message="Saving partial report…")
            job["progress"] = event
        job["events"].append(event)
        del job["events"][:-MAX_LIVE_EVENTS]
    index = job.get("condition_requests")
    if index is None:
        index = job["condition_requests"] = ConditionRequestIndex()
    index.update(event)
    summary = event.get("condition_summary")
    if not summary and event["event"] in {"condition_planned", "condition_started", "condition_finished"}:
        summary = {key: event.get(key) for key in ("target", "condition", "planned", "planned_kind",
            "status", "sent", "started", "running", "completed", "failed", "not_sent", "reason")}
    if summary and summary.get("condition"):
        key = (summary["target"], summary["condition"]["id"])
        plan = job.get("condition_plans", {}).get(key)
        if plan and plan.get("planned_reason"):
            summary = {**summary, "planned_reason": plan["planned_reason"]}
        if plan and plan.get("planned") is not None and (summary.get("planned") is None or summary["planned"] < plan["planned"]):
            # Some executors announce successive one-request groups. Keep the
            # full configured condition denominator, without altering dispatch.
            summary = {**summary, "planned": plan["planned"], "planned_kind": plan["planned_kind"]}
        summary = {**summary, **{key: value for key, value in condition_aggregate(summary).items()
                   if key not in {"status", "lifecycle_status"}}}
        index.summary(summary)
        key = (summary["target"], summary["condition"]["id"])
        if key in job["conditions"] or len(job["conditions"]) < MAX_LIVE_CONDITIONS:
            job["conditions"][key] = summary
    _track_live_request(job, event, max_requests)
    if event["event"] in {"scenario_started", "scenario_finished"}:
        job["phases"].append({key: event.get(key) for key in
            ("event", "target", "scenario", "elapsed_seconds")})
        del job["phases"][:-MAX_LIVE_PHASES]


def _validated_plan(config: RunConfig | ConfigDraft, *, pack_details: dict | None = None) -> dict:
    plan = request_plan(config, pack_details=pack_details)
    if plan["errors"]:
        raise HTTPException(422, detail=plan["errors"])
    return plan


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0," + str(MAX_WEB_IDENTIFIER_CHARS - 1) + r"}\Z")
_SHUTDOWN_SECONDS = 5
_LOCAL_AUTHORITY = re.compile(
    r"(?P<host>localhost|[0-9.]+|\[[0-9a-f:.]+\])(?::(?P<port>[0-9]+))?",
    re.IGNORECASE | re.ASCII,
)


def _is_local_authority(authority: str) -> bool:
    """Validate the complete Host value without resolving an untrusted DNS name."""
    match = _LOCAL_AUTHORITY.fullmatch(authority)
    if match is None:
        return False
    if match["port"] is not None:
        port = match["port"].lstrip("0") or "0"
        if len(port) > 5 or int(port) > 65535:
            return False
    host = match["host"]
    if host.lower() == "localhost":
        return True
    try:
        address = IPv6Address(host[1:-1]) if host.startswith("[") else IPv4Address(host)
    except ValueError:
        return False
    return address.is_loopback


class TargetAPIKey(APIKeyInput):
    target: str
    url: str


class SessionCredentials(Model):
    api_keys: list[TargetAPIKey] = Field(default_factory=list, exclude=True, repr=False,
                                       max_length=MAX_SESSION_CREDENTIALS)

    def attach_api_keys(self, config: RunConfig | ConfigDraft) -> None:
        seen = set()
        for credential in self.api_keys:
            target = next((t for t in config.targets
                           if t.name == credential.target and t.url == credential.url), None)
            if target is None or credential.target in seen:
                raise HTTPException(422, "Each session API key must match one configured target and URL.")
            seen.add(credential.target)
            try:
                attach_api_key(target, credential.api_key)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None


class StartRun(SessionCredentials):
    config: RunConfigInput
    migration_notes: list[str] = Field(default_factory=list, max_length=MAX_MIGRATION_NOTES)
    baseline_id: str | None = None
    mode: Literal["normal", "cold-start"] = "normal"
    restart_target: str | None = None


class CleanupRequest(CleanupCriteria):
    ids: list[str]


class KeepRun(Model):
    keep: bool = Field(strict=True)


class CompareRun(Model):
    baseline_id: str = Field(min_length=1, max_length=MAX_WEB_IDENTIFIER_CHARS)


class DatasetPreview(Model):
    text: str
    format: Literal["csv", "json"]
    input_field: str | None = None
    expected_field: str | None = None
    scorer: Literal["judge", "exact"] = "judge"


class TryExample(SessionCredentials):
    config: ConfigDraftInput
    fixture_id: str
    target_name: str


class SaveBaseline(Model):
    run_id: str
    name: str = Field(min_length=1, max_length=MAX_BASELINE_NAME_CHARS)
    replace: bool = False


async def discover_models(body: ModelDiscovery, *, allow_manual: bool = False) -> dict:
    try:
        return await _discover_models(body, allow_manual=allow_manual)
    except PreflightError as error:
        raise HTTPException(422, str(error)) from None


async def check_connection(body: ConnectionTest) -> dict:
    try:
        return await _check_connection(body)
    except PreflightError as error:
        raise HTTPException(422, str(error)) from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: dict) -> None:
    if path.name == ".web.json":
        write_metadata(path.parent, value)
    else:
        from .reporting import _atomic_write_chunks
        _atomic_write_chunks(path, (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n",))


def _read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _directory(root: Path, identifier: str) -> Path:
    if not _ID.fullmatch(identifier):
        raise HTTPException(404, "Unknown run or baseline")
    path = root / identifier
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise HTTPException(404, "Unknown run or baseline")
    return path



def _merged_condition_rows(planned: list[dict], observed: list[dict], *, terminal=False, reason=None) -> list[dict]:
    """Keep configured cell order through completion; evidence owns verdicts."""
    rows = {(row.get("target"), (row.get("condition") or {}).get("id")): row for row in planned}
    for observed_row in observed:
        key = (observed_row.get("target"), (observed_row.get("condition") or {}).get("id"))
        prior = rows.get(key, {})
        row = {**prior, **observed_row}
        if prior.get("planned") is not None and (row.get("planned") is None or row["planned"] < prior["planned"]):
            row.update(planned=prior["planned"], planned_kind=prior["planned_kind"])
        rows[key] = row
    result = []
    for row in rows.values():
        lifecycle = row.get("status") if row.get("status") in {"waiting", "running", "finished"} else row.get("lifecycle_status", row.get("status"))
        row = {**row, "status": lifecycle}
        if terminal and lifecycle in {"waiting", "running"}:
            row.update(status="finished", running=0,
                       reason=reason or "Work was not completed before the run ended.")
            # A stale running aggregate is never a terminal passing verdict.
            row.pop("aggregate_status", None)
        verdict = {"passed": "pass", "failed": "fail"}.get(row.get("aggregate_status"), row.get("aggregate_status"))
        result.append(condition_aggregate(row, verdict=verdict))
    return result


def _api_report(report: RunReport) -> dict:
    """Keep dispatch/condition metadata; fetch retained evidence on demand."""
    result = report.model_dump(mode="json", exclude={"requests"})
    fields = {"id", "target", "fixture_id", "scenario", "condition_id", "check_ids", "status",
              "valid", "score", "elapsed_ms", "first_output_ms", "max_stream_gap_ms", "dispatch_ms", "completed_ms",
              "started_at", "completed_at", "intentional", "http_status", "error", "score_message",
              "output_chars", "stream", "finish_reason", "input_tokens", "output_tokens", "evaluation_error",
              "requested_input_tokens", "requested_output_tokens", "input_chars", "requested_max_tokens",
              "traffic_stage", "traffic_rate_rps", "traffic_class", "arrival_elapsed_ms", "arrival_first_output_ms", "load_point", "workload"}
    result.update(request_count=len(report.requests), requests_compact=True,
                  requests=[{**row.model_dump(mode="json", include=fields, exclude_defaults=True),
                             "valid": row.valid, "elapsed_ms": row.elapsed_ms, "output_chars": row.output_chars,
                             "generation_tokens_per_second": row.generation_tokens_per_second}
                            for row in report.requests])
    return result


def _report(path: Path) -> RunReport:
    if path.is_symlink():
        raise HTTPException(404, "Report unavailable")
    try:
        return _parsed_report(str(path), *_file_revision(path))
    except (OSError, ValueError) as exc:
        message = "Report unavailable or unreadable"
        if "SHA-256 mismatch" in str(exc):
            message += ": saved input SHA-256 mismatch"
        raise HTTPException(404, message) from exc


def _parsed_report(path: str, modified: int, size: int, inode: int, changed: int) -> RunReport:
    return _report_cache.get(("parsed", path, modified, size, inode, changed), max(size * 16, 1024),
        lambda: RunReport.model_validate_json(Path(path).read_text()))


_parsed_report.cache_clear = _report_cache.clear


class _WorkerCompletion(asyncio.Future):
    def cancel(self, msg=None):
        # Raw Task.cancel() cannot interrupt a running thread. Defer delivery
        # until its completion callback, without retrying an already-cancelled await.
        return False


async def _finish_shielded(task):
    completed = _WorkerCompletion()
    task.add_done_callback(lambda _: completed.set_result(None))
    cancelled = None
    with CancelScope(shield=True):
        try:
            await completed
        except asyncio.CancelledError as error:
            cancelled = error
    try:
        result = task.result()
    except BaseException:
        if cancelled is not None:
            raise cancelled
        raise
    return result, cancelled


class _ReportDownload(Response):
    def __init__(self, path, extension, identifier, lock, record_resources):
        super().__init__()
        self.path, self.extension, self.identifier = path, extension, identifier
        self.lock, self.record_resources = lock, record_resources

    def generate(self, output):
        with output.open("x", encoding="utf-8") as stream:
            if self.extension == "html":
                stream.write(_render(_report(self.path)))
            else:
                with closing(expanded_report_chunks(self.path)) as chunks:
                    for chunk in chunks:
                        stream.write(chunk)

    async def __call__(self, scope, receive, send):
        with tempfile.TemporaryDirectory(prefix="giraffe-response-") as temporary:
            output = Path(temporary) / ("report." + self.extension)
            async with self.lock:
                sampler = PhaseSampler("download_" + self.extension)
                try:
                    with sampler:
                        _, cancelled = await _finish_shielded(asyncio.create_task(
                            run_in_threadpool(self.generate, output)))
                except (OSError, ValueError) as error:
                    message = "Report unavailable or unreadable"
                    if "SHA-256 mismatch" in str(error):
                        message += ": saved input SHA-256 mismatch"
                    raise HTTPException(404, message) from error
                _, resource_cancelled = await _finish_shielded(asyncio.create_task(
                    run_in_threadpool(self.record_resources, self.path.parent, sampler)))
                if cancelled is not None or resource_cancelled is not None:
                    raise cancelled or resource_cancelled
            # Slow clients retain only their temporary file, never the render lease.
            response = FileResponse(output, filename=f"giraffe-{self.identifier}.{self.extension}",
                                    media_type="application/json" if self.extension == "json" else "text/html")
            await response(scope, receive, send)


def _targets(config: dict) -> list[dict]:
    return [{key: target.get(key, "") for key in ("name", "model", "url")}
            for target in config.get("targets", [])]


def _bootstrap_config(defaults: dict | None) -> dict:
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost:11434/v1",
                                 "model": "placeholder"}]).model_dump(mode="json")
    config["targets"][0]["model"] = ""
    if defaults is not None:
        # Launcher-provided settings are validated there; preserve a blank model for first use.
        defaults, _ = migrate_run_limits(defaults)
        defaults, _ = migrate_concurrent_load(defaults)
        defaults, _ = migrate_load_test(defaults)
        config.update(defaults)
        if "concurrent_load" not in defaults:
            config.pop("concurrent_load", None)
    config["restart_target"] = None
    return config


def create_app(runs_dir: Path = Path("runs"), defaults: dict | None = None, *,
               migration_notes: list[str] | None = None,
               token: str | None = None,
               allow_restart_command: bool = False,
               host: str = "127.0.0.1",
               port: int = 8765,
               max_total_storage: int = DEFAULT_LIMITS["max_total_storage"],
               min_free_disk: int = DEFAULT_LIMITS["min_free_disk"],
               max_run_storage: int = DEFAULT_LIMITS["max_run_storage"],
               auto_cleanup: dict | None = None) -> FastAPI:
    configure_logging()
    launch_token = token if token is not None else secrets.token_urlsafe(32)
    if not launch_token:
        raise ValueError("UI access token must not be empty")
    local_host = _is_local_authority(f"[{host}]" if ":" in host and not host.startswith("[") else host)
    limits = {"max_total_storage": max_total_storage, "min_free_disk": min_free_disk,
              "max_run_storage": max_run_storage}
    if any(type(value) is not int or value < 0 for value in limits.values()):
        raise ValueError("Storage limits must be non-negative integers; 0 disables a limit")
    policy = CleanupCriteria.model_validate(auto_cleanup or {}).model_dump(exclude_none=True)
    root = Path(runs_dir).resolve()
    baseline_root = root / ".baselines"
    if baseline_root.is_symlink():
        raise ValueError("Baseline directory cannot be a symlink")
    active: dict = {}
    run_loop = _RunLoop()
    report_process = _ReportProcess()
    # Completed feeds remain briefly addressable for the final long poll. Full
    # detail on disk is the reset source after a restart or feed eviction.
    feeds: OrderedDict[str, dict] = OrderedDict()
    start_lock = asyncio.Lock()
    recovery_pause = threading.Event()
    download_lock = asyncio.Lock()
    connection_pending = False
    unsaved_metadata: dict[str, dict] = {}
    metadata_lock = storage_metadata_lock
    last_auto_cleanup: dict = {}
    observation_cache: dict = {}

    def live_snapshot() -> dict:
        if not active:
            return {}
        result = dict(active)
        for key in ("requests", "conditions", "condition_plans", "live_preparations", "meta"):
            result[key] = active[key].copy()
        for key in ("events", "phases"):
            result[key] = list(active[key])
        return result

    def publish(job: dict, event: dict, max_requests: int | None) -> None:
        # Called exclusively on the server loop, including worker progress.
        if job is not active or job.get("terminal"):
            return
        _track_live_progress(job, event, max_requests)
        job["seq"] += 1
        event = {key: value for key, value in event.items() if key != "performance"}
        event["seq"] = job["seq"]
        job["deltas"].append(event)
        del job["deltas"][:-_DELTA_WINDOW]
        if isinstance(event.get("check"), dict):
            check = event["check"]
            job["checks"][(check.get("target", event.get("target")), check.get("id"))] = check
        job["changed"].set()

    def api_report(path: str, modified: int, size: int, inode: int, changed: int) -> dict:
        return _report_cache.get(("api", path, modified, size, inode, changed), max(size * 8, 1024),
                                lambda: _api_report(_report(Path(path))))

    def preview_plan(config: RunConfig | ConfigDraft, *, pack_details: dict | None = None,
                     validate: bool = False) -> dict:
        plan = request_plan(config, pack_details=pack_details,
                            observations=load_observations(root, config, cache=observation_cache))
        guard = open_file_guard(config, web=True)
        plan["open_files"] = guard
        plan["resource_issues"] = ([dict(issue, blocking=True) for issue in guard["errors"]]
                                   + [dict(issue, blocking=False) for issue in guard["warnings"]])
        plan["resource_warnings"] = guard["warnings"]
        plan["warnings"].extend(item["msg"] for item in guard["warnings"])
        if validate and plan["errors"]:
            raise HTTPException(422, detail=plan["errors"])
        return plan

    def metadata(directory: Path) -> dict:
        with metadata_lock:
            if directory.name in unsaved_metadata:
                return unsaved_metadata[directory.name]
            try:
                return _read(directory / ".web.json")
            except (OSError, ValueError):
                return {}

    def merge_resources(current: dict, saved: dict) -> dict:
        resources = {**saved, **current}
        peaks = dict(saved.get("sampled_peak_rss_bytes", {}))
        for phase, peak in current.get("sampled_peak_rss_bytes", {}).items():
            previous = peaks.get(phase)
            peaks[phase] = max(previous, peak) if previous is not None and peak is not None else (
                peak if peak is not None else previous)
        if peaks:
            resources["sampled_peak_rss_bytes"] = peaks
        return resources

    def save_metadata(directory: Path, meta: dict) -> None:
        with metadata_lock:
            meta = dict(meta)
            try:
                saved = _read(directory / ".web.json")
            except (OSError, ValueError):
                saved = {}
            cached = unsaved_metadata.get(directory.name, {})
            if (saved.get("state") in {"completed", "error", "interrupted"}
                    and meta.get("state") in {"running", "stopping"}):
                return  # A delayed stop/status write cannot resurrect a finished job.
            if "latency_summary" in saved:
                meta["latency_summary"] = saved["latency_summary"]
            if saved.get("heartbeat_at"):
                meta["heartbeat_at"] = saved["heartbeat_at"]
            resources = merge_resources(meta.get("resources", {}), saved.get("resources", {}))
            resources = merge_resources(resources, cached.get("resources", {}))
            if resources:
                meta["resources"] = resources
            _write(directory / ".web.json", meta)
            unsaved_metadata.pop(directory.name, None)

    def record_download_resources(directory: Path, sampler: PhaseSampler) -> None:
        with metadata_lock:
            path = directory / ".web.json"
            # A download never adopts a legacy directory or invents lifecycle state.
            if path.is_symlink() or not path.is_file():
                return
            try:
                meta = _read(path)
            except (OSError, ValueError):
                return
            measured = {"sampled_peak_rss_bytes": dict(sampler.peaks), "sampling": SAMPLING}
            meta["resources"] = merge_resources(measured, meta.get("resources", {}))
            cached = unsaved_metadata.get(directory.name)
            if cached is not None:
                cached["resources"] = merge_resources(cached.get("resources", {}), meta["resources"])
            try:
                _write(path, meta)
            except OSError:
                pass  # Resource-only failures never cache a possibly stale lifecycle state.

    def recover_interrupted(active_id=None) -> bool:
        if active_id is not None or run_loop.busy() or disk_active_id() is not None:
            return True
        pending = False
        try:
            directories = list(root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return False
        for directory in directories:
            if recovery_pause.is_set():
                return True
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            meta = metadata(directory)
            interrupted = meta.get("state") == "interrupted" and (directory / "evidence.jsonl").is_file()
            if (meta.get("state") not in {"running", "stopping"} and not interrupted
                    or not is_giraffe_run(directory, meta) or directory.name == active_id):
                continue
            if not interrupted and run_alive(directory, meta):
                pending = True
                continue
            if meta.get("recovery_retry_at", 0) > time.time():
                pending = True
                continue
            try:
                saved = _report(directory / "report.json")
            except HTTPException:
                if (directory / "summary.json").is_file():
                    continue  # A final verdict summary owns this run's outcome.
                meta.update(state="interrupted", finished_at=_now(),
                            error="The writer stopped before a complete report was saved.")
                try:
                    recovered = recover_evidence(directory, meta, pause=recovery_pause)
                    if recovery_pause.is_set():
                        return True
                    if recovered is not None:
                        write_report(recovered, directory, max_run_storage=max_run_storage)
                        meta.update(run_id=recovered.run_id, error=recovered.abort_reason)
                except Exception as error:
                    attempts = meta.get("recovery_attempts", 0) + 1
                    terminal = attempts >= 5
                    pending = pending or not terminal
                    error_id = uuid.uuid4().hex[:8]
                    with run_logging(directory):
                        if attempts == 1:
                            logger.exception("Could not recover evidence [error %s]", error_id)
                        else:
                            logger.warning("Evidence recovery attempt %s failed (%s) [error %s]",
                                           attempts, type(error).__name__, error_id)
                    reason = ("disk full" if getattr(error, "errno", None) == 28 else type(error).__name__)
                    meta.update(error_id=error_id, recovery_attempts=attempts,
                        recovery_retry_at=time.time() + _RECOVERY_SECONDS * 2 ** (attempts - 1),
                        error=f"Recovery failed: {reason} (attempt {attempts}/5). [error {error_id}]")
                    if terminal:
                        meta["state"] = "error"
            else:
                state = "interrupted" if saved.observations.get("interrupted") else "completed"
                meta.update(state=state, finished_at=saved.finished_at, run_id=saved.run_id)
                meta.pop("error", None)
            try:
                save_metadata(directory, meta)
            except OSError:
                pending = True
                unsaved_metadata[directory.name] = meta
        return pending

    async def retry_recovery(pending):
        while pending:
            await asyncio.sleep(_RECOVERY_SECONDS)
            if not active and not run_loop.busy() and not start_lock.locked():
                recovery_pause.clear()
                pending = await run_in_threadpool(recover_interrupted)

    def saving_partial(job):
        # One deadline covers shutdown and execute cancellation; retries must
        # neither shorten report construction nor restart the waiting window.
        if "partial_report_deadline" not in job:
            count = max(job.get("live_request_count", 0), job.get("progress", {}).get("completed", 0))
            job["partial_report_deadline"] = time.monotonic() + _PARTIAL_REPORT_SECONDS + min(90, count / 1000)
        job["state"] = job["meta"]["state"] = "stopping"
        # Preserve run counters without replaying a stale request/condition event.
        job["progress"] = {key: value for key, value in job["progress"].items()
                           if key in {"completed", "attempted", "elapsed_seconds"}}
        job["progress"].update(event="saving_partial_report", phase="saving_partial_report",
                               message="Saving partial report…", state="stopping")
        publish(job, job["progress"], None)
        job["meta"]["progress"] = job["progress"]
        return max(0, job["partial_report_deadline"] - time.monotonic())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pending = await run_in_threadpool(recover_interrupted)
        recovery = asyncio.create_task(retry_recovery(pending))
        try:
            yield
        finally:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
            if active:
                job = active
                job["worker"].request_stop()
                task = job["task"]
                try:
                    await asyncio.wait_for(asyncio.shield(_completion_future(job["future"])), timeout=_SHUTDOWN_SECONDS)
                except (TimeoutError, asyncio.CancelledError):
                    saving_partial(job)
                    await run_in_threadpool(save_metadata, job["directory"], job["meta"])
                    job["worker"].request_stop(cancel=True)
                except Exception:
                    logger.exception("Run worker failed during server shutdown for %s", job["id"])
                finally:
                    remaining = (max(0, job["partial_report_deadline"] - time.monotonic())
                                 if "partial_report_deadline" in job else _SHUTDOWN_SECONDS)
                    done, _ = await asyncio.wait({task}, timeout=remaining)
                    if not done:
                        saving_partial(job)
                        task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=_FINALIZE_SECONDS + 1)
                    except (asyncio.CancelledError, TimeoutError):
                        pass
                    except Exception:
                        logger.exception("Run finalization failed during shutdown for %s", job["id"])
            await run_in_threadpool(run_loop.close)
            await run_in_threadpool(report_process.close)

    app = FastAPI(title="Giraffe", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.giraffe_token = launch_token
    cookie_name = f"giraffe_token_{port}"
    app.state.giraffe_cookie_name = cookie_name

    def valid_token(value):
        return value is not None and secrets.compare_digest(value.encode(), launch_token.encode())

    def authenticated(request):
        return (valid_token(request.headers.get("x-giraffe-token"))
                or valid_token(request.cookies.get(cookie_name)))

    def require_restart_allowed(config, *, mode="normal", restart_target=None):
        if not allow_restart_command and (mode == "cold-start" or config.restart_target or restart_target):
            raise HTTPException(403, "Restart commands are disabled. Start giraffe ui with "
                                "--allow-restart-command to enable cold-start runs.")

    def local_mutation_error(request):
        # Validate before request.url uses Host, for reads as well as mutations.
        hosts = request.headers.getlist("host")
        authorized = authenticated(request)
        token_link = request.scope["path"] == "/" and valid_token(request.query_params.get("token"))
        if len(hosts) != 1 or (not _is_local_authority(hosts[0]) and local_host):
            return JSONResponse({"detail": "Use a local Giraffe host"}, status_code=400)
        if request.scope["path"].startswith("/api/") and not authorized:
            return JSONResponse({"detail": "Open the access link printed by giraffe ui"}, status_code=401)
        # A public bind can explain how to sign in before trusting its Host.
        instruction_page = request.method in {"GET", "HEAD"} and request.scope["path"] == "/"
        if not _is_local_authority(hosts[0]) and not (authorized or token_link or instruction_page):
            return JSONResponse({"detail": "Use a local Giraffe host"}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
            origin = request.headers.get("origin")
            try:
                parsed_origin = urlsplit(origin) if origin else None
                same_origin = (parsed_origin is None and authorized) or (
                    parsed_origin is not None and
                    parsed_origin.netloc == request.url.netloc
                    and parsed_origin.scheme == request.url.scheme)
            except ValueError:
                same_origin = False
            if request.headers.get("sec-fetch-site") == "cross-site" or not same_origin:
                return JSONResponse({"detail": "Use the local Giraffe page to make changes"},
                                    status_code=403)
        return None

    class LocalRequests:
        # A plain ASGI boundary avoids per-poll task groups/stream wrappers,
        # whose debug stack capture competes with measured traffic for the GIL.
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            request = Request(scope)
            path = scope["path"]
            revalidate = path == "/" or (path.startswith("/static/") and
                Path(path).suffix in {".html", ".css", ".js", ".mjs"})
            started = False
            async def response_send(message):
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    message = {**message, "headers": [*message["headers"],
                        (b"content-security-policy", b"default-src 'self'; img-src 'self' data:; "
                         b"style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         b"connect-src 'self'; frame-ancestors 'none'")]}
                    if revalidate:
                        headers = [(key, value) for key, value in message["headers"]
                                   if key.lower() != b"cache-control"]
                        message = {**message, "headers": [*headers, (b"cache-control", b"no-cache")]}
                await send(message)
            error = local_mutation_error(request)
            if error is not None:
                return await error(scope, receive, response_send)
            try:
                await self.app(scope, receive, response_send)
            except Exception as error:
                error_id = uuid.uuid4().hex[:8]
                exc_info = (type(error), error, error.__traceback__)
                await run_in_threadpool(logger.error, "Web request failed [error %s]", error_id,
                                        exc_info=exc_info)
                if started:
                    raise
                response = JSONResponse({"detail": f"Request could not finish ({type(error).__name__}). [error {error_id}]",
                                         "error_id": error_id}, status_code=500)
                await response(scope, receive, response_send)

    app.add_middleware(LocalRequests)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Input values can contain secrets pasted into the wrong field.
        return JSONResponse({"detail": _configuration_errors(error.errors(), error.body)}, status_code=422)

    @lru_cache(maxsize=256)
    def listing_report(path: str, modified: int, size: int, inode: int, changed: int) -> dict | None:
        try:
            from .report_stream import _Reader
            result, count = {}, 0
            with Path(path).open(encoding="utf-8") as stream:
                reader = _Reader(stream)
                for key in reader.members():
                    if key == "requests":
                        count = sum(1 for _ in reader.requests())
                    else:
                        value = reader.value()
                        if key in {"run_id", "overall", "started_at", "finished_at", "abort_reason"}:
                            result[key] = value
                        elif key == "checks":
                            result[key] = [{"status": check["status"]} for check in value]
                        elif key == "manifest":
                            result[key] = {"config": {"targets": _targets(value.get("config", {}))}}
                        elif key == "baseline":
                            result[key] = {"saved_baseline_id": value["saved_baseline_id"]} if (
                                value and "saved_baseline_id" in value) else None
                if reader.peek() or not result.get("run_id") or "overall" not in result:
                    raise ValueError("Invalid report summary")
            return {**result, "request_count": count}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def run_detail(identifier: str, *, listing: bool = False, snapshot: dict | None = None) -> dict:
        directory = _directory(root, identifier)
        meta = metadata(directory)
        report_path = directory / "report.json"
        summary_path = directory / "summary.json"
        summary = None
        if summary_path.is_file() and not summary_path.is_symlink():
            try:
                summary = _read(summary_path)
                meta = {**meta, "state": "error", "error": summary.get("error") or meta.get("error")
                        or "The report could not be saved. Only check summaries are available."}
            except (OSError, ValueError):
                pass
        if not meta and not report_path.is_file():
            raise HTTPException(404, "Unknown run")
        persisted = None
        if report_path.is_file() and summary is None:
            try:
                if listing:
                    persisted = listing_report(str(report_path), *_file_revision(report_path))
                    if persisted is None:
                        raise HTTPException(404, "Saved report is unreadable")
                else:
                    persisted = dict(api_report(str(report_path), *_file_revision(report_path)))
            except (HTTPException, FileNotFoundError, NotADirectoryError):
                meta = {**meta, "state": "error", "error": "Saved report is unreadable."}
        snapshot = snapshot or {}
        live = snapshot if snapshot.get("id") == identifier else {}
        state = live.get("state", meta.get("state", "completed"))
        condition_rows = list(live["conditions"].values()) if live.get("conditions") else meta.get("conditions", [])
        if not listing:
            condition_rows = _merged_condition_rows(condition_rows, (persisted or {}).get("conditions", []),
                terminal=state not in {"running", "queued", "stopping"},
                reason=(persisted or {}).get("abort_reason") or meta.get("error"))
            if persisted is not None:
                persisted["conditions"] = condition_rows
        if summary and meta.get("error_id") and meta.get("error") and meta["error_id"] not in meta["error"]:
            meta = {**meta, "error": meta["error"] + f' [error {meta["error_id"]}]'}
        return {"id": identifier, "state": state,
                "abort_reason": (persisted or summary or {}).get("abort_reason") or meta.get("abort_reason"),
                "error": meta.get("error"), "error_id": meta.get("error_id"),
                "seq": live.get("seq", meta.get("seq", 0)),
                "progress": live.get("progress", meta.get("progress", {})),
                "events": live.get("events", meta.get("events", [])),
                "live_preparations": list(live["live_preparations"].values()) if "live_preparations" in live else meta.get("live_preparations", []),
                "live_requests": list(live.get("requests", {}).values()),
                "live_request_summary": live.get("live_request_summary", meta.get("live_request_summary", {})),
                "live_request_count": live.get("live_request_count", meta.get("live_request_count", 0)),
                "live_conditions": condition_rows,
                "live_checks": list(live.get("checks", {}).values()),
                "stoppable": bool(live) and state in {"running", "stopping"},
                "phases": live.get("phases", meta.get("phases", [])),
                "config": meta.get("config") or (persisted or {}).get("manifest", {}).get("config", {}),
                "baseline_id": ((persisted or {}).get("baseline") or {}).get("saved_baseline_id", meta.get("baseline_id")),
                "report": persisted, "summary": summary,
                "resources": meta.get("resources", {}), "auto_cleanup": meta.get("auto_cleanup"),
                "bytes": run_size(directory), "kept": (directory / ".keep").is_file(),
                "started_at": (persisted or summary or {}).get("started_at", meta.get("started_at")),
                "finished_at": (persisted or summary or {}).get("finished_at", meta.get("finished_at"))}

    # Two selections per run, four runs total. Cache derived values only, never
    # expanded retained request evidence. File identity invalidates saved values.
    performance_cache = OrderedDict()
    performance_lock = threading.Lock()
    baseline_lock = threading.Lock()

    def selected_performance(identifier, target=None, window=None, snapshot=None):
        from .performance import build_performance

        directory = _directory(root, identifier)
        live = snapshot if snapshot and snapshot.get("id") == identifier and snapshot.get("state") in {"running", "stopping"} else None
        path = directory / "report.json"
        if live is None:
            if path.is_symlink() or not path.is_file():
                raise HTTPException(404, "Report is not available")
            revision = _file_revision(path)
        else:
            revision = "live"
        cached = performance_cache.get(identifier)
        if cached is None or cached["revision"] != revision:
            cached = {"revision": revision, "selections": OrderedDict()}
            performance_cache[identifier] = cached
        performance_cache.move_to_end(identifier)
        while len(performance_cache) > 4:
            performance_cache.popitem(last=False)
        selections = cached["selections"]
        selection = (target, window)
        now = time.monotonic()
        prior = selections.get(selection)
        # Debounce live requests independently of incoming request event volume.
        if prior is not None and (live is None or now - prior[0] < .5):
            selections.move_to_end(selection)
            return prior[1]
        if live is None:
            report = _report(path)
        else:
            records = []
            for row in live.get("requests", {}).values():
                if row.get("status") == "running" or not row.get("id"):
                    continue
                records.append({**{key: value for key, value in row.items()
                                   if key in RequestRecord.model_fields and value is not None},
                                "started_at": row.get("started_at") or live["meta"]["started_at"]})
            conditions = list(live.get("conditions", {}).values())
            targets = {}
            for condition in conditions:
                targets.setdefault(condition["target"], {"conditions": []})["conditions"].append(condition)
            report = RunReport.model_validate(dict(run_id=identifier, started_at=live["meta"]["started_at"],
                finished_at=_now(), overall="inconclusive", manifest={"config": live["meta"]["config"]},
                requests=records, observations={"targets": targets}), context={"metadata_only": True})
        try:
            value = build_performance(report, target=target, window=window)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        # Window selections share the target series object in the cache as
        # well as in the wire schema. No cached copy per measurement window.
        if live is None:
            for _, previous in selections.values():
                if previous["selected_target"] == value["selected_target"] and value["targets"]:
                    value["targets"][0]["request_series"] = previous["targets"][0]["request_series"]
                    break
        if live is not None:
            started = live.get("live_request_count", 0)
            retained = len(report.requests)
            value["coverage"] = dict(retained_requests=retained, started_requests=started,
                partial=True, reason="Live view uses bounded retained completed measurements; running and evicted requests are not included.")
        selections[selection] = (now, value)
        selections.move_to_end(selection)
        while len(selections) > 2:
            selections.popitem(last=False)
        return value

    @lru_cache(maxsize=8)
    def saved_condition_requests(path: str, modified: int, size: int, inode: int, changed: int) -> ConditionRequestIndex:
        return ConditionRequestIndex.from_report(_report(Path(path)))

    def run_summary(detail: dict) -> dict:
        report = detail["report"] or detail["summary"] or {}
        counts = dict.fromkeys(("pass", "fail", "inconclusive", "skipped", "blocked"), 0)
        for check in report.get("checks", []):
            counts[check["status"]] += 1
        return {key: detail[key] for key in ("id", "state", "error", "abort_reason", "progress", "baseline_id",
                                             "started_at", "finished_at", "bytes", "kept")} | {
            "run_id": report.get("run_id"), "overall": report.get("overall"),
            "targets": _targets(detail["config"]), "check_counts": counts,
            "request_count": report.get("request_count", len(report.get("requests", []))) if report else
            detail["progress"].get("completed", 0)}

    def saved_baselines() -> list[dict]:
        if not baseline_root.exists():
            return []
        rows = []
        for directory in baseline_root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                value = _read(directory / "baseline.json")
                _directory(baseline_root, directory.name)
                if (directory / "report.json").is_file():
                    rows.append(value)
            except (OSError, ValueError, HTTPException):
                continue
        return sorted(rows, key=lambda item: item.get("saved_at", ""), reverse=True)

    async def execute(job: dict, config: RunConfig, baseline: RunReport | None) -> None:
        nonlocal active
        try:
            with run_logging(job["directory"]):
                try:
                    await execute_logged(job, config, baseline)
                except Exception as error:
                    error_id = uuid.uuid4().hex[:8]
                    logger.exception("Run finalization failed [error %s]", error_id)
                    job["meta"].update(state="error", finished_at=_now(), error_id=error_id,
                        error=f"Run finalization failed ({type(error).__name__}). [error {error_id}]")
        finally:
            # No await may separate terminal publication from releasing the run.
            # A second cancellation cannot strand the next POST behind a 409.
            if job["meta"].get("state") in {"running", "stopping"}:
                job["meta"] = {**job["meta"], "state": "interrupted", "finished_at": _now()}
            job["state"] = job["meta"]["state"]
            try:
                if job is active:
                    try:
                        publish(job, {"event": "run_finished", "state": job["state"],
                                      "abort_reason": job["meta"].get("abort_reason"),
                                      "error": job["meta"].get("error"), "error_id": job["meta"].get("error_id")}, config.max_requests)
                    except Exception:
                        logger.exception("Could not publish final status for %s", job["id"])
                job["terminal"] = True
                for identifier, feed in list(feeds.items()):
                    if identifier != job["id"] and feed.get("terminal"):
                        del feeds[identifier]
                feeds[job["id"]] = {key: job[key] for key in (
                    "id", "state", "meta", "seq", "deltas", "changed", "terminal", "checks",
                    "progress", "phases", "live_preparations", "live_request_count", "live_request_summary")}
            except Exception:
                logger.exception("Could not retain terminal feed for %s", job["id"])
            finally:
                if active is job:
                    active = {}
                if job["worker"].thread is None and not job["future"].done():
                    job["future"].set_result(None)

    async def execute_logged(job: dict, config: RunConfig, baseline: RunReport | None) -> None:
        meta = dict(job["meta"])
        cancelled = None
        writer = None
        heartbeat = None
        writer_close_attempted = False
        stop_wait_attempted = False
        condition_rows = []
        export_error_id = None
        server_loop = asyncio.get_running_loop()

        def progress(event: dict) -> None:
            # Completed request payloads are newly dumped, owned dictionaries.
            # Lifecycle dictionaries still share nested counters with the runner.
            event = dict(event)
            for key in ("condition", "condition_summary", "preparation"):
                if key in event:
                    event[key] = deepcopy(event[key])
            writer.progress(event)
            try:
                server_loop.call_soon_threadsafe(publish, job, event, config.max_requests)
            except RuntimeError:
                logger.debug("Server loop closed before progress delivery for %s", job["id"])

        def export(report):
            report.manifest["invocation"] = {"source": "web_api", "argv": None}
            report.manifest["open_files"] = job["open_files"]
            if baseline is not None:
                report = compare_baseline(report, baseline)
            merged = _merged_condition_rows(condition_rows, report.conditions,
                terminal=True, reason=report.abort_reason)
            targets = report.observations.setdefault("targets", {})
            for target in config.targets:
                targets.setdefault(target.name, {})["conditions"] = [
                    {**row, "status": row["lifecycle_status"]}
                    for row in merged if row.get("target") == target.name]
            report.conditions = aggregate_conditions(report)
            sampler = PhaseSampler("export")
            try:
                with sampler:
                    write_report(report, job["directory"], max_run_storage=max_run_storage,
                                 preserve_evidence=writer is not None and not writer.stopped)
            except OSError as error:
                logger.exception("Report export failed [error %s]", export_error_id)
                if getattr(error, "json_complete", False):
                    result = dict(state="completed", finished_at=_now(), error=html_failure_warning(error))
                else:
                    message = save_failure_summary(report, job["directory"], error)
                    result = dict(state="error", finished_at=_now(), error=message)
                result["error_id"] = export_error_id
                result["error"] += f" [error {export_error_id}]"
            else:
                result = dict(state="completed", finished_at=_now())
            result.update(run_id=report.run_id, abort_reason=report.abort_reason, resources={
                "sampled_peak_rss_bytes": dict(sampler.peaks),
                "memory_measurement": "unavailable" if sampler.peaks.get("export") is None else "sampled process RSS",
                "sampling": SAMPLING})
            return result

        try:
            writer = await run_in_threadpool(EvidenceWriter, job["directory"],
                manifest={"config": safe_config(config.model_dump(mode="json"))},
                started_at=meta["started_at"])
            heartbeat = await run_in_threadpool(lambda: Heartbeat(job["directory"]).start())

            async def measured(stop):
                nonlocal writer_close_attempted
                token = record_sink.set(writer.record_finished)
                report = None
                cancellation = None
                try:
                    with _measurement_gc() as pauses:
                        try:
                            report = await run_suite(config, progress=progress, stop_event=stop)
                        except asyncio.CancelledError as error:
                            report = getattr(error, "giraffe_report", None)
                            if report is None:
                                raise
                            cancellation = error
                    report.observations.setdefault("client_timing_health", {}).update(pauses)
                    for check in report.checks:
                        progress({"event": "check_finished", "target": check.target,
                                  "check": check.model_dump(mode="json")})
                    # Pass the exception as data: asyncio may replace a task's
                    # CancelledError when it crosses a Future/loop boundary.
                finally:
                    record_sink.reset(token)
                    # The producer owns the journal through terminal request
                    # handling and report construction. Server timeout must not
                    # close it while this engine can still append records.
                    writer_close_attempted = True
                    try:
                        _, close_cancel = await _finish_shielded(asyncio.create_task(asyncio.to_thread(writer.close)))
                        cancellation = cancellation or close_cancel
                    except Exception:
                        error_id = uuid.uuid4().hex[:8]
                        logger.exception("Evidence writer failed; exporting final report [error %s]", error_id)
                        if report is not None:
                            report.warnings.append(f"Live evidence journal failed [error {error_id}]; final report assembled from completed run state.")
                return report, cancellation

            async def finish_report(report):
                nonlocal condition_rows, export_error_id
                report.observations["evidence_dropped_count"] = writer.dropped_count
                if writer.dropped_count:
                    report.warnings.append(f"{writer.dropped_count} evidence records not written to evidence.jsonl")
                condition_rows = list(job["conditions"].values())
                export_error_id = uuid.uuid4().hex[:8]
                meta.update(await run_in_threadpool(export, report))

            worker = job["worker"]
            worker.operation = measured
            worker.start()
            waiting = _completion_future(worker.future)
            try:
                report, engine_cancel = await asyncio.shield(waiting)
            except asyncio.CancelledError as error:
                cancelled = error
                stop_wait_attempted = True
                worker.request_stop(cancel=True)
                remaining = saving_partial(job)
                meta.update(state="stopping", progress=job["progress"])
                await run_in_threadpool(save_metadata, job["directory"], meta)
                # Validators may synchronously build a large partial report.
                done, _ = await asyncio.wait({waiting}, timeout=remaining)
                if not done:
                    raise
                report, engine_cancel = waiting.result()
            cancelled = engine_cancel or cancelled
            if cancelled is not None and not report.abort_reason:
                report.abort_reason = "run cancelled"
            # Export has one owner even if shutdown cancels execute during disk
            # I/O. Finish that export before finalizing metadata and re-raising.
            _, export_cancel = await _finish_shielded(asyncio.create_task(finish_report(report)))
            cancelled = cancelled or export_cancel
        except asyncio.CancelledError as error:
            cancelled = error
            if not stop_wait_attempted:
                job["worker"].request_stop(cancel=True)
                if job["worker"].thread is not None:
                    try:
                        waiting = _completion_future(job["future"])
                        await asyncio.wait({waiting}, timeout=2)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.exception("Run failed while stopping %s", job["id"])
            meta.update(state="interrupted", finished_at=_now(),
                        error="The local server stopped before a report was saved. Recorded evidence will be recovered on the next start.")
        except Exception as error:
            error_id = uuid.uuid4().hex[:8]
            logger.exception("Run could not finish [error %s]", error_id)
            meta.update(state="error", finished_at=_now(), error_id=error_id,
                        error=f"Run could not finish ({type(error).__name__}). Review the configuration and retry. [error {error_id}]")
        finally:
            async def finalize():
                nonlocal cancelled
                if (writer is not None and not writer_close_attempted
                        and (job["worker"].thread is None or job["future"].done())):
                    try:
                        await run_in_threadpool(writer.close)
                    except Exception:
                        logger.exception("Could not close evidence writer for %s", job["id"])
                if writer is not None:
                    meta["evidence_dropped_count"] = writer.dropped_count
                meta.update(progress=job["progress"], events=job["events"], phases=job["phases"],
                            live_preparations=list(job.get("live_preparations", {}).values()),
                            conditions=list(job["conditions"].values()),
                            live_request_summary=job["live_request_summary"],
                            live_request_count=job["live_request_count"])
                status_warning_added = False

                def persist_final():
                    nonlocal status_warning_added
                    warning = "Run status could not be saved to disk."
                    with metadata_lock:
                        payload = dict(meta)
                        if status_warning_added:
                            error = (payload.get("error") or "").removesuffix(warning).strip()
                            if error:
                                payload["error"] = error
                            else:
                                payload.pop("error", None)
                        try:
                            save_metadata(job["directory"], payload)
                        except OSError:
                            logger.exception("Could not save final status for %s", job["id"])
                            if warning not in (meta.get("error") or ""):
                                meta["error"] = ((meta.get("error") or "") + " " + warning).strip()
                                status_warning_added = True
                            unsaved_metadata[job["id"]] = dict(meta)
                        else:
                            meta.clear()
                            meta.update(payload)
                            status_warning_added = False
                            unsaved_metadata.pop(job["id"], None)

                try:
                    await run_in_threadpool(persist_final)
                    if policy and cancelled is None:
                        try:
                            task = asyncio.create_task(run_in_threadpool(
                                automatic_cleanup, root, policy, active_id=job["id"]))
                            result, cleanup_cancelled = await _finish_shielded(task)
                            cancelled = cleanup_cancelled or cancelled
                        except Exception as error:
                            logger.exception("Automatic cleanup failed for %s", job["id"])
                            result = {"deleted": [], "error": f"Automatic cleanup failed ({type(error).__name__})."}
                        meta.update(auto_cleanup=result, auto_cleanup_policy=policy)
                        last_auto_cleanup.clear()
                        last_auto_cleanup.update(result)
                finally:
                    try:
                        # Persist the completion cursor before waking the final poll.
                        meta["seq"] = job["seq"] + 1
                        await run_in_threadpool(persist_final)
                    finally:
                        if heartbeat is not None:
                            await run_in_threadpool(heartbeat.close)

            async def bounded_finalize():
                cleanup = asyncio.create_task(finalize())
                done, _ = await asyncio.wait({cleanup}, timeout=_FINALIZE_SECONDS)
                if not done:
                    cleanup.cancel()
                    cleanup.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                    logger.error("Run finalization exceeded its time limit for %s", job["id"])
                else:
                    cleanup.result()

            try:
                _, final_cancel = await _finish_shielded(asyncio.create_task(bounded_finalize()))
                cancelled = cancelled or final_cancel
            finally:
                job["meta"] = dict(meta)
                job["state"] = meta["state"]
        if cancelled is not None:
            raise cancelled

    def disk_active_id():
        candidates = []
        for directory in root.iterdir() if root.exists() else ():
            if (directory.is_dir() and not directory.is_symlink() and not directory.name.startswith(".")
                    and directory.name != active.get("id")):
                meta = metadata(directory)
                if meta.get("state") in {"running", "stopping"} and run_alive(directory, meta):
                    candidates.append((meta.get("started_at", ""), directory.name))
        return max(candidates, default=("", None))[1]

    @app.get("/api/bootstrap")
    async def bootstrap():
        configuration = _bootstrap_config(defaults)
        resolved = ConfigDraft.model_validate(configuration)
        migrated_defaults, limit_notes = migrate_run_limits(defaults)
        migrated_defaults, load_notes = migrate_concurrent_load(migrated_defaults)
        _, combined_notes = migrate_load_test(migrated_defaults)
        return {**capabilities(resolved, restart_allowed=allow_restart_command), "config": resolved.model_dump(mode="json"),
                "migration_notes": list(dict.fromkeys([*(migration_notes or []), *limit_notes, *load_notes, *combined_notes, *resolved._migration_notes])),
                "active_run_id": active.get("id"), "disk_active_id": await run_in_threadpool(disk_active_id), "request_plan": await run_in_threadpool(preview_plan, resolved)}

    @app.get("/api/capabilities")
    async def describe_capabilities():
        return capabilities(restart_allowed=allow_restart_command)

    @app.post("/api/targets/models")
    async def target_models(body: ModelDiscovery):
        return await discover_models(body)

    @app.post("/api/targets/tokenizer")
    async def target_tokenizer(body: TokenizerSelection):
        from giraffe.local_tokenizer import TokenizerUnavailable, preview_tokenizer

        async with start_lock:
            if active:
                raise HTTPException(409, "A run is active. Wait for it to finish or stop it "
                                    "before preparing another tokenizer.")
        try:
            return await preview_tokenizer(body.model, body.token_id)
        except TokenizerUnavailable as error:
            raise HTTPException(422, str(error)) from None

    @app.post("/api/targets/test-connection")
    async def target_connection(body: ConnectionTest):
        nonlocal connection_pending
        if active or start_lock.locked():
            raise HTTPException(409, "A run is active. Wait for it to finish or stop it "
                                "before testing another connection.")
        if connection_pending:
            raise HTTPException(409, "A connection test is already running. Wait for its result.")
        # No await between checking and reserving: probes and runs share the event loop.
        connection_pending = True
        try:
            return await check_connection(body)
        finally:
            connection_pending = False


    @app.post("/api/config/draft")
    def validate_draft(config: ConfigDraftInput):
        """Normalize a draft for editing or export without requiring run readiness."""
        return {"config": config.model_dump(mode="json"), "migration_notes": config._migration_notes, "request_plan": preview_plan(config)}

    @app.post("/api/config/plan")
    @app.post("/api/config/validate")
    def validate_config(config: RunConfigInput):
        """Normalize edits using the same contract as a run without sending traffic."""
        return {"config": config.model_dump(mode="json"), "migration_notes": config._migration_notes, "request_plan": preview_plan(config, validate=True)}

    @app.post("/api/config/preview")
    def preview_config(config: ConfigDraftInput):
        """Describe effective requests and coverage without target calls or file imports."""
        preview = pack_preview(config)
        return {"config": config.model_dump(mode="json"), "migration_notes": config._migration_notes, "request_pack_preview": preview,
                "request_plan": preview_plan(config, pack_details=preview, validate=True)}

    @app.post("/api/datasets/preview")
    def preview_dataset_import(body: DatasetPreview):
        from giraffe.dataset_import import preview_dataset

        return preview_dataset(**body.model_dump())

    @app.post("/api/runs/try-example", status_code=202)
    async def try_example(body: TryExample):
        from giraffe.dataset_import import try_example_config

        require_restart_allowed(body.config)
        body.attach_api_keys(body.config)
        try:
            config = try_example_config(body.config, body.fixture_id, body.target_name)
        except (ValidationError, ValueError) as exc:
            message = str(exc) if not isinstance(exc, ValidationError) else "; ".join(
                ".".join(map(str, item["loc"])) + ": " + item["msg"] for item in exc.errors())
            raise HTTPException(422, message) from exc
        return await start_run(StartRun(config=config, api_keys=[credential for credential
                               in body.api_keys if credential.target == body.target_name]))

    @app.get("/api/storage")
    async def get_storage():
        result = await run_in_threadpool(storage_summary, root, active_id=active.get("id"), limits=limits,
                                         auto_cleanup=policy)
        def latest_cleanup():
            if last_auto_cleanup:
                return dict(last_auto_cleanup)
            for row in result["runs"]:
                saved = metadata(root / row["id"]).get("auto_cleanup")
                if saved:
                    return saved
            return {}

        result["last_auto_cleanup"] = await run_in_threadpool(latest_cleanup)
        return result

    @app.post("/api/storage/cleanup-preview")
    async def preview_cleanup(body: CleanupCriteria):
        return await run_in_threadpool(cleanup_plan, root, active_id=active.get("id"),
                                       **body.model_dump(exclude_none=True))

    @app.post("/api/storage/cleanup")
    async def clean_storage(body: CleanupRequest):
        async with start_lock:
            for identifier in body.ids:
                _directory(root, identifier)
            return await run_in_threadpool(apply_cleanup, root, body.ids, active_id=active.get("id"),
                                           **body.model_dump(exclude={"ids"}, exclude_none=True))

    @app.post("/api/runs/{identifier}/keep")
    async def keep_run(identifier: str, body: KeepRun):
        directory = _directory(root, identifier)
        def update_keep():
            if not directory.is_dir() or not any((directory / name).is_file() for name in
                                                ("report.json", ".web.json", "summary.json")):
                raise HTTPException(404, "Unknown run")
            set_kept(directory, body.keep)
            return {"id": identifier, "kept": body.keep}

        return await run_in_threadpool(update_keep)

    @app.get("/api/runs")
    async def list_runs():
        snapshot = live_snapshot()
        return await run_in_threadpool(list_runs_saved, snapshot)

    def list_runs_saved(snapshot):
        rows = []
        for directory in (root.iterdir() if root.exists() else ()):
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            try:
                rows.append(run_summary(run_detail(directory.name, listing=True, snapshot=snapshot)))
            except HTTPException:
                continue
        return {"runs": sorted(rows, key=lambda item: item.get("started_at") or "", reverse=True),
                "active_run_id": snapshot.get("id"), "disk_active_id": disk_active_id()}

    def prepare_run(body: StartRun, *, execute: bool = True) -> tuple[RunConfig, RunReport | None]:
        """Resolve the same execution intent for Start and Copy CLI."""
        config = body.config
        if execute:
            require_restart_allowed(config, mode=body.mode, restart_target=body.restart_target)
        config._migration_notes = list(dict.fromkeys([*config._migration_notes, *body.migration_notes]))
        body.attach_api_keys(config)
        if body.mode == "normal":
            if config.restart_target or body.restart_target:
                raise HTTPException(422, "Restart requires explicit cold-start mode and target")
        else:
            if body.restart_target and "\x00" in body.restart_target:
                raise HTTPException(422, "Restart target must not contain a NUL character")
            target = next((target for target in config.targets
                           if target.name == body.restart_target), None)
            if target is None or not target.restart_command:
                raise HTTPException(422, "Cold start requires a named target with a restart command")
            if config.restart_target and config.restart_target != body.restart_target:
                raise HTTPException(422, "Restart targets must match")
            config = config.model_copy(update={"restart_target": body.restart_target})
        if config.custom_fixtures and "correctness" in config.checks:
            try:
                load_custom_fixtures(config.custom_fixtures)
            except (OSError, ValueError, ValidationError) as exc:
                raise HTTPException(422, "Custom fixtures are missing or invalid; check the local file") from exc
        baseline = _report(_directory(baseline_root, body.baseline_id) / "report.json") if body.baseline_id else None
        return config, baseline

    @app.post("/api/config/cli")
    def copy_cli(body: StartRun):
        from giraffe.handoff import cli_command

        config, baseline = prepare_run(body, execute=False)
        return {"command": cli_command(config, mode=body.mode, restart_target=body.restart_target,
                                       baseline=baseline)}

    @app.post("/api/runs", status_code=202)
    async def start_run(body: StartRun):
        nonlocal active
        async with start_lock:
            # Recovery cooperatively stops at the next journal line; admission
            # never waits for parsing. The idle retry clears this pause.
            recovery_pause.set()
            if active:
                raise HTTPException(409, "A run is already active; stop it or wait for its report")
            if run_loop.busy():
                raise HTTPException(409, "Previous run still stopping; wait for it to finish")
            if connection_pending:
                raise HTTPException(409, "A connection test is running. Wait for its result "
                                    "before starting a run.")
            config, baseline = await run_in_threadpool(prepare_run, body)
            _validated_plan(config)
            guard = open_file_guard(config, raise_limit=True, web=True)
            if guard["errors"]:
                raise HTTPException(422, detail=guard["errors"])
            try:
                await run_in_threadpool(check_start, root, limits)
            except ValueError as error:
                raise HTTPException(507, str(error)) from None
            from giraffe.local_tokenizer import TokenizerUnavailable, ensure_tokenizers
            try:
                await ensure_tokenizers(config)
            except TokenizerUnavailable as error:
                raise HTTPException(422, detail=[{
                    "loc": ["targets", error.target_index, "tokenizer"],
                    "msg": str(error), "type": "tokenizer_unavailable",
                }]) from None
            identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            directory = _directory(root, identifier)
            await run_in_threadpool(directory.mkdir, parents=True)
            meta = {"id": identifier, "state": "running", "started_at": _now(), "heartbeat_at": _now(), "owner_pid": os.getpid(), "owner_host": socket.gethostname(),
                    "config": safe_config(config.model_dump(mode="json")), "baseline_id": body.baseline_id}
            await run_in_threadpool(save_metadata, directory, meta)
            waiting = planned_conditions(config)[:MAX_LIVE_CONDITIONS]
            condition_index = ConditionRequestIndex()
            for summary in waiting:
                condition_index.summary(summary)
            active = dict(id=identifier, state="running", directory=directory, meta=meta,
                          open_files=guard,
                          progress={}, events=[], requests=OrderedDict(), live_preparations=OrderedDict(),
                          conditions={(row["target"], row["condition_id"]): row for row in waiting},
                          condition_requests=condition_index,
                          condition_plans={(row["target"], row["condition_id"]): row for row in waiting}, phases=[],
                          live_request_summary={"evicted": 0, "through_slot": None, "statuses": {}},
                          live_request_count=0, evicted_running=set(), checks={}, seq=0, deltas=[], changed=asyncio.Event())
            active["worker"] = _RunThread(None, owner=run_loop)
            active["future"] = active["worker"].future
            feeds[identifier] = active
            while len(feeds) > 4:
                feeds.popitem(last=False)
            active["task"] = asyncio.create_task(execute(active, config, baseline))
            return {"id": identifier, "state": "running", "migration_notes": config._migration_notes,
                    "live_conditions": waiting}

    @app.get("/api/runs/{identifier}")
    async def get_run(identifier: str):
        snapshot = live_snapshot()
        def read():
            directory = _directory(root, identifier)
            path = directory / "report.json"
            meta = metadata(directory)
            if (snapshot.get("id") != identifier and path.is_file() and not path.is_symlink()
                    and meta.get("state") not in {"running", "queued", "stopping"}):
                def revision(name):
                    try:
                        return _file_revision(directory / name)
                    except FileNotFoundError:
                        return None
                key = ("detail-wire", str(path), _file_revision(path),
                       revision(".web.json"), revision("summary.json"), revision(".keep"))
                if path.stat().st_size >= _ISOLATED_REPORT_BYTES and not _IN_REPORT_PROCESS:
                    status, body = _report_cache.get(key, _helper_response_cost,
                        lambda: report_process.read(root, identifier, "detail"))
                    return Response(body, status_code=status, media_type="application/json")
                return _cached_json_response(key, lambda: run_detail(identifier, snapshot=snapshot))
            return _json_response(run_detail(identifier, snapshot=snapshot))
        return await run_in_threadpool(read)

    @app.get("/api/runs/{identifier}/events")
    async def run_events(identifier: str, after: int = Query(0, ge=0)):
        _directory(root, identifier)
        deadline = asyncio.get_running_loop().time() + _EVENT_WAIT_SECONDS
        while True:
            feed = feeds.get(identifier)
            if feed is None:
                detail = await run_in_threadpool(run_detail, identifier)
                if detail["state"] in {"running", "queued", "stopping"}:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                        detail = await run_in_threadpool(run_detail, identifier)
                    if detail["state"] in {"running", "queued", "stopping"}:
                        return {"seq": detail["seq"], "reset": False, "idle": True,
                                "events": [], "records": [], "checks": [], "state": detail["state"]}
                return {"seq": detail["seq"], "reset": True, "events": [], "records": [], "checks": [],
                        "state": detail["state"], "error": detail.get("error")}
            seq = feed["seq"]
            oldest = feed["deltas"][0]["seq"] if feed["deltas"] else seq + 1
            reset = after < oldest - 1 or after > seq
            remaining = deadline - asyncio.get_running_loop().time()
            if reset or seq > after or feed.get("terminal") or remaining <= 0:
                break
            # Clearing and checking occur without yielding, so a signal cannot
            # be lost between observing the cursor and registering this wait.
            feed["changed"].clear()
            try:
                await asyncio.wait_for(feed["changed"].wait(), remaining)
            except TimeoutError:
                pass
        events = [] if reset else [row for row in feed["deltas"] if row["seq"] > after]
        records, checks = OrderedDict(), OrderedDict()
        for event in events:
            if isinstance(event.get("check"), dict):
                check = event["check"]
                checks[("check", check.get("target", event.get("target")), check.get("id"))] = check
            elif event.get("event", "").startswith("check_") and event.get("check_id"):
                check = {key: value for key, value in event.items() if key in {"target", "status", "title", "summary", "metrics", "required"}}
                check["id"] = event["check_id"]
                checks[("check", check.get("target"), check["id"])] = check
            slot = event.get("request_slot")
            if slot is not None:
                record = feed.get("requests", {}).get(slot)
                if record is None and event.get("request"):
                    record = {**event["request"], "slot": slot}
                if record is not None:
                    records[slot] = record
            summary = event.get("condition_summary")
            if summary and summary.get("condition"):
                key = (summary["target"], summary["condition"]["id"])
                checks[key] = feed.get("conditions", {}).get(key, summary)
            elif event.get("event", "").startswith("condition_") and event.get("condition"):
                key = (event.get("target"), event["condition"]["id"])
                checks[key] = feed.get("conditions", {}).get(key, event)
        return {"seq": seq, "reset": reset, "events": events, "records": list(records.values()),
                "checks": list(checks.values()), "state": feed["state"],
                "progress": feed["progress"], "live_request_count": feed["live_request_count"],
                "live_request_summary": feed["live_request_summary"],
                "live_preparations": list(feed["live_preparations"].values()),
                "phases": feed["phases"], "abort_reason": feed["meta"].get("abort_reason"),
                "error": feed["meta"].get("error"),
                "error_id": feed["meta"].get("error_id")}

    @app.get("/api/runs/{identifier}/performance")
    async def performance_selection(identifier: str, target: str | None = None, window: str | None = None):
        snapshot = live_snapshot()
        def select():
            if snapshot.get("id") != identifier or snapshot.get("state") not in {"running", "stopping"}:
                path = _directory(root, identifier) / "report.json"
                try:
                    revision = _file_revision(path)
                except FileNotFoundError:
                    raise HTTPException(404, "Report is not available") from None
                key = ("performance-wire", str(path), revision, target, window)
                if path.stat().st_size >= _ISOLATED_REPORT_BYTES and not _IN_REPORT_PROCESS:
                    status, body = _report_cache.get(key, _helper_response_cost,
                        lambda: report_process.read(root, identifier, "performance", target, window))
                    return Response(body, status_code=status, media_type="application/json")
                with performance_lock:
                    return _cached_json_response(key,
                        lambda: selected_performance(identifier, target, window, snapshot))
            with performance_lock:
                return _json_response(selected_performance(identifier, target, window, snapshot))
        return await run_in_threadpool(select)

    @app.post("/api/runs/{identifier}/compare")
    async def compare_saved_run(identifier: str, body: CompareRun):
        # Serialize with cleanup/start and report downloads. Comparison uses only
        # saved evidence; it must never start a suite or replace a reference.
        async with start_lock, download_lock:
            if active:
                raise HTTPException(409, "Wait for the active run to finish before comparing saved reports")
            detail = run_detail(identifier)
            if detail["state"] != "completed" or detail["report"] is None:
                raise HTTPException(409, "Wait for a completed report before comparing")
            reference = next((item for item in saved_baselines() if item["id"] == body.baseline_id), None)
            if reference is None:
                raise HTTPException(404, "Unknown saved baseline")
            directory = _directory(root, identifier)
            current = _report(directory / "report.json")
            baseline = _report(_directory(baseline_root, body.baseline_id) / "report.json")
            if current.run_id == baseline.run_id:
                raise HTTPException(422, "Choose a baseline from a different run")
            report = await run_in_threadpool(compare_baseline, current, baseline)
            report.baseline.update(saved_baseline_id=body.baseline_id, saved_baseline_name=reference["name"])
            warning = None
            try:
                await run_in_threadpool(write_comparison_report, report, directory, max_run_storage=max_run_storage)
            except OSError as error:
                if not getattr(error, "json_complete", False):
                    raise HTTPException(507, "Comparison could not be saved. Check storage limits and write permissions.") from None
                warning = html_failure_warning(error)
            return {"id": identifier, "baseline_id": body.baseline_id,
                    "status": report.baseline["status"], "warning": warning}

    @app.get("/api/runs/{identifier}/requests/{request_id}")
    def request_evidence(identifier: str, request_id: str):
        directory = _directory(root, identifier)
        path = directory / "report.json"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Saved request evidence is not available yet")
        from .report_stream import load_request
        try:
            record = load_request(path, request_id)
        except (OSError, ValueError) as error:
            raise HTTPException(404, "Saved request evidence is unavailable or unreadable") from error
        if record is None:
            raise HTTPException(404, "Unknown request")
        return record.model_dump(mode="json")

    @app.get("/api/runs/{identifier}/conditions/{condition_id}/requests")
    async def condition_requests(identifier: str, condition_id: str, status: str | None = None,
                                 page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=50),
                                 target: str | None = None):
        if page_size not in {10, 25, 50}:
            raise HTTPException(422, "Rows must be 10, 25 or 50")
        directory = _directory(root, identifier)
        if active.get("id") == identifier:
            index = active.setdefault("condition_requests", ConditionRequestIndex())
        else:
            return await run_in_threadpool(saved_condition_page, directory, condition_id, target,
                                           status, page, page_size)
        return condition_page(index, condition_id, target, status, page, page_size)

    def saved_condition_page(directory, condition_id, target, status, page, page_size):
        path = directory / "report.json"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Report is not available")
        index = saved_condition_requests(str(path), *_file_revision(path))
        meta = metadata(directory)
        for summary in _merged_condition_rows(meta.get("conditions", []), list(index.summaries.values()),
                terminal=True, reason=meta.get("error") or (meta.get("progress") or {}).get("reason")):
            index.summary({**summary, "status": summary["lifecycle_status"]})
        return condition_page(index, condition_id, target, status, page, page_size)

    def condition_page(index, condition_id, target, status, page, page_size):
        try:
            return index.page(condition_id, target=target, status=status, page=page, page_size=page_size)
        except KeyError:
            raise HTTPException(404, "Unknown condition") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/runs/{identifier}/config.json")
    def download_config(identifier: str):
        saved = _report(_directory(root, identifier) / "report.json")
        config = saved.manifest.get("config")
        if not isinstance(config, dict):
            raise HTTPException(404, "Saved configuration is unavailable")
        return JSONResponse(config, headers={
            "Content-Disposition": f'attachment; filename="giraffe-{identifier}-config.json"',
        })

    @app.post("/api/runs/{identifier}/cancel")
    async def cancel_run(identifier: str):
        if active.get("id") == identifier:
            job = active
            job["state"] = "stopping"
            job["meta"]["state"] = "stopping"
            job["worker"].request_stop()
            publish(job, {"event": "run_stopping", "state": "stopping"}, None)
            try:
                await run_in_threadpool(save_metadata, job["directory"], dict(job["meta"]))
            except OSError:
                pass  # Stopping traffic must not depend on available disk space.
            return {"id": identifier, "state": "stopping"}
        detail = await run_in_threadpool(run_detail, identifier)
        return {"id": identifier, "state": detail["state"]}

    @app.get("/api/runs/{identifier}/report.{extension}")
    def download_report(identifier: str, extension: str):
        if extension not in {"json", "html"}:
            raise HTTPException(404, "Unknown report format")
        directory = _directory(root, identifier)
        path = directory / "report.json"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Report is not available yet")
        return _ReportDownload(path, extension, identifier, download_lock, record_download_resources)

    @app.get("/api/runs/{identifier}/reproduction.json")
    def export_reproduction(identifier: str):
        return reproduction_export(_report(_directory(root, identifier) / "report.json"))

    @app.get("/api/baselines")
    def list_baselines():
        return {"baselines": saved_baselines()}

    @app.post("/api/baselines", status_code=201)
    async def save_baseline(body: SaveBaseline):
        snapshot = live_snapshot()
        return await run_in_threadpool(save_baseline_saved, body, snapshot)

    def save_baseline_saved(body: SaveBaseline, snapshot: dict):
        with baseline_lock:
            return save_baseline_locked(body, snapshot)

    def save_baseline_locked(body: SaveBaseline, snapshot: dict):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Choose a baseline name")
        detail = run_detail(body.run_id, snapshot=snapshot)
        if detail["state"] != "completed" or detail["report"] is None:
            raise HTTPException(409, "Wait for the run report before saving a baseline")
        existing = next((item for item in saved_baselines() if item["name"] == name), None)
        if existing and not body.replace:
            raise HTTPException(409, "That baseline name exists; explicitly replace it or use another name")
        identifier = existing["id"] if existing else uuid.uuid4().hex
        directory = _directory(baseline_root, identifier)
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        summary = {"id": identifier, "name": name, "run_id": body.run_id,
                   "saved_at": _now(), "overall": detail["report"]["overall"],
                   "targets": _targets(detail["config"])}
        try:
            save_baseline_report(_directory(root, body.run_id) / "report.json",
                                 directory / "report.json", replace=True)
            _write(directory / "baseline.json", summary)
        except BaseException as error:
            if created:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    pass
            if isinstance(error, ValueError):
                raise HTTPException(422, "Baseline source is invalid or changed; reload the run and try again.") from None
            if isinstance(error, OSError):
                raise HTTPException(507, "Baseline could not be saved. Check disk space and write permissions.") from None
            raise
        return summary

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.api_route("/", methods=["GET", "HEAD"])
    async def index(request: Request):
        supplied_token = request.query_params.get("token")
        if supplied_token is not None:
            if not valid_token(supplied_token):
                raise HTTPException(401, "Invalid Giraffe access token")
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(cookie_name, launch_token, httponly=True, samesite="strict", path="/")
            return response
        if not authenticated(request):
            return HTMLResponse("<!doctype html><title>Giraffe access</title><p>"
                "Open the link printed by `giraffe ui` (it contains your access token).</p>")
        return FileResponse(static / "index.html")

    return app
