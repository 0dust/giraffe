"""Bounded offline timing/overhead analysis; does not change the observer."""

from __future__ import annotations

import hashlib
import csv
import base64
import json
from pathlib import Path
import shutil
import sqlite3
import statistics
from typing import Any, Iterator

from .timing_observer import validate_timing_health


def overhead_criteria() -> dict[str, Any]:
    return {"version": "paired-overhead-1", "minimum_actual_pairs": 3,
            "case_statistic": "median of measured request elapsed_ms; median of applicable max_stream_gap_ms",
            "median_elapsed_absolute_ms": 1, "median_elapsed_relative": .05,
            "maximum_pair_elapsed_absolute_ms": 3, "maximum_pair_elapsed_relative": .10,
            "paced_gap_absolute_ceiling_ms": 1, "paced_gap_relative": .10,
            "paired_median_throughput_loss_fraction": .05,
            "throughput_populations": ["measured completions in exported active window",
                                       "measured receipts in receiver-local first-receipt-anchored requested window"],
            "reference_uncertainty_budget_ns": 100_000,
            "max_records_per_case": 250_000, "max_jsonl_line_bytes": 2 * 1024**2,
            "scope": "Matched observed/unobserved finite controls for this exact workload geometry; not a capacity qualification"}


def _rows(path: Path, limit: int = 2 * 1024**2) -> Iterator[dict[str, Any]]:
    with path.open("rb") as stream:
        while True:
            line = stream.readline(limit + 1)
            if not line:
                break
            if len(line) > limit:
                raise ValueError("Timing analysis JSONL line bound exceeded")
            if line.strip():
                yield json.loads(line)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _case_statistics(directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    elapsed, gaps = [], []
    identities = set()
    active_completions = successful_completions = timestamp_samples = measured_rows = 0
    phases = manifest.get("phases", {})
    start, end = phases.get("active_start_ns"), phases.get("active_end_ns")
    duration = manifest["scenario"].get("active_seconds")
    for row in _rows(directory / "results.jsonl"):
        if row.get("phase", "measured") != "measured":
            continue
        measured_rows += 1
        if row.get("response_id"):
            identities.add(row["response_id"])
        timing = row.get("timing", {})
        raw = row.get("raw", {})
        value = timing.get("latency_ms", raw.get("elapsed_ms"))
        gap = timing.get("max_stream_gap_ms", raw.get("max_stream_gap_ms"))
        if isinstance(value, (int, float)):
            elapsed.append(value)
        if isinstance(gap, (int, float)):
            gaps.append(gap)
        completed = timing.get("lifecycle_completed_ns")
        if completed is None and isinstance(raw.get("completed_ms"), (int, float)):
            completed = round(raw["completed_ms"] * 1e6)
        if completed is not None and row.get("clock_domain") == phases.get("clock_domain"):
            timestamp_samples += 1
        if (start is not None and end is not None and completed is not None and
                row.get("clock_domain") == phases.get("clock_domain") and start <= completed < end):
            active_completions += 1
            successful_completions += row.get("valid") is True and row.get("status") == "completed"
        if measured_rows > overhead_criteria()["max_records_per_case"]:
            raise ValueError("Timing overhead sample bound exceeded")
    if not elapsed:
        raise ValueError("No measured request latency samples for overhead comparison")
    receipt_window = manifest.get("receiver_active_window", {})
    receiver_start, receiver_end = receipt_window.get("start_ns"), receipt_window.get("end_ns")
    receipts = active_receipts = receiver_rows = receiver_sequence_errors = 0
    for row in _rows(directory / "receiver-events.jsonl"):
        if row.get("sequence") != receiver_rows:
            receiver_sequence_errors += 1
        receiver_rows += 1
        if row.get("kind", row.get("event_kind")) != "request_received":
            continue
        if row.get("metadata", {}).get("response_id") not in identities:
            continue
        receipts += 1
        if (receiver_start is not None and receiver_end is not None and
                row.get("clock_domain") == receipt_window.get("clock_domain") and
                receiver_start <= row["monotonic_ns"] < receiver_end):
            active_receipts += 1
    resource_samples = []
    path = directory / "resources.csv"
    if path.exists():
        with path.open() as stream:
            bounded = iter(lambda: stream.readline(2 * 1024**2 + 1), "")
            def lines():
                for line in bounded:
                    if len(line) > 2 * 1024**2:
                        raise ValueError("Resource sample line bound exceeded")
                    yield line
            for row in csv.DictReader(lines()):
                if row.get("role") == "candidate":
                    resource_samples.append({"cpu_percent": float(row["cpu_percent"]), "rss_bytes": int(row["rss_bytes"])})
                if len(resource_samples) > 4096:
                    raise ValueError("Resource sample count bound exceeded")
    throughput_known = (isinstance(duration, (int, float)) and duration > 0 and start is not None and
                        end is not None and receiver_start is not None and receiver_end is not None and
                        timestamp_samples == measured_rows)
    receiver_health = json.loads((directory / "receiver-health.json").read_text())
    receiver_integrity = (receiver_health.get("complete") is True and receiver_sequence_errors == 0 and
                          receiver_health.get("flush_complete") is True and
                          receiver_health.get("shutdown_complete") is True and
                          all(receiver_health.get(key) == 0 for key in
                              ("overflow_count", "truncated_count", "omitted_count", "write_errors")) and
                          receiver_health.get("written") == receiver_rows and
                          receiver_health.get("last_sequence") == receiver_rows - 1 and
                          receiver_health.get("last_checkpoint_sequence") == receiver_rows - 1 and
                          receiver_health.get("events_sha256") == _hash(directory / "receiver-events.jsonl"))
    return {"measured_request_samples": len(elapsed), "median_elapsed_ms": statistics.median(elapsed),
            "applicable_gap_samples": len(gaps), "median_max_gap_ms": statistics.median(gaps) if gaps else None,
            "measured_results": len(elapsed), "measured_receipts": receipts,
            "active_completions": active_completions, "active_successful_completions": successful_completions,
            "active_receipts": active_receipts, "throughput_known": throughput_known,
            "completion_timestamp_coverage": {"observed": timestamp_samples, "expected": measured_rows},
            "receiver_integrity_valid": receiver_integrity,
            "completion_rps": active_completions / duration if throughput_known else None,
            "receipt_rps": active_receipts / duration if throughput_known else None,
            "requested_window_seconds": duration, "receiver_window_boundary": receipt_window.get("boundary"),
            "drops": {key: phases.get(key) for key in ("dropped_local", "dropped_late", "not_offered")},
            "candidate_resource_samples": len(resource_samples),
            "candidate_sampled_peak_rss_bytes": max((row["rss_bytes"] for row in resource_samples), default=None),
            "candidate_max_cpu_percent_sample": max((row["cpu_percent"] for row in resource_samples), default=None),
            "resource_scope": "Descriptive sampled whole-case candidate process; existing RAM safety limits remain authoritative"}


def analyze_overhead_pairs(profile: str, pairs: list[dict[str, Any]], output: Path, *,
                           observer_source_sha256: str, baseline_sha: str,
                           pace_ms: float | None = None) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Overhead evidence already exists; choose a versioned destination")
    criteria = overhead_criteria()
    from .verifier import timing_profile_fingerprint
    geometry_keys = ("mode", "concurrency", "rate_rps", "event_count", "event_delay_ms", "stream", "fixture")
    checked, issues, geometry, experiment = [], [], None, None
    all_baseline, all_health = True, True
    provenance, fingerprint = [], None
    for pair in pairs:
        control = Path(pair["control_case_dir"])
        observed = Path(pair["observed_case_dir"])
        try:
            cm = json.loads((control / "manifest.json").read_text())
            om = json.loads((observed / "manifest.json").read_text())
            current_geometry = {key: om["scenario"][key] for key in geometry_keys if key in om["scenario"]}
            control_geometry = {key: cm["scenario"][key] for key in geometry_keys if key in cm["scenario"]}
            if current_geometry != control_geometry or (geometry is not None and geometry != current_geometry):
                raise ValueError("Observed/control workload geometry differs")
            geometry = current_geometry
            observed_fingerprint = timing_profile_fingerprint(om["scenario"])
            if observed_fingerprint != timing_profile_fingerprint(cm["scenario"]) or (
                    fingerprint is not None and fingerprint != observed_fingerprint):
                raise ValueError("Observed/control frozen workload fingerprint differs")
            fingerprint = observed_fingerprint
            experiment = om["experiment_id"]
            if experiment != cm["experiment_id"]:
                raise ValueError("Observed/control experiment differs")
            clean = (cm.get("baseline_unchanged") is True and om.get("baseline_unchanged") is True and
                     cm.get("baseline", {}).get("sha") == baseline_sha and
                     om.get("baseline", {}).get("sha") == baseline_sha)
            all_baseline = all_baseline and clean
            if not clean:
                issues.append("baseline_identity_or_clean_tree_unconfirmed")
            health = validate_timing_health(observed, expected_observer_sha256=observer_source_sha256)
            cs, obs = _case_statistics(control, cm), _case_statistics(observed, om)
            references_clean = cs["receiver_integrity_valid"] and obs["receiver_integrity_valid"]
            all_health = all_health and health["reference_valid_for_observed_run"] and references_clean
            if not health["reference_valid_for_observed_run"] or not references_clean:
                issues.append("observer_or_reference_health_invalid")
            checked.append({"repetition": pair["repetition"], "control_case_dir": str(control),
                            "observed_case_dir": str(observed), "control_elapsed_ms": cs["median_elapsed_ms"],
                            "observed_elapsed_ms": obs["median_elapsed_ms"],
                            "elapsed_inflation_ms": obs["median_elapsed_ms"] - cs["median_elapsed_ms"],
                            "control_gap_ms": cs["median_max_gap_ms"], "observed_gap_ms": obs["median_max_gap_ms"],
                            "gap_inflation_ms": obs["median_max_gap_ms"] - cs["median_max_gap_ms"]
                                if obs["median_max_gap_ms"] is not None and cs["median_max_gap_ms"] is not None else None,
                            "control_samples": cs["measured_request_samples"], "observed_samples": obs["measured_request_samples"],
                            "control_delivery_resources": cs, "observed_delivery_resources": obs,
                            "completion_throughput_loss_fraction": 1 - obs["completion_rps"] / cs["completion_rps"]
                                if cs["completion_rps"] and obs["completion_rps"] is not None else None,
                            "receipt_throughput_loss_fraction": 1 - obs["receipt_rps"] / cs["receipt_rps"]
                                if cs["receipt_rps"] and obs["receipt_rps"] is not None else None})
            provenance.append({str(directory / name): _hash(directory / name) for directory in (control, observed)
                               for name in ("manifest.json", "results.jsonl", "receiver-health.json", "receiver-events.jsonl", "resources.csv")
                               if (directory / name).exists()})
        except (ValueError, OSError, KeyError) as error:
            issues.append(f"pair_{pair['repetition']}: {error}")
    if len(checked) < criteria["minimum_actual_pairs"] or len({pair["repetition"] for pair in checked}) != len(checked):
        issues.append("three_distinct_actual_matched_pairs_required")
    result: dict[str, Any] = {"pairs": checked, "issues": issues, "workload": geometry,
                              "workload_fingerprint": fingerprint, "experiment_id": experiment,
                              "overhead_gate_pass": False}
    if checked:
        control_median = statistics.median(pair["control_elapsed_ms"] for pair in checked)
        differences = [pair["elapsed_inflation_ms"] for pair in checked]
        result.update(median_elapsed_inflation_ms=statistics.median(differences),
                      median_budget_ms=max(criteria["median_elapsed_absolute_ms"], control_median * criteria["median_elapsed_relative"]),
                      max_elapsed_inflation_ms=max(differences),
                      max_budget_ms=max(criteria["maximum_pair_elapsed_absolute_ms"], control_median * criteria["maximum_pair_elapsed_relative"]))
        result["elapsed_gate_pass"] = (result["median_elapsed_inflation_ms"] <= result["median_budget_ms"] and
                                       result["max_elapsed_inflation_ms"] <= result["max_budget_ms"])
        if pace_ms is not None:
            values = [pair["gap_inflation_ms"] for pair in checked if pair["gap_inflation_ms"] is not None]
            if len(values) == len(checked):
                result.update(median_gap_inflation_ms=statistics.median(values),
                              gap_budget_ms=min(criteria["paced_gap_absolute_ceiling_ms"], pace_ms * criteria["paced_gap_relative"]))
                result["gap_gate_pass"] = result["median_gap_inflation_ms"] <= result["gap_budget_ms"]
            else:
                result["gap_gate_pass"] = False
                issues.append("paced_gap_samples_missing")
        if experiment in {"E3", "E4"}:
            losses = {name: [pair[name + "_throughput_loss_fraction"] for pair in checked]
                      for name in ("completion", "receipt")}
            if any(value is None for values in losses.values() for value in values):
                issues.append("active_window_throughput_evidence_missing")
                result["throughput_gate_pass"] = False
            else:
                result["paired_median_throughput_loss"] = {
                    name: statistics.median(values) for name, values in losses.items()}
                result["throughput_gate_pass"] = all(value <= criteria["paired_median_throughput_loss_fraction"]
                    for value in result["paired_median_throughput_loss"].values())
        result["overhead_gate_pass"] = (not issues and result["elapsed_gate_pass"] and
                                        result.get("gap_gate_pass", True) and result.get("throughput_gate_pass", True))
    artifact = {"observer_source_sha256": observer_source_sha256, "baseline_sha": baseline_sha,
                "baseline_unchanged": all_baseline, "source_unchanged_during_execution": all_baseline and all_health,
                "source_identity_scope": "Pinned baseline before/after proof and exact observed module hash; no independent attestation of unrelated suite files",
                "observer_health_all_complete": all_health, "criteria": criteria,
                "profiles": {profile: result}, "input_sha256": provenance}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2) + "\n")
    return artifact


def first_body_intervals_file(events: Path, output: Path, *, state_bound: int = 4096,
                               uncertainty_budget_ns: int = 100_000) -> dict[str, Any]:
    """First positive HTTP body yield, explicitly distinct from socket first byte."""
    if output.exists():
        raise ValueError("Body reference output exists")
    active = {}
    summary = {"rows": 0, "requests": 0, "issues": [], "state_bound": state_bound,
               "peak_active": 0, "complete": False}
    with output.open("w") as destination:
        for row in _rows(events):
            if row["sequence"] != summary["rows"]:
                if "observation_sequence_invalid" not in summary["issues"]:
                    summary["issues"].append("observation_sequence_invalid")
            summary["rows"] += 1
            meta = row.get("metadata", {})
            identity = meta.get("record_id")
            if not identity:
                continue
            if row["kind"] == "client_timer_start":
                if identity in active or len(active) >= state_bound:
                    if "active_timer_duplicate_or_bound_exceeded" not in summary["issues"]:
                        summary["issues"].append("active_timer_duplicate_or_bound_exceeded")
                    continue
                active[identity] = {"timer": row, "emitted": False}
                summary["peak_active"] = max(summary["peak_active"], len(active))
            elif row["kind"] in {"client_body_chunk", "client_body_complete"} and meta.get("byte_count"):
                start = active.get(identity)
                if start is None:
                    if "body_without_observed_timer" not in summary["issues"]:
                        summary["issues"].append("body_without_observed_timer")
                    continue
                if start["emitted"]:
                    continue
                timer = start["timer"]
                lower, upper = row["lower_ns"] - timer["upper_ns"], row["upper_ns"] - timer["lower_ns"]
                metric = {"lower_ms": lower / 1e6, "upper_ms": upper / 1e6,
                          "uncertainty_width_ns": upper - lower,
                          "qualified_for_observed_run": lower >= 0 and upper - lower <= uncertainty_budget_ns}
                key = "first_http_body_yield_ms" if row["kind"] == "client_body_chunk" else "nonstream_complete_body_ms"
                destination.write(json.dumps({"record_id": identity, key: metric,
                    "boundary": meta["boundary"], "is_raw_socket_first_byte": False,
                    "is_packet_arrival": False}) + "\n")
                start["emitted"] = True
                summary["requests"] += 1
            elif row["kind"] == "client_timer_end":
                active.pop(identity, None)
    if active:
        summary["issues"].append("unresolved_timers")
    summary["complete"] = not summary["issues"] and summary["requests"] > 0
    return summary


def _owned_response_id(wire: bytes) -> str:
    """Recognize only the owned endpoint's complete, close-delimited responses."""
    header, separator, body = wire.partition(b"\r\n\r\n")
    if not separator or not header.startswith(b"HTTP/1.1 ") or len(header) > 65536:
        raise ValueError("missing_or_invalid_owned_http_header")
    fields = {}
    for line in header.split(b"\r\n")[1:]:
        key, sep, value = line.partition(b":")
        if not sep or key.lower() in fields:
            raise ValueError("invalid_or_duplicate_http_header")
        fields[key.lower()] = value.strip().lower()
    if fields.get(b"connection") != b"close" or b"transfer-encoding" in fields or b"content-encoding" in fields:
        raise ValueError("unsupported_connection_or_encoding")
    if b"content-length" in fields and int(fields[b"content-length"]) != len(body):
        raise ValueError("incomplete_or_extra_http_body")
    content_type = fields.get(b"content-type", b"").split(b";", 1)[0]
    identifiers = set()
    if content_type == b"application/json":
        if b"content-length" not in fields:
            raise ValueError("nonstream_length_required")
        item = json.loads(body)
        if not isinstance(item, dict):
            raise ValueError("response_json_object_required")
        identifiers.add(item.get("id"))
    elif content_type == b"text/event-stream":
        # This is the endpoint's constrained SSE grammar, not a general HTTP parser.
        text = body.decode("utf-8").replace("\r\n", "\n")
        blocks = text.split("\n\n")
        if blocks[-1].strip():
            raise ValueError("truncated_sse_event")
        done = False
        for block in blocks[:-1]:
            data = "\n".join(line[5:].removeprefix(" ") for line in block.split("\n") if line.startswith("data:"))
            if not data:
                continue
            if done:
                raise ValueError("bytes_after_sse_termination")
            if data == "[DONE]":
                done = True
                continue
            item = json.loads(data)
            if not isinstance(item, dict):
                raise ValueError("sse_json_object_required")
            if item.get("id") is not None:
                identifiers.add(item["id"])
        if not done:
            raise ValueError("sse_termination_missing")
    else:
        raise ValueError("unsupported_owned_content_type")
    if len(identifiers) != 1 or not all(isinstance(value, str) and value for value in identifiers):
        raise ValueError("one_unique_response_id_required")
    return next(iter(identifiers))


def owned_connection_first_byte_file(events: Path, output: Path, *, results: Path,
                                     uncertainty_budget_ns: int = 100_000,
                                     max_connection_bytes: int = 8 * 1024**2,
                                     max_index_bytes: int = 256 * 1024**2,
                                     min_free_bytes: int = 2 * 1024**3) -> dict[str, Any]:
    """Correlate complete owned close responses to exported request IDs offline.

    The boundary is the first positive application socket.recv return, including
    HTTP headers. It is not a packet timestamp, body yield, or semantic answer.
    Original file offsets are indexed on disk; payloads are read one bounded
    connection at a time. Ambiguous FD/tuple reuse fails closed.
    """
    index = output.with_suffix(output.suffix + ".sqlite")
    if output.exists() or index.exists():
        raise ValueError("First-byte analysis destination exists")
    if shutil.disk_usage(output.parent).free < min_free_bytes:
        raise ValueError("Insufficient analysis disk headroom")
    summary = {"complete": False, "event_rows": 0, "connections": 0, "invalid_connections": 0,
               "expected_requests": 0, "correlated_requests": 0, "precise_requests": 0,
               "requests_with_issues": 0,
               "issues": [], "invalid_connection_reasons": {}, "max_connection_bytes": max_connection_bytes,
               "max_index_bytes": max_index_bytes, "is_packet_arrival": False,
               "boundary": "first positive socket.recv return in asyncio transport, including HTTP headers",
               "input_sha256": {str(events): _hash(events), str(results): _hash(results)}}
    db = sqlite3.connect(index)
    try:
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA cache_size=-2048")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={max(1, max_index_bytes // page_size)}")
        db.executescript("CREATE TABLE exports(record_id TEXT,response_id TEXT);"
                         "CREATE INDEX export_record ON exports(record_id);"
                         "CREATE INDEX export_response ON exports(response_id);"
                         "CREATE TABLE timers(record_id TEXT,lo INTEGER,hi INTEGER,domain TEXT);"
                         "CREATE INDEX timer_record ON timers(record_id);"
                         "CREATE TABLE reads(connection TEXT,seq INTEGER,offset INTEGER);"
                         "CREATE INDEX connection_reads ON reads(connection,seq);"
                         "CREATE TABLE parsed(response_id TEXT,lo INTEGER,hi INTEGER,domain TEXT);"
                         "CREATE INDEX parsed_response ON parsed(response_id);")
        for number, row in enumerate(_rows(results), 1):
            if number > overhead_criteria()["max_records_per_case"]:
                raise ValueError("Export record bound exceeded")
            if row.get("phase", "measured") != "measured":
                continue
            raw = row.get("raw", row)
            identity = raw.get("id")
            response = row.get("response_id", raw.get("server_request_id"))
            if not identity or not response:
                raise ValueError("Measured export lacks exact raw and response identities")
            db.execute("INSERT INTO exports VALUES(?,?)", (identity, response))
            summary["expected_requests"] += 1
        with events.open("rb") as source:
            while True:
                offset = source.tell()
                line = source.readline(2 * 1024**2 + 1)
                if not line:
                    break
                if len(line) > 2 * 1024**2:
                    raise ValueError("Timing event line bound exceeded")
                row = json.loads(line)
                if row.get("sequence") != summary["event_rows"] and "observation_sequence_invalid" not in summary["issues"]:
                    summary["issues"].append("observation_sequence_invalid")
                summary["event_rows"] += 1
                meta = row.get("metadata", {})
                if row.get("kind") == "client_timer_start":
                    db.execute("INSERT INTO timers VALUES(?,?,?,?)", (meta.get("record_id"), row["lower_ns"],
                               row["upper_ns"], row.get("clock_domain")))
                elif row.get("kind") == "client_socket_recv":
                    key = json.dumps([meta.get("socket_fd"), meta.get("local"), meta.get("peer")], separators=(",", ":"))
                    db.execute("INSERT INTO reads VALUES(?,?,?)", (key, row["sequence"], offset))
                if summary["event_rows"] % 10000 == 0:
                    db.commit()
                    if shutil.disk_usage(output.parent).free < min_free_bytes:
                        raise ValueError("Analysis disk headroom exhausted")
            db.commit()
            for (connection,) in db.execute("SELECT DISTINCT connection FROM reads"):
                summary["connections"] += 1
                try:
                    fd, local, peer = json.loads(connection)
                    if not isinstance(fd, int) or not local or not peer:
                        raise ValueError("socket_tuple_missing")
                    wire = bytearray()
                    first, previous_upper, domain = None, None, None
                    ended = False
                    for (offset,) in db.execute("SELECT offset FROM reads WHERE connection=? ORDER BY seq", (connection,)):
                        source.seek(offset)
                        row = json.loads(source.readline(2 * 1024**2 + 1))
                        meta = row["metadata"]
                        lower, upper = row["lower_ns"], meta.get("native_return_upper_ns")
                        if (not isinstance(lower, int) or not isinstance(upper, int) or lower > upper or
                                upper > row["upper_ns"] or (previous_upper is not None and lower < previous_upper)):
                            raise ValueError("socket_timestamp_order_invalid")
                        previous_upper = upper
                        if domain is not None and domain != row.get("clock_domain"):
                            raise ValueError("socket_clock_domain_changed")
                        domain = row.get("clock_domain")
                        if meta.get("wire_capture_omitted") or not isinstance(meta.get("wire_b64"), str):
                            raise ValueError("socket_wire_omitted")
                        data = base64.b64decode(meta["wire_b64"], validate=True)
                        if len(data) != meta.get("byte_count") or hashlib.sha256(data).hexdigest() != meta.get("wire_sha256"):
                            raise ValueError("socket_wire_length_or_hash_invalid")
                        if data and ended:
                            raise ValueError("positive_read_after_eof_ambiguous_reuse")
                        ended = ended or not data
                        if data and first is None:
                            first = (lower, upper)
                        if len(wire) + len(data) > max_connection_bytes:
                            raise ValueError("connection_wire_bound_exceeded")
                        wire.extend(data)
                    identity = _owned_response_id(bytes(wire))
                    if first is None:
                        raise ValueError("no_positive_socket_read")
                    db.execute("INSERT INTO parsed VALUES(?,?,?,?)", (identity, *first, domain))
                except (ValueError, KeyError, TypeError, UnicodeError) as error:
                    summary["invalid_connections"] += 1
                    reason = str(error)[:160]
                    reasons = summary["invalid_connection_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
                    if len(reasons) > 32:
                        raise ValueError("Distinct invalid connection reason bound exceeded")
        db.commit()
        with output.open("w") as destination:
            for identity, response in db.execute("SELECT record_id,response_id FROM exports"):
                issues = list(summary["issues"])
                export_count = db.execute("SELECT count(*) FROM exports WHERE record_id=? OR response_id=?", (identity, response)).fetchone()[0]
                timer = db.execute("SELECT lo,hi,domain FROM timers WHERE record_id=? LIMIT 2", (identity,)).fetchall()
                parsed = db.execute("SELECT lo,hi,domain FROM parsed WHERE response_id=? LIMIT 2", (response,)).fetchall()
                metric = None
                if export_count != 1 or len(timer) != 1 or len(parsed) != 1:
                    issues.append("unique_export_timer_complete_connection_join_required")
                elif timer[0][2] != parsed[0][2] or not timer[0][2]:
                    issues.append("clock_domain_mismatch")
                else:
                    lower, upper = parsed[0][0] - timer[0][1], parsed[0][1] - timer[0][0]
                    summary["correlated_requests"] += 1
                    if lower < 0 or timer[0][0] > timer[0][1]:
                        issues.append("timer_socket_timestamp_order_invalid")
                    metric = {"lower_ms": lower / 1e6, "upper_ms": upper / 1e6,
                              "uncertainty_width_ns": upper - lower,
                              "qualified_for_observed_run": not issues and upper - lower <= uncertainty_budget_ns}
                    summary["precise_requests"] += metric["qualified_for_observed_run"]
                destination.write(json.dumps({"record_id": identity, "response_id": response,
                    "first_application_socket_byte_ms": metric, "issues": issues,
                    "boundary": summary["boundary"], "is_packet_arrival": False,
                    "requires_valid_observer_health_and_overhead_evidence": True}) + "\n")
                summary["requests_with_issues"] += bool(issues)
        summary["complete"] = (not summary["issues"] and summary["expected_requests"] > 0 and
                               summary["requests_with_issues"] == 0 and
                               summary["correlated_requests"] == summary["expected_requests"])
        return summary
    finally:
        db.close()
        index.unlink(missing_ok=True)
