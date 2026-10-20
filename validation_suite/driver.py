"""Freeze, execute and replay a private, sequential reliability study."""

from __future__ import annotations

import argparse
import array
import csv
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import termios
import time
from collections.abc import Iterable
from typing import Any

from . import __version__
from .baseline import (baseline_command, baseline_environment, baseline_snapshot,
                       ensure_execution_allowed, exported_phases, iter_normalized_results,
                       iter_report_requests, make_configuration, support_reason)
from .scenarios import (CONCERNS, CRITERIA_VERSION, EXECUTION_POLICY, RESOURCE_POLICY, THRESHOLDS,
                        digest, observer_calibration_cases, scenario_matrix, validate_matrix)
from .scenarios import resource_policy, poisson_offsets

SCHEMA_VERSION = "1"
ARTIFACTS = ("planned.jsonl", "client-events.jsonl", "receiver-events.jsonl", "results.jsonl",
             "reconciliation.jsonl", "resources.csv", "traffic-series.csv", "stdout.log", "stderr.log")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def storage_bytes(directory: Path) -> int:
    """Account owned entries, not the old evidence behind read-only symlinks."""
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_symlink() or path.is_file():
                total += path.lstat().st_size
        except FileNotFoundError:
            # Product atomic writes can rename a temporary file during a sample.
            continue
    return total


def require_ignored(directory: Path) -> None:
    """Reject accidental publication paths, including paths outside this repository."""
    repository = Path(__file__).resolve().parents[1]
    try:
        relative = directory.resolve().relative_to(repository)
    except ValueError as exc:
        raise ValueError("Study must live under this repository's ignored runs directory") from exc
    result = subprocess.run(["git", "check-ignore", "--quiet", str(relative / "suite-manifest.json")], cwd=repository)
    if result.returncode:
        raise ValueError("Study results are not ignored; refusing to write private evidence")


def suite_source_hashes() -> dict:
    directory = Path(__file__).resolve().parent
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.iterdir()) if path.suffix in {".py", ".json", ".md"}}


def reviewed_execution_plan(path: Path) -> tuple[list[dict], dict]:
    """Read an explicit subset with fresh identities; never resume old attempts."""
    plan = read_json(path)
    if plan.get("schema_version") != 1:
        raise ValueError("Unsupported reviewed execution-plan version")
    cases = plan.get("scenarios")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Reviewed execution plan requires explicit scenarios")
    try:
        validate_matrix(cases, require_all_experiments=False)
    except (KeyError, TypeError) as error:
        raise ValueError("Reviewed execution-plan scenario fields are missing or invalid") from error
    lookup = {case["id"]: case for case in cases}

    def source_path(value: Any, label: str) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError(f"Reviewed execution plan requires {label}")
        source = Path(value)
        return (source if source.is_absolute() else path.parent / source).resolve()

    source = source_path(plan.get("source_study"), "source_study")
    plan["source_study"] = str(source)
    original = read_json(source / "suite-manifest.json")
    original_digest = original.pop("frozen_sha256")
    if digest(original) != original_digest or original_digest != plan.get("source_frozen_sha256"):
        raise ValueError("Reviewed plan source-study identity differs")
    if set(lookup) & {case["id"] for case in original["cases"]}:
        raise ValueError("Reviewed repeats require fresh case identities")
    schedule, primary = plan.get("observer_calibration_schedule", []), plan.get("cases")
    if not isinstance(schedule, list) or not isinstance(primary, list) or not primary:
        raise ValueError("Reviewed execution plan requires case selections and a valid calibration schedule")
    if plan.get("require_calibration_pass") and not schedule:
        raise ValueError("Reviewed calibration gate requires an explicit calibration schedule")
    selected = set()
    preceding_outputs = set()
    for item in [*schedule, *primary]:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str) or item["case_id"] not in lookup:
            raise ValueError("Reviewed execution-plan selection requires a known case_id")
        case = lookup[item["case_id"]]
        ensure_execution_allowed(case)
        repetitions = item.get("repetitions", [item.get("repetition")])
        if not isinstance(repetitions, list) or not repetitions:
            raise ValueError("Reviewed execution-plan repetitions must be a nonempty list")
        for repetition in repetitions:
            key = (case["id"], repetition)
            if type(repetition) is not int or not 1 <= repetition <= case["repetitions"] or key in selected:
                raise ValueError("Reviewed execution-plan repetition is invalid or duplicated")
            selected.add(key)
        item["source_case"] = str(source_path(item.get("source_case"), "source_case"))
        if not (Path(item["source_case"]) / "manifest.json").is_file() and item["source_case"] not in preceding_outputs:
            raise ValueError("Reviewed execution-plan projection source is unavailable")
        if item.get("gate_after") not in (None, "healthy_setup", "correctness_and_safety"):
            raise ValueError("Unknown reviewed post-case setup gate")
        preceding_outputs.update(str((path.parent / "current-python" / f"{case['id']}-r{repetition}").resolve())
                                 for repetition in repetitions)
    if any(not isinstance(item.get("timing_profile"), str) or not item["timing_profile"]
           or type(item.get("observer_enabled")) is not bool for item in schedule):
        raise ValueError("Calibration selections require timing_profile and an explicit observer_enabled boolean")
    expected = {(case["id"], repetition) for case in cases for repetition in range(1, case["repetitions"] + 1)}
    if not cases or selected != expected:
        raise ValueError("Reviewed execution plan must select every declared repetition exactly once")
    return cases, plan


def freeze(study: Path, baseline: Path, python: str, study_id: str | None = None,
           *, source_study: Path | None = None, include_fast_case: bool = False,
           observe_missing: bool = False, execution_plan: Path | None = None) -> dict:
    require_ignored(study)
    destination = study / "suite-manifest.json"
    if destination.exists():
        raise ValueError("Frozen manifest already exists; use a new study/version rather than overwrite criteria")
    if execution_plan is not None and (source_study is not None or include_fast_case or observe_missing):
        raise ValueError("Use either an explicit execution plan or the earlier remediation selection")
    cases, selected_plan = reviewed_execution_plan(execution_plan) if execution_plan is not None else (scenario_matrix(), None)
    if observe_missing:
        if source_study is None:
            raise ValueError("Observed missing-profile execution requires an explicit source study")
        cases.extend(observer_calibration_cases(cases))
    validate_matrix(cases, require_all_experiments=selected_plan is None)
    proof = baseline_snapshot(baseline, python)
    if proof["status_porcelain"]:
        raise ValueError("Pinned baseline checkout is not clean")
    value = {"schema_version": SCHEMA_VERSION, "study_id": study_id or study.name,
             "suite_version": __version__, "criteria_version": CRITERIA_VERSION,
             "baseline": proof, "suite_source_hashes": suite_source_hashes(),
             "thresholds": THRESHOLDS, "thresholds_provisional": True,
             "threshold_rationale": "Exact identity/content/outcome gates; 20ms dispatch/replacement p95 "
             "and 100ms maximum are initial desktop scheduler tolerances. 10% one-second arrival error "
             "allows one boundary request, with an independent receiver-spacing gate: 95% of gaps "
             "within 50% of requested spacing; fewer than 20 gaps require all in band and a small-sample label. "
             "90% occupancy for 95% of post-ramp active time detects barriers. "
             "Fine timing requires independent uncertainty at most 10% of event interval; app writes cannot qualify reads. "
             "64 MiB/hour growth tolerance is provisional and includes retained results without deductions.",
             "resource_policy": resource_policy(), "execution_policy": EXECUTION_POLICY,
             "coverage_map": CONCERNS, "cases": cases,
             "execution_selection": {
                 "initial": "Run all supported smoke and exploratory load levels sequentially, low to high.",
                 "qualification": "Per mode select the lowest healthy exploratory profile and the highest "
                 "provisionally passing exploratory profile that fits frozen resources; run each 300 seconds three times. "
                 "All other frozen qualification rows remain explicitly not_run with selection reason. "
                 "A timing-unqualified same-host profile is diagnostic, never capacity-qualified.",
                 "endurance": "Not selected in this reviewed campaign. Conditional longer-run authorization exists, but observation prerequisites remain unresolved.",
             },
             "reference": {"same_host": True, "boundary": "application_write_and_drain",
                           "wire_reference": False, "uncertainty_ms": None,
                           "limitations": "No separate endpoint host or packet/client-read trace is available; "
                           "same-host diagnostic timings cannot establish absolute generator capacity."},
             "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "construction_is_execution": False}
    if selected_plan is not None:
        value["reviewed_execution_plan"] = {"path": str(execution_plan.resolve()),
            "sha256": hashlib.sha256(execution_plan.read_bytes()).hexdigest(),
            "scope": "Explicit versioned subset; earlier completed and interrupted attempts remain immutable."}
        value["execution_selection"]["remediation"] = {key: item for key, item in selected_plan.items() if key != "scenarios"}
        value["coverage_scope"] = "Only this explicit subset is selected for new execution; full E1–E7 history remains in source_study."
        if selected_plan.get("observer_calibration_schedule"):
            from .timing_analysis import overhead_criteria
            from .timing_observer import OBSERVER_VERSION

            value["observation_policy"] = {
                "observer_source_sha256": suite_source_hashes()["timing_observer.py"],
                "observer_version": OBSERVER_VERSION, "uncertainty_budget_ns": 100_000,
                "scope": "Explicit low-load R/G/S observation controls; independent arrival A remains unavailable.",
                "gross_overhead_criteria": overhead_criteria(),
                "duration_transfer_limit": "Low-load controls do not qualify unobserved load runs or packet-arrival delay.",
                "calibration_artifact_directory": "observer-overhead"}
            value["reference"]["limitations"] = (
                "Same-host endpoint writes and client R/G/S observations are separate boundaries. "
                "Independent arrival A and a separate endpoint host are unavailable; no physical-arrival or absolute-capacity claim."
            )
    if source_study is not None:
        selections = read_json(source_study / "unrun-selection-original.json")
        selected = {}
        for row in selections["rows"]:
            case = next(case for case in cases if case["id"] == row["case_id"])
            if case["experiment_id"] == "E7":
                continue
            ensure_execution_allowed(case)
            if row.get("scenario") is not None and digest(case) != digest(row["scenario"]):
                raise ValueError("Missing-case workload changed from the preserved original selection")
            source = source_study / "current-python" / (case["id"].replace("-qualification-", "-exploration-") + "-r1")
            dimension = "c" if case["experiment_id"] == "E3" else "r"
            load = case["concurrency"] if dimension == "c" else case["rate_rps"]
            profile = f"{case['experiment_id']}-{dimension}{load}"
            if observe_missing:
                source = study / "current-python" / f"{case['experiment_id']}-observer-{dimension}{load}-observed-r1"
            selected.setdefault(case["id"], {"case_id": case["id"], "repetitions": [], "source_case": str(source.resolve()),
                                           "observer_enabled": observe_missing, "timing_profile": profile})["repetitions"].append(row["repetition"])
        if sum(len(item["repetitions"]) for item in selected.values()) != 24:
            raise ValueError("Remediation selection must preserve the 24 missing shorter repetitions")
        extra = ["E5-sustained_concurrency-control", "E5-sustained_concurrency-progress",
                 "E5-fixed_rate-control", "E5-fixed_rate-progress"]
        if include_fast_case:
            extra.append("E3-fast-zero-delay")
        for name in extra:
            selected[name] = {"case_id": name, "repetitions": [1],
                              "source_case": str((source_study / "current-python" / (name.replace("-progress", "-control") + "-r1")).resolve())}
        value["execution_selection"]["remediation"] = {
            "purpose": "Diagnostic repetition of every missing shorter profile, including prior failures; not a capacity pass.",
            "source_study": str(source_study.resolve()), "source_frozen_sha256": read_json(source_study / "suite-manifest.json")["frozen_sha256"],
            "cases": list(selected.values()), "e7_excluded": True}
        if observe_missing:
            from .timing_analysis import overhead_criteria
            from .timing_observer import OBSERVER_VERSION

            value["reference"]["limitations"] = (
                "Same-host endpoint write/drain evidence remains separate from the optional client-read observer. "
                "Observer-enabled cases require their own integrity, uncertainty and matched overhead gates; "
                "no packet-arrival trace or separate endpoint host is available."
            )
            schedule = []
            controls = {case["id"]: case for case in cases if case["tier"] == "observer_calibration"}
            for control in controls.values():
                if control["observer_enabled"]:
                    continue
                observed_id = control["id"].replace("-unobserved", "-observed")
                for repeat in (1, 2, 3):
                    order = (control["id"], observed_id) if repeat % 2 else (observed_id, control["id"])
                    for name in order:
                        calibration = controls[name]
                        source_id = calibration["calibration_for"].replace("-qualification-", "-exploration-")
                        schedule.append({"case_id": name, "repetition": repeat,
                                         "observer_enabled": calibration["observer_enabled"],
                                         "timing_profile": calibration["timing_profile"],
                                         "source_case": str((source_study / "current-python" / (source_id + "-r1")).resolve())})
            value["execution_selection"]["remediation"]["observer_calibration_schedule"] = schedule
            value["observation_policy"] = {
                "observer_source_sha256": suite_source_hashes()["timing_observer.py"],
                "observer_version": OBSERVER_VERSION,
                "uncertainty_budget_ns": 100_000,
                "scope": "Explicit instrumented diagnostics; each profile has three short matched pairs. No ordinary-capacity transfer.",
                "gross_overhead_criteria": overhead_criteria(),
                "duration_transfer_limit": "Matched 10-second load controls do not prove absence of observation overhead over 300 seconds.",
                "calibration_artifact_directory": "observer-overhead"}
    value["frozen_sha256"] = digest(value)
    study.mkdir(parents=True, exist_ok=True)
    write_json(destination, value)
    write_json(study / "adapter-baseline-before.json", proof)
    return value


def load_frozen(study: Path) -> dict:
    value = read_json(study / "suite-manifest.json")
    claimed = value.pop("frozen_sha256")
    if digest(value) != claimed:
        raise ValueError("Frozen manifest integrity check failed")
    value["frozen_sha256"] = claimed
    current = suite_source_hashes()
    if current != value["suite_source_hashes"]:
        raise ValueError("Suite source changed after freeze; create a new suite/study before candidate execution")
    return value


def _row(context: dict, kind: str, **metadata: Any) -> dict:
    return {"schema_version": SCHEMA_VERSION, **context, "logical_request_id": None,
            "attempt_id": None, "worker_id": "driver", "event_kind": kind,
            "event_sequence": 0, "monotonic_ns": time.monotonic_ns(),
            "clock_domain": "host-time.monotonic_ns", "observation_source": "external-driver",
            **metadata}


def iter_planned_rows(scenario: dict, context: dict) -> Iterable[dict]:
    """Finite intentions are declared before launch, never inferred from successes."""
    if scenario["mode"] == "sustained_concurrency":
        yield _row(context, "sustained_admission_intent", phase="measured", expected=scenario["expected"],
                   target_concurrency=scenario["concurrency"], active_seconds=scenario["active_seconds"])
        for index in range(2):
            logical = f"{context['case_id']}:control:{index:06d}"
            yield _row(context, "request_planned", logical_request_id=logical,
                       attempt_id=f"{logical}:attempt:1", phase="control", expected={"outcome": "completed"})
        return
    if scenario["mode"] == "poisson":
        # Independently reproduce the explicitly documented exponential-arrival
        # seed. Case allocation randomness is not used for these single-case packs.
        offsets = poisson_offsets(scenario)
    else:
        count = 1 if scenario["mode"] == "serial" else scenario["request_count"]
        offsets = (index / scenario["rate_rps"] if scenario["mode"] == "fixed_rate" else
                   index // scenario["batch_size"] if scenario["mode"] == "periodic_batch" else 0
                   for index in range(count))
    if (scenario["mode"] in {"serial", "burst_pack", "fixed_rate", "poisson"}
            or scenario["mode"] == "burst" and scenario["request_count"] == 1) and scenario["fixture"] != "tool_calls":
        for index in range(scenario["request_count"] if scenario["mode"] == "burst_pack" else 1):
            logical = f"{context['case_id']}:control:{index:06d}"
            yield _row(context, "request_planned", logical_request_id=logical,
                             attempt_id=f"{logical}:attempt:1", phase="control", expected={**scenario["expected"],
                             **({"request_marker": f"Validation-ID: {index:06d}"} if scenario["mode"] == "burst_pack" else {})})
    for index, offset in enumerate(offsets):
        logical = f"{context['case_id']}:measured:{index:06d}"
        yield _row(context, "request_planned", logical_request_id=logical,
                         attempt_id=f"{logical}:attempt:1", phase="measured", event_sequence=index,
                         scheduled_offset_ms=offset * 1000, expected={**scenario["expected"],
                         **({"request_marker": f"Validation-ID: {index:06d}"} if scenario["mode"] == "burst_pack" else {})})


def planned_rows(scenario: dict, context: dict) -> list[dict]:
    """Compatibility helper for small controls; live planning writes the iterator."""
    return list(iter_planned_rows(scenario, context))


def resource_summary(path: Path, receiver_window: dict | None = None) -> dict:
    """One pass; retain the original lifetime metric and separate descriptive spans."""
    groups: dict[str, dict] = {"lifetime": {"roles": {}, "bucket": None, "bucket_sum": 0, "peak_sum": 0}}
    first = (receiver_window or {}).get("start_ns")
    last = (receiver_window or {}).get("last_measured_receipt_ns")
    if first is not None and last is not None:
        groups.update({name: {"roles": {}, "bucket": None, "bucket_sum": 0, "peak_sum": 0}
                       for name in ("receipt_span", "after_last_receipt")})
    with path.open() as handle:
        for row in csv.DictReader(handle):
            if row["role"] not in {"candidate", "driver", "reference"}:
                raise ValueError("Unexpected resource-sample role")
            timestamp = int(row["monotonic_ns"])
            rss, cpu = int(row["rss_bytes"]), float(row["cpu_percent"])
            names = ["lifetime"]
            if first is not None and last is not None:
                if first <= timestamp <= last:
                    names.append("receipt_span")
                elif timestamp > last:
                    names.append("after_last_receipt")
            for name in names:
                group = groups[name]
                values = group["roles"].setdefault(row["role"], {
                    "count": 0, "cpu_sum": 0.0, "cpu_max": cpu,
                    "first": rss, "last": rss, "peak": rss, "descriptor_count": 0})
                values.update(count=values["count"] + 1, cpu_sum=values["cpu_sum"] + cpu,
                              cpu_max=max(values["cpu_max"], cpu), last=rss,
                              peak=max(values["peak"], rss))
                if row.get("fd_count") not in {None, ""}:
                    descriptors, sockets = int(row["fd_count"]), int(row["connections"])
                    values.update(descriptor_count=values["descriptor_count"] + 1,
                                  fd_last=descriptors, fd_max=max(values.get("fd_max", descriptors), descriptors),
                                  socket_last=sockets, socket_max=max(values.get("socket_max", sockets), sockets))
                bucket = timestamp // 1_000_000_000
                if group["bucket"] is not None and bucket < group["bucket"]:
                    raise ValueError("Resource samples are not in chronological order")
                if group["bucket"] != bucket:
                    group["peak_sum"] = max(group["peak_sum"], group["bucket_sum"])
                    group.update(bucket=bucket, bucket_sum=0)
                group["bucket_sum"] += rss

    def finish(group: dict) -> dict:
        cpu, ram, descriptors = {}, {}, {}
        for role, value in group["roles"].items():
            cpu[role] = {"unit": "percent of one core, ps lifetime average", "sample_count": value["count"],
                         "mean_sample": value["cpu_sum"] / value["count"], "max_sample": value["cpu_max"]}
            ram[role] = {"unit": "bytes", "sample_count": value["count"], "first_sample": value["first"],
                         "last_sample": value["last"], "peak_sample": value["peak"],
                         "growth": value["last"] - value["first"]}
            if value["descriptor_count"]:
                descriptors[role] = {"sample_count": value["descriptor_count"], "fd_max": value["fd_max"],
                                     "fd_last": value["fd_last"], "network_socket_fd_max": value["socket_max"],
                                     "network_socket_fd_last": value["socket_last"]}
        if not ram:
            return {}
        ram["aggregate"] = {"unit": "bytes", "peak_observed_one_second_bucket": max(group["peak_sum"], group["bucket_sum"])}
        return {"cpu": cpu, "ram": ram, "descriptor_observations": {
            "roles": descriptors, "state": "measured" if descriptors else "not_measured",
            "scope": "Sparse file-descriptor counts and IPv4/IPv6 socket descriptor counts; not established-connection telemetry."}}

    result = finish(groups["lifetime"])
    if len(groups) > 1:
        result["phase_descriptions"] = {
            "scope": "Same-host monotonic-clock alignment assumption; descriptive only, not a replacement memory gate.",
            "receipt_span": {"start_ns": first, "end_ns": last, **finish(groups["receipt_span"])},
            "after_last_receipt": {"start_ns": last,
                "scope": "Includes response drain and final reporting; does not isolate report latency.",
                **finish(groups["after_last_receipt"])}}
    return result


def export_client_events(rows: Iterable[dict], context: dict) -> list[dict]:
    events = []
    for row in rows:
        raw = row["raw"]
        if isinstance(raw.get("dispatch_ms"), (int, float)):
            events.append(_row(context, "request_dispatched", logical_request_id=row["logical_request_id"],
                attempt_id=row["attempt_id"], worker_id="baseline-process", phase=row["phase"],
                monotonic_ns=round(raw["dispatch_ms"] * 1_000_000), clock_domain=row["clock_domain"],
                observation_source="baseline-supported-report-export-retrospective",
                metadata={"raw_dispatch_ms": raw["dispatch_ms"], "response_id": row["response_id"],
                          "dispatch_lag_ms": raw.get("dispatch_lag_ms"),
                          "boundary": "Runner entered supported request call; endpoint receipt remains independent."}))
    return events


def receiver_active_window(path: Path, response_ids: sqlite3.Connection, scenario: dict) -> dict:
    from .verifier import bounded_jsonl_rows

    first = None
    last = None
    domain = None
    for row in bounded_jsonl_rows(path):
        if row.get("kind", row.get("event_kind")) != "request_received":
            continue
        identity = row.get("metadata", {}).get("response_id")
        match = response_ids.execute("SELECT measured,control FROM identities WHERE response_id=?", (identity,)).fetchone()
        if match != (1, 0):
            continue
        timestamp = row.get("monotonic_ns")
        first = timestamp if first is None else min(first, timestamp)
        last = timestamp if last is None else max(last, timestamp)
        domain = row.get("clock_domain")
    if first is None:
        return {"qualification": "unknown", "reason": "No uniquely correlated measured receipt anchors this window."}
    return {"start_ns": first, "end_ns": first + round(scenario["active_seconds"] * 1e9),
            "last_measured_receipt_ns": last,
            "clock_domain": domain, "boundary": "first uniquely correlated measured receipt plus requested duration",
            "uncertainty_ms": None, "qualification": "diagnostic_first_receipt_anchored",
            "reason": "Receiver-local arrival history only; unknown generator-start offset and omitted early offers prevent full phase alignment."}


def export_product_outputs(case_dir: Path, scenario: dict, context: dict) -> dict:
    """Stream full response evidence; scratch contains only correlation IDs."""
    from .verifier import verification_limits

    limits = verification_limits()
    metadata: dict = {}
    results = case_dir / "results-export.partial.jsonl"
    dispatches = case_dir / "dispatch-export.partial.jsonl"
    scratch = case_dir / "baseline-export.sqlite"
    connection = sqlite3.connect(scratch)
    connection.executescript("PRAGMA cache_size=-2048; PRAGMA journal_mode=OFF; PRAGMA temp_store=FILE; "
        "CREATE TABLE identities(response_id TEXT PRIMARY KEY,measured INTEGER,control INTEGER)")
    count = 0
    arrival_origins = []
    try:
        requests = iter_report_requests(case_dir / "product-report" / "report.json", metadata=metadata)
        with results.open("w") as output, dispatches.open("w") as events:
            for row in iter_normalized_results(requests, scenario, context):
                if scenario['mode'] == 'periodic_batch':
                    workload = row['raw'].get('workload', {})
                    if 'scheduled_ms' in workload and 'arrival_index' in workload:
                        arrival_origins.append(workload['scheduled_ms'] -
                            (workload['arrival_index'] // scenario['batch_size']) * 1000)
                encoded = json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                if len(encoded.encode()) > limits["max_line_bytes"]:
                    raise ValueError("Normalized result exceeds bounded JSONL record size; partial evidence retained")
                output.write(encoded)
                for event in export_client_events((row,), context):
                    events.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                if row["response_id"]:
                    connection.execute("INSERT INTO identities VALUES(?,?,?) ON CONFLICT(response_id) DO UPDATE SET "
                        "measured=measured+excluded.measured,control=control+excluded.control",
                        (row["response_id"], int(row["phase"] == "measured"), int(row["phase"] == "control")))
                count += 1
                if count % 1000 == 0:
                    connection.commit()
                    if count > limits["max_distinct_attempt_ids"] or scratch.stat().st_size > limits["max_sqlite_bytes"]:
                        raise ValueError("Product export scratch/attempt bound reached; partial evidence retained")
                    if shutil.disk_usage(case_dir).free < RESOURCE_POLICY["minimum_free_disk_bytes"]:
                        raise ValueError("Insufficient free disk during bounded product export; partial evidence retained")
                    if storage_bytes(case_dir) > RESOURCE_POLICY["case_storage_stop_bytes"]:
                        raise ValueError("Per-case disk bound reached during product export; partial evidence retained")
        connection.commit()
        if arrival_origins:
            if max(arrival_origins) - min(arrival_origins) > .001:
                raise ValueError('Inconsistent exported periodic burst schedule origins')
            metadata['validation_arrival_origin_ms'] = min(arrival_origins)
        if count > limits["max_distinct_attempt_ids"] or scratch.stat().st_size > limits["max_sqlite_bytes"]:
            raise ValueError("Product export scratch/attempt bound reached; partial evidence retained")
        window = receiver_active_window(case_dir / "receiver-events.jsonl", connection, scenario)
        index_bytes = scratch.stat().st_size
        results.replace(case_dir / "results.jsonl")
        with (case_dir / "client-events.jsonl").open("a") as output, dispatches.open() as source:
            shutil.copyfileobj(source, output, length=64 * 1024)
        dispatches.unlink()
        return {"report_metadata": metadata, "receiver_active_window": window,
                "export": {"backend": "incremental JSON values and SQLite response IDs", "request_count": count,
                           "scratch_index_bytes": index_bytes, "limits": RESOURCE_POLICY["product_report_decoder"]}}
    finally:
        connection.close()
        scratch.unlink(missing_ok=True)


def _sample_processes(processes: list[tuple[str, int]]) -> list[dict]:
    if not processes:
        return []
    result = subprocess.run(["ps", "-p", ",".join(str(pid) for _, pid in processes),
                             "-o", "pid=,pcpu=,rss="], text=True, capture_output=True)
    roles = {pid: role for role, pid in processes}
    samples = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3:
            pid = int(fields[0])
            samples.append({"monotonic_ns": time.monotonic_ns(), "pid": pid, "role": roles.get(pid),
                            "cpu_percent": float(fields[1]), "rss_bytes": int(fields[2]) * 1024,
                            "fd_count": "", "connections": "", "observation_source": "ps-process-sample"})
    return samples


def _descriptor_counts(processes: list[tuple[str, int]]) -> dict:
    """One sparse read-only query; permission gaps stay unknown."""
    binary = shutil.which("lsof")
    if binary is None:
        return {"state": "not_measured", "reason": "lsof unavailable", "counts": {}}
    command = [binary, "-nP", "-a", "-p", ",".join(str(pid) for _, pid in processes), "-Fpfnt"]
    started = time.monotonic()
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"state": "unknown", "reason": str(error), "counts": {}}
    counts = {}
    pid = descriptor = kind = None

    def finish_descriptor() -> None:
        if pid is not None and descriptor is not None and descriptor.isdigit():
            counts[pid]["fd_count"] += 1
            counts[pid]["network_socket_fd_count"] += int(kind in {"IPv4", "IPv6"})

    for line in result.stdout.splitlines():
        if line.startswith("p"):
            finish_descriptor()
            pid, descriptor, kind = int(line[1:]), None, None
            counts[pid] = {"fd_count": 0, "network_socket_fd_count": 0}
        elif line.startswith("f"):
            finish_descriptor()
            descriptor, kind = line[1:], None
        elif line.startswith("t"):
            kind = line[1:]
    finish_descriptor()
    return {"state": "measured" if counts else "unknown", "counts": counts,
            "elapsed_ms": (time.monotonic() - started) * 1000, "command": command,
            "reason": result.stderr.strip(), "returncode": result.returncode,
            "scope": "Numeric FDs and IPv4/IPv6 socket FDs, including listeners/UDP; not established connections."}


def _stop_owned(process: subprocess.Popen, *, force: bool = False) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    try:
        process.wait(timeout=RESOURCE_POLICY["shutdown_grace_seconds"])
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)


def _bounded_storage() -> None:
    # Deliberately limit only this test child. Never fill a host filesystem.
    resource.setrlimit(resource.RLIMIT_FSIZE, (32768, 32768))


class _ProgressConsumer:
    """Pace a real stderr pipe without retaining output in controller memory."""

    def __init__(self, stream, directory: Path):
        self.stream = stream
        self.directory = directory
        self.last_read = float("-inf")
        self.summary = {"mechanism": "native stderr pipe with 64 bytes consumed each second",
                        "bytes_per_read": 64, "read_interval_seconds": 1,
                        "drain_deadline_seconds": 2, "paced_bytes": 0, "paced_reads": 0,
                        "post_exit_drain_bytes": 0, "max_observed_pending_bytes": None,
                        "eof_observed": False,
                        "scope": "Consumer delay and observed unread bytes only; no pipe capacity or saturation inferred."}
        os.set_blocking(stream.fileno(), False)

    def consume(self, now: float, *, drain: bool = False) -> bool:
        if not drain and now - self.last_read < 1:
            return False
        pending = array.array("i", [0])
        try:
            fcntl.ioctl(self.stream.fileno(), termios.FIONREAD, pending, True)
            queued = pending[0]
            self.summary["max_observed_pending_bytes"] = max(self.summary["max_observed_pending_bytes"] or 0, queued)
        except OSError:
            queued = None
        try:
            chunk = os.read(self.stream.fileno(), 64 * 1024 if drain else 64)
        except BlockingIOError:
            chunk = None
        if chunk == b"":
            self.summary["eof_observed"] = True
        if chunk:
            with (self.directory / "stderr.log").open("ab") as output:
                output.write(chunk)
            field = "post_exit_drain_bytes" if drain else "paced_bytes"
            self.summary[field] += len(chunk)
        if not drain:
            self.last_read = now
            self.summary["paced_reads"] += 1
        with (self.directory / "progress-consumer.jsonl").open("a") as log:
            log.write(json.dumps({"monotonic_ns": time.monotonic_ns(), "phase": "drain" if drain else "paced",
                                  "pending_pipe_bytes": queued, "consumed_bytes": len(chunk) if chunk else 0,
                                  "eof": chunk == b""}) + "\n")
        return chunk == b""

    def finish(self) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.consume(time.monotonic(), drain=True):
            time.sleep(.01)
        self.stream.close()
        self.summary["all_stderr_retained"] = self.summary["eof_observed"]
        return self.summary


def _report(case_dir: Path) -> dict:
    from .verifier import generate_report
    return generate_report(case_dir)


def _record_missing_html(case_dir: Path, manifest: dict) -> None:
    if not (case_dir / "product-report" / "report.html").is_file():
        manifest.setdefault("artifact_absences", {})["product-report/report.html"] = (
            "HTML is optional: current Giraffe runs save JSON only; export with giraffe report RUN_DIR --html. JSON availability and execution state are tracked separately.")


def run_case(study: Path, suite: dict, scenario: dict, repetition: int,
             *, label: str = "measured", python: str | None = None,
             observer: dict | None = None, capture: bool = False) -> dict:
    """Run an independent endpoint and the real CLI; stop only owned children."""
    ensure_execution_allowed(scenario)
    if shutil.disk_usage(study).free < RESOURCE_POLICY["minimum_free_disk_bytes"] + resource_policy()["offline_verifier"]["max_sqlite_bytes"]:
        raise ValueError("Free disk cannot reserve the minimum host headroom and bounded verifier scratch; no subprocess was started")
    context = {"study_id": suite["study_id"], "case_id": f"{scenario['id']}-r{repetition}",
               "candidate": "current-python", "repetition": repetition}
    case_dir = study / context["candidate"] / context["case_id"]
    if case_dir.exists():
        raise ValueError(f"Case evidence already exists: {case_dir}; refusing to overwrite")
    case_dir.mkdir(parents=True)
    for name in ARTIFACTS:
        (case_dir / name).touch()
    write_jsonl(case_dir / "planned.jsonl", iter_planned_rows(scenario, context))
    manifest = {"schema_version": SCHEMA_VERSION, **context,
                "experiment_id": scenario["experiment_id"], "scenario": scenario,
                "purpose": scenario["purpose"], "execution_status": "not_run", "run_label": label,
                "suite_version": suite["suite_version"], "criteria_version": suite["criteria_version"],
                "suite_frozen_sha256": suite["frozen_sha256"], "thresholds": suite["thresholds"],
                "commits": {"baseline": suite["baseline"]["sha"], "engine": suite["baseline"]["sha"],
                            "suite": suite["suite_version"] + ":" + suite["frozen_sha256"]},
                "baseline": {key: suite["baseline"][key] for key in ("sha", "checkout", "product_source_sha256")},
                "resource_policy": suite["resource_policy"], "reference": suite["reference"],
                "planned_semantics": "duration_admission_intent" if scenario["mode"] == "sustained_concurrency" else "finite",
                "artifact_absences": {}, "commands": [],
                "timing_observer": observer or {"enabled": False, "reason": "Uninstrumented CLI execution."},
                "observation_mode": "instrumented_diagnostic" if observer else "uninstrumented",
                "observability": {
                    "dispatch": "Baseline report export, relative run clock; entering HTTP call is not endpoint receipt.",
                    "client_read": "Not exposed by unchanged baseline; no fabricated events.",
                    "parsing_evaluation_persistence_latency": "Not exposed; baseline final report proves saved result only.",
                    "event_contents": "Baseline retains aggregate output; individual text events unexposed.",
                    "resource_cpu": "ps averaged process CPU, not per-thread instantaneous accounting.",
                    "descriptors_connections": "Sparse lsof FD and IPv4/IPv6 socket-FD counts; includes listeners/UDP, not established connections. Permission gaps remain unknown.",
                    "network_disk_throughput": "Not collected.",
                    "retention": "all; no intentionally omitted output records.",
                },
                "hardware": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                             "observations": suite["baseline"].get("hardware_observations", {}),
                             "configured_resource_envelope_ram_bytes": 8 * 1024**3},
                "workers": {"processes": 1, "traffic_workers": "same process async coroutines", "evaluator_processes": 0},
                "phases": {"warmup": "Explicit baseline control/probe rows retained separately.",
                           "active_seconds": scenario["active_seconds"], "drain_seconds": 5, "retries": 0}}
    manifest["processes"] = {**manifest["workers"], "reference_processes": 1, "driver_processes": 1,
                             "runtime_threads": "not_measured"}
    manifest["hashes"] = {"workload_sha256": digest(scenario), "seed": scenario["seed"],
                           "suite_sha256": suite["frozen_sha256"]}
    manifest["retention"] = {"policy": "all", "intentionally_excluded_records": 0}
    manifest["token_count_provenance"] = scenario["token_count_provenance"]
    reason = support_reason(scenario)
    if reason:
        manifest.update(execution_status="blocked", execution_reason=reason)
        manifest["artifact_absences"] = {name: reason for name in ARTIFACTS if name != "planned.jsonl"}
        write_json(case_dir / "manifest.json", manifest)
        return _report(case_dir)
    baseline = Path(suite["baseline"]["checkout"])
    python = python or suite["baseline"]["python_executable"]
    before = baseline_snapshot(baseline, python)
    write_json(case_dir / "baseline-before.json", before)
    identity_fields = ("sha", "status_porcelain", "tracked_tree_sha256", "installed_dependencies_sha256")
    if before["status_porcelain"] or any(before[key] != suite["baseline"][key] for key in identity_fields):
        raise ValueError("Pinned baseline SHA, clean tree, product files or installed dependencies changed before execution")
    endpoint_scenario = dict(scenario)
    if scenario.get("interference") == "telemetry":
        endpoint_scenario["metrics_delay_ms"] = 1000
    write_json(case_dir / "endpoint-scenario.json", endpoint_scenario)
    write_json(case_dir / "endpoint-context.json", context)
    ready_path = case_dir / "endpoint-ready.json"
    endpoint_command = [python, "-m", "validation_suite.endpoint", "--host", "127.0.0.1", "--port", "0",
                        "--events", str((case_dir / "receiver-events.jsonl").resolve()), "--ready", str(ready_path.resolve()),
                        "--context", str((case_dir / "endpoint-context.json").resolve()),
                        "--scenario", str((case_dir / "endpoint-scenario.json").resolve())]
    repository = Path(__file__).resolve().parents[1]
    endpoint_env = os.environ.copy()
    endpoint_env["PYTHONPATH"] = str(repository)
    endpoint = None
    candidate = None
    progress_consumer = None
    packet_session = None
    events = []
    started = time.monotonic()
    manifest["commands"].append({"role": "reference", "argv": endpoint_command, "cwd": str(repository)})
    try:
        with (case_dir / "endpoint-stdout.log").open("w") as endpoint_stdout, (case_dir / "endpoint-stderr.log").open("w") as endpoint_stderr:
            endpoint = subprocess.Popen(endpoint_command, cwd=repository, env=endpoint_env, stdout=endpoint_stdout,
                                        stderr=endpoint_stderr, start_new_session=True)
            ready_deadline = time.monotonic() + 10
            while not ready_path.exists():
                if endpoint.poll() is not None or time.monotonic() > ready_deadline:
                    raise RuntimeError("Reference endpoint failed to become ready; see endpoint-stderr.log")
                time.sleep(.02)
            ready = read_json(ready_path)
            manifest["reference"] = {**manifest["reference"], **ready, "health_evidence": "receiver-health.json"}
            configuration = make_configuration(scenario, ready["base_url"])
            if scenario.get("fault") == "connection_refused":
                _stop_owned(endpoint)
                manifest["reference"]["expected_unavailable"] = True
                events.append(_row(context, "external_fault_injected", fault="connection_refused",
                                   mechanism="Stopped test-owned endpoint before candidate launch"))
            if scenario.get("fault") in {"bounded_storage", "global_budget"}:
                events.append(_row(context, "fault_configured", fault=scenario["fault"],
                                   mechanism="Child-only RLIMIT_FSIZE32768" if scenario["fault"] == "bounded_storage" else "Public max_requests5 configuration"))
            config_path = case_dir / "giraffe-config.json"
            if capture:
                from urllib.parse import urlsplit
                from .packet_capture import CaptureSession
                if scenario["active_seconds"] > 300:
                    raise ValueError("Packet diagnostic is restricted to at most300-second traffic windows")
                packet_session = CaptureSession(case_dir, urlsplit(ready["base_url"]).port,
                    maximum_seconds=min(420, math.ceil(configuration["max_duration_seconds"] + 30)))
                packet_session.start()
                manifest["packet_capture"] = {"enabled": True, "command": packet_session.command,
                                              "pid": packet_session.process.pid,
                                              "scope": "Owned loopback TCP endpoint; independent packet evidence"}
            write_json(config_path, configuration)
            manifest["configuration_sha256"] = digest(configuration)
            manifest["hashes"]["configuration_sha256"] = digest(configuration)
            manifest["configuration"] = configuration
            command = baseline_command(baseline, python, config_path, case_dir / "product-report")
            candidate_env = baseline_environment(baseline)
            if observer:
                manifest["underlying_unchanged_cli_argv"] = command
                command = [python, "-m", "validation_suite.timing_observer", "--baseline", str(baseline.resolve()),
                           "--events", str((case_dir / "timing-events.jsonl").resolve()),
                           "--health", str((case_dir / "timing-health.json").resolve()),
                           "--context", str((case_dir / "endpoint-context.json").resolve()),
                           "--reference-url", ready["base_url"], "--reference-pid", str(endpoint.pid),
                           "--", *command[3:]]
                candidate_env["PYTHONPATH"] = str(repository) + os.pathsep + str((baseline / "src").resolve())
            manifest["commands"].append({"role": "candidate", "argv": command, "cwd": str(baseline),
                                         "environment": {"PYTHONPATH": candidate_env["PYTHONPATH"], "PYTHONNOUSERSITE": "1"}})
            manifest["execution_status"] = "aborted"
            write_json(case_dir / "manifest.json", manifest)
            with (case_dir / "stdout.log").open("w") as stdout, (case_dir / "stderr.log").open("wb") as stderr, (case_dir / "resources.csv").open("w", newline="") as resources:
                writer = csv.DictWriter(resources, fieldnames=["monotonic_ns", "pid", "role", "cpu_percent", "rss_bytes", "fd_count", "connections", "observation_source", "descriptor_observation_source"])
                writer.writeheader()
                candidate = subprocess.Popen(command, cwd=baseline, env=candidate_env,
                    stdout=stdout, stderr=subprocess.PIPE if scenario.get("interference") == "progress" else stderr,
                    start_new_session=True,
                    preexec_fn=_bounded_storage if scenario.get("fault") == "bounded_storage" else None)
                events.append(_row(context, "candidate_process_started", pid=candidate.pid, command=command))
                if scenario.get("interference") == "progress":
                    progress_consumer = _ProgressConsumer(candidate.stderr, case_dir)
                    events.append(_row(context, "interference_configured", interference="progress",
                                       mechanism=progress_consumer.summary["mechanism"]))
                active_start = time.monotonic()
                last_sample, fault_sent = 0.0, False
                last_storage_sample = 0.0
                last_descriptor_sample = 0.0
                last_progress = active_start
                missing_resource_samples = 0
                deadline = active_start + configuration["max_duration_seconds"] + 15
                while candidate.poll() is None:
                    now = time.monotonic()
                    if progress_consumer is not None:
                        progress_consumer.consume(now)
                    fault = scenario.get("fault")
                    if not fault_sent and now - active_start >= 2 and fault in {
                            "user_cancel", "controller_crash", "terminate_before_persistence"}:
                        injected_signal = signal.SIGINT if fault == "user_cancel" else signal.SIGKILL
                        os.killpg(candidate.pid, injected_signal)
                        events.append(_row(context, "external_fault_injected", signal=injected_signal.name, fault=fault))
                        fault_sent = True
                    if now - last_sample >= 1:
                        samples = _sample_processes([("candidate", candidate.pid), ("reference", endpoint.pid), ("driver", os.getpid())])
                        if now - last_descriptor_sample >= RESOURCE_POLICY["descriptor_sample_seconds"]:
                            descriptors = _descriptor_counts([("candidate", candidate.pid), ("reference", endpoint.pid), ("driver", os.getpid())])
                            with (case_dir / "descriptor-samples.jsonl").open("a") as descriptor_log:
                                descriptor_log.write(json.dumps({"monotonic_ns": time.monotonic_ns(), **descriptors}) + "\n")
                            for sample in samples:
                                observed = descriptors["counts"].get(sample["pid"])
                                if observed is not None:
                                    sample.update(fd_count=observed["fd_count"], connections=observed["network_socket_fd_count"],
                                                  descriptor_observation_source="sparse lsof; network socket FDs, not established connections")
                            last_descriptor_sample = now
                        writer.writerows(samples)
                        resources.flush()
                        candidate_samples = [sample for sample in samples if sample["role"] == "candidate"]
                        candidate_rss = sum(sample["rss_bytes"] for sample in candidate_samples) if candidate_samples else None
                        expected_roles = {"candidate", "driver"} | ({"reference"} if endpoint.poll() is None else set())
                        missing_roles = expected_roles - {sample["role"] for sample in samples}
                        total_rss = sum(sample["rss_bytes"] for sample in samples) if not missing_roles else None
                        missing_resource_samples = missing_resource_samples + 1 if missing_roles else 0
                        stop_reason = None
                        if ((total_rss is not None and total_rss > RESOURCE_POLICY["aggregate_owned_rss_stop_bytes"])
                                or (candidate_rss is not None and candidate_rss > RESOURCE_POLICY["candidate_rss_stop_bytes"])):
                            stop_reason = "Frozen owned-process RSS safety threshold exceeded"
                        if missing_roles:
                            manifest["resource_observation_gaps"] = manifest.get("resource_observation_gaps", 0) + 1
                            manifest["resource_observation_reason"] = "Process sampler omitted roles: " + ", ".join(sorted(missing_roles))
                        if missing_resource_samples >= RESOURCE_POLICY["max_missing_resource_samples"]:
                            stop_reason = "Resource sampler unavailable for three consecutive samples; safe memory headroom cannot be established"
                        if shutil.disk_usage(case_dir).free < RESOURCE_POLICY["minimum_free_disk_bytes"]:
                            stop_reason = "Frozen minimum free disk safety threshold reached"
                        if now - last_storage_sample >= RESOURCE_POLICY["storage_check_seconds"]:
                            storage = storage_bytes(study)
                            if storage > RESOURCE_POLICY["study_storage_stop_bytes"]:
                                stop_reason = "Frozen study evidence storage threshold reached"
                            case_storage = storage_bytes(case_dir)
                            if case_storage > RESOURCE_POLICY["case_storage_stop_bytes"]:
                                stop_reason = "Frozen per-case evidence storage threshold reached"
                            last_storage_sample = now
                        pressure = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                                  text=True, capture_output=True)
                        pressure_value = pressure.stdout.strip() if pressure.returncode == 0 else None
                        pressure_summary = manifest.setdefault("host_pressure_observations", {
                            "sample_counts_by_level": {}, "unavailable_samples": 0,
                            "scope": "OS pressure is a safety diagnostic; level2 alone is not exhaustion. Only critical level4 triggers this stop."})
                        if pressure_value is None:
                            pressure_summary["unavailable_samples"] += 1
                        else:
                            levels = pressure_summary["sample_counts_by_level"]
                            levels[pressure_value] = levels.get(pressure_value, 0) + 1
                        pressure_summary["last_level"] = pressure_value
                        if pressure.returncode == 0 and pressure.stdout.strip() == "4":
                            stop_reason = "Host reports critical memory pressure"
                        if stop_reason:
                            manifest["execution_reason"] = stop_reason
                            manifest["interrupted_by_driver"] = True
                            events.append(_row(context, "resource_safety_stop", reason=stop_reason))
                            _stop_owned(candidate)
                            break
                        last_sample = now
                    if now - last_progress >= 60:
                        health_path = case_dir / "receiver-health.json"
                        try:
                            health = read_json(health_path) if health_path.exists() else {}
                        except (OSError, ValueError):
                            health = {"state": "unknown", "reason": "Reference health checkpoint not readable at this sample."}
                        status = {**context, "phase": "candidate_running", "elapsed_seconds": now - active_start,
                                  "candidate_pid": candidate.pid, "resource_samples": samples,
                                  "receiver_recording_health": health,
                                  "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                        write_json(case_dir / "live-status.json", status)
                        write_json(study / "current-case-status.json", status)
                        rss_text = f"{candidate_rss}B" if candidate_rss is not None else "unknown"
                        print(f"PROGRESS {context['case_id']} elapsed={now-active_start:.1f}s "
                              f"rss={rss_text} recorder_written={health.get('written', 'unknown')}", flush=True)
                        last_progress = now
                    if now > deadline:
                        manifest["execution_reason"] = "External case deadline exceeded"
                        manifest["interrupted_by_driver"] = True
                        _stop_owned(candidate)
                        break
                    time.sleep(.05)
                candidate.wait()
                manifest["returncode"] = candidate.returncode
                manifest["execution_status"] = "completed" if candidate.returncode in {0, 1, 2} and not manifest.get("interrupted_by_driver") else "aborted"
                if fault_sent:
                    manifest["execution_reason"] = "Expected external fault injected; partial evidence preserved"
                events.append(_row(context, "candidate_process_exited", returncode=candidate.returncode))
    except (OSError, ValueError, RuntimeError) as error:
        manifest.update(execution_status="blocked", execution_reason=f"{type(error).__name__}: {error}")
    finally:
        if candidate is not None:
            _stop_owned(candidate)
        if progress_consumer is not None:
            manifest["progress_consumer"] = progress_consumer.finish()
        if endpoint is not None:
            _stop_owned(endpoint)
        if packet_session is not None:
            manifest["packet_capture_health"] = packet_session.finish()
        if observer:
            for name in ("timing-events.jsonl", "timing-health.json"):
                if not (case_dir / name).exists():
                    manifest["artifact_absences"][name] = "Explicit observer execution did not retain this artifact."
        manifest["wall_seconds"] = time.monotonic() - started
        write_jsonl(case_dir / "client-events.jsonl", events)
        product_report = case_dir / "product-report" / "report.json"
        if product_report.exists():
            try:
                exported = export_product_outputs(case_dir, scenario, context)
                report = exported["report_metadata"]
                manifest["product_export"] = exported["export"]
                manifest["observability"]["dispatch_events"] = True
                manifest["observability"]["complete_response_evidence"] = True
                manifest["phases"] = exported_phases(report, scenario, context)
                manifest["receiver_active_window"] = exported["receiver_active_window"]
                manifest["product_overall"] = report.get("overall")
                manifest["product_abort_reason"] = report.get("abort_reason")
                manifest["product_observations"] = report.get("observations", {})
            except (OSError, ValueError, sqlite3.Error) as error:
                manifest["artifact_absences"]["results.jsonl"] = f"Saved product report unreadable: {error}"
        else:
            manifest["artifact_absences"]["results.jsonl"] = "Baseline writes its final report after run; no readable product report survived."
            if manifest.get("returncode") in {0, 1, 2} and not manifest.get("interrupted_by_driver"):
                with (case_dir / "receiver-events.jsonl").open() as receiver:
                    traffic_started = any(json.loads(line).get("kind") == "request_received" for line in receiver)
                manifest["execution_status"] = "aborted" if traffic_started else "blocked"
                manifest["execution_reason"] = "CLI exited without a saved run report; independent receipts prove execution began." if traffic_started else "CLI exited without traffic or a saved run report."
        _record_missing_html(case_dir, manifest)
        if scenario.get("fault") == "bounded_storage":
            fault_output = (case_dir / "stdout.log").read_text() + (case_dir / "stderr.log").read_text()
            confirmed = "[Errno 27]" in fault_output or "File too large" in fault_output or manifest.get("returncode") == -signal.SIGXFSZ
            manifest["storage_failure_confirmed"] = confirmed
            manifest["storage_failure_evidence"] = ["stdout.log", "stderr.log"] if confirmed else []
        manifest["resource_summary"] = resource_summary(case_dir / "resources.csv", manifest.get("receiver_active_window"))
        after = baseline_snapshot(baseline, python)
        write_json(case_dir / "baseline-after.json", after)
        manifest["baseline_unchanged"] = all(before[key] == after[key] for key in (
            "sha", "status_porcelain", "tracked_tree_sha256", "installed_dependencies_sha256"))
        if not manifest["baseline_unchanged"]:
            manifest["execution_reason"] = "Baseline identity/source/dependencies changed during run"
        write_json(case_dir / "manifest.json", manifest)
    return _report(case_dir)


def projected_feasibility(case_dir: Path, target: dict, *, destination_study: Path | None = None) -> dict:
    """Project saved raw volume; never treat an online RSS pass as offline safety."""
    manifest = read_json(case_dir / "manifest.json")
    from .verifier import verification_limits

    source_duration = manifest["scenario"]["active_seconds"]
    phases = manifest.get("phases", {})
    if phases.get("active_start_ns") is not None and phases.get("active_end_ns") is not None:
        observed = (phases["active_end_ns"] - phases["active_start_ns"]) / 1e9
        if observed > 0:
            source_duration = min(source_duration, observed)
    multiplier = target["active_seconds"] / source_duration
    names = ("planned.jsonl", "client-events.jsonl", "receiver-events.jsonl", "results.jsonl")
    sizes, counts, max_lines = {}, {}, {}
    for name in names:
        path = case_dir / name
        sizes[name] = math.ceil(path.stat().st_size * multiplier)
        count = maximum = 0
        with path.open("rb") as handle:
            for line in handle:
                count += 1
                maximum = max(maximum, len(line))
        counts[name] = math.ceil(count * multiplier)
        max_lines[name] = maximum
    limits = verification_limits()
    feasible = max(max_lines.values()) <= limits["max_line_bytes"] and counts["results.jsonl"] <= limits["max_distinct_attempt_ids"]
    product = case_dir / "product-report" / "report.json"
    product_bytes = math.ceil(product.stat().st_size * multiplier) if product.exists() else None
    feasible = feasible and product_bytes is not None
    study = destination_study or case_dir.parents[1]
    existing_storage = storage_bytes(study)
    source_storage = storage_bytes(case_dir)
    projected_storage = math.ceil(source_storage * multiplier * RESOURCE_POLICY["storage_projection_multiplier"])
    reservation = projected_storage + limits["max_sqlite_bytes"]
    free_disk = shutil.disk_usage(study).free
    feasible = feasible and reservation <= RESOURCE_POLICY["case_storage_stop_bytes"] and existing_storage + reservation <= RESOURCE_POLICY["study_storage_stop_bytes"] and free_disk >= reservation + RESOURCE_POLICY["minimum_free_disk_bytes"]
    return {"feasible": feasible, "projection": "linear from matching observed active duration with 25% disk margin and worst-case SQLite reservation; not a capacity claim",
            "source_case": str(case_dir), "multiplier": multiplier, "projected_bytes": sizes,
            "projected_rows": counts, "projected_product_report_bytes": product_bytes, "limits": limits,
            "source_active_seconds": source_duration, "largest_source_line_bytes": max_lines,
            "existing_study_storage_bytes": existing_storage, "projected_case_storage_bytes": projected_storage,
            "scratch_reservation_bytes": limits["max_sqlite_bytes"], "required_reservation_bytes": reservation,
            "free_disk_bytes": free_disk}


def campaign(study: Path, suite: dict, *, stage: str = "all") -> None:
    """A restartable sequential campaign; existing attempts are never replayed."""
    if stage == "endurance":
        raise ValueError("E7 is not selected in this reviewed campaign; no subprocess was started")
    remediation = suite.get("execution_selection", {}).get("remediation")
    if stage == "all" and remediation:
        stage = "remediation"
    lookup = {case["id"]: case for case in suite["cases"]}
    decisions_path = study / "campaign-decisions.json"
    decisions = read_json(decisions_path) if decisions_path.exists() else {"schema_version": 1, "stages": {}}

    def execute(case: dict, repetition: int = 1, *, calibration_item: dict | None = None) -> dict | None:
        ensure_execution_allowed(case)
        case_dir = study / "current-python" / f"{case['id']}-r{repetition}"
        if (case_dir / "report.json").exists():
            return read_json(case_dir / "report.json")
        if case_dir.exists():
            decisions.setdefault("unresolved_existing_attempts", []).append(str(case_dir))
            write_json(decisions_path, decisions)
            print(f"PRESERVED incomplete prior attempt {case_dir}; no automatic duplicate replay", flush=True)
            return None
        item = calibration_item or next((item for item in (remediation or {}).get("cases", []) if item["case_id"] == case["id"]), None)
        if item is not None:
            source_case = Path(item["source_case"])
            if not (source_case / "manifest.json").exists():
                decisions.setdefault("resource_blocked_cases", []).append({"case_id": case["id"], "repetition": repetition,
                    "reason": "Matching source evidence is unavailable for a safe prelaunch reservation."})
                write_json(decisions_path, decisions)
                return None
            projection = projected_feasibility(source_case, case, destination_study=study)
            decisions.setdefault("prelaunch_reservations", []).append({"case_id": case["id"], "repetition": repetition,
                                                                       **projection})
            write_json(decisions_path, decisions)
            if not projection["feasible"]:
                reason = "Projected case, scratch, study or free-disk reservation is not safe; no candidate launched."
                decisions.setdefault("resource_blocked_cases", []).append({"case_id": case["id"], "repetition": repetition, "reason": reason})
                write_json(decisions_path, decisions)
                print(f"RESOURCE BLOCKED {case['id']} repetition={repetition}: {reason}", flush=True)
                return None
        storage = storage_bytes(study)
        if storage >= RESOURCE_POLICY["study_storage_stop_bytes"] or shutil.disk_usage(study).free < RESOURCE_POLICY["minimum_free_disk_bytes"]:
            reason = "Frozen study storage/free-disk envelope exhausted; remaining cases are resource blocked."
            decisions["resource_blocked_reason"] = reason
            write_json(decisions_path, decisions)
            write_json(study / "campaign-status.json", {"stage": stage, "status": "resource_blocked", "reason": reason})
            write_summary(study, suite)
            write_json(study / "adapter-baseline-after.json", baseline_snapshot(Path(suite["baseline"]["checkout"]), suite["baseline"]["python_executable"]))
            raise RuntimeError(reason)
        write_json(study / "campaign-status.json", {"stage": stage, "case_id": case["id"],
                   "repetition": repetition, "status": "running", "pid": os.getpid()})
        print(f"CASE {case['id']} repetition={repetition} duration={case['active_seconds']}s", flush=True)
        observer = None
        if item and item.get("observer_enabled"):
            from .verifier import timing_profile_fingerprint

            policy = suite["observation_policy"]
            observer = {"enabled": True, "observer_source_sha256": policy["observer_source_sha256"],
                        "version": policy["observer_version"], "uncertainty_budget_ns": policy["uncertainty_budget_ns"],
                        "workload_fingerprint": timing_profile_fingerprint(case),
                        "scope": policy["scope"], "calibration_constituent": calibration_item is not None}
            artifact = study / "observer-overhead" / (item["timing_profile"] + ".json")
            if calibration_item is None and artifact.exists():
                observer["overhead_evidence"] = {"path": str(artifact.resolve()),
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "profile": item["timing_profile"]}
        report = run_case(study, suite, case, repetition,
                          label="observer_calibration" if calibration_item else
                                "diagnostic_missing_repetition" if stage == "remediation" else "measured",
                          observer=observer)
        write_summary(study, suite)
        return report

    def healthy(report: dict) -> bool:
        verdicts = report["verdicts"]
        return all(verdicts.get(name) == "pass" for name in (
            "requested_load_delivered", "response_accounting_correctness", "evaluation_correctness")) and verdicts.get("reference_test_valid") == "yes"

    def setup_valid(report: dict | None) -> bool:
        if not report or report.get("execution_status") != "completed":
            return False
        verdicts = report.get("verdicts", {})
        return verdicts.get("reference_test_valid") == "yes" and all(verdicts.get(key) == "pass" for key in
            ("response_accounting_correctness", "evaluation_correctness"))

    def stop_for_setup(reason: str) -> None:
        decisions["setup_blocked_reason"] = reason
        write_json(decisions_path, decisions)
        write_json(study / "campaign-status.json", {"stage": stage, "status": "setup_blocked",
                   "reason": reason, "pid": os.getpid()})
        write_summary(study, suite)
        write_json(study / "adapter-baseline-after.json",
                   baseline_snapshot(Path(suite["baseline"]["checkout"]), suite["baseline"]["python_executable"]))

    if stage in {"remediation", "calibration"}:
        if not remediation:
            raise ValueError("Frozen study has no reviewed remediation selection")
        schedule = remediation.get("observer_calibration_schedule", [])
        if stage == "calibration" and not schedule:
            raise ValueError("Frozen study has no reviewed calibration schedule")
        calibration_reports = []
        for item in schedule:
            calibration_reports.append(execute(lookup[item["case_id"]], item["repetition"], calibration_item=item))
        if schedule:
            from .timing_analysis import analyze_overhead_pairs

            directory = study / "observer-overhead"
            directory.mkdir(exist_ok=True)
            profiles = dict.fromkeys(item["timing_profile"] for item in schedule)
            for profile in profiles:
                entries = [item for item in schedule if item["timing_profile"] == profile]
                pairs = []
                for repetition in (1, 2, 3):
                    pair = {"repetition": repetition}
                    for item in entries:
                        if item["repetition"] == repetition:
                            key = "observed_case_dir" if item["observer_enabled"] else "control_case_dir"
                            pair[key] = str(study / "current-python" / f"{item['case_id']}-r{repetition}")
                    pairs.append(pair)
                output = directory / (profile + ".json")
                try:
                    calibration = lookup[entries[0]["case_id"]]
                    pace = calibration.get("event_delay_ms", 0) if calibration.get("stream") else 0
                    analyze_overhead_pairs(profile, pairs, output,
                        observer_source_sha256=suite["observation_policy"]["observer_source_sha256"],
                        baseline_sha=suite["baseline"]["sha"], pace_ms=pace or None)
                except (OSError, ValueError, RuntimeError) as error:
                    write_json(output, {"observer_source_sha256": suite["observation_policy"]["observer_source_sha256"],
                        "baseline_sha": suite["baseline"]["sha"], "profiles": {profile: {
                            "overhead_gate_pass": False, "analysis_error": str(error)}}})
            decisions["stages"]["observer_calibration"] = f"{len(schedule)} reviewed short controls attempted; failed/missing overhead evidence cannot qualify timing"
            write_json(decisions_path, decisions)
            if remediation.get("require_calibration_pass"):
                failed = [profile for profile in profiles if read_json(directory / (profile + ".json"))
                          .get("profiles", {}).get(profile, {}).get("overhead_gate_pass") is not True]
                if failed:
                    stop_for_setup("Reviewed low-load observation setup failed; no following case launched: " + ", ".join(failed))
                    return
            if remediation.get("require_calibration_integrity") and not all(setup_valid(report) for report in calibration_reports):
                stop_for_setup("Low-load execution, fixture correctness or endpoint-reference integrity failed; no following case launched.")
                return
        if stage == "calibration":
            write_json(study / "campaign-status.json", {"stage": stage, "status": "setup_review_required",
                       "reason": "Short controls retained; explicit parent review/release is required before the longer stage.", "pid": os.getpid()})
            write_summary(study, suite)
            write_json(study / "adapter-baseline-after.json",
                       baseline_snapshot(Path(suite["baseline"]["checkout"]), suite["baseline"]["python_executable"]))
            return
        print("STAGE remediation: reviewed explicit repetitions; observed failures remain diagnostic", flush=True)
        for item in remediation["cases"]:
            for repetition in item["repetitions"]:
                report = execute(lookup[item["case_id"]], repetition)
                gate = item.get("gate_after")
                if gate:
                    verdicts = report.get("verdicts", {}) if report else {}
                    passed = setup_valid(report)
                    if gate == "healthy_setup":
                        passed = passed and verdicts.get("requested_load_delivered") == "pass"
                    if not passed:
                        stop_for_setup(f"Reviewed {gate} gate did not pass for {item['case_id']}-r{repetition}; no following case launched.")
                        return
        decisions["stages"]["remediation"] = "attempted reviewed shorter cases; individual failures, blocks and partial evidence retained"
        decisions["stages"]["endurance"] = "not_run; not selected in this reviewed campaign; conditional longer-run authorization exists but observation prerequisites remain unresolved"
        write_json(decisions_path, decisions)

    if stage in {"all", "initial"}:
        print("STAGE initial correctness, smoke and exploratory matrix", flush=True)
        for case in suite["cases"]:
            if case["tier"] in {"smoke", "exploration"}:
                execute(case)
        decisions["stages"]["initial"] = "completed; individual blocked/aborted cases retained"
        write_json(decisions_path, decisions)
    selected = decisions.get("qualification_selection")
    if stage in {"all", "qualification"} and selected is None:
        selected = []
        rejected = []
        for experiment, dimension in (("E3", "concurrency"), ("E4", "rate_rps")):
            eligible = []
            for case in suite["cases"]:
                if not (case["experiment_id"] == experiment and case["tier"] == "exploration"
                        and case["id"].startswith(f"{experiment}-exploration-")):
                    continue
                directory = study / "current-python" / f"{case['id']}-r1"
                if not (directory / "report.json").exists() or not healthy(read_json(directory / "report.json")):
                    rejected.append({"case_id": case["id"], "reason": "No passing delivery/correctness/scoring exploratory evidence."})
                    continue
                suffix = f"c{case[dimension]}" if experiment == "E3" else f"r{case[dimension]}"
                target = lookup[f"{experiment}-qualification-{suffix}"]
                projection = projected_feasibility(directory, target)
                if projection["feasible"]:
                    eligible.append(target)
                else:
                    rejected.append({"case_id": target["id"], "reason": "Projected evidence exceeds frozen offline safety envelope.", "projection": projection})
            if eligible:
                eligible.sort(key=lambda case: case[dimension])
                selected.extend(dict.fromkeys((eligible[0]["id"], eligible[-1]["id"])))
        decisions["qualification_selection"] = selected
        decisions["qualification_not_selected"] = rejected
        decisions["timing_limit"] = "Selection is provisional for delivery/correctness; unqualified timing prevents a capacity pass."
        write_json(decisions_path, decisions)
    if stage in {"all", "qualification"}:
        print(f"STAGE qualification selected={selected}", flush=True)
        for name in selected or []:
            case = lookup[name]
            for repetition in range(1, case["repetitions"] + 1):
                execute(case, repetition)
        decisions["stages"]["qualification"] = "completed; unselected frozen profiles remain not_run"
        write_json(decisions_path, decisions)
    if stage == "all":
        decisions["stages"]["endurance"] = "not_run; not selected in this reviewed campaign; conditional longer-run authorization exists but observation prerequisites remain unresolved"
        write_json(decisions_path, decisions)
    write_json(study / "campaign-status.json", {"stage": stage, "status": "finished", "pid": os.getpid(),
                                               "decisions": str(decisions_path)})
    after = baseline_snapshot(Path(suite["baseline"]["checkout"]), suite["baseline"]["python_executable"])
    write_json(study / "adapter-baseline-after.json", after)


def write_summary(study: Path, suite: dict) -> None:
    """Use the shared renderer for measured and explicitly unrun repetitions."""
    from .reporting import empty_report, metric, render_summary, set_metric

    reports = []
    decisions = read_json(study / "campaign-decisions.json") if (study / "campaign-decisions.json").exists() else {}
    not_run_reason = decisions.get("setup_blocked_reason", decisions.get("resource_blocked_reason", "Frozen case has not been executed."))
    for case in suite["cases"]:
        for repetition in range(1, case["repetitions"] + 1):
            case_id = f"{case['id']}-r{repetition}"
            path = study / "current-python" / case_id / "report.json"
            if path.exists():
                reports.append(read_json(path))
                continue
            report = empty_report({"case_id": case_id, "candidate": "current-python",
                                   "repetition": repetition, "experiment_id": case["experiment_id"],
                                   "scenario": case, "execution_status": "not_run"})
            verdicts = {"requested_load_delivered": "inconclusive",
                        "response_accounting_correctness": "inconclusive",
                        "measurement_accuracy": "unknown", "evaluation_correctness": "inconclusive",
                        "reference_test_valid": "unknown", "expected_fault_behavior": "not_applicable",
                        "overall_experiment_result": "not run", "capacity_contribution": "inconclusive"}
            report["verdicts"] = verdicts
            for label, value in verdicts.items():
                set_metric(report, label, metric(value, state="not_measured", reason=not_run_reason,
                                                evidence=["suite-manifest.json"]))
            reports.append(report)
    header = {**suite, "baseline_sha": suite["baseline"]["sha"],
              "hardware": {"platform": suite["baseline"].get("platform"),
                           "cpu_count": suite["baseline"].get("cpu_count"),
                           "resource_policy": suite["resource_policy"]}}
    (study / "summary.md").write_text(render_summary(header, reports))


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(prog="python -m validation_suite")
    commands = app.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Print named cases, coverage, and wall-time envelope without execution")
    plan.add_argument("--json", action="store_true")
    create = commands.add_parser("freeze", help="Freeze criteria/cases/source hashes before candidate runs")
    create.add_argument("--study", required=True, type=Path)
    create.add_argument("--baseline", required=True, type=Path)
    create.add_argument("--python", default=sys.executable)
    create.add_argument("--source-study", type=Path, help="Original study with preserved 24 missing shorter repetitions")
    create.add_argument("--include-fast", action="store_true", help="Include reviewed 60-second fast-case retry in remediation selection")
    create.add_argument("--observe-missing", action="store_true", help="Freeze matched 10-second observation controls and explicit observed 300-second diagnostics")
    create.add_argument("--execution-plan", type=Path, help="Freeze a reviewed explicit subset with fresh case identities and setup gates")
    run = commands.add_parser("run", help="Execute exact frozen cases sequentially through unchanged CLI")
    run.add_argument("--study", required=True, type=Path)
    run.add_argument("--case", action="append", required=True)
    run.add_argument("--repetition", type=int, action="append")
    run.add_argument("--label", choices=["measured", "preflight", "diagnostic_missing_repetition"], default="measured")
    campaign_command = commands.add_parser("campaign", help="Run restartable sequential stages with frozen safety and selection rules")
    campaign_command.add_argument("--study", required=True, type=Path)
    campaign_command.add_argument("--stage", choices=["all", "initial", "qualification", "calibration", "remediation", "endurance"], default="all")
    for name in ("replay", "reconcile"):
        replay = commands.add_parser(name, help="Recompute reconciliation and reports from saved raw evidence")
        replay.add_argument("--case-dir", required=True, type=Path)
    summary = commands.add_parser("summary", help="Regenerate complete study table, retaining unrun rows")
    summary.add_argument("--study", required=True, type=Path)
    return app


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "plan":
            cases = scenario_matrix()
            exclusions = {case["id"]: EXECUTION_POLICY["reason"] for case in cases
                          if case["experiment_id"] in EXECUTION_POLICY["excluded_experiments"]
                          or case["active_seconds"] > EXECUTION_POLICY["maximum_active_seconds"]}
            value = {"cases": cases, "coverage_map": CONCERNS, "thresholds": THRESHOLDS,
                     "execution_policy": EXECUTION_POLICY, "execution_exclusions": exclusions,
                     "max_active_seconds_if_all_executable_cases_run": sum(case["active_seconds"] * case["repetitions"]
                         for case in cases if case["id"] not in exclusions and support_reason(case) is None)}
            if args.json:
                print(json.dumps(value, indent=2))
            else:
                for case in cases:
                    reason = "EXCLUDED: " + exclusions[case["id"]] if case["id"] in exclusions else support_reason(case) or ""
                    print(f"{case['id']:44} {case['tier']:14} {case['active_seconds']:5}s ×{case['repetitions']} {reason}")
            return 0
        if args.command == "freeze":
            suite = freeze(args.study.resolve(), args.baseline.resolve(), args.python,
                           source_study=args.source_study.resolve() if args.source_study else None,
                           include_fast_case=args.include_fast, observe_missing=args.observe_missing,
                           execution_plan=args.execution_plan.resolve() if args.execution_plan else None)
            write_summary(args.study.resolve(), suite)
            print(f"Frozen {len(suite['cases'])} cases: {suite['frozen_sha256']}")
        elif args.command == "campaign":
            suite = load_frozen(args.study.resolve())
            campaign(args.study.resolve(), suite, stage=args.stage)
        elif args.command == "run":
            suite = load_frozen(args.study.resolve())
            lookup = {case["id"]: case for case in suite["cases"]}
            for name in args.case:
                if name not in lookup:
                    raise ValueError(f"Unknown frozen case: {name}")
                case = lookup[name]
                for repetition in args.repetition or range(1, case["repetitions"] + 1):
                    if not 1 <= repetition <= case["repetitions"]:
                        raise ValueError("Repetition outside frozen case specification")
                    print(f"Running {name} repetition {repetition}", flush=True)
                    run_case(args.study.resolve(), suite, case, repetition, label=args.label)
                    write_summary(args.study.resolve(), suite)
            after = baseline_snapshot(Path(suite["baseline"]["checkout"]), suite["baseline"]["python_executable"])
            write_json(args.study / "baseline-after.json", after)
        elif args.command in {"replay", "reconcile"}:
            study = args.case_dir.resolve().parents[1]
            if (study / "suite-manifest.json").exists():
                load_frozen(study)
            report = _report(args.case_dir.resolve())
            print(json.dumps(report.get("verdicts", {}), indent=2))
        elif args.command == "summary":
            suite = load_frozen(args.study.resolve())
            write_summary(args.study.resolve(), suite)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"validation-suite: {error}", file=sys.stderr)
        return 2
