"""Compare like-for-like runs and write a portable, local results document."""

from __future__ import annotations

from collections import Counter, defaultdict
from fractions import Fraction
from html import escape
import json
import math
from pathlib import Path
import random
from statistics import median
import tempfile
from typing import Any

from .deployment import deployment_diff, safe_config, sanitize
from .models import CHECK_NAMES, CheckResult, RequestRecord, RunReport, overall_status
from .workloads import WORKLOAD_CHECKS


# Identities are deliberately absent: changing a runtime/image is a reason to run the suite.
_MATCH_CONFIG = (
    "concurrency", "max_requests", "max_duration_seconds", "request_timeout_seconds",
    "max_output_tokens", "context_limit", "samples", "sustained_seconds", "checks",
    "structured_json", "overlap_models", "stream", "request_options", "stop_after_errors",
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
                and record.scenario not in {"client_cancel", "client_deadline"}):
            for check in record.check_ids:
                groups[(check, record.scenario)].append(record)
    return groups


def _settings_reasons(report: RunReport, baseline: RunReport) -> list[str]:
    reasons = []
    if report.suite_version != baseline.suite_version:
        reasons.append("Suite versions differ.")
    if report.schema_version != baseline.schema_version:
        reasons.append("Report schema versions differ.")
    if report.abort_reason:
        reasons.append("Current run was aborted; incomplete traffic cannot establish a regression.")
    if baseline.abort_reason:
        reasons.append("Baseline run was aborted; it cannot establish a complete reference.")
    current_config = report.manifest.get("config", {})
    old_config = baseline.manifest.get("config", {})
    selected = set(current_config.get("checks", [])) | set(old_config.get("checks", []))
    workload_fields = [key for key, checks in WORKLOAD_CHECKS.items() if checks & selected]
    for key in (*_MATCH_CONFIG, *workload_fields):
        if key not in old_config or key not in current_config:
            reasons.append(f"Recorded {key} is missing; comparability is unknown.")
        elif old_config[key] != current_config[key]:
            reasons.append(f"Run setting {key} differs.")
    traffic_selected = any("capacity" in config.get("checks", []) for config in (current_config, old_config))
    for key, effective in (("traffic", "effective_traffic"), ("test_options", "effective_checks")):
        if key == "traffic" and not traffic_selected:
            continue
        if effective in report.manifest and effective in baseline.manifest:
            continue  # Saved drafts and explicit defaults do not change executed work.
        if key in current_config or key in old_config:
            if key not in current_config or key not in old_config:
                reasons.append(f"Recorded {key} is missing; comparability is unknown.")
            elif current_config[key] != old_config[key]:
                reasons.append(f"Run setting {key} differs.")
    for key in ("effective_checks", "effective_traffic"):
        if key == "effective_traffic" and not traffic_selected:
            continue
        if key in report.manifest or key in baseline.manifest:
            if report.manifest.get(key) != baseline.manifest.get(key):
                reasons.append(f"Recorded {key} differs or is missing.")
    # The suite version identifies bundled fixtures; custom packs also need a content identity.
    current_pack = report.manifest.get("fixture_pack")
    old_pack = baseline.manifest.get("fixture_pack")
    if not isinstance(current_pack, dict) or not isinstance(old_pack, dict) or not current_pack or not old_pack:
        reasons.append("Fixture pack identity is missing or unknown.")
    elif ({key: value for key, value in current_pack.items() if key != "custom"}
          != {key: value for key, value in old_pack.items() if key != "custom"}):
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
        elif report.manifest[key] != baseline.manifest[key]:
            reasons.append(f"{key} differs.")
    return reasons


def _generator_saturated(report: RunReport, target: str) -> bool:
    warm_first = [r.first_output_ms for r in report.requests if r.target == target
                  and r.scenario == "warm" and r.status == "completed" and r.valid
                  and r.first_output_ms is not None]
    threshold = max(100, (_percentile(warm_first, .5) or 0) * .1)
    return report.observations.get("generator_max_lag_ms", 0) > threshold


def _metric_values(records: list[RequestRecord]) -> dict[str, list[float]]:
    completed = [r for r in records if r.status == "completed" and r.valid]
    return {
        "valid_completion_rate": [float(r.status == "completed" and r.valid) for r in records],
        "error_rate": [float(r.status in {"failed", "timeout"}) for r in records],
        "correctness_rate": [float(r.score) for r in records if r.score is not None],
        "latency_ms": [r.elapsed_ms for r in completed],
        "first_output_ms": [r.first_output_ms for r in completed if r.first_output_ms is not None],
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
    if min(len(current), len(old)) < minimum:
        result["reason"] = f"Needs at least {minimum} measured samples in each run."
        return result
    is_rate = name.endswith("_rate")
    aggregate = (lambda data: sum(data) / len(data)) if is_rate else median
    current_value, old_value = aggregate(current), aggregate(old)
    higher_is_better = name in {"correctness_rate", "valid_completion_rate", "output_tokens_per_second",
                               "generation_tokens_per_second"}
    direction = -1 if higher_is_better else 1
    # Rates use percentage points, avoiding undefined percentages for a zero baseline error rate.
    scale = 100 if is_rate else (100 / old_value if old_value > 0 else None)
    result.update(current=current_value, baseline=old_value,
                  aggregation="mean" if is_rate else "median",
                  change_unit="percentage_points" if is_rate else "percent")
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
    if reasons:
        result["reason"] = "Traffic comparison is unavailable: " + " ".join(reasons)
        return result
    if current is None or old is None:
        result["reason"] = "One run lacks arrival-stage evidence; legacy concurrency is not arrival capacity."
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


def compare_baseline(report: RunReport, baseline: RunReport) -> RunReport:
    """Return a new report with explicit baseline evidence; never mutate or promote a baseline."""
    result = report.model_copy(deep=True)
    result.checks = [check for check in result.checks if check.id != "baseline"]
    reasons = _settings_reasons(report, baseline)
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
        traffic = _compare_traffic(report, baseline, name, target_reasons, threshold)
        current_groups, old_groups = _groups(report.requests, name), _groups(baseline.requests, name)
        if (not current_groups or not old_groups) and traffic is None:
            target_reasons.append("Measured request samples are absent from one or both runs.")
        if not target_reasons:
            for check, scenario in sorted(set(current_groups) | set(old_groups)):
                current_records = current_groups.get((check, scenario), [])
                old_records = old_groups.get((check, scenario), [])
                # Requested shape is checked too, rather than relying on configuration alone.
                def shapes(records: list[RequestRecord]) -> dict[tuple, Fraction]:
                    counts = Counter((r.fixture_id, r.input_chars, r.requested_max_tokens, r.stream)
                                     for r in records)
                    return {shape: Fraction(count, len(records)) for shape, count in counts.items()}
                if not current_records or not old_records or shapes(current_records) != shapes(old_records):
                    target_reasons.append(f"{check}/{scenario}: missing or different request shapes or proportions.")
                    continue
                current_metrics, old_metrics = _metric_values(current_records), _metric_values(old_records)
                current_work, old_work = _output_work(current_records), _output_work(old_records)
                different_work = _different_output_work(current_work, old_work, threshold)
                current_inputs = [r.input_tokens for r in current_records if r.valid and r.input_tokens is not None]
                old_inputs = [r.input_tokens for r in old_records if r.valid and r.input_tokens is not None]
                input_work_changed = bool(current_inputs and old_inputs and median(old_inputs)>0 and abs(median(current_inputs)-median(old_inputs))*100/median(old_inputs)>threshold)
                live_inputs_differ = any(r.workload.get("mode")=="live" for r in current_records) and Counter(r.request_hash for r in current_records) != Counter(r.request_hash for r in old_records)
                for metric, values in current_metrics.items():
                    if not values and not old_metrics[metric]:
                        continue
                    row = _compare_metric(metric, values, old_metrics[metric], minimum, threshold)
                    if (input_work_changed or live_inputs_differ) and metric in {"latency_ms", "first_output_ms", "max_stream_gap_ms", "output_tokens_per_second", "generation_tokens_per_second"}:
                        row.update(status="inconclusive", reason="Observed input work or live generated histories differ; timing is not like-for-like.")
                    if different_work and metric in {"latency_ms", "max_stream_gap_ms", "output_tokens_per_second", "generation_tokens_per_second"}:
                        row.update(status="inconclusive", reason="Actual generated output lengths differ "
                                   "materially; these performance samples are not like-for-like.")
                    if saturated_runs and metric in {"latency_ms", "first_output_ms", "max_stream_gap_ms", "output_tokens_per_second", "generation_tokens_per_second"}:
                        row.update(status="inconclusive", reason=" / ".join(saturated_runs) +
                                   " generator scheduling lag prevents a reliable timing/rate comparison.")
                    row.update(check=check, scenario=scenario,
                               output_work={"current": current_work, "baseline": old_work},
                               evidence_ids=[record.id for record in current_records])
                    rows.append(row)
        assessed = [row for row in rows if "worsening" in row]
        failures = [row for row in assessed if row["status"] == "fail"]
        uncertain = [row for row in assessed if row["status"] == "inconclusive"]
        missing_measurements = [row for row in rows if "worsening" not in row and
                                max(row["current_samples"], row["baseline_samples"]) >= minimum]
        traffic_failures = [row for row in (traffic or {}).get("stages", []) if row["status"] == "fail"]
        if failures or traffic_failures:
            status = "fail"
            summary = (f"{len(failures)} material metric regression(s) and "
                       f"{len(traffic_failures)} lost acceptable traffic rate(s) against {baseline.run_id}.")
            if target_reasons:
                summary += " Other scenarios could not be compared: " + " ".join(target_reasons)
        elif target_reasons:
            status = "inconclusive"
            summary = "Baseline comparison is inconclusive: " + " ".join(target_reasons)
        elif traffic is not None and traffic["status"] == "inconclusive":
            status = "inconclusive"
            summary = traffic["reason"]
        elif (not assessed and traffic is None) or uncertain or missing_measurements:
            status = "inconclusive"
            if not assessed:
                summary = "No metric has enough comparable samples."
            elif missing_measurements:
                summary = "Some metrics lost measurement coverage or have undefined baseline values."
            elif saturated_runs:
                summary = "Generator scheduling lag prevents a reliable timing/rate comparison."
            else:
                summary = "Sample variation or changed output lengths prevent a clear material-regression decision."
        else:
            status = "pass"
            summary = f"No material regression detected in {len(assessed)} comparable metric(s)."
            if traffic is not None:
                summary += " " + traffic["reason"]
        excluded = len(rows) - len(assessed)
        if excluded:
            summary += f" {excluded} low-sample or undefined metric(s) excluded; see comparison details."
        entry = {"target": name, "status": status, "reasons": target_reasons,
                 "generator_saturated_runs": saturated_runs,
                 "identity_changes": identity_changes, "configuration_diff": configuration_diff,
                 "consistency_changes": consistency_changes,
                 "telemetry": {"current": report.observations.get("targets", {}).get(name, {}).get("serving_telemetry"),
                               "baseline": baseline.observations.get("targets", {}).get(name, {}).get("serving_telemetry")},
                 "comparisons": rows}
        if traffic is not None:
            entry["traffic_capacity"] = traffic
        comparison["targets"].append(entry)
        result.checks.append(CheckResult(
            id="baseline", target=name, title="Baseline comparison", required=True,
            status=status, summary=summary,
            metrics={"compared_metrics": len(assessed), "excluded_metrics": excluded,
                     "regressions": len(failures) + len(traffic_failures),
                     "baseline_run_id": baseline.run_id},
            evidence_ids=list(dict.fromkeys(rid for row in failures + traffic_failures
                                            for rid in row["evidence_ids"])),
        ))
    baseline_checks = [check for check in result.checks if check.id == "baseline"]
    comparison["status"] = overall_status(baseline_checks)
    result.baseline = comparison
    result.overall = overall_status(result.checks, result.abort_reason)
    return result


def _retained(report: RunReport) -> RunReport:
    result = report.model_copy(deep=True)
    result.manifest["config"] = safe_config(result.manifest.get("config", {}))
    if result.baseline:
        result.baseline = sanitize(result.baseline, bounded=False)
    policy = result.manifest.get("config", {}).get("retention", "failures")
    failed_evidence = {rid for check in result.checks if check.status in {"fail", "blocked"}
                       for rid in check.evidence_ids}
    for record in result.requests:
        keep = policy == "all" or (policy == "failures" and (
            (record.status in {"failed", "timeout"} and record.scenario not in {"client_cancel", "client_deadline"})
            or (record.status == "completed" and not record.valid) or record.score is False
            or record.id in failed_evidence
        ))
        if not keep:
            record.output = ""
            record.reasoning = ""
            record.tool_calls = []
    return result


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _json(value: Any) -> str:
    return _e(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _number(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:,.1f}"


def _badge(status: str) -> str:
    return f'<span class="badge {_e(status)}">{_e(status)}</span>'


def _rps(value: float | None) -> str:
    return "not established" if value is None else f"{value:,.4g} requests/s"


def _traffic_table(check: CheckResult) -> str:
    parts = [f'<h3>{_e(check.target)} — arrival traffic</h3>',
             '<p class="muted">Cohort goodput counts valid, correct answers meeting every configured '
             'SLO, including bounded drain completions, divided by the arrival window. '
             'Arrival latency includes local dispatch delay; service latency starts at dispatch. '
             'Neither measures internal server queue time.</p>',
             '<div class="table"><table><tr><th>Offered rate / outcome</th><th>Scheduled / sent</th>'
             '<th>Sent rate</th><th>Completed / failed / timed out</th>'
             '<th>Local / late / not offered</th><th>Cohort goodput / good fraction</th>'
             '<th>Dispatch delay p95 ms</th><th>Service latency p95 ms</th>'
             '<th>Arrival first output / latency p95 ms</th></tr>']
    for stage in check.metrics["traffic_stages"]:
        def count(name: str) -> str:
            return _e(stage.get(name, "—"))
        parts.append(
            f'<tr><td>{_e(_rps(stage.get("rate_rps")))}<br>{_badge(stage.get("status", "inconclusive"))}'
            f'<br>{_e(" ".join(stage.get("reasons", [])))}</td>'
            f'<td>{count("scheduled")} / {count("started")}</td>'
            f'<td>{_e(_rps(stage.get("achieved_rps")))}</td>'
            f'<td>{count("completed")} / {count("failed")} / {count("timed_out")}</td>'
            f'<td>{count("dropped_local")} / {count("dropped_late")} / {count("not_offered")}</td>'
            f'<td>{_e(_rps(stage.get("goodput_rps")))} / '
            f'{_number(stage["good_fraction"] * 100) if stage.get("good_fraction") is not None else "—"}%</td>'
            f'<td>{_number(stage.get("dispatch_lag_p95_ms"))}</td>'
            f'<td>{_number(stage.get("latency_p95_ms"))}</td>'
            f'<td>{_number(stage.get("arrival_first_output_p95_ms"))} / '
            f'{_number(stage.get("arrival_latency_p95_ms"))}</td></tr>'
        )
    parts.append('</table></div><details><summary>Arrival cohort windows and stage accounting</summary>'
                 f'<pre>{_json(check.metrics["traffic_stages"])}</pre></details>')
    return "".join(parts)


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


def _render(report: RunReport) -> str:
    anchors = {record.id: f"request-{index}" for index, record in enumerate(report.requests)}
    config = report.manifest.get("config", {})
    parts = ["<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">",
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f"<title>Giraffe — {_e(report.run_id)}</title>", """<style>
*{box-sizing:border-box}body{margin:0;background:#f6f7f8;color:#17212c;font:15px/1.55 system-ui,sans-serif}
main{max-width:1160px;margin:auto;padding:28px}h1{font-size:30px;margin-bottom:8px}h2{font-size:22px;margin-top:30px}
h3{font-size:17px}a{color:#17569b}p{max-width:95ch}.muted{color:#53616e}.card,details{background:white;border:1px solid #d7dfe5;border-radius:8px;padding:14px 18px;margin:12px 0}
.badge{display:inline-block;border-radius:4px;padding:2px 9px;font-size:13px;font-weight:650;background:#edf0f3;color:#3d4752}
.pass{background:#e2f2e8;color:#1b6539}.fail,.blocked{background:#ffe8e5;color:#932c21}.inconclusive{background:#fff0cb;color:#795300}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid #e1e6ea;vertical-align:top}
th{background:#f2f5f7;white-space:nowrap}.table{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f4f6f8;padding:12px}
summary{cursor:pointer;overflow-wrap:anywhere}code{overflow-wrap:anywhere}ul{padding-left:22px}section{scroll-margin-top:16px}
@media(max-width:640px){main{padding:16px}h1{font-size:25px}td,th{padding:7px}}
</style><main>""", f"<h1>Giraffe acceptance results {_badge(report.overall)}</h1>",
             f'<p class="muted">Run {_e(report.run_id)} · suite {_e(report.suite_version)}<br>'
             f'{_e(report.started_at)} → {_e(report.finished_at)}</p>',
             '<p><a href="#checks">Checks</a> · <a href="#measurements">Measurements</a> · '
             '<a href="#baseline">Baseline</a> · <a href="#requests">Request evidence</a> · '
             '<a href="#reproduction">Reproduce</a></p>']
    if report.abort_reason:
        parts.append(f'<p class="card">Run stopped: {_e(report.abort_reason)}</p>')
    for target in config.get("targets", []):
        parts.append(f'<div class="card"><strong>{_e(target.get("name", ""))}</strong> · '
                     f'{_e(target.get("route", "unknown"))}<br>Model: <code>{_e(target.get("model", ""))}</code>'
                     f'<br>Endpoint: <code>{_e(target.get("url", ""))}</code></div>')
    for check in report.checks:
        if check.id == "capacity":
            arrival = "traffic_stages" in check.metrics
            highest = check.metrics.get("highest_acceptable_rate_rps" if arrival
                                        else "highest_tested_acceptable_concurrency")
            value = _rps(highest) if arrival else str(highest) if highest is not None else "not established"
            measure = "arrival rate" if arrival else "concurrency"
            parts.append(f'<p><strong>{_e(check.target)} — highest tested acceptable {measure}: '
                         f'{_e(value)}</strong> {_badge(check.status)}<br>'
                         f'{_e(check.summary)} Maximum capacity is not established by this run.</p>')
            if arrival:
                parts.append(_traffic_table(check))
    missing = [f"{c.target} / {c.title}: {c.summary}" for c in report.checks
               if c.status in {"skipped", "inconclusive", "blocked"}]
    missing.extend(report.warnings)
    if missing:
        parts.append('<h2>Coverage and limitations</h2><ul>' +
                     "".join(f"<li>{_e(item)}</li>" for item in dict.fromkeys(missing)) + "</ul>")
    parts.append('<section id="checks"><h2>Checks</h2><div class="table"><table>'
                 '<tr><th>Target / check</th><th>Outcome</th><th>Finding and evidence</th></tr>')
    for check in report.checks:
        evidence = " ".join(f'<a href="#{anchors[rid]}">{_e(rid)}</a>'
                            for rid in check.evidence_ids if rid in anchors)
        metrics = (f'<details><summary>Measurements</summary><pre>{_json(check.metrics)}</pre></details>'
                   if check.metrics else "")
        parts.append(f'<tr><td>{_e(check.target)}<br><strong>{_e(check.title)}</strong>'
                     f'<br>{"Required" if check.required else "Optional"}</td>'
                     f'<td>{_badge(check.status)}</td><td>{_e(check.summary)}'
                     f'{metrics}<div>{evidence}</div></td></tr>')
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
        parts.append(f'<p>{_badge(report.baseline.get("status", "inconclusive"))} '
                     f'Compared with {_e(report.baseline.get("run_id", "unknown"))}. '
                     'Absolute check outcomes appear separately above.</p>')
        for target in report.baseline.get("targets", []):
            if "traffic_capacity" in target:
                parts.append(_traffic_baseline_html(target))
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
                 'Retention changes stored bodies only; scores, character counts and measurements remain.</p>')
    for record in report.requests:
        label = f"{record.target} · {record.scenario} · {record.fixture_id} · {record.status}"
        parts.append(f'<details id="{anchors[record.id]}"><summary>{_e(label)}'
                     f' · {_number(record.elapsed_ms)} ms</summary>'
                     f'<p>Request {_e(record.id)} · scored: {_e(record.score)} · '
                     f'valid completion: {_e(record.valid)} · output characters: {record.output_chars}</p>')
        if record.error or record.score_message:
            parts.append(f'<p>{_e(record.error or "")} {_e(record.score_message or "")}</p>')
        if record.output:
            parts.append(f'<h3>Raw response</h3><pre>{_e(record.output)}</pre>')
        else:
            parts.append('<p class="muted">No retained response body.</p>')
        if record.reasoning:
            parts.append(f'<h3>Reasoning response</h3><pre>{_e(record.reasoning)}</pre>')
        parts.append(f'<details><summary>Local fixture and measured request evidence</summary><pre>'
                     f'{_json(record.model_dump(exclude={"output", "reasoning"}))}</pre></details></details>')
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


def _atomic_write(path: Path, contents: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(contents)
            file.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_report(report: RunReport, directory: Path) -> tuple[Path, Path]:
    """Write matching JSON and standalone HTML, applying retention only to output copies."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    retained = _retained(report)
    json_path, html_path = directory / "report.json", directory / "report.html"
    _atomic_write(json_path, retained.model_dump_json(indent=2))
    _atomic_write(html_path, _render(retained))
    return json_path, html_path


def reproduction_export(report: RunReport) -> dict:
    retained = _retained(report)
    return {"schema_version": "1", "source_run_id": report.run_id,
            "source_outcome": report.overall,
            "config": retained.manifest.get("config", {}),
            "fixture_pack": retained.manifest.get("fixture_pack", {}),
            "deployments": {name: obs.get("deployment") for name, obs in retained.observations.get("targets", {}).items()},
            "deployment_history": {name: obs.get("deployment_history", []) for name, obs in retained.observations.get("targets", {}).items()},
            "instructions": ["Supply secret environment-variable references locally.",
                "Match suite and fixture hashes; supply any local fixture/metadata files.",
                "Verify unknown server settings manually. This export cannot recreate inaccessible server state.",
                "Imported launch arguments describe intent and are never executed by this export.",
                "A saved failed run is a reference, not an approved deployment."]}
