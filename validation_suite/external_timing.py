"""Assess one externally supplied semantic-event timing record.

This is an ingestion/analysis contract, not a packet capture implementation.
It cannot supply unavailable anchors or certify an external capture tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


VERSION = "external-timing-1"
BOUNDARIES = {
    "A": "complete_event_bytes_at_client_capture_point",
    "R": "read_return_containing_final_event_byte",
    "G": "giraffe_event_timestamp_value",
    "S": "giraffe_timer_start",
}
MAX_INPUT_BYTES = 1024**2


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _interval(lower: int, upper: int) -> dict[str, int]:
    return {"lower_ns": lower, "upper_ns": upper, "uncertainty_width_ns": upper - lower}


def _difference(after: dict, before: dict) -> dict:
    return _interval(after["lower_ns"] - before["upper_ns"],
                     after["upper_ns"] - before["lower_ns"])


def _accuracy(interval: dict | None, budget: int) -> str:
    if interval is None:
        return "unknown"
    if interval["lower_ns"] >= -budget and interval["upper_ns"] <= budget:
        return "pass"
    if interval["lower_ns"] > budget or interval["upper_ns"] < -budget:
        return "fail"
    return "inconclusive"


def assess_external_timing(evidence: dict[str, Any], *, accuracy_budget_ns: int,
                           uncertainty_budget_ns: int) -> dict[str, Any]:
    """Keep arrival delay, timestamp placement and reported arithmetic separate.

    All external gate fields are assertions supplied by the named evidence
    producer. The result retains their provenance; it does not independently
    establish packet loss, clock or capture timestamp quality from booleans.
    """
    if not isinstance(evidence, dict):
        raise ValueError("External timing record must be an object")
    try:
        json.dumps(evidence, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("Finite, acyclic JSON timing record required") from error
    if not _integer(accuracy_budget_ns) or not _integer(uncertainty_budget_ns) or min(
            accuracy_budget_ns, uncertainty_budget_ns) < 0:
        raise ValueError("Nonnegative integer timing budgets required")
    if evidence.get("schema_version") != VERSION:
        raise ValueError("Unsupported external timing schema")
    scope = evidence.get("scope")
    if scope not in {"synthetic_contract_control", "observed_baseline_run"}:
        raise ValueError("Explicit synthetic or observed-baseline scope required")
    request_id, event_id = evidence.get("request_id"), evidence.get("event_id")
    event = evidence.get("event", {})
    if not isinstance(event, dict):
        raise ValueError("Semantic event must be an object")
    if not all(isinstance(value, str) and value.strip() for value in (request_id, event_id)):
        raise ValueError("Exact request and semantic-event IDs required")
    begin, end = event.get("stream_offset_begin"), event.get("stream_offset_end")
    if not _integer(begin) or not _integer(end) or not 0 <= begin < end:
        raise ValueError("Positive complete-event byte span required")
    wire_hash = event.get("wire_sha256", "")
    if not isinstance(wire_hash, str) or not re.fullmatch("[0-9a-f]{64}", wire_hash):
        raise ValueError("Complete-event wire SHA256 required")
    if event.get("field") not in {"answer", "reasoning", "tool"}:
        raise ValueError("Semantic field must be explicit")
    domain = evidence.get("reference_clock_domain")
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("Reference clock domain required")
    gates = evidence.get("gates", {})
    if not isinstance(gates, dict):
        raise ValueError("Evidence gates must be an object")
    health = gates.get("reference_health", {})
    overhead = gates.get("matched_overhead", {})
    if not isinstance(health, dict) or not isinstance(overhead, dict):
        raise ValueError("Health and overhead evidence must be objects")
    gate_issues = []
    if not (health.get("complete") is True and health.get("flush_complete") is True and
            all(_integer(health.get(key)) and health[key] == 0 for key in
                ("loss_count", "omission_count", "sequence_errors", "duplicate_count")) and
            health.get("capture_truncated") is False):
        gate_issues.append("reference_loss_or_completeness_unqualified")
    if not (overhead.get("gate_pass") is True and overhead.get("profile_matches") is True and
            _integer(overhead.get("actual_pairs")) and overhead["actual_pairs"] >= 3 and
            re.fullmatch("[0-9a-f]{64}", str(overhead.get("evidence_sha256", "")))):
        gate_issues.append("matched_overhead_evidence_unqualified")
    if gates.get("timestamp_source_validated") is not True:
        gate_issues.append("capture_timestamp_source_unqualified")
    if gates.get("clock_mapping_validated") is not True:
        gate_issues.append("clock_mapping_unqualified")
    anchors, anchor_issues = {}, {}
    mappings = evidence.get("clock_mappings", [])
    if not isinstance(mappings, list) or len(mappings) > 8 or not all(isinstance(item, dict) for item in mappings):
        raise ValueError("At most eight explicit clock mappings supported")
    raw_anchors = evidence.get("anchors", {})
    if not isinstance(raw_anchors, dict):
        raise ValueError("Timing anchors must be an object")
    for name, boundary in BOUNDARIES.items():
        raw = raw_anchors.get(name, {})
        if not isinstance(raw, dict):
            raise ValueError("Every timing anchor must be an object")
        issues = []
        if raw.get("state") != "observed":
            anchor_issues[name] = [raw.get("reason", "anchor_unavailable")]
            continue
        if (raw.get("request_id") != request_id or raw.get("event_id") != event_id or
                not _integer(raw.get("stream_offset_begin")) or not _integer(raw.get("stream_offset_end")) or
                raw.get("stream_offset_begin") != begin or raw.get("stream_offset_end") != end or
                raw.get("wire_sha256") != wire_hash):
            issues.append("exact_event_identity_or_byte_span_mismatch")
        if (raw.get("boundary") != boundary or not isinstance(raw.get("source"), str) or
                not raw["source"].strip()):
            issues.append("observation_boundary_or_source_unconfirmed")
        if name in {"A", "R", "S"} and raw.get("independent") is not True:
            issues.append("independent_reference_observation_required")
        lower, upper = raw.get("lower_ns"), raw.get("upper_ns")
        if not _integer(lower) or not _integer(upper) or lower > upper:
            issues.append("invalid_anchor_interval")
        source_domain = raw.get("clock_domain")
        if not isinstance(source_domain, str) or not source_domain.strip():
            issues.append("anchor_clock_domain_required")
        if source_domain != domain:
            found = [item for item in mappings if item.get("from_domain") == source_domain and
                     item.get("to_domain") == domain]
            if len(found) != 1:
                issues.append("unique_bounded_clock_mapping_missing")
            else:
                mapping = found[0]
                lo, hi = mapping.get("offset_lower_ns"), mapping.get("offset_upper_ns")
                if (mapping.get("validated") is not True or not mapping.get("source") or
                        not _integer(lo) or not _integer(hi) or lo > hi):
                    issues.append("invalid_clock_offset_bounds")
                elif _integer(lower) and _integer(upper):
                    lower, upper = lower + lo, upper + hi
        if issues:
            anchor_issues[name] = issues
        else:
            anchors[name] = _interval(lower, upper)
    # A packet observed at a different capture point is not a client arrival.
    if "A" in anchors and raw_anchors.get("A", {}).get("capture_location") != "client":
        anchors.pop("A")
        anchor_issues["A"] = ["client_capture_location_required"]
    causal_issues = []
    for after, before in (("R", "A"), ("A", "S"), ("R", "S")):
        if after in anchors and before in anchors and _difference(anchors[after], anchors[before])["upper_ns"] < 0:
            causal_issues.append(f"{after}_strictly_precedes_{before}")
    gate_issues.extend(causal_issues)
    metrics = {}
    definitions = {"arrival_to_read_delay": ("R", "A"),
                   "read_to_observation_delay": ("G", "R"),
                   "total_client_delay": ("G", "A"),
                   "arrival_based_latency": ("A", "S"),
                   "read_based_latency": ("R", "S"),
                   "giraffe_timestamp_latency": ("G", "S")}
    for name, (after, before) in definitions.items():
        value = _difference(anchors[after], anchors[before]) if after in anchors and before in anchors else None
        metrics[name] = {"interval": value, "precision_pass": value is not None and
                         value["uncertainty_width_ns"] <= uncertainty_budget_ns,
                         "required_anchors": [after, before]}
    reported = evidence.get("reported_latency_ns")
    reported_uncertainty = evidence.get("reported_latency_uncertainty_ns", 0)
    reported_interval = None
    if reported is not None:
        if not _integer(reported) or not _integer(reported_uncertainty) or reported_uncertainty < 0:
            raise ValueError("Reported duration requires integer ns and nonnegative uncertainty")
        reported_interval = _interval(reported - reported_uncertainty, reported + reported_uncertainty)
    for name, reference in (("reported_arithmetic_error", "giraffe_timestamp_latency"),
                            ("reported_minus_read_latency", "read_based_latency"),
                            ("arrival_reference_latency_distortion", "arrival_based_latency")):
        base = metrics[reference]["interval"]
        value = _difference(reported_interval, base) if reported_interval is not None and base is not None else None
        metrics[name] = {"interval": value, "precision_pass": value is not None and
                         value["uncertainty_width_ns"] <= uncertainty_budget_ns,
                         "required_anchors": metrics[reference]["required_anchors"],
                         "budget_comparison": _accuracy(value, accuracy_budget_ns)}
    for metric in metrics.values():
        metric["conditional_precision_and_gates_pass"] = not gate_issues and metric["precision_pass"]
    arrival = metrics["arrival_reference_latency_distortion"]
    arithmetic = metrics["reported_arithmetic_error"]
    return {"schema_version": VERSION, "scope": scope, "request_id": request_id, "event_id": event_id,
            "event": event, "metrics": metrics, "anchor_issues": anchor_issues, "gate_issues": gate_issues,
            "mapped_anchors": anchors,
            "conditional_arithmetic_assessment": arithmetic["budget_comparison"] if arithmetic["conditional_precision_and_gates_pass"] else "unknown",
            "conditional_arrival_distortion_assessment": arrival["budget_comparison"] if arrival["conditional_precision_and_gates_pass"] else "unknown",
            "measurement_claim": "Conditional calculation from supplied evidence assertions; not independently certified capture or product/run qualification",
            "actual_giraffe_run_qualified": False,
            "accuracy_budget_ns": accuracy_budget_ns, "uncertainty_budget_ns": uncertainty_budget_ns,
            "budget_scope": "Explicit caller-supplied per-record diagnostic budgets; future suite adapter must preserve min(1ms,10%metric),100us precision,99%coverage gates",
            "evidence_gate_assertions": gates,
            "attribution": "Delay includes network-stack/capture-point, OS scheduling, buffering, transport and client work; this record cannot attribute it solely to Giraffe code.",
            "capture_pipeline_implemented": False}


def assess_external_timing_file(source: Path, output: Path, **budgets) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Assessment output exists; choose a versioned destination")
    with source.open("rb") as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("External timing record exceeds 1MiB bound")
    try:
        evidence = json.loads(data)
    except (ValueError, RecursionError) as error:
        raise ValueError("Malformed external timing JSON record") from error
    result = assess_external_timing(evidence, **budgets)
    result["input_sha256"] = hashlib.sha256(data).hexdigest()
    result["analyzer_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--accuracy-budget-ns", type=int, required=True)
    parser.add_argument("--uncertainty-budget-ns", type=int, required=True)
    args = parser.parse_args()
    assess_external_timing_file(args.input, args.output, accuracy_budget_ns=args.accuracy_budget_ns,
                               uncertainty_budget_ns=args.uncertainty_budget_ns)


if __name__ == "__main__":
    main()
