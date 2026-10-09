"""A local CLI: configure an endpoint, run the suite, keep a report/baseline."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from giraffe import __version__
from giraffe.models import CHECK_NAMES, RunConfig, RunReport


class UsageError(ValueError):
    """An argument error that can use the caller's requested output format."""


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
    validate = commands.add_parser("validate", help="Validate and normalize a configuration without traffic")
    validate.add_argument("--config", required=True, type=Path, help="YAML/JSON configuration; - reads stdin")
    validate.add_argument("--output-format", choices=["text", "json"], default="text")
    web = commands.add_parser("ui", help="Open the local web interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--runs-dir", type=Path, default=Path("runs"))
    web.add_argument("--config", type=Path, help="Prefill the run form from a YAML/JSON config")
    for name in ("run", "cold-start"):
        run = commands.add_parser(name, help="Run the suite" if name == "run" else
                                  "Explicitly restart one configured target and test readiness")
        source = run.add_mutually_exclusive_group()
        source.add_argument("--config", type=Path, help="YAML or JSON run configuration; - reads stdin")
        source.add_argument("--manifest", type=Path, help="Rerun the configuration in a saved report")
        run.add_argument("--url", help="OpenAI-compatible origin, API base, or chat-completions URL")
        run.add_argument("--model")
        run.add_argument("--api-key-env", help="Name of an environment variable containing the key")
        run.add_argument("--concurrency", type=int)
        run.add_argument("--max-requests", type=int)
        run.add_argument("--max-duration", type=float, dest="max_duration_seconds")
        run.add_argument("--request-timeout", type=float, dest="request_timeout_seconds")
        run.add_argument("--max-output-tokens", type=int)
        run.add_argument("--context-limit", type=int)
        run.add_argument("--samples", type=int)
        run.add_argument("--sustained-seconds", type=float)
        run.add_argument("--stop-after-errors", type=int)
        run.add_argument("--checks", help="Comma-separated: " + ",".join(CHECK_NAMES))
        run.add_argument("--json", action="store_true", default=None, dest="structured_json")
        run.add_argument("--metrics", action="store_true", default=None)
        run.add_argument("--nonstream", action="store_false", default=None, dest="stream")
        run.add_argument("--overlap-models", action="store_true", default=None)
        run.add_argument("--fixtures", dest="custom_fixtures", help="Optional custom fixture JSON")
        run.add_argument("--retention", choices=["all", "failures", "none"])
        run.add_argument("--ca-bundle")
        run.add_argument("--proxy")
        for option in ("first-output-ms", "latency-ms", "stream-gap-ms",
                       "min-output-tokens-per-second", "max-error-rate", "min-correctness",
                       "fairness-max-ratio", "regression-percent"):
            run.add_argument("--" + option, type=float)
        run.add_argument("--baseline", type=Path, help="Compare to this explicitly saved report")
        run.add_argument("--output", type=Path, help="New report directory (never overwrites a run)")
        run.add_argument("--output-format", choices=["text", "json"], default="text")
        if name == "cold-start":
            run.add_argument("--restart", required=True, help="Name of target with restart_command")
    baseline = commands.add_parser("baseline", help="Explicitly save a run for future comparisons")
    actions = baseline.add_subparsers(dest="action", required=True)
    save = actions.add_parser("save", help="Save a chosen report; never changes it during runs")
    save.add_argument("report", type=Path)
    save.add_argument("destination", type=Path)
    save.add_argument("--replace", action="store_true", help="Explicitly replace an existing baseline")
    save.add_argument("--output-format", choices=["text", "json"], default="text")
    return app


def load_report(path: Path) -> RunReport:
    return RunReport.model_validate_json(path.read_text())


def load_configuration(path: Path) -> dict:
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
        for flag, check in (("structured_json", "json"), ("metrics", "gpu")):
            if getattr(args, flag, None) and check not in values["checks"]:
                values["checks"].append(check)
    limits = dict(values.get("limits") or {})
    for name in RunConfig.model_fields["limits"].annotation.model_fields:
        value = getattr(args, name, None)
        if value is not None:
            limits[name] = value
    values["limits"] = limits
    if args.command == "cold-start":
        values["restart_target"] = args.restart
    return RunConfig.model_validate(values)


async def _run(config: RunConfig) -> RunReport:
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
        current = (event.get("target"), event.get("scenario"))
        if current[1] and (current != last or time.monotonic() - last_update >= 1):
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
    json_output = any(value == "--output-format=json" or (
        value == "--output-format" and index + 1 < len(arguments) and arguments[index + 1] == "json"
    ) for index, value in enumerate(arguments))
    kind = arguments[0] if arguments else "usage"

    def emit(outcome: str, exit_code: int, **details) -> None:
        print(json.dumps({"kind": kind, "outcome": outcome, "exit_code": exit_code, **details},
                         allow_nan=False))

    def failure(errors: list[dict]) -> int:
        if json_output:
            emit("error", 2, errors=errors)
        else:
            for error in errors:
                location = ".".join(map(str, error["loc"]))
                print(f"giraffe: {location + ': ' if location else ''}{error['message']}", file=sys.stderr)
        return 2

    try:
        args = parser().parse_args(arguments)
        kind = "baseline.save" if args.command == "baseline" else args.command
        if args.command == "describe":
            from giraffe.capabilities import capabilities

            print(json.dumps(capabilities(), allow_nan=False))
            return 0
        if args.command == "validate":
            config = RunConfig.model_validate(load_configuration(args.config))
            if json_output:
                emit("success", 0, config=config.model_dump(mode="json"))
            else:
                print("Configuration is valid. No traffic was sent.")
                print(config.model_dump_json(indent=2))
            return 0
        if args.command == "ui":
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            defaults = None
            if args.config:
                defaults = RunConfig.model_validate(yaml.safe_load(args.config.read_text())).model_dump(
                    mode="json"
                )
                defaults["restart_target"] = None
            import uvicorn
            from giraffe.web import create_app

            print(f"Giraffe UI: http://127.0.0.1:{args.port}\n"
                  f"Local runs: {args.runs_dir.resolve()}\n"
                  "Open the address in your browser. No tests start until you choose Run suite.",
                  flush=True)
            uvicorn.run(create_app(runs_dir=args.runs_dir, defaults=defaults),
                        host="127.0.0.1", port=args.port, log_level="warning")
            return 0
        if args.command == "baseline":
            report = load_report(args.report)
            args.destination.parent.mkdir(parents=True, exist_ok=True)
            with args.destination.open("w" if args.replace else "x") as file:
                file.write(report.model_dump_json(indent=2) + "\n")
            if json_output:
                emit("success", 0, run_id=report.run_id, destination=str(args.destination.resolve()))
            else:
                print(f"Saved baseline {report.run_id} ({report.overall}): {args.destination.resolve()}")
            return 0
        config = configuration(args)
        baseline = load_report(args.baseline) if args.baseline else None
        directory = args.output or Path("runs") / datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        if directory.exists() and any(directory.iterdir()):
            raise ValueError(f"Output directory is not empty: {directory}; choose a new directory")
        # Check output writability before sending traffic.
        directory.mkdir(parents=True, exist_ok=True)
        print(f"Giraffe: {len(config.targets)} target(s), at most {config.max_requests} requests, "
              f"{config.concurrency} in flight, {config.max_duration_seconds:g}s total, "
              f"{config.request_timeout_seconds:g}s/request, {config.max_output_tokens} output "
              "tokens/request. Ctrl-C saves a partial report.", file=sys.stderr, flush=True)
        from giraffe.reporting import compare_baseline, write_report

        report = asyncio.run(_run(config))
        if baseline:
            report = compare_baseline(report, baseline)
        json_path, html_path = write_report(report, directory)
        exit_code = 0 if report.overall == "pass" else 1 if report.overall == "fail" else 2
        if json_output:
            emit(report.overall, exit_code, run_id=report.run_id, request_count=len(report.requests),
                 abort_reason=report.abort_reason,
                 report_paths={"json": str(json_path.resolve()), "html": str(html_path.resolve())})
        else:
            print(f"{report.overall.upper()}: {len(report.requests)} requests")
            if report.abort_reason:
                print(f"Stopped: {report.abort_reason}")
            for check in report.checks:
                print(f"  {check.target} / {check.id}: {check.status} — {check.summary}")
            print(f"JSON: {json_path.resolve()}\nHTML: {html_path.resolve()}")
        return exit_code
    except ValidationError as error:
        # Pydantic's full repr contains raw input; only print locations/messages.
        return failure([{"loc": list(item["loc"]), "message": item["msg"], "type": item["type"]}
                        for item in error.errors(include_input=False, include_url=False, include_context=False)])
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = ["config", mark.line + 1, mark.column + 1] if mark else []
        return failure([{"loc": location, "message": "Invalid YAML/JSON configuration", "type": "parse_error"}])
    except (OSError, ValueError, KeyError) as error:
        return failure([{"loc": [], "message": str(error), "type": type(error).__name__}])


if __name__ == "__main__":
    sys.exit(main())
