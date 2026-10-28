"""Compare like-for-like runs and write a portable, local results document."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import errno
from fractions import Fraction
from hashlib import sha256
from html import escape
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import tempfile
from statistics import median
from typing import Any

from pydantic import ValidationError

from .caps import (MAX_HTML_PREVIEW_CHARS, MAX_HTML_JSON_PREVIEW_CHARS,
                   DEFAULT_HTML_REQUEST_DETAILS, MAX_FAILURE_SUMMARY_TITLE_CHARS,
                   MAX_FAILURE_SUMMARY_CHARS)
from .deployment import deployment_diff, fixture_hash, safe_config, sanitize
from .report_inputs import input_ref as _input_ref
from .models import (CHECK_NAMES, SHAPES, CheckResult, RequestRecord, RunConfig, RunReport, overall_status, load_shape_label,
                     migrate_concurrent_load, migrate_run_limits)
from .storage import DEFAULT_LIMITS, format_size
from .workloads import WORKLOAD_CHECKS
from .timing import TIMING_METRICS, qualify, threshold as timing_threshold


# Identities are deliberately absent: changing a runtime/image is a reason to run the suite.
_MATCH_CONFIG = (
    "request_timeout_seconds",
    "max_output_tokens", "context_limit", "samples", "sustained_seconds", "checks",
    "structured_json", "overlap_models", "stream", "request_options", "stop_after_errors",
    "unresponsive_after_failures", "unresponsive_for_seconds",
    "limits",
)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _rate(record: RequestRecord) -> float | None:
    if record.output_tokens is None or record.output_tokens <= 0:
        return None
    return record.output_tokens * 1000 / record.elapsed_ms if record.elapsed_ms > 0 else None


def _groups(records: list[RequestRecord], target: str) -> dict[tuple[str, str], list[RequestRecord]]:
    groups: dict[tuple[str, str], list[RequestRecord]] = defaultdict(list)
    for record in records:
        if (record.target == target and getattr(record, "traffic_stage", None) is None
                and record.scenario not in {"client_cancel", "client_deadline", "sglang_cancel", "backend_cancel"}):
            for check in record.check_ids:
                if check == "load" and not (record.load_point or {}).get("point_id"):
                    continue  # Setup calibration is not a workload result group.
                scenario = record.scenario
                if check == "load" and record.load_point:
                    # A mixed test has one sending window but distinct shape cohorts.
                    scenario += "." + str(record.load_point.get("shape", "unknown"))
                    if record.load_point.get("role") != "request":
                        scenario += "." + str(record.load_point.get("role", "unknown"))
                groups[(check, scenario)].append(record)
    return groups


def _comparison_config(value: dict) -> dict:
    """Normalize saved config I/O without changing the recorded workload identity."""
    config, _ = migrate_run_limits(deepcopy(value))
    config, _ = migrate_concurrent_load(config)
    if isinstance(config.get("limits"), dict):
        config["limits"].pop("regression_percent", None)
    default = config.setdefault("default_concurrency", 4)
    ceiling = config.get("concurrency")
    if isinstance(ceiling, int):
        targets = len(config.get("targets", [])) if config.get("overlap_models") else 1
        default = max(1, min(default, ceiling // max(1, targets)))
    if isinstance(config.get("arrivals"), dict):
        arrivals = config["arrivals"]
        if arrivals.get("max_in_flight") is None:
            arrivals["max_in_flight"] = default
    if isinstance(config.get("traffic"), dict):
        # The user-supplied response-time estimate affects preview advice only.
        config["traffic"].pop("expected_response_seconds", None)
        if config["traffic"].get("max_in_flight") is None:
            config["traffic"]["max_in_flight"] = default
    return config


def _comparison_manifest(manifest: dict) -> dict:
    config = _comparison_config(manifest.get("config", {}))
    value = {**manifest, "config": config}
    try:
        resolved = RunConfig.model_validate({**config, "max_requests": None, "max_duration_seconds": None})
    except ValidationError:
        return value  # Preserve unknown/missing evidence rather than invent executed work.
    for field, checks in WORKLOAD_CHECKS.items():
        if checks.intersection(resolved.checks):
            settings = resolved.effective_arrivals() if field == "arrivals" else getattr(resolved, field)
            config[field] = settings.model_dump(mode="json") if hasattr(settings, "model_dump") else settings
    effective = {name: resolved.effective_check(name) for name in resolved.checks}
    if "effective_checks" not in value:
        value["effective_checks"] = effective
    elif isinstance(value.get("effective_checks"), dict):
        # Fill known historical omissions from the saved configuration. Retain
        # recorded effective values; missing earlier evidence stays unknown.
        added = {
            "serving": {"samples", "max_output_tokens", "request_timeout_seconds"},
            "first_output": {"max_output_tokens", "request_timeout_seconds"},
            "generation": {"request_timeout_seconds"},
            "context": {"max_output_tokens", "request_timeout_seconds"},
            "correctness": {"max_output_tokens", "request_timeout_seconds"},
            "json": {"max_output_tokens", "request_timeout_seconds"},
            "cancellation": {"max_output_tokens", "request_timeout_seconds",
                             "cancel_after_chunks", "cleanup_seconds"},
            "recovery": {"max_output_tokens", "request_timeout_seconds"},
            "capacity": {"max_output_tokens", "request_timeout_seconds"},
            "arrivals": {"max_output_tokens", "request_timeout_seconds"},
            "prefix": {"context_limit", "request_timeout_seconds"},
            "sessions": {"context_limit", "request_timeout_seconds"},
            "consistency": {"context_limit", "max_output_tokens", "request_timeout_seconds"},
            "tools": {"context_limit", "max_output_tokens", "request_timeout_seconds"},
            "concurrency": {"request_timeout_seconds"},
        }
        value["effective_checks"] = {
            check: {**{field: effective.get(check, {}).get(field)
                        for field in added.get(check, set()) if field in effective.get(check, {})},
                    **settings} if isinstance(settings, dict) else settings
            for check, settings in value["effective_checks"].items()
        }
    if "capacity" in resolved.checks:
        value.setdefault("effective_traffic", resolved.effective_traffic().model_dump(mode="json"))
    return value


def _run_limits_workload_identity(manifest: dict, *, revision: str) -> dict | None:
    """Reconstruct only identities whose generator revision is known exactly."""
    raw = manifest.get("config", {})
    legacy = revision == "legacy"
    try:
        config, _ = migrate_run_limits(deepcopy(raw))
        config, _ = migrate_concurrent_load(config)
        resolved = RunConfig.model_validate({**config, "max_requests": None, "max_duration_seconds": None})
    except ValidationError:
        return None
    if revision == "issue45" and not set(resolved.checks) <= {"sessions", "consistency"}:
        return None  # Load generators changed; this attestation covers unchanged specialists only.
    settings = {}
    for field, checks in WORKLOAD_CHECKS.items():
        if not checks.intersection(resolved.checks):
            continue
        if field not in raw:
            return None  # A fingerprint cannot attest settings missing from the saved config.
        value = raw[field] if legacy else getattr(resolved, field)
        settings[field] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    # These exact source revisions preserve default generated workloads across
    # the bounds/request-setting split. Unknown revisions retain strict matching.
    identity = {
        "generator_source_sha256": {
            "legacy": "4aa7fb48fa4be4a51b3341f34d86755dcfcca5c1043d1d362205d006115eccb0",
            "phase2": "c4edf02ed8db34d203da2328543acc3a49d98d3af41dc9471855b232c9ccf9f3",
            "phase3": "e147840e6530a40c0ac1607ac05840a5aec5abeb2faaff31e53bd2613057eaec",
            "review26": "00a83c3d261b368514d64e9c79a135d8b6e1b2839bf62a20a695e75e94c72eb7",
            "caps26": "00a83c3d261b368514d64e9c79a135d8b6e1b2839bf62a20a695e75e94c72eb7",
            "issue45": "9c47aa627a061bd90f0e181ce26b7faa912be790ae75547348c72445c4144b35",
        }[revision] if any(settings.values()) else None,
        "settings": settings,
    }
    if "concurrency" in resolved.checks:
        identity["concurrent_load_source_sha256"] = {
            "legacy": "33b66f3784ad9613db082fa1f00a2f3292fc910b9f842bd3fe018340a0fe7aa7",
            "phase2": "5d4c4519c9f4b83dbec1286cd77287cfbeabbb1b9578b0d1365e9ea7480c5b88",
            "phase3": "4124cfbd14b126c663cfa1e6f2e0a1cdc7146bfe415249e801e06049e3f79eb3",
            "review26": "af6447cbef3456e303a590b430b3d11ea8d93a6c1a553d54f26e41086320f222",
            # Named caps and readable remedies preserve the default requests.
            "caps26": "5fed7f8033592e8392a51a598f6eddddf1462b74e963569527d793539f62ff88",
        }[revision]
    if "consistency" in resolved.checks and resolved.consistency and resolved.consistency.logprobs:
        identity["logprob_source_sha256"] = {
            "legacy": "1cecd981fea42d75a7abffbe8a069699b97cca73a273c2786c240b5a8c78b7b2",
            "phase2": "1cecd981fea42d75a7abffbe8a069699b97cca73a273c2786c240b5a8c78b7b2",
            "phase3": "7e55d5a237bcc4c03b6f7c43ee3eb49385a3aeddf842a65466ad3e24d704ef81",
            "review26": "7e55d5a237bcc4c03b6f7c43ee3eb49385a3aeddf842a65466ad3e24d704ef81",
            "caps26": "d51d49b2716b3aa1f50c3b1e689cdc903399b9b7862bce59525194e44fea402e",
            "issue45": "d51d49b2716b3aa1f50c3b1e689cdc903399b9b7862bce59525194e44fea402e",
        }[revision]
    if "tools" in resolved.checks:
        from .tool_checks import resolved_tool_fixtures, resolved_tool_modes
        identity["resolved_tool_checks"] = {
            "fixtures": [fixture.model_dump(mode="json") for fixture in resolved_tool_fixtures(resolved)],
            "modes": resolved_tool_modes(resolved),
            **({"conversations": [fixture.model_dump(mode="json") for fixture in resolved.tool_checks.conversations],
                "conversation_source_sha256": ("4080132330b5ceb3d7e0b824bb26244a73967d81ae6b467441600e45c483eb48" if revision in {"phase3", "review26", "caps26"} else
                                               "283d1eb18bb852ca0132a2d4a8c63e49bc7155614603e74740dd80afe8189714")}
               if resolved.tool_checks and resolved.tool_checks.conversations else {}),
        }
    return identity


def _same_run_limits_workload(current: dict, old: dict) -> bool:
    if current.get("config", {}).get("config_version") != 2 or old.get("config", {}).get("config_version", 2) != 2:
        return False
    for manifest in (current, old):
        revisions = ("phase2", "phase3", "review26", "caps26", "issue45") if "config_version" in manifest.get("config", {}) else ("legacy",)
        identities = [_run_limits_workload_identity(manifest, revision=revision) for revision in revisions]
        if not any(identity is not None and fixture_hash(identity) == manifest.get("fixture_pack", {}).get("workload_fixture_sha256")
                   for identity in identities):
            return False
    current_config = _comparison_manifest(current)["config"]
    old_config = _comparison_manifest(old)["config"]
    selected = set(current_config.get("checks", [])) | set(old_config.get("checks", []))
    return all(current_config.get(field) == old_config.get(field)
               for field, checks in WORKLOAD_CHECKS.items() if checks & selected)


def _limits_reached(report: RunReport) -> set[str]:
    reached = set(report.limits_reached)
    if any("Global request budget exhausted" in warning for warning in report.warnings):
        reached.add("max_requests")
    load_metrics = [check.metrics for check in report.checks if check.id == "concurrency"]
    load_metrics.extend(target.get("concurrent_load", {})
                        for target in report.observations.get("targets", {}).values()
                        if isinstance(target, dict))
    if any(point.get("reason_code") == "budget_limited"
           for metrics in load_metrics
           if isinstance(metrics, dict)
           for group in metrics.get("load_types", {}).values()
           for point in group.get("points", [])):
        reached.add("max_requests")
    return reached


def _settings_reasons(report: RunReport, baseline: RunReport) -> list[str]:
    reasons = []
    if report.suite_version != baseline.suite_version:
        reasons.append("Suite versions differ.")
    if (report.schema_version != baseline.schema_version
            and {report.schema_version, baseline.schema_version} != {"2", "3"}):
        reasons.append("Report schema versions differ.")
    if report.abort_reason or report.observations.get("target_abort_reasons"):
        reasons.append("Current run was aborted; incomplete traffic cannot establish a regression.")
    if baseline.abort_reason or baseline.observations.get("target_abort_reasons"):
        reasons.append("Baseline run was aborted; it cannot establish a complete reference.")
    for label, run in (("Current", report), ("Baseline", baseline)):
        if any(record.evaluation_error for record in run.requests):
            reasons.append(f"{label} answer evaluation failed; correctness and capacity cannot be compared.")
    manifests = []
    for label, run in (("Current", report), ("Baseline", baseline)):
        try:
            manifests.append(_comparison_manifest(run.manifest))
        except ValidationError as error:
            # Older saved reports may contain inputs that current migrations reject.
            # Their unknown workload must not discard a completed current run.
            if any(item["loc"] and item["loc"][0] == "config_version"
                   for item in error.errors(include_input=False, include_url=False)):
                reasons.append(f"{label} recorded configuration has an unsupported config_version; comparison requires "
                               "version 2 or an unversioned legacy configuration. "
                               f"Rerun the {label.lower()} run with a supported configuration.")
            else:
                reasons.append(f"{label} recorded configuration is invalid; comparability is unknown. "
                               f"Rerun the {label.lower()} run with a valid configuration.")
    if len(manifests) != 2:
        return reasons
    current_manifest, old_manifest = manifests
    current_config = current_manifest["config"]
    old_config = old_manifest["config"]
    reached = _limits_reached(report) | _limits_reached(baseline)
    for key in ("concurrency", "max_requests", "max_duration_seconds"):
        if key in reached and current_config.get(key) != old_config.get(key):
            reasons.append(f"Run setting {key} differs and its limit stopped work or constrained concurrency.")
    pack_modes = [config.get("request_source") == "request_pack"
                  and bool({"load", "capacity", "correctness"}.intersection(config.get("checks", [])))
                  for config in (current_config, old_config)]
    if any(pack_modes):
        identities = [run.manifest.get("request_pack") for run in (report, baseline)]
        if not all(pack_modes):
            reasons.append("The effective request source differs.")
        elif not all(isinstance(value, dict) and value.get("sha256") for value in identities):
            reasons.append("Request pack identity is missing; comparability is unknown.")
        elif identities[0] != identities[1]:
            reasons.append("Request pack identity differs.")
        for label, run, config, active in zip(
            ("Current", "Baseline"), (report, baseline), (current_config, old_config), pack_modes
        ):
            if not active:
                continue
            judged_ids = {f"pack:{case['fixture_id']}"
                          for case in (config.get("request_pack") or {}).get("cases", [])
                          if case.get("scorer") == "judge"}
            if any(record.fixture_id in judged_ids and record.status == "completed"
                   and record.valid and record.score is None for record in run.requests):
                reasons.append(f"{label} AI judge results are incomplete; "
                               "correctness and capacity cannot be compared.")

    def effective_value(manifest, key):
        value = manifest.get(key)
        if key == "effective_traffic" and isinstance(value, dict):
            value = {name: setting for name, setting in value.items() if name != "expected_response_seconds"}
        if key == "effective_checks" and isinstance(value, dict):
            value = {check: {**settings, **({"limits": {
                name: limit for name, limit in settings["limits"].items()
                if name != "regression_percent"
            }} if isinstance(settings.get("limits"), dict) else {})}
                            if isinstance(settings, dict) else settings
                     for check, settings in value.items()}
        if key == "effective_checks" and isinstance(value, dict):
            cancellation = value.get("cancellation")
            if isinstance(cancellation, dict):
                # The runner schedules every client disconnect by useful chunks;
                # the legacy millisecond option never reaches an executed request.
                inactive = {"cancel_after_ms"}
                if cancellation.get("backend_verification", "off") == "off":
                    inactive.update({"backend_verification", "trace_file", "server_log", "trace_wait_seconds"})
                    if "recovery" not in manifest.get("config", {}).get("checks", []):
                        inactive.add("cleanup_seconds")
                    elif isinstance(cancellation.get("cleanup_seconds"), (int, float)):
                        # Recovery waits at most five seconds. Backend verification,
                        # when enabled, also uses the unbounded value as a verdict limit.
                        cancellation = {**cancellation, "cleanup_seconds": min(5, cancellation["cleanup_seconds"])}
                value = {**value, "cancellation": {
                    name: setting for name, setting in cancellation.items() if name not in inactive
                }}
        if key == "effective_traffic" and all(pack_modes) and isinstance(value, dict):
            return {name: setting for name, setting in value.items()
                    if name not in {"mix", "long_input_chars", "long_output_words"}}
        if key == "effective_checks" and all(pack_modes) and isinstance(value, dict):
            correctness = value.get("correctness")
            if isinstance(correctness, dict):
                # A pack always uses one serial quiet pass. Capacity, when
                # selected, also owns the payload used by that shared pass.
                shared = bool({"load", "capacity"}.intersection(manifest.get("config", {}).get("checks", [])))
                inactive = {"samples", "concurrency"}
                if shared:
                    inactive.update({"max_output_tokens", "request_timeout_seconds",
                                     "stream", "request_options"})
                correctness = {name: setting for name, setting in correctness.items()
                               if name not in inactive}
                return {**value, "correctness": correctness}
        return value

    selected = set(current_config.get("checks", [])) | set(old_config.get("checks", []))
    inactive_pack_globals = ({"context_limit", "samples", "sustained_seconds"}
                             if all(pack_modes) and selected <= {"load", "capacity", "correctness"} else set())
    comparison_workloads = {**WORKLOAD_CHECKS, "concurrent_load": {"concurrency"},
                            "arrivals": {"arrivals"}, "buckets": {"buckets", "mixed"},
                            "concurrent_workloads": {"token_stress"}}
    workload_fields = [key for key, checks in comparison_workloads.items() if checks & selected]
    for key in (*_MATCH_CONFIG, *workload_fields):
        if key in inactive_pack_globals:
            continue
        if key not in old_config or key not in current_config:
            reasons.append(f"Recorded {key} is missing; comparability is unknown.")
        elif old_config[key] != current_config[key]:
            reasons.append(f"Run setting {key} differs.")
    if any(config.get("test_options", {}).get("cancellation", {}).get("backend_verification") == "required"
           for config in (current_config, old_config)):
        cancellation_grading = [{check.target: "cancellation_results" in check.metrics
                                 for check in item.checks if check.id == "cancellation"}
                                for item in (report, baseline)]
        if cancellation_grading[0] != cancellation_grading[1]:
            reasons.append("Cancellation verdict definitions differ; rerun the baseline with separate server-stop and answer grading.")
    traffic_selected = any("capacity" in config.get("checks", []) for config in (current_config, old_config))
    for key, effective in (("traffic", "effective_traffic"), ("test_options", "effective_checks")):
        if key == "traffic" and not traffic_selected:
            continue
        if effective in current_manifest and effective in old_manifest:
            continue  # Saved drafts and explicit defaults do not change executed work.
        if key in current_config or key in old_config:
            if key not in current_config or key not in old_config:
                reasons.append(f"Recorded {key} is missing; comparability is unknown.")
            elif current_config[key] != old_config[key]:
                reasons.append(f"Run setting {key} differs.")
    for key in ("effective_checks", "effective_traffic"):
        if key not in current_manifest and key not in old_manifest:
            continue
        if key == "effective_checks" and ((key in report.manifest) != (key in baseline.manifest)):
            missing = baseline.manifest if key in report.manifest else report.manifest
            if "config_version" in missing.get("config", {}):
                reasons.append(f"Recorded {key} differs or is missing.")
                continue
        if key == "effective_traffic" and not traffic_selected:
            continue
        if key in current_manifest or key in old_manifest:
            if effective_value(current_manifest, key) != effective_value(old_manifest, key):
                reasons.append(f"Recorded {key} differs or is missing.")
    # The suite version identifies bundled fixtures; custom packs also need a content identity.
    current_pack = report.manifest.get("fixture_pack")
    old_pack = baseline.manifest.get("fixture_pack")
    if not isinstance(current_pack, dict) or not isinstance(old_pack, dict) or not current_pack or not old_pack:
        reasons.append("Fixture pack identity is missing or unknown.")
    else:
        inactive = {"custom"}
        if _same_run_limits_workload(report.manifest, baseline.manifest):
            inactive.add("workload_fixture_sha256")
        if ({key: value for key, value in current_pack.items() if key not in inactive}
                != {key: value for key, value in old_pack.items() if key not in inactive}):
            reasons.append("Fixture pack identity differs.")
    for label, config, pack in (("Current", current_config, current_pack),
                                ("Baseline", old_config, old_pack)):
        if config.get("custom_fixtures") and "correctness" in config.get("checks", []):
            if not isinstance(pack, dict) or not any(
                pack.get(key) for key in ("sha256", "custom_sha256", "hash", "content_hash")
            ):
                reasons.append(f"{label} custom fixtures have no recorded content hash.")
    for key in ("run_location", "load_shape"):
        if key not in report.manifest or key not in baseline.manifest:
            reasons.append(f"Recorded {key} is missing; comparability is unknown.")
        else:
            current, old = report.manifest[key], baseline.manifest[key]
            if key == "load_shape" and "max_requests" not in reached:
                current, old = ({name: setting for name, setting in shape.items()
                                 if name != "target_request_quotas"} if isinstance(shape, dict) else shape
                                for shape in (current, old))
            if current != old:
                reasons.append(f"{key} differs.")
    return reasons


def _generator_saturated(report: RunReport, target: str) -> bool:
    records = [r for r in report.requests if r.target == target]
    return _timing_quality(report, target, records, TIMING_METRICS)["status"] == "client_delayed"


def _timing_quality(report, target, records, metrics):
    return qualify(records, report.observations.get("client_timing_health"),
                   timing_threshold([r for r in report.requests if r.target == target]), metrics,
                   legacy_max_lag_ms=report.observations.get("generator_max_lag_ms", 0))


def _metric_values(records: list[RequestRecord]) -> dict[str, list[float]]:
    completed = [r for r in records if r.status == "completed" and r.valid]
    def clock_value(record, arrival_field, dispatch_field):
        arrival = (record.load_point or {}).get("pattern") in {"constant", "poisson", "bursts"}
        return getattr(record, arrival_field if arrival else dispatch_field)
    return {
        "valid_completion_rate": [float(r.status == "completed" and r.valid) for r in records],
        "error_rate": [float(r.status in {"failed", "timeout"}) for r in records],
        "correctness_rate": [float(r.score) for r in records if r.score is not None],
        "latency_ms": [v for r in completed if (v := clock_value(r, "arrival_elapsed_ms", "elapsed_ms")) is not None],
        "first_output_ms": [v for r in completed if (v := clock_value(r, "arrival_first_output_ms", "first_output_ms")) is not None],
        "max_stream_gap_ms": [
            r.max_stream_gap_ms for r in completed if r.max_stream_gap_ms is not None
        ],
        "output_tokens_per_second": [value for r in completed if (value := _rate(r)) is not None],
        "generation_tokens_per_second": [value for r in completed
                                         if (value := r.generation_tokens_per_second) is not None],
    }


def _output_work(records: list[RequestRecord]) -> dict[str, Any]:
    completed = [r for r in records if r.status == "completed" and r.valid]
    tokens = [r.output_tokens for r in completed if r.output_tokens is not None]
    return {
        "valid_completions": len(completed),
        "output_chars_p50": median([r.output_chars for r in completed]) if completed else None,
        "reported_output_tokens_p50": median(tokens) if tokens else None,
        "reported_token_samples": len(tokens),
    }


def _output_observations(check, scenario, current_work, old_work):
    observations = []
    for metric, value_key, sample_key in (
        ("output_tokens", "reported_output_tokens_p50", "reported_token_samples"),
        ("output_chars", "output_chars_p50", "valid_completions"),
    ):
        current_value, old_value = current_work[value_key], old_work[value_key]
        observations.append({"metric": metric, "check": check, "scenario": scenario,
            "current": current_value, "baseline": old_value, "aggregation": "median",
            "current_samples": current_work[sample_key], "baseline_samples": old_work[sample_key],
            "descriptive": True, "change_unit": "percent",
            "observed_delta": current_value - old_value if current_value is not None and old_value is not None else None,
            "observed_change": (current_value - old_value) * 100 / old_value
            if current_value is not None and old_value is not None and old_value > 0 else None})
    return observations


def _different_output_work(current: dict[str, Any], old: dict[str, Any], threshold: float) -> bool:
    for key in ("output_chars_p50", "reported_output_tokens_p50"):
        c, b = current[key], old[key]
        if c is not None and b is not None and c != b:
            if b == 0 or abs(c - b) * 100 / b > threshold:
                return True
    return False


def _compare_metric(
    name: str, current: list[float], old: list[float], minimum: int, threshold: float
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "metric": name, "current_samples": len(current), "baseline_samples": len(old),
        "status": "inconclusive",
    }
    is_rate = name.endswith("_rate")
    aggregate = (lambda data: sum(data) / len(data)) if is_rate else median
    current_value = aggregate(current) if current else None
    old_value = aggregate(old) if old else None
    result.update(current=current_value, baseline=old_value,
                  aggregation="mean" if is_rate else "median",
                  change_unit="percentage_points" if is_rate else "percent")
    if current_value is not None and old_value is not None:
        result["observed_delta"] = current_value - old_value
        result["observed_change"] = ((current_value - old_value) * 100 if is_rate else
                                     (current_value - old_value) * 100 / old_value if old_value > 0 else None)
    if min(len(current), len(old)) < minimum:
        result["reason"] = f"Needs at least {minimum} measured samples in each run."
        return result
    higher_is_better = name in {"correctness_rate", "valid_completion_rate", "output_tokens_per_second",
                               "generation_tokens_per_second"}
    direction = -1 if higher_is_better else 1
    # Rates use percentage points, avoiding undefined percentages for a zero baseline error rate.
    scale = 100 if is_rate else (100 / old_value if old_value > 0 else None)
    if scale is None:
        if current_value == old_value:
            result.update(status="pass", worsening=0, reason="Both measurements are zero.")
        else:
            result["reason"] = "A zero baseline does not support a relative timing comparison."
        return result
    worsening = (current_value - old_value) * direction * scale
    # Fixed seed makes re-rendering/repeating the same comparison reproducible.
    rng = random.Random(0)
    bootstrap = []
    for _ in range(400):
        c = aggregate(rng.choices(current, k=len(current)))
        b = aggregate(rng.choices(old, k=len(old)))
        bootstrap.append((c - b) * direction * scale)
    low, high = _percentile(bootstrap, 0.025), _percentile(bootstrap, 0.975)
    result.update(worsening=worsening, worsening_interval_95=[low, high], threshold=threshold)
    if worsening > 0 and low is not None and low >= threshold:
        result.update(status="fail", reason="Material worsening exceeds observed sample uncertainty.")
    elif high is not None and high <= threshold:
        result.update(status="pass", reason="No material worsening detected in these samples.")
    else:
        result["reason"] = "Sample variation crosses the material-regression threshold."
    return result


def _traffic_metrics(report: RunReport, target: str) -> dict[str, Any] | None:
    return next((check.metrics for check in report.checks
                 if check.id == "capacity" and check.target == target
                 and "traffic_stages" in check.metrics), None)


_LOAD_POINT_FIELDS = ("source", "type", "workload", "shape", "pattern", "level", "rate", "bursts")


def _load_work_comparison(current, old):
    if not current or not old:
        return "Request samples are absent from one run.", False
    cohorts = []
    for records in (current, old):
        groups = defaultdict(list)
        for record in records:
            groups[(record.load_point or {}).get("shape")].append(record)
        cohorts.append(groups)
    if set(cohorts[0]) != set(cohorts[1]):
        return "Result-group shapes differ.", False
    mix_changed = any(abs(Fraction(len(cohorts[0][shape]), len(current))
                          - Fraction(len(cohorts[1][shape]), len(old))) > Fraction(1, 10)
                      for shape in cohorts[0])
    for shape in cohorts[0]:
        paired = [groups[shape] for groups in cohorts]
        calibrated = all(r.workload.get("input_preparation", {}).get("target_tokens") is not None
                         for records in paired for r in records)
        counts = []
        for records in paired:
            identities = []
            for record in records:
                preparation = record.workload.get("input_preparation", {})
                input_identity = (("target_tokens", preparation["target_tokens"], tuple(
                                   (name, name in preparation, json.dumps(preparation.get(name), sort_keys=True))
                                   for name in ("method", "tokenizer", "chat_template", "count_scope", "token_count_scope", "encoding")))
                                  if calibrated else ("input_chars", record.input_chars))
                identities.append((record.fixture_id, record.requested_max_tokens, record.stream, input_identity))
            counts.append(Counter(identities))
        if set(counts[0]) != set(counts[1]):
            fields = [name for index, name in enumerate(("fixtures", "requested output caps", "streaming", "input identities"))
                      if {key[index] for key in counts[0]} != {key[index] for key in counts[1]}]
            return "Requested work differs: " + (", ".join(fields) or "work identity combinations") + ".", False
        mix_changed |= any(abs(Fraction(counts[0][key], len(paired[0]))
                               - Fraction(counts[1][key], len(paired[1]))) > Fraction(1, 10)
                           for key in counts[0])
    return None, mix_changed


def _load_not_delivered(point):
    return bool(point and point.get("reason_code") == "load_not_delivered")


def _server_limited_load(point):
    return bool(_load_not_delivered(point) and point.get("pattern") == "concurrent"
                and point.get("level") and point.get("server_evidenced_concurrency") is not None
                and point.get("client_mean_inflight") is not None
                and point["client_mean_inflight"] >= .9 * point["level"]
                and point["server_evidenced_concurrency"] < point["level"]
                and not point.get("concurrency_reason"))


def _compare_load_tests(
    report: RunReport, baseline: RunReport, target: str, reasons: list[str],
) -> dict[str, Any] | None:
    """Compare established acceptance separately from statistical metric sampling."""
    def metrics(run):
        return next((check.metrics for check in run.checks
                     if check.id == "load" and check.target == target
                     and check.metrics.get("points")), None)

    current, old = metrics(report), metrics(baseline)
    if current is None and old is None:
        return None
    result = {
        "status": "inconclusive", "reason": "", "points": [], "summary_counts": {},
        "current_highest_accepted": (current or {}).get("highest_accepted", []),
        "baseline_highest_accepted": (old or {}).get("highest_accepted", []),
        "method": "Matched workload, source, traffic pattern, overlays and tested level/rate; "
                  "qualification outcomes are distinct from statistical metric comparisons.",
    }
    if reasons:
        result["reason"] = "Load test comparison is unavailable: " + " ".join(reasons)
        return result
    if report.manifest.get("timing_measurement_version", 1) != baseline.manifest.get("timing_measurement_version", 1):
        result["reason"] = "Client timing measurement boundaries differ; rerun the baseline."
        return result
    if all(value.get("points") and all(point.get("pattern") == "interference" for point in value["points"])
           for value in (current or {}, old or {})):
        return None

    def points(value):
        return {tuple(point.get(name) for name in _LOAD_POINT_FIELDS): point
                for point in (value or {}).get("points", []) if point.get("pattern") != "interference"}

    def incomplete(point):
        for field, message in (("not_run", "test was not run"), ("cut_short", "test was cut short"),
                               ("schedule_overflow", "planned schedule overflowed"),
                               ("not_sent", "scheduled requests were not sent"), ("cancelled", "requests were cancelled")):
            if point.get(field):
                return message
        if point.get("scheduled") is not None and point["scheduled"] != point.get("dispatched"):
            return "dispatched requests do not cover the schedule"
        if not point.get("dispatched"):
            return "no requests were dispatched"
        return None

    def acceptance(point):
        if not point:
            return "not_established"
        if point.get("status") == "fail":
            return "fail"
        if incomplete(point):
            return "not_established"
        if _server_limited_load(point):
            return "fail"
        if (point.get("status") == "pass" and point.get("groups")
                and all(group.get("status") in {"pass", "fail"} for group in point["groups"])):
            return "pass"
        return "not_established"

    def records_for(run, point):
        ids = set(point.get("request_ids", []))
        records = [r for r in run.requests if r.target == target and r.id in ids]
        if len(records) != point.get("dispatched", 0) or len(ids) != point.get("dispatched", 0):
            return None
        return records

    def snapshot(run, point):
        if point is None:
            return None
        records = records_for(run, point)
        groups = point.get("groups", [])
        settings = run.manifest.get("effective_checks", {}).get("load", {})
        if point.get("source", "primary") != "primary":
            settings = next((item.get("settings", {}) for item in settings.get("migrated_tests", [])
                             if item.get("source") == point["source"] or item.get("id") == point["source"]), settings)
        required = point.get("required")
        if required is None:
            required = next((g["required"] for g in groups if g.get("required") is not None),
                            settings.get("pass_criteria", {}).get("min_valid_responses"))
        if required is None:
            required = run.manifest.get("config", {}).get("load_test", {}).get("pass_criteria", {}).get("min_valid_responses")
        valid = point.get("valid")
        if valid is None and records is not None:
            valid = sum(r.status == "completed" and r.valid for r in records)
        return {**{name: point.get(name) for name in (
            "id", "source", "type", "workload", "shape", "pattern", "level", "rate", "bursts",
            "status", "reason", "reason_code", "dispatched", "scheduled", "not_sent", "cancelled",
            "cut_short", "not_run", "server_evidenced_concurrency", "client_mean_inflight", "concurrency_reason")},
            "valid": valid, "required": required,
            "groups": [{name: group.get(name) for name in ("shape", "status", "reason_code", "reason")}
                       for group in groups]}

    def explanation(point, evidence):
        structural = incomplete(point)
        detail = structural if point.get("status") == "pass" and structural else point.get("reason")
        code = point.get("reason_code")
        if not detail or (point.get("status") == "pass" and code == "criteria_met"):
            if structural:
                detail = structural
            elif code and code != "criteria_met":
                detail = code.replace("_", " ")
            else:
                detail = "point reported failure" if point.get("status") == "fail" else "result-group evidence is incomplete"
        text = detail.rstrip(".") + (f" ({code})" if code and code != "criteria_met" else "")
        if code == "insufficient_responses" and evidence.get("valid") is not None and evidence.get("required") is not None:
            text += f"; {evidence['valid']} valid responses; {evidence['required']} required"
        return text

    def server_reason(point):
        return f"the server ran at most {point['server_evidenced_concurrency']} of {point['level']} requested concurrent requests"

    current_points, old_points = points(current), points(old)
    for cohort in dict.fromkeys([*old_points, *current_points]):
        point, previous = current_points.get(cohort), old_points.get(cohort)
        before, after = acceptance(previous), acceptance(point)
        row = {
            "current": snapshot(report, point), "baseline": snapshot(baseline, previous),
            "status": "inconclusive", "acceptance_change": "not_established", "reason": "",
            "baseline_acceptance": before, "current_acceptance": after,
            "evidence_ids": (point or {}).get("request_ids", []),
        }
        if point is None or previous is None:
            side = "current" if point is None else "baseline"
            row.update(acceptance_change=f"not_tested_in_{side}", reason=f"This level was not tested in the {side}.")
        else:
            current_work, old_work = records_for(report, point), records_for(baseline, previous)
            work_reason, mix_changed = None, False
            if current_work is None or old_work is None:
                work_reason = "Dispatched request metadata is incomplete."
            elif current_work and old_work:
                work_reason, mix_changed = _load_work_comparison(current_work, old_work)
            current_shapes = {g["shape"] for g in point.get("groups", [])}
            old_shapes = {g["shape"] for g in previous.get("groups", [])}
            if current_shapes and old_shapes and current_shapes != old_shapes:
                work_reason = "Result-group shapes differ."
            if work_reason:
                row.update(acceptance_change="work_changed", reason=work_reason)
            elif before == "pass" and after == "pass":
                row.update(acceptance_change="unchanged_accepted", status="pass", reason="This tested load remains accepted.")
            elif before == "fail" and after == "fail":
                row.update(acceptance_change="unchanged_not_accepted", reason="Still not accepted; baseline: " + explanation(previous, row["baseline"]) + "; current: " + explanation(point, row["current"]) + ".")
            elif before == "pass" and after == "fail":
                reason = ("No longer accepted: " + server_reason(point) + "." if _server_limited_load(point)
                          else "This previously acceptable tested load now fails; current: " + explanation(point, row["current"]) + ".")
                row.update(acceptance_change="lost", status="fail", reason=reason)
            elif before == "fail" and after == "pass":
                reason = ("Newly accepted: the baseline server ran at most "
                          f"{previous['server_evidenced_concurrency']} of {previous['level']} requested concurrent requests."
                          if _server_limited_load(previous) else "Newly accepted; baseline: " + explanation(previous, row["baseline"]) + ".")
                row.update(acceptance_change="gained", status="pass", reason=reason)
            elif after == "pass":
                row.update(acceptance_change="accepted_baseline_not_established", reason="Accepted now; not established in baseline; baseline: " + explanation(previous, row["baseline"]) + ".")
            elif before == "pass":
                row.update(acceptance_change="not_established_in_current", reason="Preservation is unverified; current: " + explanation(point, row["current"]) + ".")
            elif before == "not_established" and after == "fail":
                row.update(acceptance_change="not_established_in_baseline", reason="No loss is established; baseline: " + explanation(previous, row["baseline"]) + "; current: " + explanation(point, row["current"]) + ".")
            else:
                row["reason"] = "Acceptance is not established; baseline: " + explanation(previous, row["baseline"]) + "; current: " + explanation(point, row["current"]) + "."
            if mix_changed and row["acceptance_change"] in {"gained", "lost"}:
                row.update(acceptance_change="work_changed", status="inconclusive",
                           reason="Request mix shifted by more than 10 percentage points; acceptance change is not attributable.")
        result["points"].append(row)
    counts = Counter(row["acceptance_change"] for row in result["points"])
    result["summary_counts"] = {"gained": counts["gained"], "lost": counts["lost"], **counts}
    unverified = any(row["baseline_acceptance"] == "pass" and row["acceptance_change"] in {
        "not_established_in_current", "not_tested_in_current", "work_changed"} for row in result["points"])
    if counts["lost"]:
        result["status"] = "fail"
    elif unverified:
        result["status"] = "inconclusive"
    elif counts["unchanged_accepted"] or counts["gained"]:
        result["status"] = "pass"
    else:
        result["reason_code"] = "no_accepted_baseline"
    labels = {"gained": "tested load(s) newly accepted", "lost": "lost",
              "unchanged_accepted": "still accepted", "unchanged_not_accepted": "still not accepted",
              "accepted_baseline_not_established": "accepted now (baseline not established)",
              "not_established": "not established", "not_established_in_baseline": "not established in baseline",
              "not_established_in_current": "not established in current", "not_tested_in_baseline": "not tested in baseline",
              "not_tested_in_current": "not tested in current", "work_changed": "work changed"}
    summary = []
    for change, label in labels.items():
        if change in {"gained", "lost"} or counts[change]:
            text = f"{counts[change]} {label}"
            if change in {"gained", "lost"} and counts[change]:
                text += " (" + ", ".join(_comparison_load_label(row["current"] or row["baseline"])
                                         for row in result["points"] if row["acceptance_change"] == change) + ")"
            summary.append(text)
    result["reason"] = "; ".join(summary) + "."
    if unverified:
        result["reason"] += " Preservation of previously accepted load is unverified."
    if result.get("reason_code") == "no_accepted_baseline":
        result["reason"] += " No accepted baseline level can be assessed."
    return result


def _compare_traffic(
    report: RunReport, baseline: RunReport, target: str, reasons: list[str], threshold: float,
) -> dict[str, Any] | None:
    current, old = _traffic_metrics(report, target), _traffic_metrics(baseline, target)
    if current is None and old is None:
        return None
    result: dict[str, Any] = {
        "status": "inconclusive", "reason": "", "stages": [],
        "current_highest_acceptable_rate_rps": (current or {}).get("highest_acceptable_rate_rps"),
        "baseline_highest_acceptable_rate_rps": (old or {}).get("highest_acceptable_rate_rps"),
        "delta_rps": None, "change_percent": None,
        "method": "Matched planned arrival cohorts and acceptance criteria. Stage acceptance "
                  "changes are observed qualification results, not statistical capacity estimates.",
    }
    if report.manifest.get("timing_measurement_version", 1) != baseline.manifest.get("timing_measurement_version", 1):
        result["reason"] = "Client timing measurement boundaries differ; rerun the baseline before comparing traffic acceptance."
        return result
    for label, metrics in (("current", current), ("baseline", old)):
        pack = (metrics or {}).get("request_pack")
        if pack and pack.get("control_status") != "pass":
            # The source check retains its independently measured loaded rate.
            result[f"{label}_highest_acceptable_rate_rps"] = None
    if reasons:
        result["reason"] = "Traffic comparison is unavailable: " + " ".join(reasons)
        return result
    if current is None or old is None:
        result["reason"] = "One run lacks arrival-stage evidence; legacy concurrency is not arrival capacity."
        return result
    if report.manifest.get("request_pack") or baseline.manifest.get("request_pack"):
        if any(not metrics.get("request_pack", {}).get("coverage_complete")
               or not metrics.get("request_pack", {}).get("all_cases_scored")
               for metrics in (current, old)):
            result["reason"] = "Request pack comparison needs scored quiet coverage of every case in both runs."
            return result
        if any(metrics.get("request_pack", {}).get("control_status") != "pass"
               for metrics in (current, old)):
            result["reason"] = ("Request pack capacity comparison needs passing quiet controls in both runs; "
                                "loaded results remain available in each run's capacity check.")
            return result
    current_stages, old_stages = current["traffic_stages"], old["traffic_stages"]
    planned_rates = report.manifest.get("effective_traffic", {}).get("rates")
    if planned_rates is None:
        planned_rates = report.manifest.get("config", {}).get("traffic", {}).get("rates")
    if (not current_stages or not old_stages
            or [stage.get("rate_rps") for stage in current_stages]
            != [stage.get("rate_rps") for stage in old_stages]
            or [stage.get("rate_rps") for stage in current_stages] != planned_rates):
        result["reason"] = "The runs lack the same complete set of planned rate stages."
        return result
    for index, (stage, previous) in enumerate(zip(current_stages, old_stages)):
        row: dict[str, Any] = {
            "rate_rps": stage["rate_rps"], "current": stage, "baseline": previous,
            "status": "inconclusive", "reason": "Stage evidence is incomplete or generator-limited.",
            "goodput_delta_rps": None, "timing_comparable": False,
            "evidence_ids": [record.id for record in report.requests
                             if record.target == target and record.traffic_stage == index],
        }
        comparable = all(
            item.get("fully_offered") and not item.get("generator_limited")
            and item.get("status") in {"pass", "fail"} for item in (stage, previous)
        )
        if report.manifest.get("request_pack"):
            comparable = comparable and all(item.get("coverage_complete")
                                            and item.get("all_cases_scored")
                                            for item in (stage, previous))
        if comparable:
            row["goodput_delta_rps"] = stage["goodput_rps"] - previous["goodput_rps"]
            if previous.get("accepted") and not stage.get("accepted"):
                row.update(status="fail", reason="This previously acceptable rate now fails its configured criteria.")
            else:
                row.update(status="pass", reason="No loss of previously acceptable traffic at this tested rate.")
            current_work, old_work = (
                _output_work([record for record in run.requests
                              if record.target == target and record.traffic_stage == index])
                for run in (report, baseline)
            )
            row["output_work"] = {"current": current_work, "baseline": old_work}
            row["timing_comparable"] = bool(
                current_work["valid_completions"] and old_work["valid_completions"]
                and not _different_output_work(current_work, old_work, threshold)
            )
            if not row["timing_comparable"]:
                row["timing_comparison_reason"] = (
                    "Generated output work differs or is unmeasured; displayed timings do not establish "
                    "a like-for-like performance change. Qualification and goodput outcomes still apply."
                )
        result["stages"].append(row)
    failures = [row for row in result["stages"] if row["status"] == "fail"]
    incomplete = any(row["status"] == "inconclusive" for row in result["stages"])
    highest = result["current_highest_acceptable_rate_rps"]
    old_highest = result["baseline_highest_acceptable_rate_rps"]
    if not incomplete and highest is not None and old_highest is not None:
        result["delta_rps"] = highest - old_highest
        result["change_percent"] = (highest - old_highest) * 100 / old_highest
    if failures:
        result.update(status="fail", reason=f"{len(failures)} previously acceptable tested rate(s) now fail.")
    elif incomplete:
        result["reason"] = "Incomplete or generator-limited stages prevent a capacity comparison."
    elif old_highest is None:
        result["reason"] = "The baseline established no acceptable arrival rate."
    else:
        result.update(status="pass", reason="Previously acceptable tested arrival rates remain acceptable.")
    return result


def _load_group_notes_html(group):
    notes = []
    qualification = group.get("input_qualification")
    if qualification:
        notes.append("Input shape qualification: " + str(qualification.get("in_class", 0))
                     + " / " + str(qualification.get("known_valid", 0))
                     + " known-length valid responses in " + str(qualification.get("size", "")).capitalize()
                     + " class; " + str(qualification.get("out_of_class", 0))
                     + " outside; " + str(qualification.get("unknown_valid", 0))
                     + " unknown-length valid responses. " + str(qualification.get("reason") or "Input class exercised."))
    notes.extend(row["note"] for row in group.get("saved_question_budgets", []) if row.get("note"))
    coverage = group.get("coverage") or {}
    if coverage.get("threshold_tokens") is not None and "profile_output_cap" in coverage:
        notes.append(f"Required long-output stress coverage: {coverage['threshold_tokens']} tokens "
                     f"({coverage['min_output_percent']:g}% of Long output cap {coverage['profile_output_cap']})")
        notes.append("Actual sent output budgets: " + ", ".join(str(budget) for budget in coverage.get("sent_budgets", [])) + " tokens")
    return "".join("<br>" + _e(note) for note in notes)


def _load_test_html(metrics):
    parts = ['<h4>Load test</h4><p>Sequential workload × traffic tests. Highest accepted values are highest tested, not absolute capacity limits. Burst overlays are separate from steady-arrival capacity.</p>']
    for calibration in metrics.get("calibrations", []):
        if calibration.get("reason"):
            parts.append('<p>Local input preparation: ' + _e(calibration["reason"]) + '</p>')
    if metrics.get("workload_source"):
        parts.append('<p>Active workload source: '+_e(metrics["workload_source"])+'. '+_e(metrics.get("context_note", ""))+'</p>')
    for row in metrics.get("highest_accepted", []):
        parts.append('<p>'+_e(row['workload'])+' · '+_e(row['pattern'])+(' + bursts' if row.get('bursts') else '')+': <strong>'+_e(row['label'])+' '+_e(row['value'])+'</strong> (highest tested).</p>')
    for point in metrics.get("points", []):
        from giraffe.load_test import point_label
        parts.append('<h5>'+_e(point_label(point))+' · '+_e(point.get('status','inconclusive'))+'</h5><p>'+_e(point.get('reason',''))+'</p>')
        parts.append('<p>Observed concurrency: mean '+_e(point.get('client_mean_inflight'))+', peak '+_e(point.get('client_peak_inflight'))+', streamed evidence '+_e(point.get('server_evidenced_concurrency'))+'. Timing clock: '+_e(point.get('timing_clock','unknown'))+'.</p>')
        parts.append('<div class="table"><table><tr><th>Request type / delivery</th><th>Completions and evidence</th><th>Timing and throughput</th></tr>')
        for group in point.get('groups', []):
            lengths = group.get('lengths', {})
            parts.append('<tr><td>'+_e(load_shape_label(group['shape']))+'<br>Requested: '+_e(group.get('requested'))+' · scheduled: '+_e(group.get('scheduled'))+' · dispatched: '+_e(group.get('dispatched'))+'<br>Not sent: '+_e(group.get('not_sent'))+' (local drops '+_e(group.get('local_drops'))+', late '+_e(group.get('late_arrivals'))+', not offered '+_e(group.get('not_offered'))+')</td><td>'+_e(group.get('status'))+' · '+_e(group.get('reason'))+'<br>Valid '+_e(group.get('valid'))+' · failed '+_e(group.get('failed'))+' · timeout '+_e(group.get('timed_out'))+' · cancelled '+_e(group.get('cancelled'))+'<br>Error denominator: '+_e(group.get('error_denominator'))+'. '+_e(group.get('error_denominator_description'))+'<br>Scored '+_e(group.get('scored'))+' · correct '+_e(group.get('correct'))+' · unknown judgments '+_e(group.get('unscored'))+'<br>Actual input tokens '+_e(lengths.get('input_tokens'))+'; output tokens '+_e(lengths.get('output_tokens'))+'; unknown input/output '+_e(lengths.get('unknown_input_count'))+'/'+_e(lengths.get('unknown_output_count'))+'. '+_e(lengths.get('input_method'))+'; '+_e(lengths.get('output_method'))+_load_group_notes_html(group)+'<br>Coverage: '+_e(group.get('coverage','not required'))+'</td><td>First-answer p50/p95 ms: '+_e(group.get('first_answer_p50_ms'))+'/'+_e(group.get('first_answer_p95_ms'))+'<br>Completion p50/p95 ms: '+_e(group.get('completion_p50_ms'))+'/'+_e(group.get('completion_p95_ms'))+'<br>Longest pause ms '+_e(group.get('longest_stream_pause_ms'))+' · generation tokens/sec '+_e(group.get('generation_tokens_per_second'))+'<br>'+_e(lengths.get('generation_speed_method'))+'<br>Dispatch delay ms '+_e(group.get('dispatch_delay_ms'))+'<br>Completed requests/sec '+_e(group.get('completed_requests_per_second'))+' · aggregate output tokens/sec '+_e(group.get('aggregate_output_tokens_per_second'))+'<br>Correct work meeting limits '+_e(group.get('correct_work_meeting_limits'))+' ('+_e(group.get('correct_work_per_second'))+'/sec)<br>Window '+_e(group.get('throughput_window_seconds'))+' seconds; '+_e(group.get('completed_during_finish'))+' completions after sending stopped. '+_e(group.get('throughput_description'))+'</td></tr>')
        parts.append('</table></div>')
        for key,label in (('observed_mix','Observed request shares'),('interference','Short-only vs long-request comparison'),('burst_delivery','Burst delivery and actual spread'),('response_by_origin','Steady-arrival and burst response degradation')):
            if point.get(key):
                parts.append('<details><summary>'+label+'</summary><pre>'+_json(point[key])+'</pre></details>')
    if metrics.get('migration_notes'):
        parts.append('<p>Configuration migration: '+_e(' '.join(metrics['migration_notes']))+'</p>')
    return ''.join(parts)


def _concurrent_load_html(metrics):
    from giraffe.conditions import condition_for
    def shown(value):
        return "not reported" if value is None else f"{value:.3g}" if isinstance(value, float) else str(value)
    def tokens(lengths, name):
        values = lengths[name]
        actual = f"{shown(values['median'])} ({shown(values['min'])}–{shown(values['max'])})"
        return actual + " / " + shown(lengths["input_target" if name == "input_tokens" else "output_cap"])
    seconds = metrics.get("configured_load_seconds")
    parts = [f"<p>Configured load time: {_e(seconds)} {'second' if seconds == 1 else 'seconds'}.</p>"]
    passed = [p for group in metrics["load_types"].values() for p in group["points"] if p["status"] == "pass"]
    if passed:
        level = max(p["level"] for p in passed)
        parts.append(f"<p>Highest observed among tested settings: {level} concurrent {'request' if level == 1 else 'requests'}.</p>")
    for kind, group in metrics["load_types"].items():
        parts.append(f"<h4>{_e(group['title'])}</h4><div class=table><table><tr>" + "".join(
            f"<th>{_e(h)}</th>" for h in ("Workload", "Requested concurrency", "Delivered / configured window (s)",
            "Sent / completed", "Errors failed / timeouts", "Cancelled", "Valid / required", "Correct %",
            "Client concurrency mean / peak", "Server-evidenced concurrency", "First-output p95 (ms)", "Response p95 (ms)",
            "Requests/s", "Output tok/s", "Input tokens actual median (min–max) / target", "Output tokens actual median (min–max) / cap", "Status and reason")) + "</tr>")
        for point in group["points"]:
            lengths = point["token_lengths"]
            cells = [condition_for(point["id"])["label"], point["level"],
                f"{shown(point['delivered_window_seconds'])} / {shown(point['configured_window_seconds'])}",
                f"{point['sent']} / {point['completed']}", f"{point['failed']} / {point['timed_out']}", point["cancelled"],
                f"{point['valid']} / {point['required']}", shown(point["correct_percent"]) if point["correctness_applicable"] else "not applicable",
                f"{shown(point['client_mean_inflight'])} / {point['client_peak_inflight']}", point["server_evidenced_concurrency"],
                shown(point["first_output_p95_ms"]), shown(point["response_p95_ms"]), shown(point["requests_per_second"]), shown(point["output_tokens_per_second"]),
                "; ".join(f"{load_shape_label(shape)}: {tokens(value, 'input_tokens')}" for shape, value in lengths.items()),
                "; ".join(f"{load_shape_label(shape)}: {tokens(value, 'output_tokens')}" for shape, value in lengths.items()),
                f"{point['status']} · {point['reason']}"]
            parts.append("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in cells) + "</tr>")
            notes = [f"Drain: {shown(point['drain_seconds'])} s. Reasoning budget exhausted: {point['reasoning_budget_exhausted']}."]
            if point.get("throughput_warning"):
                notes.append(point["throughput_warning"])
            if kind == "mixed":
                notes.append("Requested / observed request mix / observed in-flight time: " + "; ".join(
                    f"{load_shape_label(shape)}: {shown(m['requested_percent'])}% / {shown(m['sent_percent'])}% ({m['sent']} sent) / {shown(m['inflight_percent'])}%"
                    for shape, m in point.get("observed_mix", {}).items()))
            if kind == "interference" and (data := point.get("interference")):
                notes.append(data["allocation"] + ". Control vs probe decode medians: " +
                    f"{shown(data['control']['decode_median_ms'])} / {shown(data['probe']['decode_median_ms'])} ms; " +
                    f"matched: {data['control']['matched_count']} / {data['probe']['matched_count']}; slowdown: {shown(data['slowdown'])}; " +
                    f"response-time ratio: {shown(data['response_time_ratio'])}; longest pauses: {shown(data['control']['longest_stream_pause_ms'])} / {shown(data['probe']['longest_stream_pause_ms'])} ms; " +
                    f"trigger latency median: {shown(data['trigger_latency_ms']['median'])} ms; probes never triggered: {data['probes_never_triggered']}. " +
                    f"Short-stream concurrency mean / peak / server-evidenced: {shown(point.get('short_stream_mean_inflight'))} / {shown(point.get('short_stream_peak_inflight'))} / {shown(point.get('short_stream_evidenced_concurrency'))}. " +
                    f"Buffered short streams excluded from matching: {shown(data.get('buffered_short_streams'))} / {shown(data.get('valid_short_streams'))}; ambiguous A/B overlaps excluded from both groups: {shown(data.get('ambiguous_short_streams'))}.")
            if coverage := point.get("coverage"):
                notes.append(f"Output coverage: {coverage['covered_valid_responses']} / {coverage['required']} required responses at or above {coverage['threshold_tokens']} {'token' if coverage['threshold_tokens'] == 1 else 'tokens'}.")
            parts.append('<tr><td colspan="17">' + _e(" ".join(notes)) + "</td></tr>")
        parts.append("</table></div>")
    parts.append("<p><strong>How these are measured.</strong> Sending windows begin at first dispatch; individual slots refill until the window ends. In-flight requests drain and remain in latency, correctness and error counts. Client concurrency is a time-weighted mean and peak during sending; server concurrency needs overlapping streamed output. Interference qualifies its N−1 continuous short streams plus enough matched probes in both groups; probe trigger idle time does not count against short-stream load. Throughput uses completions from the first completion through the sending-window end, excluding drain. With few samples p95 is effectively the maximum. Missing token counts are not reported, never zero. Input tokens are targets; output tokens are caps and answers may finish sooner.</p>")
    for calibration in metrics.get("calibrations", []):
        if calibration.get("reason_code") == "minimum_prompt_length":
            parts.append("<p>Local input preparation: " + _e(calibration["reason"]) + "</p>")
    if metrics.get("migration_notes"):
        parts.append("<p>Configuration migration: " + _e(" ".join(metrics["migration_notes"])) + "</p>")
    return "".join(parts)


def _acceptance_status(report: RunReport) -> str:
    checks = [check for check in report.checks if check.id != "baseline"]
    interrupted = {(item["target"], item["id"]) for item in
                   report.observations.get("resources", {}).get("memory_interrupted_checks", [])}
    return overall_status(checks, report.abort_reason,
        memory_limit_interrupted=any(check.required and (check.target, check.id) in interrupted for check in checks))


def compare_baseline(report: RunReport, baseline: RunReport) -> RunReport:
    """Return a new report with explicit baseline evidence; never mutate or promote a baseline."""
    result = report.model_copy(deep=True)
    result.checks = [check for check in result.checks if check.id != "baseline"]
    reasons = _settings_reasons(report, baseline)
    current_load = [c for c in report.checks if c.id == "concurrency" and c.required]
    previous_load = [c for c in baseline.checks if c.id == "concurrency" and c.required]
    if any("load_types" in c.metrics for c in current_load) != any("load_types" in c.metrics for c in previous_load) and current_load and previous_load:
        reasons.insert(0, "baseline uses the previous Concurrent workloads format")
    config = report.manifest.get("config", {})
    limits = config.get("limits", {})
    minimum = max(2, int(limits.get("min_samples", 5)))
    threshold = float(limits.get("regression_percent", 20))
    old_targets = {t["name"]: t for t in baseline.manifest.get("config", {}).get("targets", [])}
    targets = config.get("targets", [])
    comparison: dict[str, Any] = {
        "run_id": baseline.run_id, "status": "inconclusive", "targets": [],
        "min_samples": minimum, "regression_threshold": threshold,
        "deployment_changes_establish_causality": False,
        "source_outcome": baseline.overall,
        "acceptance": {label: _acceptance_status(run)
                       for label, run in (("baseline", baseline), ("current", report))},
        "source_started_at": baseline.started_at, "source_finished_at": baseline.finished_at,
        "method": "Per check/scenario; medians for timing and means for rates; "
                  "400 deterministic bootstrap resamples, 95% sample intervals. "
                  "Rates use percentage points; timing and output rate use relative percent. "
                  "Output token rate is end-to-end server-reported output tokens / elapsed seconds. "
                  "Intervals describe within-run variation, not cross-run environmental stability.",
    }
    for target in targets or [{"name": "unknown"}]:
        name = target["name"]
        target_reasons = list(reasons)
        saturated_runs = [label for label, run in (("Current", report), ("Baseline", baseline))
                          if _generator_saturated(run, name)]
        old_target = old_targets.get(name)
        identity_changes = {}
        if old_target is None:
            target_reasons.append("Target is absent from the baseline.")
        else:
            for key in ("model", "url", "route", "parent"):
                if key not in target or key not in old_target or target[key] != old_target[key]:
                    target_reasons.append(f"Target {key} differs or is unknown.")
            for key in set(target.get("identity", {})) | set(old_target.get("identity", {})):
                previous, current = old_target.get("identity", {}).get(key), target.get("identity", {}).get(key)
                if previous != current:
                    identity_changes[key] = {"baseline": previous, "current": current}
        current_deployment = report.observations.get("targets", {}).get(name, {}).get("deployment")
        old_deployment = baseline.observations.get("targets", {}).get(name, {}).get("deployment")
        configuration_diff = deployment_diff(current_deployment, old_deployment)
        consistency_changes = []
        for check in report.checks:
            if check.target == name and check.id == "consistency":
                previous = next((c for c in baseline.checks if c.target == name and c.id == "consistency"), None)
                for scenario, bucket in check.metrics.get("buckets", {}).items():
                    old_bucket = previous.metrics.get("buckets", {}).get(scenario, {}) if previous else {}
                    consistency_changes.append({"scenario": scenario,
                        "current_agreement": bucket.get("agreement_rate"),
                        "baseline_agreement": old_bucket.get("agreement_rate"),
                        "comparable": not target_reasons,
                        "interpretation": "Repeatability and task correctness are separate. Different answers alone do not prove regression."})
        rows = []
        load_exclusions = []
        observed_outputs = []
        traffic = _compare_traffic(report, baseline, name, target_reasons, threshold)
        load_tests = _compare_load_tests(report, baseline, name, target_reasons)
        load_points = {label: {point['id']: point for check in run.checks
                              if check.id == 'load' and check.target == name
                              for point in check.metrics.get('points', [])}
                       for label, run in (("Current", report), ("Baseline", baseline))}
        current_groups, old_groups = _groups(report.requests, name), _groups(baseline.requests, name)
        if (not current_groups or not old_groups) and traffic is None and load_tests is None:
            target_reasons.append("Measured request samples are absent from one or both runs.")
        if not target_reasons:
            for check, scenario in sorted(set(current_groups) | set(old_groups)):
                current_records = current_groups.get((check, scenario), [])
                old_records = old_groups.get((check, scenario), [])
                point_records = current_records or old_records
                load_point = ({field: point_records[0].load_point.get(field) for field in (*_LOAD_POINT_FIELDS, "point_id")}
                              if check == "load" and point_records else None)
                if load_point and load_point["pattern"] == "interference":
                    load_point.update({field: point_records[0].load_point.get(field) for field in ("phase", "role")})
                # Requested shape is checked too, rather than relying on configuration alone.
                def shapes(records: list[RequestRecord]) -> dict[tuple, Fraction]:
                    counts = Counter((r.fixture_id, r.input_chars, r.requested_max_tokens, r.stream)
                                     for r in records)
                    return {shape: Fraction(count, len(records)) for shape, count in counts.items()}
                work_reason, mix_changed = (_load_work_comparison(current_records, old_records)
                                            if check == "load" else (None, False))
                if check != "load" and (not current_records or not old_records or shapes(current_records) != shapes(old_records)):
                    work_reason = "missing or different request shapes or proportions."
                if work_reason:
                    if check == "load":
                        load_exclusions.append({"scenario": scenario, "load_point": load_point, "reason": work_reason})
                    else:
                        target_reasons.append(f"{check}/{scenario}: {work_reason}")
                    continue
                current_metrics, old_metrics = _metric_values(current_records), _metric_values(old_records)
                current_work, old_work = _output_work(current_records), _output_work(old_records)
                outputs = _output_observations(check, scenario, current_work, old_work)
                if load_point:
                    for output in outputs:
                        output["load_point"] = load_point
                observed_outputs.extend(outputs)
                different_work = _different_output_work(current_work, old_work, threshold)
                current_inputs = [r.input_tokens for r in current_records if r.valid and r.input_tokens is not None]
                old_inputs = [r.input_tokens for r in old_records if r.valid and r.input_tokens is not None]
                input_work_changed = bool(current_inputs and old_inputs and median(old_inputs)>0 and abs(median(current_inputs)-median(old_inputs))*100/median(old_inputs)>threshold)
                live_inputs_differ = any(r.workload.get("mode")=="live" for r in current_records) and Counter(r.request_hash for r in current_records) != Counter(r.request_hash for r in old_records)
                for metric, values in current_metrics.items():
                    if not values and not old_metrics[metric]:
                        continue
                    row = _compare_metric(metric, values, old_metrics[metric], minimum, threshold)
                    warnings = []
                    initial_uncertainty = row["reason"] if row["status"] == "inconclusive" else None
                    def warn(code, message, *, preserve_failure=False):
                        warnings.append({"code": code, "message": message})
                        if not preserve_failure or row["status"] != "fail":
                            row["status"] = "inconclusive"
                    if (input_work_changed or live_inputs_differ) and metric in {"latency_ms", "first_output_ms", "max_stream_gap_ms", "output_tokens_per_second", "generation_tokens_per_second"}:
                        warn("input_work_changed", "Observed input work or live generated histories differ; timing is not like-for-like.")
                    if different_work and metric in {"latency_ms", "max_stream_gap_ms", "output_tokens_per_second", "generation_tokens_per_second"}:
                        warn("output_work_changed", "Actual generated output lengths differ materially: "
                             f"median {old_work['reported_output_tokens_p50']} → {current_work['reported_output_tokens_p50']} tokens "
                             f"and {old_work['output_chars_p50']} → {current_work['output_chars_p50']} characters. "
                             "These timings include different amounts of generated work; this does not establish a speed improvement or regression.")
                    if metric in TIMING_METRICS:
                        if mix_changed:
                            warn("request_mix_changed", "Request work identity shares differ by more than 10 percentage points; timings reflect different request mixes.")
                        if load_point:
                            for label, records in (("Current", current_records), ("Baseline", old_records)):
                                point = load_points[label].get(records[0].load_point.get("point_id"), records[0].load_point)
                                if _load_not_delivered(point):
                                    if _server_limited_load(point):
                                        message = f"{label} server ran at most {point['server_evidenced_concurrency']} of {point['level']} concurrent requests; requested load was not delivered, so the timing populations differ."
                                    else:
                                        detail = point.get("concurrency_reason") or point.get("reason") or "Client occupancy or stream concurrency evidence is insufficient."
                                        message = f"{label} requested concurrency was not delivered or evidenced; {detail} Server queueing is not established."
                                    warn("load_not_delivered", message, preserve_failure=label == "Current")
                        quality = {"Current": _timing_quality(report, name, current_records, (metric,)),
                                   "Baseline": _timing_quality(baseline, name, old_records, (metric,))}
                        delayed_runs = [label for label, item in quality.items() if item["status"] == "client_delayed"]
                        row["timing_quality"] = quality
                        if delayed_runs:
                            for label in delayed_runs:
                                q = quality[label]
                                warn("client_delayed", f"{label} client scheduling delay overlaps "
                                     f"{q['affected_requests']}/{q['total_requests']} requests for this metric "
                                     f"(maximum overlapping delay {q['max_overlapping_lag_ms']:.0f} ms; threshold {q['threshold_ms']:.0f} ms). "
                                     "Some waiting time may come from Giraffe, so endpoint timing attribution is uncertain.")
                        if check == "load" and metric in {"latency_ms", "first_output_ms"}:
                            completed_counts = [sum(r.status == "completed" and r.valid for r in cohort)
                                                for cohort in (current_records, old_records)]
                            if len(values) < completed_counts[0] or len(old_metrics[metric]) < completed_counts[1]:
                                warn("missing_timing", "Per-response timing evidence is incomplete; missing clocks remain unknown.")
                        if report.manifest.get("timing_measurement_version", 1) != baseline.manifest.get("timing_measurement_version", 1):
                            warn("timing_version", "Client timing measurement boundaries differ; rerun the baseline.")
                    if warnings:
                        if initial_uncertainty:
                            warnings.insert(0, {"code": "sample_uncertainty", "message": initial_uncertainty})
                        row["reason"] = " ".join(warning["message"] for warning in warnings)
                    row["warnings"] = warnings
                    row.update(check=check, scenario=scenario,
                               output_work={"current": current_work, "baseline": old_work},
                               evidence_ids=[record.id for record in current_records])
                    if load_point:
                        row["load_point"] = load_point
                    rows.append(row)
        covered_load_points = {point["id"] for row in (load_tests or {}).get("points", [])
                               for side in ("current", "baseline") if (point := row.get(side))
                               and point.get("id") and point.get("pattern") != "interference"}
        for exclusion in load_exclusions:
            point = exclusion.get("load_point") or {}
            if point.get("pattern") == "interference" or point.get("point_id") not in covered_load_points:
                target_reasons.append(f"load/{exclusion['scenario']}: {exclusion['reason']}")
        assessed = [row for row in rows if "worsening" in row and row["status"] in {"pass", "fail"}]
        failures = [row for row in assessed if row["status"] == "fail"]
        uncertain = [row for row in rows if "worsening" in row and row["status"] == "inconclusive"]
        missing_measurements = [row for row in rows if "worsening" not in row and
                                max(row["current_samples"], row["baseline_samples"]) >= minimum]
        traffic_failures = [row for row in (traffic or {}).get("stages", []) if row["status"] == "fail"]
        load_failures = [row for row in (load_tests or {}).get("points", []) if row["status"] == "fail"]
        if failures or traffic_failures or load_failures:
            status = "fail"
            summary = (f"{len(failures)} material metric regression(s) and "
                       f"{len(traffic_failures)} lost acceptable traffic rate(s) against {baseline.run_id}.")
            if load_failures:
                summary += f" {len(load_failures)} previously acceptable tested load(s) now fail."
            if target_reasons:
                summary += " Other scenarios could not be compared: " + " ".join(target_reasons)
        elif target_reasons:
            status = "inconclusive"
            summary = "Baseline comparison is inconclusive: " + " ".join(target_reasons)
        elif traffic is not None and traffic["status"] == "inconclusive":
            status = "inconclusive"
            summary = traffic["reason"]
        elif (load_tests is not None and load_tests["status"] == "inconclusive"
              and load_tests.get("reason_code") != "no_accepted_baseline"):
            status = "inconclusive"
            summary = load_tests["reason"]
        elif (not assessed and (traffic is None or traffic["status"] != "pass")
              and (load_tests is None or load_tests["status"] != "pass"
                   or load_tests.get("reason_code") == "no_accepted_baseline")) or uncertain or missing_measurements:
            status = "inconclusive"
            if not assessed:
                summary = "No metric has enough comparable samples."
            elif missing_measurements:
                summary = "Some metrics lost measurement coverage or have undefined baseline values."
            elif saturated_runs:
                summary = "Overlapping client scheduling delay prevents a reliable timing/rate comparison."
            else:
                summary = "Sample variation or changed output lengths prevent a clear material-regression decision."
        else:
            status = "pass"
            summary = f"No material regression detected in {len(assessed)} comparable metric(s)."
            if traffic is not None:
                summary += " " + traffic["reason"]
        excluded = len(rows) - len(assessed)
        if (load_tests and load_tests["reason"] not in summary
                and any(load_tests.get("summary_counts", {}).get(change) for change in ("gained", "lost"))):
            summary += " " + load_tests["reason"]
        if excluded:
            summary += f" {excluded} metric(s) lack a qualified comparison; see comparison details."
        if status == "inconclusive" and any(min(row["current_samples"], row["baseline_samples"]) < minimum for row in rows):
            summary += f" Collect at least {minimum} measured samples per metric in both the current and baseline runs."
        entry = {"target": name, "status": status, "summary": summary, "reasons": target_reasons,
                 "load_exclusions": load_exclusions,
                 "observed_outputs": observed_outputs,
                 "client_delayed_runs": saturated_runs,
                 "generator_saturated_runs": saturated_runs,
                 "identity_changes": identity_changes, "configuration_diff": configuration_diff,
                 "consistency_changes": consistency_changes,
                 "telemetry": {"current": report.observations.get("targets", {}).get(name, {}).get("serving_telemetry"),
                               "baseline": baseline.observations.get("targets", {}).get(name, {}).get("serving_telemetry")},
                 "comparisons": rows}
        if traffic is not None:
            entry["traffic_capacity"] = traffic
        if load_tests is not None:
            entry["load_test"] = load_tests
        comparison["targets"].append(entry)
        result.checks.append(CheckResult(
            id="baseline", target=name, title="Baseline comparison", required=True,
            status=status, summary=summary,
            metrics={"compared_metrics": len(assessed), "excluded_metrics": excluded,
                     "regressions": len(failures) + len(traffic_failures) + len(load_failures),
                     "baseline_run_id": baseline.run_id},
            evidence_ids=list(dict.fromkeys(rid for row in failures + traffic_failures + load_failures
                                            for rid in row["evidence_ids"])),
        ))
    baseline_checks = [check for check in result.checks if check.id == "baseline"]
    comparison["status"] = overall_status(baseline_checks)
    result.baseline = comparison
    interrupted = {(item["target"], item["id"]) for item in
                   result.observations.get("resources", {}).get("memory_interrupted_checks", [])}
    result.overall = overall_status(result.checks, result.abort_reason,
        memory_limit_interrupted=any(check.required and (check.target, check.id) in interrupted
                                     for check in result.checks))
    from .performance import refresh_performance
    return refresh_performance(result)


def _retained(report: RunReport) -> RunReport:
    from .invocation import safe_invocation
    result = report.model_copy(update={"manifest": {**report.manifest,
        **({"invocation": safe_invocation(report.manifest["invocation"])}
           if "invocation" in report.manifest else {}),
        "config": safe_config(report.manifest.get("config", {}))}})
    if result.baseline:
        result.baseline = sanitize(result.baseline, bounded=False)
    policy = result.manifest.get("config", {}).get("retention", "failures")
    if policy == "none":
        result.checks = []
        for check in report.checks:
            summary = _without_answer_excerpts(check.summary)
            scenarios = check.metrics.get("scenarios", {})
            changed = {name: {**scenario, "summary": _without_answer_excerpts(scenario["summary"])}
                       for name, scenario in scenarios.items()
                       if _without_answer_excerpts(scenario["summary"]) != scenario["summary"]}
            metrics = ({**check.metrics, "scenarios": {**scenarios, **changed}}
                       if changed else check.metrics)
            result.checks.append(check.model_copy(update={"summary": summary, "metrics": metrics})
                                 if summary != check.summary or changed else check)
    failed_evidence = {rid for check in result.checks if check.status in {"fail", "blocked"}
                       for rid in check.evidence_ids}
    result.requests = []
    for record in report.requests:
        keep = policy == "all" or (policy == "failures" and (
            (record.status in {"failed", "timeout"} and record.scenario not in {"client_cancel", "client_deadline"})
            or (record.status == "completed" and not record.valid) or record.score is False
            or record.evaluation_error is not None or record.id in failed_evidence
        ))
        result.requests.append(record if keep else _without_response(record))
    from .performance import refresh_performance
    return refresh_performance(result)


def _without_response(record: RequestRecord) -> RequestRecord:
    updates: dict[str, Any] = {"output": "", "reasoning": "", "tool_calls": [], "assistant_message": None}
    if record.score_message:
        updates["score_message"] = _without_answer_excerpts(record.score_message)
    if record.logprob_evidence and record.logprob_evidence.tokens:
        updates["logprob_evidence"] = record.logprob_evidence.model_copy(update={"tokens": []})
    if record.tool_conversation:
        updates["tool_conversation"] = {**record.tool_conversation, "final_content": None,
            "calls": [{**call, "result": None} for call in record.tool_conversation["calls"]]}
        if record.evaluation and record.evaluation.messages:
            updates["evaluation"] = record.evaluation.model_copy(update={"messages": []})
    if record.judgement and any(vote.reason is not None for vote in record.judgement.votes):
        updates["judgement"] = record.judgement.model_copy(update={"votes": [
            vote.model_copy(update={"reason": None}) if vote.reason is not None else vote
            for vote in record.judgement.votes]})
    updates = {name: value for name, value in updates.items() if value != getattr(record, name)}
    return record.model_copy(update=updates) if updates else record


def _has_response(record: RequestRecord) -> bool:
    return bool(record.output or record.reasoning or record.tool_calls or record.assistant_message
                or (record.logprob_evidence and record.logprob_evidence.tokens)
                or (record.judgement and any(v.reason for v in record.judgement.votes))
                or (record.tool_conversation and (record.tool_conversation.get("final_content")
                    or any(call.get("result") is not None
                           for call in record.tool_conversation.get("calls", [])))))


def _request_json(record: RequestRecord) -> str:
    value = record.model_dump(mode="json")
    if record.evaluation and record.evaluation.messages:
        value["input_ref"] = _input_ref(record.evaluation.messages)
        del value["evaluation"]["messages"]
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _report_json_chunks(report: RunReport):
    # Keep defaults for raw consumers. Only exact input messages move to the table.
    metadata = report.model_copy(update={"schema_version": "3"}).model_dump_json(exclude={"requests", "performance"})
    yield metadata[:-1] + ',"report_format":2,"inputs":{'
    seen = set()
    for record in report.requests:
        if not record.evaluation or not record.evaluation.messages:
            continue
        messages = record.evaluation.messages
        reference = _input_ref(messages)
        if reference in seen:
            continue
        yield "," if seen else ""
        seen.add(reference)
        yield json.dumps(reference) + ":"
        yield from json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")).iterencode(messages)
    yield '},"requests":['
    for index, record in enumerate(report.requests):
        yield "," if index else ""
        yield _request_json(record)
    yield "]}"


def _portable_json_chunks(report: RunReport):
    metadata = report.model_copy(update={"schema_version": "2"}).model_dump_json(exclude={"requests", "performance"})
    yield metadata[:-1] + ',"requests":['
    for index, record in enumerate(report.requests):
        yield "," if index else ""
        yield record.model_dump_json()
    yield "]}"


def save_baseline_report(source: Path, destination: Path, *, replace: bool = False,
                         report: RunReport | None = None, portable: bool = False) -> None:
    """Publish an independent snapshot, sharing immutable bytes where supported."""
    source, destination = Path(source), Path(destination)
    report = report if report is not None else RunReport.model_validate_json(source.read_text())
    retained = _retained(report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp",
                                       dir=destination.parent)
    temporary = Path(name)
    try:
        os.close(descriptor)
        temporary.unlink()
        if portable:
            _atomic_write_chunks(temporary, _portable_json_chunks(retained))
            os.chmod(temporary, 0o644)
        elif (source.is_symlink() or source.name != "report.json"
              or retained != report or report.schema_version != "3"):
            _atomic_write_chunks(temporary, _report_json_chunks(retained))
        else:
            try:
                os.link(source, temporary)
            except OSError:
                with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=65536)
        if replace:
            temporary.replace(destination)
        else:
            # Exclusive publication keeps the CLI's no-overwrite contract.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise
            except OSError:
                with destination.open("xb"):
                    pass
                try:
                    temporary.replace(destination)
                except OSError:
                    destination.unlink(missing_ok=True)
                    raise
    finally:
        temporary.unlink(missing_ok=True)


def _json_size(report: RunReport) -> int:
    return sum(len(chunk.encode("utf-8")) for chunk in _report_json_chunks(report))


def _storage_limited(report: RunReport, limit: int) -> None:
    storage = {"limit_bytes": limit, "response_bodies_omitted": 0,
               "reason": "Per-run JSON storage limit on disk.", "limit_exceeded": False}
    report.manifest["storage"] = storage
    if not limit:
        return
    size = _json_size(report)
    if size <= limit:
        return
    evidence = {rid for check in report.checks if check.status in {"fail", "blocked", "inconclusive"}
                for rid in check.evidence_ids}
    ordered = sorted((i for i, r in enumerate(report.requests) if _has_response(r)), key=lambda i: bool(
        report.requests[i].id in evidence or report.requests[i].score is False
        or report.requests[i].evaluation_error
        or (report.requests[i].status in {"failed", "timeout"}
            and report.requests[i].scenario not in {"client_cancel", "client_deadline"})
        or (report.requests[i].status == "completed" and not report.requests[i].valid)))
    previous_reason = storage["reason"]
    storage["reason"] = "Response bodies omitted to fit the per-run JSON limit on disk."
    size += len(storage["reason"]) - len(previous_reason)
    for index in ordered:
        original = report.requests[index]
        before = len(_request_json(original).encode("utf-8"))
        record = report.requests[index] = _without_response(original)
        size -= before - len(_request_json(record).encode("utf-8"))
        previous_count = storage["response_bodies_omitted"]
        storage["response_bodies_omitted"] += 1
        size += len(str(storage["response_bodies_omitted"])) - len(str(previous_count))
        if original.evaluation and record.evaluation and original.evaluation.messages != record.evaluation.messages:
            # Tool-conversation retention can also remove an input-table entry.
            size = _json_size(report)
        if size <= limit:
            return
    storage["limit_exceeded"] = True
    storage["reason"] = ("Request metadata exceeds the per-run JSON limit on disk; "
                         "all request metadata was preserved.")


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _without_answer_excerpts(text: str) -> str:
    return re.sub(r' Expected: "(?:[^"\\]|\\.)*"; received: "(?:[^"\\]|\\.)*"\.', '', text)


def connection_summary(records: list[RequestRecord], url: str) -> str | None:
    attempted = [r for r in records if r.status != "cancelled"]
    classes = ("ConnectError", "ConnectTimeout", "SSLError", "ConnectionError",
               "ConnectionRefusedError", "ProxyError", "UnsupportedProtocol")
    errors = []
    for record in attempted:
        error = next((name for name in classes if name in (record.error or "")), None)
        if error is None and "connection refused" in (record.error or "").lower():
            error = "ConnectionRefusedError"
        if record.http_status is not None or record.status != "failed" or error is None:
            return None
        errors.append(error)
    if not errors:
        return None
    return f"Could not connect to {url} ({', '.join(dict.fromkeys(errors))}); no checks could run"


def _preview(value: Any, limit: int = MAX_HTML_PREVIEW_CHARS) -> str:
    text = str(value)
    if len(text) <= limit:
        return _e(text)
    return _e(text[:limit]) + '… [Preview truncated; <a href="report.json">full evidence in report.json</a>]'


def _json(value: Any, limit: int = MAX_HTML_JSON_PREVIEW_CHARS) -> str:
    # This helper emits text inside <pre>, never an attribute. Quote entities
    # inflate retained JSON substantially without adding protection in text.
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    content = escape(text[:limit], quote=False)
    return content + ('… [Preview truncated; <a href="report.json">full evidence in report.json</a>]'
                      if len(text) > limit else '')


def _tool_evidence(record: RequestRecord) -> str:
    expectation = record.evaluation.tool_expectation if record.evaluation else None
    if expectation is None and record.tool_evaluation is None:
        return ""
    parts = []
    if record.tool_conversation:
        parts.append('<h3>Tool-result conversation</h3>'
                     f'<pre>{_json(record.tool_conversation)}</pre>')
    if expectation is not None:
        parts.append('<h3>Tool-call expectations</h3>'
                     '<p>Expected calls are checked in order, including their count.</p>'
                     f'<pre>{_json([call.model_dump() for call in expectation.expected_calls]) if expectation.expected_calls is not None else "No task-specific call expectation configured."}</pre>'
                     '<details><summary>Declared tools</summary>'
                     f'<pre>{_json(expectation.tools)}</pre></details>')
        if expectation.choice:
            parts.append('<h3>Tool choice rule</h3>'
                         f'<pre>{_json(expectation.choice.model_dump())}</pre>')
        parts.append('<h3>Assistant content expectation</h3>')
        content = expectation.expected_content
        if content is None:
            parts.append('<p>No assistant content expectation configured.</p>')
        else:
            parts.append(f'<p>Match: {_e(content.mode)}; '
                         f'normalization: {_e(content.normalization)}.</p>'
                         f'<pre>{_e(content.value)}</pre>')
    if record.tool_evaluation is not None:
        parts.append('<h3>Tool response outcomes</h3>'
                     '<table><thead><tr><th>Component</th><th>Outcome</th>'
                     '<th>Evidence</th></tr></thead><tbody>')
        components = [("protocol", "Protocol"), ("content", "Assistant content")]
        components += ([("choice", "Tool choice"), ("arguments", "Argument schemas"),
                        ("task", "Task expectations")] if record.tool_evaluation.choice
                       else [("calls", "Tool calls")])
        if record.tool_evaluation.support:
            parts.append(f'<tr><td>Support</td><td colspan="2">{_e(record.tool_evaluation.support)}</td></tr>')
        for field, label in components:
            outcome = getattr(record.tool_evaluation, field)
            if outcome is None:
                continue
            parts.append(f'<tr><td>{label}</td><td>{_e(outcome.status.replace("_", " "))}</td>'
                         f'<td>{_e(outcome.message)}</td></tr>')
        parts.append('</tbody></table>')
    if record.tool_request_options is not None:
        parts.append('<details><summary>Sent tool options</summary>'
                     f'<pre>{_json(record.tool_request_options)}</pre></details>')
    parts.append('<h3>Retained tool calls</h3>')
    if record.tool_calls:
        parts.append(f'<pre>{_json(record.tool_calls)}</pre>')
    else:
        parts.append('<p>No retained tool-call body. Consult the recorded outcomes; '
                     'absence here does not establish that no call was returned.</p>')
    return "".join(parts)


def _number(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:,.1f}"


def _badge(status: str) -> str:
    return f'<span class="badge {_e(status)}">{_e(status)}</span>'


def _rps(value: float | None) -> str:
    return "not established" if value is None else f"{value:,.4g} requests/s"


def _judgement_table(record: RequestRecord) -> str:
    judgement = record.judgement
    if not judgement:
        return ""
    votes = judgement.votes
    yes = sum(vote.verdict is True for vote in votes)
    no = sum(vote.verdict is False for vote in votes)
    parts = [f'<h3>AI judge verdicts {_badge(judgement.status)}</h3>',
             f'<p>{yes} accept · {no} reject · {len(votes) - yes - no} unavailable. '
             'A complete panel needs a strict majority; ties and errors are inconclusive.</p>',
             '<div class="table"><table><tr><th>Judge / model</th><th>Verdict</th><th>Reason</th></tr>']
    for vote in votes:
        verdict = "Accept" if vote.verdict is True else "Reject" if vote.verdict is False else "Unavailable"
        reason = vote.reason or vote.error_category or "Reason omitted by response retention."
        parts.append(f'<tr><td>{_e(vote.judge)}<br>{_e(vote.model)}</td>'
                     f'<td>{verdict}</td><td>{_e(reason)}</td></tr>')
    parts.append('</table></div>')
    return "".join(parts)


def _pack_table(check: CheckResult) -> str:
    pack = check.metrics.get("request_pack")
    if not pack:
        return ""
    parts = ['<h3>Request pack cases</h3><p class="muted">Repeated, warmed workload. '
             'Quiet failures remain failures even when loaded requests pass. '
             'Unscored cases cannot establish full-pack correct capacity.</p>',
             '<div class="table"><table><tr><th>Case</th><th>Quiet result</th>'
             '<th>Loaded rate</th><th>Planned / offered / started</th>'
             '<th>Valid / scored / correct</th></tr>']
    for case in pack.get("cases", []):
        for stage in case.get("stages") or [{}]:
            planned = " / ".join(_e(stage.get(key, "—")) for key in ("planned", "offered", "started"))
            scored = " / ".join(_e(stage.get(key, "—")) for key in ("valid", "scored", "correct"))
            parts.append(f'<tr><td>{_e(case["case_id"])}</td>'
                         f'<td>{_badge(case.get("quiet", {}).get("status", "inconclusive"))}</td>'
                         f'<td>{_e(_rps(stage.get("rate_rps"))) if stage else "—"}</td>'
                         f'<td>{planned}</td><td>{scored}</td></tr>')
    parts.append('</table></div>')
    return "".join(parts)


def _traffic_table(check: CheckResult) -> str:
    pack = check.metrics.get("request_pack")
    quiet_failed = pack and pack.get("control_status") == "fail"
    goodput_label = ("Scored-subset correct goodput" if pack and not pack.get("all_cases_scored")
                     else "Correct goodput" if pack else "Cohort goodput")
    parts = [f'<h3>{_e(check.target)} — arrival traffic</h3>',
             '<p class="muted">Cohort goodput counts valid, correct answers meeting every configured '
             'SLO, including bounded drain completions, divided by the arrival window. '
             'Arrival latency includes local dispatch delay; service latency starts at dispatch. '
             'Neither measures internal server queue time.</p>',
             '<div class="table"><table><tr><th>Offered rate / outcome</th><th>Scheduled / sent</th>'
             '<th>Sent rate</th><th>Completed / failed / timed out</th>'
             '<th>Local / late / not offered</th>'
             + ('<th>Valid throughput</th>' if pack else '')
             + f'<th>{goodput_label} / good fraction</th>'
             '<th>Dispatch delay p95 ms</th><th>Service latency p95 ms</th>'
             '<th>Arrival first output / latency p95 ms</th></tr>']
    for stage in check.metrics["traffic_stages"]:
        def count(name: str) -> str:
            return _e(stage.get(name, "—"))
        outcome = (f'Loaded result: {_e(stage.get("status", "inconclusive"))}; quiet failed'
                   if quiet_failed else _badge(stage.get("status", "inconclusive")))
        parts.append(
            f'<tr><td>{_e(_rps(stage.get("rate_rps")))}<br>{outcome}'
            f'<br>{_e(" ".join(stage.get("reasons", [])))}</td>'
            f'<td>{count("scheduled")} / {count("started")}</td>'
            f'<td>{_e(_rps(stage.get("achieved_rps")))}</td>'
            f'<td>{count("completed")} / {count("failed")} / {count("timed_out")}</td>'
            f'<td>{count("dropped_local")} / {count("dropped_late")} / {count("not_offered")}</td>'
            + (f'<td>{_e(_rps(stage.get("valid_throughput_rps")))}</td>' if pack else '')
            + f'<td>{_e(_rps(stage.get("goodput_rps")))} / '
            f'{_number(stage["good_fraction"] * 100) if stage.get("good_fraction") is not None else "—"}%</td>'
            f'<td>{_number(stage.get("dispatch_lag_p95_ms"))}</td>'
            f'<td>{_number(stage.get("latency_p95_ms"))}</td>'
            f'<td>{_number(stage.get("arrival_first_output_p95_ms"))} / '
            f'{_number(stage.get("arrival_latency_p95_ms"))}</td></tr>'
        )
    parts.append('</table></div><details><summary>Arrival cohort windows and stage accounting</summary>'
                 f'<pre>{_json(check.metrics["traffic_stages"])}</pre></details>')
    return "".join(parts)


def _session_performance_html(data: dict[str, Any]) -> str:
    headline = (f'<p><strong>Average generation speed per user: '
                f'{_number(data["mean_user_generation_tokens_per_second"])} tok/s</strong> '
                f'({_e(data["users_with_measured_generation"])} measured users). '
                f'{_e(data["completed_conversations"])} / {_e(data["planned_conversations"])} '
                f'conversations completed; {_e(data["achieved_request_overlap"])} / '
                f'{_e(data["configured_concurrency"])} concurrent requests observed/configured.</p>'
                f'<p class="muted">{_e(data["interpretation"])}</p>')
    rows = []
    for row in data["turns"]:
        rows.append(f'<tr><td>{_e(row["task_type"])} / {_e(row["mode"])} / turn {_e(row["turn"])}</td>'
                    f'<td>{_e(row["completed"])} / {_e(row["attempted"])}</td>'
                    f'<td>{_number(row["generation_tokens_per_second_mean"])} / '
                    f'{_number(row["generation_tokens_per_second_p50"])} / '
                    f'{_number(row["generation_tokens_per_second_p10"])}</td>'
                    f'<td>{_number(row["first_output_p95_ms"])}</td>'
                    f'<td>{_number(row["p95_ms"])}</td>'
                    f'<td>{_e(row["unmeasured_generation_requests"])} / '
                    f'{_e(row["capped_responses"])}</td></tr>')
    return (headline + '<details><summary>Conversation performance by task and turn</summary>'
            '<div class="table"><table><tr><th>Task / mode / turn</th><th>Completed / sent</th>'
            '<th>Generation mean / median / p10 tok/s</th><th>First output p95 ms</th>'
            '<th>Response p95 ms</th><th>Unmeasured rate / capped replies</th></tr>'
            + ''.join(rows) + '</table></div></details>')


def _traffic_baseline_html(target: dict[str, Any]) -> str:
    traffic = target["traffic_capacity"]
    delta = traffic.get("delta_rps")
    delta_text = (f' Change: {delta:+.4g} requests/s ({traffic["change_percent"]:+.1f}%).'
                  if delta is not None else " No comparable capacity delta established.")
    parts = [f'<h3>{_e(target["target"])} — tested arrival capacity</h3>',
             f'<p>{_badge(traffic["status"])} {_e(traffic["reason"])}<br>'
             f'Highest acceptable tested rate: '
             f'{_e(_rps(traffic.get("baseline_highest_acceptable_rate_rps")))} → '
             f'{_e(_rps(traffic.get("current_highest_acceptable_rate_rps")))}.'
             f'{_e(delta_text)}</p>']
    if traffic["stages"]:
        parts.append('<div class="table"><table><tr><th>Offered rate</th><th>Baseline / current</th>'
                     '<th>Cohort goodput, baseline / current</th><th>Finding</th></tr>')
        for stage in traffic["stages"]:
            before, after = stage["baseline"], stage["current"]
            parts.append(
                f'<tr><td>{_e(_rps(stage["rate_rps"]))}</td>'
                f'<td>{_badge(before["status"])} / {_badge(after["status"])}</td>'
                f'<td>{_e(_rps(before.get("goodput_rps")))} / {_e(_rps(after.get("goodput_rps")))}</td>'
                f'<td>{_badge(stage["status"])} {_e(stage["reason"])} '
                f'{_e(stage.get("timing_comparison_reason", ""))}</td></tr>'
            )
        parts.append('</table></div>')
    return "".join(parts)


def _comparison_load_label(point):
    from .load_test import LOAD_TITLES

    shape, pattern = point.get("shape") or point.get("workload"), point.get("pattern")
    label = load_shape_label(shape) if shape in SHAPES else LOAD_TITLES.get(point.get("type"), str(shape))
    traffic = {"constant": "constant arrivals", "poisson": "Poisson arrivals", "bursts": "bursts",
               "interference": "short-answer comparison"}.get(pattern, str(pattern))
    if pattern == "concurrent":
        traffic = "maintained concurrency" + (f" {point['level']}" if point.get("level") is not None else "")
    label += " · " + traffic
    if point.get("rate") is not None:
        label += f" · {point['rate']:g} requests/second"
    if point.get("bursts") and pattern != "bursts":
        label += " + bursts"
    if point.get("source") and point["source"] != "primary":
        label += " · " + point["source"].replace("_", " ").capitalize()
    if pattern == "interference":
        phase = "Long-input probe" if point.get("role") == "probe" else {"baseline": "Short-only control", "probe": "During long-input probe"}.get(point.get("phase"))
        if phase:
            label += " · " + phase
    return label


def _load_baseline_html(target: dict[str, Any]) -> str:
    changes = {"gained": "Newly accepted", "lost": "Lost", "unchanged_accepted": "Still accepted",
               "accepted_baseline_not_established": "Accepted now; not established in baseline",
               "unchanged_not_accepted": "Still not accepted", "not_established_in_baseline": "Not established in baseline",
               "not_established_in_current": "Not established in current", "not_established": "Not established",
               "not_tested_in_baseline": "Not tested in baseline", "not_tested_in_current": "Not tested in current",
               "work_changed": "Requested work changed"}
    comparison = target.get("load_test", {"points": [], "status": "inconclusive", "reason": ""})
    def point_status(point):
        if point is None:
            return "Not tested"
        if point.get("not_run"):
            return "Not run"
        if point.get("cut_short"):
            return "Cut short"
        return point.get("status", "inconclusive")

    parts = [f'<h3>{_e(target["target"])} — tested load acceptance</h3>',
             f'<p>{_badge(comparison["status"])} {_e(comparison["reason"])}</p>']
    if target.get("load_exclusions"):
        parts.append('<h4>Load measurements not compared</h4><ul>')
        for exclusion in target["load_exclusions"]:
            label = _comparison_load_label(exclusion["load_point"]) if exclusion.get("load_point") else exclusion["scenario"]
            parts.append(f'<li>{_e(label)}: {_e(exclusion["reason"])}</li>')
        parts.append('</ul>')
    def scope(point):
        return tuple(point.get(name, "primary" if name == "source" else None)
                     for name in ("source", "type", "workload", "pattern", "bursts"))
    highest = [{scope(point): point for point in comparison.get(f"{side}_highest_accepted", [])}
               for side in ("baseline", "current")]
    scopes = {scope(point): point for row in comparison["points"]
              if (point := row["current"] or row["baseline"]) and point.get("pattern") in {"concurrent", "constant", "poisson"}}
    for key, point in {**highest[0], **highest[1]}.items():
        scopes.setdefault(key, point)
    for key, point in scopes.items():
        values = [str(points[key]["value"]) if key in points else "Not established" for points in highest]
        label = _comparison_load_label({**point, "shape": point.get("shape") or (point.get("workload") if point.get("type") == "combinations" else None), "level": None, "rate": None})
        unit = "concurrency" if point["pattern"] == "concurrent" else "request rate"
        parts.append(f'<p>{_e(label)} · Highest accepted {unit} (tested) · '
                     f'Baseline: {_e(values[0])} · Current: {_e(values[1])}</p>')
    if comparison["points"]:
        parts.append('<div class="table load-acceptance-export"><table><tr><th>Tested load</th>'
                     '<th>Baseline</th><th>Current</th><th>Change</th><th>Reason</th></tr>')
        for row in comparison["points"]:
            before, after = row["baseline"], row["current"]
            parts.append(f'<tr><td>{_e(_comparison_load_label(after or before))}</td>')
            for point in (before, after):
                evidence = []
                if point:
                    for field, label in (("valid", "valid"), ("dispatched", "dispatched"), ("required", "required")):
                        if point.get(field) is not None:
                            evidence.append(f'{point[field]} {label}')
                    for field, label in (("server_evidenced_concurrency", "server-evidenced concurrency"), ("client_mean_inflight", "mean client in flight")):
                        if point.get(field) is not None:
                            evidence.append(f'{label} {point[field]:g}')
                parts.append(f'<td>{_e(point_status(point).capitalize())}'
                             + (f'<small>{_e(" · ".join(evidence))}</small>' if evidence else '') + '</td>')
            parts.append(f'<td><strong>{_e(changes.get(row.get("acceptance_change"), "Not established"))}</strong></td>'
                         f'<td>{_e(row["reason"])}</td></tr>')
        parts.append('</table></div>')
    return "".join(parts)


def _comparison_outputs(target):
    if "observed_outputs" in target:
        return target["observed_outputs"]
    outputs, seen = [], set()
    for row in target.get("comparisons", []):
        key = row.get("check"), row.get("scenario")
        work = row.get("output_work")
        if key not in seen and work and work.get("current") and work.get("baseline"):
            seen.add(key)
            observations = _output_observations(*key, work["current"], work["baseline"])
            if row.get("load_point"):
                for observation in observations:
                    observation["load_point"] = row["load_point"]
            outputs.extend(observations)
    return outputs


def _comparison_values_html(target: dict[str, Any]) -> str:
    labels = {"correctness_rate": "Correct answers", "valid_completion_rate": "Completed valid responses",
              "error_rate": "Request errors", "latency_ms": "Full response time",
              "first_output_ms": "First useful output", "max_stream_gap_ms": "Longest pause per response",
              "output_tokens_per_second": "Output rate, including initial wait",
              "generation_tokens_per_second": "Generation pace", "output_tokens": "Answer length · tokens",
              "output_chars": "Answer length · characters"}

    def value(row, amount):
        if amount is None:
            return "Not measured"
        metric = row["metric"]
        if metric.endswith("_rate"):
            return f"{amount * 100:.1f}%"
        suffix = " ms" if metric.endswith("_ms") else " tok/s" if metric.endswith("tokens_per_second") else " tokens" if metric == "output_tokens" else " chars" if metric == "output_chars" else ""
        return _number(amount) + suffix

    parts = [f'<h3>{_e(target.get("target", ""))}</h3>']
    for reason in target.get("reasons", []):
        parts.append(f'<p class="card">Cannot compare: {_e(reason)}</p>')
    for row in [*target.get("comparisons", []), *_comparison_outputs(target)]:
        current, old = row.get("current"), row.get("baseline")
        if current is None or old is None:
            change = "Not measured"
        elif row["metric"].endswith("_rate"):
            change = f"{(current - old) * 100:+.1f} percentage points"
        elif current == old:
            change = "No change"
        elif old == 0:
            change = ("+" if current > old else "") + value(row, current - old) + " · from zero"
        else:
            change = f"{(current - old) * 100 / old:+.1f}%"
        warnings = [w["message"] for w in row.get("warnings", [])]
        if not warnings and row.get("status") == "inconclusive":
            warnings = [row.get("reason", "Insufficient evidence to interpret this change.")]
        label = "Observed result" if row.get("descriptive") else "Interpretation limited" if warnings else "Regression detected" if row.get("status") == "fail" else "No material regression detected"
        parts.append(f'<article class="card"><h4>{_e(labels.get(row["metric"], row["metric"]))}</h4>'
                     f'<p class="muted">{_e(row.get("check"))} · {_e(_comparison_load_label(row["load_point"]) if row.get("load_point") else row.get("scenario"))} · {_e(row.get("aggregation", "median"))}</p>'
                     '<dl class="comparison-values-export">'
                     f'<div><dt>Baseline</dt><dd>{value(row, old)}</dd><small>n={_e(row.get("baseline_samples"))}</small></div>'
                     f'<div><dt>Current</dt><dd>{value(row, current)}</dd><small>n={_e(row.get("current_samples"))}</small></div>'
                     f'<div><dt>Observed change</dt><dd>{change}</dd></div></dl>'
                     f'<strong>{label}</strong>')
        if warnings:
            parts.append('<ul>' + ''.join(f'<li>{_e(w)}</li>' for w in warnings) + '</ul>')
        elif row.get("descriptive"):
            parts.append('<p>Answer length is an observed result; shorter or longer is not automatically better.</p>')
        else:
            parts.append(f'<p>{_e(row.get("reason", ""))}</p>')
        parts.append('</article>')
    return ''.join(parts)


def _render(report: RunReport) -> str:
    from .performance import build_performance, refresh_performance
    from .performance_html import STYLE, render_performance

    report = refresh_performance(report.model_copy())
    evidence = {rid for check in report.checks if check.status in {"fail", "inconclusive", "blocked"}
                for rid in check.evidence_ids}
    ordered = sorted(report.requests, key=lambda r: (
        r.id not in evidence,
        not (r.score is False or r.status in {"failed", "timeout"} or r.evaluation_error)))
    detailed = ordered[:max(DEFAULT_HTML_REQUEST_DETAILS, sum(r.id in evidence for r in report.requests))]
    anchors = {record.id: f"request-{index}" for index, record in enumerate(detailed)}
    config = report.manifest.get("config", {})
    disconnected = {target["name"]: summary for target in config.get("targets", [])
                    if (summary := connection_summary(
                        [r for r in report.requests if r.target == target["name"]], target.get("url", target["name"])))}
    headline = ("; ".join(disconnected.values()) if disconnected and len(disconnected) == len(config.get("targets", []))
                else "Giraffe acceptance results")
    parts = ["<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">",
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f"<title>Giraffe — {_e(report.run_id)}</title>", '<style>' + STYLE + '.comparison-values-export{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.comparison-values-export dd{margin:4px 0;font-weight:600;overflow-wrap:anywhere}.comparison-values-export dt{font-size:12px}.load-acceptance-export{max-width:100%;overflow-x:auto}.load-acceptance-export table{min-width:760px}.load-acceptance-export th:nth-child(2),.load-acceptance-export th:nth-child(3),.load-acceptance-export td:nth-child(2),.load-acceptance-export td:nth-child(3){white-space:nowrap;min-width:140px}.load-acceptance-export small{display:block;white-space:normal}' + '</style><main>',  f"<h1>{_e(headline)} {_badge(_acceptance_status(report))}</h1>",
             f'<p class="muted">Run {_e(report.run_id)} · suite {_e(report.suite_version)}<br>'
             f'{_e(report.started_at)} → {_e(report.finished_at)}</p>']
    if headline == "Giraffe acceptance results":
        for summary in disconnected.values():
            parts.append(f'<p class="card"><strong>{_e(summary)}</strong></p>')
    if report.abort_reason:
        parts.append(f'<p class="card">Run stopped: {_e(report.abort_reason)}</p>')
    for target in config.get("targets", []):
        parts.append(f'<p class="muted"><strong>{_e(target.get("name", ""))}</strong> · '
                     f'{_e(target.get("route", "unknown"))} · Model: <code>{_e(target.get("model", ""))}</code>'
                     f'<br>Endpoint: <code>{_e(target.get("url", ""))}</code></p>')
    # Native radio navigation keeps the portable report keyboard-accessible
    # without scripts, external assets, or duplicating the aggregation layer.
    parts.append('<div class="html-tabs" role="group" aria-label="Report tabs">'
                 '<label><input id="html-tab-performance" name="report-tab" type="radio" '
                 'aria-controls="html-performance" checked>Performance</label>'
                 '<label><input id="html-tab-results" name="report-tab" type="radio" '
                 'aria-controls="html-results">Test results '
                 + str(len(report.checks)) + '</label><small>Measurement: default window per target · '
                 '<a href="report.json">JSON export</a></small></div>'
                 '<div id="html-performance" aria-label="Performance">')
    for entry in report.performance["available_targets"]:
        selected = (report.performance if entry["target"] == report.performance["selected_target"]
                    else build_performance(report, target=entry["target"]))
        parts.append(render_performance(selected, prefix="performance-" + str(len(parts))))
    if not report.performance["available_targets"]:
        parts.append(render_performance(report.performance))
    parts.append('</div><div id="html-results" aria-label="Test results">')
    parts.append('<p><a href="#checks">Checks</a> · <a href="#measurements">Measurements</a> · '
                 '<a href="#baseline">Baseline</a> · <a href="#requests">Request evidence</a> · '
                 '<a href="#reproduction">Reproduce</a></p>')
    storage = report.manifest.get("storage", {})
    if storage.get("response_bodies_omitted") or storage.get("limit_exceeded"):
        omitted = storage.get("response_bodies_omitted", 0)
        limit = f'{format_size(storage["limit_bytes"])} limit. ' if "limit_bytes" in storage else ""
        parts.append(f'<p class="card" role="status"><strong>Storage limit on disk:</strong> '
                     f'{limit}'
                     f'Response bodies omitted for {_e(omitted)} {"request" if omitted == 1 else "requests"}. '
                     f'{_e(storage.get("reason", ""))} Scores, request counts, timings and verdicts are unchanged.</p>')
    for reason in report.observations.get("target_abort_reasons", {}).values():
        if reason != report.abort_reason:
            parts.append(f'<p class="card">Target stopped: {_e(reason)}</p>')
    for check in report.checks:
        if check.id == "capacity":
            if check.target in disconnected:
                continue
            arrival = "traffic_stages" in check.metrics
            highest = check.metrics.get("highest_acceptable_rate_rps" if arrival
                                        else "highest_tested_acceptable_concurrency")
            value = _rps(highest) if arrival else str(highest) if highest is not None else "not established"
            measure = "arrival rate" if arrival else "concurrency"
            label = f'highest tested acceptable {measure}'
            if check.metrics.get("request_pack", {}).get("control_status") == "fail":
                label = "highest passing loaded rate (quiet failed)"
            parts.append(f'<p><strong>{_e(check.target)} — {label}: '
                         f'{_e(value)}</strong> {_badge(check.status)}<br>'
                         f'{_e(check.summary)} Maximum capacity is not established by this run.</p>')
            if arrival:
                parts.append(_traffic_table(check))
            parts.append(_pack_table(check))
        elif check.id == "correctness" and "capacity" not in config.get("checks", []):
            parts.append(_pack_table(check))
    missing = [f"{c.target} / {c.title}: {c.summary}" for c in report.checks
               if c.status in {"skipped", "inconclusive", "blocked"}]
    missing.extend(report.warnings)
    if missing:
        parts.append('<h2>Coverage and limitations</h2><ul>' +
                     "".join(f"<li>{_e(item)}</li>" for item in dict.fromkeys(missing)) + "</ul>")
    parts.append('<section id="checks"><h2>Checks</h2><div class="table"><table>'
                 '<tr><th>Target / check</th><th>Outcome</th><th>Finding and evidence</th></tr>')
    for check in report.checks:
        probability = check.metrics.get("logprobs")
        if probability:
            parts.append('<tr><td>Token probability consistency</td><td>' + _e(probability['status']) + '</td><td>'
                         + _e(probability['interpretation']) + '<br>Tolerance: ' + _e(probability['tolerance']) + '</td></tr>')
            for cohort in probability['conditions'] + probability['cross_conditions']:
                for example in cohort['examples']:
                    links = ' '.join(f'<a href="#{anchors[rid]}">{_e(rid)}</a>' for rid in example['request_ids'] if rid in anchors)
                    parts.append('<tr><td>' + _e(example['kind']) + '</td><td>Position ' + str(example['position'])
                                 + '</td><td>' + links + ' · absolute logprob difference: ' + _e(example['absolute_delta']) + '</td></tr>')
        evidence = " ".join(f'<a href="#{anchors[rid]}">{_e(rid)}</a>'
                            for rid in check.evidence_ids if rid in anchors)
        metrics = (f'<details><summary>Measurements</summary><pre>{_json(check.metrics)}</pre></details>'
                   if check.metrics else "")
        if check.id == "load":
            metrics = _load_test_html(check.metrics) + metrics
        if check.id == "concurrency" and "load_types" in check.metrics:
            metrics = _concurrent_load_html(check.metrics) + metrics
        if check.id == "sessions" and check.metrics.get("conversation_performance"):
            metrics = _session_performance_html(check.metrics["conversation_performance"]) + metrics
        parts.append(f'<tr><td>{_e(check.target)}<br><strong>{_e(check.title)}</strong>'
                     f'<br>{"Required" if check.required else "Optional"}</td>'
                     f'<td>{_badge(check.status)}</td><td>{_e(check.summary)}'
                     f'{metrics}<div>{evidence}</div></td></tr>')
        for outcome in check.metrics.get("cancellation_results", {}).values():
            parts.append(f'<tr><td>{_e(check.target)}<br>{_e(outcome["title"])}</td>'
                         f'<td>{_badge(outcome["status"])}</td>'
                         f'<td>{_e(outcome["summary"])}</td></tr>')
        for scenario in check.metrics.get("scenarios", {}).values():
            parts.append(f'<tr><td>{_e(check.target)}<br><strong>{_e(scenario["title"])}</strong></td>'
                         f'<td>{_badge(scenario["status"])}</td><td>{_e(scenario["summary"])}'
                         f'<details><summary>Scenario measurements and evidence</summary>'
                         f'<pre>{_json(scenario)}</pre></details></td></tr>')
            for case in scenario.get("metrics", {}).get("cases", {}).values():
                parts.append(f'<tr><td>{_e(case["title"])}</td><td>{_badge(case["status"])}</td>'
                             f'<td>Requested input/output tokens: {_e(case["requested_input_tokens"])} / '
                             f'{_e(case["requested_output_tokens"])}. '
                             f'Observed input: {_e(case["actual_input_tokens"])}; '
                             f'output: {_e(case["actual_output_tokens"])}. '
                             f'{_e("; ".join(case["coverage_gaps"]))}</td></tr>')
    # A partially selected suite must not look like complete coverage.
    seen = {check.id for check in report.checks}
    absent = [title for check_id, title in CHECK_NAMES.items() if check_id not in seen]
    parts.append("</table></div>")
    if absent:
        parts.append(f'<p class="muted">Not included in this run: {_e(", ".join(absent))}.</p>')
    parts.append('</section><section id="measurements"><h2>Measurements by scenario</h2>'
                 '<p class="muted">Initial requests are separated from warm and loaded requests. '
                 'Latency percentiles include valid completed requests only; failures and timeouts '
                 'are counted alongside them. Small samples do not establish tail latency.</p>'
                 '<div class="table"><table><tr><th>Target / scenario</th><th>Requests</th>'
                 '<th>Valid</th><th>Errors / timeouts</th><th>Cancelled</th><th>Latency p50 / p95 ms</th>'
                 '<th>First output p50 ms</th><th>Generation pace p50 tok/s (estimate)</th>'
                 '<th>End-to-end output p50 tok/s</th><th>Scored correct</th></tr>')
    groups: dict[tuple[str, str], list[RequestRecord]] = defaultdict(list)
    for record in report.requests:
        groups[(record.target, record.scenario)].append(record)
    for (target, scenario), records in groups.items():
        valid = [r for r in records if r.valid and r.status == "completed"]
        times = [r.elapsed_ms for r in valid]
        first = [r.first_output_ms for r in valid if r.first_output_ms is not None]
        generation = [rate for r in valid if (rate := r.generation_tokens_per_second) is not None]
        end_to_end = [rate for r in valid if (rate := _rate(r)) is not None]
        scored = [r for r in records if r.score is not None]
        parts.append(f'<tr><td>{_e(target)} / {_e(scenario)}</td><td>{len(records)}</td><td>{len(valid)}</td>'
                     f'<td>{sum(r.status == "failed" for r in records)} / '
                     f'{sum(r.status == "timeout" for r in records)}</td>'
                     f'<td>{sum(r.status == "cancelled" for r in records)}</td>'
                     f'<td>{_number(_percentile(times, .5))} / {_number(_percentile(times, .95))}</td>'
                     f'<td>{_number(_percentile(first, .5))}</td>'
                     f'<td>{_number(_percentile(generation, .5))} (n={len(generation)})</td>'
                     f'<td>{_number(_percentile(end_to_end, .5))}</td>'
                     f'<td>{sum(r.score is True for r in scored)} / {len(scored)}</td></tr>')
    parts.append('</table></div></section><section id="baseline"><h2>Baseline comparison</h2>')
    if report.baseline:
        acceptance = report.baseline.get("acceptance", {})
        parts.append('<h3>What changed?</h3><p>Observed differences do not prove what caused them.</p>'
                     f'<p>Compared with {_e(report.baseline.get("saved_baseline_name") or report.baseline.get("run_id", "unknown"))}.</p>'
                     '<h3>Did each run meet its requirements?</h3>'
                     f'<p>Baseline: {_e(acceptance.get("baseline", "Not recorded separately"))} · '
                     f'Current: {_e(acceptance.get("current", "Not recorded separately"))}. '
                     'Pass/fail checks configured requirements; measured changes appear below.</p>')
        for target in report.baseline.get("targets", []):
            parts.append(_comparison_values_html(target))
            if "traffic_capacity" in target:
                parts.append(_traffic_baseline_html(target))
            if "load_test" in target or target.get("load_exclusions"):
                parts.append(_load_baseline_html(target))
        parts.append(f'<details><summary>Changes, comparability and metric evidence</summary>'
                     f'<pre>{_json(report.baseline)}</pre></details>')
    else:
        parts.append('<p>No baseline supplied. This run reports absolute checks only.</p>')
    parts.append('<section id="deployment"><h2>Deployment and serving telemetry</h2>'
                 '<p>Deployment changes provide investigation context and do not establish causality. '
                 'Unknown fields and telemetry remain unknown. Client first output is separate from server TTFT.</p>')
    for name, observation in report.observations.get("targets", {}).items():
        parts.append(f'<h3>{_e(name)}</h3><details><summary>Deployment snapshot and unknowns</summary>'
                     f'<pre>{_json(observation.get("deployment"))}</pre></details>'
                     f'<details><summary>Deployment observations during the run</summary>'
                     f'<pre>{_json(observation.get("deployment_history", []))}</pre></details>'
                     f'<details><summary>Serving telemetry, phases and collection coverage</summary>'
                     f'<pre>{_json(observation.get("serving_telemetry"))}</pre></details>')
    parts.append('</section>')
    parts.append('</section><section id="requests"><h2>Request evidence</h2>'
                 f'<p class="muted">Response retention: {_e(config.get("retention", "failures"))}. '
                 'Retention controls response text; request metadata, timings, status and scores are always saved. '
                 'Retention does not cap disk usage; storage limits do.</p>'
                 f'<p>Showing {len(detailed)} of {len(report.requests)} request details, failures first. '
                 '<a href="report.json">All requests and complete retained bodies in report.json</a>.</p>')
    input_previews = {}
    for record in detailed:
        label = f"{record.target} · {record.scenario} · {record.fixture_id} · {record.status}"
        parts.append(f'<details id="{anchors[record.id]}"><summary>{_e(label)}'
                     f' · {_number(record.elapsed_ms)} ms</summary>'
                     f'<p>Request {_e(record.id)} · scored: {_e(record.score)} · '
                     f'valid completion: {_e(record.valid)} · output characters: {record.output_chars}</p>')
        if record.error or record.score_message:
            parts.append(f'<p>{_e(record.error or "")} {_e(record.score_message or "")}</p>')
        if record.evaluation:
            evidence = record.evaluation
            messages_key = sha256(json.dumps(evidence.messages, ensure_ascii=False, sort_keys=True).encode()).digest()
            if messages_key in input_previews:
                parts.append('<h3>Input messages</h3><p>Same recorded input as '
                             f'<a href="#{input_previews[messages_key]}">earlier request evidence</a>.</p>')
            else:
                input_previews[messages_key] = anchors[record.id]
                parts.append(f'<h3>Input messages</h3><pre>{_json(evidence.messages, MAX_HTML_PREVIEW_CHARS)}</pre>')
            parts.append(f'<h3>Answer check</h3><p>{_e(evidence.scorer)}</p>')
            if evidence.scorer == "json":
                parts.append('<p>JSON accepts one complete Markdown code fence; surrounding prose is rejected. '
                             'Expected values are compared structurally.</p>')
            if evidence.tool_expectation is None:
                if evidence.scorer == "none":
                    purpose = evidence.expected.get("purpose") if isinstance(evidence.expected, dict) else None
                    if isinstance(purpose, str) and purpose:
                        parts.append(f'<h3>Purpose</h3><p>{_preview(purpose)}</p>')
                elif evidence.expected_provided:
                    expected = _json(evidence.expected) if evidence.scorer == "json" else _preview(evidence.expected)
                    parts.append(f'<h3>Expected answer</h3><pre>{expected}</pre>')
                else:
                    parts.append('<p>No expected answer configured.</p>')
            if evidence.rubric:
                parts.append(f'<h3>Judge rubric</h3><pre>{_preview(evidence.rubric)}</pre>')
            if evidence.schema_ is not None:
                parts.append(f'<h3>JSON schema</h3><pre>{_json(evidence.schema_)}</pre>')
        if record.logprob_evidence:
            probability = record.logprob_evidence.model_dump()
            for token in probability["tokens"]:
                token.pop("bytes")
                for alternative in token["top_logprobs"]:
                    alternative.pop("bytes")
            parts.append('<h3>Token log probabilities</h3>'
                         '<p>Token text, probabilities and alternatives. '
                         '<a href="report.json">Complete evidence including token bytes in report.json</a>.</p>'
                         '<pre>' + _json(probability) + '</pre>')
        parts.append(_tool_evidence(record))
        parts.append(_judgement_table(record))
        if record.output:
            parts.append(f'<h3>Raw response</h3><pre>{_preview(record.output)}</pre>')
        else:
            parts.append('<p class="muted">No retained response body.</p>')
        if record.reasoning:
            parts.append(f'<h3>Reasoning response</h3><pre>{_preview(record.reasoning)}</pre>')
        parts.append(f'<details><summary>Local fixture and measured request evidence</summary><pre>'
                     f'{_json(record.model_dump(exclude_defaults=True, exclude={"output", "reasoning", "evaluation", "logprob_evidence", "tool_calls", "tool_evaluation", "tool_request_options", "tool_conversation", "judgement"}))}</pre></details></details>')
    parts.append('</section><section id="reproduction"><h2>Reproduce this run</h2>'
                 '<p>Use the recorded configuration with the same suite and fixture pack. '
                 'Environment variable names are references; supply their values locally.</p>'
                 f'<details><summary>Configuration, fixture pack and run location</summary>'
                 f'<pre>{_json(report.manifest)}</pre></details>')
    if report.observations:
        parts.append(f'<details><summary>Additional observations</summary><pre>'
                     f'{_json(report.observations)}</pre></details>')
    parts.append('</section></div></main></html>')
    return "".join(parts)


def _atomic_write_chunks(path: Path, chunks) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            for chunk in chunks:
                file.write(chunk)
            file.flush()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write(path: Path, contents: str) -> None:
    _atomic_write_chunks(path, (contents,))


class ReportWriteError(OSError):
    def __init__(self, error: OSError, failed_file: str, *, json_complete: bool):
        super().__init__(error.errno, error.strerror, error.filename)
        self.failed_file = failed_file
        self.json_complete = json_complete


def _write_failure_reason(error: OSError) -> str:
    return {errno.ENOSPC: "disk full", errno.EDQUOT: "disk quota exceeded",
            errno.EROFS: "read-only disk", errno.EACCES: "permission denied",
            errno.EPERM: "permission denied"}.get(error.errno, "disk write error")


def html_failure_warning(error: OSError) -> str:
    return f"HTML report could not be saved: {_write_failure_reason(error)}; report.json is complete"


def write_comparison_report(report: RunReport, directory: Path, *, max_run_storage: int) -> None:
    """Replace a saved report atomically without reapplying response retention.

    Baseline snapshots may share the original inode. Atomic replacement preserves
    those snapshots, and a comparison never discards existing request evidence.
    """
    if max_run_storage and _json_size(report) > max_run_storage:
        raise OSError(errno.ENOSPC, "Comparison exceeds the per-run storage limit")
    directory = Path(directory)
    _atomic_write_chunks(directory / "report.json", _report_json_chunks(report))
    html_path = directory / "report.html"
    if html_path.exists():
        try:
            _atomic_write(html_path, _render(report))
        except OSError as error:
            try:
                html_path.unlink(missing_ok=True)
            finally:
                raise ReportWriteError(error, "report.html", json_complete=True) from error


def write_report(report: RunReport, directory: Path, *, max_run_storage: int = DEFAULT_LIMITS["max_run_storage"],
                 html: bool = False) -> tuple[Path, Path | None]:
    """Save compact JSON; standalone HTML is an explicit optional export."""
    if isinstance(max_run_storage, bool) or not isinstance(max_run_storage, int) or max_run_storage < 0:
        raise ValueError("max_run_storage must be a non-negative integer")
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReportWriteError(error, "report.json", json_complete=False) from error
    retained = _retained(report)
    json_path, html_path = directory / "report.json", directory / "report.html"
    from .latency import latency_summary

    metadata_path = directory / ".web.json"
    try:
        try:
            metadata = json.loads(metadata_path.read_text())
            if not isinstance(metadata, dict):
                metadata = {}
        except (OSError, ValueError):
            metadata = {}
        metadata["latency_summary"] = latency_summary(report)
        _atomic_write(metadata_path, json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        reason = _write_failure_reason(error) if isinstance(error, OSError) else type(error).__name__
        warning = f"Latency summary could not be saved: {reason}"
        report.warnings.append(warning)
        if retained.warnings is not report.warnings:
            retained.warnings.append(warning)
    _storage_limited(retained, max_run_storage)
    try:
        _atomic_write_chunks(json_path, _report_json_chunks(retained))
    except OSError as error:
        raise ReportWriteError(error, "report.json", json_complete=False) from error
    if not html:
        return json_path, None
    try:
        _atomic_write(html_path, _render(retained))
    except OSError as error:
        for path in (html_path, html_path.with_name("report.html.tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ReportWriteError(error, "report.html", json_complete=True) from error
    return json_path, html_path


def save_failure_summary(report: RunReport, directory: Path, error: OSError) -> str:
    if getattr(error, "json_complete", False):
        for name in ("report.html", "report.html.tmp"):
            try:
                (Path(directory) / name).unlink(missing_ok=True)
            except OSError:
                pass
        return html_failure_warning(error)
    reason = _write_failure_reason(error)
    prefix = (f"Report could not be saved ({reason}). Request evidence for "
              f"{len(report.requests)} requests was lost; ")
    directory = Path(directory)
    for name in ("report.json", "report.html"):
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            pass
    message = prefix + "a summary was saved."
    summary = {"run_id": report.run_id, "overall": report.overall, "state": "error",
               "started_at": report.started_at, "finished_at": report.finished_at,
               "request_count": len(report.requests), "summary_saved": True, "error": message,
               "checks": [{"id": check.id, "target": check.target, "title": check.title[:MAX_FAILURE_SUMMARY_TITLE_CHARS],
                           "status": check.status, "summary": (
                               text if len(text := _without_answer_excerpts(check.summary)) <= MAX_FAILURE_SUMMARY_CHARS
                               else text[:MAX_FAILURE_SUMMARY_CHARS] + " … [truncated]")}
                          for check in report.checks]}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / "summary.json", json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    except OSError:
        return prefix + "no summary could be saved."
    return message


def reproduction_export(report: RunReport) -> dict:
    from .invocation import safe_invocation
    retained = _retained(report)
    return {"schema_version": "1", "source_run_id": report.run_id,
            "source_outcome": report.overall,
            "invocation": safe_invocation(retained.manifest.get("invocation")),
            "config": retained.manifest.get("config", {}),
            "fixture_pack": retained.manifest.get("fixture_pack", {}),
            "deployments": {name: obs.get("deployment") for name, obs in retained.observations.get("targets", {}).items()},
            "deployment_history": {name: obs.get("deployment_history", []) for name, obs in retained.observations.get("targets", {}).items()},
            "instructions": ["Supply secret environment-variable references locally.",
                "Match suite and fixture hashes; supply any local fixture/metadata files.",
                "Verify unknown server settings manually. This export cannot recreate inaccessible server state.",
                "Imported launch arguments describe intent and are never executed by this export.",
                "A saved failed run is a reference, not an approved deployment."]}
