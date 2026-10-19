"""Small local HTTP wrapper around the same suite and saved reports as the CLI."""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from giraffe.capabilities import capabilities
from giraffe.client import LLMClient, _headers, completion_url
from giraffe.deployment import safe_config
from giraffe.fixtures import load_custom_fixtures
from giraffe.models import ConfigDraft, Model, RequestSpec, RunConfig, RunReport, Target
from giraffe.planning import request_plan
from giraffe.reporting import compare_baseline, reproduction_export, write_report
from giraffe.request_pack import pack_preview
from giraffe.runner import run_suite


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
_SHUTDOWN_SECONDS = 5


class APIKeyInput(Model):
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("api_key", mode="before")
    @classmethod
    def validate_api_key(cls, value):
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            raise ValueError("API key must be text")
        raw = raw.strip()
        if not raw:
            return None
        if len(raw) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in raw):
            raise ValueError("API key must contain printable ASCII characters without whitespace")
        return raw


def _attach_api_key(target: Target, key: SecretStr | None) -> None:
    if key is None:
        return
    if any(header.lower() == "authorization" for header in target.headers_env):
        raise HTTPException(422, "Use either a pasted API key or a custom Authorization header.")
    target._api_key = key
    # Export a usable reference, never silently turn an authenticated setup into an open one.
    target.api_key_env = target.api_key_env or "GIRAFFE_API_KEY"


class TargetAPIKey(APIKeyInput):
    target: str
    url: str


class SessionCredentials(Model):
    api_keys: list[TargetAPIKey] = Field(default_factory=list, exclude=True, repr=False,
                                       max_length=64)

    def attach_api_keys(self, config: RunConfig | ConfigDraft) -> None:
        seen = set()
        for credential in self.api_keys:
            target = next((t for t in config.targets
                           if t.name == credential.target and t.url == credential.url), None)
            if target is None or credential.target in seen:
                raise HTTPException(422, "Each session API key must match one configured target and URL.")
            seen.add(credential.target)
            _attach_api_key(target, credential.api_key)


class StartRun(SessionCredentials):
    config: RunConfig
    baseline_id: str | None = None
    mode: Literal["normal", "cold-start"] = "normal"
    restart_target: str | None = None


class DatasetPreview(Model):
    text: str
    format: Literal["csv", "json"]
    input_field: str | None = None
    expected_field: str | None = None
    scorer: Literal["judge", "exact"] = "judge"


class TryExample(SessionCredentials):
    config: ConfigDraft
    fixture_id: str
    target_name: str


class SaveBaseline(Model):
    run_id: str
    name: str = Field(min_length=1, max_length=120)
    replace: bool = False


class ModelDiscovery(APIKeyInput):
    url: str
    api_key_env: str | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)
    ca_bundle: str | None = None
    proxy: str | None = None

    def target(self, model: str = "discovery") -> Target:
        target = Target(name="connection", model=model, url=self.url.strip(),
                        api_key_env=self.api_key_env, headers_env=self.headers_env)
        _attach_api_key(target, self.api_key)
        return target

    def configuration(self, model: str, *, request_options: dict | None = None) -> RunConfig:
        return RunConfig(targets=[self.target(model)], checks=["access"], concurrency=1,
                         max_requests=1, max_duration_seconds=15, request_timeout_seconds=15,
                         max_output_tokens=32, samples=1, stream=False,
                         ca_bundle=self.ca_bundle, proxy=self.proxy,
                         request_options=request_options or {})

    @model_validator(mode="after")
    def validate_endpoint(self) -> ModelDiscovery:
        self.configuration("discovery")
        return self


class ConnectionTest(ModelDiscovery):
    model: str | None = None
    request_options: dict = Field(default_factory=dict)

    @field_validator("model", "api_key_env", mode="before")
    @classmethod
    def trim_optional(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_request_options(self) -> ConnectionTest:
        self.configuration(self.model or "connection", request_options=self.request_options)
        return self


async def discover_models(body: ModelDiscovery, *, allow_manual: bool = False) -> dict:
    """Read model IDs on explicit request; never send a completion or follow redirects."""
    target = body.target()
    try:
        headers = _headers(target)
    except ValueError:
        raise HTTPException(422, "An authentication environment variable is not set. "
                            "Set it before starting Giraffe, then check the API key section.") from None
    url = completion_url(target.url).removesuffix("/chat/completions") + "/models"
    try:
        verify = ssl.create_default_context(cafile=body.ca_bundle) if body.ca_bundle else True
        client = httpx.AsyncClient(
            timeout=6, follow_redirects=False, verify=verify, proxy=body.proxy, trust_env=True,
        )
    except OSError:
        raise HTTPException(422, "Could not load the TLS trust file. Check ca_bundle "
                            "on the machine running Giraffe.") from None
    except (ValueError, ImportError, httpx.InvalidURL):
        raise HTTPException(422, "Could not use the connection settings. "
                            "Check the endpoint and proxy configuration.") from None
    try:
        async with asyncio.timeout(8), client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in {401, 403}:
                    raise HTTPException(422, "Server denied access. Check your authentication: "
                                        "the API key or environment variable in the API key section.")
                if response.status_code != 200:
                    if allow_manual:
                        return {"models": [], "message": "The server does not provide a model list. "
                                "Enter its model name to test the connection."}
                    raise HTTPException(422, "This server did not return a model list. "
                                        "Check the endpoint or enter the model name manually.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 1_048_576:
                        if allow_manual:
                            return {"models": [], "message": "The model list is too large. "
                                    "Enter the model name to test the connection."}
                        raise HTTPException(422, "The model list is too large. Enter the model name manually.")
        payload = json.loads(content)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("Unexpected model list")
        models = list(dict.fromkeys(item["id"] for item in data if isinstance(item, dict)
                                   and isinstance(item.get("id"), str) and item["id"].strip()))
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
        raise HTTPException(422, "Could not reach the model list. Check that the server is running "
                            "and check the address, TLS trust and proxy settings.") from None
    except (ValueError, UnicodeError):
        if allow_manual:
            return {"models": [], "message": "The server's model list could not be read. "
                    "Enter the model name to test the connection."}
        raise HTTPException(422, "The server returned an unreadable model list. "
                            "You can still enter the model name manually.") from None
    return {"models": models, "message": "Model list received. No inference requests were sent."
            if models else "The server returned no models. Enter an installed model name manually."}


async def check_connection(body: ConnectionTest) -> dict:
    """Resolve a model, then send at most one bounded inference without saving a run."""
    model = body.model
    try:
        async with asyncio.timeout(15):
            if not model:
                discovered = await discover_models(body, allow_manual=True)
                models = discovered["models"]
                if len(models) != 1:
                    return {"status": "needs_model", "models": models,
                            "message": "Choose a model, then test its connection."
                            if models else discovered["message"]}
                model = models[0]
            config = body.configuration(model, request_options=body.request_options)
            target = config.targets[0]
            try:
                _headers(target)
            except ValueError:
                raise HTTPException(422, "An authentication environment variable is not set. "
                                    "Set it before starting Giraffe.") from None
            async with LLMClient(target, config) as client:
                record = await client.execute(RequestSpec(
                    fixture_id="connection", scenario="connection", check_ids=["access"],
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    max_tokens=32, stream=False,
                ))
    except TimeoutError:
        raise HTTPException(422, "Connection test timed out after 15 seconds. "
                            "Check that the model is loaded, then try again.") from None
    except OSError:
        raise HTTPException(422, "Could not load the TLS trust file. "
                            "Check ca_bundle on the machine running Giraffe.") from None
    except (ValueError, ImportError, httpx.HTTPError, httpx.InvalidURL):
        raise HTTPException(422, "Could not connect. Check the endpoint, authentication, "
                            "TLS trust and proxy settings.") from None
    if record.status in {"timeout", "cancelled"}:
        raise HTTPException(422, "Connection test timed out after 15 seconds. "
                            "Check that the model is loaded, then try again.")
    if record.http_status in {401, 403}:
        raise HTTPException(422, "Server denied access. Check your authentication: the API key, "
                            "environment variable and header settings.")
    if not record.valid:
        if record.http_status is None:
            message = "Could not reach the model. Check the endpoint, TLS trust and proxy settings."
        elif not 200 <= record.http_status < 300:
            message = (f"The server returned HTTP {record.http_status}. "
                       "Check the model name and OpenAI-compatible endpoint.")
        else:
            message = ("The server responded but did not return a usable answer within the "
                       "32-token test. Check the model's settings or continue with its name.")
        raise HTTPException(422, message)
    return {"status": "connected", "model": model, "latency_ms": round(record.elapsed_ms, 1),
            "message": "The model returned an answer. Your endpoint is ready for checks."}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
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


def _report(path: Path) -> RunReport:
    if path.is_symlink():
        raise HTTPException(404, "Report unavailable")
    try:
        return RunReport.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "Report unavailable or unreadable") from exc


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
        config.update(defaults)
    config["restart_target"] = None
    return config


def create_app(runs_dir: Path = Path("runs"), defaults: dict | None = None) -> FastAPI:
    root = Path(runs_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    baseline_root = root / ".baselines"
    if baseline_root.is_symlink():
        raise ValueError("Baseline directory cannot be a symlink")
    baseline_root.mkdir(exist_ok=True)
    active: dict = {}
    connection_pending = False
    unsaved_metadata: dict[str, dict] = {}

    def metadata(directory: Path) -> dict:
        if directory.name in unsaved_metadata:
            return unsaved_metadata[directory.name]
        try:
            return _read(directory / ".web.json")
        except (OSError, ValueError):
            return {}

    def recover_interrupted() -> None:
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            meta = metadata(directory)
            if meta.get("state") in {"running", "stopping"}:
                # A report can finish just before a process exits while metadata is being saved.
                meta["state"] = "completed" if (directory / "report.json").is_file() else "interrupted"
                meta["finished_at"] = _now()
                if meta["state"] == "interrupted":
                    meta["error"] = "The local server stopped before a report was saved."
                _write(directory / ".web.json", meta)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recover_interrupted()
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
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Input values can contain secrets pasted into the wrong field.
        return JSONResponse({"detail": [{"loc": item["loc"], "msg": item["msg"]}
                                         for item in error.errors()]}, status_code=422)

    def run_detail(identifier: str) -> dict:
        directory = _directory(root, identifier)
        meta = metadata(directory)
        report_path = directory / "report.json"
        if not meta and not report_path.is_file():
            raise HTTPException(404, "Unknown run")
        persisted = None
        if report_path.is_file():
            try:
                persisted = _report(report_path).model_dump(mode="json")
            except HTTPException:
                meta = {**meta, "state": "error", "error": "Saved report is unreadable."}
        live = active if active.get("id") == identifier else {}
        return {"id": identifier, "state": live.get("state", meta.get("state", "completed")),
                "error": meta.get("error"), "progress": live.get("progress", meta.get("progress", {})),
                "events": live.get("events", meta.get("events", [])),
                "live_requests": list(live.get("requests", {}).values()),
                "phases": live.get("phases", meta.get("phases", [])),
                "config": meta.get("config") or (persisted or {}).get("manifest", {}).get("config", {}),
                "baseline_id": meta.get("baseline_id"), "report": persisted,
                "started_at": (persisted or {}).get("started_at", meta.get("started_at")),
                "finished_at": (persisted or {}).get("finished_at", meta.get("finished_at"))}

    def run_summary(detail: dict) -> dict:
        report = detail["report"] or {}
        counts = dict.fromkeys(("pass", "fail", "inconclusive", "skipped", "blocked"), 0)
        for check in report.get("checks", []):
            counts[check["status"]] += 1
        return {key: detail[key] for key in ("id", "state", "error", "progress", "baseline_id",
                                             "started_at", "finished_at")} | {
            "run_id": report.get("run_id"), "overall": report.get("overall"),
            "targets": _targets(detail["config"]), "check_counts": counts,
            "request_count": len(report.get("requests", [])) if report else
            detail["progress"].get("completed", 0)}

    def saved_baselines() -> list[dict]:
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

        def progress(event: dict) -> None:
            job["progress"] = event
            job["events"].append(event)
            del job["events"][:-100]
            slot = event.get("request_slot")
            if isinstance(slot, int) and 0 <= slot < config.max_requests:
                if event["event"] == "request_started":
                    job["requests"][slot] = {"slot": slot, "status": "running",
                        "target": event.get("target"), "scenario": event.get("scenario"),
                        "fixture_id": event.get("fixture_id")}
                elif event["event"] == "request_completed" and event.get("request"):
                    job["requests"][slot] = {**event["request"], "slot": slot,
                        "intentional": event.get("intentional", False)}
            if event["event"] in {"scenario_started", "scenario_finished"}:
                job["phases"].append({key: event.get(key) for key in
                    ("event", "target", "scenario", "elapsed_seconds")})
                del job["phases"][:-1024]

        try:
            report = await run_suite(config, progress=progress, stop_event=job["stop"])
            if baseline is not None:
                report = compare_baseline(report, baseline)
            write_report(report, job["directory"])
            meta.update(state="completed", finished_at=_now())
        except asyncio.CancelledError:
            meta.update(state="interrupted", finished_at=_now(),
                        error="The local server stopped before a report was saved.")
        except Exception as error:
            # The exception message may include endpoint content, headers, or credentials.
            meta.update(state="error", finished_at=_now(),
                        error=f"Run could not finish ({type(error).__name__}). Review the configuration and retry.")
        finally:
            meta.update(progress=job["progress"], events=job["events"], phases=job["phases"])
            try:
                _write(job["directory"] / ".web.json", meta)
            except OSError:
                # Keep the final state visible even when the disk cannot accept metadata.
                meta["error"] = (meta.get("error", "") +
                                 " Run status could not be saved to disk.").strip()
                unsaved_metadata[job["id"]] = meta
            finally:
                active.clear()

    @app.get("/api/bootstrap")
    async def bootstrap():
        configuration = _bootstrap_config(defaults)
        resolved = ConfigDraft.model_validate(configuration)
        return {**capabilities(resolved), "config": resolved.model_dump(mode="json"),
                "active_run_id": active.get("id"), "request_plan": request_plan(resolved)}

    @app.get("/api/capabilities")
    async def describe_capabilities():
        return capabilities()

    @app.post("/api/targets/models")
    async def target_models(body: ModelDiscovery):
        return await discover_models(body)

    @app.post("/api/targets/test-connection")
    async def target_connection(body: ConnectionTest):
        nonlocal connection_pending
        if active:
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
    async def validate_draft(config: ConfigDraft):
        """Normalize a draft for editing or export without requiring run readiness."""
        return {"config": config.model_dump(mode="json"), "request_plan": request_plan(config)}

    @app.post("/api/config/validate")
    async def validate_config(config: RunConfig):
        """Normalize edits using the same contract as a run without sending traffic."""
        return {"config": config.model_dump(mode="json"), "request_plan": request_plan(config)}

    @app.post("/api/config/preview")
    async def preview_config(config: ConfigDraft):
        """Describe effective requests and coverage without target calls or file imports."""
        preview = pack_preview(config)
        return {"config": config.model_dump(mode="json"), "request_pack_preview": preview,
                "request_plan": request_plan(config, pack_details=preview)}

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

    @app.get("/api/runs")
    async def list_runs():
        rows = []
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            try:
                rows.append(run_summary(run_detail(directory.name)))
            except HTTPException:
                continue
        return {"runs": sorted(rows, key=lambda item: item.get("started_at") or "", reverse=True),
                "active_run_id": active.get("id")}

    def prepare_run(body: StartRun) -> tuple[RunConfig, RunReport | None]:
        """Resolve the same execution intent for Start and Copy CLI."""
        config = body.config
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
        if active:
            raise HTTPException(409, "A run is already active; stop it or wait for its report")
        if connection_pending:
            raise HTTPException(409, "A connection test is running. Wait for its result "
                                "before starting a run.")
        config, baseline = prepare_run(body)
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = _directory(root, identifier)
        directory.mkdir()
        meta = {"id": identifier, "state": "running", "started_at": _now(),
                "config": safe_config(config.model_dump(mode="json")), "baseline_id": body.baseline_id}
        _write(directory / ".web.json", meta)
        active.update(id=identifier, state="running", directory=directory, meta=meta,
                      progress={}, events=[], requests={}, phases=[], stop=asyncio.Event())
        active["task"] = asyncio.create_task(execute(active, config, baseline))
        return {"id": identifier, "state": "running"}

    @app.get("/api/runs/{identifier}")
    async def get_run(identifier: str):
        return run_detail(identifier)

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
                _write(active["directory"] / ".web.json", active["meta"])
            except OSError:
                pass  # Stopping traffic must not depend on available disk space.
            return {"id": identifier, "state": "stopping"}
        return {"id": identifier, "state": detail["state"]}

    @app.get("/api/runs/{identifier}/report.{extension}")
    async def download_report(identifier: str, extension: str):
        if extension not in {"json", "html"}:
            raise HTTPException(404, "Unknown report format")
        directory = _directory(root, identifier)
        path = directory / f"report.{extension}"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Report is not available yet")
        return FileResponse(path, filename=f"giraffe-{identifier}.{extension}",
                            media_type="application/json" if extension == "json" else "text/html")

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
        directory.mkdir(exist_ok=True)
        summary = {"id": identifier, "name": name, "run_id": body.run_id,
                   "saved_at": _now(), "overall": detail["report"]["overall"],
                   "targets": _targets(detail["config"])}
        _write(directory / "report.json", detail["report"])
        _write(directory / "baseline.json", summary)
        return summary

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    return app
