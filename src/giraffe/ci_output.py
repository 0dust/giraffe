"""Small CI exports for current and saved reports."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .deployment import REDACTED
from .models import CHECK_NAMES, CheckResult, RunReport


_XML_CONTROL = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _text(value: object) -> str:
    return _XML_CONTROL.sub("", str(value))


def _url(value: str) -> str:
    # Parse the complete value: regex matching can stop inside quoted userinfo.
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")) + REDACTED
        return value
    except ValueError:
        return REDACTED


def _rows(report: RunReport) -> list[tuple[CheckResult, str, str]]:
    config = report.manifest.get("config", {})
    selected = config.get("checks")
    checks = [check for check in report.checks if check.id != "baseline" and (
        check.id in selected if isinstance(selected, list)
        else check.required or check.summary != "Not selected."
    )]
    if report.overall == "blocked" and isinstance(selected, list):
        present = {(check.target, check.id) for check in checks}
        for target in config.get("targets", []):
            for check_id in selected:
                if (target["name"], check_id) not in present:
                    checks.append(CheckResult(id=check_id, target=target["name"],
                        title=CHECK_NAMES.get(check_id, check_id), status="blocked",
                        summary=report.abort_reason or "Run blocked before this check could execute."))
    overrides: dict[tuple[str, str], tuple[str, str]] = {}
    baseline = report.baseline or {}
    for comparison in report.checks:
        if comparison.id != "baseline" or comparison.status not in {"fail", "inconclusive", "blocked"}:
            continue
        target_checks = [check for check in checks if check.target == comparison.target]
        if not target_checks:
            continue
        target = next((target for target in baseline.get("targets", [])
                       if target.get("target") == comparison.target), {})
        regressed = {row.get("check") for row in target.get("comparisons", []) if row.get("status") == "fail"}
        for key, check_id, points in (("traffic_capacity", "capacity", "stages"), ("load_test", "load", "points")):
            if any(row.get("status") == "fail" for row in (target.get(key) or {}).get(points, [])):
                regressed.add(check_id)
        affected = [check for check in target_checks if check.id in regressed]
        # Legacy comparison records may not identify a check. Preserve their
        # verdict on a real testcase rather than inventing a synthetic check.
        if not affected:
            affected = target_checks[:1]
        for check in affected:
            overrides[check.target, check.id] = (
                comparison.status, f"Baseline comparison: {comparison.summary}")
    rows = []
    for check in checks:
        override = overrides.get((check.target, check.id))
        status, reason = check.status, check.summary
        if override:
            if status not in {"fail", "blocked"}:
                status = override[0]
            reason += " " + override[1]
        rows.append((check, status, reason))
    return rows


def write_junit(report: RunReport, path: Path) -> None:
    """Write one testcase per selected target/check, including baseline verdicts."""
    rows = _rows(report)

    def counts(selected):
        return {"tests": str(len(selected)), "failures": str(sum(status == "fail" for _, status, _ in selected)),
                "errors": str(sum(status == "blocked" for _, status, _ in selected)),
                "skipped": str(sum(status in {"skipped", "inconclusive"} for _, status, _ in selected))}

    totals = counts(rows)
    # The common Jenkins JUnit XSD does not allow skipped on testsuites.
    root = ET.Element("testsuites", {key: value for key, value in totals.items() if key != "skipped"})
    targets = {target.get("name"): target for target in report.manifest.get("config", {}).get("targets", [])}
    for target_name in dict.fromkeys(check.target for check, _, _ in rows):
        selected = [row for row in rows if row[0].target == target_name]
        suite = ET.SubElement(root, "testsuite", {"name": _text(target_name), **counts(selected)})
        properties = ET.SubElement(suite, "properties")
        target = targets.get(target_name, {})
        for name, value in (("model", target.get("model") or ""),
                            ("url", _url(target.get("url") or "")), ("run_id", report.run_id)):
            ET.SubElement(properties, "property", name=name, value=_text(value))
        for check, status, reason in selected:
            case = ET.SubElement(suite, "testcase", name=_text(check.id), classname=_text(check.target))
            if status in {"fail", "blocked"}:
                element = ET.SubElement(case, "failure" if status == "fail" else "error",
                                        message=_text(reason), type="GiraffeCheckFailure" if status == "fail" else "GiraffeBlocked")
                element.text = _text(reason)
            elif status in {"skipped", "inconclusive"}:
                ET.SubElement(case, "skipped").text = _text(reason)
            ET.SubElement(case, "system-out").text = "Title: " + _text(check.title)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _md(value: object, *, table_cell: bool = False) -> str:
    text = _text(value).replace("\r", " ").replace("\n", " ").replace("`", r"\`")
    text = re.sub(r"^(\s*)([#>*_~+\-\[!])", r"\1\\\2", text)
    text = re.sub(r"^(\s*\d{1,9})([.)])(?=\s)", r"\1\\\2", text)
    return text.replace("|", r"\|") if table_cell else text


def _baseline_delta(report: RunReport) -> str:
    baseline = report.baseline
    if not baseline:
        return "Baseline delta: not requested."
    changes = []
    for target in baseline.get("targets", []):
        for row in target.get("comparisons", []):
            worsening = row.get("worsening")
            if not isinstance(worsening, (int, float)) or row.get("status") not in {"pass", "fail"}:
                continue
            unit = "percentage points" if row.get("change_unit") == "percentage_points" else "%"
            changes.append(f"{target.get('target', '')}/{row.get('check', '')} "
                           f"{row.get('metric', '')}: {worsening:+.3g} {unit} worsening")
        for key, label in (("traffic_capacity", "traffic rate"), ("load_test", "tested load")):
            comparison = target.get(key) or {}
            if comparison:
                changes.append(f"{target.get('target', '')} {label}: {comparison.get('status', 'inconclusive')}")
    detail = "; ".join(changes[:8]) or "no comparable metric delta"
    if len(changes) > 8:
        detail += f"; {len(changes) - 8} more in the report"
    return f"Baseline delta vs {baseline.get('name') or baseline.get('run_id', 'unknown')}: {baseline.get('status', 'inconclusive')}; {detail}."


def write_summary_md(report: RunReport, path: Path, report_path: Path | None) -> None:
    """Write a concise summary suitable for GitHub step summaries and comments."""
    lines = [f"## Giraffe: **{_md(report.overall.upper())}**", "", f"Run: {_md(report.run_id)}", "",
             "| Target | Check | Verdict | Reason |", "| --- | --- | --- | --- |"]
    for check, status, reason in _rows(report):
        lines.append(f"| {_md(check.target, table_cell=True)} | {_md(check.id, table_cell=True)} | "
                     f"{_md(status, table_cell=True)} | {_md(reason, table_cell=True)} |")
    if report.abort_reason:
        lines.extend(["", "Reason: " + _md(report.abort_reason)])
    report_line = (f"Report: {_md(report_path)}" if report_path is not None
                   else "No report (run blocked before start)")
    lines.extend(["", _md(_baseline_delta(report)), "", report_line, ""])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
