"""A local CLI: configure an endpoint, run the suite, keep a report/baseline."""

from __future__ import annotations

import json
import argparse
import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from giraffe import __version__
from giraffe.models import CHECK_NAMES, RunConfig, RunReport


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(
        prog="giraffe", description="Run a bounded, built-in health suite against your LLM endpoint."
    )
    app.add_argument("--version", action="version", version=__version__)
    commands = app.add_subparsers(dest="command", required=True)
    web = commands.add_parser("ui", help="Open the local web interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--runs-dir", type=Path, default=Path("runs"))
    web.add_argument("--config", type=Path, help="Prefill the run form from a YAML/JSON config")
    for name in ("run", "cold-start"):
        run = commands.add_parser(name, help="Run the suite" if name == "run" else
                                  "Explicitly restart one configured target and test readiness")
        source = run.add_mutually_exclusive_group()
        source.add_argument("--config", type=Path, help="YAML or JSON run configuration")
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
        run.add_argument("--deployment-file", type=Path, help="Bounded deployment JSON for the first target")
        run.add_argument("--metrics-url", help="Separate statistics endpoint for the first target")
        run.add_argument("--metrics-api-key-env", help="Separate metrics secret reference")
        run.add_argument("--metrics-profile", choices=["unknown", "ollama", "vllm-v1", "vllm-legacy"])
        run.add_argument("--tool-calling", action="store_true", default=None)
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
    return app


def load_report(path: Path) -> RunReport:
    return RunReport.model_validate_json(path.read_text())


def configuration(args: argparse.Namespace) -> RunConfig:
    values = {}
    if args.config:
        values = yaml.safe_load(args.config.read_text())
        if not isinstance(values, dict):
            raise ValueError("Configuration must be a YAML/JSON object")
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
    limits = dict(values.get("limits") or {})
    for name in RunConfig.model_fields["limits"].annotation.model_fields:
        value = getattr(args, name, None)
        if value is not None:
            limits[name] = value
    values["limits"] = limits
    if args.command == "cold-start":
        values["restart_target"] = args.restart
    if getattr(args, "deployment_file", None):
        from giraffe.deployment import load_metadata
        values["targets"][0].setdefault("deployment", {}).update(load_metadata(args.deployment_file))
    for key in ("metrics_url", "metrics_api_key_env", "metrics_profile"):
        if getattr(args, key, None) is not None:
            values["targets"][0][key] = getattr(args, key)
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
    args = parser().parse_args(argv)
    try:
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
        if args.command in {"export", "inspect"}:
            from giraffe.reporting import reproduction_export, _retained
            report = _retained(load_report(args.report))
            value = reproduction_export(report) if args.command == "export" else {
                "run_id": report.run_id, "observations": report.observations, "baseline": report.baseline}
            print(json.dumps(value, indent=2, allow_nan=False))
            return 0
        if args.command == "baseline":
            from giraffe.reporting import _retained
            report = _retained(load_report(args.report))
            args.destination.parent.mkdir(parents=True, exist_ok=True)
            with args.destination.open("w" if args.replace else "x") as file:
                file.write(report.model_dump_json(indent=2) + "\n")
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
        print(f"{report.overall.upper()}: {len(report.requests)} requests")
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
        return 0 if report.overall == "pass" else 1 if report.overall == "fail" else 2
    except ValidationError as error:
        # Pydantic's full repr contains raw input; only print locations/messages.
        for item in error.errors(include_input=False, include_url=False, include_context=False):
            print(f"giraffe: {'.'.join(map(str, item['loc']))}: {item['msg']}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, yaml.YAMLError) as error:
        print(f"giraffe: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
