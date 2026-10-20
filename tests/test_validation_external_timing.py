from __future__ import annotations

import copy
import hashlib
import json

import pytest

from validation_suite.external_timing import (BOUNDARIES, MAX_INPUT_BYTES, VERSION,
                                             assess_external_timing as _assess,
                                             assess_external_timing_file as _assess_file)


def assess_external_timing(evidence):
    return _assess(evidence, accuracy_budget_ns=1_000_000, uncertainty_budget_ns=100_000)


def assess_external_timing_file(source, output):
    return _assess_file(source, output, accuracy_budget_ns=1_000_000, uncertainty_budget_ns=100_000)


def event_evidence(*, arrival=10_000_000, read=10_000_000, giraffe=None, start=0,
                   reported=None, event_id="answer-0", begin=0, end=60, wire=None):
    giraffe = read if giraffe is None else giraffe
    wire = wire or hashlib.sha256(b"independently declared complete event bytes").hexdigest()
    result = {"schema_version": VERSION, "scope": "synthetic_contract_control", "request_id": "request-Aa",
        "event_id": event_id, "event": {"field": "answer", "stream_offset_begin": begin,
            "stream_offset_end": end, "wire_sha256": wire}, "reference_clock_domain": "fixture-ns",
        "reported_latency_ns": giraffe - start if reported is None else reported,
        "anchors": {}, "gates": {"reference_health": {"complete": True, "flush_complete": True,
            "loss_count": 0, "omission_count": 0, "sequence_errors": 0, "duplicate_count": 0,
            "capture_truncated": False}, "matched_overhead": {"gate_pass": True,
                "profile_matches": True, "actual_pairs": 3, "evidence_sha256": "b" * 64},
            "timestamp_source_validated": True, "clock_mapping_validated": True}}
    for name, value in zip("ARGS", (arrival, read, giraffe, start), strict=True):
        result["anchors"][name] = {"state": "observed", "request_id": result["request_id"],
            "event_id": event_id, "stream_offset_end": end, "wire_sha256": wire,
            "stream_offset_begin": begin,
            "boundary": BOUNDARIES[name], "source": "synthetic-explicit-ns-control",
            "independent": name != "G", "lower_ns": value, "upper_ns": value,
            "clock_domain": "fixture-ns"}
    result["anchors"]["A"]["capture_location"] = "client"
    return result


def interval(result, metric):
    return result["metrics"][metric]["interval"]


def test_known_read_delay_is_distinct_from_timestamp_accuracy_and_arithmetic():
    value = assess_external_timing(event_evidence(arrival=10_000_000, read=35_000_000))
    assert interval(value, "arrival_to_read_delay")["lower_ns"] == 25_000_000
    assert interval(value, "total_client_delay")["lower_ns"] == 25_000_000
    assert interval(value, "read_to_observation_delay")["lower_ns"] == 0
    assert value["conditional_arithmetic_assessment"] == "pass"
    assert value["conditional_arrival_distortion_assessment"] == "fail"
    assert not value["actual_giraffe_run_qualified"]
    assert not value["capture_pipeline_implemented"]
    assert "cannot attribute it solely" in value["attribution"]


def test_wrong_giraffe_timestamp_and_wrong_reported_value_are_different_failures():
    wrong_g = assess_external_timing(event_evidence(giraffe=15_000_000))
    assert interval(wrong_g, "read_to_observation_delay")["lower_ns"] == 5_000_000
    assert wrong_g["conditional_arithmetic_assessment"] == "pass"
    wrong_value = assess_external_timing(event_evidence(reported=14_000_000))
    assert interval(wrong_value, "read_to_observation_delay")["lower_ns"] == 0
    assert wrong_value["conditional_arithmetic_assessment"] == "fail"


def tcp_fixture_events(wire_events, segments, reads):
    """Declared reassembled TCP spans, not real packets or Giraffe measurements.

    Segment tuples are byte begin/end and independently specified capture time.
    Read tuples are cumulative consumed byte end and specified read return time.
    Their expected values are the fixture, not timings emitted by the assessor.
    """
    stream = b"".join(wire_events)
    assert b"".join(stream[a:b] for a, b, _ in segments) == stream
    result, begin = [], 0
    for index, wire in enumerate(wire_events):
        end = begin + len(wire)
        arrived = max(at for a, b, at in segments if a < end and b > begin)
        read = next(at for consumed, at in reads if consumed >= end)
        result.append(event_evidence(arrival=arrived, read=read, begin=begin, end=end,
                                    event_id=f"answer-{index}", wire=hashlib.sha256(wire).hexdigest()))
        begin = end
    return result


def test_fragmented_utf8_tcp_and_coalesced_read_map_complete_event_bytes():
    first = 'data: {"id":"x","choices":[{"delta":{"content":"café 🦒"}}]}\r\n\r\n'.encode()
    second = b'data: {"id":"x","choices":[{"delta":{"content":"two"}}]}\n\n'
    length = len(first + second)
    # Boundaries deliberately split both UTF8 and CRLF/SSE framing.
    cuts = [0, first.index("é".encode()) + 1, len(first) - 1, len(first), length]
    segments = [(a, b, t) for (a, b), t in zip(zip(cuts, cuts[1:]),
                                               (3_000_000, 4_000_000, 10_000_000, 15_000_000), strict=True)]
    fixtures = tcp_fixture_events([first, second], segments, [(length, 20_000_000)])
    one, two = map(assess_external_timing, fixtures)
    assert one["mapped_anchors"]["A"]["lower_ns"] == 10_000_000
    assert two["mapped_anchors"]["A"]["lower_ns"] == 15_000_000
    assert one["mapped_anchors"]["G"] == two["mapped_anchors"]["G"]
    assert interval(one, "arrival_to_read_delay")["lower_ns"] == 10_000_000
    assert interval(two, "arrival_to_read_delay")["lower_ns"] == 5_000_000
    # Identical read timestamps do not imply identical event-arrival timestamps.
    assert one["event_id"] != two["event_id"]


@pytest.mark.parametrize("name", list("ARGS"))
def test_unavailable_anchor_fails_only_dependent_metrics_closed(name):
    evidence = event_evidence()
    evidence["anchors"][name] = {"state": "unavailable", "reason": "platform_permission_missing"}
    value = assess_external_timing(evidence)
    assert value["anchor_issues"][name] == ["platform_permission_missing"]
    for metric in value["metrics"].values():
        if name in metric["required_anchors"]:
            assert metric["interval"] is None and not metric["conditional_precision_and_gates_pass"]
    assert not value["actual_giraffe_run_qualified"]


@pytest.mark.parametrize("field,value", [("event_id", "different"), ("request_id", "other"),
    ("stream_offset_begin", 1), ("stream_offset_end", 61), ("wire_sha256", "c" * 64), ("independent", False),
    ("boundary", "server_application_write"), ("capture_location", "server")])
def test_arrival_requires_exact_complete_event_at_client_capture(field, value):
    evidence = event_evidence()
    evidence["anchors"]["A"][field] = value
    result = assess_external_timing(evidence)
    assert result["conditional_arrival_distortion_assessment"] == "unknown" and "A" in result["anchor_issues"]


def test_cross_clock_offsets_are_bounded_and_uncertainty_cannot_be_hidden():
    evidence = event_evidence()
    evidence["anchors"]["A"].update(clock_domain="capture-clock", lower_ns=5_000_000, upper_ns=5_000_000)
    assert assess_external_timing(evidence)["conditional_arrival_distortion_assessment"] == "unknown"
    evidence["clock_mappings"] = [{"from_domain": "capture-clock", "to_domain": "fixture-ns",
        "offset_lower_ns": 4_999_990, "offset_upper_ns": 5_000_010, "validated": True,
        "source": "independent bounded calibration control"}]
    result = assess_external_timing(evidence)
    assert result["conditional_arrival_distortion_assessment"] == "pass"
    assert interval(result, "arrival_to_read_delay")["uncertainty_width_ns"] == 20
    evidence["clock_mappings"][0]["offset_upper_ns"] = 5_200_000
    result = assess_external_timing(evidence)
    assert result["conditional_arrival_distortion_assessment"] == "unknown"
    assert not result["metrics"]["arrival_reference_latency_distortion"]["precision_pass"]


@pytest.mark.parametrize("fault", ["loss", "omission", "sequence", "duplicate", "truncate", "flush", "overhead", "profile", "pairs", "timestamp", "clock"])
def test_reference_and_matched_overhead_gates_cannot_be_skipped(fault):
    evidence = event_evidence()
    gates = evidence["gates"]
    health = gates["reference_health"]
    if fault in {"loss", "omission", "duplicate"}:
        health[fault + "_count"] = 1
    elif fault == "sequence":
        health["sequence_errors"] = 1
    elif fault == "truncate":
        health["capture_truncated"] = True
    elif fault == "flush":
        health["flush_complete"] = False
    elif fault == "overhead":
        gates["matched_overhead"]["gate_pass"] = False
    elif fault == "profile":
        gates["matched_overhead"]["profile_matches"] = False
    elif fault == "pairs":
        gates["matched_overhead"]["actual_pairs"] = 2
    elif fault == "timestamp":
        gates["timestamp_source_validated"] = False
    elif fault == "clock":
        gates["clock_mapping_validated"] = False
    value = assess_external_timing(evidence)
    assert value["conditional_arrival_distortion_assessment"] == "unknown" and value["gate_issues"]
    assert all(not metric["conditional_precision_and_gates_pass"] for metric in value["metrics"].values())


def test_causal_and_threshold_edges_do_not_turn_uncertainty_into_a_pass():
    invalid = assess_external_timing(event_evidence(arrival=12_000_000, read=10_000_000))
    assert "R_strictly_precedes_A" in invalid["gate_issues"]
    evidence = event_evidence(giraffe=11_000_000, reported=12_000_000)
    evidence["anchors"]["G"]["lower_ns"] -= 10
    evidence["anchors"]["G"]["upper_ns"] += 10
    assert assess_external_timing(evidence)["conditional_arithmetic_assessment"] == "inconclusive"


def test_file_ingestion_is_bounded_hashed_and_preserves_existing_evidence(tmp_path):
    source, output = tmp_path / "event.json", tmp_path / "assessment.json"
    source.write_text(json.dumps(event_evidence()))
    result = assess_external_timing_file(source, output)
    assert result["input_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(result["analyzer_source_sha256"]) == 64
    with pytest.raises(ValueError, match="exists"):
        assess_external_timing_file(source, output)
    source.write_bytes(b"x" * (MAX_INPUT_BYTES + 1))
    with pytest.raises(ValueError, match="bound"):
        assess_external_timing_file(source, tmp_path / "new.json")


def test_scope_and_duration_inputs_must_be_explicit():
    evidence = event_evidence()
    missing = copy.deepcopy(evidence)
    missing.pop("scope")
    with pytest.raises(ValueError, match="scope"):
        assess_external_timing(missing)
    evidence["reported_latency_ns"] = float("nan")
    with pytest.raises(ValueError, match="Finite"):
        assess_external_timing(evidence)


@pytest.mark.parametrize("key,bad", [("event", []), ("gates", []), ("anchors", []),
    ("clock_mappings", [False]), ("reference_clock_domain", "  "), ("anchors", {"A": []})])
def test_malformed_structures_raise_closed_validation_errors(key, bad):
    evidence = event_evidence()
    evidence[key] = bad
    with pytest.raises(ValueError):
        assess_external_timing(evidence)


def test_boolean_counts_and_nonfinite_metadata_cannot_pass_reference_health():
    evidence = event_evidence()
    evidence["gates"]["reference_health"]["loss_count"] = False
    assert assess_external_timing(evidence)["conditional_arithmetic_assessment"] == "unknown"
    evidence = event_evidence()
    evidence["anchors"]["A"]["stream_offset_begin"] = False
    assert "A" in assess_external_timing(evidence)["anchor_issues"]
    evidence["metadata"] = {"unknown": float("inf")}
    with pytest.raises(ValueError, match="Finite"):
        assess_external_timing(evidence)


def test_missing_read_keeps_arrival_to_giraffe_delay_and_no_product_qualification():
    evidence = event_evidence(arrival=10_000_000, read=20_000_000)
    evidence["anchors"]["R"] = {"state": "unavailable", "reason": "read capture unsupported"}
    evidence["scope"] = "observed_baseline_run"
    result = assess_external_timing(evidence)
    assert interval(result, "total_client_delay")["lower_ns"] == 10_000_000
    assert interval(result, "arrival_to_read_delay") is None
    assert interval(result, "read_to_observation_delay") is None
    assert result["metrics"]["total_client_delay"]["conditional_precision_and_gates_pass"]
    assert result["conditional_arithmetic_assessment"] == "pass"
    assert not result["actual_giraffe_run_qualified"]
    assert "Conditional" in result["measurement_claim"]


def test_budgets_are_required_and_not_silently_run_qualification():
    with pytest.raises(TypeError):
        _assess(event_evidence())
    result = assess_external_timing(event_evidence())
    assert "caller-supplied" in result["budget_scope"]
    assert "99%" in result["budget_scope"] and not result["actual_giraffe_run_qualified"]
