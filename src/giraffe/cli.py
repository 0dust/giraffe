"""A local CLI: configure an endpoint, run the suite, keep a report/baseline."""

from __future__ import annotations

import argparse
import asyncio
import errno
import os
import socket
import json
import logging
import signal
import secrets
import ipaddress
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from giraffe import __version__
from giraffe.configuration_errors import configuration_errors
from giraffe.diagnostics import configure_logging, run_logging
from giraffe.models import (CATALOGUE_NAMES, CHECK_NAMES, ConfigDraft, RunConfig, RunReport, is_legacy_config,
                           migrate_concurrent_load, migrate_load_test, migrate_run_limits)
from giraffe.storage import DEFAULT_LIMITS, Heartbeat, format_size as _disk_size, parse_size
from giraffe.resources import PhaseSampler, SAMPLING


class UsageError(SystemExit):
    """An argument error that can use the caller's requested output format."""

    def __init__(self, message: str):
        super().__init__(4)
        self.message = message

    def __str__(self) -> str:
        return self.message


class ModelSelectionError(ValueError):
    """A URL-only run needs an explicit model choice before it can start."""


class LegacyNonstream(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        namespace.stream = False
        namespace._legacy_nonstream = True


class FullHelp(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.show_advanced = True
        parser.print_help()
        parser.exit()


class ArgumentParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        hidden = [(action, action.help) for action in self._actions
                  if getattr(action, "advanced", False) and not getattr(self, "show_advanced", False)]
        try:
            for action, _ in hidden:
                action.help = argparse.SUPPRESS
            return super().format_help()
        finally:
            for action, help_text in hidden:
                action.help = help_text

    def error(self, message: str) -> None:
        raise UsageError(message)


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError("must be non-negative")
    return number


def _cleanup_size(value: str) -> int:
    try:
        size = parse_size(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Cleanup --max-total must be positive; use --older-than 0 to remove all unprotected runs") from None
    if not size:
        raise argparse.ArgumentTypeError("Cleanup --max-total must be positive; use --older-than 0 to remove all unprotected runs")
    return size


logger = logging.getLogger(__name__)
_HEARTBEAT_SECONDS = 5


@contextmanager
def _heartbeat(path: Path):
    heartbeat = Heartbeat(path.parent, interval=_HEARTBEAT_SECONDS).start()
    try:
        yield heartbeat.close
    finally:
        heartbeat.close()


def _storage_flags(command: argparse.ArgumentParser) -> None:
    for name, label in (("max_total_storage", "saved runs on disk, excluding baselines"),
                        ("min_free_disk", "free disk required before starting"),
                        ("max_run_storage", "retained JSON per run on disk")):
        command.add_argument("--" + name.replace("_", "-"), type=parse_size,
                             default=DEFAULT_LIMITS[name], metavar="SIZE",
                             help=f"Limit for {label}; bytes or e.g. 500MiB, 2GiB; 0 disables "
                                  f"(default: {DEFAULT_LIMITS[name]} bytes)")


def _auto_cleanup_flags(command: argparse.ArgumentParser) -> None:
    command.add_argument("--auto-cleanup-keep-latest", type=_nonnegative, metavar="N",
                         help="After each run, delete eligible runs beyond newest N (default: off)")
    command.add_argument("--auto-cleanup-older-than", type=_nonnegative, metavar="DAYS",
                         help="After each run, delete eligible runs older than DAYS (default: off)")
    command.add_argument("--auto-cleanup-max-total", type=_cleanup_size, metavar="SIZE",
                         help="After each run, delete oldest eligible runs to reach SIZE (default: off)")


def _auto_cleanup_policy(args: argparse.Namespace) -> dict:
    names = {"keep_latest": "auto_cleanup_keep_latest", "older_than_days": "auto_cleanup_older_than",
             "max_total_bytes": "auto_cleanup_max_total"}
    return {key: value for key, name in names.items() if (value := getattr(args, name, None)) is not None}


def _storage_limits(args: argparse.Namespace) -> dict:
    return {name: getattr(args, name) for name in DEFAULT_LIMITS}


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
    validate.add_argument("--runs-dir", type=Path, help="Use recent run observations from this directory")
    preview = commands.add_parser("preview", help="Preview a draft configuration without traffic")
    preview.add_argument("--config", required=True, type=Path, help="YAML/JSON configuration; - reads stdin")
    preview.add_argument("--output-format", choices=["text", "json"], default="json")
    preview.add_argument("--runs-dir", type=Path, help="Use recent run observations from this directory")
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
    _storage_flags(trial)
    web = commands.add_parser("ui", aliases=["web"], help="Open the local web interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    web.add_argument("--no-browser", action="store_true", help="Print the access link without opening a browser")
    web.add_argument("--allow-restart-command", action="store_true", help="Allow configured server restart commands")
    web.add_argument("--runs-dir", type=Path, default=Path("runs"))
    web.add_argument("--config", type=Path, help="Prefill the run form from a YAML/JSON config")
    _storage_flags(web)
    _auto_cleanup_flags(web)
    init = commands.add_parser("init", help="Write a starter configuration")
    from giraffe.init_config import ENGINES
    init.add_argument("--engine", choices=list(ENGINES), default="ollama")
    init.add_argument("--url", help="Endpoint to discover the served model from")
    init.add_argument("--output", type=Path, default=Path("giraffe.yaml"))
    init.add_argument("--force", action="store_true", help="Replace an existing configuration")
    storage = commands.add_parser("storage", help=argparse.SUPPRESS)
    storage.add_argument("--runs-dir", type=Path, default=Path("runs"))
    storage.add_argument("--output-format", choices=["text", "json"], default="text")
    _storage_flags(storage)
    _auto_cleanup_flags(storage)
    cleanup = commands.add_parser("cleanup", help=argparse.SUPPRESS)
    cleanup.add_argument("--runs-dir", type=Path, default=Path("runs"))
    cleanup.add_argument("--older-than", type=_nonnegative, dest="older_than_days", metavar="DAYS")
    cleanup.add_argument("--keep-latest", type=_nonnegative, metavar="N",
                         help="Keep newest N runs; protected runs count toward N too")
    cleanup.add_argument("--max-total", type=_cleanup_size, dest="max_total_bytes", metavar="SIZE",
                         help="Positive target size on disk; use --older-than 0 to remove all unprotected runs")
    cleanup.add_argument("--yes", action="store_true", help="Delete the previewed candidates")
    keep = commands.add_parser("keep", help=argparse.SUPPRESS)
    keep.add_argument("run_dir", type=Path, metavar="RUN_DIR")
    keep.add_argument("--off", action="store_true", help="Remove the cleanup protection marker")
    runs = commands.add_parser("runs", help="List, clean up or protect saved runs")
    run_actions = runs.add_subparsers(dest="runs_action", required=True)
    for name, parent, description in (("list", storage, "Show saved-run sizes and limits"),
                                       ("cleanup", cleanup, "Preview or delete old runs"),
                                       ("keep", keep, "Protect a run from cleanup")):
        nested = run_actions.add_parser(name, parents=[parent], add_help=False, help=description)
        nested.set_defaults(command={"list": "storage"}.get(name, name))
    hidden_commands = {"storage", "cleanup", "keep"}
    commands._choices_actions[:] = [action for action in commands._choices_actions
                                   if action.dest not in hidden_commands]
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
        run.add_argument("--concurrency", type=int, help="Set both the simultaneous-request limit and default check concurrency (legacy)")
        run.add_argument("--simultaneous-limit", "--max-concurrency", type=int, help="Simultaneous limit across the suite (default: Not set)")
        run.add_argument("--default-concurrency", type=int, help="Default parallelism for checks (default: 4)")
        run.add_argument("--no-simultaneous-limit", action="store_true", help="Leave simultaneous target requests without a suite ceiling")
        run.add_argument("--max-requests", type=int, help="Maximum requests across all targets (default: Not set)")
        run.add_argument("--no-max-requests", action="store_true", help="Leave the suite request budget Not set")
        run.add_argument("--no-max-duration", action="store_true", help="Leave the suite traffic duration Not set")
        run.add_argument("--max-memory", type=int, dest="max_memory_mb",
                         help="Maximum memory (RAM growth during this run) in MiB (default: 4096); 0 disables. Only unfinished work stops when sampled RAM growth exceeds the limit; completed checks keep their results")
        run.add_argument("--max-duration", type=float, dest="max_duration_seconds", help="Maximum duration in seconds (default: Not set). Stops new requests and cancels in-progress requests when reached. Excludes AI evaluation and report saving")
        run.add_argument("--request-timeout", type=float, dest="request_timeout_seconds", help="Request timeout in seconds (default per request); per-check overrides and load_test.request_timeout_seconds take precedence")
        run.add_argument("--max-output-tokens", type=int, help="Maximum output tokens per request (default); check and saved-request overrides apply")
        run.add_argument("--context-limit", type=int, help="Context window for context and history checks; Concurrent load uses profile token targets")
        run.add_argument("--samples", type=int, help="Samples for count-based checks; Concurrent load uses its configured duration")
        run.add_argument("--sustained-seconds", type=float, help="Duration of sustained recovery load in seconds")
        run.add_argument("--stop-after-errors", type=int, help="Stop after this many request errors")
        run.add_argument("--unresponsive-after-failures", type=int,
                         help="Consecutive no-body inference failures before stopping a target, together with the silence interval (default: 5; 0 disables)")
        run.add_argument("--unresponsive-for", type=float, dest="unresponsive_for_seconds",
                         help="Minimum seconds without response body data before stopping a target (default: 45; headers and readiness polls do not reset it)")
        run.add_argument("--checks", metavar="CHECKS", help="Comma-separated names: " + ", ".join(CHECK_NAMES))
        run.add_argument("--structured-output", action="store_true", default=None, dest="structured_json",
                         help="Include structured JSON output checks")
        run.add_argument("--skip-preflight", action="store_true", help="Skip the bounded connection/model probe")
        run.add_argument("--deployment-file", type=Path, help="Bounded deployment JSON for the first target")
        run.add_argument("--metrics-url", help="Separate statistics endpoint for the first target")
        run.add_argument("--host-metrics-url", help="Optional node_exporter endpoint for server host CPU/RAM")
        run.add_argument("--metrics-api-key-env", help="Separate metrics secret reference")
        run.add_argument("--metrics-profile", choices=["unknown", "ollama", "vllm-v1", "vllm-legacy", "sglang", "llamacpp", "tensorrt-llm", "lmdeploy", "tgi"], help="Metrics format used by the statistics endpoint")
        run.add_argument("--tool-calling", action="store_true", default=None, help="Include tool-calling capability checks")
        run.add_argument("--metrics", action="store_true", default=None, help="Include GPU metrics observations")
        run.add_argument("--nonstream", action=LegacyNonstream, nargs=0, default=None, dest="stream", help=argparse.SUPPRESS)
        run.add_argument("--no-stream", action="store_false", default=None, dest="stream", help="Disable streaming responses")
        run.add_argument("--overlap-models", action="store_true", default=None, help="Run targets concurrently within the shared request limit")
        run.add_argument("--fixtures", dest="custom_fixtures", help="Optional custom fixture JSON")
        run.add_argument("--request-pack", dest="request_pack_file", metavar="PATH",
                         help="Import local request-pack JSON into the portable configuration")
        run.add_argument("--retention", choices=["all", "failures", "none"], help="Keep response text for all requests, failures only, or none; request metadata (timings, status, scores) is always saved. Retention does not cap disk usage; storage limits do")
        run.add_argument("--ca-bundle", help="TLS CA certificate bundle path")
        run.add_argument("--proxy", help="HTTP proxy URL")
        for option in ("first-output-ms", "latency-ms", "stream-gap-ms",
                       "min-output-tokens-per-second", "max-error-rate", "min-correctness",
                       "regression-percent"):
            run.add_argument("--" + option, type=float, help="Threshold for " + option.replace("-", " "))
        run.add_argument("--baseline", type=Path, help="Compare to this explicitly saved report")
        run.add_argument("--output", type=Path, help="New report directory (never overwrites a run)")
        run.add_argument("--save-config", type=Path, help="Write the normalized version-2 run configuration as JSON")
        run.add_argument("--output-format", choices=["text", "json"], default="text", help="Result format on stdout; progress is written to stderr")
        run.add_argument("--json", action="store_const", const="json", dest="output_format",
                         help="Alias for --output-format json")
        run.add_argument("--quiet", action="store_true", help="Print only the final verdict; warnings stay on stderr")
        run.add_argument("--fail-on", choices=["fail", "inconclusive", "never"], default="inconclusive",
                         help="Gate on fail/inconclusive (default), fail only, or never; errors still exit nonzero")
        run.add_argument("--junit", type=Path, metavar="PATH", help="Write JUnit XML")
        run.add_argument("--summary-md", type=Path, metavar="PATH", help="Write a Markdown summary")
        run.add_argument("--help-all", action=FullHelp, nargs=0, help="Show advanced options too")
        run.add_argument("--verbose", action="store_true", help="Show internal stages and memory observations")
        _storage_flags(run)
        _auto_cleanup_flags(run)
        run.add_argument("--runs-dir", type=Path, help="Explicit root for automatic cleanup; output must be a direct child")
        run.add_argument("--html", action="store_true", help="Also save a standalone HTML report")
        if name == "cold-start":
            run.add_argument("--restart", required=True, help="Name of target with restart_command")
        legacy = {"--concurrency", "--metrics", "--structured-output", "--tool-calling", "--nonstream"}
        common = {"help", "help_all", "config", "url", "model", "api_key_env", "simultaneous_limit",
                  "default_concurrency", "max_requests", "max_duration_seconds", "request_timeout_seconds",
                  "max_output_tokens", "checks", "stream", "baseline", "output", "output_format", "verbose",
                  "quiet", "fail_on", "junit", "summary_md", "html", "skip_preflight", "restart"}
        advanced = run.add_argument_group("advanced (shown with --help-all)")
        for action in list(run._actions):
            if legacy.intersection(action.option_strings):
                action.help = argparse.SUPPRESS
            elif action.dest not in common:
                action.advanced = True
                for group in run._action_groups:
                    if action in group._group_actions:
                        group._group_actions.remove(action)
                advanced._group_actions.append(action)
    report = commands.add_parser("report", help="Render a saved report without model requests")
    report.add_argument("run_dir", type=Path, metavar="RUN_DIR")
    report.add_argument("--html", action="store_true", help="Write a standalone HTML report")
    report.add_argument("--junit", type=Path, metavar="PATH")
    report.add_argument("--summary-md", type=Path, metavar="PATH")
    report.add_argument("--output", type=Path, metavar="PATH")
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
    commands.metavar = "{" + ",".join(name for name in commands.choices if name not in hidden_commands) + "}"
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


def _migrated_values(values):
    values, limit_notes = migrate_run_limits(values)
    values, load_notes = migrate_concurrent_load(values)
    values, combined_notes = migrate_load_test(values)
    return values, limit_notes + load_notes + combined_notes


def _configuration_notes(config, notes):
    config._migration_notes = list(dict.fromkeys([*notes, *config._migration_notes]))
    for note in notes:
        print(f"Migration: {note}", file=sys.stderr)
    return config


def migrated_configuration(values, model=RunConfig):
    values, notes = _migrated_values(values)
    return _configuration_notes(model.model_validate(values), notes)


def configuration(args: argparse.Namespace) -> RunConfig:
    for dest, flag, replacement in (("structured_json", "--structured-output", "--checks json"),
                                     ("tool_calling", "--tool-calling", "--checks tools"),
                                     ("metrics", "--metrics", "--checks gpu")):
        if getattr(args, dest, None):
            print(f"Deprecated: {flag}; use {replacement}.", file=sys.stderr)
    if getattr(args, "_legacy_nonstream", False):
        print("Deprecated: --nonstream; use --no-stream.", file=sys.stderr)
    values, notes = {}, []
    if args.config:
        values = load_configuration(args.config)
    elif args.manifest:
        values = dict(load_report(args.manifest).manifest["config"])
    else:
        values["config_version"] = 2
    if args.concurrency is not None and is_legacy_config(values):
        # Rebuild inherited legacy parallelism from the overridden limit while
        # preserving settings the saved input explicitly assigned to checks.
        values["concurrency"] = args.concurrency
    values, notes = _migrated_values(values)
    if values.get("restart_target"):
        if args.command != "cold-start":
            raise ValueError("Restart requires the explicit cold-start --restart TARGET command")
        values.pop("restart_target", None)
    if args.url or args.model or args.api_key_env:
        if "targets" in values:
            raise ValueError("Use targets in the config, or --url/--model; do not combine them")
        if not args.url:
            raise ValueError("Provide --url with --model, or use --config")
        model = args.model or _discover_model(args, values)
        values["targets"] = [{"name": "local", "url": args.url, "model": model,
                              "api_key_env": args.api_key_env}]
    if "targets" not in values:
        raise ValueError("Provide --url, or --config with at least one target")
    for target in values["targets"] if isinstance(values["targets"], list) else []:
        if isinstance(target, dict) and not target.get("model"):
            target["model"] = _discover_model(args, values, target)
    for name in RunConfig.model_fields:
        value = getattr(args, name, None)
        if value is not None and name not in {"checks", "limits"}:
            values[name] = value
    if args.concurrency is not None:
        values["default_concurrency"] = args.concurrency
        notes.append(f"--concurrency {args.concurrency} sets both the simultaneous-request limit and default_concurrency; use --simultaneous-limit to set only the ceiling.")
    if args.default_concurrency is not None:
        values["default_concurrency"] = args.default_concurrency
    if args.simultaneous_limit is not None:
        values["concurrency"] = args.simultaneous_limit
    for flag, field in (("no_max_requests", "max_requests"),
                        ("no_max_duration", "max_duration_seconds"),
                        ("no_simultaneous_limit", "concurrency")):
        if getattr(args, flag):
            values[field] = None
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
    for key in ("metrics_url", "metrics_api_key_env", "metrics_profile", "host_metrics_url"):
        if getattr(args, key, None) is not None:
            values["targets"][0][key] = getattr(args, key)
    args._configuration_input = values
    return _configuration_notes(RunConfig.model_validate(values), notes)


def _discover_model(args: argparse.Namespace, values: dict | None = None, target: dict | None = None) -> str:
    from giraffe.preflight import ModelDiscovery, PreflightError, discover_models

    values, target = values or {}, target or {}
    body = ModelDiscovery(url=target.get("url") or args.url,
                          api_key_env=args.api_key_env or target.get("api_key_env"),
                          headers_env=target.get("headers_env") or {},
                          ca_bundle=args.ca_bundle if args.ca_bundle is not None else values.get("ca_bundle"),
                          proxy=args.proxy if args.proxy is not None else values.get("proxy"))
    try:
        discovered = asyncio.run(discover_models(body, allow_manual=True))
    except PreflightError as error:
        message = str(error).rstrip(". ")
        if "environment variable" in message and "is not set" in message:
            raise ModelSelectionError(message) from None
        raise ModelSelectionError(f"{message}. Provide --model to select the served model.") from None
    models = discovered["models"]
    if len(models) > 1:
        raise ModelSelectionError("Choose --model: " + ", ".join(models))
    if not models:
        raise ModelSelectionError("No model could be discovered. Provide --model with the served model name.")
    if not getattr(args, "quiet", False):
        print(f"Using model {models[0]} (only model served)", file=sys.stderr)
    return models[0]


def _stage_name(scenario: str, config=None) -> str | None:
    if scenario == "initial":
        return "Connection check"
    if scenario == "warm":
        return "Warm-up"
    for check, title in CATALOGUE_NAMES.items():
        if scenario == check or scenario.startswith(check + "_"):
            return title
    if scenario in {"runtime_metrics_discovery", "performance_evidence_probe"}:
        return None
    from giraffe.conditions import condition_for
    label = condition_for(scenario, config=config)["label"]
    if label != scenario and not label.startswith(("load_", "traffic_", "pack_", "client_")):
        return label
    return "Running checks"


async def _run(config: RunConfig, *, json_progress: bool = False, verbose: bool = False, quiet: bool = False) -> RunReport:
    from giraffe.runner import run_suite

    stop = asyncio.Event()
    interrupted = False

    def request_stop():
        nonlocal interrupted
        interrupted = True
        stop.set()

    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    last = (None, None)
    last_update = 0.0

    def progress(event: dict) -> None:
        nonlocal last, last_update
        if quiet:
            return
        if json_progress:
            _progress_print(json.dumps(event, allow_nan=False))
            return
        current = (event.get("target"), event.get("scenario"))
        if current == last and time.monotonic() - last_update < 5:
            return
        title = (current[1] if verbose else _stage_name(current[1], config)) if current[1] else None
        if title:
            parts = [f"  {current[0]}: {title}"]
            if config.max_requests is not None:
                parts.append(f"{event.get('completed', 0)} of {config.max_requests} requests")
            parts.append(f"{event.get('elapsed_seconds', 0):g}s")
            _progress_print(" · ".join(parts))
            last = current
            last_update = time.monotonic()

    try:
        report = await run_suite(config, progress=progress, stop_event=stop)
        if interrupted:
            # Cooperative signal stops return normally from the suite. Keep
            # their partial evidence on the same export path as task cancellation.
            error = asyncio.CancelledError()
            error.giraffe_report = report
            raise error
        return report
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _print_memory(resources: dict) -> None:
    def size(value):
        if value is None:
            return ("not measured on this platform" if resources.get("memory_measurement") in {"unavailable", "not measured on this platform"}
                    or resources.get("measurement_available") is False else "not recorded")
        return f"{value / 1024**3:.2f} GiB" if value >= 1024**3 else f"{value / 1024**2:.1f} MiB"

    phases = resources.get("sampled_peak_rss_bytes", {})
    growth = resources.get("sampled_peak_growth_bytes", {})
    print("Peak memory (RAM), process RSS: " + " · ".join(f"{phase}: {size(phases.get(phase))}" for phase in ("traffic", "scoring", "export")))
    print("RAM at run start: " + size(resources.get("rss_at_start_bytes")))
    print("Peak RAM growth during this run: " + " · ".join(f"{phase}: {size(growth.get(phase))}" for phase in ("traffic", "scoring")))
    limit = resources.get("memory_limit_bytes")
    print("Maximum memory (RAM growth during this run): " + ("Off (0 = off)" if limit == 0 else size(limit)))
    if resources.get("memory_limit_reached"):
        print("Memory limit exceeded; " + ("unfinished work stopped with partial results; completed checks keep their results."
              if resources.get("memory_limit_stopped_run") else "completed checks keep their results; no work was stopped."))
    print("Process lifetime peak memory (RAM): " + size(resources.get("process_peak_rss_bytes")))
    print(resources.get("sampling", "Sampling details not recorded."))


def _main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = (bool(arguments) and arguments[0] in {"preview", "import-dataset"}) or "--json" in arguments or any(value == "--output-format=json" or (
        value == "--output-format" and index + 1 < len(arguments) and arguments[index + 1] == "json"
    ) for index, value in enumerate(arguments))
    if arguments and arguments[0] == "preview" and any(value == "--output-format=text" or (
        value == "--output-format" and index + 1 < len(arguments) and arguments[index + 1] == "text"
    ) for index, value in enumerate(arguments)):
        json_output = False
    kind = arguments[0] if arguments else "usage"
    validation_input = {}

    def emit(outcome: str, exit_code: int, **details) -> None:
        print(json.dumps({"kind": kind, "outcome": outcome, "exit_code": exit_code, **details},
                         allow_nan=False))

    def failure(errors: list[dict]) -> int:
        errors = [{**error, "message": error["message"].removeprefix("Value error, ").removeprefix("Assertion failed, ")}
                  for error in errors]
        if json_output:
            emit("error", 4, errors=errors)
        else:
            for error in errors:
                location = ".".join(map(str, error["loc"]))
                if len(error["loc"]) == 2 and error["loc"][0] == "targets" and error["message"].startswith(
                    ("target url ", "metrics_url ", "host_metrics_url ")
                ):
                    location = ""
                print(f"giraffe: {location + ': ' if location else ''}{error['message']}", file=sys.stderr)
        return 4

    try:
        args = parser().parse_args(arguments)
        if hasattr(args, "output_format"):
            json_output = args.output_format == "json"
        kind = "baseline.save" if args.command == "baseline" else args.command
        if arguments and arguments[0] in {"storage", "cleanup", "keep"}:
            replacement = {"storage": "list"}.get(arguments[0], arguments[0])
            print(f"Deprecated: giraffe {arguments[0]}; use giraffe runs {replacement}.", file=sys.stderr)
        if args.command == "init":
            from giraffe.init_config import write_config
            write_config(args.output, engine=args.engine, url=args.url, force=args.force)
            print(f"Configuration: {args.output}")
            return 0
        if args.command == "sglang-traces":
            from giraffe.sglang_traces import serve
            serve(args.output, args.port)
            return 0
        if args.command == "describe":
            from giraffe.capabilities import capabilities

            print(json.dumps(capabilities(), allow_nan=False))
            return 0
        if args.command == "validate":
            validation_input = load_configuration(args.config)
            config = migrated_configuration(validation_input)
            from giraffe.planning import request_plan
            from giraffe.latency import load_observations

            plan = request_plan(config, observations=load_observations(args.runs_dir or Path("runs"), config))
            if plan["errors"]:
                raise ValueError(plan["errors"][0]["msg"])
            warnings = plan["warnings"]
            if json_output:
                emit("success", 0, config=config.model_dump(mode="json"), migration_notes=config._migration_notes, warnings=warnings)
            else:
                print("Configuration is valid. No traffic was sent.")
                print("Targets: " + ", ".join(target.name for target in config.targets))
                print("Checks: " + ", ".join(config.checks))
                print(f"Maximum requests: {format(config.max_requests, ',') if config.max_requests is not None else 'Not set'}")
                for warning in warnings:
                    print(f"Warning: {warning}")
                if args.show_config:
                    print(config.model_dump_json(indent=2))
            return 0
        if args.command == "preview":
            from giraffe.request_pack import pack_preview
            from giraffe.planning import request_plan
            from giraffe.latency import load_observations
            from giraffe.open_files import open_file_guard

            validation_input = load_configuration(args.config)
            config = migrated_configuration(validation_input, ConfigDraft)
            preview = pack_preview(config)
            plan = request_plan(config, pack_details=preview,
                                observations=load_observations(args.runs_dir or Path("runs"), config))
            guard = open_file_guard(config)
            plan["open_files"] = guard
            plan["resource_issues"] = ([dict(issue, blocking=True) for issue in guard["errors"]]
                                       + [dict(issue, blocking=False) for issue in guard["warnings"]])
            plan["resource_warnings"] = guard["warnings"]
            plan["warnings"].extend(item["msg"] for item in guard["warnings"])
            if args.output_format == "json":
                print(json.dumps({"config": config.model_dump(mode="json"),
                                  "migration_notes": config._migration_notes, "request_pack_preview": preview, "request_plan": plan}, allow_nan=False))
            else:
                print("Configuration preview. No traffic was sent.")
                print("Targets: " + ", ".join(target.name for target in config.targets))
                print("Checks: " + ", ".join(config.checks))
                print(f"Known requests per target: {plan['known_per_target']:,}")
                print(f"Maximum requests: {format(config.max_requests, ',') if config.max_requests is not None else 'Not set'}")
                for warning in plan["warnings"]:
                    print(f"Warning: {warning}")
            return 0
        if args.command == "import-dataset":
            from giraffe.dataset_import import preview_dataset, read_dataset

            result = preview_dataset(read_dataset(args.file), args.file.suffix.lower().lstrip("."),
                                     input_field=args.input_field, expected_field=args.expected_field,
                                     scorer=args.scorer)
            print(json.dumps(result, allow_nan=False))
            return 4 if result["errors"] else 0
        if args.command == "storage":
            from giraffe.storage import storage_summary

            result = storage_summary(args.runs_dir, limits=_storage_limits(args), auto_cleanup=_auto_cleanup_policy(args))
            if json_output:
                print(json.dumps(result, allow_nan=False))
            else:
                print(f"Saved runs on disk: {_disk_size(result['runs_bytes'])}")
                print(f"Baselines on disk: {_disk_size(result['baselines_bytes'])}; "
                      f"{_disk_size(result['baselines_shared_bytes'])} shared with source run")
                print(f"Automatic cleanup: {json.dumps(result['auto_cleanup']) if result['auto_cleanup'] else 'off'}")
                print(f"Free disk: {_disk_size(result['free_bytes'])}")
                for name, value in result["limits"].items():
                    print(f"{name}: {_disk_size(value) if value else 'disabled'}")
                for row in result["runs"]:
                    protection = f"; protected: {row['protected_reason']}" if row["protected"] else ""
                    print(f"  {row['id']}: {_disk_size(row['bytes'])} on disk{protection}")
            return 0
        if args.command == "cleanup":
            from giraffe.storage import apply_cleanup, cleanup_plan

            criteria = {name: getattr(args, name) for name in
                        ("older_than_days", "keep_latest", "max_total_bytes")}
            plan = cleanup_plan(args.runs_dir, **criteria)
            count = len(plan["candidates"])
            print(f"Cleanup preview: {count} {'run' if count == 1 else 'runs'}, {_disk_size(plan['bytes_freed'])} on disk")
            days, latest = criteria["older_than_days"], criteria["keep_latest"]
            reasons = {"older_than_days": f"Older than {days} {'day' if days == 1 else 'days'}",
                       "keep_latest": "Beyond the newest run" if latest == 1 else f"Beyond the newest {latest} runs",
                       "max_total_bytes": f"Oldest first to reach {_disk_size(criteria['max_total_bytes'] or 0)} on disk"}
            for row in plan["candidates"]:
                reason = row.get("criterion", row.get("criteria", "selected"))
                reason = "; ".join(reasons.get(key, key) for key in reason.split(", "))
                print(f"  {row['id']}: {_disk_size(row['bytes'])} on disk; "
                      f"{row['started_at']}; {reason}")
            for row in plan["protected"]:
                print(f"  Protected {row['id']}: {row['protected_reason']}")
            if args.yes:
                result = apply_cleanup(args.runs_dir, [row["id"] for row in plan["candidates"]], **criteria)
                count = len(result["deleted"])
                print(f"Deleted {count} {'run' if count == 1 else 'runs'}, freed {_disk_size(result['bytes_freed'])} on disk.")
                if result["skipped"]:
                    print(f"Skipped {len(result['skipped'])}:")
                for row in result["skipped"]:
                    print(f"  {row['id']} — {row['reason']}")
            else:
                print("No runs deleted. Use --yes to delete these candidates.")
            return 0
        if args.command == "keep":
            from giraffe.storage import set_kept

            set_kept(args.run_dir, not args.off)
            print(f"{args.run_dir}: {'cleanup protection removed' if args.off else 'kept; protected from cleanup'}")
            return 0
        if args.command in {"ui", "web"}:
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            defaults, migration_notes = None, []
            if args.config:
                resolved = migrated_configuration(load_configuration(args.config))
                defaults = resolved.model_dump(mode="json")
                migration_notes = resolved._migration_notes
                defaults["restart_target"] = None
            import uvicorn
            from giraffe.web import create_app

            with socket.socket(socket.AF_INET6 if ":" in args.host else socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    listener.bind((args.host, args.port))
                    listener.listen(128)
                except OSError as error:
                    if error.errno == errno.EADDRINUSE:
                        raise ValueError(f"port {args.port} is in use; choose another with --port") from None
                    raise
                token = secrets.token_urlsafe(32)
                app = create_app(runs_dir=args.runs_dir, defaults=defaults, migration_notes=migration_notes,
                                 token=token, host=args.host, port=args.port, allow_restart_command=args.allow_restart_command,
                                 auto_cleanup=_auto_cleanup_policy(args), **_storage_limits(args))
                try:
                    loopback = ipaddress.ip_address(args.host).is_loopback
                except ValueError:
                    loopback = args.host.lower() == "localhost"
                if not loopback:
                    print("Warning: UI is listening beyond loopback; anyone with the access link can control runs. "
                          "Use a trusted network.", file=sys.stderr)
                link_host = "localhost" if args.host in {"0.0.0.0", "::"} else args.host
                url_host = f"[{link_host}]" if ":" in link_host else link_host
                access_url = f"http://{url_host}:{args.port}/?token={token}"
                print(f"Giraffe UI: {access_url} — open in your browser\n"
                      f"Local runs: {args.runs_dir.resolve()}\n"
                      "Review the plan, then choose Launch.", flush=True)
                server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
                                                       log_level="warning"))
                try:
                    server.run(sockets=[listener])
                except KeyboardInterrupt:
                    print("\nGiraffe UI stopped.")
            return 0
        if args.command == "report":
            report = load_report(args.run_dir)
            if not (args.html or args.junit or args.summary_md):
                raise ValueError("Choose --html, --junit PATH or --summary-md PATH")
            if args.output and not args.html:
                raise ValueError("--output requires --html")
            if args.html:
                from giraffe.reporting import _atomic_write, _render
                output = args.output or (args.run_dir if args.run_dir.is_dir() else args.run_dir.parent) / "report.html"
                _atomic_write(output, _render(report))
                print(f"HTML: {output.resolve()}")
            _ci_outputs(report, args, args.run_dir)
            return 0
        if args.command in {"export", "inspect"}:
            from giraffe.reporting import reproduction_export, _retained
            report = _retained(load_report(args.report))
            value = reproduction_export(report) if args.command == "export" else {
                "run_id": report.run_id, "observations": report.observations, "baseline": report.baseline,
                "performance": report.performance, "conditions": report.conditions,
                "workload_selection": report.manifest.get("workload_selection", {})}
            print(json.dumps(value, indent=2, allow_nan=False))
            return 0
        if args.command == "baseline":
            from giraffe.reporting import save_baseline_report
            report = load_report(args.report)
            source = args.report / "report.json" if args.report.is_dir() else args.report
            try:
                save_baseline_report(source, args.destination, replace=args.replace, report=report, portable=True)
            except FileExistsError:
                raise ValueError(f"Baseline already exists: {args.destination}; use --replace to overwrite it") from None
            if json_output:
                emit("success", 0, run_id=report.run_id, destination=str(args.destination.resolve()))
            else:
                print(f"Saved baseline {report.run_id} ({report.overall}): {args.destination.resolve()}")
            return 0
        if args.command == "try-example":
            from giraffe.dataset_import import try_example_config

            validation_input = load_configuration(args.config)
            config = try_example_config(migrated_configuration(validation_input, ConfigDraft),
                                        args.fixture_id, args.target_name)
            baseline = None
        else:
            config = configuration(args)
            baseline = load_report(args.baseline) if args.baseline else None
        if getattr(args, "save_config", None):
            args.save_config.write_text(json.dumps(config.model_dump(mode="json"), indent=2, allow_nan=False) + "\n")
        runs_root = getattr(args, "runs_dir", None) or Path("runs")
        directory = args.output or runs_root / datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        policy = _auto_cleanup_policy(args)
        if policy and directory.resolve().parent != runs_root.resolve():
            raise ValueError("Automatic cleanup requires an explicit --runs-dir and --output directly inside it; "
                             "without --runs-dir, --output must be inside the default runs directory")
        if directory.exists() and any(directory.iterdir()):
            raise ValueError(f"Output directory is not empty: {directory}; choose a new directory")
        from giraffe.storage import check_start

        check_start(directory.parent, _storage_limits(args))
        from giraffe.planning import request_plan
        from giraffe.preflight import PreflightError, check_target
        from giraffe.latency import load_observations

        plan = request_plan(config, observations=load_observations(runs_root, config))
        if plan["errors"]:
            raise ValueError(plan["errors"][0]["msg"])

        warnings = []
        guard = None
        if args.command in {"run", "cold-start", "try-example"}:
            from giraffe.open_files import open_file_guard

            guard = open_file_guard(config, raise_limit=True)
            if guard["errors"]:
                message = guard["errors"][0]["msg"]
                print(f"giraffe: {message}", file=sys.stderr)
                _blocked_outputs(config, args, directory, message)
                if json_output:
                    emit("blocked", 3, message=message, errors=guard["errors"])
                return 3
            warnings.extend(item["msg"] for item in guard["warnings"])
            for item in guard["warnings"]:
                print(f"Warning: {item['msg']}", file=sys.stderr)
        from giraffe.local_tokenizer import TokenizerUnavailable, ensure_tokenizers
        try:
            asyncio.run(ensure_tokenizers(config))
        except TokenizerUnavailable as error:
            print(f"giraffe: {error}", file=sys.stderr)
            _blocked_outputs(config, args, directory, str(error))
            if json_output:
                emit("blocked", 3, message=str(error))
            return 3
        if args.command == "run" and not args.skip_preflight:
            from giraffe.deployment import preparation_residency
            config._preparation_residency = asyncio.run(preparation_residency(config))
            async def preflight():
                for target in config.targets:
                    try:
                        await check_target(target, config)
                    except PreflightError as error:
                        if error.blocking:
                            return target.name, str(error).rstrip(". ") + ". Use --skip-preflight to run anyway."
                        warning = f"preflight for {target.name}: {error}; continuing."
                        warnings.append(warning)
                        print(f"Warning: {warning}", file=sys.stderr)
                return None

            blocked = asyncio.run(preflight())
            if blocked:
                target, message = blocked
                print(f"giraffe: preflight failed for {target}: {message}", file=sys.stderr)
                _blocked_outputs(config, args, directory, message)
                if json_output:
                    emit("blocked", 3, target=target, message=message)
                return 3
        planning_warnings = plan["warnings"]
        for warning in planning_warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        warnings = planning_warnings + warnings
        directory.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(timezone.utc).isoformat()

        def save_state(state: str, **details) -> None:
            from giraffe.reporting import _atomic_write
            from giraffe.storage import metadata_lock

            with metadata_lock:
                try:
                    saved = json.loads((directory / ".web.json").read_text())
                except (OSError, ValueError):
                    saved = {}
                _atomic_write(directory / ".web.json", json.dumps(
                    {"id": directory.name, "state": state, "started_at": started_at,
                     **({"latency_summary": saved["latency_summary"]} if "latency_summary" in saved else {}),
                     "owner_pid": os.getpid(), "owner_host": socket.gethostname(),
                     **({"heartbeat_at": datetime.now(timezone.utc).isoformat()}
                        if state in {"running", "stopping"} else {}), **details}, allow_nan=False, separators=(",", ":")) + "\n")

        save_state("running")
        requests = f"{config.max_requests:,}" if config.max_requests is not None else "Not set"
        simultaneous = f"{config.concurrency:,}" if config.concurrency is not None else "Not set"
        duration = f"{config.max_duration_seconds:,g}s" if config.max_duration_seconds is not None else "Not set"
        if not getattr(args, "quiet", False):
            print(f"Giraffe: {len(config.targets)} target(s), Maximum requests: {requests}, "
                  f"Simultaneous limit: {simultaneous}, Maximum duration: {duration}, "
                  f"Request timeout: {config.request_timeout_seconds:g}s, Maximum output tokens: {config.max_output_tokens}. "
                  "Check and saved-request overrides apply. Ctrl-C saves a partial report.", file=sys.stderr, flush=True)
        timeouts = []
        for check_id in config.checks:
            if check_id == "load":
                from giraffe.load_test import settings_sets
                timeouts.extend(f"load/{name}={settings.request_timeout_seconds:g}s"
                                for name, settings in settings_sets(config.load_test))
            elif (timeout := config.effective_check(check_id).get("request_timeout_seconds")) is not None:
                timeouts.append(f"{check_id}={timeout:g}s")
        if timeouts and not getattr(args, "quiet", False):
            print("Per-check request timeouts: " + ", ".join(timeouts)
                  + ". Saved requests may override these.", file=sys.stderr, flush=True)
        from giraffe.reporting import _retained, compare_baseline, html_failure_warning, save_failure_summary, write_report

        warning = None
        cancelled = None

        async def run_with_partial_report():
            try:
                report = await (_run(config, quiet=True) if getattr(args, "quiet", False) else
                                _run(config, json_progress=True) if json_output else
                                _run(config, verbose=True) if getattr(args, "verbose", False) else _run(config))
            except asyncio.CancelledError as error:
                report = getattr(error, "giraffe_report", None)
                if report is None:
                    raise
                return report, error
            return report, None

        with run_logging(directory), _heartbeat(directory / ".web.json") as stop_heartbeat:
            try:
                report, cancelled = asyncio.run(run_with_partial_report())
                from giraffe.invocation import cli_invocation
                report.manifest["invocation"] = cli_invocation(arguments)
                if guard is not None:
                    report.manifest["open_files"] = guard
                if baseline:
                    report = compare_baseline(report, baseline)
            except (KeyboardInterrupt, asyncio.CancelledError):
                logger.exception("CLI run stopped", extra={"giraffe_file_only": True})
                stop_heartbeat()
                try:
                    save_state("interrupted", finished_at=datetime.now(timezone.utc).isoformat(),
                               abort_reason="run cancelled")
                except OSError:
                    pass
                print("Stopped.", file=sys.stderr)
                if json_output:
                    emit("interrupted", 130, message="Stopped.")
                return 130
            except BaseException as error:
                logger.exception("CLI run could not finish")
                stop_heartbeat()
                try:
                    save_state("error", finished_at=datetime.now(timezone.utc).isoformat(),
                               error=f"Run could not finish ({type(error).__name__}).")
                except OSError:
                    pass
                raise
            sampler = PhaseSampler("export")
            resources = dict(report.observations.get("resources", {}))

            def export_resources() -> dict:
                resources["sampled_peak_rss_bytes"] = {
                    **resources.get("sampled_peak_rss_bytes", {}), **sampler.peaks}
                resources["sampling"] = SAMPLING
                if not resources.get("memory_measurement"):
                    resources["memory_measurement"] = "measured" if sampler.peaks.get("export") is not None else "unavailable"
                return resources

            try:
                with sampler:
                    json_path, html_path = write_report(report, directory, max_run_storage=args.max_run_storage,
                                                       html=getattr(args, "html", False))
            except OSError as error:
                logger.exception("CLI report export failed")
                export_resources()
                if getattr(error, "json_complete", False):
                    warning = html_failure_warning(error)
                    warnings.append(warning)
                    json_path, html_path = directory / "report.json", None
                    print(f"Warning: {warning}", file=sys.stderr)
                else:
                    stop_heartbeat()
                    message = save_failure_summary(report, directory, error)
                    try:
                        save_state("error", error=message, run_id=report.run_id, finished_at=report.finished_at,
                                   progress={"completed": len(report.requests)}, resources=resources)
                    except OSError:
                        pass
                    if json_output:
                        emit("error", 4, run_id=report.run_id, message=message, request_count=len(report.requests), resources=resources)
                    else:
                        print(f"giraffe: {message}", file=sys.stderr)
                        if getattr(args, "verbose", False) and not getattr(args, "quiet", False):
                            _print_memory(resources)
                    return 4
        export_resources()
        from giraffe.storage import automatic_cleanup

        policy = _auto_cleanup_policy(args)
        cleanup_result = None
        if policy and cancelled is None:
            try:
                cleanup_result = automatic_cleanup(runs_root, policy, active_id=directory.name)
            except OSError as error:
                cleanup_result = {"deleted": [], "error": str(error)}
            for identifier in cleanup_result["deleted"]:
                if not json_output and not getattr(args, "quiet", False):
                    print(f"Automatic cleanup deleted run: {identifier}")
        try:
            save_state("completed", run_id=report.run_id, finished_at=report.finished_at, abort_reason=report.abort_reason,
                       **({"auto_cleanup": cleanup_result, "auto_cleanup_policy": policy} if policy else {}),
                       **({"error": warning} if warning else {}), resources={
                "sampled_peak_rss_bytes": dict(sampler.peaks), "sampling": resources["sampling"],
                "memory_measurement": "measured" if sampler.peaks.get("export") is not None else "unavailable"})
        except OSError as error:
            warning = f"Run completed, but run metadata could not be saved ({error.strerror or type(error).__name__})."
            warnings.append(warning)
            print(f"Warning: {warning}", file=sys.stderr)
        report = _retained(report)
        exit_code = 130 if cancelled is not None else {"pass": 0, "fail": 1, "inconclusive": 2, "blocked": 3}.get(report.overall, 2)
        # The aggregate verdict prioritizes failed checks over blocked targets.
        # A client error can also occur after all of its checks already failed.
        operational_error = (any(check.required and check.status == "blocked" for check in report.checks)
                             or any(target.get("client_error")
                                    for target in report.observations.get("targets", {}).values()))
        if cancelled is None and operational_error:
            exit_code = 3
        _ci_outputs(report, args, directory)
        policy = getattr(args, "fail_on", "inconclusive")
        if cancelled is None and not operational_error and (
                (report.overall == "inconclusive" and policy in {"fail", "never"})
                or (report.overall == "fail" and policy == "never")):
            exit_code = 0
            print(f"Note: {report.overall} verdict accepted by --fail-on {policy}.", file=sys.stderr)
        summary = report.observations.get("summary")
        if json_output:
            emit(report.overall, exit_code, warnings=warnings, run_id=report.run_id, request_count=len(report.requests),
                 performance=report.performance,
                 abort_reason=report.abort_reason, resources=resources, auto_cleanup=cleanup_result,
                 **({"summary": summary} if summary else {}),
                 report_paths={"json": str(json_path.resolve()), "html": str(html_path.resolve()) if html_path else None})
        else:
            _print_result(report, json_path, html_path, directory, resources, verbose=getattr(args, "verbose", False), quiet=getattr(args, "quiet", False))
        return exit_code
    except ModelSelectionError as error:
        print(f"giraffe: {error}", file=sys.stderr)
        if json_output:
            emit("inconclusive", 2, message=str(error))
        return 2
    except BrokenPipeError:
        raise
    except UsageError as error:
        return failure([{"loc": [], "message": str(error), "type": "UsageError"}])
    except ValidationError as error:
        # One safe formatter owns both API and CLI messages and destinations.
        items = error.errors(include_input=False, include_url=False)
        formatted = configuration_errors(items, getattr(args, "_configuration_input", validation_input))
        errors = []
        for item, diagnostic in zip(items, formatted):
            entry = {"loc": list(item["loc"]), "message": diagnostic["msg"], "type": item["type"]}
            entry.update({key: diagnostic[key] for key in ("field", "fields", "values", "actions")})
            if "note" in diagnostic:
                entry["note"] = diagnostic["note"]
            errors.append(entry)
        return failure(errors)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = ["config", mark.line + 1, mark.column + 1] if mark else []
        return failure([{"loc": location, "message": "Invalid YAML/JSON configuration", "type": "parse_error"}])
    except (OSError, ValueError, KeyError) as error:
        return failure([{"loc": [], "message": str(error), "type": type(error).__name__}])


def _blocked_outputs(config: RunConfig, args: argparse.Namespace, directory: Path, reason: str) -> None:
    """Export CI evidence for a valid configuration blocked before suite traffic."""
    if not (getattr(args, "junit", None) or getattr(args, "summary_md", None)):
        return
    from giraffe.models import CheckResult
    now = datetime.now(timezone.utc).isoformat()
    report = RunReport(run_id=directory.name, started_at=now, finished_at=now, overall="blocked",
                       abort_reason=reason, manifest={"config": config.model_dump(mode="json")},
                       checks=[CheckResult(id=check, target=target.name, title=CHECK_NAMES[check],
                                           status="blocked", summary=reason)
                               for target in config.targets for check in config.checks])
    _ci_outputs(report, args, None)


def _ci_outputs(report: RunReport, args: argparse.Namespace, report_path: Path | None) -> None:
    from giraffe.ci_output import write_junit, write_summary_md
    if getattr(args, "junit", None):
        write_junit(report, args.junit)
    if getattr(args, "summary_md", None):
        write_summary_md(report, args.summary_md, report_path)


def _print_result(report: RunReport, json_path: Path, html_path: Path | None,
                  directory: Path, resources: dict, *, verbose: bool = False, quiet: bool = False) -> None:
    from giraffe.performance import cli_summary

    passed = sum(check.status == "pass" for check in report.checks)
    report_path = html_path or json_path
    print(f"{report.overall.upper()}  {passed} checks passed · {len(report.requests)} requests · report: {report_path}")
    if quiet:
        return
    for check in report.checks:
        if check.status in {"fail", "inconclusive", "blocked"}:
            print(f"  {check.target} / {check.id}: {check.status} — {' '.join(check.summary.split())}")
    if report.abort_reason:
        print(f"Stopped: {report.abort_reason}")
    if summary := report.observations.get("summary"):
        print(summary)
    for line in cli_summary(report):
        if not verbose and line.startswith("Performance · "):
            _, target_name, window_id = line.split(" · ", 2)
            window = next((window for target in (report.performance or {}).get("targets", [])
                           if target["target"] == target_name for window in target.get("windows", [])
                           if window["id"] == window_id), {})
            label = window.get("workload_label") or _stage_name(window_id.removeprefix("full:"))
            line = f"Performance · {target_name} · {label}"
        print(line)
    unselected = []
    for check in report.checks:
        if check.status == "skipped" and check.summary.rstrip(".") in {"Not selected", "Not selected for this run"}:
            if check.id not in unselected:
                unselected.append(check.id)
        elif check.status in {"pass", "skipped"}:
            print(f"  {check.target} / {check.id}: {check.status} — {' '.join(check.summary.split())}")
    if unselected:
        print("Not selected: " + ", ".join(unselected))
    for name, observation in report.observations.get("targets", {}).items():
        telemetry = observation.get("serving_telemetry", {})
        states = sorted({s.get("status", "unknown") for s in telemetry.get("snapshots", [])})
        print(f"  {name} / serving telemetry: {', '.join(states)}; source freshness and other traffic remain qualified in JSON.")
    if report.baseline:
        for target in report.baseline["targets"]:
            diff = target["configuration_diff"]
            changes = [row["field"] for row in diff["fields"] if row["change"] != "no_observed_change"]
            print(f"  {target['target']} / deployment changes: {', '.join(changes) or 'none observed; unknowns remain'}; confounded={diff['confounded']}; causality unverified")
    print(f"JSON: {json_path.resolve()}")
    if html_path:
        print(f"HTML: {html_path.resolve()}")
    else:
        print(f"Export HTML: giraffe report {directory.resolve()} --html")
    if verbose:
        _print_memory(resources)


class _PipeSafeStream:
    def __init__(self, stream):
        self.stream, self.broken = stream, False

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def _call(self, method, *args):
        if self.broken:
            return len(args[0]) if args else None
        try:
            return getattr(self.stream, method)(*args)
        except OSError as error:
            if error.errno != errno.EPIPE and not isinstance(error, BrokenPipeError):
                raise
            self.broken = True
            try:
                with open(os.devnull, "w") as sink:
                    os.dup2(sink.fileno(), self.stream.fileno())
            except (OSError, ValueError, AttributeError):
                pass
            return len(args[0]) if args else None

    def write(self, text):
        return self._call("write", text)

    def flush(self):
        return self._call("flush")


def _progress_print(message):
    stream = sys.stderr if isinstance(sys.stderr, _PipeSafeStream) else _PipeSafeStream(sys.stderr)
    print(message, file=stream, flush=True)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    stdout, stderr = sys.stdout, sys.stderr
    out, err = _PipeSafeStream(stdout), _PipeSafeStream(stderr)
    sys.stdout, sys.stderr = out, err
    try:
        try:
            return _main(argv)
        except SystemExit as error:
            if out.broken or err.broken:
                return int(error.code or 0)
            raise
    finally:
        try:
            out.flush()
            err.flush()
        finally:
            sys.stdout, sys.stderr = stdout, stderr


if __name__ == "__main__":
    sys.exit(main())
