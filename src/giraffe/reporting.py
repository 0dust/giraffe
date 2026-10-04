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

from .models import CHECK_NAMES, CheckResult, RequestRecord, RunReport, overall_status


# Identities are deliberately absent: changing a runtime/image is a reason to run the suite.
_MATCH_CONFIG = (
    "concurrency", "max_requests", "max_duration_seconds", "request_timeout_seconds",
    "max_output_tokens", "context_limit", "samples", "sustained_seconds", "checks",
    "structured_json", "overlap_models", "stream", "request_options", "stop_after_errors",
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
        if record.target == target and record.scenario not in {"client_cancel", "client_deadline"}:
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
    for key in _MATCH_CONFIG:
        if key not in old_config or key not in current_config:
            reasons.append(f"Recorded {key} is missing; comparability is unknown.")
        elif old_config[key] != current_config[key]:
            reasons.append(f"Run setting {key} differs.")
    # The suite version identifies bundled fixtures; custom packs also need a content identity.
    current_pack = report.manifest.get("fixture_pack")
    old_pack = baseline.manifest.get("fixture_pack")
    if not isinstance(current_pack, dict) or not isinstance(old_pack, dict) or not current_pack or not old_pack:
        reasons.append("Fixture pack identity is missing or unknown.")
    elif ({key: value for key, value in current_pack.items() if key != "custom"}
          != {key: value for key, value in old_pack.items() if key != "custom"}):
        reasons.append("Fixture pack identity differs.")
    if current_config.get("custom_fixtures") or old_config.get("custom_fixtures"):
        for label, pack in (("Current", current_pack), ("Baseline", old_pack)):
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
    higher_is_better = name in {"correctness_rate", "valid_completion_rate", "output_tokens_per_second"}
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
        rows = []
        current_groups, old_groups = _groups(report.requests, name), _groups(baseline.requests, name)
        if not current_groups or not old_groups:
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
                for metric, values in current_metrics.items():
                    if not values and not old_metrics[metric]:
                        continue
                    row = _compare_metric(metric, values, old_metrics[metric], minimum, threshold)
                    if different_work and metric in {"latency_ms", "max_stream_gap_ms", "output_tokens_per_second"}:
                        row.update(status="inconclusive", reason="Actual generated output lengths differ "
                                   "materially; these performance samples are not like-for-like.")
                    if saturated_runs and metric in {"latency_ms", "first_output_ms", "max_stream_gap_ms", "output_tokens_per_second"}:
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
        if failures:
            status = "fail"
            summary = f"{len(failures)} material metric regression(s) against {baseline.run_id}."
            if target_reasons:
                summary += " Other scenarios could not be compared: " + " ".join(target_reasons)
        elif target_reasons:
            status = "inconclusive"
            summary = "Baseline comparison is inconclusive: " + " ".join(target_reasons)
        elif not assessed or uncertain or missing_measurements:
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
        excluded = len(rows) - len(assessed)
        if excluded:
            summary += f" {excluded} low-sample or undefined metric(s) excluded; see comparison details."
        entry = {"target": name, "status": status, "reasons": target_reasons,
                 "generator_saturated_runs": saturated_runs,
                 "identity_changes": identity_changes, "comparisons": rows}
        comparison["targets"].append(entry)
        result.checks.append(CheckResult(
            id="baseline", target=name, title="Baseline comparison", required=True,
            status=status, summary=summary,
            metrics={"compared_metrics": len(assessed), "excluded_metrics": excluded,
                     "regressions": len(failures), "baseline_run_id": baseline.run_id},
            evidence_ids=list(dict.fromkeys(rid for row in failures for rid in row["evidence_ids"])),
        ))
    baseline_checks = [check for check in result.checks if check.id == "baseline"]
    comparison["status"] = overall_status(baseline_checks)
    result.baseline = comparison
    result.overall = overall_status(result.checks, result.abort_reason)
    return result


def _retained(report: RunReport) -> RunReport:
    result = report.model_copy(deep=True)
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
    return result


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _json(value: Any) -> str:
    return _e(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _number(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:,.1f}"


def _badge(status: str) -> str:
    return f'<span class="badge {_e(status)}">{_e(status)}</span>'


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
            highest = check.metrics.get("highest_tested_acceptable_concurrency")
            value = str(highest) if highest is not None else "not established"
            parts.append(f'<p><strong>{_e(check.target)} — highest tested acceptable concurrency: '
                         f'{_e(value)}</strong> {_badge(check.status)}<br>'
                         f'{_e(check.summary)} Maximum capacity is not established by this run.</p>')
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
                 '<th>First output p50 ms</th><th>Scored correct</th></tr>')
    groups: dict[tuple[str, str], list[RequestRecord]] = defaultdict(list)
    for record in report.requests:
        groups[(record.target, record.scenario)].append(record)
    for (target, scenario), records in groups.items():
        valid = [r for r in records if r.valid and r.status == "completed"]
        times = [r.elapsed_ms for r in valid]
        first = [r.first_output_ms for r in valid if r.first_output_ms is not None]
        scored = [r for r in records if r.score is not None]
        parts.append(f'<tr><td>{_e(target)} / {_e(scenario)}</td><td>{len(records)}</td><td>{len(valid)}</td>'
                     f'<td>{sum(r.status == "failed" for r in records)} / '
                     f'{sum(r.status == "timeout" for r in records)}</td>'
                     f'<td>{sum(r.status == "cancelled" for r in records)}</td>'
                     f'<td>{_number(_percentile(times, .5))} / {_number(_percentile(times, .95))}</td>'
                     f'<td>{_number(_percentile(first, .5))}</td>'
                     f'<td>{sum(r.score is True for r in scored)} / {len(scored)}</td></tr>')
    parts.append('</table></div></section><section id="baseline"><h2>Baseline comparison</h2>')
    if report.baseline:
        parts.append(f'<p>{_badge(report.baseline.get("status", "inconclusive"))} '
                     f'Compared with {_e(report.baseline.get("run_id", "unknown"))}. '
                     'Absolute check outcomes appear separately above.</p>')
        parts.append(f'<details><summary>Changes, comparability and metric evidence</summary>'
                     f'<pre>{_json(report.baseline)}</pre></details>')
    else:
        parts.append('<p>No baseline supplied. This run reports absolute checks only.</p>')
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
