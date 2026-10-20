"""Hand-specified controls: expectations do not call product or endpoint scorers."""

import copy
import hashlib
import json
import subprocess

import pytest

from validation_suite import verifier
from validation_suite.reporting import get_metric


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def known_case(directory):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"case_id": "known", "experiment_id": "E1", "candidate": "control",
                "repetition": 1, "execution_status": "completed", "deterministic_control": True,
                "scenario": {"mode": "serial", "request_count": 2, "stream": True},
                "reference": {"boundary": "deterministic_vectors", "uncertainty_ms": 0},
                "thresholds": {"timing_error_ms": 1},
                "observability": {"dispatch_events": True, "complete_response_evidence": True}}
    planned, client, receiver, results = [], [], [], []
    for index, content in enumerate(("A B", "C D")):
        identity = f"request-{index}"
        timing = {"latency_ms": 100 + 100 * index, "first_answer_ms": 10 + 10 * index,
                  "dispatch_ns": 1_000_000_000 * (index + 1),
                  "completed_ns": 1_100_000_000 if index == 0 else 2_200_000_000}
        planned.append({"logical_request_id": identity, "attempt_id": identity,
                        "expected": {"content": content, "outcome": "completed", "score": True,
                                     "prompt_tokens": 7, "completion_tokens": 2,
                                     "event_contents": [content[:2], content[2:]], "timing": timing}})
        client.append({"attempt_id": identity, "kind": "request_dispatched", "monotonic_ns": timing["dispatch_ns"]})
        receiver.append({"attempt_id": identity, "kind": "request_received", "metadata": {"response_id": f"response-{index}"}})
        for event_index, chunk in enumerate((content[:2], content[2:])):
            receiver.append({"attempt_id": identity, "kind": "response_event", "metadata": {"event_seq": event_index, "content": chunk}})
        receiver.append({"attempt_id": identity, "kind": "response_finished", "metadata": {"outcome": "completed"}})
        results.append({"logical_request_id": identity, "attempt_id": identity,
                        "response_id": f"response-{index}", "status": "completed", "valid": True,
                        "content": content, "score": True, "prompt_tokens": 7, "completion_tokens": 2,
                        "event_contents": [content[:2], content[2:]], "timing": timing})
    for sequence, row in enumerate(receiver):
        row.update(schema_version=1, sequence=sequence, clock_domain="hand-specified", monotonic_ns=sequence * 100)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    for name, rows in (("planned", planned), ("client-events", client), ("receiver-events", receiver), ("results", results)):
        write_rows(directory / f"{name}.jsonl", rows)
    checkpoint(directory)
    return manifest


def checkpoint(directory):
    data = (directory / "receiver-events.jsonl").read_bytes()
    rows = [json.loads(line) for line in data.splitlines()]
    health = {"complete": True, "flush_complete": True, "overflow_count": 0,
              "truncated_count": 0, "omitted_count": 0, "write_errors": 0,
              "written": len(rows), "attempted": len(rows),
              "last_sequence": rows[-1]["sequence"], "events_sha256": hashlib.sha256(data).hexdigest()}
    (directory / "receiver-health.json").write_text(json.dumps(health))


def rows(directory, name):
    return [json.loads(line) for line in (directory / f"{name}.jsonl").read_text().splitlines()]


def issue_codes(report):
    return {item["code"] for item in get_metric(report, "Limitations, failed gates and next diagnostic")["value"]["issues"]}


def test_known_positive_control_and_exact_arithmetic(tmp_path):
    known_case(tmp_path)
    report = verifier.verify_case(tmp_path)
    assert report["verdicts"]["overall_experiment_result"] == "pass"
    assert report["verdicts"]["capacity_contribution"] == "not_applicable"
    assert get_metric(report, "Requests planned")["value"] == 2
    assert get_metric(report, "Requests confirmed received by endpoint")["value"] == 2
    assert get_metric(report, "Expected and actual scores/verdicts")["value"] == {
        "expected_records": 2, "actual_records": 2, "expected_scores": {"True": 2},
        "actual_scores": {"True": 2}, "mismatches": 0}
    assert not issue_codes(report)


@pytest.mark.parametrize("name,index,operation,expected", [
    ("planned", 0, "omit", "unplanned_attempt"),
    ("planned", 0, "duplicate", "duplicate_plan"),
    ("receiver-events", 0, "omit", "missing_receipt"),
    ("receiver-events", 0, "duplicate", "duplicate_request"),
    ("results", 0, "omit", "missing_result"),
    ("results", 0, "duplicate", "duplicate_result"),
    ("receiver-events", 1, "omit", "event_sequence"),
    ("receiver-events", 1, "duplicate", "event_sequence"),
])
def test_omitted_and_duplicated_controls(tmp_path, name, index, operation, expected):
    known_case(tmp_path)
    values = rows(tmp_path, name)
    if operation == "omit":
        values.pop(index)
    else:
        values.insert(index, copy.deepcopy(values[index]))
    write_rows(tmp_path / f"{name}.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert expected in issue_codes(report)
    assert report["verdicts"]["overall_experiment_result"] == "fail"


@pytest.mark.parametrize("field,value,expected", [
    ("content", "A C", "content_corruption"),
    ("score", False, "score_mismatch"),
    ("completion_tokens", 7, "completion_tokens_mismatch"),
    ("prompt_tokens", 8, "prompt_tokens_mismatch"),
    ("event_contents", ["A "], "client_event_mismatch"),
])
def test_corrupt_content_token_score_controls(tmp_path, field, value, expected):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    values[0][field] = value
    write_rows(tmp_path / "results.jsonl", values)
    assert expected in issue_codes(verifier.verify_case(tmp_path))


def test_corrupt_endpoint_and_its_own_expected_cannot_self_confirm(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "receiver-events")
    values[1]["metadata"]["content"] = "X "
    values[3]["metadata"]["expected"] = {"content": "X B"}
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    report = verifier.verify_case(tmp_path)
    assert "reference_content_corruption" in issue_codes(report)
    assert report["verdicts"]["overall_experiment_result"] == "fail"


def test_false_success_is_separate_from_answer_score(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "planned")
    values[0]["expected"]["outcome"] = "failed"
    write_rows(tmp_path / "planned.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert "false_success" in issue_codes(report)
    assert get_metric(report, "Errors incorrectly classified as successes")["value"] == 1
    assert get_metric(report, "Expected and actual scores/verdicts")["verdict"] == "pass"


def test_invalid_protocol_is_not_success_and_wrong_answer_is_independent(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    values[0]["valid"] = False
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert "outcome_mismatch" in issue_codes(report)
    assert get_metric(report, "Successful, failed, timed-out, cancelled and unresolved requests")["value"] == {"error": 1, "success": 1}


def test_timestamp_shift_and_hidden_reference_record(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    values[0]["timing"]["dispatch_ns"] += 1_000_000
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert {"timestamp_arithmetic", "timing_mismatch"} <= issue_codes(report)
    assert report["verdicts"]["measurement_accuracy"] == "unreliable"
    values = rows(tmp_path, "receiver-events")
    values.pop(2)
    write_rows(tmp_path / "receiver-events.jsonl", values)
    assert verifier.verify_case(tmp_path)["verdicts"]["reference_test_valid"] == "no"


def test_missing_timing_or_application_write_reference_never_qualifies(tmp_path):
    manifest = known_case(tmp_path)
    manifest.pop("deterministic_control")
    manifest["reference"] = {"boundary": "endpoint_application", "uncertainty_ms": 0}
    report = verifier.verify_case(tmp_path, manifest)
    assert report["verdicts"]["measurement_accuracy"] == "unreliable"
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    values = rows(tmp_path, "results")
    for value in values:
        value["timing"] = {}
    write_rows(tmp_path / "results.jsonl", values)
    assert verifier.verify_case(tmp_path, manifest)["verdicts"]["measurement_accuracy"] == "unknown"


@pytest.mark.parametrize("field,value", [("flush_complete", False), ("overflow_count", 1),
                                          ("complete", False), ("omitted_count", 1)])
def test_incomplete_reference_invalidates_performance(tmp_path, field, value):
    known_case(tmp_path)
    path = tmp_path / "receiver-health.json"
    health = json.loads(path.read_text())
    health[field] = value
    path.write_text(json.dumps(health))
    report = verifier.verify_case(tmp_path)
    assert report["verdicts"]["reference_test_valid"] == "no"
    assert report["verdicts"]["measurement_accuracy"] != "qualified"
    assert report["verdicts"]["overall_experiment_result"] != "pass"


def test_response_id_join_overrides_independent_reference_ids_and_result_order(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "receiver-events")
    for index, row in enumerate(values):
        row["attempt_id"] = f"reference-{index // 4}"
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    write_rows(tmp_path / "results.jsonl", list(reversed(rows(tmp_path, "results"))))
    report = verifier.verify_case(tmp_path)
    assert report["verdicts"]["overall_experiment_result"] == "pass"
    assert get_metric(report, "Timing error distributions and allowed tolerances")["value"]["max"] == 0


def test_unexported_client_events_and_auxiliary_population_are_explicit(tmp_path):
    manifest = known_case(tmp_path)
    values = rows(tmp_path, "results")
    values[0]["event_contents"] = None
    values[1]["phase"] = "control"
    write_rows(tmp_path / "results.jsonl", values)
    values = rows(tmp_path, "planned")
    values[1]["phase"] = "control"
    write_rows(tmp_path / "planned.jsonl", values)
    manifest["scenario"]["request_count"] = 1
    report = verifier.verify_case(tmp_path, manifest)
    assert not issue_codes(report)
    assert get_metric(report, "Requests planned")["value"] == 1
    assert get_metric(report, "Requests confirmed received by endpoint")["value"] == 1


def test_known_not_dispatched_is_accounted_drop_not_missing_result(tmp_path):
    manifest = known_case(tmp_path)
    values = rows(tmp_path, "receiver-events")[4:]
    for index, value in enumerate(values):
        value["sequence"] = index
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    write_rows(tmp_path / "results.jsonl", rows(tmp_path, "results")[1:])
    values = rows(tmp_path, "client-events")
    values[0].update(kind="not_dispatched", metadata={"reason": "ceiling"})
    write_rows(tmp_path / "client-events.jsonl", values)
    report = verifier.verify_case(tmp_path, manifest)
    assert "missing_result" not in issue_codes(report)
    assert "missing_receipt" not in issue_codes(report)
    assert report["verdicts"]["requested_load_delivered"] == "fail"


def test_percentiles_and_small_sample_tail_claims_are_hand_specified():
    value = verifier.distribution([10, 40, 20, 30], qualified_count=4)
    assert (value["min"], value["median"], value["p95"], value["p99"], value["max"]) == (10, 25, 40, 40, 40)
    assert value["tail_claims"] == {"p95": "unsupported_small_sample", "p99": "unsupported_small_sample"}
    assert verifier.distribution([])["qualification_coverage"]["percentage"] is None
    assert verifier.percentile(range(1, 101), 95) == 95
    assert verifier.percentile(range(1, 101), 99) == 99


def test_concurrency_is_time_weighted_and_includes_idle_time():
    # 0--2: one; 2--4: two; 4--6: one; 6--10: zero. Area=8 request seconds.
    value = verifier.time_weighted_concurrency([(0, 6_000_000_000), (2_000_000_000, 4_000_000_000)], 0, 10_000_000_000, target=1)
    assert value["mean"] == .8
    assert value["minimum"] == 0 and value["maximum"] == 2
    assert value["within_tolerance_percent"] == 40


def test_bounded_verifier_never_reads_oversized_evidence(tmp_path, monkeypatch):
    known_case(tmp_path)
    monkeypatch.setattr(verifier, "MAX_ATTEMPT_BYTES", 1)
    monkeypatch.setattr(verifier, "_load_jsonl", lambda path: pytest.fail("oversized evidence was read"))
    report = verifier.verify_case(tmp_path)
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    assert get_metric(report, "Requests confirmed received by endpoint")["value"] is None
    for label in ("Warm-up, probe, reference and retry attempt counts",
                  "Valid completions incorrectly classified as failures",
                  "Endpoint completed and failed responses"):
        item = get_metric(report, label)
        assert item["state"] == "not_measured" and item["value"] is None
        assert item["verdict"] == "inconclusive" and "offline input limit" in item["reason"]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["planned_semantics"] = "duration_admission_intent"
    limited_intent = get_metric(verifier.verify_case(tmp_path, manifest), "Requests planned")
    assert limited_intent["state"] == "not_measured" and limited_intent["value"] is None
    assert "offline input limit" in limited_intent["reason"]
    assert (tmp_path / "receiver-events.jsonl").exists()


def test_mixed_unit_timestamp_shift_is_converted_to_milliseconds(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    values[0]["timing"]["dispatch_ns"] += 1_000_000
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert get_metric(report, "Timing error distributions and allowed tolerances")["value"]["max"] == 1


def test_deterministic_vectors_with_missing_observations_cannot_qualify(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    values[0]["timing"] = {}
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert report["verdicts"]["measurement_accuracy"] != "qualified"
    assert report["verdicts"]["overall_experiment_result"] != "pass"


def test_fixed_rate_concurrency_limit_is_not_an_occupancy_target(tmp_path):
    manifest = known_case(tmp_path)
    manifest["scenario"].update(mode="fixed_rate", concurrency=256, rate_rps=1, active_seconds=2)
    manifest["phases"] = {"active_start_ns": 1_000_000_000, "active_end_ns": 3_000_000_000,
                          "clock_domain": "hand-specified"}
    values = rows(tmp_path, "results")
    for value in values:
        value["clock_domain"] = "hand-specified"
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path, manifest)
    assert get_metric(report, "Target concurrency")["state"] == "not_applicable"
    assert get_metric(report, "Active time within concurrency tolerance")["state"] != "measured"
    assert get_metric(report, "Actual time-weighted mean, minimum and maximum concurrency")["value"]["mean"] == .15


def test_changed_baseline_invalidates_an_otherwise_passing_comparison(tmp_path):
    manifest = known_case(tmp_path)
    manifest["baseline_unchanged"] = False
    report = verifier.verify_case(tmp_path, manifest)
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    assert "Baseline source/dependency identity changed" in str(get_metric(report, "Limitations, failed gates and next diagnostic")["value"])


def test_fault_specification_alone_does_not_prove_injection(tmp_path):
    manifest = known_case(tmp_path)
    manifest["scenario"].update(fault="user_cancel", fault_expectations={"require_injection_evidence": True})
    report = verifier.verify_case(tmp_path, manifest)
    assert report["verdicts"]["expected_fault_behavior"] == "inconclusive"
    assert get_metric(report, "Observed detection, classification and recovery")["value"]["injection_confirmed"] is False


def test_shutdown_deadline_and_safety_abort_are_independent_gates(tmp_path):
    manifest = known_case(tmp_path)
    manifest["scenario"].update(fault="user_cancel", fault_expectations={"cleanup_deadline_seconds": 10})
    values = rows(tmp_path, "client-events")
    values += [{"kind": "external_fault_injected", "fault": "user_cancel", "clock_domain": "driver", "monotonic_ns": 0},
               {"kind": "candidate_process_exited", "clock_domain": "driver", "monotonic_ns": 11_000_000_000},
               {"kind": "resource_safety_stop", "reason": "RSS limit"}]
    write_rows(tmp_path / "client-events.jsonl", values)
    report = verifier.verify_case(tmp_path, manifest)
    assert report["execution_status"] == "aborted"
    assert report["verdicts"]["expected_fault_behavior"] == "fail"
    assert get_metric(report, "Shutdown time and deadline")["verdict"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "fail"


def test_unexported_http_error_ids_are_unknown_not_proven_losses(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    for value in values:
        value["response_id"] = None
    write_rows(tmp_path / "results.jsonl", values)
    values = rows(tmp_path, "receiver-events")
    for index, value in enumerate(values):
        value["attempt_id"] = f"reference-{index // 4}"
        if value["kind"] == "request_received":
            value["metadata"]["correlation_confidence"] = "response_id_only"
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    report = verifier.verify_case(tmp_path)
    assert report["verdicts"]["response_accounting_correctness"] == "inconclusive"
    assert {"missing_result", "unplanned_attempt", "missing_receipt"}.isdisjoint(issue_codes(report))
    assert get_metric(report, "Requests confirmed received by endpoint")["state"] == "unknown"


def test_frozen_fixture_catalog_is_independent_expected_answer(tmp_path):
    manifest = known_case(tmp_path)
    manifest["scenario"]["expected_by_fixture"] = {"fixed": {"content": "WRONG"}}
    values = rows(tmp_path, "planned")
    values[0]["expected"].pop("content")
    write_rows(tmp_path / "planned.jsonl", values)
    values = rows(tmp_path, "results")
    values[0]["fixture_id"] = "fixed"
    write_rows(tmp_path / "results.jsonl", values)
    assert "content_corruption" in issue_codes(verifier.verify_case(tmp_path, manifest))


def test_receiver_spacing_detects_two_requests_at_once_despite_window_allowance():
    thresholds = {"receiver_spacing_relative_tolerance": .5, "receiver_spacing_coverage_fraction": .95}
    verdict, values = verifier.receiver_spacing([0, 0], 1, thresholds)
    assert verdict == "fail"
    assert values["gap_distribution"]["max"] == 0
    assert values["violations"] == 1
    assert values["observed_fraction"] == 0
    verdict, values = verifier.receiver_spacing([0, 1_000_000_000], 1, thresholds)
    assert verdict == "pass"
    assert values["required_fraction"] == 1


def test_receiver_spacing_large_sample_coverage_and_small_sample_fail_closed():
    thresholds = {"receiver_spacing_relative_tolerance": .5, "receiver_spacing_coverage_fraction": .95}
    # 19 normal gaps and1 burst gap: exactly95% passes the frozen20-gap rule.
    times = [index * 1_000_000_000 for index in range(20)] + [19_000_000_000]
    assert verifier.receiver_spacing(times, 1, thresholds)[0] == "pass"
    assert verifier.receiver_spacing(times[1:], 1, thresholds)[0] == "fail"
    assert verifier.receiver_spacing([0], 1, thresholds)[0] == "inconclusive"


def test_adapter_result_enumeration_is_not_exact_preplanned_identity(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    for value in values:
        value["planned_identity_provenance"] = "adapter_enumeration"
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert "planned_slot_correlation_unknown" in issue_codes(report)
    assert report["verdicts"]["response_accounting_correctness"] == "inconclusive"
    assert report["verdicts"]["requested_load_delivered"] == "inconclusive"


def test_unknown_slot_correlation_does_not_hide_a_proven_result_count_deficit(tmp_path):
    known_case(tmp_path)
    values = rows(tmp_path, "receiver-events")
    for index, value in enumerate(values):
        value["attempt_id"] = f"reference-{index // 4}"
        if value["kind"] == "request_received":
            value["metadata"]["correlation_confidence"] = "response_id_only"
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    values = rows(tmp_path, "results")[1:]
    values[0]["response_id"] = None
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path)
    assert "result_count_deficit" in issue_codes(report)
    assert report["verdicts"]["response_accounting_correctness"] == "fail"


@pytest.mark.parametrize("swap", [False, True])
def test_unique_request_pack_fixture_requires_independent_frozen_marker(tmp_path, swap):
    known_case(tmp_path)
    values = rows(tmp_path, "planned")
    for index, value in enumerate(values):
        value["expected"]["request_marker"] = f"Validation-ID: {index:06d}"
    write_rows(tmp_path / "planned.jsonl", values)
    values = rows(tmp_path, "results")
    for value in values:
        value["planned_identity_provenance"] = "unique_request_pack_fixture"
    write_rows(tmp_path / "results.jsonl", values)
    values = rows(tmp_path, "receiver-events")
    for index in (0, 4):
        marker_index = 1-index//4 if swap else index//4
        values[index]["metadata"]["request"] = {"messages": [{"role": "user", "content": f"Synthetic request\nValidation-ID: {marker_index:06d}"}]}
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    report = verifier.verify_case(tmp_path)
    assert ("request_marker_mismatch" in issue_codes(report)) is swap
    assert report["verdicts"]["response_accounting_correctness"] == ("fail" if swap else "pass")


def test_burst_spread_gate_names_its_client_boundary(tmp_path):
    manifest = known_case(tmp_path)
    manifest["scenario"]["mode"] = "burst"
    manifest["thresholds"]["launch_spread_ms"] = 100
    report = verifier.verify_case(tmp_path, manifest)
    threshold = get_metric(report, "Burst dispatch and receiver arrival spreads")["threshold"]
    assert threshold["client_dispatch_max_ms"] == 100
    assert threshold["receiver_application_receipt_spread"] == "observed; no numeric gate declared"


def test_e7_growth_above_frozen_bound_cannot_pass_otherwise_passing_control(tmp_path):
    manifest = known_case(tmp_path)
    assert verifier.verify_case(tmp_path, manifest)["verdicts"]["overall_experiment_result"] == "pass"
    manifest["experiment_id"] = "E7"
    manifest["phases"] = {"active_start_ns": 0, "active_end_ns": 3_600_000_000_000,
                          "clock_domain": "hand-specified"}
    manifest["thresholds"]["memory_growth_bytes_per_hour"] = 64 * 1024**2
    manifest["resource_summary"] = {"ram": {
        "candidate": {"growth": 65 * 1024**2, "sample_count": 3600},
        "reference": {"growth": 0, "sample_count": 3600},
        "driver": {"growth": 0, "sample_count": 3600}}}
    report = verifier.verify_case(tmp_path, manifest)
    assert get_metric(report, "RAM per process and aggregate RAM, peak and growth")["verdict"] == "fail"
    assert get_metric(report, "Memory, connections and backlog trends over time")["verdict"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "fail"


def test_short_resource_observations_do_not_pass_or_extrapolate_hour_gate(tmp_path):
    manifest = known_case(tmp_path)
    manifest["resource_summary"] = {"cpu": {"candidate": {"mean_sample": 10}},
                                    "ram": {"candidate": {"growth": 65 * 1024**2, "sample_count": 2}}}
    manifest["thresholds"]["memory_growth_bytes_per_hour"] = 64 * 1024**2
    report = verifier.verify_case(tmp_path, manifest)
    assert get_metric(report, "CPU by process/thread and aggregate CPU")["verdict"] == "inconclusive"
    assert get_metric(report, "RAM per process and aggregate RAM, peak and growth")["verdict"] == "inconclusive"


@pytest.mark.parametrize("spread_ms", [118.77, 221.91, 287.19])
def test_known_burst_launch_failure_propagates_to_delivery_and_overall(tmp_path, spread_ms):
    manifest = known_case(tmp_path)
    manifest["scenario"]["mode"] = "burst"
    manifest["thresholds"]["launch_spread_ms"] = 100
    values = rows(tmp_path, "client-events")
    values[1]["monotonic_ns"] = values[0]["monotonic_ns"] + round(spread_ms * 1e6)
    write_rows(tmp_path / "client-events.jsonl", values)
    report = verifier.verify_case(tmp_path, manifest)
    assert get_metric(report, "Burst dispatch and receiver arrival spreads")["value"]["dispatch_ms"] == spread_ms
    assert get_metric(report, "Burst dispatch and receiver arrival spreads")["verdict"] == "fail"
    assert report["verdicts"]["requested_load_delivered"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "fail"


@pytest.mark.parametrize("retained_dispatches", [0, 1])
def test_missing_burst_dispatch_observations_do_not_become_failure_or_pass(tmp_path, retained_dispatches):
    manifest = known_case(tmp_path)
    manifest["scenario"]["mode"] = "burst"
    manifest["thresholds"]["launch_spread_ms"] = 100
    write_rows(tmp_path / "client-events.jsonl", rows(tmp_path, "client-events")[:retained_dispatches])
    report = verifier.verify_case(tmp_path, manifest)
    assert get_metric(report, "Burst dispatch and receiver arrival spreads")["verdict"] == "inconclusive"
    assert report["verdicts"]["requested_load_delivered"] == "inconclusive"
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"


def test_known_fixed_rate_lateness_failure_propagates_without_absolute_clock_alignment(tmp_path):
    manifest = known_case(tmp_path)
    manifest["scenario"].update(mode="fixed_rate", rate_rps=1, active_seconds=2)
    manifest["phases"] = {"active_start_ns": 1_000_000_000, "active_end_ns": 3_000_000_000,
                          "clock_domain": "client-relative"}
    manifest["receiver_active_window"] = {"start_ns": 0, "end_ns": 2_000_000_000,
                                          "clock_domain": "hand-specified",
                                          "qualification": "diagnostic_first_receipt_anchored"}
    manifest["thresholds"].update(dispatch_lateness_p95_ms=20, dispatch_lateness_max_ms=100,
                                  receiver_spacing_relative_tolerance=.5, receiver_spacing_coverage_fraction=.95,
                                  rate_window_relative_error=.1, rate_window_count_allowance=1,
                                  active_duration_error_seconds=.25)
    values = rows(tmp_path, "client-events")
    for value in values:
        value["clock_domain"] = "client-relative"
    write_rows(tmp_path / "client-events.jsonl", values)
    values = rows(tmp_path, "results")
    for value in values:
        value["clock_domain"] = "client-relative"
        value["timing"]["dispatch_lag_ms"] = 101
    write_rows(tmp_path / "results.jsonl", values)
    values = rows(tmp_path, "receiver-events")
    for index, value in enumerate(values):
        value["monotonic_ns"] = (index // 4) * 1_000_000_000 + index % 4
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    report = verifier.verify_case(tmp_path, manifest)
    assert get_metric(report, "Scheduled-start lateness distribution")["verdict"] == "fail"
    assert get_metric(report, "Catch-up bursts and rate overshoots")["verdict"] == "pass"
    assert get_metric(report, "Per-second traffic history and rate deviations")["state"] == "unreliable"
    assert report["verdicts"]["requested_load_delivered"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "fail"




@pytest.mark.parametrize("boundary", ["unconfirmed_admission", "known_not_dispatched", "dispatch_without_response"])
def test_no_phantom_response_events_when_no_response_was_observed(tmp_path, boundary):
    manifest = known_case(tmp_path)
    receiver = rows(tmp_path, "receiver-events")[4:]
    for sequence, row in enumerate(receiver):
        row["sequence"] = sequence
    write_rows(tmp_path / "receiver-events.jsonl", receiver)
    checkpoint(tmp_path)
    write_rows(tmp_path / "results.jsonl", rows(tmp_path, "results")[1:])
    client = rows(tmp_path, "client-events")
    if boundary == "unconfirmed_admission":
        client = client[1:]
    elif boundary == "known_not_dispatched":
        client[0].update(kind="not_dispatched", metadata={"reason": "late_arrival"})
    write_rows(tmp_path / "client-events.jsonl", client)
    report = verifier.verify_case(tmp_path, manifest)
    codes = issue_codes(report)
    assert "reference_event_mismatch" not in codes
    assert "reference_content_corruption" not in codes
    assert "content_corruption" not in codes
    assert "outcome_mismatch" not in codes
    assert "false_success" not in codes
    assert "score_mismatch" not in codes
    if boundary == "unconfirmed_admission":
        assert "not_dispatched_unconfirmed" in codes
    elif boundary == "dispatch_without_response":
        assert {"missing_receipt", "missing_result"} <= codes
    assert report["verdicts"]["requested_load_delivered"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "fail"


def test_received_request_with_missing_response_events_still_fails(tmp_path):
    known_case(tmp_path)
    receiver = rows(tmp_path, "receiver-events")
    receiver = [row for index, row in enumerate(receiver) if index not in {1, 2}]
    for sequence, row in enumerate(receiver):
        row["sequence"] = sequence
    write_rows(tmp_path / "receiver-events.jsonl", receiver)
    checkpoint(tmp_path)
    assert "reference_event_mismatch" in issue_codes(verifier.verify_case(tmp_path))


def test_recorded_orphan_response_corruption_remains_visible(tmp_path):
    known_case(tmp_path)
    receiver = rows(tmp_path, "receiver-events")[1:]
    receiver[0]["metadata"]["content"] = "BROKEN"
    for sequence, row in enumerate(receiver):
        row["sequence"] = sequence
    write_rows(tmp_path / "receiver-events.jsonl", receiver)
    checkpoint(tmp_path)
    codes = issue_codes(verifier.verify_case(tmp_path))
    assert {"reference_event_mismatch", "reference_content_corruption", "missing_receipt"} <= codes


def test_positive_event_control_remains_passing(tmp_path):
    known_case(tmp_path)
    assert verifier.verify_case(tmp_path)["verdicts"]["overall_experiment_result"] == "pass"




def _classification_control(tmp_path, *, live, keep_server_response):
    manifest = known_case(tmp_path)
    if live:
        manifest.pop("deterministic_control")
        manifest["reference"] = {"boundary": "endpoint_application", "uncertainty_ms": None}
    values = rows(tmp_path, "results")
    values[0].update(status="timeout", valid=False)
    write_rows(tmp_path / "results.jsonl", values)
    if not keep_server_response:
        receiver = rows(tmp_path, "receiver-events")[4:]
        for sequence, row in enumerate(receiver):
            row["sequence"] = sequence
        write_rows(tmp_path / "receiver-events.jsonl", receiver)
        checkpoint(tmp_path)
    return manifest


def _independent_complete_reference(identity="request-0", **overrides):
    reference = {"schema_version": 1, "study_id": "attribution-control", "case_id": "known",
                 "candidate": "control", "repetition": 1, "logical_request_id": identity,
                 "attempt_id": identity, "worker_id": "independent-reference", "sequence": 0,
                 "kind": "client_protocol_complete_reference", "monotonic_ns": 1_100_000_000,
                 "clock_domain": "independent-control-clock", "source": "independent_control_reference",
                 "metadata": {"protocol_complete": True, "content": "A B"}}
    reference.update(overrides)
    return reference


def test_live_transport_timeout_without_independent_completion_is_not_proven_misclassification(tmp_path):
    manifest = _classification_control(tmp_path, live=True, keep_server_response=False)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    item = get_metric(report, "Valid completions incorrectly classified as failures")
    assert item["state"] == "unknown" and item["verdict"] == "inconclusive"
    assert item["value"] == {"fixture_outcome_mismatches": 1,
                             "confirmed_valid_completion_misclassifications": None,
                             "unattributed_fixture_outcome_mismatches": 1}
    assert "outcome_mismatch" in issue_codes(report)
    assert get_metric(report, "Successful, failed, timed-out, cancelled and unresolved requests")["value"] == {"timeout": 1, "success": 1}
    assert report["verdicts"]["requested_load_delivered"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "fail"


def test_server_emission_and_deterministic_flag_alone_do_not_establish_client_completion(tmp_path):
    manifest = _classification_control(tmp_path, live=False, keep_server_response=True)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    item = get_metric(report, "Valid completions incorrectly classified as failures")
    assert item["state"] == "unknown"
    assert item["value"]["fixture_outcome_mismatches"] == 1
    assert item["value"]["confirmed_valid_completion_misclassifications"] is None


def test_deterministic_independent_complete_reference_proves_misclassification(tmp_path):
    manifest = _classification_control(tmp_path, live=False, keep_server_response=True)
    client = rows(tmp_path, "client-events") + [_independent_complete_reference()]
    write_rows(tmp_path / "client-events.jsonl", client)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    item = get_metric(report, "Valid completions incorrectly classified as failures")
    assert item["state"] == "measured" and item["verdict"] == "fail"
    assert item["value"] == {"fixture_outcome_mismatches": 1,
                             "confirmed_valid_completion_misclassifications": 1,
                             "unattributed_fixture_outcome_mismatches": 0}
    assert "outcome_mismatch" in issue_codes(report)
    assert report["verdicts"]["overall_experiment_result"] == "fail"


@pytest.mark.parametrize("invalid", ["wrong_identity", "wrong_content", "incomplete_protocol", "candidate_source", "live_flag"])
def test_unqualified_completion_reference_does_not_create_a_misclassification_claim(tmp_path, invalid):
    manifest = _classification_control(tmp_path, live=invalid == "live_flag", keep_server_response=True)
    reference = _independent_complete_reference()
    if invalid == "wrong_identity":
        reference["attempt_id"] = "unmatched"
    elif invalid == "wrong_content":
        reference["metadata"]["content"] = "CORRUPTED"
    elif invalid == "incomplete_protocol":
        reference["metadata"]["protocol_complete"] = False
    elif invalid == "candidate_source":
        reference["source"] = "candidate_self_report"
    write_rows(tmp_path / "client-events.jsonl", rows(tmp_path, "client-events") + [reference])
    item = get_metric(verifier.verify_case(tmp_path, manifest, write=False), "Valid completions incorrectly classified as failures")
    assert item["state"] == "unknown" and item["verdict"] == "inconclusive"
    assert item["value"]["fixture_outcome_mismatches"] == 1
    assert item["value"]["confirmed_valid_completion_misclassifications"] is None




def _duration_intent_with_recovery_controls(tmp_path):
    manifest = known_case(tmp_path)
    manifest.update(planned_semantics="duration_admission_intent")
    manifest["scenario"].update(mode="sustained_concurrency", request_count=None, concurrency=1, active_seconds=2)
    planned = rows(tmp_path, "planned")
    for row in planned:
        row.update(event_kind="request_planned", phase="control")
    intent = {"event_kind": "sustained_admission_intent", "phase": "measured",
              "logical_request_id": None, "attempt_id": None, "target_concurrency": 1, "active_seconds": 2}
    write_rows(tmp_path / "planned.jsonl", [intent] + planned)
    results = rows(tmp_path, "results")
    for index, row in enumerate(results):
        row.update(phase="control", raw={"id": f"retained-control-{index}",
                                        "scenario": "recovery_baseline" if index == 0 else "recovery"})
    write_rows(tmp_path / "results.jsonl", results)
    client = rows(tmp_path, "client-events")
    for row in client:
        row["phase"] = "control"
    write_rows(tmp_path / "client-events.jsonl", client)
    return manifest


def test_duration_intent_is_not_three_requests_or_three_auxiliary_attempts(tmp_path):
    manifest = _duration_intent_with_recovery_controls(tmp_path)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    planned = get_metric(report, "Requests planned")
    counts = get_metric(report, "Warm-up, probe, reference and retry attempt counts")
    assert planned["state"] == "unknown"
    assert planned["value"] == {"admission_intents": 1}
    assert counts["value"]["planned_request_rows_by_phase"] == {"control": 2}
    assert counts["value"]["observed_retained_result_attempts_by_phase"] == {"control": 2}
    assert counts["value"]["observed_retained_result_raw_scenarios"] == {"recovery_baseline": 1, "recovery": 1}
    assert counts["value"]["observed_counts_are_lower_bounds"] is True
    assert "retry" not in counts["value"]["observed_retained_result_attempts_by_phase"]


def test_only_explicit_sustained_intent_rows_are_counted_as_intents(tmp_path):
    manifest = _duration_intent_with_recovery_controls(tmp_path)
    planned = rows(tmp_path, "planned")
    planned.insert(0, dict(planned[0]))
    write_rows(tmp_path / "planned.jsonl", planned)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    assert get_metric(report, "Requests planned")["value"] == {"admission_intents": 2}
    counts = get_metric(report, "Warm-up, probe, reference and retry attempt counts")["value"]
    assert counts["planned_request_rows_by_phase"] == {"control": 2}
    assert counts["observed_retained_result_attempts_by_phase"] == {"control": 2}


def test_explicit_phase_wins_over_population_without_semantic_probe_inference(tmp_path):
    manifest = _duration_intent_with_recovery_controls(tmp_path)
    planned = rows(tmp_path, "planned")
    for row in planned[1:]:
        row["population"] = "unknown"
    write_rows(tmp_path / "planned.jsonl", planned)
    results = rows(tmp_path, "results")
    for row in results:
        row["population"] = "unknown"
    write_rows(tmp_path / "results.jsonl", results)
    value = get_metric(verifier.verify_case(tmp_path, manifest, write=False), "Warm-up, probe, reference and retry attempt counts")["value"]
    assert value["planned_request_rows_by_phase"] == {"control": 2}
    assert value["observed_retained_result_attempts_by_phase"] == {"control": 2}
    assert "probe" not in value["observed_retained_result_attempts_by_phase"]


def test_retained_attempt_ids_are_not_double_counted_by_duplicate_result_rows(tmp_path):
    manifest = _duration_intent_with_recovery_controls(tmp_path)
    results = rows(tmp_path, "results")
    results.append(dict(results[0]))
    write_rows(tmp_path / "results.jsonl", results)
    item = get_metric(verifier.verify_case(tmp_path, manifest, write=False), "Warm-up, probe, reference and retry attempt counts")
    assert item["value"]["observed_retained_result_attempts_by_phase"] == {"control": 2}
    assert item["value"]["duplicate_retained_result_rows"] == 1
    assert item["state"] == "unknown"


def test_missing_retained_result_stays_lower_bound_and_receipt_phase_unknown(tmp_path):
    manifest = _duration_intent_with_recovery_controls(tmp_path)
    write_rows(tmp_path / "results.jsonl", rows(tmp_path, "results")[1:])
    item = get_metric(verifier.verify_case(tmp_path, manifest, write=False), "Warm-up, probe, reference and retry attempt counts")
    assert item["value"]["planned_request_rows_by_phase"] == {"control": 2}
    assert item["value"]["observed_retained_result_attempts_by_phase"] == {"control": 1}
    assert item["value"]["receipt_rows_without_retained_result_join"] == 1
    assert item["value"]["receipt_only_attempt_phases"] is None
    assert item["value"]["observed_counts_are_lower_bounds"] is True
    assert item["state"] == "unknown"


def test_unlabelled_retained_attempt_does_not_invent_a_retry_or_probe_count(tmp_path):
    manifest = _duration_intent_with_recovery_controls(tmp_path)
    results = rows(tmp_path, "results")
    results[0].pop("phase")
    results[0]["raw"].pop("scenario")
    write_rows(tmp_path / "results.jsonl", results)
    item = get_metric(verifier.verify_case(tmp_path, manifest, write=False), "Warm-up, probe, reference and retry attempt counts")
    assert item["value"]["observed_retained_result_attempts_by_phase"] == {"unknown": 1, "control": 1}
    assert "probe" not in item["value"]["observed_retained_result_attempts_by_phase"]
    assert "retry" not in item["value"]["observed_retained_result_attempts_by_phase"]
    assert item["state"] == "unknown"


def _measured_with_control(tmp_path):
    manifest = known_case(tmp_path)
    manifest.pop("deterministic_control")
    manifest["reference"] = {"boundary": "endpoint_application", "uncertainty_ms": None}
    for name in ("planned", "client-events", "receiver-events", "results"):
        values = rows(tmp_path, name)
        for row in values:
            row["phase"] = "measured"
        originals = values[:4] if name == "receiver-events" else values[:1]
        controls = copy.deepcopy(originals)
        for row in controls:
            row.update(attempt_id="control-0", phase="control")
            if "logical_request_id" in row:
                row["logical_request_id"] = "control-0"
            if "response_id" in row:
                row["response_id"] = "control-response"
            if "response_id" in row.get("metadata", {}):
                row["metadata"]["response_id"] = "control-response"
        values += controls
        if name == "receiver-events":
            for sequence, row in enumerate(values):
                row["sequence"] = sequence
        write_rows(tmp_path / f"{name}.jsonl", values)
    checkpoint(tmp_path)
    return manifest


def test_coverage_and_processed_counts_exclude_one_control_from_two_measured(tmp_path):
    manifest = _measured_with_control(tmp_path)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    item = get_metric(report, "Qualified measurement count and percentage")
    assert item["value"] == {"qualified_count": 0, "observed_count": 2,
                             "expected_count": 2, "percentage": 0}
    assert report["verdicts"]["measurement_accuracy"] == "unreliable"
    assert get_metric(report, "Responses fully processed by Giraffe")["value"] == 2
    assert get_metric(report, "Reported and reference completion latency")["value"]["client_latency_ms"]["sample_count"] == 2
    terminal = get_metric(report, "Endpoint completed and failed responses")
    assert terminal["value"] == {"all_phase_outcomes": {"success": 3},
                                  "by_explicit_endpoint_phase": {"measured": {"success": 2}, "control": {"success": 1}}}
    assert terminal["population"].startswith("all endpoint")
    persisted = get_metric(report, "Records processed, evaluated and persisted")
    assert persisted["value"] == {"all_populations_persisted_result_rows": 3, "unique_measured_results": 2}
    assert "all persisted" in persisted["population"]


def test_coverage_control_latency_does_not_fill_missing_measured_latency(tmp_path):
    manifest = _measured_with_control(tmp_path)
    values = rows(tmp_path, "results")
    for row in values:
        if row["phase"] == "measured":
            row["timing"].pop("latency_ms")
    write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    coverage = get_metric(report, "Qualified measurement count and percentage")["value"]
    assert coverage["observed_count"] == 0 and coverage["expected_count"] == 2
    assert coverage["qualified_count"] == 0 and coverage["percentage"] == 0
    assert report["verdicts"]["measurement_accuracy"] == "unreliable"


def test_duration_intent_coverage_has_no_fabricated_expected_request_count(tmp_path):
    manifest = _duration_intent_with_recovery_controls(tmp_path)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    coverage = get_metric(report, "Qualified measurement count and percentage")["value"]
    assert coverage == {"qualified_count": 0, "observed_count": 0, "expected_count": None, "percentage": None}


def test_endpoint_phase_absence_stays_unknown_in_all_phase_breakdown(tmp_path):
    manifest = _measured_with_control(tmp_path)
    values = rows(tmp_path, "receiver-events")
    for row in values:
        row.pop("phase")
    write_rows(tmp_path / "receiver-events.jsonl", values)
    checkpoint(tmp_path)
    item = get_metric(verifier.verify_case(tmp_path, manifest, write=False), "Endpoint completed and failed responses")
    assert item["value"]["by_explicit_endpoint_phase"] == {"unknown": {"success": 3}}


@pytest.mark.parametrize("control", ["normal", "duplicate_result", "corrupt_event", "truncated_json", "missing_file"])
def test_sqlite_and_small_reference_paths_have_identical_semantics(tmp_path, control):
    known_case(tmp_path)
    if control == "duplicate_result":
        values = rows(tmp_path, "results")
        write_rows(tmp_path / "results.jsonl", values + values[:1])
    elif control == "corrupt_event":
        values = rows(tmp_path, "receiver-events")
        values[1]["metadata"]["content"] = "CORRUPTED"
        write_rows(tmp_path / "receiver-events.jsonl", values)
        checkpoint(tmp_path)
    elif control == "truncated_json":
        with (tmp_path / "receiver-events.jsonl").open("ab") as stream:
            stream.write(b'{"truncated":')
    elif control == "missing_file":
        (tmp_path / "results.jsonl").unlink()
    assert verifier.verify_case(tmp_path, write=False) == verifier.verify_case(tmp_path, write=False, disk_backed=False)


def test_total_raw_bytes_and_old_row_limit_do_not_reject_bounded_working_sets(tmp_path, monkeypatch):
    known_case(tmp_path)
    monkeypatch.setattr(verifier, "MAX_ARTIFACT_ROWS", 1)
    statistics = {}
    report = verifier.verify_case(tmp_path, write=False, backend_statistics=statistics)
    assert report["verdicts"]["overall_experiment_result"] == "pass"
    assert statistics["complete"] is True and statistics["raw_rows"]["receiver-events"] == 8
    assert statistics["sqlite_cache_bytes"] == 8 * 1024**2


def test_compact_working_set_bound_fails_closed_without_partial_counts(tmp_path, monkeypatch):
    known_case(tmp_path)
    monkeypatch.setattr(verifier, "MAX_COMPACT_BYTES", 1)
    report = verifier.verify_case(tmp_path, write=False)
    assert get_metric(report, "Requests planned")["value"] is None
    for label in ("Responses processed per second", "Stream protocol events processed per second", "Response bytes processed per second"):
        assert get_metric(report, label)["value"] is None
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"


def _refused_connection_control(tmp_path, *, injection=True, error="request failed (ConnectError)"):
    manifest = known_case(tmp_path)
    manifest["scenario"].update(fault="connection_refused", healthy_capacity=False)
    planned = rows(tmp_path, "planned")
    for row in planned:
        row["expected"] = {"outcome": "error"}
    write_rows(tmp_path / "planned.jsonl", planned)
    values = rows(tmp_path, "results")
    for row in values:
        row.update(status="failed", valid=False, content="", raw={"error": error},
                   planned_identity_provenance="independent_control_id")
    write_rows(tmp_path / "results.jsonl", values)
    write_rows(tmp_path / "receiver-events.jsonl", [])
    # A stopped endpoint can retain a valid completed checkpoint with probe rows.
    write_rows(tmp_path / "receiver-events.jsonl", [{"kind": "probe_received", "sequence": 0}])
    checkpoint(tmp_path)
    if injection:
        write_rows(tmp_path / "client-events.jsonl", rows(tmp_path, "client-events") + [
            {"kind": "external_fault_injected", "fault": "connection_refused"}])
    return manifest


def test_confirmed_connect_error_has_expected_absent_receipt_and_preserved_delivery_failure(tmp_path):
    manifest = _refused_connection_control(tmp_path)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    assert "missing_receipt" not in issue_codes(report)
    assert report["verdicts"]["expected_fault_behavior"] == "pass"
    assert report["verdicts"]["response_accounting_correctness"] == "pass"
    assert report["verdicts"]["requested_load_delivered"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    assert report["verdicts"]["capacity_contribution"] == "not_applicable"
    assert get_metric(report, "Dispatched requests with receipt unconfirmed")["value"] == {"unexpected": 0, "expected_connection_refused": 2}


@pytest.mark.parametrize("unsupported", ["no_injection", "http_error", "healthy"])
def test_refusal_waiver_cannot_hide_a_real_or_unsupported_missing_receipt(tmp_path, unsupported):
    manifest = _refused_connection_control(tmp_path, injection=unsupported != "no_injection",
                                            error="request failed (HTTPStatusError)" if unsupported == "http_error" else "request failed (ConnectError)")
    if unsupported == "healthy":
        manifest["scenario"].pop("fault")
    report = verifier.verify_case(tmp_path, manifest, write=False)
    assert "missing_receipt" in issue_codes(report)
    assert report["verdicts"]["response_accounting_correctness"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "fail"


@pytest.mark.parametrize("fault", ["global_budget", "user_cancel"])
def test_intentional_shortfall_keeps_delivery_failure_without_claiming_product_fault(tmp_path, fault):
    manifest = known_case(tmp_path)
    manifest["scenario"].update(fault=fault, healthy_capacity=False, request_count=3)
    planned = rows(tmp_path, "planned")
    for row in planned:
        row["expected"] = {}
    planned.append({"attempt_id": "not-offered", "expected": {}})
    write_rows(tmp_path / "planned.jsonl", planned)
    events = rows(tmp_path, "client-events")
    if fault == "global_budget":
        events.append({"kind": "fault_configured", "fault": fault})
        manifest["product_abort_reason"] = "Global budget reached"
    else:
        events.extend([{"kind": "external_fault_injected", "fault": fault, "clock_domain": "driver", "monotonic_ns": 0},
                       {"kind": "candidate_process_exited", "clock_domain": "driver", "monotonic_ns": 1_000_000}])
    write_rows(tmp_path / "client-events.jsonl", events)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    assert report["verdicts"]["expected_fault_behavior"] == "pass"
    assert report["verdicts"]["requested_load_delivered"] == "fail"
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    # A genuine unexpected corruption remains a product failure during the same injected fault.
    planned[0]["expected"] = {"content": "INDEPENDENT GOLDEN"}
    write_rows(tmp_path / "planned.jsonl", planned)
    broken = verifier.verify_case(tmp_path, manifest, write=False)
    assert "content_corruption" in issue_codes(broken)
    assert broken["verdicts"]["overall_experiment_result"] == "fail"


def test_large_multirecord_evidence_keeps_bounded_peak_rss_and_exact_metrics(tmp_path):
    import subprocess
    import sys

    known_case(tmp_path)
    expected = verifier.verify_case(tmp_path, write=False)
    event_path = tmp_path / "receiver-events.jsonl"
    digest = hashlib.sha256(event_path.read_bytes())
    padding = "x" * 4096
    count = 8
    with event_path.open("ab") as stream:
        for index in range(35_000):
            row = {"kind": "response_write_end", "sequence": count, "attempt_id": f"write-only-{index % 1000}",
                   "metadata": {"wire_b64": padding}}
            line = (json.dumps(row) + "\n").encode()
            stream.write(line)
            digest.update(line)
            count += 1
    assert event_path.stat().st_size > 128 * 1024**2
    health = json.loads((tmp_path / "receiver-health.json").read_text())
    health.update(written=count, attempted=count, last_sequence=count-1, events_sha256=digest.hexdigest())
    (tmp_path / "receiver-health.json").write_text(json.dumps(health))
    script = """import hashlib,json,resource,sys
from validation_suite.verifier import verify_case
stats={}
report=verify_case(sys.argv[1],write=False,backend_statistics=stats)
peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
print(json.dumps({'peak':peak,'stats':stats,'report_sha':hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()}))
"""
    actual = json.loads(subprocess.check_output([sys.executable, "-c", script, str(tmp_path)], text=True))
    assert actual["report_sha"] == hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    assert actual["stats"]["complete"] is True
    assert actual["stats"]["raw_rows"]["receiver-events"] == 35_008
    assert actual["peak"] < 200 * 1024**2


def test_identity_bound_is_enforced_at_short_final_source(tmp_path, monkeypatch):
    known_case(tmp_path)
    monkeypatch.setattr(verifier, "MAX_IDENTITIES", 1)
    report = verifier.verify_case(tmp_path, write=False)
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    assert get_metric(report, "Requests planned")["value"] is None


def test_sqlite_hard_bound_covers_reconciliation_group_index_growth(tmp_path, monkeypatch):
    import sqlite3

    known_case(tmp_path)
    monkeypatch.setattr(verifier, "MAX_SQLITE_BYTES", 64 * 1024)
    storage = verifier._EvidenceStore(tmp_path)
    try:
        artifacts = storage.load()
        row = next(iter(artifacts["results"]))
        with pytest.raises(sqlite3.DatabaseError):
            for _ in range(10_000):
                storage.groups("extra-group")["one-attempt"].append(row)
        assert storage.statistics()["scratch_bytes"] <= 64 * 1024
    finally:
        storage.close()


def test_discrepancy_working_set_limit_never_emits_partial_pass(tmp_path, monkeypatch):
    known_case(tmp_path)
    values = rows(tmp_path, "results")
    for row in values:
        row["content"] = "CORRUPTED"
    write_rows(tmp_path / "results.jsonl", values)
    monkeypatch.setattr(verifier, "MAX_RECONCILIATION_ISSUES", 1)
    report = verifier.verify_case(tmp_path, write=False)
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    assert get_metric(report, "Requests planned")["value"] is None


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_nonfinite_json_is_explicitly_invalid_instead_of_crashing_assessment(tmp_path, number):
    known_case(tmp_path)
    with (tmp_path / "results.jsonl").open("a") as stream:
        stream.write('{"attempt_id":"bad-number","timing":{"latency_ms":' + number + '}}\n')
    report = verifier.verify_case(tmp_path, write=False)
    assert report["verdicts"]["response_accounting_correctness"] != "pass"
    assert "artifact_incomplete" in issue_codes(report)
    with pytest.raises(ValueError):
        list(verifier.bounded_jsonl_rows(tmp_path / "results.jsonl"))


def _client_identity_control(tmp_path):
    checkout = tmp_path / "pinned-baseline"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    command = ["git", "-C", str(checkout)]
    content = b"independent pinned client control\n"
    blob = subprocess.check_output([*command, "hash-object", "-w", "--stdin"], input=content).decode().strip()
    tree = subprocess.check_output([*command, "mktree"], input=f"100644 blob {blob}\tclient.py\n".encode()).decode().strip()
    tree = subprocess.check_output([*command, "mktree"], input=f"040000 tree {tree}\tgiraffe\n".encode()).decode().strip()
    tree = subprocess.check_output([*command, "mktree"], input=f"040000 tree {tree}\tsrc\n".encode()).decode().strip()
    commit = subprocess.check_output([*command, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                     "commit-tree", tree], input=b"independent test fixture\n").decode().strip()
    source_sha = hashlib.sha256(content).hexdigest()
    hashes = {"src/giraffe/client.py": source_sha}
    aggregate = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    snapshot = {"sha": commit, "checkout": str(checkout), "status_porcelain": "", "tracked_file_hashes": hashes,
                "tracked_tree_sha256": aggregate, "product_source_sha256": aggregate,
                "dependency_specification_sha256": hashlib.sha256(b"{}").hexdigest(),
                "installed_dependencies": "[]", "installed_dependencies_sha256": hashlib.sha256(b"[]").hexdigest()}
    for name in ("baseline-before.json", "baseline-after.json"):
        (tmp_path / name).write_text(json.dumps(snapshot))
    return {"sha": commit, "checkout": str(checkout), "product_source_sha256": aggregate}, source_sha


def _observed_timing_control(tmp_path):
    from tests.test_validation_timing_observer import fixture_rows
    from validation_suite import timing_observer

    manifest = known_case(tmp_path)
    manifest.pop("deterministic_control")
    manifest["reference"] = {"boundary": "endpoint_application", "uncertainty_ms": None}
    manifest["thresholds"].update(timing_relative_error=.1, timing_coverage_fraction=.99,
                                   reference_uncertainty_fraction_of_interval=.1)
    baseline, source_sha = _client_identity_control(tmp_path)
    observer_sha = hashlib.sha256(timing_observer.Path(timing_observer.__file__).read_bytes()).hexdigest()
    manifest["baseline"] = baseline
    planned = rows(tmp_path, "planned")
    for row in planned:
        row["expected"].pop("timing")
    write_rows(tmp_path / "planned.jsonl", planned)
    values, observed = rows(tmp_path, "results"), []
    for index, row in enumerate(values):
        row.update(raw={"id": f"r{index}"}, planned_identity_provenance="independent_control_id")
        row["timing"].update(latency_ms=30, first_answer_ms=20, first_reasoning_ms=10, max_stream_gap_ms=5,
                             completed_ns=row["timing"]["dispatch_ns"]+30_000_000)
        for reference in fixture_rows():
            reference["metadata"]["record_id"] = f"r{index}"
            if reference["metadata"].get("field") == "content":
                reference["metadata"]["content"] = ("A " if index == 0 else "C ") if reference["metadata"]["content"] == "4" else ("B" if index == 0 else "D")
            reference["sequence"] = len(observed)
            observed.append(reference)
    write_rows(tmp_path / "results.jsonl", values)
    write_rows(tmp_path / "timing-events.jsonl", observed)
    clock = {"consistent": True, "offset_lower_ns": 0, "offset_upper_ns": 0}
    health = {"complete": True, "flush_complete": True, "overflow_count": 0, "write_errors": 0,
              "callback_errors": 0, "state_overflow_count": 0, "state_unresolved_at_uninstall": 0,
              "written": len(observed), "attempted": len(observed), "source_unchanged": True,
              "source_sha256_before": source_sha, "source_sha256_after": source_sha,
              "baseline": baseline["checkout"], "import_proof": baseline["checkout"] + "/src/giraffe/client.py",
              "observer_source_sha256": observer_sha, "clock_calibration_before": clock, "clock_calibration_after": clock,
              "events_sha256": hashlib.sha256((tmp_path / "timing-events.jsonl").read_bytes()).hexdigest()}
    (tmp_path / "timing-health.json").write_text(json.dumps(health))
    profile = {"pairs": [{"control_elapsed_ms": 100, "observed_elapsed_ms": 100.1} for _ in range(3)],
               "median_budget_ms": 5, "max_budget_ms": 10, "elapsed_gate_pass": True, "overhead_gate_pass": True}
    overhead = {"baseline_unchanged": True, "source_unchanged_during_execution": True, "observer_health_all_complete": True,
                "observer_source_sha256": observer_sha, "baseline_sha": baseline["sha"], "profiles": {"E1-control": profile}}
    path = tmp_path / "timing-overhead.json"
    path.write_text(json.dumps(overhead))
    manifest["timing_observer"] = {"enabled": True, "observer_source_sha256": observer_sha,
                                   "uncertainty_budget_ns": 100_000,
                                   "overhead_evidence": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                         "profile": "E1-control"}}
    return manifest


def test_independent_observed_intervals_qualify_named_metrics_only(tmp_path):
    manifest = _observed_timing_control(tmp_path)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    assert report["verdicts"]["measurement_accuracy"] == "qualified"
    assert get_metric(report, "Qualified measurement count and percentage")["value"]["qualified_count"] == 2
    timing = get_metric(report, "Timing error distributions and allowed tolerances")["value"]
    assert timing["qualified_metric_samples"]["latency_ms"] == 2
    assert timing["metrics"]["latency_ms"]["reported_ms"]["qualification_coverage"]["qualified_count"] == 2
    uncertainty = get_metric(report, "Reference clock/network/observation uncertainty")
    assert uncertainty["state"] == "measured"
    assert uncertainty["value"]["network_packet_arrival"] == "not_measured"
    assert report["verdicts"]["capacity_contribution"] == "inconclusive"


@pytest.mark.parametrize("broken", ["health_hash", "health_missing", "health_nonobject", "missing_overhead", "lying_overhead_flag", "wrong_source", "wide_interval", "duplicate_id", "reported_shift", "relative_error"])
def test_observed_timing_fails_closed_on_broken_reference_or_metric(tmp_path, broken):
    manifest = _observed_timing_control(tmp_path)
    if broken == "health_hash":
        health = json.loads((tmp_path / "timing-health.json").read_text())
        health["events_sha256"] = "wrong"
        (tmp_path / "timing-health.json").write_text(json.dumps(health))
    elif broken == "health_missing":
        (tmp_path / "timing-health.json").unlink()
    elif broken == "health_nonobject":
        (tmp_path / "timing-health.json").write_text("[]")
    elif broken in {"missing_overhead", "lying_overhead_flag"}:
        path = tmp_path / "timing-overhead.json"
        if broken == "missing_overhead":
            path.unlink()
        else:
            value = json.loads(path.read_text())
            for pair in value["profiles"]["E1-control"]["pairs"]:
                pair["observed_elapsed_ms"] = 200
            path.write_text(json.dumps(value))
            manifest["timing_observer"]["overhead_evidence"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif broken == "wrong_source":
        manifest["baseline"]["product_source_sha256"] = "0"*64
    elif broken == "wide_interval":
        manifest["timing_observer"]["uncertainty_budget_ns"] = 1
    else:
        values = rows(tmp_path, "results")
        if broken == "duplicate_id":
            values[1]["raw"]["id"] = values[0]["raw"]["id"]
        elif broken == "reported_shift":
            values[0]["timing"]["first_answer_ms"] = 25
        else:
            # 0.75ms lies below1ms absolute but exceeds10% of the5ms reference gap.
            values[0]["timing"]["max_stream_gap_ms"] = 5.75
        write_rows(tmp_path / "results.jsonl", values)
    report = verifier.verify_case(tmp_path, manifest, write=False)
    assert report["verdicts"]["measurement_accuracy"] != "qualified"
    if broken == "health_missing":
        for fields in report["sections"].values():
            for item in fields.values():
                assert "timing-health.json" not in item["evidence"]
    timing = get_metric(report, "Timing error distributions and allowed tolerances")["value"]
    if broken in {"reported_shift", "relative_error"}:
        assert report["verdicts"]["overall_experiment_result"] == "fail"
        name = "first_answer_ms" if broken == "reported_shift" else "max_stream_gap_ms"
        assert timing["metrics"][name]["reported_ms"]["qualification_coverage"]["qualified_count"] == 1
    elif broken == "wide_interval":
        assert timing["metrics"]["latency_ms"]["reported_ms"]["qualification_coverage"]["qualified_count"] == 0


def test_timing_profile_fingerprint_matches_load_geometry_not_duration():
    base = {"mode": "fixed_rate", "rate_rps": 200, "event_count": 4, "active_seconds": 10, "request_count": 2000}
    longer = {**base, "active_seconds": 300, "request_count": 60000, "id": "different"}
    assert verifier.timing_profile_fingerprint(base) == verifier.timing_profile_fingerprint(longer)
    assert verifier.timing_profile_fingerprint(base) != verifier.timing_profile_fingerprint({**base, "rate_rps": 100})


@pytest.mark.parametrize("behavior", [
    {"first_delay_ms": 500}, {"straggler_every": 10}, {"straggler_delay_ms": 500},
    {"events": ["different", "chunks"]}, {"request_timeout_seconds": 1},
    {"unknown_future_behavior": True},
])
def test_timing_profile_fingerprint_rejects_changed_workload_behavior(behavior):
    base = {"mode": "sustained_concurrency", "concurrency": 50, "event_delay_ms": 320}
    assert verifier.timing_profile_fingerprint(base) != verifier.timing_profile_fingerprint({**base, **behavior})


def test_timing_profile_fingerprint_retains_burst_count():
    base = {"mode": "burst", "concurrency": 50, "request_count": 10}
    assert verifier.timing_profile_fingerprint(base) != verifier.timing_profile_fingerprint({**base, "request_count": 50})


def test_observer_coverage_denominator_includes_unoffered_finite_slots(tmp_path):
    manifest = _observed_timing_control(tmp_path)
    manifest["scenario"]["request_count"] = 3
    result = verifier._observer_timing(tmp_path, manifest, rows(tmp_path, "results"), manifest["thresholds"], write=False)
    assert result["qualified_count"] == 2 and result["expected_measurements"] == 3
    assert result["coverage_fraction"] == pytest.approx(2/3)
    assert result["qualified"] is False


@pytest.mark.parametrize("changed", [{"concurrency": 100}, {"first_delay_ms": 500},
                                      {"straggler_every": 10}, {"straggler_delay_ms": 500}])
def test_load_overhead_requires_matching_frozen_geometry_fingerprint(tmp_path, changed):
    manifest = _observed_timing_control(tmp_path)
    manifest.update(experiment_id="E3", planned_semantics="duration_admission_intent")
    manifest["scenario"].update(mode="sustained_concurrency", concurrency=50)
    path = tmp_path / "timing-overhead.json"
    overhead = json.loads(path.read_text())
    fingerprint = verifier.timing_profile_fingerprint(manifest["scenario"])
    overhead["profiles"]["E1-control"].update(experiment_id="E3", workload_fingerprint=fingerprint)
    path.write_text(json.dumps(overhead))
    manifest["timing_observer"]["overhead_evidence"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["timing_observer"]["workload_fingerprint"] = fingerprint
    eligible = verifier._observer_timing(tmp_path, manifest, rows(tmp_path, "results"), manifest["thresholds"], write=False)
    assert eligible["qualified"] is True
    manifest["scenario"].update(changed)
    mismatched = verifier._observer_timing(tmp_path, manifest, rows(tmp_path, "results"), manifest["thresholds"], write=False)
    assert mismatched["qualified"] is False
    assert mismatched["overhead"]["valid_for_profile"] is False


def test_real_driver_manifest_identity_uses_git_blob_not_current_worktree(tmp_path):
    manifest = _observed_timing_control(tmp_path)
    assert set(manifest["baseline"]) == {"sha", "checkout", "product_source_sha256"}
    checkout = tmp_path / "pinned-baseline"
    current = checkout / "src/giraffe/client.py"
    current.parent.mkdir(parents=True)
    current.write_text("different current worktree; not the saved execution oracle\n")
    health = json.loads((tmp_path / "timing-health.json").read_text())
    proof = verifier.validate_client_source_identity(tmp_path, manifest, health)
    assert proof["valid"] is True and proof["issues"] == []
    assert proof["client_sha256"] != hashlib.sha256(current.read_bytes()).hexdigest()
    assert len(proof["git_blob"]) == 40
    timing = verifier._observer_timing(tmp_path, manifest, rows(tmp_path, "results"), manifest["thresholds"], write=False)
    assert timing["qualified"] is True and timing["source_identity"]["valid"] is True


@pytest.mark.parametrize("broken", ["missing_before", "missing_after", "hash_before", "hash_after", "import_path",
                                      "snapshot_hash", "source_aggregate", "dependencies", "commit", "missing_git"])
def test_source_identity_chain_failures_stay_unqualified(tmp_path, broken):
    manifest = _observed_timing_control(tmp_path)
    health = json.loads((tmp_path / "timing-health.json").read_text())
    if broken.startswith("missing_") and broken != "missing_git":
        (tmp_path / ("baseline-before.json" if broken == "missing_before" else "baseline-after.json")).unlink()
    elif broken in {"hash_before", "hash_after"}:
        health["source_sha256_" + broken.removeprefix("hash_")] = "0"*64
    elif broken == "import_path":
        health["import_proof"] = str(tmp_path / "other.py")
    elif broken == "source_aggregate":
        manifest["baseline"]["product_source_sha256"] = "0"*64
    elif broken == "commit":
        manifest["baseline"]["sha"] = "0"*40
    elif broken == "missing_git":
        import shutil
        shutil.rmtree(tmp_path / "pinned-baseline/.git")
    else:
        path = tmp_path / "baseline-after.json"
        snapshot = json.loads(path.read_text())
        if broken == "snapshot_hash":
            snapshot["tracked_file_hashes"]["src/giraffe/client.py"] = "0"*64
        else:
            snapshot["installed_dependencies"] = "changed"
        path.write_text(json.dumps(snapshot))
    proof = verifier.validate_client_source_identity(tmp_path, manifest, health)
    assert proof["valid"] is False and proof["issues"]


def test_missing_calibration_overhead_has_explicit_reason(tmp_path):
    manifest = _observed_timing_control(tmp_path)
    manifest["timing_observer"].pop("overhead_evidence")
    manifest["timing_observer"]["calibration_constituent"] = True
    timing = verifier._observer_timing(tmp_path, manifest, rows(tmp_path, "results"), manifest["thresholds"], write=False)
    assert timing["qualified"] is False and timing["source_identity"]["valid"] is True
    assert timing["overhead"]["valid_for_profile"] is False
    assert any("overhead evidence is absent" in reason for reason in timing["issues"])
    assert not any("directory" in reason for reason in timing["issues"])


def _productivity_rows():
    return [{"phase": "control", "status": "completed", "valid": True, "score": True,
             "content": "CONTROL", "raw": {"stream": True, "chunks": 100, "answer_chunks": 100,
                                             "reasoning": "CONTROL", "evaluation_error": None}},
            {"phase": "measured", "status": "completed", "valid": True, "score": True,
             "content": "é", "raw": {"stream": True, "chunks": 3, "answer_chunks": 2,
                                       "reasoning": "x", "evaluation_error": None}},
            {"phase": "measured", "status": "completed", "valid": True, "score": False,
             "content": "A", "raw": {"stream": True, "chunks": 2, "answer_chunks": 1,
                                       "reasoning": "", "evaluation_error": None}}]


def test_productivity_exact_phase_counts_utf8_bytes_and_whole_process_rates():
    state = verifier._RetainedProductivity()
    for row in _productivity_rows():
        state.add([row])
    summary = state.summary({"seconds": 10})["by_explicit_phase"]
    measured = summary["measured"]
    assert measured["counts"]["unique_retained_attempts"] == 2
    assert measured["counts"]["protocol_valid_completed"] == 2
    assert measured["counts"]["score_true"] == measured["counts"]["score_false"] == 1
    assert measured["rates_per_second"]["unique_retained_attempts"] == .2
    assert measured["volumes"]["choice_messages"]["unambiguous_retained_total"] == 5
    assert measured["volumes"]["choice_messages"]["rate_per_second"] == .5
    assert measured["volumes"]["answer_chunks"]["rate_per_second"] == .3
    assert measured["volumes"]["answer_utf8_bytes"]["rate_per_second"] == .3
    assert measured["volumes"]["reasoning_utf8_bytes"]["rate_per_second"] == .1
    assert summary["control"]["counts"]["unique_retained_attempts"] == 1


def test_productivity_duplicate_missing_and_nonboolean_score_are_not_inflated():
    _, a, b = _productivity_rows()
    state = verifier._RetainedProductivity()
    state.add([a, copy.deepcopy(a)])
    b["raw"].pop("reasoning")
    b["raw"]["chunks"] = True
    b["raw"]["evaluation_error"] = "scorer failed"
    b["score"] = "False"
    state.add([b])
    measured = state.summary({"seconds": None})["by_explicit_phase"]["measured"]
    assert measured["counts"]["duplicate_result_groups_excluded"] == 1
    assert measured["counts"]["unique_retained_attempts"] == 1
    assert measured["counts"]["score_false"] == 0 and measured["counts"]["score_unavailable"] == 1
    assert measured["counts"]["evaluation_errors"] == 1
    assert measured["volumes"]["reasoning_utf8_bytes"]["unambiguous_retained_total"] is None
    assert measured["volumes"]["choice_messages"]["unambiguous_retained_total"] is None
    assert measured["volumes"]["answer_utf8_bytes"]["unambiguous_retained_total"] == 1
    assert measured["rates_per_second"]["unique_retained_attempts"] is None


@pytest.mark.parametrize("labels", [{}, {"phase": "measured", "population": "control"},
                                      {"phase": "measured", "metadata": {"population": "control"}}, {"phase": ["measured"]}])
def test_productivity_absent_or_conflicting_phase_is_unknown(labels):
    row = _productivity_rows()[1]
    row.pop("phase")
    row.update(labels)
    state = verifier._RetainedProductivity()
    state.add([row])
    assert state.cohorts["unknown"]["counts"]["unique_retained_attempts"] == 1
    assert state.cohorts["measured"]["counts"]["unique_retained_attempts"] == 0


@pytest.mark.parametrize("broken", [{"clock_domain": ""}, {"clock_domain": None}, {"clock_domain": 1},
                                      {"clock_domain": "other"}, {"monotonic_ns": True}, {"monotonic_ns": 0}])
def test_productivity_window_requires_nonempty_same_clock_and_integer_times(broken):
    start = {"kind": "candidate_process_started", "clock_domain": "driver", "monotonic_ns": 1_000_000_000}
    end = {"kind": "candidate_process_exited", "clock_domain": "driver", "monotonic_ns": 11_000_000_000}
    assert verifier._candidate_window([start, end])["seconds"] == 10
    assert verifier._candidate_window([start, {**end, **broken}])["seconds"] is None
    assert verifier._candidate_window([start, end, end])["seconds"] is None
    assert verifier._candidate_window([start], 20)["seconds"] == 20


def test_productivity_nonstream_malformed_raw_and_missing_window_preserve_states():
    state = verifier._RetainedProductivity()
    a, b = _productivity_rows()[1:]
    a["raw"]["stream"] = False
    b["raw"] = ["invalid"]
    state.add([a])
    state.add([b])
    measured = state.summary({"seconds": None})["by_explicit_phase"]["measured"]
    assert measured["volumes"]["choice_messages"]["not_applicable_samples"] == 1
    assert measured["volumes"]["choice_messages"]["missing_samples"] == 1
    assert measured["volumes"]["answer_utf8_bytes"]["rate_per_second"] is None


def test_productivity_sqlite_equivalence_and_wait_attribution(tmp_path):
    manifest = known_case(tmp_path)
    values = rows(tmp_path, "results")
    for row in values:
        row.update(phase="measured", raw={"chunks": 3, "answer_chunks": 2, "stream": True, "reasoning": ""})
        row["timing"].update(client_wait_ms=4, client_preparation_ms=.5)
    write_rows(tmp_path / "results.jsonl", values)
    small = verifier.verify_case(tmp_path, manifest, disk_backed=False, write=False)
    stored = verifier.verify_case(tmp_path, manifest, write=False)
    for label in ("Responses processed per second", "Stream protocol events processed per second", "Response bytes processed per second"):
        assert get_metric(small, label) == get_metric(stored, label)
        assert get_metric(stored, label)["verdict"] == "inconclusive"
        assert get_metric(stored, label)["state"] == "unknown"  # No process window exists in this control.
    wait = get_metric(stored, "Connection-pool or execution-slot wait")
    assert wait["state"] == "unknown" and wait["verdict"] == "inconclusive"
    assert wait["value"]["combined_scheduled_to_dispatch_ms"]["max"] == 4
    assert wait["value"]["isolated_pool_or_slot_wait"] is None
