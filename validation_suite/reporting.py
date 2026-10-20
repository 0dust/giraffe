"""One JSON representation for the prescribed human and machine reports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema


HERE = Path(__file__).parent
SECTIONS = json.loads((HERE / "report_fields.json").read_text())
SCHEMA = json.loads((HERE / "report.schema.json").read_text())
METRIC_STATES = {"measured", "not_applicable", "not_measured", "unknown", "unreliable"}
METRIC_VERDICTS = {"pass", "fail", "inconclusive", "not_applicable"}


def key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def metric(
    value: Any = None,
    *,
    state: str = "not_measured",
    unit: str = "none",
    population: str = "measured attempts",
    window: str = "whole case including drain",
    threshold: Any = None,
    verdict: str = "inconclusive",
    reason: str = "No observation was collected at this boundary.",
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return dict(state=state, value=value, unit=unit, population=population, window=window,
                threshold=threshold, verdict=verdict, reason=reason, evidence=evidence or [])


def empty_report(manifest: dict[str, Any]) -> dict[str, Any]:
    scenario = manifest.get("scenario", {})
    report = {
        "schema_version": 1,
        "manifest": manifest,
        "experiment_id": manifest.get("experiment_id", scenario.get("experiment_id", "E1")),
        "case_id": manifest.get("case_id", scenario.get("id", "unspecified")),
        "candidate": manifest.get("candidate", "unspecified"),
        "repetition": manifest.get("repetition", 1),
        "execution_status": manifest.get("execution_status", "not_run"),
        "sections": {section["key"]: {field["key"]: metric() for field in section["fields"]}
                     for section in SECTIONS},
        "verdicts": {},
    }
    return report


def set_metric(report: dict[str, Any], label: str, value: dict[str, Any]) -> None:
    field_key = key(label)
    matches = [fields for fields in report["sections"].values() if field_key in fields]
    if len(matches) != 1:
        raise KeyError(f"Expected one report field for {label!r}, found {len(matches)}")
    matches[0][field_key] = value


def get_metric(report: dict[str, Any], label: str) -> dict[str, Any]:
    field_key = key(label)
    for fields in report["sections"].values():
        if field_key in fields:
            return fields[field_key]
    raise KeyError(label)


def validate_report(report: dict[str, Any]) -> None:
    # JSON round-trip rejects NaN/Infinity, which the JSON Schema number type alone permits.
    json.dumps(report, allow_nan=False)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    for label, verdict in report["verdicts"].items():
        if get_metric(report, label)["value"] != verdict:
            raise ValueError(f"Final verdict differs between sections and verdicts: {label}")


def _plain(value: Any) -> str:
    if isinstance(value, dict) and "sample_count" in value and "percentile_method" in value:
        core = "; ".join(f"{name}={value[name]}" for name in
                         ("min", "median", "p95", "p99", "max", "sample_count"))
        return (f"{core}; percentile method={value['percentile_method']}; "
                f"qualification coverage={json.dumps(value['qualification_coverage'], sort_keys=True)}; "
                f"tail claims={json.dumps(value['tail_claims'], sort_keys=True)}")
    if isinstance(value, dict):
        return "; ".join(f"{name}: {_plain(item)}" for name, item in value.items())
    if isinstance(value, list):
        return "[" + "; ".join(_plain(item) for item in value) + "]"
    return "none" if value is None else str(value)


def render_metric(item: dict[str, Any]) -> str:
    evidence = ", ".join(f"[{ref}]({ref})" for ref in item["evidence"]) or "none"
    return (f"{_plain(item['value'])} {item['unit']} | state: {item['state']} | "
            f"verdict: {item['verdict']} | population: {item['population']} | "
            f"window: {item['window']} | threshold: {_plain(item['threshold'])} | "
            f"reason: {item['reason']} | evidence: {evidence}")


def render_markdown(report: dict[str, Any]) -> str:
    validate_report(report)
    manifest = report["manifest"]
    scenario = manifest.get("scenario", {})
    headers = [
        ("Experiment", f"{report['experiment_id']} {scenario.get('mode', scenario.get('variant', ''))}"),
        ("Purpose", scenario.get("purpose", manifest.get("purpose", "unknown: purpose unspecified"))),
        ("Execution", report["execution_status"]),
        ("Baseline/engine/suite commits", manifest.get("commits", {
            "baseline": manifest.get("baseline_sha", manifest.get("baseline", {}).get("sha", "unknown")),
            "engine": manifest.get("engine_sha", manifest.get("baseline", {}).get("sha", "unknown")),
            "suite": manifest.get("suite_version", "unknown")})),
        ("Hardware, OS, runtime/compiler, CPU/RAM limits", manifest.get("hardware", "not_measured")),
        ("Worker/thread/evaluator counts", manifest.get("processes", manifest.get("workers", "not_measured"))),
        ("Reference host/process, clocks, network, protocol/TLS, connection policy",
         manifest.get("reference", "not_measured")),
        ("Workload/seed/configuration hashes", manifest.get("hashes", "not_measured")),
        ("Warm-up, active phase, ramp, drain, deadline and retry policy",
         manifest.get("phases", scenario.get("phases", "not_measured"))),
        ("Acceptance criteria and reference uncertainty", manifest.get("thresholds", "not_measured")),
        ("Commands and raw evidence", manifest.get("commands", "not_measured")),
    ]
    lines = [f"# {report['candidate']} — {report['case_id']} — repetition {report['repetition']}", ""]
    lines.extend(f"{label}: {_plain(value)}" for label, value in headers)
    for section in SECTIONS:
        lines.extend(["", f"## {section['title']}", ""])
        lines.extend(f"- {field['label']}: {render_metric(report['sections'][section['key']][field['key']])}"
                     for field in section["fields"])
    return "\n".join(lines) + "\n"


def write_report(case_dir: str | Path, report: dict[str, Any]) -> None:
    directory = Path(case_dir)
    validate_report(report)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                                      allow_nan=False) + "\n")
    (directory / "report.md").write_text(render_markdown(report))


def render_summary(study: dict[str, Any], reports: list[dict[str, Any]]) -> str:
    def cell(value: Any) -> str:
        return _plain(value).replace("|", "\\|").replace("\n", " ")

    lines = [f"# Giraffe reliability validation — {study['study_id']}", "",
             f"Baseline: {study.get('baseline_sha', 'unknown')} | Suite: {study.get('suite_version', 'unknown')} | Criteria: {study.get('criteria_version', 'unknown')}",
             f"Hardware/resource envelope: {cell(study.get('hardware', 'not_measured'))} | Reference endpoint: {cell(study.get('reference', 'not_measured'))}",
             "Values are measured unless explicitly labelled otherwise. This is not a synthetic demonstration.",
             "",
             "| Candidate | Case / repeat | Experiment | Requested | Observed delivery | Response/accounting correctness | Timing qualification | Reference valid | Result |",
             "|---|---|---|---|---|---|---|---|---|"]
    for report in reports:
        validate_report(report)
        verdicts = report["verdicts"]
        scenario = report["manifest"].get("scenario", {})
        requested = []
        for name, unit in (("request_count", "requests"), ("concurrency", "in flight"),
                           ("rate_rps", "requests/s"), ("active_seconds", "s"),
                           ("duration_seconds", "s")):
            if name in scenario:
                requested.append(f"{scenario[name]} {unit}")
        def brief(label: str, short: str) -> str:
            item = get_metric(report, label)
            return f"{short}={_plain(item['value'])} ({item['state']})"
        delivery = "; ".join(brief(label, short) for label, short in (
            ("Requests dispatched", "dispatched"),
            ("Requests confirmed received by endpoint", "received"),
            ("Actual load-generation duration", "active_s")))
        occupancy = get_metric(report, "Actual time-weighted mean, minimum and maximum concurrency")
        if occupancy["state"] == "measured":
            delivery += f"; mean_in_flight={occupancy['value']['mean']}"
        link = f"{report['candidate']}/{report['case_id']}/report.md"
        values = [report["candidate"], f"[{report['case_id']}/{report['repetition']}]({link})",
                  f"{report['experiment_id']} {scenario.get('mode', '')}", ", ".join(requested) or "unknown",
                  delivery, verdicts["response_accounting_correctness"],
                  verdicts["measurement_accuracy"], verdicts["reference_test_valid"],
                  verdicts["overall_experiment_result"]]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    return "\n".join(lines) + "\n"
