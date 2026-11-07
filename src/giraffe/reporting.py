"""Compare like-for-like runs and write a portable, local results document."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import errno
from fractions import Fraction
from hashlib import sha256
from html import escape
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import shutil
import tempfile
from typing import Any

from pydantic import ValidationError

from .caps import (MAX_HTML_PREVIEW_CHARS, MAX_HTML_JSON_PREVIEW_CHARS,
                   DEFAULT_HTML_REQUEST_DETAILS, MAX_FAILURE_SUMMARY_TITLE_CHARS,
                   MAX_FAILURE_SUMMARY_CHARS)
from .deployment import deployment_diff, fixture_hash, safe_config, sanitize
from .report_inputs import input_ref as _input_ref
from .multi_user_metrics import (_error as _user_error, _length_mix_differs, _total_generated_tokens,
                                 _uncached_input_tokens, _valid as _user_valid_completion,
                                 truncated_conversation_turn, user_delivery_completions, user_latency)
from .models import (CHECK_NAMES, CORE_CHECKS, SHAPES, CheckResult, RequestRecord, RunConfig, RunReport, overall_status, load_shape_label,
                     migrate_concurrent_load, migrate_run_limits, is_supported_schema, CURRENT_SCHEMA)
from .stats import median, percentile
from .storage import DEFAULT_LIMITS, format_size, fsync_directory, update_metadata, write_metadata
from .workloads import WORKLOAD_CHECKS
from .timing import TIMING_METRICS, qualify, threshold as timing_threshold

logger = logging.getLogger(__name__)


# Identities are deliberately absent: changing a runtime/image is a reason to run the suite.
_MATCH_CONFIG = (
    "request_timeout_seconds",
    "max_output_tokens", "context_limit", "samples", "sustained_seconds", "checks",
    "structured_json", "overlap_models", "stream", "request_options", "stop_after_errors",
    "unresponsive_after_failures", "unresponsive_for_seconds",
    "limits",
)


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
                if check == "multi_user":
                    if (record.intentional or record.workload.get("phase") != "measurement"
                            or not record.workload.get("user_id")):
                        continue
                    scenario = " / ".join(str(record.workload.get(key, "unknown"))
                        for key in ("scenario", "population", "profile", "user_id"))
                    if record.workload.get("role"):
                        scenario += " / " + " / ".join(str(record.workload.get(key) or "")
                            for key in ("role", "trigger", "heavy_profile"))
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
        for field in ("first_output_ms", "stream_gap_ms"):
            config["limits"].setdefault(field, None)
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
    for check, field in (("first_output", "first_output_ms"), ("generation", "stream_gap_ms")):
        settings = value.get("effective_checks", {}).get(check)
        if isinstance(settings, dict) and isinstance(settings.get("limits"), dict):
            supplied = (config.get("test_options", {}).get(check) or {}).get("limits") or {}
            threshold = supplied[field] if field in supplied else (config.get("limits") or {}).get(field)
            value["effective_checks"][check] = {**settings, "limits": {**settings["limits"], field: threshold}}
    if "capacity" in resolved.checks:
        value.setdefault("effective_traffic", resolved.effective_traffic().model_dump(mode="json"))
    return value


_CONVERSATION_REVISIONS = {"persistent_users", "shared_conversations"}
_CONVERSATION_BRIDGE_CHECKS = {"prefix", "sessions", "consistency"}


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
    if revision in {"issue45", "issue95", "issue95_pauses", "issue95_pause_evidence", "persistent_users", "shared_conversations"}:
        unchanged_checks = {"sessions", "consistency"}
        if revision in {"issue95_pause_evidence", "persistent_users", "shared_conversations"}:
            # User and conversation wiring changed the shared file, but prefix
            # requests are unchanged across these exact generator revisions.
            unchanged_checks.add("prefix")
        if revision in _CONVERSATION_REVISIONS:
            # Core and Tool calling dispatch are unchanged in this refactor.
            # Load also has a separate source digest below.
            unchanged_checks.update(CORE_CHECKS, {"tools"})
        if not set(resolved.checks) <= unchanged_checks:
            return None  # Load generators changed; attest unchanged specialists only.
    sampled_sessions = "sessions" in resolved.checks and resolved.sessions and resolved.sessions.sampling is not None
    if sampled_sessions and revision not in {"persistent_users", "shared_conversations"}:
        return None  # Earlier revisions have no attested sampled-traffic equivalent.
    settings = {}
    for field, checks in WORKLOAD_CHECKS.items():
        if not checks.intersection(resolved.checks):
            continue
        if field not in raw:
            return None  # A fingerprint cannot attest settings missing from the saved config.
        value = raw[field] if legacy else getattr(resolved, field)
        settings[field] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        if revision == "issue45" and field == "sessions" and isinstance(settings[field], dict):
            settings[field].pop("sampling", None)
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
            "issue95": "ebc8dc0b150701e1a97f571a6ea7a37cdcf62f4d3d9db97244d8c2bd5c17b092",
            "issue95_pauses": "37bf5c074bfdb19f367e5348b678e5a923977fe7bf013b8c781a43bb8efca19e",
            "issue95_pause_evidence": "ab5875f7c635f8f454429a88aac87d52ce3a141a84bcf7b936fa737ffa41fe83",
            "persistent_users": "aee9ca71bc5ffd780d1574422b761e3177471a12f951a10b04322573ca062f76",
            "shared_conversations": "d9a9e921584262148e06b636f0e8f195b7e530fd2a69c5e98f013359cf947ede",
        }[revision] if any(settings.values()) else None,
        "settings": settings,
    }
    if "load" in resolved.checks and revision in _CONVERSATION_REVISIONS:
        identity["load_test_source_sha256"] = "9b694c97a39d449d8ea15b09a9aeb406bf835179c0882827685c4d7888687d36"
    if sampled_sessions:
        identity["session_sampling_source_sha256"] = {
            "persistent_users": "2fab1370faeab7ddb91a7ae9c0e49520ee9a67e305441a0663877bb12bca24c3",
            "shared_conversations": "d8d498b51cbb006145e0448f685e431f4f5befd2a6907746987d0934f055a3f4",
        }[revision]
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
            "issue95": "d51d49b2716b3aa1f50c3b1e689cdc903399b9b7862bce59525194e44fea402e",
            "issue95_pauses": "d51d49b2716b3aa1f50c3b1e689cdc903399b9b7862bce59525194e44fea402e",
            "issue95_pause_evidence": "d51d49b2716b3aa1f50c3b1e689cdc903399b9b7862bce59525194e44fea402e",
            "persistent_users": "d51d49b2716b3aa1f50c3b1e689cdc903399b9b7862bce59525194e44fea402e",
            "shared_conversations": "d51d49b2716b3aa1f50c3b1e689cdc903399b9b7862bce59525194e44fea402e",
        }[revision]
    if "tools" in resolved.checks:
        from .tool_checks import resolved_tool_fixtures, resolved_tool_modes
        tool_conversation_revisions = _CONVERSATION_REVISIONS | {"phase3", "review26", "caps26"}
        identity["resolved_tool_checks"] = {
            "fixtures": [fixture.model_dump(mode="json") for fixture in resolved_tool_fixtures(resolved)],
            "modes": resolved_tool_modes(resolved),
            **({"conversations": [fixture.model_dump(mode="json") for fixture in resolved.tool_checks.conversations],
                "conversation_source_sha256": ("4080132330b5ceb3d7e0b824bb26244a73967d81ae6b467441600e45c483eb48" if revision in tool_conversation_revisions else
                                               "283d1eb18bb852ca0132a2d4a8c63e49bc7155614603e74740dd80afe8189714")}
               if resolved.tool_checks and resolved.tool_checks.conversations else {}),
        }
    return identity


def _same_run_limits_workload(current: dict, old: dict) -> bool:
    if current.get("config", {}).get("config_version") != 2 or old.get("config", {}).get("config_version", 2) != 2:
        return False
    matched_revisions = []
    for manifest in (current, old):
        revisions = ("phase2", "phase3", "review26", "caps26", "issue45", "issue95", "issue95_pauses", "issue95_pause_evidence", "persistent_users", "shared_conversations") if "config_version" in manifest.get("config", {}) else ("legacy",)
        fingerprint = manifest.get("fixture_pack", {}).get("workload_fixture_sha256")
        matched = {revision for revision in revisions
                   if (identity := _run_limits_workload_identity(manifest, revision=revision)) is not None
                   and fixture_hash(identity) == fingerprint}
        if not matched:
            return False
        matched_revisions.append(matched)
    current_config = _comparison_manifest(current)["config"]
    old_config = _comparison_manifest(old)["config"]
    selected = set(current_config.get("checks", [])) | set(old_config.get("checks", []))
    if not selected <= _CONVERSATION_BRIDGE_CHECKS:
        # Newly attested companion checks must match the exact conversation
        # revisions on both reports. Earlier bridges keep their existing scope.
        if not (all(matches & _CONVERSATION_REVISIONS for matches in matched_revisions)
                or all(matches - _CONVERSATION_REVISIONS for matches in matched_revisions)):
            return False
    return all(current_config.get(field) == old_config.get(field)
               for field, checks in WORKLOAD_CHECKS.items() if checks & selected)


def _same_load_workload(current: dict, old: dict) -> bool:
    """Attest the optional nonce argument only for unchanged ordinary Load runs."""
    settings = []
    for manifest in (current, old):
        raw = manifest.get("config", {})
        if raw.get("config_version") != 2 or "load_test" not in raw:
            return False
        try:
            resolved = RunConfig.model_validate(raw)
        except ValidationError:
            return False
        fields = {field for field, checks in WORKLOAD_CHECKS.items() if checks.intersection(resolved.checks)}
        if fields != {"load_test"}:
            return False
        load_settings = resolved.load_test.model_dump(mode="json")
        identity = {"generator_source_sha256": "d9a9e921584262148e06b636f0e8f195b7e530fd2a69c5e98f013359cf947ede",
                    "settings": {"load_test": load_settings}}
        if not any(fixture_hash({**identity, "load_test_source_sha256": source}) ==
                   manifest.get("fixture_pack", {}).get("workload_fixture_sha256") for source in (
                       "9b694c97a39d449d8ea15b09a9aeb406bf835179c0882827685c4d7888687d36",
                       "df3b7bbf30cab87d2e8c1c6dab8819f9cfe592b181407caa7c32af4c5f720cd4")):
            return False
        settings.append(load_settings)
    return settings[0] == settings[1]


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


def _settings_reasons(report: RunReport, baseline: RunReport, *, details: dict[str, dict[str, Any]] | None = None) -> list[str]:
    reasons = []

    def setting_reason(text, code, key, current=None, previous=None):
        reasons.append(text)
        if details is not None:
            details[text] = {"code": code, "key": key, "current": current,
                             "baseline": previous, "text": text}
    if not is_supported_schema(report.schema_version) or not is_supported_schema(baseline.schema_version):
        reasons.append("Report schema versions differ.")
    if report.manifest.get("timing_measurement_version", 1) != baseline.manifest.get("timing_measurement_version", 1):
        reasons.append("Client timing measurement methods differ; rerun the baseline.")
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
            setting_reason(f"Run setting {key} differs and its limit stopped work or constrained concurrency.",
                           "limit_constrained", key, current_config.get(key), old_config.get(key))
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
    if "multi_user" in selected:
        policies = [{check.target: (check.metrics.get("multi_user", {}).get("version"),
                                   check.metrics.get("multi_user", {}).get("window_policy"))
                     for check in run.checks if check.id == "multi_user"}
                    for run in (report, baseline)]
        if (policies[0] != policies[1] or not policies[0]
                or any(None in policy for policies_by_target in policies for policy in policies_by_target.values())):
            reasons.append("Multi-user summary or window policy differs or is missing; rerun a matching baseline.")
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
            setting_reason(f"Recorded {key} is missing; comparability is unknown.",
                           "setting_missing", key)
        elif old_config[key] != current_config[key]:
            setting_reason(f"Run setting {key} differs.", "setting_differs", key,
                           current_config[key], old_config[key])
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
                setting_reason(f"Recorded {key} is missing; comparability is unknown.",
                               "setting_missing", key)
            elif current_config[key] != old_config[key]:
                setting_reason(f"Run setting {key} differs.", "setting_differs", key,
                               current_config[key], old_config[key])
    for key in ("effective_checks", "effective_traffic"):
        if key not in current_manifest and key not in old_manifest:
            continue
        if key == "effective_checks" and ((key in report.manifest) != (key in baseline.manifest)):
            missing = baseline.manifest if key in report.manifest else report.manifest
            if "config_version" in missing.get("config", {}):
                setting_reason(f"Recorded {key} differs or is missing.", "setting_unknown", key)
                continue
        if key == "effective_traffic" and not traffic_selected:
            continue
        if key in current_manifest or key in old_manifest:
            if effective_value(current_manifest, key) != effective_value(old_manifest, key):
                setting_reason(f"Recorded {key} differs or is missing.", "setting_unknown", key)
    # Fixture-set identity is independent of the installed package version.
    current_pack = report.manifest.get("fixture_pack")
    old_pack = baseline.manifest.get("fixture_pack")
    if not isinstance(current_pack, dict) or not isinstance(old_pack, dict) or not current_pack or not old_pack:
        reasons.append("Fixture pack identity is missing or unknown.")
    else:
        inactive = {"custom"}
        if (_same_run_limits_workload(report.manifest, baseline.manifest)
                or _same_load_workload(report.manifest, baseline.manifest)):
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
            setting_reason(f"Recorded {key} is missing; comparability is unknown.", "setting_missing", key)
        else:
            current, old = report.manifest[key], baseline.manifest[key]
            if key == "load_shape" and "max_requests" not in reached:
                current, old = ({name: setting for name, setting in shape.items()
                                 if name != "target_request_quotas"} if isinstance(shape, dict) else shape
                                for shape in (current, old))
            if current != old:
                setting_reason(f"{key} differs.", "setting_differs", key, current, old)
    return reasons


def _generator_saturated(report: RunReport, target: str) -> bool:
    records = [r for r in report.requests if r.target == target]
    return _timing_quality(report, target, records, TIMING_METRICS)["status"] == "client_delayed"


def _timing_quality(report, target, records, metrics):
    records = [record.model_copy(update={"scheduled_ms": record.workload.get("ready_ms")})
               if "multi_user" in record.check_ids else record for record in records]
    return qualify(records, report.observations.get("client_timing_health"),
                   timing_threshold([r for r in report.requests if r.target == target]), metrics,
                   legacy_max_lag_ms=report.observations.get("generator_max_lag_ms", 0))


def _valid_completion(record: RequestRecord, check: str | None) -> bool:
    if check == "multi_user" and record.workload.get("session_id"):
        return _user_valid_completion(record)
    return record.status == "completed" and record.valid


def _metric_values(records: list[RequestRecord], *, check: str | None = None,
                   timing_records: dict[str, list[RequestRecord]] | None = None) -> dict[str, list[float]]:
    valid = [_valid_completion(record, check) for record in records]
    completed = [record for record, success in zip(records, valid) if success]
    def clock_value(record, arrival_field, dispatch_field):
        if "multi_user" in record.check_ids:
            return user_latency(record, first=dispatch_field == "first_output_ms")
        arrival = (record.load_point or {}).get("pattern") in {"constant", "poisson", "bursts"}
        return getattr(record, arrival_field if arrival else dispatch_field)

    def stream_gap(record):
        if check == "multi_user" and (record.output_tokens == 0 or record.completion_tokens == 0):
            return None
        return record.max_stream_gap_ms

    def timing_values(name, measure):
        values = []
        cohort = []
        for record in completed:
            if (value := measure(record)) is not None:
                values.append(value)
                if timing_records is not None:
                    cohort.append(record)
        if timing_records is not None:
            timing_records[name] = cohort
        return values

    timing = {
        "latency_ms": lambda r: clock_value(r, "arrival_elapsed_ms", "elapsed_ms"),
        "first_output_ms": lambda r: clock_value(r, "arrival_first_output_ms", "first_output_ms"),
        "max_stream_gap_ms": stream_gap,
        "output_tokens_per_second": lambda r: _rate(r) if "multi_user" not in r.check_ids else None,
        "generation_tokens_per_second": lambda r: r.generation_tokens_per_second,
    }
    return {
        "valid_completion_rate": [float(success) for success in valid],
        "error_rate": [float(_user_error(r) if check == "multi_user" and r.workload.get("session_id")
                             else r.status in {"failed", "timeout"}) for r in records],
        "correctness_rate": [float(r.score) for r in records if r.score is not None],
        **{name: timing_values(name, measure) for name, measure in timing.items()},
    }


def _output_work(records: list[RequestRecord], *, check: str | None = None) -> dict[str, Any]:
    completed = [r for r in records if _valid_completion(r, check)]
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
    low, high = percentile(bootstrap, 0.025), percentile(bootstrap, 0.975)
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


def _input_work_changed(current, old, threshold):
    current_input = median(r.input_tokens for r in current if r.valid)
    old_input = median(r.input_tokens for r in old if r.valid)
    return (current_input is not None and old_input is not None and old_input > 0
            and abs(current_input - old_input)*100/old_input > threshold)


def _live_input_work_changed(current, old, threshold, *, compare_output=False):
    """Match generated history at each repeated conversation's logical turn."""
    def output_work(record):
        return record.output_tokens, _total_generated_tokens(record), record.output_chars

    histories = []
    for records in (current, old):
        history = {}
        for record in records:
            if record.intentional or record.workload.get("mode") != "live":
                continue
            sequence, turn = (record.workload.get(key) for key in ("conversation_sequence", "turn_id"))
            if (type(sequence) is not int or sequence < 1 or type(turn) is not int or turn < 0
                    or not record.request_hash or record.input_tokens is None or (sequence, turn) in history):
                return True
            history[sequence, turn] = record
        histories.append(history)
    if not any(histories):
        return False

    positions = [sorted(history) for history in histories]
    common = min(map(len, positions))
    if not common or positions[0][:common] != positions[1][:common]:
        return True
    for position in positions[0][:common]:
        latest, previous = (history[position] for history in histories)
        if (latest.request_hash != previous.request_hash
                or abs(latest.input_tokens - previous.input_tokens)*100 > previous.input_tokens*threshold):
            return True
        if compare_output and any(latest_size is None or previous_size is None
                                  or abs(latest_size-previous_size)*100 > previous_size*threshold
                                  for latest_size, previous_size in zip(output_work(latest), output_work(previous))):
            return True
    if len(positions[0]) == len(positions[1]):
        return False

    # An extra exact-recall repetition has a fresh marker. Other extra turns
    # need a witnessed repetition of the same history and actual work.
    def observed_turn(turn, record):
        return turn, record.fixture_id, record.request_hash, record.input_tokens, output_work(record)

    def completed(record):
        return _user_valid_completion(record) and None not in output_work(record)

    shorter, longer = sorted(histories, key=len)
    witnessed = {observed_turn(turn, record) for (_, turn), record in shorter.items() if completed(record)}
    for (sequence, turn), record in longer.items():
        if (sequence, turn) in shorter:
            continue
        verified_recall = record.workload.get("task_type") == "exact_repeat" and record.score is True
        if not completed(record) or not (verified_recall or observed_turn(turn, record) in witnessed):
            return True

    cohorts = []
    for history in histories:
        turns = defaultdict(list)
        for (_, turn), record in history.items():
            turns[turn].append(record)
        cohorts.append(turns)
    if cohorts[0].keys() != cohorts[1].keys():
        return True
    measures = (lambda r: r.input_tokens, lambda r: r.output_tokens, _total_generated_tokens)
    if compare_output:
        measures += (lambda r: r.output_chars,)
    for turn in cohorts[0]:
        for measure in measures:
            lengths = [[measure(record) for record in turns[turn]] for turns in cohorts]
            if any(None in values for values in lengths) or _length_mix_differs(lengths, threshold):
                return True
    return False


def _fixed_generation_work_changed(current, old, threshold):
    # Fixed fixtures identify logical turns across repetitions. Compare known
    # totals for each turn so heavier and lighter turns cannot cancel out.
    cohorts = []
    for records in (current, old):
        turns = defaultdict(list)
        for record in records:
            if record.workload.get("session_id") and record.workload.get("mode") == "fixed":
                turns[record.fixture_id].append(_total_generated_tokens(record))
        cohorts.append(turns)
    latest, previous = cohorts
    return any(_length_mix_differs([latest[turn], previous[turn]], threshold)
               for turn in latest.keys() & previous.keys())


def _comparable_user_timing_miss(current, old, user, failed_criteria, threshold):
    """Qualify a deadline miss against the same turn in the accepted run."""
    def comparable_attempts():
        return (not any(_load_work_comparison(current, old))
                and not _live_input_work_changed(current, old, threshold, compare_output=True))

    def turn_key(record):
        mode = record.workload.get("mode")
        sequence = record.workload.get("conversation_sequence")
        turn = record.workload.get("turn_id")
        if type(sequence) is int and sequence > 0:
            if mode == "fixed":
                # The fixed fixture identifies the turn even in older records.
                return mode, sequence, record.fixture_id
            if mode == "live" and type(turn) is int and turn >= 0:
                return mode, sequence, record.fixture_id, turn
        return None

    quality = user.get("timing_quality")
    affected_metrics = quality.get("affected_metrics") if isinstance(quality, dict) else None
    if not isinstance(affected_metrics, dict):
        return comparable_attempts()

    previous_turns = defaultdict(list)
    for record in old:
        if key := turn_key(record):
            previous_turns[key].append(record)

    checked_miss = False
    fallback_miss = False
    for name, first, metric in (("max_first_answer_ms", True, "first_output_ms"),
                                ("max_completion_ms", False, "latency_ms")):
        if name not in failed_criteria:
            continue
        criterion = user["criteria"][name]
        limit = criterion.get("limit")
        fallback_miss |= bool(criterion.get("pending_breach"))
        affected_ids = affected_metrics.get(metric)
        if (type(limit) not in {int, float} or not 0 < limit < math.inf
                or not isinstance(affected_ids, list) or not all(isinstance(item, str) for item in affected_ids)):
            fallback_miss = True
            continue
        affected = set(affected_ids)
        for record in current:
            error = _user_error(record)
            if not error:
                if record.status == "cancelled" or record.id in affected:
                    continue
                value = user_latency(record, first)
                if value is None or value <= limit:
                    continue
            checked_miss = True
            key = turn_key(record)
            if key is None:
                fallback_miss |= error or not _fixed_generation_work_changed([record], old, threshold)
                continue
            previous = previous_turns.get(key, [])
            if len(previous) != 1 or any(_load_work_comparison([record], previous)):
                continue
            if key[0] == "fixed":
                if (not _input_work_changed([record], previous, threshold)
                        and (error or not _fixed_generation_work_changed([record], previous, threshold))):
                    return True
            elif error and record.input_tokens is None:
                # An unsuccessful turn can miss the criterion without producing usage.
                if (record.request_hash and record.request_hash == previous[0].request_hash
                        and previous[0].input_tokens is not None):
                    return True
            elif not _live_input_work_changed([record], previous, threshold, compare_output=not error):
                return True
    # Pending admission and legacy summaries can lack an observed turn to match.
    return (fallback_miss or not checked_miss) and comparable_attempts()


def _load_not_delivered(point):
    # A higher-priority failure can own reason_code despite missing concurrency.
    # The flag applies only to concurrent points; older reports may omit it.
    return bool(point and (point.get("reason_code") == "load_not_delivered"
                          or (point.get("pattern") == "concurrent"
                              and point.get("load_qualified") is False)))


def _server_limited_load(point):
    return bool(_load_not_delivered(point) and point.get("pattern") == "concurrent"
                and point.get("level") and point.get("server_evidenced_concurrency") is not None
                and point.get("client_mean_inflight") is not None
                and point["client_mean_inflight"] >= .9 * point["level"]
                and 0 < point["server_evidenced_concurrency"] < point["level"]
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
        return f"streamed output evidenced peak overlap of {point['server_evidenced_concurrency']} against {point['level']} requested concurrent requests"

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
                reason = ("No longer accepted: " + server_reason(point) + "." if point.get("status") != "fail" and _server_limited_load(point)
                          else "This previously acceptable tested load now fails; current: " + explanation(point, row["current"]) + ".")
                row.update(acceptance_change="lost", status="fail", reason=reason)
            elif before == "fail" and after == "pass":
                reason = ("Newly accepted: baseline " + server_reason(previous) + "."
                          if previous.get("status") != "fail" and _server_limited_load(previous) else "Newly accepted; baseline: " + explanation(previous, row["baseline"]) + ".")
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


def _user_measurements(report: RunReport, target: str) -> dict[tuple, tuple[dict, dict]]:
    summary = next((check.metrics.get("multi_user", {}) for check in report.checks
                    if check.id == "multi_user" and check.target == target), {})
    return {(window["scenario"], window["population"], user["profile"], user["user_id"],
             window.get("role") or "", window.get("trigger") or "", window.get("heavy_profile") or ""): (window, user)
            for window in summary.get("windows", []) if window.get("phase") == "measurement"
            for user in window.get("users", [])}


def _user_scenario_evidence(window: dict, user_id: str) -> dict:
    if window.get("scenario") == "noisy_neighbors" and window.get("role") == "probe":
        evidence = window.get("interference") or {"status": "inconclusive", "qualified": False}
        return next((user for user in evidence.get("users", []) if user.get("user_id") == user_id), evidence)
    if window.get("scenario") == "cancellation":
        evidence = window.get("cancellation") or {}
        if user_id != (evidence.get("selected_user_id") or window.get("selected_user_id")):
            return {}
        qualified = (evidence.get("status") in {"pass", "fail"}
                     and not (window.get("cut_short") or window.get("drain_reason"))
                     and all(evidence.get(name, {}).get("status") == "pass"
                             for name in ("cancellation", "peer_continuation"))
                     and evidence.get("followup", {}).get("status") in {"pass", "fail"})
        return {**evidence, "status": evidence.get("status", "inconclusive"), "qualified": qualified}
    return {}


def _cancellation_evidence_ids(evidence: dict, records: dict[str, RequestRecord]) -> list[str]:
    return list(dict.fromkeys(rid for name in ("cancellation", "peer_continuation", "followup")
                              for rid in evidence.get(name, {}).get("request_ids", []) if rid in records))


def _compare_interference_work(current, old, user_id, current_records, old_records, tolerance):
    if current.get("user_id") != user_id or old.get("user_id") != user_id:
        return [], "Matched short-user interference evidence is missing."

    retained_ids, reasons = [], []
    for role in ("control", "probe", "heavy"):
        field = f"{role}_request_ids"
        cohorts = []
        complete = True
        for evidence, records in ((current, current_records), (old, old_records)):
            ids = evidence.get(field, [])
            cohort = [records[rid] for rid in ids if rid in records]
            complete = complete and bool(ids) and len(cohort) == len(ids) == len(set(ids)) and all(
                record.status == "completed" and record.valid for record in cohort)
            if role != "heavy" and len(ids) != evidence.get("matched_samples"):
                complete = False
            cohorts.append(cohort)
        retained_ids.extend(record.id for record in cohorts[0])
        if not complete:
            reasons.append(f"Matched {role} request evidence is incomplete.")
        elif _interference_role_work_changed(*cohorts, tolerance):
            reasons.append(f"Matched {role} work differs or lacks observed token evidence.")
    if not reasons:
        links = [_interference_exposure(evidence) for evidence in (current, old)]
        if any(groups is None for groups in links):
            reasons.append("Matched heavy exposure per probe is missing or incomplete.")
        else:
            exposure = [[[records[rid] for rid in group] for group in groups]
                        for groups, records in zip(links, (current_records, old_records))]
            if _heavy_work_per_probe_changed(exposure, tolerance):
                reasons.append("Matched heavy exposure per matched probe differs between runs.")
            elif reason := _compare_interference_groups(
                    (current, current_records, exposure[0]), (old, old_records, exposure[1]), tolerance):
                reasons.append(reason)
    return list(dict.fromkeys(retained_ids)), " ".join(reasons) or None


def _interference_role_work_changed(current, old, tolerance):
    return any(_load_work_comparison(current, old)) or _user_token_work_changed(current, old, tolerance)


def _interference_groups(evidence, records, exposure):
    groups = defaultdict(list)
    for control_id, probe_id, heavy in zip(evidence["control_request_ids"], evidence["probe_request_ids"], exposure):
        control, probe = records[control_id], records[probe_id]
        identity = probe.workload.get("matched_work_sha256")
        if not isinstance(identity, str) or not identity or control.workload.get("matched_work_sha256") != identity:
            return None
        groups[identity].append((control, probe, heavy))
    return groups


def _compare_interference_groups(current, old, tolerance):
    latest, previous = _interference_groups(*current), _interference_groups(*old)
    if latest is None or previous is None:
        return "Matched logical short work evidence is missing or inconsistent."
    current_count = sum(len(pairs) for pairs in latest.values())
    old_count = sum(len(pairs) for pairs in previous.values())
    if latest.keys() != previous.keys() or any(
            abs(Fraction(len(pairs), current_count) - Fraction(len(previous[identity]), old_count)) > tolerance / 100
            for identity, pairs in latest.items()):
        return "Matched logical short work or its proportions differ between runs."
    if len(latest) == 1:
        return None  # The aggregate checks covered the only logical work group.

    for identity, pairs in latest.items():
        current_controls, current_probes, current_heavy = zip(*pairs)
        old_controls, old_probes, old_heavy = zip(*previous[identity])
        for role, cohorts in (("control", (current_controls, old_controls)), ("probe", (current_probes, old_probes))):
            if _interference_role_work_changed(*cohorts, tolerance):
                return f"Matched {role} work differs for the same logical short work."
        heavy_cohorts = [[record for group in run for record in group] for run in (current_heavy, old_heavy)]
        if (_interference_role_work_changed(*heavy_cohorts, tolerance)
                or _heavy_work_per_probe_changed((current_heavy, old_heavy), tolerance)):
            return "Matched heavy exposure per matched probe differs for the same logical short work."
    return None


def _interference_exposure(evidence):
    probes, heavy_ids = evidence.get("probe_request_ids", []), set(evidence.get("heavy_request_ids", []))
    if not probes or not heavy_ids or len(probes) != len(set(probes)):
        return None
    links = evidence.get("probe_heavy_request_ids")
    if links is None and (len(probes) == 1 or len(heavy_ids) == 1):
        # Older reports only retain the union. In either case it determines every group.
        links = {probe: sorted(heavy_ids) for probe in probes}
    if not isinstance(links, dict) or set(links) != set(probes):
        return None
    represented = set()
    for ids in links.values():
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(rid, str) or rid not in heavy_ids for rid in ids)
                or len(ids) != len(set(ids))):
            return None
        represented.update(ids)
    if represented != heavy_ids:
        return None
    return [links[probe] for probe in probes]


def _heavy_work_per_probe_changed(exposure, tolerance):
    if _work_samples_changed([[len(group) for group in run] for run in exposure], tolerance):
        return True
    measures = _token_work_measures(record for run in exposure for group in run for record in group)
    # Every retained heavy record has already passed the individual token work check.
    return any(_work_samples_changed(
        [[sum(measure(record) for record in group) for group in run] for run in exposure], tolerance)
        for measure in measures)


def _heavy_interference_peers(evidence, request_ids):
    """Keep only the short pairs whose recorded competitor was this heavy user."""
    peers = {}
    request_ids = set(request_ids)
    for short in evidence.get("users", []):
        heavy = request_ids.intersection(short.get("heavy_request_ids", []))
        if not heavy:
            continue
        peer_id = short.get("user_id")
        peers[peer_id] = {}
        if not peer_id or not short.get("qualified") or short.get("status") == "inconclusive":
            continue
        control, probe = (short.get(f"{role}_request_ids", []) for role in ("control", "probe"))
        links = _interference_exposure(short)
        if links is None or len(control) != len(probe) or len(probe) != short.get("matched_samples"):
            continue
        matched = {pid: [rid for rid in competitors if rid in heavy]
                   for pid, competitors in zip(probe, links)}
        matched = {pid: ids for pid, ids in matched.items() if ids}
        peers[peer_id] = {"user_id": peer_id, "matched_samples": len(matched),
                          "control_request_ids": [cid for cid, pid in zip(control, probe) if pid in matched],
                          "probe_request_ids": list(matched), "heavy_request_ids": sorted(heavy),
                          "probe_heavy_request_ids": matched}
    return peers


def _compare_heavy_interference_work(current, old, current_records, old_records, tolerance):
    if not current or current.keys() != old.keys():
        return (), "Matched short-peer exposure for this heavy user is missing or differs between runs."
    linked = [Counter(), Counter()]
    for peer_id, latest in current.items():
        previous = old[peer_id]
        if not latest or not previous:
            return (), "Matched short-peer exposure for this heavy user is incomplete."
        _, reason = _compare_interference_work(latest, previous, peer_id, current_records, old_records, tolerance)
        if reason:
            return (), reason
        for counts, peer in zip(linked, (latest, previous)):
            counts.update(rid for ids in peer["probe_heavy_request_ids"].values() for rid in ids)
    if _work_samples_changed([list(counts.values()) for counts in linked], tolerance):
        return (), "Matched short-probe exposure per heavy request differs between runs."
    return tuple({"probe_request_ids": counts.keys()} for counts in linked), None


def _probe_has_overlap(evidence: dict, records: list[RequestRecord]) -> bool:
    exposed = set(evidence.get("probe_request_ids", []))
    return bool(records) and all(record.id in exposed for record in records)


def _token_work_measures(records):
    measures = (lambda r: r.input_tokens, lambda r: r.output_tokens, _total_generated_tokens)
    if any((record.cached_prompt_tokens or 0) > 0 for record in records):
        measures += (_uncached_input_tokens,)
    return measures


def _work_samples_changed(lengths, tolerance):
    if any(not values or None in values for values in lengths):
        return True
    medians = [median(values) for values in lengths]
    return max(medians) > min(medians) * (1 + tolerance / 100) or _length_mix_differs(lengths, tolerance)


def _user_token_work_changed(current, previous, tolerance):
    cohorts = current, previous
    measures = _token_work_measures(record for cohort in cohorts for record in cohort)
    return any(_work_samples_changed([[measure(record) for record in cohort] for cohort in cohorts], tolerance)
               for measure in measures)


def _compare_user_fairness(current, previous, current_records, old_records):
    """Compare a previously accepted fairness limit on the same observed work."""
    latest, old = current.get("fairness", {}), previous.get("fairness", {})
    limit = old.get("threshold")
    latest_status = latest.get("status")
    if (current.get("role") or previous.get("role") or limit is None or latest.get("threshold") != limit
            or old.get("status") != "pass" or old.get("qualified") is not True
            or latest_status not in {"pass", "fail", "inconclusive"}):
        return None

    tolerance = old.get("tolerance_percent", 10)
    compared = set(old.get("compared_user_ids", []))
    comparable = (latest_status in {"pass", "fail"} and latest.get("qualified") is True
                  and latest.get("tolerance_percent", 10) == tolerance
                  and old.get("ratio") is not None and latest.get("ratio") is not None
                  and len(compared) >= 2 and all(set(fairness.get(field, [])) == compared
                      for fairness in (latest, old) for field in ("compared_user_ids", "eligible_user_ids")))
    evidence = []
    if comparable:
        latest_users, old_users = ({user["user_id"]: user for user in window["users"]}
                                   for window in (current, previous))
        latest_samples, old_samples = ({sample["user_id"]: sample for sample in fairness["users"]}
                                       for fairness in (latest, old))
        for user_id in sorted(compared):
            latest_sample, old_sample = latest_samples.get(user_id, {}), old_samples.get(user_id, {})
            latest_exposure, old_exposure = (sample.get("active_exposure_ms") for sample in (latest_sample, old_sample))
            if (not latest_exposure or not old_exposure
                    or abs(latest_exposure - old_exposure)*100 > min(latest_exposure, old_exposure)*tolerance):
                comparable = False
                break
            cohorts = []
            for window, user, records, sample in (
                    (current, latest_users.get(user_id, {}), current_records, latest_sample),
                    (previous, old_users.get(user_id, {}), old_records, old_sample)):
                cohort = user_delivery_completions(
                    [records[rid] for rid in user.get("request_ids", []) if rid in records], window, user_id)
                if not cohort or len(cohort) != sample.get("valid_samples"):
                    comparable = False
                    break
                cohorts.append(cohort)
            if not comparable:
                break
            if (latest_users[user_id]["profile"] != old_users[user_id]["profile"]
                    or any(_load_work_comparison(*cohorts)) or _live_input_work_changed(*cohorts, tolerance)
                    or _user_token_work_changed(*cohorts, tolerance)):
                comparable = False
                break
            evidence.extend(record.id for record in cohorts[0])

    scenario, population = current["scenario"], current["population"]
    status = latest_status if comparable else "inconclusive"
    reasons = {"pass": "Observed delivery fairness stayed within the previously accepted threshold.",
               "fail": "Observed delivery fairness exceeded the previously accepted threshold.",
               "inconclusive": "Observed fairness work or overlap differs or lacks evidence."}
    return {"check": "multi_user", "scenario": f"{scenario} / {population}", "population": population,
            "window_id": current["id"], "metric": "fairness_acceptance", "aggregation": "selected_criteria",
            "current": latest.get("ratio"), "baseline": old.get("ratio"), "threshold": limit,
            "current_samples": sum(item.get("valid_samples", 0) for item in latest.get("users", [])),
            "baseline_samples": sum(item.get("valid_samples", 0) for item in old.get("users", [])),
            "status": status, "worsening": {"pass": 0, "fail": 100}.get(status),
            "reason": f"Multi-user fairness at {population} users: {reasons[status]}", "evidence_ids": evidence}


def _compare_user_delivery(report, baseline, target, reasons, threshold):
    current, previous = _user_measurements(report, target), _user_measurements(baseline, target)
    rows, incomplete = [], []
    if reasons or not (current or previous):
        return rows, incomplete
    current_records = {record.id: record for record in report.requests if record.target == target}
    old_records = {record.id: record for record in baseline.requests if record.target == target}
    tolerance = (report.manifest.get("config", {}).get("multi_user") or {}).get("fairness_tolerance_percent", 10)
    window_pairs = {}
    for key in sorted(current.keys() | previous.keys()):
        scenario, population, profile, user_id, role, trigger, heavy_profile = key
        c_window, c = current.get(key, ({}, {}))
        b_window, b = previous.get(key, ({}, {}))
        if c_window and b_window:
            window_pairs[c_window["id"], b_window["id"]] = c_window, b_window
        c_rate, b_rate = c.get("output_tokens_per_second"), b.get("output_tokens_per_second")
        current_work = [current_records[rid] for rid in c.get("request_ids", []) if rid in current_records]
        old_work = [old_records[rid] for rid in b.get("request_ids", []) if rid in old_records]
        c_scenario, b_scenario = (_user_scenario_evidence(window, user_id) for window in (c_window, b_window))
        scenario_reason = ("Scenario evidence is missing or unqualified in the current run or baseline."
                           if any(item and (not item.get("qualified") or item.get("status") == "inconclusive")
                                  for item in (c_scenario, b_scenario)) else None)
        current_delivery = user_delivery_completions(current_work, c_window, user_id)
        old_delivery = user_delivery_completions(old_work, b_window, user_id)
        comparable_work = current_delivery, old_delivery
        complete = all(user.get("coverage", {}).get("status") == "complete" for user in (c, b))
        drain_comparison = False
        if complete and c_rate is not None and b_rate is not None and (
                (c_rate == 0 and not current_delivery) or (b_rate == 0 and not old_delivery)):
            # A measured zero can have no in-window completions. Match the
            # actual admitted work only when it all finished successfully.
            drained = tuple(user_delivery_completions(work, window, user_id, include_drain=True)
                            for work, window in ((current_work, c_window), (old_work, b_window)))
            if all(cohort and len(cohort) == user.get("outcomes", {}).get("attempted")
                   for cohort, user in zip(drained, (c, b))):
                comparable_work = drained
                drain_comparison = True
        work_reason, mix_changed = _load_work_comparison(*comparable_work)
        if drain_comparison and _user_token_work_changed(*comparable_work, threshold):
            work_reason = "Observed work including drained requests differs or lacks token evidence."
        live_work_changed = _live_input_work_changed(*comparable_work, threshold, compare_output=True)
        live_work_reason = ("Observed work or live generated histories differ or lack evidence."
                            if live_work_changed else None)
        noisy_probe = scenario == "noisy_neighbors" and role == "probe"
        short_probe = noisy_probe and any(evidence.get("user_id") == user_id
                                          for evidence in (c_scenario, b_scenario))
        interference_ids, interference_reason = ([], None)
        if short_probe:
            interference_ids, interference_reason = _compare_interference_work(
                c_scenario, b_scenario, user_id, current_records, old_records, tolerance)
        elif noisy_probe:
            peers = [_heavy_interference_peers(evidence, user.get("request_ids", []))
                     for evidence, user in ((c_scenario, c), (b_scenario, b))]
            overlap, interference_reason = _compare_heavy_interference_work(
                *peers, current_records, old_records, tolerance)
            # The window-level status combines every short user's evidence. This
            # heavy user's rows depend only on the qualified peers it overlapped.
            c_scenario, b_scenario = overlap or ({}, {})
            scenario_reason = None
        delivery_reason = ("Shared-window delivery includes samples without qualified heavy overlap."
                           if noisy_probe and not (scenario_reason or interference_reason) and not all(
                               _probe_has_overlap(evidence, records)
                               for evidence, records in zip((c_scenario, b_scenario), comparable_work))
                           else None)
        missing = (not complete
                   or c_rate is None or b_rate is None or work_reason or mix_changed
                   or live_work_reason or scenario_reason or interference_reason or delivery_reason)
        identity = {"check": "multi_user", "scenario": " / ".join(map(str, key if role else key[:4])),
                    "user_id": user_id, "population": population, "profile": profile,
                    "role": role, "trigger": trigger, "heavy_profile": heavy_profile,
                    "window_id": c_window.get("id", b_window.get("id"))}
        row = {**identity, "metric": "shared_window_output_tokens_per_second", "aggregation": "common_window",
               "current": c_rate, "baseline": b_rate, "status": "inconclusive",
               "current_samples": len(current_delivery), "baseline_samples": len(old_delivery),
               "threshold": threshold, "change_unit": "percent",
               "evidence_ids": [record.id for record in (current_delivery or comparable_work[0])],
               "method": "Observed completed-output delivery over matching common windows; no cross-run confidence interval."}
        if noisy_probe:
            row["timing_exposure_qualified"] = not (scenario_reason or interference_reason) and all(
                _probe_has_overlap(evidence, [r for r in records if r.status == "completed" and r.valid])
                for evidence, records in ((c_scenario, current_work), (b_scenario, old_work)))
        if missing:
            detail = (work_reason or "Requested work proportions differ." if mix_changed or work_reason else
                      live_work_reason or scenario_reason or interference_reason or delivery_reason
                      or "Missing or incomplete shared-window coverage.")
            message = f"Multi-user {user_id} at {population} users: {detail}"
            row["reason"] = message
            incomplete.append(message)
        elif b_rate == 0 and c_rate != 0:
            row["reason"] = "Zero baseline delivery does not support a relative rate comparison."
            incomplete.append(f"Multi-user {user_id} has zero baseline delivery.")
        else:
            worsening = (b_rate-c_rate)*100/b_rate if b_rate else 0
            row.update(worsening=worsening, observed_delta=c_rate-b_rate,
                       observed_change=-worsening,
                       status="fail" if worsening > threshold else "pass",
                       reason="Observed shared-window delivery decreased beyond the selected threshold."
                              if worsening > threshold else "No material shared-window delivery decrease observed.")
        rows.append(row)
        if short_probe and b_scenario.get("status") == "pass":
            reason = scenario_reason or interference_reason
            observed = c_scenario.get("status", "inconclusive")
            if reason or observed not in {"pass", "fail"}:
                status = "inconclusive"
                reason = f"Multi-user {user_id} at {population} users: {reason or 'Interference evidence is unavailable.'}"
                incomplete.append(reason)
            else:
                status = observed
                reason = ("This user's previously accepted interference scenario now fails its selected criterion."
                          if status == "fail" else "The observed slowdown remains within the selected limit.")
            rows.append({**row, "metric": "scenario_acceptance", "status": status,
                         "current": observed, "baseline": "pass", "aggregation": "selected_scenario_criteria",
                         "worsening": {"fail": 100, "pass": 0}.get(status), "reason": reason,
                         "current_samples": c_scenario.get("matched_samples", 0),
                         "baseline_samples": b_scenario.get("matched_samples", 0), "evidence_ids": interference_ids,
                         "observed_change": None, "observed_delta": None})
        elif short_probe and b_scenario.get("status") == "measured" and interference_reason:
            reason = f"Multi-user {user_id} at {population} users: {interference_reason}"
            incomplete.append(reason)
            rows.append({**identity,
                "metric": "matched_interference_slowdown", "status": "inconclusive", "threshold": None,
                "current": c_scenario.get("slowdown"), "baseline": b_scenario.get("slowdown"),
                "current_samples": c_scenario.get("matched_samples", 0),
                "baseline_samples": b_scenario.get("matched_samples", 0),
                "reason": reason, "evidence_ids": interference_ids,
            })
        if (not noisy_probe and c_scenario.get("status") == "fail" and b_scenario.get("status") == "pass"
                and not scenario_reason and not any(_load_work_comparison(current_work, old_work))):
            acceptance = {**row, "metric": "scenario_acceptance", "status": "fail",
                          "current": "fail", "baseline": "pass", "aggregation": "selected_scenario_criteria",
                          "worsening": 100, "reason": "This user's previously accepted scenario now fails its selected criteria.",
                          "observed_change": None, "observed_delta": None}
            if scenario == "cancellation":
                current_evidence = _cancellation_evidence_ids(c_scenario, current_records)
                old_evidence = _cancellation_evidence_ids(b_scenario, old_records)
                acceptance.update(evidence_ids=current_evidence, current_samples=len(current_evidence),
                                  baseline_samples=len(old_evidence))
            rows.append(acceptance)
        if c.get("status") == "fail" and b.get("status") == "pass":
            # Acceptance also includes attempts that drain after delivery closes.
            current_attempts = [record for record in current_work if not record.intentional]
            old_attempts = [record for record in old_work if not record.intentional]
            failed_criteria = {name for name, criterion in c.get("criteria", {}).items()
                               if criterion.get("status") == "fail"}
            delivery_failure = "min_output_tokens_per_second" in failed_criteria and not missing
            failed_timing = failed_criteria & {"max_first_answer_ms", "max_completion_ms"}
            timing_exposed = not noisy_probe or (not (scenario_reason or interference_reason)
                and _probe_has_overlap(c_scenario, current_attempts)
                and _probe_has_overlap(b_scenario, old_attempts))
            # A pending admission has no request and cannot establish heavy overlap.
            observed_timing_failures = [name for name in failed_timing if not noisy_probe
                or not c["criteria"][name].get("pending_breach") or c["criteria"][name].get("missed")]
            attempt_failure = ("max_error_percent" in failed_criteria or timing_exposed and not failed_criteria) and not (
                any(_load_work_comparison(current_attempts, old_attempts))
                or _live_input_work_changed(current_attempts, old_attempts, threshold))
            timing_failure = timing_exposed and observed_timing_failures and _comparable_user_timing_miss(
                current_attempts, old_attempts, c, observed_timing_failures, threshold)
            acceptance = {**row, "metric": "user_acceptance", "current": "fail", "baseline": "pass",
                          "aggregation": "selected_criteria", "observed_change": None, "observed_delta": None,
                          "current_samples": len(current_attempts), "baseline_samples": len(old_attempts),
                          "evidence_ids": [record.id for record in current_attempts]}
            if not (delivery_failure or attempt_failure or timing_failure):
                detail = (interference_reason or "Timing acceptance lacks qualified heavy overlap."
                          if noisy_probe and failed_timing and (not timing_exposed or not observed_timing_failures)
                          else "Work behind the selected acceptance criteria differs or lacks evidence.")
                reason = f"Multi-user {user_id} at {population} users: {detail}"
                incomplete.append(reason)
                acceptance.update(status="inconclusive", worsening=None, reason=reason)
            else:
                acceptance.update(status="fail", worsening=100,
                                  reason="This previously accepted user now fails its selected criteria.")
            rows.append(acceptance)
    for c_window, b_window in window_pairs.values():
        if row := _compare_user_fairness(c_window, b_window, current_records, old_records):
            rows.append(row)
            if row["status"] == "inconclusive":
                incomplete.append(row["reason"])
    return rows, incomplete


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
    reason_details = {}
    reasons = _settings_reasons(report, baseline, details=reason_details)
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
                  "Intervals describe within-run variation, not cross-run environmental stability. "
                  "Per-user shared-window delivery compares observed common-window rates without a confidence interval.",
    }
    for target in targets or [{"name": "unknown"}]:
        name = target["name"]
        target_reasons = list(reasons)
        target_details = dict(reason_details)
        saturated_runs = [label for label, run in (("Current", report), ("Baseline", baseline))
                          if _generator_saturated(run, name)]
        old_target = old_targets.get(name)
        identity_changes = {}
        if old_target is None:
            target_reasons.append("Target is absent from the baseline.")
        else:
            for key in ("model", "url", "route", "parent"):
                if key not in target or key not in old_target or target[key] != old_target[key]:
                    text = f"Target {key} differs or is unknown."
                    target_reasons.append(text)
                    target_details[text] = {"code": "target_differs", "key": key,
                                            "baseline": old_target.get(key), "current": target.get(key), "text": text}
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
        user_rows, user_reasons = _compare_user_delivery(report, baseline, name, target_reasons, threshold)
        rows.extend(user_rows)
        qualified_user_probes = {row["scenario"] for row in user_rows if row.get("timing_exposure_qualified")}
        # Optional observations and checks without inference/acceptance cohorts
        # do not participate in baseline verdicts or sample-collection guidance.
        non_request_checks = {"gpu"}
        non_verdict_checks = non_request_checks | {
            check.id for run in (report, baseline) for check in run.checks
            if check.target == name and not check.required
        }
        traffic = (_compare_traffic(report, baseline, name, target_reasons, threshold)
                   if "capacity" not in non_verdict_checks else None)
        load_tests = (_compare_load_tests(report, baseline, name, target_reasons)
                      if "load" not in non_verdict_checks else None)
        load_exclusions = []
        observed_outputs = []
        load_points = {label: {point['id']: point for check in run.checks
                              if check.id == 'load' and check.target == name
                              for point in check.metrics.get('points', [])}
                       for label, run in (("Current", report), ("Baseline", baseline))}
        def load_shape_shares(run):
            counts = defaultdict(Counter)
            for record in run.requests:
                point = record.load_point or {}
                if record.target == name and "load" in record.check_ids and point.get("point_id"):
                    counts[point["point_id"]][point.get("shape")] += 1
            return {point_id: {shape: Fraction(count, sum(shapes.values())) for shape, count in shapes.items()}
                    for point_id, shapes in counts.items()}
        current_shares, old_shares = load_shape_shares(report), load_shape_shares(baseline)
        current_groups, old_groups = _groups(report.requests, name), _groups(baseline.requests, name)
        if not target_reasons:
            for check, scenario in sorted(set(current_groups) | set(old_groups)):
                if check in non_verdict_checks:
                    continue
                current_records = current_groups.get((check, scenario), [])
                old_records = old_groups.get((check, scenario), [])
                record_cohorts = current_records, old_records
                conversation_user = check == "multi_user" and any(
                    r.workload.get("session_id") for cohort in record_cohorts for r in cohort)
                point_records = current_records or old_records
                workload = point_records[0].workload
                unqualified_user_probe = (check == "multi_user" and workload.get("scenario") == "noisy_neighbors"
                                          and workload.get("role") == "probe" and scenario not in qualified_user_probes)
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
                                            if check in {"load", "multi_user"} else (None, False))
                if check not in {"load", "multi_user"} and (not current_records or not old_records or shapes(current_records) != shapes(old_records)):
                    work_reason = "missing or different request shapes or proportions."
                if work_reason:
                    if check == "load":
                        load_exclusions.append({"scenario": scenario, "load_point": load_point, "reason": work_reason})
                    else:
                        rows.append({"check": check, "scenario": scenario, "metric": "request_shapes",
                                     "status": "inconclusive", "reason": "Missing or different request shapes or proportions.",
                                     "current_samples": len(current_records), "baseline_samples": len(old_records),
                                     "evidence_ids": [record.id for record in current_records]})
                    # Conversation timing can still match even when attempts differ.
                    if not conversation_user:
                        continue
                if load_point:
                    current_mix = current_shares.get(current_records[0].load_point.get("point_id"), {})
                    old_mix = old_shares.get(old_records[0].load_point.get("point_id"), {})
                    mix_changed |= any(abs(current_mix.get(shape, 0) - old_mix.get(shape, 0)) > Fraction(1, 10)
                                       for shape in current_mix.keys() | old_mix.keys())
                current_timing, old_timing = {}, {}
                current_metrics = _metric_values(current_records, check=check,
                                                 timing_records=current_timing if conversation_user else None)
                old_metrics = _metric_values(old_records, check=check,
                                             timing_records=old_timing if conversation_user else None)
                current_work, old_work = (_output_work(records, check=check) for records in record_cohorts)
                if not work_reason:
                    outputs = _output_observations(check, scenario, current_work, old_work)
                    if load_point:
                        for output in outputs:
                            output["load_point"] = load_point
                    observed_outputs.extend(outputs)
                different_work = _different_output_work(current_work, old_work, threshold)
                live_inputs_differ = live_output_differs = False
                if check == "multi_user" and not conversation_user:
                    completions = tuple([r for r in cohort if _valid_completion(r, check)]
                                        for cohort in record_cohorts)
                    live_inputs_differ = _live_input_work_changed(*completions, threshold)
                    live_output_differs = (not live_inputs_differ and _live_input_work_changed(
                        *completions, threshold, compare_output=True))
                elif check != "multi_user":
                    live_inputs_differ = (any(r.workload.get("mode") == "live" for r in current_records)
                                          and Counter(r.request_hash for r in current_records)
                                          != Counter(r.request_hash for r in old_records))
                input_work_changed = _input_work_changed(*record_cohorts, threshold)
                for metric, values in current_metrics.items():
                    measured_conversation = conversation_user and metric in TIMING_METRICS
                    if (work_reason and not measured_conversation) or (not values and not old_metrics[metric]):
                        continue
                    row = _compare_metric(metric, values, old_metrics[metric],
                                          1 if check == "access" and metric in {"valid_completion_rate", "error_rate"} else minimum,
                                          threshold)
                    current_samples, old_samples = record_cohorts
                    current_output, old_output = current_work, old_work
                    metric_work_reason, metric_mix_changed = None, mix_changed
                    metric_input_changed, metric_output_changed = input_work_changed, different_work
                    metric_live_inputs, metric_live_output = live_inputs_differ, live_output_differs
                    metric_fixed_output = False
                    if measured_conversation:
                        current_samples, old_samples = current_timing[metric], old_timing[metric]
                        metric_work_reason, metric_mix_changed = (
                            _load_work_comparison(current_samples, old_samples)
                            if current_samples and old_samples else (None, False))
                        current_output = _output_work(current_samples, check=check)
                        old_output = _output_work(old_samples, check=check)
                        metric_input_changed = _input_work_changed(current_samples, old_samples, threshold)
                        metric_output_changed = _different_output_work(current_output, old_output, threshold)
                        metric_live_inputs = _live_input_work_changed(current_samples, old_samples, threshold)
                        metric_live_output = (not metric_live_inputs and _live_input_work_changed(
                            current_samples, old_samples, threshold, compare_output=True))
                        metric_fixed_output = _fixed_generation_work_changed(current_samples, old_samples, threshold)
                    warnings = []
                    initial_uncertainty = row["reason"] if row["status"] == "inconclusive" else None
                    def warn(code, message, *, preserve_failure=False):
                        warnings.append({"code": code, "message": message})
                        if not preserve_failure or row["status"] != "fail":
                            row["status"] = "inconclusive"
                    if (metric_input_changed or metric_live_inputs) and metric in TIMING_METRICS:
                        warn("input_work_changed", "Observed input work or live generated histories differ; timing is not like-for-like.")
                    if metric_live_output and metric in TIMING_METRICS:
                        warn("output_work_changed", "Observed generated work differs at matching live conversation turns; timing is not like-for-like.")
                    elif metric_fixed_output:
                        warn("output_work_changed", "Observed total generation differs at matching fixed conversation turns. Timing requires comparable generated work.")
                    elif metric_output_changed and metric in {"latency_ms", "max_stream_gap_ms", "output_tokens_per_second", "generation_tokens_per_second"}:
                        warn("output_work_changed", "Actual generated output lengths differ materially: "
                             f"median {old_output['reported_output_tokens_p50']} → {current_output['reported_output_tokens_p50']} tokens "
                             f"and {old_output['output_chars_p50']} → {current_output['output_chars_p50']} characters. "
                             "These timings include different amounts of generated work; this does not establish a speed improvement or regression.")
                    if metric_work_reason:
                        warn("requested_work_changed", metric_work_reason)
                    if metric_mix_changed:
                        warn("request_mix_changed", "Request work identity shares differ by more than 10 percentage points; observed aggregate values reflect different request mixes.")
                    if metric in TIMING_METRICS:
                        if unqualified_user_probe:
                            warn("unqualified_interference", "Noisy-neighbor probe timing requires comparable matched work and qualified heavy overlap in both runs.")
                        if load_point:
                            for label, records in (("Current", current_records), ("Baseline", old_records)):
                                point = load_points[label].get(records[0].load_point.get("point_id"), records[0].load_point)
                                if _load_not_delivered(point):
                                    if _server_limited_load(point):
                                        message = f"{label} streamed output evidenced peak overlap of {point['server_evidenced_concurrency']} against {point['level']} requested concurrent requests; requested load was not evidenced, so the timing populations differ."
                                    else:
                                        detail = point.get("concurrency_reason") or point.get("reason") or "Client occupancy or stream concurrency evidence is insufficient."
                                        message = f"{label} requested concurrency was not delivered or evidenced; {detail} Server queueing is not established."
                                    warn("load_not_delivered", message, preserve_failure=label == "Current")
                        quality = {"Current": _timing_quality(report, name, current_samples, (metric,)),
                                   "Baseline": _timing_quality(baseline, name, old_samples, (metric,))}
                        delayed_runs = [label for label, item in quality.items() if item["status"] == "client_delayed"]
                        row["timing_quality"] = quality
                        if delayed_runs:
                            for label in delayed_runs:
                                q = quality[label]
                                if q["scope"] == "legacy_run_maximum":
                                    delay = (f"{label} recorded a run-wide maximum client scheduling delay of "
                                             f"{q['max_overlapping_lag_ms']:.0f} ms "
                                             f"(threshold {q['threshold_ms']:.0f} ms). "
                                             "Request-level overlap was not recorded; the legacy fallback "
                                             "conservatively qualifies all samples.")
                                else:
                                    delay = (f"{label} client scheduling delay overlaps "
                                             f"{q['affected_requests']}/{q['total_requests']} requests for this metric "
                                             f"(maximum overlapping delay {q['max_overlapping_lag_ms']:.0f} ms; "
                                             f"threshold {q['threshold_ms']:.0f} ms).")
                                warn("client_delayed", delay + " Some waiting time may come from Giraffe, "
                                     "so endpoint timing attribution is uncertain.")
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
                               output_work={"current": current_output, "baseline": old_output},
                               evidence_ids=[record.id for record in current_samples])
                    if load_point:
                        row["load_point"] = load_point
                    if check == "multi_user":
                        row.update({key: point_records[0].workload.get(key)
                                    for key in ("user_id", "population", "profile", "window_id", "role", "trigger", "heavy_profile")})
                        row["timing_origin"] = "user_ready"
                    rows.append(row)
        target_reasons.extend(user_reasons)
        covered_load_points = {point["id"] for row in (load_tests or {}).get("points", [])
                               for side in ("current", "baseline") if (point := row.get(side))
                               and point.get("id") and point.get("pattern") != "interference"}
        for exclusion in load_exclusions:
            point = exclusion.get("load_point") or {}
            if point.get("pattern") == "interference" or point.get("point_id") not in covered_load_points:
                target_reasons.append(f"load/{exclusion['scenario']}: {exclusion['reason']}")
        assessed = [row for row in rows if "worsening" in row and row["status"] in {"pass", "fail"}]
        failures = [row for row in assessed if row["status"] == "fail"]
        # A sparse secondary metric cannot invalidate an otherwise measured check.
        # Coverage must nevertheless concern that check's actual verdict evidence.
        verdict_metrics = {
            "access": {"valid_completion_rate", "error_rate"},
            "serving": {"valid_completion_rate", "error_rate"},
            "first_output": {"first_output_ms"},
            "generation": {"generation_tokens_per_second", "max_stream_gap_ms", "output_tokens_per_second"},
            **{check: {"correctness_rate"} for check in
               ("correctness", "json", "context", "tools", "consistency")},
        }
        request_paths = ((set(CHECK_NAMES) - non_request_checks)
                         | {"capacity", "concurrency", "fairness", "arrivals", "buckets", "mixed", "token_stress"}
                         | {check for check, _ in set(current_groups) | set(old_groups)})
        selected_checks = {check.id for check in report.checks
                           if check.target == name and check.id != "baseline" and check.required
                           and check.status != "skipped" and check.id not in non_verdict_checks
                           and check.id in config.get("checks", []) and check.id in request_paths}
        covered = {row["check"] for row in assessed
                   if row["metric"] in verdict_metrics.get(row["check"], {row["metric"]})}
        if any(row["status"] in {"pass", "fail"} for row in (traffic or {}).get("stages", [])):
            covered.add("capacity")
        if any(row["status"] in {"pass", "fail"} for row in (load_tests or {}).get("points", [])):
            covered.add("load")
        missing_checks = sorted(selected_checks - covered)
        traffic_failures = [row for row in (traffic or {}).get("stages", []) if row["status"] == "fail"]
        load_failures = [row for row in (load_tests or {}).get("points", []) if row["status"] == "fail"]
        point_count = sum(row["status"] in {"pass", "fail"}
                          for row in (traffic or {}).get("stages", []) + (load_tests or {}).get("points", []))
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
            prefix = ("Nothing comparable between the runs. " if not assessed and not point_count
                      else "Baseline comparison is inconclusive: ")
            summary = prefix + " ".join(target_reasons)
            if missing_checks:
                summary += " No comparable verdict metric for selected check(s): " + ", ".join(missing_checks) + "."
        elif missing_checks:
            status = "inconclusive"
            prefix = "Nothing comparable between the runs. " if not assessed and not point_count else ""
            summary = prefix + "No comparable verdict metric for selected check(s): " + ", ".join(missing_checks) + "."
        elif (load_tests is not None and load_tests["status"] == "inconclusive"
              and load_tests.get("reason_code") != "no_accepted_baseline"):
            # A comparable point covers the selected check, but cannot establish
            # preservation of a different, previously accepted load level.
            status = "inconclusive"
            summary = load_tests["reason"]
        elif not assessed and not point_count:
            status = "inconclusive"
            summary = "Nothing comparable between the runs."
        else:
            status = "pass"
            measurements = ([f"{len(assessed)} comparable metric(s)"] if assessed else [])
            if point_count:
                measurements.append(f"{point_count} comparable acceptance point(s)")
            summary = "No material regression detected in " + " and ".join(measurements) + "."
            if traffic is not None:
                summary += " " + traffic["reason"]
        excluded = len(rows) - len(assessed)
        if (load_tests and load_tests["reason"] not in summary
                and (load_tests["status"] == "inconclusive"
                     or any(load_tests.get("summary_counts", {}).get(change) for change in ("gained", "lost")))):
            summary += " " + load_tests["reason"]
        if excluded:
            summary += f" {excluded} metric(s) not comparable (low samples, undefined values or uncertain attribution); see comparison details."
        if status == "inconclusive" and missing_checks:
            summary += (f" To conclude, collect at least {minimum} measured samples per metric in both the current and baseline runs,"
                        " using the same workload and settings; rerun longer if variation remains uncertain.")
        entry = {"target": name, "status": status, "summary": summary, "reasons": target_reasons,
                 "load_exclusions": load_exclusions,
                 "observed_outputs": observed_outputs,
                 "reason_details": [target_details.get(text, {"code": "comparison_unavailable", "text": text})
                                    for text in target_reasons],
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
    return refresh_performance(result, force=True)


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
            or ("multi_user" in record.check_ids and truncated_conversation_turn(record))
            or record.evaluation_error is not None or record.id in failed_evidence
        ))
        if (policy != "all" and "multi_user" in record.check_ids
                and record.workload.get("session_id") and record.workload.get("mode") == "live"
                and record.evaluation and any(message.get("role") == "assistant"
                    for message in record.evaluation.messages)):
            # Prior replies are response bodies even when copied into later inputs.
            record = record.model_copy(update={"evaluation": record.evaluation.model_copy(update={
                "messages": [{**message, "content": ""} if message.get("role") == "assistant" else message
                             for message in record.evaluation.messages]})})
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
    metadata = report.model_copy(update={"schema_version": CURRENT_SCHEMA, "performance": {
        **report.performance, "targets": [{key: value for key, value in target.items() if key != "request_series"}
                                         for target in report.performance.get("targets", [])]
    }}).model_dump_json(exclude={"requests"})
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
    metadata = report.model_copy(update={"schema_version": CURRENT_SCHEMA, "performance": {
        **report.performance, "targets": [{key: value for key, value in target.items() if key != "request_series"}
                                         for target in report.performance.get("targets", [])]
    }}).model_dump_json(exclude={"requests"})
    yield metadata[:-1] + ',"requests":['
    for index, record in enumerate(report.requests):
        yield "," if index else ""
        yield record.model_dump_json()
    yield "]}"


def save_baseline_report(source: Path, destination: Path, *, replace: bool = False,
                         report: RunReport | None = None, portable: bool = False) -> None:
    """Publish a private snapshot; portable CLI exports remain mode 0644."""
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
        elif (source.name == "report.json" and not source.is_symlink() and retained == report
              and report._source_schema == CURRENT_SCHEMA):
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as outgoing, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, outgoing, length=65536)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        else:
            # Never chmod an inode shared with the source report. Managed
            # baselines are independent mode-0600 snapshots.
            _atomic_write_chunks(temporary, _report_json_chunks(retained))
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
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
        fsync_directory(destination.parent)
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


def _duration_ms(value: float) -> str:
    amount, unit = (value / 1000, "s") if value >= 1000 else (value, "ms")
    return f"{_number(amount).rstrip('0').rstrip('.')} {unit}"


def _badge(status: str) -> str:
    label = "Not applicable" if status == "not_applicable" else status
    return f'<span class="badge {_e(status)}">{_e(label)}</span>'


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


def _multi_user_scenario_export(window: dict[str, Any]) -> dict[str, Any]:
    result = {}
    if evidence := window.get("interference"):
        fields = ("status", "qualified", "control_window_id", "trigger", "heavy_profile", "heavy_user_ids", "reasons")
        user_fields = ("user_id", "status", "qualified", "reasons", "matched_samples", "control_request_ids",
            "probe_request_ids", "heavy_request_ids", "probe_heavy_request_ids", "slowdown",
            "first_answer_change_ms", "completion_change_ms",
            "stream_gap_change_ms", "output_tokens_change", "control_input_median_tokens", "probe_input_median_tokens",
            "control_output_median_tokens", "probe_output_median_tokens",
            "heavy_input_median_tokens", "heavy_output_median_tokens")
        result["interference"] = {key: evidence[key] for key in fields if key in evidence}
        result["interference"]["users"] = [{key: user[key] for key in user_fields if key in user}
                                             for user in evidence.get("users", [])]
    if evidence := window.get("cancellation"):
        result["cancellation"] = {key: evidence[key] for key in ("status", "selected_user_id", "backend_release") if key in evidence}
        for name in ("cancellation", "peer_continuation", "followup"):
            item = evidence.get(name, {})
            result["cancellation"][name] = {key: item[key] for key in ("status", "request_ids", "reasons") if key in item}
    return result


def _multi_user_export(data: dict[str, Any]) -> dict[str, Any]:
    """Export user plans and compact outcomes, never prompts or response bodies."""
    user_fields = ("user_id", "profile", "status", "reasons", "activated_ms", "ready_count",
        "dispatched_count", "request_ids", "outcomes", "known_output_tokens", "missing_usage_count",
        "output_tokens_per_second", "late_output_tokens", "late_missing_usage_count", "late_completed",
        "partial_output_tokens", "partial_missing_usage_count", "cancelled_output_tokens",
        "cancelled_missing_usage_count", "latency", "lengths", "finish_reasons", "pending_wait",
        "coverage", "criteria", "timing_quality", "stop_reason", "conversations", "think_time_plan")
    window_fields = ("id", "scenario", "population", "phase", "start_ms", "end_ms", "planned_end_ms",
        "duration_seconds", "planned_duration_seconds", "drain_seconds", "cut_short", "reason", "drain_reason", "status", "fairness",
        "role", "trigger", "heavy_profile", "heavy_user_ids", "control_window_id", "selected_user_id")
    value = {key: data.get(key) for key in ("version", "window_policy", "settings", "population",
                                          "status", "worst_user", "interpretation")}
    value["windows"] = [{**{key: window.get(key) for key in window_fields}, **_multi_user_scenario_export(window),
                         "users": [{key: user[key] for key in user_fields if key in user}
                                   for user in window.get("users", [])]}
                        for window in data.get("windows", [])]
    return sanitize(value, bounded=False)


def _multi_user_scenario_html(window: dict[str, Any], anchors: dict[str, str]) -> str:
    def links(ids):
        return ' '.join(f'<a href="#{anchors[rid]}">{_e(rid)}</a>' for rid in ids if rid in anchors)

    parts = []
    if evidence := window.get("interference"):
        parts.append(f'<h5>Matched noisy-neighbor comparison {_badge(evidence["status"])}</h5>'
            f'<p>{_e(evidence.get("trigger"))} · {_e(evidence.get("heavy_profile"))} · '
            f'heavy users: {_e(", ".join(evidence.get("heavy_user_ids", [])))}. '
            f'{_e(" ".join(evidence.get("reasons", [])))}</p>'
            '<div class="table"><table><tr><th>Short user / qualification</th><th>Matched samples / slowdown</th>'
            '<th>First answer / completion / stream gap change (ms)</th><th>Actual input / output medians (control → probe)</th>'
            '<th>Output change / request evidence</th></tr>')
        for user in evidence.get("users", []):
            parts.append(f'<tr><td>{_e(user["user_id"])} {_badge(user["status"])}<br>'
                f'{_e(" ".join(user.get("reasons", [])))}</td><td>{_number(user.get("matched_samples"))} / '
                f'{_number(user.get("slowdown"))}×</td><td>{_number(user.get("first_answer_change_ms"))} / '
                f'{_number(user.get("completion_change_ms"))} / {_number(user.get("stream_gap_change_ms"))}</td>'
                f'<td>{_number(user.get("control_input_median_tokens"))} → {_number(user.get("probe_input_median_tokens"))} / '
                f'{_number(user.get("control_output_median_tokens"))} → {_number(user.get("probe_output_median_tokens"))}'
                f'<br>Heavy input / output medians: {_number(user.get("heavy_input_median_tokens"))} / '
                f'{_number(user.get("heavy_output_median_tokens"))} tokens</td>'
                f'<td>{_number(user.get("output_tokens_change"))} tokens<br>Control: {links(user.get("control_request_ids", []))}'
                f'<br>Probe: {links(user.get("probe_request_ids", []))}<br>Heavy: {links(user.get("heavy_request_ids", []))}</td></tr>')
        parts.append('</table></div>')
    if evidence := window.get("cancellation"):
        parts.append(f'<h5>Selected request cancellation · {_e(evidence.get("selected_user_id"))} '
                     f'{_badge(evidence["status"])}</h5>')
        for key, label in (("cancellation", "Selected request cancellation"),
                           ("peer_continuation", "Peer continuation"), ("followup", "Same-user follow-up")):
            item = evidence.get(key, {})
            parts.append(f'<p><strong>{label}</strong> {_badge(item.get("status", "inconclusive"))} · '
                         f'{_e(" ".join(item.get("reasons", [])))} {links(item.get("request_ids", []))}</p>')
        parts.append('<p>Backend compute and memory release: unverified. Follow-up success only establishes client-observed service.</p>')
    return ''.join(parts)


def _multi_user_html(data: dict[str, Any], anchors: dict[str, str]) -> str:
    parts = ['<h3>Per-user shared-window delivery</h3>']
    worst = data.get("worst_user")
    if worst:
        parts.append(f'<p><strong>Worst served user: {_e(worst["user_id"])} at '
                     f'{_e(worst["population"])} users</strong> · {_e(worst["status"])} · '
                     f'{_number(worst.get("output_tokens_per_second"))} tok/s. '
                     f'{_e(" ".join(worst.get("reasons", [])))}</p>')
    parts.append(f'<p class="muted">{_e(data.get("interpretation", ""))}</p>')
    for window in data.get("windows", []):
        label = " · ".join(str(window[key]) for key in ("scenario", "role", "trigger", "heavy_profile") if window.get(key))
        parts.append(f'<h4>{_e(window["population"])} users · {_e(window["phase"])} · {_e(label)}</h4>'
                     f'<p>Common window: {_number(window.get("duration_seconds"))} / '
                     f'{_number(window.get("planned_duration_seconds"))} planned seconds. '
                     f'{_e(window.get("reason") or window.get("drain_reason") or "")}</p>')
        if window.get("phase") != "measurement":
            parts.append('<p>Warmup is excluded from per-user acceptance and delivered throughput.</p>')
            continue
        parts.append(_multi_user_scenario_html(window, anchors))
        fairness = window.get("fairness")
        if fairness:
            parts.append(f'<p><strong>Observed delivery fairness</strong> {_badge(fairness["status"])} · '
                f'median / worst ratio: {_number(fairness.get("ratio"))}; limit: {_number(fairness.get("threshold"))}. '
                f'Eligible users: {_e(", ".join(fairness.get("eligible_user_ids", [])) or "None")} / '
                f'{len(fairness.get("compared_user_ids", []))} compared.</p>'
                f'<p>{_e(" ".join(fairness.get("reasons", [])))}</p>')
        parts.append('<div class="table"><table><tr><th>User / profile</th><th>Outcome</th>'
                     '<th>Completed / sent / errors</th><th>Shared-window tok/s</th>'
                     '<th>Ready-to-first / completion p95 ms</th><th>Coverage / requests</th></tr>')
        for user in window.get("users", []):
            outcome, latency = user.get("outcomes", {}), user.get("latency", {})
            links = ' '.join(f'<a href="#{anchors[rid]}">{_e(rid)}</a>'
                             for rid in user.get("request_ids", []) if rid in anchors)
            for item in user.get("conversations", []):
                links += (f'<p>{_e(item["id"])} · {_e(item["mode"])} · '
                          f'{len(item["turns"])} / {item["planned_turns"]} turns. '
                          f'{_e(item.get("stop_reason") or "Completed")}</p>' + ' '.join(
                          f'<a href="#{anchors[rid]}">Turn {turn + 1}</a>'
                          for turn, rid in enumerate(item["turns"]) if rid in anchors))
            pending = user.get("pending_wait")
            notes = " ".join(user.get("reasons", []) + user.get("coverage", {}).get("reasons", []))
            if pending:
                notes += f' Client admission wait: {pending["wait_ms"]:.3g} ms ({pending["status"]}).'
            parts.append(f'<tr><td>{_e(user["user_id"])}<br>{_e(user["profile"])}</td>'
                f'<td>{_badge(user["status"])}<br>{_e(notes)}</td>'
                f'<td>{_e(outcome.get("completed", 0))} / {_e(outcome.get("attempted", 0))} / {_e(outcome.get("errors", 0))}</td>'
                f'<td>{_number(user.get("output_tokens_per_second"))}<br>Known tokens: {_e(user.get("known_output_tokens", 0))}; '
                f'missing usage: {_e(user.get("missing_usage_count", 0))}; late tokens: {_e(user.get("late_output_tokens", 0))} '
                f'(missing usage: {_e(user.get("late_missing_usage_count", 0))}); partial tokens: '
                f'{_e(user.get("partial_output_tokens", 0))} (missing usage: {_e(user.get("partial_missing_usage_count", 0))})</td>'
                f'<td>{_number(latency.get("ready_first_answer_ms", {}).get("p95"))} / '
                f'{_number(latency.get("ready_completion_ms", {}).get("p95"))}</td>'
                f'<td>{_e(user.get("coverage", {}).get("status", "unknown"))}'
                f'<details><summary>{len(user.get("request_ids", []))} associated requests</summary>'
                f'{links}<p>Full request records are in report.json.</p></details></td></tr>')
        parts.append('</table></div>')
    return ''.join(parts)


def _session_performance_html(data: dict[str, Any]) -> str:
    headline = (f'<p><strong>Average generation speed per user: '
                f'{_number(data["mean_user_generation_tokens_per_second"])} tok/s</strong> '
                f'({_e(data["users_with_measured_generation"])} measured users). '
                f'{_e(data["completed_conversations"])} / {_e(data["planned_conversations"])} '
                f'conversations completed; {_e(data["achieved_request_overlap"])} / '
                f'{_e(data["configured_concurrency"])} concurrent requests observed/configured.</p>'
                f'<p class="muted">{_e(data["interpretation"])}</p>')
    if data.get("sampling"):
        headline += (f'<details><summary>Sampled pauses and lengths · seed {_e(data["sampling"]["seed"])}</summary>'
                     f'<p>{_e(data["attempted"])} sent / {_e(data["planned_turns"])} planned turns. '
                     'Input targets and local counts cover the new user message only; server input usage includes history. '
                     'Actual pauses include dispatch wait. Output caps do not guarantee answer lengths.</p>'
                     '<div class="table"><table><tr><th>Conversation / turn</th><th>Requested / actual pause (s)</th>'
                     '<th>Input target / local new message</th><th>Server input / output tokens</th><th>Output cap</th></tr>')
        for row in data.get("sampled_requests", []):
            headline += (f'<tr><td>{_e(row["session_id"])} / {_e(row["turn"])}</td>'
                         f'<td>{_number(row["requested_pause_seconds"])} / {_number(row["actual_pause_seconds"])}</td>'
                         f'<td>{_e(row["input_target_tokens"])} / {_e((row.get("input_preparation") or {}).get("input_tokens"))}</td>'
                         f'<td>{_e(row["actual_input_tokens"])} / {_e(row["actual_output_tokens"])}</td>'
                         f'<td>{_e(row["output_cap_tokens"])}</td></tr>')
        headline += '</table></div></details>'
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
    delta_text = " No comparable capacity delta established."
    if delta is not None:
        percent = traffic.get("change_percent")
        delta_text = f" Change: {delta:+.4g} requests/s"
        delta_text += f" ({percent:+.1f}%)." if percent is not None else "."
    parts = [f'<h3>{_e(target.get("target", "Target"))} — tested arrival capacity</h3>',
             f'<p>{_badge(traffic.get("status", "inconclusive"))} {_e(traffic.get("reason", ""))}<br>'
             f'Highest acceptable tested rate: '
             f'{_e(_rps(traffic.get("baseline_highest_acceptable_rate_rps")))} → '
             f'{_e(_rps(traffic.get("current_highest_acceptable_rate_rps")))}.'
             f'{_e(delta_text)}</p>']
    if traffic.get("stages"):
        parts.append('<div class="table"><table><tr><th>Offered rate</th><th>Baseline / current</th>'
                     '<th>Cohort goodput, baseline / current</th><th>Finding</th></tr>')
        for stage in traffic["stages"]:
            before, after = stage.get("baseline") or {}, stage.get("current") or {}
            parts.append(
                f'<tr><td>{_e(_rps(stage["rate_rps"]))}</td>'
                f'<td>{_badge(before.get("status", "inconclusive"))} / {_badge(after.get("status", "inconclusive"))}</td>'
                f'<td>{_e(_rps(before.get("goodput_rps")))} / {_e(_rps(after.get("goodput_rps")))}</td>'
                f'<td>{_badge(stage.get("status", "inconclusive"))} {_e(stage.get("reason", ""))} '
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

    parts = [f'<h3>{_e(target.get("target", "Target"))} — tested load acceptance</h3>',
             f'<p>{_badge(comparison.get("status", "inconclusive"))} {_e(comparison.get("reason", ""))}</p>']
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
    scopes = {scope(point): point for row in comparison.get("points", [])
              if (point := row["current"] or row["baseline"]) and point.get("pattern") in {"concurrent", "constant", "poisson"}}
    for key, point in {**highest[0], **highest[1]}.items():
        scopes.setdefault(key, point)
    for key, point in scopes.items():
        values = [str(points[key]["value"]) if key in points else "Not established" for points in highest]
        label = _comparison_load_label({**point, "shape": point.get("shape") or (point.get("workload") if point.get("type") == "combinations" else None), "level": None, "rate": None})
        unit = "concurrency" if point["pattern"] == "concurrent" else "request rate"
        parts.append(f'<p>{_e(label)} · Highest accepted {unit} (tested) · '
                     f'Baseline: {_e(values[0])} · Current: {_e(values[1])}</p>')
    if comparison.get("points", []):
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
            change = (changes.get(row["acceptance_change"], "Not established") if "acceptance_change" in row
                      else "Recorded: " + row.get("status", "inconclusive").capitalize())
            parts.append(f'<td><strong>{_e(change)}</strong></td>'
                         f'<td>{_e(row["reason"])}</td></tr>')
        parts.append('</table></div>')
    return "".join(parts)


def _comparison_cohort(row):
    point = row.get("load_point")
    if point:
        return tuple(point.get(name) for name in
                     ("source", "type", "workload", "shape", "pattern", "level", "rate", "bursts", "phase", "role"))
    return row.get("scenario")


def _comparison_outputs(target):
    if "observed_outputs" in target:
        return target["observed_outputs"]
    outputs, seen = [], set()
    for row in target.get("comparisons", []):
        key = row.get("check"), _comparison_cohort(row)
        work = row.get("output_work")
        if key not in seen and work and work.get("current") and work.get("baseline"):
            seen.add(key)
            observations = _output_observations(row.get("check"), row.get("scenario"), work["current"], work["baseline"])
            if row.get("load_point"):
                for observation in observations:
                    observation["load_point"] = row["load_point"]
            outputs.extend(observations)
    return outputs


def _baseline_verdict(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("descriptive"):
        return "Observed result", "muted"
    if row.get("status") == "fail":
        return "Worse", "fail"
    if row.get("warnings"):
        return "Interpretation limited", "inconclusive"
    if row.get("status") != "pass":
        return "Not comparable", "inconclusive"
    if row.get("acceptance_improved"):
        return "Better", "pass"
    interval = row.get("worsening_interval_95") or []
    threshold = row.get("threshold")
    if (len(interval) == 2 and interval[1] is not None and threshold is not None
            and interval[1] <= -threshold and row.get("worsening", 0) < 0):
        return "Better", "pass"
    return "No material change", "muted"


def _baseline_display_rows(target: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [*target.get("comparisons", []), *_comparison_outputs(target)]
    traffic = target.get("traffic_capacity")
    if traffic:
        rows.append(dict(traffic, metric="Highest acceptable tested arrival rate (requests/s)",
                         baseline=traffic.get("baseline_highest_acceptable_rate_rps"),
                         current=traffic.get("current_highest_acceptable_rate_rps"),
                         acceptance_improved=(traffic.get("status") == "pass"
                             and isinstance(traffic.get("baseline_highest_acceptable_rate_rps"), (int, float))
                             and isinstance(traffic.get("current_highest_acceptable_rate_rps"), (int, float))
                             and traffic["current_highest_acceptable_rate_rps"] > traffic["baseline_highest_acceptable_rate_rps"])))
    load = target.get("load_test")
    if load:
        from .load_test import point_label

        for point in load.get("points", []):
            before, after = point.get("baseline") or {}, point.get("current") or {}
            rows.append(dict(point, metric="Load acceptance: " + point_label(after or before),
                             baseline=point.get("baseline_acceptance", before.get("status", "Unknown")),
                             current=point.get("current_acceptance", after.get("status", "Unknown")), check="load",
                             scenario=point_label(after or before),
                             acceptance_improved=(point.get("acceptance_change") == "gained"
                                 if "acceptance_change" in point else point.get("status") == "pass"
                                 and before.get("status") == "fail" and after.get("status") == "pass")))
        if not load.get("points"):
            rows.append(dict(load, metric="Tested load acceptance", baseline=None, current=None))
    if not rows:
        rows.append(dict(metric="Comparison unavailable", status="inconclusive",
                         reason=" ".join(target.get("reasons", []))))
    return rows


def _baseline_summary_html(baseline: dict[str, Any]) -> str:
    rows = [row for target in baseline.get("targets", [])
            for row in [*target.get("comparisons", []), *_comparison_outputs(target)]]
    warnings = sum(bool(row.get("warnings")) or row.get("status") == "inconclusive" for row in rows)
    summary = f"{len(rows)} measurements · {warnings} with warnings"
    return (f'<p class="baseline-summary"><a href="#baseline-heading">vs baseline '
            f'{_e(baseline.get("saved_baseline_name") or baseline.get("name") or baseline.get("run_id", "unknown"))}: {_e(summary)}</a></p>')


def _deployment_change_html(diff: dict[str, Any]) -> str:
    if not diff:
        return ""
    changed = [row["field"] for row in diff.get("fields", [])
               if row.get("change") != "no_observed_change"]
    intended = diff.get("intended_change")
    summary = ("Deployment change: " + ", ".join(changed) if changed
               else "No observed deployment changes.")
    if changed and intended and not diff.get("confounded"):
        summary += " (one intended change)"
    parts = [f'<p class="deployment-change">{_e(summary)}</p>']
    if diff.get("confounded"):
        parts.append('<p><strong>Confounded: multiple unrelated changes.</strong></p>')
        if intended:
            fields = intended if isinstance(intended, list) else [intended]
            parts.append(f'<p class="muted">Intended change: {_e(", ".join(fields))}</p>')
    if diff.get("unknown_baseline"):
        parts.append('<p class="muted">Baseline deployment configuration was not recorded.</p>')
    if diff.get("interpretation"):
        parts.append(f'<p class="muted">{_e(diff["interpretation"])}</p>')
    return "".join(parts)


def _condition_label(scenario: str, *, target: str | None = None,
                     report: RunReport | None = None, workload: dict[str, Any] | None = None) -> str:
    from .conditions import condition_display_name

    summaries = list(report.conditions or []) if report else []
    if report:
        summaries.extend({**row, "target": name} for name, observation in report.observations.get("targets", {}).items()
                         for row in observation.get("conditions", []))
    summary = next((row for row in summaries if (target is None or row.get("target", target) == target)
                    and (row.get("condition_id") or row.get("condition", {}).get("id")) == scenario), {})
    return condition_display_name(scenario, workload, condition=summary.get("condition"), name=summary.get("name"))


def _baseline_table_html(baseline: dict[str, Any], report: RunReport | None = None) -> str:
    conditions: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    notes = []
    seen = defaultdict(set)
    for target in baseline.get("targets", []):
        deployment = _deployment_change_html(target.get("configuration_diff") or {})
        if deployment:
            notes.append(f'<div><h3>{_e(target.get("target", "Target"))} · Deployment</h3>'
                         f'{deployment}</div>')
        rows = _baseline_display_rows(target)
        for row in rows:
            key = (target.get("target"), row.get("check"), _comparison_cohort(row))
            fingerprint = json.dumps([row.get(name) for name in ("metric", "baseline", "current", "status", "reason")], sort_keys=True)
            if fingerprint not in seen[key]:
                seen[key].add(fingerprint)
                conditions[key].append(row)
        traffic = target.get("traffic_capacity") or {}
        if traffic.get("delta_rps") is not None:
            delta = f'Change: {traffic["delta_rps"]:+.4g} requests/s'
            if traffic.get("change_percent") is not None:
                delta += f' ({traffic["change_percent"]:+.1f}%)'
            notes.append(f'<p class="muted">{_e(target.get("target", "Target"))} · '
                         f'Tested arrival capacity · {_e(delta)}</p>')
        load = target.get("load_test") or {}
        if load.get("points") and load.get("reason"):
            notes.append(f'<p class="muted">{_e(target.get("target", "Target"))} · '
                         f'Load acceptance: {_e(load["reason"])}</p>')

    def table(rows):
        reason_column = any(row.get("warnings") or (row.get("reason") and row["reason"] !=
                            "No material worsening detected in these samples.")
                            for row in rows)
        parts = ['<div class="table"><table class="baseline-table"><thead><tr>'
                 '<th>Metric</th><th>Baseline</th><th>Current</th><th>Observed change</th><th>Interpretation</th>'
                 + ('<th>Reason</th>' if reason_column else '') + '</tr></thead><tbody>']
        for row in rows:
            label = str(row.get("metric", "Metric")).replace("_", " ")
            label = label[:1].upper() + label[1:]
            before, after = row.get("baseline"), row.get("current")
            rate = row.get("change_unit") == "percentage_points"

            def value(number):
                if number is None:
                    return "—"
                if isinstance(number, str):
                    return _e(number.replace("_", " ").capitalize())
                return f"{number * 100:.1f}%" if rate else _number(number)

            change = "—"
            if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                if rate:
                    change = f"{(after - before) * 100:+.1f} pp"
                elif before:
                    change = f"{(after - before) * 100 / abs(before):+.1f}%"
                elif after == before:
                    change = "+0.0%"
                else:
                    metric = row.get("metric", "")
                    suffix = (" ms" if metric.endswith("_ms") else
                              " tok/s" if metric.endswith("tokens_per_second") else
                              " tokens" if metric == "output_tokens" else
                              " chars" if metric == "output_chars" else "")
                    change = f"{after - before:+,.1f}{suffix} · from zero"
            verdict, tone = _baseline_verdict(row)
            parts.append(f'<tr><th scope="row">{_e(label)}</th>'
                         f'<td>{value(before)}</td><td>{value(after)}</td>'
                         f'<td class="{tone}">{_e(change)}</td>'
                         f'<td><strong class="{tone}">{verdict}</strong></td>')
            if reason_column:
                reason = row.get("reason", "")
                if reason == "No material worsening detected in these samples.":
                    reason = ""
                warnings = [warning["message"] for warning in row.get("warnings", [])]
                if warnings:
                    reason = " ".join(warnings)
                parts.append(f'<td class="muted">{_e(reason)}</td>')
            parts.append('</tr>')
        parts.append('</tbody></table></div>')
        return ''.join(parts)

    parts = notes
    # Conditions with changed acceptance appear before conditions without changes.
    ordered = sorted(conditions.items(), key=lambda item: min(
        {"Worse": 0, "Better": 1, "Interpretation limited": 2, "No material change": 3, "Not comparable": 4, "Observed result": 5}[
            _baseline_verdict(row)[0]] for row in item[1]))
    for condition, entries in ordered:
        row = entries[0]
        context = ((_comparison_load_label(row['load_point']) if row.get('load_point') else row.get('scenario_label'))
                   or _condition_label(row.get('condition_id') or row.get('scenario') or row.get('check') or condition[2] or 'Run comparison',
                                       target=condition[0], report=report))
        label = ' · '.join(str(value) for value in (condition[0], CHECK_NAMES.get(condition[1], condition[1]), context) if value)
        parts.append(f'<div class="baseline-condition"><h3>{_e(label or "Run comparison")}</h3>')
        for verdict, title in (("Worse", "Worse"), ("Better", "Better"),
                               ("Interpretation limited", "Warnings"), ("No material change", "Unchanged"),
                               ("Not comparable", "Not comparable"), ("Observed result", "Observed outputs")):
            rows = [row for row in entries if _baseline_verdict(row)[0] == verdict]
            if not rows:
                continue
            if verdict in {"No material change", "Not comparable"}:
                parts.append(f'<details class="baseline-group"><summary>{title} ({len(rows)})</summary>'
                             f'{table(rows)}</details>')
            else:
                parts.append(f'<h4>{title} ({len(rows)})</h4>{table(rows)}')
        parts.append('</div>')
    return ''.join(parts)


def _request_prompt_excerpt(record: RequestRecord) -> str:
    messages = record.evaluation.messages if record.evaluation else []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if isinstance(content, str) and content.strip():
            text = " ".join(content.split())
            return text[:80] + ("…" if len(text) > 80 else "")
    return record.fixture_id


def _response_saving_note(record: RequestRecord, report: RunReport) -> str:
    policy = report.manifest.get("config", {}).get("retention", "failures")
    if record.output and record.output_chars > len(record.output):
        return "Response truncated"
    if record.output or record.tool_calls:
        return ""
    if record.status in {"failed", "timeout"} and not record.output_chars:
        return "No response received" + (f": {record.error}" if record.error else "")
    if policy == "none" and not record.valid and record.output_chars == 0:
        return ""
    if policy == "none":
        return "Response not saved (retention: none)"
    omitted = report.manifest.get("storage", {}).get("response_bodies_omitted")
    if omitted and (record.output_chars or record.valid or record.tool_evaluation):
        return "Response not saved (storage limit)"
    if omitted and record.evaluation and (record.evaluation.scorer == "tool" or record.evaluation.tool_expectation):
        return "Response evidence unavailable (storage limit)"
    if record.output_chars or record.answer_chars or (policy == "failures" and record.valid):
        return f"Response not saved (retention: {policy})"
    return ""


def _received_answer(record: RequestRecord, report: RunReport) -> str:
    if record.output:
        return record.output
    if record.tool_calls:
        calls = []
        for call in record.tool_calls:
            function = call.get("function") or call
            arguments = function.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            calls.append(f'{function.get("name", "Unknown tool")}({arguments})')
        return "Tool calls: " + "; ".join(calls)
    return _response_saving_note(record, report) or ("Empty response" if record.status == "completed" else "No response recorded")


def _answer_findings_html(check: CheckResult, records: list[RequestRecord], report: RunReport) -> str:
    matches = [r for r in records if r.target == check.target and check.id in r.check_ids
               and (r.score is False or r.evaluation_error) and r.evaluation][:3]
    parts = []
    for record in matches:
        evidence = record.evaluation
        if evidence.expected_provided:
            expected = evidence.expected
            expected = expected if isinstance(expected, str) else json.dumps(expected, ensure_ascii=False)
            criterion = "Expected"
        elif evidence.schema_ is not None:
            criterion, expected = "JSON schema", json.dumps(evidence.schema_, ensure_ascii=False)
        elif evidence.rubric:
            criterion, expected = "Rubric", evidence.rubric
        elif evidence.tool_expectation is not None:
            criterion, expected = "Tool expectation", json.dumps(evidence.tool_expectation.model_dump(), ensure_ascii=False)
        else:
            criterion, expected = "Answer check", evidence.scorer
        received = _received_answer(record, report)
        saving_note = _response_saving_note(record, report)
        received_html = _preview(received) if record.output or record.tool_calls else f'<span class="muted">{_e(received)}</span>'
        excerpt = (f'{criterion}: {expected[:160]}{"…" if len(expected) > 160 else ""} · '
                   f'Received: {received[:160]}{"…" if len(received) > 160 else ""}')
        reason = record.evaluation_error or record.score_message
        check_name = CHECK_NAMES.get(check.id, check.id)
        condition = _condition_label(record.scenario, target=record.target, report=report, workload=record.workload)
        if condition != check_name:
            condition = f'{check_name} · {condition}'
        parts.append(f'<details class="answer-finding"><summary>{_e(_request_prompt_excerpt(record))}'
                     f' · {_e(excerpt)}</summary><p class="muted">{_e(condition)} · {_e(record.id)}</p>'
                     + (f'<p>{_e(reason)}</p>' if reason else '') +
                     f'<p><strong>{criterion}:</strong> {_preview(expected)}</p>'
                     f'<p><strong>Received:</strong> {received_html}</p>'
                     + (f'<p class="muted">{_e(saving_note)}</p>' if saving_note and (record.output or record.tool_calls) else '')
                     + '</details>')
    return ''.join(parts)


def _json_export_html(report: RunReport) -> str:
    return ('<p class="muted"><a href="report.json">JSON export</a> · '
            'report.json — keep it next to this file</p>')


def _check_timing_violation_ids(check: CheckResult) -> set[str]:
    # Current capacity reports store IDs once per stage; older reports aggregate them.
    sources = [check.metrics.get("violations", []),
               *(stage.get("violations", []) for stage in check.metrics.get("traffic_stages", []))]
    return {rid for source in sources if isinstance(source, list) for rid in source if isinstance(rid, str)}


def _render(report: RunReport) -> str:
    from .performance import build_performance, refresh_performance
    from .performance_html import STYLE, render_performance

    report = refresh_performance(report.model_copy())
    evidence = {rid for check in report.checks if check.status in {"fail", "inconclusive", "blocked"}
                for rid in check.evidence_ids}
    timing_failures = {rid for check in report.checks if check.status == "fail"
                       for rid in _check_timing_violation_ids(check)}
    evidence.update(timing_failures)
    ordered = sorted(report.requests, key=lambda r: (
        r.id not in evidence,
        not (r.score is False or r.status in {"failed", "timeout"} or r.evaluation_error or r.id in timing_failures)))
    detailed = ordered[:max(DEFAULT_HTML_REQUEST_DETAILS, sum(r.id in evidence for r in report.requests))]
    anchors = {record.id: f"request-{index}" for index, record in enumerate(detailed)}
    config = report.manifest.get("config", {})
    disconnected = {target["name"]: summary for target in config.get("targets", [])
                    if (summary := connection_summary(
                        [r for r in report.requests if r.target == target["name"]], target.get("url", target["name"])))}
    verdict = {"pass": "Passed", "fail": "Failed", "inconclusive": "Inconclusive",
               "partial": "Partial results"}.get(_acceptance_status(report), _acceptance_status(report).capitalize())
    parts = ["<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">",
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f"<title>Giraffe — {_e(report.run_id)}</title>", '<style>' + STYLE + '.load-acceptance-export{max-width:100%;overflow-x:auto}.load-acceptance-export table{min-width:760px}.load-acceptance-export th:nth-child(2),.load-acceptance-export th:nth-child(3),.load-acceptance-export td:nth-child(2),.load-acceptance-export td:nth-child(3){white-space:nowrap;min-width:140px}.load-acceptance-export small{display:block;white-space:normal}' + '</style><main>',  f"<h1>{_e(verdict)}</h1>",
             f'<p class="muted">Run {_e(report.run_id)} · suite {_e(report.suite_version)}<br>'
             f'{_e(report.started_at)} → {_e(report.finished_at)}</p>']
    for summary in disconnected.values():
        parts.append(f'<p class="card"><strong>{_e(summary)}</strong></p>')
    if report.abort_reason:
        parts.append(f'<p class="card">Run stopped: {_e(report.abort_reason)}</p>')
    for target in config.get("targets", []):
        parts.append(f'<p class="muted"><strong>{_e(target.get("name", ""))}</strong> · '
                     f'{_e(target.get("route", "unknown"))} · Model: <code>{_e(target.get("model", ""))}</code>'
                     f'<br>Endpoint: <code>{_e(target.get("url", ""))}</code></p>')
    parts.append('<nav aria-label="Report sections"><a href="#checks">Checks</a> · '
                 '<a href="#baseline">Baseline comparison</a> · <a href="#html-performance">Performance</a> · '
                 '<a href="#requests">Request evidence</a> · <a href="#reproduction">Reproduce</a></nav>')
    parts.append(_json_export_html(report))
    if report.baseline:
        parts.append(_baseline_summary_html(report.baseline))
    parts.append('<div id="html-results" aria-label="Results"><section id="findings"><h2>Findings</h2>')
    findings = [check for check in report.checks if check.status in {"fail", "inconclusive", "blocked"}]
    for check in findings:
        parts.append(f'<article class="card"><strong>{_e(CHECK_NAMES.get(check.id, check.id))}</strong> · {_e(check.target)}'
                     f'<p>{_e(check.summary)}</p>{_answer_findings_html(check, report.requests, report)}</article>')
    if not findings:
        parts.append('<p>All selected checks passed.</p>' if report.overall == "pass"
                     else '<p>No check findings were recorded.</p>')
    parts.append('</section>')
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
    selected_checks = set(config.get("checks", []))
    missing = [*report.warnings, *(f'{CHECK_NAMES.get(check.id, check.id)}: {check.summary}' for check in report.checks
                if check.status in {"blocked", "skipped"}
                and (check.required or check.id in selected_checks))]
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
        if check.id == "multi_user" and check.metrics.get("multi_user"):
            metrics = _multi_user_html(check.metrics["multi_user"], anchors) + metrics
        if check.id == "sessions" and check.metrics.get("conversation_performance"):
            metrics = _session_performance_html(check.metrics["conversation_performance"]) + metrics
        applied = ((report.manifest.get("effective_checks") or {}).get(check.id) or {}).get("limits") or {}
        default_limits = (report.manifest.get("default_check_limits") or {}).get(check.id) or []
        timing = {"first_output": [("first_output_ms", "p95 first useful output")],
                  "generation": [("latency_ms", "Response time"), ("stream_gap_ms", "Stream pause")]}
        limit_text = "; ".join(f"{label} ≤ {_duration_ms(applied[field])}" + (" (default)" if field in default_limits else "")
                               for field, label in timing.get(check.id, []) if applied.get(field) is not None)
        if limit_text:
            metrics = f'<p>{_e(limit_text)}</p>' + metrics
        parts.append(f'<tr><td>{_e(check.target)}<br><strong>{_e(CHECK_NAMES.get(check.id, check.id))}</strong>'
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
    parts.append('</section><section id="baseline"><h2 tabindex="-1" id="baseline-heading">Baseline comparison</h2>')
    if report.baseline:
        acceptance = report.baseline.get("acceptance", {})
        parts.append('<h3>What changed?</h3><p>Observed differences do not prove what caused them.</p>'
                     f'<p>Compared with {_e(report.baseline.get("saved_baseline_name") or report.baseline.get("run_id", "unknown"))}.</p>'
                     '<h3>Did each run meet its requirements?</h3>'
                     f'<p>Baseline: {_e(acceptance.get("baseline", "Not recorded separately"))} · '
                     f'Current: {_e(acceptance.get("current", "Not recorded separately"))}. '
                     'Pass/fail checks configured requirements; measured changes appear below.</p>')
        parts.append(_baseline_table_html(report.baseline, report))
        for target in report.baseline.get("targets", []):
            if target.get("traffic_capacity") or target.get("load_test") or target.get("load_exclusions"):
                parts.append(f'<details><summary>{_e(target.get("target", "Target"))} · '
                             'Tested load and arrival details</summary>')
                if target.get("traffic_capacity"):
                    parts.append(_traffic_baseline_html(target))
                if target.get("load_test") or target.get("load_exclusions"):
                    parts.append(_load_baseline_html(target))
                parts.append('</details>')
        parts.append(f'<details><summary>Changes, comparability and metric evidence</summary>'
                     f'<pre>{_json(report.baseline)}</pre></details>')
    else:
        parts.append('<p>No baseline supplied. This run reports absolute checks only.</p>')
    parts.append('</section></div><div id="html-performance" aria-label="Performance">')
    for entry in report.performance["available_targets"]:
        selected = (report.performance if entry["target"] == report.performance["selected_target"]
                    else build_performance(report, target=entry["target"]))
        parts.append(render_performance(selected, prefix="performance-" + str(len(parts))))
    if not report.performance["available_targets"]:
        parts.append(render_performance(report.performance))
    parts.append('</div>')
    parts.append('<section id="measurements"><h2>Measurements by scenario</h2>'
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
        parts.append(f'<tr><td>{_e(target)} / {_e(_condition_label(scenario, target=target, report=report, workload=records[0].workload))}</td><td>{len(records)}</td><td>{len(valid)}</td>'
                     f'<td>{sum(r.status == "failed" for r in records)} / '
                     f'{sum(r.status == "timeout" for r in records)}</td>'
                     f'<td>{sum(r.status == "cancelled" for r in records)}</td>'
                     f'<td>{_number(percentile(times, .5))} / {_number(percentile(times, .95))}</td>'
                     f'<td>{_number(percentile(first, .5))}</td>'
                     f'<td>{_number(percentile(generation, .5))} (n={len(generation)})</td>'
                     f'<td>{_number(percentile(end_to_end, .5))}</td>'
                     f'<td>{sum(r.score is True for r in scored)} / {len(scored)}</td></tr>')
    parts.append('</table></div></section>')
    parts.append('<section id="deployment"><h2>Deployment and serving telemetry</h2>'
                 '<p>Deployment changes provide investigation context and do not establish causality. '
                 'Unknown fields and telemetry remain unknown. Client first output is separate from server TTFT.</p>')
    for name, observation in report.observations.get("targets", {}).items():
        residency = (observation.get("deployment") or {}).get("model_residency", {})
        if residency:
            parts.append(f'<h3>{_e(name)}: model residency</h3>'
                         '<p>Observed around this run’s preparation. Residency does not prove a cold start or explain latency.</p>')
            for phase, label in (("before_preparation", "Before preparation"), ("after_preparation", "After preparation")):
                row = residency.get(phase, {})
                parts.append(f'<p><strong>{label}:</strong> {_e(row.get("state", "unknown"))} · '
                             f'{_e(row.get("source") or "No runtime source")} · {_e(row.get("collected_at") or "Not observed")} · '
                             f'{_e(row.get("scope", "unknown"))}</p>')
        parts.append(f'<h3>{_e(name)}</h3><details><summary>Deployment snapshot and unknowns</summary>'
                     f'<pre>{_json(observation.get("deployment"))}</pre></details>'
                     f'<details><summary>Deployment observations during the run</summary>'
                     f'<pre>{_json(observation.get("deployment_history", []))}</pre></details>'
                     f'<details><summary>Serving telemetry, phases and collection coverage</summary>'
                     f'<pre>{_json(observation.get("serving_telemetry"))}</pre></details>')
    parts.append('</section>')
    parts.append('<section id="requests"><h2>Request evidence</h2>'
                 f'<p class="muted">Response retention: {_e(config.get("retention", "failures"))}. '
                 'Retention controls response text; request metadata, timings, status and scores are always saved. '
                 'Retention does not cap disk usage; storage limits do.</p>'
                 f'<p>Showing {len(detailed)} of {len(report.requests)} request details, failures first. '
                 '<a href="report.json">All requests and complete retained bodies in report.json</a>.</p>')
    input_previews = {}
    for record in detailed:
        label = f"{_request_prompt_excerpt(record)} · {record.target} · {_condition_label(record.scenario, target=record.target, report=report, workload=record.workload)} · {record.status}"
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
        elif record.tool_calls:
            parts.append(f'<h3>Tool response</h3><pre>{_preview(_received_answer(record, report))}</pre>')
        else:
            parts.append(f'<p class="muted">{_e(_received_answer(record, report))}</p>')
        if record.output and (saving_note := _response_saving_note(record, report)):
            parts.append(f'<p class="muted">{_e(saving_note)}</p>')
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
    parts.append('</section></main></html>')
    return "".join(parts)


def _atomic_write_chunks(path: Path, chunks) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.name + ".",
                                         suffix=".tmp", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            for chunk in chunks:
                file.write(chunk)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write(path: Path, contents: str) -> None:
    if path.name == ".web.json":
        write_metadata(path.parent, json.loads(contents))
    else:
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
    if getattr(error, "failed_file", "report.html") != "report.html":
        return f"Report cleanup could not finish: {_write_failure_reason(error)}; report.json is complete"
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
                 html: bool = False, preserve_evidence: bool = False) -> tuple[Path, Path | None]:
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

    try:
        update_metadata(directory, {"latency_summary": latency_summary(report)})
    except Exception as error:
        logger.exception("Could not save latency summary for %s", report.run_id)
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
    # The completed, durably published report now owns the evidence. A crash
    # before this point must leave the journal available for recovery.
    try:
        if not preserve_evidence:
            (directory / "evidence.jsonl").unlink(missing_ok=True)
        fsync_directory(directory)
    except OSError as error:
        raise ReportWriteError(error, "evidence.jsonl", json_complete=True) from error
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
            **({"multi_user": {check.target: _multi_user_export(check.metrics["multi_user"])
                               for check in retained.checks
                               if check.id == "multi_user" and check.metrics.get("multi_user")}}
               if any(check.id == "multi_user" and check.metrics.get("multi_user") for check in retained.checks) else {}),
            "deployments": {name: obs.get("deployment") for name, obs in retained.observations.get("targets", {}).items()},
            "deployment_history": {name: obs.get("deployment_history", []) for name, obs in retained.observations.get("targets", {}).items()},
            "instructions": ["Supply secret environment-variable references locally.",
                "Match suite and fixture hashes; supply any local fixture/metadata files.",
                "Verify unknown server settings manually. This export cannot recreate inaccessible server state.",
                "Imported launch arguments describe intent and are never executed by this export.",
                "A saved failed run is a reference, not an approved deployment."]}
