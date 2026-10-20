"""Assess preserved execution evidence into a separate versioned output tree."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
from collections import Counter
from itertools import zip_longest
from pathlib import Path
from typing import Any

from validation_suite.reporting import (empty_report, key, metric, render_markdown,
                                        render_summary, set_metric, validate_report, write_report)
from validation_suite.verifier import bounded_jsonl_rows, verify_case

ASSESSOR_VERSION = "0.3.0"
HERE = Path(__file__).parent
DERIVED_FILES = {"report.json", "report.md", "reconciliation.jsonl", "traffic-series.csv", "verification-backend.json", "timing-verification.json"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def input_snapshot(study: Path) -> dict[str, str]:
    """Hash raw evidence, original derived artifacts, frozen source/tests, and selection."""
    roots = [study / "current-python", study / "observer-overhead", *study.glob("frozen-source*"), *study.glob("frozen-tests-*"),
             *study.glob("offline-assessments/0.1.*"), *study.glob("offline-assessments/0.2.*")]
    paths = {path for root in roots for path in root.rglob("*") if path.is_file()}
    paths.update(path for name in ("suite-manifest.json", "unrun-selection-original.json",
                                   "unrun-selection-original.md", "summary.original-v0.1.1.md",
                                   "execution-final-source-proof-v0.1.1.json", "execution-cleanup-proof.json",
                                   "assessment-selection-original.json", "summary.original-v0.2.0-paused.md",
                                   "user-stop-provenance.json", "remaining-selection-user-stop.json")
                 if (path := study / name).is_file())
    content_cache, hashes = {}, {}
    for path in sorted(paths):
        resolved = path.resolve()
        if resolved not in content_cache:
            content_cache[resolved] = file_hash(path)
        digest = content_cache[resolved]
        hashes[str(path.relative_to(study))] = json_hash({"symlink": os.readlink(path), "target_sha256": digest}) if path.is_symlink() else digest
    return hashes


def compare_case(original: dict[str, Any], derived: dict[str, Any], old_dir: Path,
                 new_dir: Path) -> dict[str, Any]:
    """Index newly interpreted evidence and narrowly justified fault corrections."""
    for name in ("schema_version", "experiment_id", "case_id", "candidate", "repetition", "execution_status"):
        if original[name] != derived[name]:
            raise ValueError(f"Execution identity/status changed: {name}")
    manifests = []
    for report in (original, derived):
        manifest = copy.deepcopy(report["manifest"])
        manifest.pop("offline_assessment", None)
        manifests.append(manifest)
    if manifests[0] != manifests[1]:
        raise ValueError("Original execution manifest changed")
    recovered = any("bounded offline input limit" in item["reason"] or "verifier safety limit" in item["reason"]
                    for fields in original["sections"].values() for item in fields.values())
    fault = derived["manifest"].get("scenario", {}).get("fault")
    fault_supported = fault in {"connection_refused", "global_budget", "user_cancel"} and derived["verdicts"]["expected_fault_behavior"] == "pass"
    productivity_fields = {key(label) for label in ("Responses processed per second", "Stream protocol events processed per second",
                                       "Response bytes processed per second", "Request preparation time", "Connection-pool or execution-slot wait")}
    allowed = set(productivity_fields)
    observer_applicable = derived["manifest"].get("timing_observer", {}).get("enabled") is True
    timing_value = derived["sections"]["7_timing_accuracy"][key("Timing error distributions and allowed tolerances")]["value"]
    source_proven = observer_applicable and isinstance(timing_value, dict) and timing_value.get("source_identity", {}).get("valid") is True
    timing_failure = source_proven and timing_value.get("measurement_failure") is True
    if observer_applicable and original["verdicts"]["measurement_accuracy"] != derived["verdicts"]["measurement_accuracy"]:
        if not source_proven:
            raise ValueError("Timing verdict changed without strict saved source proof")
        if derived["verdicts"]["measurement_accuracy"] == "qualified" and not (
                timing_value.get("qualified") is True and not timing_value.get("issues")
                and timing_value.get("health", {}).get("reference_valid_for_observed_run") is True
                and timing_value.get("overhead", {}).get("valid_for_profile") is True
                and timing_value.get("reference_analysis", {}).get("complete") is True):
            raise ValueError("Timing promotion lacks independent health/overhead/coverage proof")
    if observer_applicable:
        allowed.update(key(label) for label in ("Reported and reference first-byte, first-answer and first-reasoning timings",
            "Reported and reference completion latency", "Reported and reference answer-event gaps",
            "Independently measurable response-read delay", "Timing error distributions and allowed tolerances",
            "Reference clock/network/observation uncertainty", "Qualified measurement count and percentage",
            "Unreliable, missing or unknown measurements, by reason", "measurement_accuracy",
            "overall_experiment_result", "Limitations, failed gates and next diagnostic"))
    if timing_failure:
        allowed.add("capacity_contribution")
    if fault_supported:
        allowed.update({key("overall_experiment_result"), key("Limitations, failed gates and next diagnostic")})
        if fault == "connection_refused":
            allowed.update(key(label) for label in (
                "Dispatched requests with receipt unconfirmed", "Logical-request and attempt reconciliation",
                "Unreconciled response differences", "response_accounting_correctness", "expected_fault_behavior",
                "Observed detection, classification and recovery"))
    if not recovered:
        for before, after in zip_longest(bounded_jsonl_rows(old_dir / "reconciliation.jsonl"),
                                        bounded_jsonl_rows(new_dir / "reconciliation.jsonl")):
            if before is not None and fault_supported and fault == "connection_refused":
                before["issues"] = [code for code in before["issues"] if code != "missing_receipt"]
            if before != after:
                raise ValueError("Reconciliation lifecycle/count/outcome changed outside confirmed refusal correction")
        fixed_verdicts = {"requested_load_delivered", "evaluation_correctness", "reference_test_valid", "response_accounting_correctness", "expected_fault_behavior"}
        if fault_supported:
            fixed_verdicts -= {"response_accounting_correctness", "expected_fault_behavior"}
        if not source_proven:
            fixed_verdicts.add("measurement_accuracy")
        if not timing_failure:
            fixed_verdicts.add("capacity_contribution")
        for label in fixed_verdicts:
            if original["verdicts"][label] != derived["verdicts"][label]:
                raise ValueError(f"Delivery/timing/selection verdict changed: {label}")
        if not fault_supported and not source_proven and original["verdicts"] != derived["verdicts"]:
            raise ValueError("Unapproved healthy-case verdict change")
        if not fault_supported and original["verdicts"]["overall_experiment_result"] == "fail" and derived["verdicts"]["overall_experiment_result"] != "fail":
            raise ValueError("Source identity correction cannot erase an existing direct experiment failure")
        if file_hash(old_dir / "traffic-series.csv") != file_hash(new_dir / "traffic-series.csv"):
            raise ValueError("Previously interpreted traffic series changed")
    changes = []
    for section, fields in original["sections"].items():
        for field, before in fields.items():
            after = derived["sections"][section][field]
            if before == after:
                continue
            if recovered:
                reason = "saved raw evidence is newly interpretable with bounded-memory per-attempt verification"
            elif field in allowed:
                reason = ("retained whole-process productivity or wait attribution corrected; no isolated processing-speed or queue claim"
                          if field in productivity_fields else
                          "source-identity contract / explicit overhead attribution corrected; existing timing budgets and delivery gates unchanged"
                          if observer_applicable else "confirmed injected fault semantics; requested healthy delivery failure remains explicit")
            elif field == "capacity_contribution" and {k: v for k, v in before.items() if k != "reason"} == {k: v for k, v in after.items() if k != "reason"}:
                reason = "capacity attribution clarified; verdict and all observations unchanged"
            else:
                raise ValueError(f"Unapproved report field change: {section}.{field}")
            changes.append({"section": section, "field": field, "reason": reason, "original": before, "derived": after})
    return {"case_id": derived["case_id"], "previously_bounded_evidence_recovered": recovered,
            "fault_semantics_supported": fault_supported, "field_changes": changes,
            "invariants": {"execution_identity_status": True, "execution_manifest_criteria": True,
                           "all_changes_within_declared_correction_scope": True,
                           "previously_interpreted_lifecycle_counts_outcomes": True,
                           "delivery_evaluation_reference_and_traffic_preserved_outside_confirmed_fault_correction": True,
                           "timing_changes_require_proven_source_and_unchanged_acceptance_gates": True}}

def _not_run_report(study: dict[str, Any], row: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    execution_status = row.get("report_execution_status", "not_run")
    if execution_status not in {"not_run", "aborted"}:
        raise ValueError("Selection-only report must be not_run or explicitly user-stopped/aborted")
    manifest = {"schema_version": 1, "study_id": study["study_id"], "suite_version": study["suite_version"],
                "suite_frozen_sha256": study["frozen_sha256"], "criteria_version": study["criteria_version"],
                "case_id": row["report_case_id"], "candidate": Path(row["case_directory"]).parts[0],
                "repetition": row["repetition"], "experiment_id": row["scenario"].get("experiment_id", row["case_id"].split("-")[0]),
                "execution_status": execution_status, "scenario": row["scenario"], "thresholds": study["thresholds"],
                "reference": study.get("reference", {}), "baseline": study.get("baseline", {}),
                "not_run_selection": row, "offline_assessment": provenance}
    report = empty_report(manifest)
    reason = ("User-stopped partial execution: " if execution_status == "aborted" else "Not run: ") + row["reason"]
    reason += " Preserved partial raw evidence is not interpreted by this selection-only report; measurements remain unknown." if execution_status == "aborted" else " No execution observations exist; counts and measurements are unavailable."
    for fields in report["sections"].values():
        for field in fields:
            fields[field] = metric(reason=reason, evidence=["selection-original.json"])
    verdicts = {"requested_load_delivered": "inconclusive", "response_accounting_correctness": "inconclusive",
                "measurement_accuracy": "unknown", "evaluation_correctness": "inconclusive",
                "reference_test_valid": "unknown", "expected_fault_behavior": "not_applicable",
                "overall_experiment_result": "inconclusive" if execution_status == "aborted" else "not run", "capacity_contribution": "not_applicable"}
    report["verdicts"] = verdicts
    for label, value in verdicts.items():
        set_metric(report, label, metric(value, state="not_applicable" if value == "not_applicable" else "unknown",
                                        verdict="not_applicable" if value == "not_applicable" else "inconclusive",
                                        reason=reason, evidence=["selection-original.json"]))
    set_metric(report, "Limitations, failed gates and next diagnostic", metric(
        {"not_run_reason": row["reason"], "selection_details": row.get("selection_details"),
         "original_selection_evidence": row["evidence"]}, state="unknown", reason=reason,
        evidence=["selection-original.json"]))
    return report


def assess_study(study_dir: str | Path) -> dict[str, Any]:
    study = Path(study_dir).resolve()
    frozen = json.loads((study / "suite-manifest.json").read_text())
    selection_path = next((study / name for name in ("assessment-selection-original.json", "unrun-selection-original.json")
                           if (study / name).is_file()), None)
    if selection_path is None:
        raise ValueError("Preserved assessment selection index is absent")
    selection = json.loads(selection_path.read_text())
    original_reports = sorted((study / "current-python").glob("*/report.json"))
    if len(original_reports) != selection["original_report_count"]:
        raise ValueError("Original report count differs from preserved selection index")
    if len(original_reports) + len(selection["rows"]) != selection["frozen_repetitions"]:
        raise ValueError("Frozen repetition coverage differs from preserved selection index")
    for original in selection.get("original_reports", []):
        path = study / original["path"]
        if not path.resolve().is_relative_to(study) or file_hash(path) != original["sha256"]:
            raise ValueError("Original report differs from the preserved selection hash")
    for name, expected in frozen["suite_source_hashes"].items():
        archive = next((path for path in (study / f"frozen-source-v{frozen['suite_version']}" / name,
                                         study / "frozen-source/validation_suite" / name) if path.is_file()), None)
        if archive is None or file_hash(archive) != expected:
            raise ValueError(f"Frozen source archive hash mismatch: {name}")
    output = study / "offline-assessments" / ASSESSOR_VERSION
    output.mkdir(parents=True, exist_ok=False)
    before = input_snapshot(study)
    write_json(output / "input-hashes-before.json", before)
    source_names = sorted({path.name for path in HERE.glob("*.py")} | {"report.schema.json", "report_fields.json"})
    source_hashes = {name: file_hash(HERE / name) for name in source_names}
    source_archive = output / "assessor-source"
    source_archive.mkdir()
    for name in source_names:
        shutil.copyfile(HERE / name, source_archive / name)
        if file_hash(source_archive / name) != source_hashes[name]:
            raise ValueError("Assessor dependency source changed while archiving")
    provenance = {"assessor_version": ASSESSOR_VERSION, "assessor_source_hashes": source_hashes,
                  "assessor_sha256": json_hash(source_hashes), "execution_suite_version": frozen["suite_version"],
                  "original_frozen_sha256": frozen["frozen_sha256"],
                  "criteria_sha256": json_hash({name: frozen.get(name) for name in
                                               ("criteria_version", "thresholds", "threshold_rationale", "thresholds_provisional")}),
                  "criteria_version": frozen["criteria_version"], "input_hashes": str(output / "input-hashes-before.json"),
                  "source_study": str(study), "selection_index": str(selection_path),
                  "source_archive": str(source_archive),
                  "scope": "strict saved source identity, retained productivity, wait/overhead attribution and bounded evidence interpretation; no new execution, candidate or threshold changes"}
    write_json(output / "assessor.json", provenance)
    diffs, reports = [], []
    for number, source in enumerate(original_reports, 1):
        old_dir = source.parent
        destination = output / old_dir.relative_to(study)
        destination.mkdir(parents=True)
        inputs = {}
        for path in sorted(old_dir.iterdir()):
            if path.name not in DERIVED_FILES:
                (destination / path.name).symlink_to(os.path.relpath(path, destination), target_is_directory=path.is_dir())
                inputs[path.name] = str(path.resolve())
        (destination / "original-report.json").symlink_to(os.path.relpath(source, destination))
        (destination / "original-report.md").symlink_to(os.path.relpath(old_dir / "report.md", destination))
        case_provenance = {**provenance, "source_case": str(old_dir), "source_inputs": inputs,
                           "original_input_sha256": {name: digest for name, digest in before.items()
                                                      if name.startswith(str(old_dir.relative_to(study)) + "/")}}
        manifest = json.loads((old_dir / "manifest.json").read_text())
        manifest["offline_assessment"] = {"assessor_version": ASSESSOR_VERSION, "provenance": "assessment.json",
                                          "original_case": str(old_dir), "assessor_sha256": provenance["assessor_sha256"]}
        report = verify_case(destination, manifest)
        comparison_source = old_dir
        for version in ("0.2.0", "0.1.2"):
            previous = study / "offline-assessments" / version / old_dir.relative_to(study)
            if (previous / "report.json").is_file():
                comparison_source = previous
                break
        (destination / "previous-assessment-report.json").symlink_to(os.path.relpath(comparison_source / "report.json", destination))
        (destination / "previous-assessment-report.md").symlink_to(os.path.relpath(comparison_source / "report.md", destination))
        case_provenance["comparison_report"] = str((comparison_source / "report.json").resolve())
        case_provenance["comparison_report_sha256"] = file_hash(comparison_source / "report.json")
        case_provenance["bounded_verifier"] = json.loads((destination / "verification-backend.json").read_text())
        diff = compare_case(json.loads((comparison_source / "report.json").read_text()), report, comparison_source, destination)
        write_json(destination / "assessment.json", case_provenance)
        write_json(destination / "original-v-derived.json", diff)
        diffs.append(diff)
        reports.append(report)
        print(f"assessed {number}/{len(original_reports)}: {report['case_id']}", flush=True)
    for row in selection["rows"]:
        destination = output / row["case_directory"]
        if not destination.resolve().is_relative_to(output) or destination.exists():
            raise ValueError("Unsafe or duplicate not-run output path")
        destination.mkdir(parents=True)
        (destination / "selection-original.json").symlink_to(os.path.relpath(selection_path, destination))
        partial_source = study / row["case_directory"]
        if partial_source.exists():
            (destination / "original-partial-evidence").symlink_to(os.path.relpath(partial_source, destination), target_is_directory=True)
        report = _not_run_report(frozen, row, provenance)
        write_json(destination / "manifest.json", report["manifest"])
        write_json(destination / "assessment.json", {**provenance, "not_run_selection": row,
                                                    "selection_sha256": before[selection_path.name]})
        write_report(destination, report)
        reports.append(report)
    for report in reports:
        destination = output / report["candidate"] / report["case_id"]
        validate_report(report)
        if (destination / "report.md").read_text() != render_markdown(report):
            raise ValueError("JSON/Markdown render disagreement")
        for fields in report["sections"].values():
            for item in fields.values():
                for reference in item["evidence"]:
                    if not (destination / reference).exists():
                        raise ValueError(f"Unresolved evidence link: {destination}/{reference}")
    after = input_snapshot(study)
    write_json(output / "input-hashes-after.json", after)
    unchanged = before == after
    proof = {"input_files": len(before), "input_bytes_unchanged": unchanged,
             "original_reports_preserved": len(original_reports), "derived_reports_validated": len(reports),
             "selection_only_reports": len(selection["rows"]),
             "not_run_reports": sum(row.get("report_execution_status", "not_run") == "not_run" for row in selection["rows"]),
             "user_stopped_reports": sum(row.get("execution_status") == "user_stopped" for row in selection["rows"]),
             "json_markdown_agreement": True,
             "all_evidence_links_resolve": True, "per_case_invariants": all(all(row["invariants"].values()) for row in diffs)}
    write_json(output / "immutability-proof.json", proof)
    if not unchanged:
        raise ValueError("Original inputs changed during assessment")
    if {name: file_hash(HERE / name) for name in source_names} != source_hashes:
        raise ValueError("Assessor dependency source changed during assessment; preserve this attempt and assess again after freeze")
    write_json(output / "original-v-derived-index.json", diffs)
    summary_study = {**frozen, "baseline_sha": frozen.get("baseline", {}).get("sha"),
                     "suite_version": f"execution {frozen['suite_version']}; offline assessor {ASSESSOR_VERSION}"}
    (output / "summary.md").write_text(render_summary(summary_study, reports))
    result = {**proof, "output": str(output), "execution_status_counts": dict(Counter(report["execution_status"] for report in reports)),
              "overall_result_counts": dict(Counter(report["verdicts"]["overall_experiment_result"] for report in reports)),
              "changed_metric_count": sum(len(row["field_changes"]) for row in diffs)}
    write_json(output / "assessment-summary.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study_dir", type=Path, help="preserved study with suite manifest and unrun selection index")
    args = parser.parse_args()
    print(json.dumps(assess_study(args.study_dir), indent=2))


if __name__ == "__main__":
    main()
