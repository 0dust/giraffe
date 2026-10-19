"""A local CLI: configure an endpoint, run the suite, keep a report/baseline."""

from __future__ import annotations

import argparse
import asyncio
import errno
import socket
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from giraffe import __version__
from giraffe.models import CHECK_NAMES, ConfigDraft, RunConfig, RunReport


class UsageError(SystemExit):
    """An argument error that can use the caller's requested output format."""

    def __init__(self, message: str):
        super().__init__(4)
        self.message = message

    def __str__(self) -> str:
        return self.message


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def parser() -> argparse.ArgumentParser:
    app = ArgumentParser(
        prog="giraffe", description="Run a bounded, built-in health suite against your LLM endpoint."
    )
    app.add_argument("--version", action="version", version=__version__)
    commands = app.add_subparsers(dest="command", required=True)
    commands.add_parser("describe", help="Print checks, defaults and the full configuration schema as JSON")
    traces = commands.add_parser("sglang-traces", help="Collect local SGLang terminal traces (optional tracing extra)")
    traces.add_argument("--output", required=True, type=Path)
    traces.add_argument("--port", type=int, default=4318)
    validate = commands.add_parser("validate", help="Validate and normalize a configuration without traffic")
    validate.add_argument("--config", required=True, type=Path, help="YAML/JSON configuration; - reads stdin")
    validate.add_argument("--output-format", choices=["text", "json"], default="text")
    validate.add_argument("--show-config", action="store_true", help="Print the normalized configuration in text mode")
    preview = commands.add_parser("preview", help="Preview a draft configuration as JSON without traffic")
    preview.add_argument("--config", required=True, type=Path, help="YAML/JSON configuration; - reads stdin")
    importer = commands.add_parser("import-dataset", help="Preview CSV or JSON dataset import as JSON without traffic")
    importer.add_argument("--file", required=True, type=Path)
    importer.add_argument("--input-field")
    importer.add_argument("--expected-field")
    importer.add_argument("--scorer", choices=["judge", "exact"], default="judge")
    trial = commands.add_parser("try-example", help="Run one dataset example against one target")
    trial.add_argument("--config", required=True, type=Path)
    trial.add_argument("--case", required=True, dest="fixture_id")
    trial.add_argument("--target", required=True, dest="target_name")
    trial.add_argument("--output", type=Path)
    trial.add_argument("--output-format", choices=["text", "json"], default="text")
    web = commands.add_parser("ui", help="Open the local web interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--runs-dir", type=Path, default=Path("runs"))
    web.add_argument("--config", type=Path, help="Prefill the run form from a YAML/JSON config")
    for name in ("run", "cold-start"):
        run = commands.add_parser(name, help="Run the suite" if name == "run" else
                                  "Explicitly restart one configured target and test readiness",
                                  formatter_class=argparse.RawDescriptionHelpFormatter,
                                  epilog="""Examples:
  giraffe run --url http://127.0.0.1:11434/v1 --model llama3.2:3b
  giraffe run --config giraffe.yaml
  giraffe run --config giraffe.yaml --baseline runs/previous

Exit codes:
  0  pass
  1  fail
  2  inconclusive
  3  blocked (including preflight failure)
  4  usage or configuration error""")
        source = run.add_mutually_exclusive_group()
        source.add_argument("--config", type=Path, help="YAML or JSON run configuration; - reads stdin")
        source.add_argument("--manifest", type=Path, help="Rerun the configuration in a saved report")
        run.add_argument("--url", help="OpenAI-compatible origin, API base, or chat-completions URL")
        run.add_argument("--model", help="Model name served by the endpoint")
        run.add_argument("--api-key-env", help="Name of an environment variable containing the key")
        run.add_argument("--concurrency", type=int, help="Maximum requests in flight")
        run.add_argument("--max-requests", type=int, help="Maximum suite request budget across all targets")
        run.add_argument("--max-duration", type=float, dest="max_duration_seconds", help="Maximum suite duration in seconds")
        run.add_argument("--request-timeout", type=float, dest="request_timeout_seconds", help="Per-request timeout in seconds")
        run.add_argument("--max-output-tokens", type=int, help="Maximum output tokens per request")
        run.add_argument("--context-limit", type=int, help="Model context token limit")
        run.add_argument("--samples", type=int, help="Samples per selected check")
        run.add_argument("--sustained-seconds", type=float, help="Duration of sustained recovery load in seconds")
        run.add_argument("--stop-after-errors", type=int, help="Stop after this many request errors")
        run.add_argument("--checks", metavar="CHECKS", help="Comma-separated names: " + ", ".join(CHECK_NAMES))
        run.add_argument("--structured-output", action="store_true", default=None, dest="structured_json",
                         help="Include structured JSON output checks")
        run.add_argument("--json", action="store_true", default=None, dest="structured_json",
                         help=argparse.SUPPRESS)
        run.add_argument("--skip-preflight", action="store_true", help="Skip the bounded connection/model probe")
        run.add_argument("--deployment-file", type=Path, help="Bounded deployment JSON for the first target")
        run.add_argument("--metrics-url", help="Separate statistics endpoint for the first target")
        run.add_argument("--metrics-api-key-env", help="Separate metrics secret reference")
        run.add_argument("--metrics-profile", choices=["unknown", "ollama", "vllm-v1", "vllm-legacy", "sglang", "llamacpp", "tensorrt-llm", "lmdeploy", "tgi"], help="Metrics format used by the statistics endpoint")
        run.add_argument("--tool-calling", action="store_true", default=None, help="Include tool-calling capability checks")
        run.add_argument("--metrics", action="store_true", default=None, help="Include GPU metrics observations")
        run.add_argument("--nonstream", action="store_false", default=None, dest="stream", help="Disable streaming responses")
        run.add_argument("--overlap-models", action="store_true", default=None, help="Run targets concurrently within the shared request limit")
        run.add_argument("--fixtures", dest="custom_fixtures", help="Optional custom fixture JSON")
        run.add_argument("--request-pack", dest="request_pack_file", metavar="PATH",
                         help="Import local request-pack JSON into the portable configuration")
        run.add_argument("--retention", choices=["all", "failures", "none"], help="Retain all responses, failures only, or no response content")
        run.add_argument("--ca-bundle", help="TLS CA certificate bundle path")
        run.add_argument("--proxy", help="HTTP proxy URL")
        for option in ("first-output-ms", "latency-ms", "stream-gap-ms",
                       "min-output-tokens-per-second", "max-error-rate", "min-correctness",
                       "fairness-max-ratio", "regression-percent"):
            run.add_argument("--" + option, type=float, help="Threshold for " + option.replace("-", " "))
        run.add_argument("--baseline", type=Path, help="Compare to this explicitly saved report")
        run.add_argument("--output", type=Path, help="New report directory (never overwrites a run)")
        run.add_argument("--output-format", choices=["text", "json"], default="text", help="Result format on stdout; progress is written to stderr")
        if name == "cold-start":
            run.add_argument("--restart", required=True, help="Name of target with restart_command")
    export = commands.add_parser("export", help="Print a sanitized reproduction snapshot as JSON")
    export.add_argument("report", type=Path)
    inspect = commands.add_parser("inspect", help="Print recorded deployment/telemetry and comparison JSON")
    inspect.add_argument("report", type=Path)
    baseline = commands.add_parser("baseline", help="Explicitly save a run for future comparisons")
    actions = baseline.add_subparsers(dest="action", required=True)
    save = actions.add_parser("save", help="Save a chosen report; never changes it during runs")
    save.add_argument("report", type=Path)
    save.add_argument("destination", type=Path)
    save.add_argument("--replace", action="store_true", help="Explicitly replace an existing baseline")
    save.add_argument("--output-format", choices=["text", "json"], default="text")
    return app


def load_report(path: Path) -> RunReport:
    return RunReport.model_validate_json((path / "report.json" if path.is_dir() else path).read_text())


def load_configuration(path: Path) -> dict:
    if path != Path("-") and not path.exists():
        raise ValueError(f"config file not found: {path}")
    values = yaml.safe_load(sys.stdin.read() if path == Path("-") else path.read_text())
    if not isinstance(values, dict):
        raise ValueError("Configuration must be a YAML/JSON object")
    return values


def configuration(args: argparse.Namespace) -> RunConfig:
    values = {}
    if args.config:
        values = load_configuration(args.config)
    elif args.manifest:
        values = dict(load_report(args.manifest).manifest["config"])
    if values.get("restart_target"):
        if args.command != "cold-start":
            raise ValueError("Restart requires the explicit cold-start --restart TARGET command")
        values.pop("restart_target", None)
    if args.url or args.model or args.api_key_env:
        if "targets" in values:
            raise ValueError("Use targets in the config, or --url/--model; do not combine them")
        if not args.url or not args.model:
            raise ValueError("Provide both --url and --model, or use --config")
        values["targets"] = [{"name": "local", "url": args.url, "model": args.model,
                              "api_key_env": args.api_key_env}]
    if "targets" not in values:
        raise ValueError("Provide --url and --model, or --config with at least one target")
    for name in RunConfig.model_fields:
        value = getattr(args, name, None)
        if value is not None and name not in {"checks", "limits"}:
            values[name] = value
    if args.checks is not None:
        values["checks"] = [part.strip() for part in args.checks.split(",") if part.strip()]
    elif "checks" in values:
        # Explicit CLI opt-ins can extend a saved selection. An explicit --checks
        # list remains authoritative, including when it deselects optional tests.
        values["checks"] = list(values["checks"])
        for flag, check in (("structured_json", "json"), ("metrics", "gpu"), ("tool_calling", "tools")):
            if getattr(args, flag, None) and check not in values["checks"]:
                values["checks"].append(check)
    limits = dict(values.get("limits") or {})
    for name in RunConfig.model_fields["limits"].annotation.model_fields:
        value = getattr(args, name, None)
        if value is not None:
            limits[name] = value
    values["limits"] = limits
    if args.request_pack_file is not None:
        if values.get("request_pack") is not None:
            raise ValueError("Do not combine an inline request_pack with --request-pack")
        if values.get("custom_fixtures"):
            raise ValueError("request_pack cannot be combined with custom_fixtures")
        if "://" in args.request_pack_file:
            raise ValueError("--request-pack requires a local JSON file path")
        from giraffe.request_pack import load_request_pack

        values["request_pack"] = load_request_pack(args.request_pack_file).model_dump(mode="json")
        values["request_source"] = "request_pack"
    if args.command == "cold-start":
        values["restart_target"] = args.restart
    if getattr(args, "deployment_file", None):
        from giraffe.deployment import load_metadata
        values["targets"][0].setdefault("deployment", {}).update(load_metadata(args.deployment_file))
    for key in ("metrics_url", "metrics_api_key_env", "metrics_profile"):
        if getattr(args, key, None) is not None:
            values["targets"][0][key] = getattr(args, key)
    return RunConfig.model_validate(values)


async def _run(config: RunConfig, *, json_progress: bool = False) -> RunReport:
    from giraffe.runner import run_suite

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    last = (None, None)
    last_update = 0.0

    def progress(event: dict) -> None:
        nonlocal last, last_update
        if json_progress:
            print(json.dumps(event, allow_nan=False), file=sys.stderr, flush=True)
            return
        current = (event.get("target"), event.get("scenario"))
        if current[1] and (current != last or time.monotonic() - last_update >= 5):
            print(f"  {current[0]}: {current[1]} · "
                  f"{event.get('completed', 0)}/{config.max_requests} request budget · "
                  f"{event.get('elapsed_seconds', 0):g}s", file=sys.stderr, flush=True)
            last = current
            last_update = time.monotonic()

    try:
        return await run_suite(config, progress=progress, stop_event=stop)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = (bool(arguments) and arguments[0] in {"preview", "import-dataset"}) or any(value == "--output-format=json" or (
        value == "--output-format" and index + 1 < len(arguments) and arguments[index + 1] == "json"
    ) for index, value in enumerate(arguments))
    kind = arguments[0] if arguments else "usage"

    def emit(outcome: str, exit_code: int, **details) -> None:
        print(json.dumps({"kind": kind, "outcome": outcome, "exit_code": exit_code, **details},
                         allow_nan=False))

    def failure(errors: list[dict]) -> int:
        if json_output:
            emit("error", 4, errors=errors)
        else:
            for error in errors:
                location = ".".join(map(str, error["loc"]))
                print(f"giraffe: {location + ': ' if location else ''}{error['message']}", file=sys.stderr)
        return 4

    try:
        args = parser().parse_args(arguments)
        kind = "baseline.save" if args.command == "baseline" else args.command
        if args.command == "sglang-traces":
            from giraffe.sglang_traces import serve
            serve(args.output, args.port)
            return 0
        if args.command == "describe":
            from giraffe.capabilities import capabilities

            print(json.dumps(capabilities(), allow_nan=False))
            return 0
        if args.command == "validate":
            config = RunConfig.model_validate(load_configuration(args.config))
            from giraffe.planning import request_plan

            warnings = request_plan(config)["warnings"]
            if json_output:
                emit("success", 0, config=config.model_dump(mode="json"), warnings=warnings)
            else:
                print("Configuration is valid. No traffic was sent.")
                print("Targets: " + ", ".join(target.name for target in config.targets))
                print("Checks: " + ", ".join(config.checks))
                print(f"max_requests: {config.max_requests}")
                for warning in warnings:
                    print(f"Warning: {warning}")
                if args.show_config:
                    print(config.model_dump_json(indent=2))
            return 0
        if args.command == "preview":
            from giraffe.request_pack import pack_preview

            config = ConfigDraft.model_validate(load_configuration(args.config))
            print(json.dumps({"config": config.model_dump(mode="json"),
                              "request_pack_preview": pack_preview(config)}, allow_nan=False))
            return 0
        if args.command == "import-dataset":
            from giraffe.dataset_import import preview_dataset, read_dataset

            result = preview_dataset(read_dataset(args.file), args.file.suffix.lower().lstrip("."),
                                     input_field=args.input_field, expected_field=args.expected_field,
                                     scorer=args.scorer)
            print(json.dumps(result, allow_nan=False))
            return 4 if result["errors"] else 0
        if args.command == "ui":
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            defaults = None
            if args.config:
                defaults = RunConfig.model_validate(load_configuration(args.config)).model_dump(
                    mode="json"
                )
                defaults["restart_target"] = None
            import uvicorn
            from giraffe.web import create_app

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    listener.bind(("127.0.0.1", args.port))
                    listener.listen(128)
                except OSError as error:
                    if error.errno == errno.EADDRINUSE:
                        raise ValueError(f"port {args.port} is in use; choose another with --port") from None
                    raise
                app = create_app(runs_dir=args.runs_dir, defaults=defaults)
                print(f"Giraffe UI: http://127.0.0.1:{args.port} — open in your browser\n"
                      f"Local runs: {args.runs_dir.resolve()}\n"
                      "Review the plan, then choose Launch.", flush=True)
                server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                                       log_level="warning"))
                server.run(sockets=[listener])
            return 0
        if args.command in {"export", "inspect"}:
            from giraffe.reporting import reproduction_export, _retained
            report = _retained(load_report(args.report))
            value = reproduction_export(report) if args.command == "export" else {
                "run_id": report.run_id, "observations": report.observations, "baseline": report.baseline,
                "workload_selection": report.manifest.get("workload_selection", {})}
            print(json.dumps(value, indent=2, allow_nan=False))
            return 0
        if args.command == "baseline":
            from giraffe.reporting import _retained
            report = _retained(load_report(args.report))
            args.destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with args.destination.open("w" if args.replace else "x") as file:
                    file.write(report.model_dump_json(indent=2) + "\n")
            except FileExistsError:
                raise ValueError(f"Baseline already exists: {args.destination}; use --replace to overwrite it") from None
            if json_output:
                emit("success", 0, run_id=report.run_id, destination=str(args.destination.resolve()))
            else:
                print(f"Saved baseline {report.run_id} ({report.overall}): {args.destination.resolve()}")
            return 0
        if args.command == "try-example":
            from giraffe.dataset_import import try_example_config

            config = try_example_config(ConfigDraft.model_validate(load_configuration(args.config)),
                                        args.fixture_id, args.target_name)
            baseline = None
        else:
            config = configuration(args)
            baseline = load_report(args.baseline) if args.baseline else None
        directory = args.output or Path("runs") / datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        if directory.exists() and any(directory.iterdir()):
            raise ValueError(f"Output directory is not empty: {directory}; choose a new directory")
        from giraffe.planning import request_plan
        from giraffe.preflight import PreflightError, check_target

        warnings = request_plan(config)["warnings"]
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        if args.command == "run" and not args.skip_preflight:
            async def preflight():
                for target in config.targets:
                    try:
                        await check_target(target, config)
                    except PreflightError as error:
                        if error.blocking:
                            return target.name, str(error) + " Use --skip-preflight to run anyway."
                        warning = f"preflight for {target.name}: {error}; continuing."
                        warnings.append(warning)
                        print(f"Warning: {warning}", file=sys.stderr)
                return None

            blocked = asyncio.run(preflight())
            if blocked:
                target, message = blocked
                print(f"giraffe: preflight failed for {target}: {message}", file=sys.stderr)
                if json_output:
                    emit("blocked", 3, target=target, message=message, warnings=warnings)
                return 3
        directory.mkdir(parents=True, exist_ok=True)
        print(f"Giraffe: {len(config.targets)} target(s), at most {config.max_requests} requests, "
              f"{config.concurrency} in flight, {config.max_duration_seconds:g}s total, "
              f"{config.request_timeout_seconds:g}s/request, {config.max_output_tokens} output "
              "tokens/request. Ctrl-C saves a partial report.", file=sys.stderr, flush=True)
        from giraffe.reporting import _retained, compare_baseline, write_report

        report = asyncio.run(_run(config, json_progress=True) if json_output else _run(config))
        if baseline:
            report = compare_baseline(report, baseline)
        json_path, html_path = write_report(report, directory)
        report = _retained(report)
        exit_code = {"pass": 0, "fail": 1, "inconclusive": 2, "blocked": 3}.get(report.overall, 2)
        summary = report.observations.get("summary")
        if json_output:
            emit(report.overall, exit_code, warnings=warnings, run_id=report.run_id, request_count=len(report.requests),
                 abort_reason=report.abort_reason,
                 **({"summary": summary} if summary else {}),
                 report_paths={"json": str(json_path.resolve()), "html": str(html_path.resolve())})
        else:
            print(f"{report.overall.upper()}: {len(report.requests)} requests")
            if summary:
                print(summary)
            if report.abort_reason:
                print(f"Stopped: {report.abort_reason}")
            for check in report.checks:
                print(f"  {check.target} / {check.id}: {check.status} — {check.summary}")
            for name, observation in report.observations.get("targets", {}).items():
                telemetry = observation.get("serving_telemetry", {})
                states = sorted({s.get("status", "unknown") for s in telemetry.get("snapshots", [])})
                print(f"  {name} / serving telemetry: {', '.join(states)}; source freshness and other traffic remain qualified in JSON.")
            if report.baseline:
                for target in report.baseline["targets"]:
                    diff = target["configuration_diff"]
                    changes = [row["field"] for row in diff["fields"] if row["change"]!="no_observed_change"]
                    print(f"  {target['target']} / deployment changes: {', '.join(changes) or 'none observed; unknowns remain'}; confounded={diff['confounded']}; causality unverified")
            print(f"JSON: {json_path.resolve()}\nHTML: {html_path.resolve()}")
        return exit_code
    except UsageError as error:
        return failure([{"loc": [], "message": str(error), "type": "UsageError"}])
    except ValidationError as error:
        # Pydantic's full repr contains raw input; only print locations/messages.
        return failure([{"loc": list(item["loc"]), "message": ("Target name is required; add e.g. name: local"
                          if item["type"] == "missing" and item["loc"][-1:] == ("name",)
                          and "targets" in item["loc"] else item["msg"]), "type": item["type"]}
                        for item in error.errors(include_input=False, include_url=False, include_context=False)])
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = ["config", mark.line + 1, mark.column + 1] if mark else []
        return failure([{"loc": location, "message": "Invalid YAML/JSON configuration", "type": "parse_error"}])
    except (OSError, ValueError, KeyError) as error:
        return failure([{"loc": [], "message": str(error), "type": type(error).__name__}])


if __name__ == "__main__":
    sys.exit(main())
