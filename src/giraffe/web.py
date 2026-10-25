"""Small local HTTP wrapper around the same suite and saved reports as the CLI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from functools import lru_cache
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
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
                               save_baseline_report, save_failure_summary, write_report)
from giraffe.report_stream import expanded_report_chunks
from giraffe.request_pack import pack_preview
from giraffe.resources import PhaseSampler, SAMPLING
from giraffe.runner import run_suite
from giraffe.storage import (DEFAULT_LIMITS, apply_cleanup, automatic_cleanup, check_start, cleanup_plan,
                             is_giraffe_run, run_alive, run_size, set_kept, storage_summary)


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
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")
    temporary.replace(path)


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
              "valid", "score", "elapsed_ms", "first_output_ms", "dispatch_ms", "completed_ms",
              "started_at", "completed_at", "intentional", "http_status", "error", "score_message",
              "output_chars", "stream", "finish_reason", "input_tokens", "output_tokens", "evaluation_error",
              "requested_input_tokens", "requested_output_tokens", "input_chars", "requested_max_tokens",
              "traffic_stage", "traffic_rate_rps", "traffic_class", "load_point", "workload"}
    result.update(request_count=len(report.requests), requests_compact=True,
                  requests=[{**row.model_dump(mode="json", include=fields, exclude_defaults=True),
                             "valid": row.valid, "elapsed_ms": row.elapsed_ms, "output_chars": row.output_chars}
                            for row in report.requests])
    return result


def _report(path: Path) -> RunReport:
    if path.is_symlink():
        raise HTTPException(404, "Report unavailable")
    try:
        return RunReport.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        message = "Report unavailable or unreadable"
        if "SHA-256 mismatch" in str(exc):
            message += ": saved input SHA-256 mismatch"
        raise HTTPException(404, message) from exc


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
    if defaults is None:
        config["checks"] = ["access", "correctness"]
    else:
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
               max_total_storage: int = DEFAULT_LIMITS["max_total_storage"],
               min_free_disk: int = DEFAULT_LIMITS["min_free_disk"],
               max_run_storage: int = DEFAULT_LIMITS["max_run_storage"],
               auto_cleanup: dict | None = None) -> FastAPI:
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
    start_lock = asyncio.Lock()
    download_lock = asyncio.Lock()
    connection_pending = False
    unsaved_metadata: dict[str, dict] = {}
    metadata_lock = threading.RLock()
    last_auto_cleanup: dict = {}
    observation_cache: dict = {}

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
            try:
                saved = _read(directory / ".web.json")
            except (OSError, ValueError):
                saved = {}
            cached = unsaved_metadata.get(directory.name, {})
            if "latency_summary" in saved:
                meta["latency_summary"] = saved["latency_summary"]
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

    def recover_interrupted() -> None:
        try:
            directories = list(root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return
        for directory in directories:
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            meta = metadata(directory)
            if (meta.get("state") not in {"running", "stopping"} or run_alive(directory, meta)
                    or not is_giraffe_run(directory, meta)):
                continue
            try:
                saved = _report(directory / "report.json")
            except HTTPException:
                meta.update(state="interrupted", finished_at=_now(),
                            error="The writer stopped before a complete report was saved.")
            else:
                meta.update(state="completed", finished_at=saved.finished_at, run_id=saved.run_id)
                meta.pop("error", None)
            try:
                save_metadata(directory, meta)
            except OSError:
                unsaved_metadata[directory.name] = meta

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(recover_interrupted)
        yield
        if active:
            active["stop"].set()
            task = active["task"]
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_SHUTDOWN_SECONDS)
            except TimeoutError:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2)
                except (TimeoutError, asyncio.CancelledError):
                    pass

    app = FastAPI(title="Giraffe", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def local_mutations(request: Request, call_next):
        # Validate before request.url uses Host, for reads as well as mutations.
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or not _is_local_authority(hosts[0]):
            return JSONResponse({"detail": "Use a local Giraffe host"}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
            origin = request.headers.get("origin")
            try:
                parsed_origin = urlsplit(origin) if origin else None
                same_origin = parsed_origin is None or (
                    parsed_origin.netloc == request.url.netloc
                    and parsed_origin.scheme == request.url.scheme)
            except ValueError:
                same_origin = False
            if request.headers.get("sec-fetch-site") == "cross-site" or not same_origin:
                return JSONResponse({"detail": "Use the local Giraffe page to make changes"},
                                    status_code=403)
        response = await call_next(request)
        path = request.url.path
        # Mutable frontend assets must revalidate across upgrades, including 304 responses.
        if path == "/" or (path.startswith("/static/")
                           and Path(path).suffix in {".html", ".css", ".js", ".mjs"}):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Input values can contain secrets pasted into the wrong field.
        return JSONResponse({"detail": _configuration_errors(error.errors(), error.body)}, status_code=422)

    @lru_cache(maxsize=256)
    def listing_report(path: str, modified: int, size: int) -> dict | None:
        try:
            saved = _report(Path(path))
        except HTTPException:
            return None
        return {"run_id": saved.run_id, "overall": saved.overall,
                "started_at": saved.started_at, "finished_at": saved.finished_at,
                "request_count": len(saved.requests),
                "checks": [{"status": check.status} for check in saved.checks],
                "manifest": {"config": {"targets": _targets(saved.manifest.get("config", {}))}}}

    def run_detail(identifier: str, *, listing: bool = False) -> dict:
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
                    stat = report_path.stat()
                    persisted = listing_report(str(report_path), stat.st_mtime_ns, stat.st_size)
                    if persisted is None:
                        raise HTTPException(404, "Saved report is unreadable")
                else:
                    persisted = _api_report(_report(report_path))
            except (HTTPException, FileNotFoundError, NotADirectoryError):
                meta = {**meta, "state": "error", "error": "Saved report is unreadable."}
        live = active if active.get("id") == identifier else {}
        state = live.get("state", meta.get("state", "completed"))
        condition_rows = list(live["conditions"].values()) if live.get("conditions") else meta.get("conditions", [])
        if not listing:
            condition_rows = _merged_condition_rows(condition_rows, (persisted or {}).get("conditions", []),
                terminal=state not in {"running", "queued", "stopping"},
                reason=(persisted or {}).get("abort_reason") or meta.get("error"))
            if persisted is not None:
                persisted["conditions"] = condition_rows
        return {"id": identifier, "state": state,
                "error": meta.get("error"), "progress": live.get("progress", meta.get("progress", {})),
                "events": live.get("events", meta.get("events", [])),
                "live_preparations": list(live["live_preparations"].values()) if "live_preparations" in live else meta.get("live_preparations", []),
                "live_requests": list(live.get("requests", {}).values()),
                "live_request_summary": live.get("live_request_summary", meta.get("live_request_summary", {})),
                "live_request_count": live.get("live_request_count", meta.get("live_request_count", 0)),
                "live_conditions": condition_rows,
                "phases": live.get("phases", meta.get("phases", [])),
                "config": meta.get("config") or (persisted or {}).get("manifest", {}).get("config", {}),
                "baseline_id": meta.get("baseline_id"), "report": persisted, "summary": summary,
                "resources": meta.get("resources", {}), "auto_cleanup": meta.get("auto_cleanup"),
                "bytes": run_size(directory), "kept": (directory / ".keep").is_file(),
                "started_at": (persisted or summary or {}).get("started_at", meta.get("started_at")),
                "finished_at": (persisted or summary or {}).get("finished_at", meta.get("finished_at"))}

    # Two selections per run, four runs total. Cache derived values only, never
    # expanded retained request evidence. File identity invalidates saved values.
    performance_cache = OrderedDict()

    def selected_performance(identifier, target=None, window=None):
        from .performance import build_performance

        directory = _directory(root, identifier)
        live = active if active.get("id") == identifier and active.get("state") in {"running", "stopping"} else None
        path = directory / "report.json"
        if live is None:
            if path.is_symlink() or not path.is_file():
                raise HTTPException(404, "Report is not available")
            stat = path.stat()
            revision = (stat.st_mtime_ns, stat.st_size)
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
    def saved_condition_requests(path: str, modified: int, size: int) -> ConditionRequestIndex:
        return ConditionRequestIndex.from_report(_report(Path(path)))

    def run_summary(detail: dict) -> dict:
        report = detail["report"] or detail["summary"] or {}
        counts = dict.fromkeys(("pass", "fail", "inconclusive", "skipped", "blocked"), 0)
        for check in report.get("checks", []):
            counts[check["status"]] += 1
        return {key: detail[key] for key in ("id", "state", "error", "progress", "baseline_id",
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
        meta = job["meta"]
        finished = False
        cancelled = None

        def progress(event: dict) -> None:
            if finished:
                return
            _track_live_progress(job, event, config.max_requests)

        try:
            report = await run_suite(config, progress=progress, stop_event=job["stop"])
            report.manifest["invocation"] = {"source": "web_api", "argv": None}
            report.manifest["open_files"] = job["open_files"]
            if baseline is not None:
                report = compare_baseline(report, baseline)
            # Persist the same condition cohort shown while live, including
            # work a stop/budget never reached. These are report observations,
            # not executor counters or invented request records.
            merged = _merged_condition_rows(list(job["conditions"].values()), report.conditions,
                terminal=True, reason=report.abort_reason)
            targets = report.observations.setdefault("targets", {})
            for target in config.targets:
                targets.setdefault(target.name, {})["conditions"] = [
                    {**row, "status": row["lifecycle_status"]}
                    for row in merged if row.get("target") == target.name]
            report.conditions = aggregate_conditions(report)
            meta["run_id"] = report.run_id
            sampler = PhaseSampler("export")
            try:
                with sampler, metadata_lock:
                    write_report(report, job["directory"], max_run_storage=max_run_storage)
            except OSError as error:
                if getattr(error, "json_complete", False):
                    meta.update(state="completed", finished_at=_now(), error=html_failure_warning(error))
                else:
                    message = save_failure_summary(report, job["directory"], error)
                    meta.update(state="error", finished_at=_now(), error=message)
            else:
                meta.update(state="completed", finished_at=_now())
            finally:
                meta["resources"] = {"sampled_peak_rss_bytes": dict(sampler.peaks),
                                     "memory_measurement": "unavailable" if sampler.peaks.get("export") is None else "sampled process RSS",
                                     "sampling": SAMPLING}
        except asyncio.CancelledError as error:
            cancelled = error
            meta.update(state="interrupted", finished_at=_now(),
                        error="The local server stopped before a report was saved.")
        except Exception as error:
            # The exception message may include endpoint content, headers, or credentials.
            meta.update(state="error", finished_at=_now(),
                        error=f"Run could not finish ({type(error).__name__}). Review the configuration and retry.")
        finally:
            finished = True
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
                        if warning not in (meta.get("error") or ""):
                            meta["error"] = ((meta.get("error") or "") + " " + warning).strip()
                            status_warning_added = True
                        unsaved_metadata[job["id"]] = meta
                    else:
                        meta.clear()
                        meta.update(payload)
                        status_warning_added = False
                        unsaved_metadata.pop(job["id"], None)

            try:
                # Publish the result before cleanup can block or be cancelled.
                persist_final()
                if policy and cancelled is None:
                    try:
                        task = asyncio.create_task(run_in_threadpool(
                            automatic_cleanup, root, policy, active_id=job["id"]))
                        result, cleanup_cancelled = await _finish_shielded(task)
                        cancelled = cleanup_cancelled or cancelled
                    except Exception as error:
                        result = {"deleted": [], "error": f"Automatic cleanup failed ({type(error).__name__})."}
                    meta.update(auto_cleanup=result, auto_cleanup_policy=policy)
                    last_auto_cleanup.clear()
                    last_auto_cleanup.update(result)
            finally:
                try:
                    persist_final()
                finally:
                    active.clear()
            if cancelled is not None:
                raise cancelled

    @app.get("/api/bootstrap")
    async def bootstrap():
        configuration = _bootstrap_config(defaults)
        resolved = ConfigDraft.model_validate(configuration)
        migrated_defaults, limit_notes = migrate_run_limits(defaults)
        migrated_defaults, load_notes = migrate_concurrent_load(migrated_defaults)
        _, combined_notes = migrate_load_test(migrated_defaults)
        return {**capabilities(resolved), "config": resolved.model_dump(mode="json"),
                "migration_notes": list(dict.fromkeys([*(migration_notes or []), *limit_notes, *load_notes, *combined_notes, *resolved._migration_notes])),
                "active_run_id": active.get("id"), "request_plan": preview_plan(resolved)}

    @app.get("/api/capabilities")
    async def describe_capabilities():
        return capabilities()

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
    async def validate_draft(config: ConfigDraftInput):
        """Normalize a draft for editing or export without requiring run readiness."""
        return {"config": config.model_dump(mode="json"), "migration_notes": config._migration_notes, "request_plan": preview_plan(config)}

    @app.post("/api/config/plan")
    @app.post("/api/config/validate")
    async def validate_config(config: RunConfigInput):
        """Normalize edits using the same contract as a run without sending traffic."""
        return {"config": config.model_dump(mode="json"), "migration_notes": config._migration_notes, "request_plan": preview_plan(config, validate=True)}

    @app.post("/api/config/preview")
    async def preview_config(config: ConfigDraftInput):
        """Describe effective requests and coverage without target calls or file imports."""
        preview = pack_preview(config)
        return {"config": config.model_dump(mode="json"), "migration_notes": config._migration_notes, "request_pack_preview": preview,
                "request_plan": preview_plan(config, pack_details=preview, validate=True)}

    @app.post("/api/datasets/preview")
    async def preview_dataset_import(body: DatasetPreview):
        from giraffe.dataset_import import preview_dataset

        return preview_dataset(**body.model_dump())

    @app.post("/api/runs/try-example", status_code=202)
    async def try_example(body: TryExample):
        from giraffe.dataset_import import try_example_config

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
        rows = []
        for directory in (root.iterdir() if root.exists() else ()):
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            try:
                rows.append(run_summary(run_detail(directory.name, listing=True)))
            except HTTPException:
                continue
        return {"runs": sorted(rows, key=lambda item: item.get("started_at") or "", reverse=True),
                "active_run_id": active.get("id")}

    def prepare_run(body: StartRun) -> tuple[RunConfig, RunReport | None]:
        """Resolve the same execution intent for Start and Copy CLI."""
        config = body.config
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
    async def copy_cli(body: StartRun):
        from giraffe.handoff import cli_command

        config, baseline = prepare_run(body)
        return {"command": cli_command(config, mode=body.mode, restart_target=body.restart_target,
                                       baseline=baseline)}

    @app.post("/api/runs", status_code=202)
    async def start_run(body: StartRun):
        async with start_lock:
            if active:
                raise HTTPException(409, "A run is already active; stop it or wait for its report")
            if connection_pending:
                raise HTTPException(409, "A connection test is running. Wait for its result "
                                    "before starting a run.")
            config, baseline = prepare_run(body)
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
            directory.mkdir(parents=True)
            meta = {"id": identifier, "state": "running", "started_at": _now(), "owner_pid": os.getpid(), "owner_host": socket.gethostname(),
                    "config": safe_config(config.model_dump(mode="json")), "baseline_id": body.baseline_id}
            save_metadata(directory, meta)
            waiting = planned_conditions(config)[:MAX_LIVE_CONDITIONS]
            condition_index = ConditionRequestIndex()
            for summary in waiting:
                condition_index.summary(summary)
            active.update(id=identifier, state="running", directory=directory, meta=meta,
                          open_files=guard,
                          progress={}, events=[], requests=OrderedDict(), live_preparations=OrderedDict(),
                          conditions={(row["target"], row["condition_id"]): row for row in waiting},
                          condition_requests=condition_index,
                          condition_plans={(row["target"], row["condition_id"]): row for row in waiting}, phases=[],
                          live_request_summary={"evicted": 0, "through_slot": None, "statuses": {}},
                          live_request_count=0, evicted_running=set(), stop=asyncio.Event())
            active["task"] = asyncio.create_task(execute(active, config, baseline))
            return {"id": identifier, "state": "running", "migration_notes": config._migration_notes,
                    "live_conditions": waiting}


    @app.get("/api/runs/{identifier}")
    async def get_run(identifier: str):
        return run_detail(identifier)

    @app.get("/api/runs/{identifier}/performance")
    async def performance_selection(identifier: str, target: str | None = None, window: str | None = None):
        return selected_performance(identifier, target, window)

    @app.get("/api/runs/{identifier}/requests/{request_id}")
    async def request_evidence(identifier: str, request_id: str):
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
            path = directory / "report.json"
            if path.is_symlink() or not path.is_file():
                raise HTTPException(404, "Report is not available")
            stat = path.stat()
            index = saved_condition_requests(str(path), stat.st_mtime_ns, stat.st_size)
            meta = metadata(directory)
            for summary in _merged_condition_rows(meta.get("conditions", []), list(index.summaries.values()),
                    terminal=True, reason=meta.get("error") or (meta.get("progress") or {}).get("reason")):
                index.summary({**summary, "status": summary["lifecycle_status"]})
        try:
            return index.page(condition_id, target=target, status=status, page=page, page_size=page_size)
        except KeyError:
            raise HTTPException(404, "Unknown condition") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/runs/{identifier}/config.json")
    async def download_config(identifier: str):
        saved = _report(_directory(root, identifier) / "report.json")
        config = saved.manifest.get("config")
        if not isinstance(config, dict):
            raise HTTPException(404, "Saved configuration is unavailable")
        return JSONResponse(config, headers={
            "Content-Disposition": f'attachment; filename="giraffe-{identifier}-config.json"',
        })

    @app.post("/api/runs/{identifier}/cancel")
    async def cancel_run(identifier: str):
        detail = run_detail(identifier)
        if active.get("id") == identifier:
            active["state"] = "stopping"
            active["meta"]["state"] = "stopping"
            active["stop"].set()
            try:
                save_metadata(active["directory"], active["meta"])
            except OSError:
                pass  # Stopping traffic must not depend on available disk space.
            return {"id": identifier, "state": "stopping"}
        return {"id": identifier, "state": detail["state"]}

    @app.get("/api/runs/{identifier}/report.{extension}")
    async def download_report(identifier: str, extension: str):
        if extension not in {"json", "html"}:
            raise HTTPException(404, "Unknown report format")
        directory = _directory(root, identifier)
        path = directory / "report.json"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Report is not available yet")
        return _ReportDownload(path, extension, identifier, download_lock, record_download_resources)

    @app.get("/api/runs/{identifier}/reproduction.json")
    async def export_reproduction(identifier: str):
        return reproduction_export(_report(_directory(root, identifier) / "report.json"))

    @app.get("/api/baselines")
    async def list_baselines():
        return {"baselines": saved_baselines()}

    @app.post("/api/baselines", status_code=201)
    async def save_baseline(body: SaveBaseline):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Choose a baseline name")
        detail = run_detail(body.run_id)
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

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    return app
