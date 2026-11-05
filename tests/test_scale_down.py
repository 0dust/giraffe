"""Evidence rules for an externally controlled replica removal."""
from copy import deepcopy

import pytest

from giraffe.models import Limits, RequestRecord, ScaleDownConfig, ScaleDownEvidence
from giraffe.scale_down import evaluate_scale_down


def scenario():
    settings = ScaleDownConfig(target="service", long_cohort_size=1, min_overlap_samples=1,
                              min_baseline_samples=1, min_post_samples=1,
                              requests_per_second=1, post_seconds=1, grace_seconds=1)
    events = [
        dict(event_id="remove", sequence=2, time_ms=100, kind="removal_requested", replica_id="b"),
        dict(event_id="admit", sequence=3, time_ms=110, kind="request_admitted", replica_id="b", request_id="long", attempt_id="original"),
        dict(event_id="drain", sequence=4, time_ms=200, kind="drain_effective", replica_id="b"),
        dict(event_id="done", sequence=5, time_ms=300, kind="request_completed", replica_id="b", request_id="long", attempt_id="original", outcome="completed"),
        dict(event_id="terminated", sequence=6, time_ms=400, kind="replica_terminated", replica_id="b"),
    ]
    def snapshot(start, end, sequence, present, replicas):
        return dict(collected_start_ms=start, collected_end_ms=end, capture_start_ms=start, capture_end_ms=end,
                    export=dict(schema_version=1, target="service", source="router", clock_id="router-boot-1", through_sequence=sequence,
                                inventories=[dict(event_id=f"inventory-{sequence}", sequence=sequence, time_ms={1: 0, 4: 200, 6: 400}[sequence], replicas=replicas)],
                                events=present, coverage=[dict(replica_id="b", start_sequence=0, end_sequence=sequence, complete=True, scope="all")]))
    evidence = dict(schema_version=1, timing_health={"schema_version": 1, "windows": []}, timing_threshold_ms=100,
                    armed_ms=50, marker_accepted_ms=90,
                    marker=dict(event_id="operator", nonce="fresh-run", target="service", replica_id="b", source="test"),
                    baseline_request_ids=["baseline"], long_request_ids=["long"], followup_request_ids=["post"],
                    snapshots=[snapshot(50, 60, 1, [], ["a", "b"]), snapshot(200, 210, 4, events[:3], ["a", "b"]), snapshot(400, 410, 6, events, ["a"])],
                    answer_started_ms={"baseline": 10, "long": 20, "post": 430},
                    post_started_ms=410, post_ended_ms=1410)
    def record(id, dispatch, completed):
        return RequestRecord(id=id, target="service", fixture_id="fixture", scenario="scale_down", check_ids=["scale_down"],
                             started_at="2026-11-05T00:00:00+00:00", status="completed", valid=True, score=True,
                             stream=True, timing_schema=1, answer_read_timestamps_ms=[10, 20], stream_terminated=True, finish_reason="stop", answer_chars=10, answer_chunks=2,
                             dispatch_ms=dispatch, timing_start_ms=dispatch, first_output_ms=10,
                             completed_ms=completed, elapsed_ms=completed-dispatch, backend_id="b" if id == "long" else "a")
    return settings, evidence, [record("baseline", 0, 30), record("long", 10, 350), record("post", 420, 450)]


def evaluate(settings, evidence, records):
    return evaluate_scale_down(settings, ScaleDownEvidence.model_validate(evidence), records, Limits())


def statuses(result):
    return {key: value["status"] for key, value in result.metrics["results"].items()}


def test_complete_graceful_evidence_passes_each_result():
    result = evaluate(*scenario())
    assert result.id == "scale_down" and result.status == "pass"
    assert statuses(result) == dict(continuity="pass", drain="pass", capacity="pass")
    assert "long" in result.metrics["results"]["continuity"]["request_ids"]


def test_interrupted_original_is_not_erased_by_successful_retry():
    settings, evidence, records = scenario()
    records[1] = records[1].model_copy(update=dict(status="failed", valid=False, stream_terminated=False, error="peer closed"))
    records.append(records[1].model_copy(update=dict(id="retry", status="completed", valid=True, stream_terminated=True, error=None)))
    result = evaluate(settings, evidence, records)
    assert result.status == "fail" and statuses(result)["continuity"] == "fail"
    assert statuses(result)["capacity"] == "pass"


def test_operator_marker_alone_cannot_establish_transition():
    settings, evidence, records = scenario()
    evidence["snapshots"] = []
    assert set(statuses(evaluate(settings, evidence, records)).values()) == {"inconclusive"}


def test_positive_admission_violation_survives_incomplete_coverage():
    settings, evidence, records = scenario()
    latest = evidence["snapshots"][-1]["export"]
    latest["events"].append(dict(event_id="late", sequence=7, time_ms=450, kind="request_admitted", replica_id="b", request_id="other", attempt_id="late"))
    latest["through_sequence"] = 7
    latest["coverage"][0].update(complete=False, truncated=True)
    assert statuses(evaluate(settings, evidence, records))["drain"] == "fail"


@pytest.mark.parametrize("change", ["no_overlap", "no_attribution", "unknown_clock", "truncated", "giraffe_scope"])
def test_incomplete_drain_evidence_never_passes(change):
    settings, evidence, records = scenario()
    if change == "no_overlap":
        records[1] = records[1].model_copy(update={"completed_ms": 40})
    elif change == "no_attribution":
        for snapshot in evidence["snapshots"]:
            for event in snapshot["export"]["events"]:
                if event["kind"].startswith("request_"):
                    event["request_id"] = "unrelated"
    elif change == "unknown_clock":
        for snapshot in evidence["snapshots"]:
            snapshot["capture_start_ms"] = snapshot["capture_end_ms"] = None
    elif change == "truncated":
        evidence["snapshots"][-1]["export"]["coverage"][0]["truncated"] = True
    else:
        for snapshot in evidence["snapshots"]:
            snapshot["export"]["coverage"][0]["scope"] = "giraffe"
    assert statuses(evaluate(settings, evidence, records))["drain"] == "inconclusive"


def test_grace_boundary_uses_declared_anchor_not_client_cleanup_allowance():
    settings, evidence, records = scenario()
    settings = settings.model_copy(update=dict(grace_seconds=.2, completion_seconds=20, grace_anchor="drain_effective"))
    assert statuses(evaluate(settings, evidence, records))["drain"] == "pass"
    settings = settings.model_copy(update=dict(grace_seconds=.199))
    assert statuses(evaluate(settings, evidence, records))["drain"] == "fail"


def test_unfinished_work_at_termination_is_failure_even_with_force_policy():
    settings, evidence, records = scenario()
    for snapshot in evidence["snapshots"]:
        snapshot["export"]["events"] = [e for e in snapshot["export"]["events"] if e["kind"] != "request_completed"]
    assert statuses(evaluate(settings, evidence, records))["drain"] == "fail"


def test_client_cancelled_cohort_is_inconclusive_not_endpoint_failure():
    settings, evidence, records = scenario()
    records[1] = records[1].model_copy(update=dict(status="cancelled", valid=False, termination_reason="run_budget"))
    evidence["stop_reason"] = "request budget"
    assert statuses(evaluate(settings, evidence, records))["continuity"] == "inconclusive"


def test_stale_initial_lifecycle_history_cannot_be_reused():
    settings, evidence, records = scenario()
    evidence["snapshots"][0]["export"]["events"] = deepcopy(evidence["snapshots"][-1]["export"]["events"])
    evidence["snapshots"][0]["export"]["through_sequence"] = 6
    assert set(statuses(evaluate(settings, evidence, records)).values()) == {"inconclusive"}


def test_conflicting_event_id_cannot_manufacture_pass():
    settings, evidence, records = scenario()
    evidence["snapshots"][-1]["export"]["events"][2]["replica_id"] = "different"
    assert statuses(evaluate(settings, evidence, records))["drain"] == "inconclusive"


def test_unknown_termination_and_slo_failure_are_continuity_failures():
    settings, evidence, records = scenario()
    records[1] = records[1].model_copy(update=dict(stream_terminated=False))
    assert statuses(evaluate(settings, evidence, records))["continuity"] == "fail"
    settings, evidence, records = scenario()
    assert statuses(evaluate_scale_down(settings, ScaleDownEvidence.model_validate(evidence), records, Limits(latency_ms=200)))["continuity"] == "fail"


def test_insufficient_delivered_post_load_is_inconclusive():
    settings, evidence, records = scenario()
    settings = settings.model_copy(update=dict(requests_per_second=2))
    assert statuses(evaluate(settings, evidence, records))["capacity"] == "inconclusive"


def test_reasoning_reclassified_after_arming_cannot_create_answer_overlap():
    settings, evidence, records = scenario()
    records[1] = records[1].model_copy(update=dict(answer_read_timestamps_ms=[240]))
    result = evaluate(settings, evidence, records)
    assert statuses(result)["continuity"] == "inconclusive"
    assert statuses(result)["drain"] == "inconclusive"


def test_wrong_target_marker_cannot_attribute_a_violation_to_this_scenario():
    settings, evidence, records = scenario()
    evidence["marker"]["target"] = "another-service"
    latest = evidence["snapshots"][-1]["export"]
    latest["events"].append(dict(event_id="late", sequence=7, time_ms=450, kind="request_admitted", replica_id="b", request_id="other", attempt_id="late"))
    latest["through_sequence"] = 7
    assert set(statuses(evaluate(settings, evidence, records)).values()) == {"inconclusive"}


def test_intentionally_unscored_fixture_does_not_invent_correctness_requirement():
    from giraffe.models import EvaluationEvidence
    settings, evidence, records = scenario()
    records[1] = records[1].model_copy(update=dict(score=None, evaluation=EvaluationEvidence(messages=[], scorer="none")))
    assert statuses(evaluate(settings, evidence, records))["continuity"] == "pass"


def test_failed_request_before_confirmed_transition_is_not_scale_down_failure():
    settings, evidence, records = scenario()
    records[1] = records[1].model_copy(update=dict(status="failed", valid=False, completed_ms=30))
    assert statuses(evaluate(settings, evidence, records))["continuity"] == "inconclusive"


def test_requests_after_post_window_cannot_fill_missing_load():
    settings, evidence, records = scenario()
    records[2] = records[2].model_copy(update=dict(dispatch_ms=1500, completed_ms=1550))
    assert statuses(evaluate(settings, evidence, records))["capacity"] == "inconclusive"


@pytest.mark.parametrize("completion", ["missing", "failed_after_termination"])
def test_abrupt_termination_without_drain_cutoff_fails_original_stream(completion):
    settings, evidence, records = scenario()
    for snapshot in evidence["snapshots"]:
        snapshot["export"]["events"] = [e for e in snapshot["export"]["events"] if e["kind"] not in {"drain_effective", "request_completed"}]
    if completion == "failed_after_termination":
        latest = evidence["snapshots"][-1]["export"]
        latest["events"].append(dict(event_id="failed", sequence=7, time_ms=410, kind="request_completed", replica_id="b", request_id="long", attempt_id="original", outcome="failed"))
        latest["through_sequence"] = 7
        latest["coverage"][0]["end_sequence"] = 7
    records[1] = records[1].model_copy(update=dict(status="failed", valid=False, stream_terminated=False, error="connection closed"))
    result = evaluate(settings, evidence, records)
    assert statuses(result) == dict(continuity="fail", drain="fail", capacity="pass")
    assert "cutoff" in result.metrics["results"]["drain"]["summary"]


def test_completed_before_abrupt_removal_does_not_claim_continuity():
    settings, evidence, records = scenario()
    for snapshot in evidence["snapshots"]:
        snapshot["export"]["events"] = [e for e in snapshot["export"]["events"] if e["kind"] != "drain_effective"]
    result = evaluate(settings, evidence, records)
    assert statuses(result) == dict(continuity="inconclusive", drain="inconclusive", capacity="pass")


@pytest.mark.parametrize("change", ["future", "conflicting_id", "conflicting_sequence"])
def test_inventory_conflicts_cannot_manufacture_topology_pass(change):
    settings, evidence, records = scenario()
    if change == "future":
        evidence["snapshots"][0]["export"]["inventories"][0]["sequence"] = 99
    elif change == "conflicting_id":
        evidence["snapshots"][-1]["export"]["inventories"][0]["event_id"] = "inventory-1"
    else:
        inventory = evidence["snapshots"][-1]["export"]["inventories"][0]
        evidence["snapshots"][-1]["export"]["inventories"].append({**inventory, "event_id": "other-inventory", "replicas": ["a", "b"]})
    assert "pass" not in statuses(evaluate(settings, evidence, records)).values()


def test_completed_failure_before_transition_is_not_a_drain_violation():
    settings, evidence, records = scenario()
    old = [dict(event_id="old-admit", sequence=0, time_ms=1, kind="request_admitted", replica_id="b", request_id="old", attempt_id="old"),
           dict(event_id="old-fail", sequence=1, time_ms=10, kind="request_completed", replica_id="b", request_id="old", attempt_id="old", outcome="failed")]
    for snapshot in evidence["snapshots"]:
        snapshot["export"]["events"] = [*old, *snapshot["export"]["events"]]
    evidence["snapshots"][0]["export"]["inventories"][0]["time_ms"] = 10
    assert statuses(evaluate(settings, evidence, records)) == dict(continuity="pass", drain="pass", capacity="pass")


def test_backend_retry_completion_does_not_erase_failed_original_attempt():
    settings, evidence, records = scenario()
    latest = evidence["snapshots"][-1]["export"]
    original_completion = next(event for event in latest["events"] if event["event_id"] == "done")
    original_completion["outcome"] = "failed"
    latest["events"][-1].update(sequence=8, time_ms=500)
    latest["events"].extend([
        dict(event_id="retry-admit", sequence=6, time_ms=350, kind="request_admitted", replica_id="b", request_id="long", attempt_id="retry"),
        dict(event_id="retry-done", sequence=7, time_ms=450, kind="request_completed", replica_id="b", request_id="long", attempt_id="retry", outcome="completed"),
    ])
    latest["through_sequence"] = 8
    latest["coverage"][0]["end_sequence"] = 8
    latest["inventories"][0].update(sequence=8, time_ms=500)
    records[1] = records[1].model_copy(update=dict(status="failed", valid=False, stream_terminated=False, error="original interrupted"))
    result = evaluate(settings, evidence, records)
    assert statuses(result)["continuity"] == "fail"
    assert statuses(result)["drain"] == "fail"
    assert "unfinished or unsuccessful" in result.metrics["results"]["drain"]["summary"]


@pytest.mark.parametrize("window,expected", [
    ({"start_ms": 100, "end_ms": 300, "lag_ms": 150}, "inconclusive"),
    ({"start_ms": 800, "end_ms": 1000, "lag_ms": 150}, "fail"),
])
def test_timing_limits_qualify_only_overlapping_client_delay(window, expected):
    settings, evidence, records = scenario()
    evidence["timing_health"] = {"schema_version": 1, "windows": [window]}
    evidence["timing_threshold_ms"] = 100
    result = evaluate_scale_down(settings, ScaleDownEvidence.model_validate(evidence), records, Limits(latency_ms=200))
    assert statuses(result)["continuity"] == expected
    if expected == "inconclusive":
        assert "client scheduling" in result.metrics["results"]["continuity"]["summary"].lower()
    assert statuses(result)["capacity"] == "pass"


def test_missing_scoped_timing_health_cannot_certify_or_attribute_limits():
    settings, evidence, records = scenario()
    evidence.pop("timing_health")
    evidence.pop("timing_threshold_ms")
    result = evaluate_scale_down(settings, ScaleDownEvidence.model_validate(evidence), records, Limits(latency_ms=200))
    assert statuses(result)["continuity"] == "inconclusive"


def test_conflicting_attempt_terminals_cannot_certify_drain():
    settings, evidence, records = scenario()
    latest = evidence["snapshots"][-1]["export"]
    latest["events"].append(dict(event_id="contradictory", sequence=7, time_ms=500, kind="request_completed", replica_id="b", request_id="long", attempt_id="original", outcome="failed"))
    latest["through_sequence"] = latest["coverage"][0]["end_sequence"] = 7
    result = evaluate(settings, evidence, records)
    assert statuses(result)["drain"] == "inconclusive"
    assert "conflicting" in result.metrics["results"]["drain"]["summary"].lower()
    latest["events"].append(dict(event_id="late-with-conflict", sequence=8, time_ms=600, kind="request_admitted", replica_id="b", request_id="other", attempt_id="late"))
    latest["through_sequence"] = 8
    assert statuses(evaluate(settings, evidence, records))["drain"] == "fail"


def test_survivor_inventory_with_prebaseline_source_time_is_not_proof():
    settings, evidence, records = scenario()
    evidence["snapshots"][-1]["export"]["inventories"][0]["time_ms"] = 0
    result = evaluate(settings, evidence, records)
    assert statuses(result)["capacity"] == "inconclusive"
    assert "inventory" in result.metrics["results"]["capacity"]["summary"].lower()


def test_inventory_at_same_watermark_can_follow_its_last_event():
    settings, evidence, records = scenario()
    evidence["snapshots"][-1]["export"]["inventories"][0]["time_ms"] = 410
    assert statuses(evaluate(settings, evidence, records))["capacity"] == "pass"


def test_rolling_coverage_cannot_exclude_unknown_earlier_active_work():
    settings, evidence, records = scenario()
    evidence["snapshots"][-1]["export"]["coverage"][0]["start_sequence"] = 3
    assert statuses(evaluate(settings, evidence, records))["drain"] == "inconclusive"


def test_known_prearming_removal_does_not_fail_active_scenario():
    settings, evidence, records = scenario()
    evidence.update(armed_ms=500, marker_accepted_ms=550)
    latest = evidence["snapshots"][-1]["export"]
    latest["events"].append(dict(event_id="historical-late", sequence=7, time_ms=450, kind="request_admitted", replica_id="b", request_id="old", attempt_id="old"))
    latest["through_sequence"] = latest["coverage"][0]["end_sequence"] = 7
    assert set(statuses(evaluate(settings, evidence, records)).values()) == {"inconclusive"}


@pytest.mark.asyncio
async def test_execution_freezes_scoped_timing_health_and_threshold():
    from giraffe.scale_down import execute, result
    from tests.test_scale_down_runner import make_run
    run, target, client, fixtures, _ = make_run()
    await execute(run, target, client, fixtures)
    saved = deepcopy(run.observations[target.name]["scale_down"])
    assert saved["timing_health"]["schema_version"] == 1
    assert saved["timing_threshold_ms"] == 100
    run.timing_health.windows.append({"start_ms": 0, "end_ms": 100000, "lag_ms": 9000})
    run.records.append(run.records[0].model_copy(update=dict(id="later-warm", scenario="warm", first_output_ms=10000, status="completed", valid=True)))
    result(run, target)
    assert run.observations[target.name]["scale_down"]["timing_health"] == saved["timing_health"]
    assert run.observations[target.name]["scale_down"]["timing_threshold_ms"] == 100


def test_recovery_retains_scoped_timing_instead_of_reintroducing_false_failure(tmp_path):
    from tests.test_scale_down_reporting import journal_report, report_fixture
    report = report_fixture()
    health = {"schema_version": 1, "windows": [{"start_ms": 100, "end_ms": 300, "lag_ms": 150}]}
    report.observations["targets"]["service"]["scale_down"]["timing_health"] = health
    report.manifest["config"]["limits"] = {"latency_ms": 200}
    recovered = journal_report(tmp_path, report)
    evidence = recovered.observations["targets"]["service"]["scale_down"]
    check = next(check for check in recovered.checks if check.id == "scale_down")
    assert evidence["timing_health"] == health
    assert evidence["timing_threshold_ms"] == 100
    assert check.metrics["results"]["continuity"]["status"] == "inconclusive"
    assert check.metrics["results"]["continuity"]["metrics"]["unattributed_timing_violations"] == ["long"]
