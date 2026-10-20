import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

from validation_suite.baseline import (baseline_command, baseline_environment, iter_report_requests,
                                      make_configuration, exported_phases, normalize_results, support_reason)
from validation_suite.driver import (_ProgressConsumer, _descriptor_counts, campaign, export_product_outputs,
                                     freeze, load_frozen, planned_rows, projected_feasibility,
                                     require_ignored, resource_summary, run_case)
from validation_suite.driver import _record_missing_html, main, reviewed_execution_plan, storage_bytes
from validation_suite.scenarios import digest, resource_policy, scenario_matrix
from validation_suite.verifier import verification_limits


def cases():
    return {case["id"]: case for case in scenario_matrix()}


def test_adapter_keeps_venv_executable_and_pinned_import_path(tmp_path):
    executable = tmp_path / "venv-python"
    executable.symlink_to("/usr/bin/python3")
    command = baseline_command(tmp_path, str(executable), tmp_path / "config.json", tmp_path / "output")
    assert command[0] == str(executable)
    assert command[1:4] == ["-m", "giraffe.cli", "run"]
    assert baseline_environment(tmp_path)["PYTHONPATH"] == str(tmp_path / "src")


def test_configuration_uses_real_modes_and_reserves_auxiliary_budget():
    selected = cases()
    burst = make_configuration(selected["E2-burst-10"], "http://127.0.0.1:1/v1")
    assert burst["checks"] == ["arrivals"]
    assert burst["arrivals"]["requests"] == burst["max_requests"] == 10
    sustained = make_configuration(selected["E3-qualification-c10"], "http://127.0.0.1:1/v1")
    assert sustained["checks"] == ["recovery"]
    assert sustained["sustained_seconds"] == 300
    assert sustained["max_duration_seconds"] > 300
    fixed = make_configuration(selected["E4-qualification-r10"], "http://127.0.0.1:1/v1")
    assert fixed["traffic"]["arrival"] == "steady"
    assert fixed["traffic"]["duration_seconds"] == 300
    assert fixed["max_requests"] == 3002
    assert fixed["request_pack"]["cases"][0]["scorer"] == "exact"


def test_unavailable_injectors_remain_explicit_per_case():
    selected = cases()
    assert support_reason(selected["E5-fixed_rate-evaluation"])
    assert support_reason(selected["E6-worker_crash"])
    assert support_reason(selected["E6-terminate_during_persistence"])
    assert support_reason(selected["E5-fixed_rate-telemetry"]) is None
    assert support_reason(selected["E6-bounded_storage"]) is None


def test_planned_arrivals_are_not_derived_from_completed_results():
    selected = cases()
    context = {"case_id": "test-r1", "candidate": "current-python", "repetition": 1, "study_id": "test"}
    rows = planned_rows(selected["E4-smoke-r10"], context)
    assert len(rows) == 21
    assert rows[1]["scheduled_offset_ms"] == 0
    assert rows[-1]["scheduled_offset_ms"] == 1900
    assert len({row["logical_request_id"] for row in rows}) == 21
    dynamic = planned_rows(selected["E3-smoke-c10"], context)
    assert len(dynamic) == 3
    assert dynamic[0]["logical_request_id"] is None
    assert dynamic[0]["event_kind"] == "sustained_admission_intent"


def test_normalization_preserves_boundaries_ids_and_missing_observations():
    scenario = cases()["E4-smoke-r10"]
    raw = {"id": "product-id", "scenario": "request_pack", "server_request_id": "reference-00000001",
           "scheduled_offset_ms": 200, "dispatch_ms": 300, "completed_ms": 315,
           "elapsed_ms": 15, "status": "completed", "output": "42", "score": True,
           "input_tokens": 13, "completion_tokens": 1, "valid": True}
    row = normalize_results({"requests": [raw]}, scenario, {"case_id": "case-r1"})[0]
    assert row["logical_request_id"] == "case-r1:measured:000002"
    assert row["response_id"] == "reference-00000001"
    assert row["raw"] == raw
    assert row["monotonic_ns"] is None
    assert row["event_contents"] is None
    assert row["timing"]["first_answer_ms"] is None


def test_conditioned_poisson_matches_frozen_slots_even_when_completion_order_changes():
    from validation_suite.scenarios import _case

    scenario = _case("conditioned", "E4", "poisson", seed=1, rate_rps=2,
                     active_seconds=1, request_count=None, poisson_schedule="conditioned_uniform")
    context = {"case_id": "test", "candidate": "current-python", "repetition": 1, "study_id": "test"}
    planned = [r for r in planned_rows(scenario, context) if r["phase"] == "measured"]
    assert [r["scheduled_offset_ms"] for r in planned] == pytest.approx(
        [134.36424411240122, 847.4337369372327])
    # Persisted completion order is deliberately the reverse of scheduled order.
    raw = [{"id": str(i), "scenario": "traffic_stage_0", "scheduled_offset_ms": offset,
            "status": "completed", "output": "42"}
           for i, offset in [(1, 847.4337369372327), (0, 134.36424411240122)]]
    results = normalize_results({"requests": raw}, scenario, context)
    assert [r["logical_request_id"] for r in results] == [
        "test:measured:000001", "test:measured:000000"]
    # A changed generator contract must fail, not invent correlation by row order.
    old = {**scenario, "poisson_schedule": "exponential_gaps"}
    with pytest.raises(ValueError, match="frozen Poisson"):
        normalize_results({"requests": raw}, old, context)


def test_legacy_poisson_sequence_and_unknown_contract():
    from validation_suite.scenarios import _case, poisson_offsets

    scenario = _case("legacy", "E4", "poisson", seed=1, rate_rps=2,
                     active_seconds=1, request_count=None)
    assert list(poisson_offsets(scenario)) == pytest.approx([0.0721455320547546])
    with pytest.raises(ValueError, match="Unknown Poisson"):
        list(poisson_offsets({**scenario, "poisson_schedule": "unknown"}))


def test_manifest_integrity_rejects_changed_thresholds(tmp_path, monkeypatch):
    value = {"thresholds": {"missing_identities": 0}, "suite_source_hashes": {}}
    value["frozen_sha256"] = digest(value)
    value["thresholds"]["missing_identities"] = 1
    (tmp_path / "suite-manifest.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="integrity"):
        load_frozen(tmp_path)


def test_private_results_require_ignored_repository_path(tmp_path):
    with pytest.raises(ValueError, match="ignored runs"):
        require_ignored(tmp_path)


def test_sustained_active_duration_separates_final_response_drain():
    scenario = cases()["E3-smoke-c1"]
    report = {"observations": {"targets": {"reference": {"phase_boundaries": [
        {"phase": "sustained", "event": "scenario_started", "elapsed_seconds": 1},
        {"phase": "sustained", "event": "scenario_finished", "elapsed_seconds": 4.25}],
        "sustained_completion": {"reason": "duration elapsed"}}}}}
    result = exported_phases(report, scenario, {"case_id": "test"})
    assert result["active_start_ns"] == 1_000_000_000
    assert result["active_end_ns"] == 3_000_000_000
    assert result["drain_seconds"] == 1.25


def test_offline_projection_rejects_hour_that_exceeds_frozen_evidence_bound(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"scenario": {"active_seconds": 300}}))
    for name in ("planned.jsonl", "client-events.jsonl", "receiver-events.jsonl", "results.jsonl"):
        (tmp_path / name).write_text('{}\n' * 20_000)
    (tmp_path / "product-report").mkdir()
    (tmp_path / "product-report" / "report.json").write_text('{}')
    assert projected_feasibility(tmp_path, {"active_seconds": 300})["feasible"] is True
    projection = projected_feasibility(tmp_path, {"active_seconds": 3600})
    assert projection["feasible"] is False
    assert projection["projected_rows"]["receiver-events.jsonl"] == 240_000


@pytest.mark.parametrize("identifier,seconds", [("E7-fixed_rate-1", 3600), ("E1-normal", 301)])
def test_execution_guard_rejects_before_any_subprocess_or_case_creation(tmp_path, monkeypatch, identifier, seconds):
    scenario = {**cases()[identifier], "active_seconds": seconds}
    monkeypatch.setattr("validation_suite.driver.subprocess.Popen", lambda *args, **kwargs: pytest.fail("subprocess started"))
    with pytest.raises(ValueError, match="not authorized"):
        run_case(tmp_path, {}, scenario, 1)
    with pytest.raises(ValueError, match="not authorized"):
        make_configuration(scenario, "http://127.0.0.1:1/v1")
    assert list(tmp_path.iterdir()) == []


def test_endurance_campaign_rejects_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr("validation_suite.driver.subprocess.Popen", lambda *args, **kwargs: pytest.fail("subprocess started"))
    with pytest.raises(ValueError, match="E7 is not selected"):
        campaign(tmp_path, {}, stage="endurance")


def test_product_report_stream_handles_unicode_chunk_boundaries_without_read_text(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    expected = [{"output": "café 🦒" * 20_000}, {"output": "42"}]
    path.write_text(json.dumps({"schema_version": "2", "requests": expected,
                               "overall": "pass", "observations": {"count": 2}}, ensure_ascii=False))
    original = Path.read_text

    def bounded_only(self, *args, **kwargs):
        if self == path:
            pytest.fail("whole product report read")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", bounded_only)
    metadata = {}
    assert list(iter_report_requests(path, metadata=metadata)) == expected
    assert metadata == {"overall": "pass", "observations": {"count": 2}}


@pytest.mark.parametrize("text", ['{"requests":[{}],"overall":', '{"requests":[{},]}',
                                  '{"requests":[],"requests":[]}', '{"requests":[]} true'])
def test_product_report_stream_rejects_partial_or_ambiguous_json(tmp_path, text):
    path = tmp_path / "report.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        list(iter_report_requests(path))


def _export_fixture(tmp_path, count=2):
    (tmp_path / "product-report").mkdir()
    requests = [{"scenario": "request_pack", "server_request_id": f"reference-{index}",
                 "output": "42", "status": "completed", "score": True,
                 "dispatch_ms": index, "completed_ms": index + 1} for index in range(count)]
    (tmp_path / "product-report/report.json").write_text(json.dumps({"requests": requests, "overall": "pass"}))
    (tmp_path / "results.jsonl").write_text("")
    (tmp_path / "client-events.jsonl").write_text('{"event_kind":"candidate_process_exited"}\n')
    (tmp_path / "receiver-events.jsonl").write_text("\n".join(json.dumps({
        "kind": "request_received", "monotonic_ns": 1_000_000 + index, "clock_domain": "reference-clock",
        "metadata": {"response_id": f"reference-{index}"}}) for index in range(count)) + "\n")


def test_product_export_streams_results_and_retains_existing_lifecycle_events(tmp_path):
    _export_fixture(tmp_path)
    result = export_product_outputs(tmp_path, cases()["E1-normal"], {"case_id": "case-r1"})
    assert result["export"]["request_count"] == 2
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 2
    events = [json.loads(line) for line in (tmp_path / "client-events.jsonl").read_text().splitlines()]
    assert [event["event_kind"] for event in events] == ["candidate_process_exited", "request_dispatched", "request_dispatched"]
    assert result["receiver_active_window"]["last_measured_receipt_ns"] == 1_000_001
    assert not (tmp_path / "baseline-export.sqlite").exists()


@pytest.mark.parametrize("bound", [{"max_distinct_attempt_ids": 1}, {"max_sqlite_bytes": 1}])
def test_product_export_checks_final_short_batch_before_publishing(tmp_path, monkeypatch, bound):
    _export_fixture(tmp_path)
    limits = {**verification_limits(), **bound}
    monkeypatch.setattr("validation_suite.verifier.verification_limits", lambda: limits)
    with pytest.raises(ValueError, match="attempt bound"):
        export_product_outputs(tmp_path, cases()["E1-normal"], {"case_id": "case-r1"})
    assert (tmp_path / "results.jsonl").read_text() == ""
    assert len((tmp_path / "results-export.partial.jsonl").read_text().splitlines()) == 2
    assert not (tmp_path / "baseline-export.sqlite").exists()


def test_progress_pipe_delays_real_bytes_and_drains_every_byte(tmp_path):
    read_fd, write_fd = os.pipe()
    payload = ("real progress café\n" * 30).encode()
    os.write(write_fd, payload)
    consumer = _ProgressConsumer(os.fdopen(read_fd, "rb", buffering=0), tmp_path)
    consumer.consume(0)
    consumer.consume(.5)
    consumer.consume(1)
    os.close(write_fd)
    summary = consumer.finish()
    assert (tmp_path / "stderr.log").read_bytes() == payload
    assert summary["paced_bytes"] == 128
    assert summary["post_exit_drain_bytes"] == len(payload) - 128
    assert summary["paced_reads"] == 2
    assert summary["all_stderr_retained"] is True


def test_sparse_descriptor_counts_do_not_call_socket_fds_connections(monkeypatch):
    monkeypatch.setattr("validation_suite.driver.shutil.which", lambda _: "/usr/sbin/lsof")
    monkeypatch.setattr("validation_suite.driver.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        stdout="p12\nfcwd\ntDIR\nf0\ntREG\nf3\ntIPv4\np13\nf2\ntPIPE\n", stderr="", returncode=0))
    observed = _descriptor_counts([("candidate", 12), ("driver", 13)])
    assert observed["counts"][12] == {"fd_count": 2, "network_socket_fd_count": 1}
    assert observed["counts"][13] == {"fd_count": 1, "network_socket_fd_count": 0}
    assert "not established connections" in observed["scope"]


def test_resource_descriptions_preserve_lifetime_and_separate_receipt_span(tmp_path):
    path = tmp_path / "resources.csv"
    path.write_text("monotonic_ns,role,rss_bytes,cpu_percent,fd_count,connections\n"
                    "0,candidate,1000,0,,\n1000000000,candidate,2000,1,8,2\n"
                    "2000000000,candidate,1500,1,,\n3000000000,candidate,5000,10,6,0\n")
    result = resource_summary(path, {"start_ns": 1_000_000_000, "last_measured_receipt_ns": 2_000_000_000})
    assert result["ram"]["candidate"]["growth"] == 4000
    assert result["phase_descriptions"]["receipt_span"]["ram"]["candidate"]["growth"] == -500
    assert result["phase_descriptions"]["after_last_receipt"]["ram"]["candidate"]["peak_sample"] == 5000
    assert result["descriptor_observations"]["roles"]["candidate"]["fd_max"] == 8


def test_frozen_remediation_runs_all_missing_short_profiles_even_after_failures(tmp_path, monkeypatch):
    selected = cases()
    names = [f"E3-qualification-c{n}" for n in [50, 100, 200, 256]] + [f"E4-qualification-r{n}" for n in [10, 50, 100, 200]]
    source = tmp_path / "source"
    source.mkdir()
    rows = [{"case_id": name, "repetition": repetition, "scenario": selected[name]}
            for name in [*names, "E7-fixed_rate-10"] for repetition in range(1, selected[name]["repetitions"] + 1)]
    (source / "unrun-selection-original.json").write_text(json.dumps({"rows": rows}))
    (source / "suite-manifest.json").write_text(json.dumps({"frozen_sha256": "original"}))
    monkeypatch.setattr("validation_suite.driver.require_ignored", lambda _: None)
    monkeypatch.setattr("validation_suite.driver.baseline_snapshot", lambda *args: {
        "status_porcelain": "", "sha": "pinned", "checkout": str(tmp_path), "python_executable": "python"})
    monkeypatch.setattr("validation_suite.driver.suite_source_hashes", lambda: {"timing_observer.py": "frozen-observer"})
    study = tmp_path / "new"
    suite = freeze(study, tmp_path, "python", source_study=source)
    assert suite["resource_policy"]["offline_verifier"] == verification_limits()
    assert resource_policy()["offline_verifier"] == verification_limits()
    for item in suite["execution_selection"]["remediation"]["cases"]:
        directory = Path(item["source_case"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text('{}')
    launched = []
    monkeypatch.setattr("validation_suite.driver.projected_feasibility", lambda *args, **kwargs: {"feasible": True})
    monkeypatch.setattr("validation_suite.driver.write_summary", lambda *args: None)
    monkeypatch.setattr("validation_suite.driver.run_case", lambda study, suite, case, repetition, **kwargs:
                        launched.append((case["id"], repetition)) or {"verdicts": {"requested_load_delivered": "fail"}})
    campaign(study, suite, stage="remediation")
    assert {(name, repetition) for name in names for repetition in [1, 2, 3]} <= set(launched)
    assert len(launched) == 28  # 24 missing repetitions and two matched progress/control pairs.
    assert not any(name.startswith("E7") for name, _ in launched)
    observed = freeze(tmp_path / "observed", tmp_path, "python", source_study=source, observe_missing=True)
    selection = observed["execution_selection"]["remediation"]
    schedule = selection["observer_calibration_schedule"]
    assert len(schedule) == 48
    assert [item["observer_enabled"] for item in schedule[:6]] == [False, True, True, False, False, True]
    assert all(case["active_seconds"] == 10 for case in observed["cases"] if case["tier"] == "observer_calibration")
    assert sum(len(item["repetitions"]) for item in selection["cases"] if item.get("observer_enabled")) == 24


def test_product_decoder_rejects_an_oversized_request_value(tmp_path, monkeypatch):
    from validation_suite.scenarios import RESOURCE_POLICY

    monkeypatch.setitem(RESOURCE_POLICY, "product_report_decoder", {"request_value_bytes": 32, "metadata_value_bytes": 1024})
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"requests": [{"output": "x" * 64}]}))
    with pytest.raises(ValueError, match="bounded decoder"):
        list(iter_report_requests(path))


def test_disk_accounting_does_not_duplicate_referenced_original_evidence(tmp_path):
    source = tmp_path / "original"
    source.write_bytes(b"evidence" * 1000)
    destination = tmp_path / "new"
    destination.mkdir()
    link = destination / "read-only-evidence"
    link.symlink_to(source)
    assert storage_bytes(destination) == link.lstat().st_size
    assert storage_bytes(destination) < source.stat().st_size


def test_plan_discloses_current_endurance_exclusions(capsys):
    assert main(["plan", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution_policy"]["maximum_active_seconds"] == 300
    assert set(plan["execution_exclusions"]) == {"E7-fixed_rate-1", "E7-fixed_rate-10"}
    assert main(["plan"]) == 0
    assert all("EXCLUDED:" in line for line in capsys.readouterr().out.splitlines() if line.startswith("E7-"))


def test_missing_html_is_separate_from_surviving_json(tmp_path):
    product = tmp_path / "product-report"
    product.mkdir()
    (product / "report.json").write_text('{"requests":[]}')
    manifest = {"artifact_absences": {}}
    _record_missing_html(tmp_path, manifest)
    assert set(manifest["artifact_absences"]) == {"product-report/report.html"}
    assert (product / "report.json").read_text() == '{"requests":[]}'
    (product / "report.html").write_text("saved HTML")
    complete = {"artifact_absences": {}}
    _record_missing_html(tmp_path, complete)
    assert complete["artifact_absences"] == {}


def _explicit_plan(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = cases()["E4-smoke-r1"]
    manifest = {"cases": [original]}
    manifest["frozen_sha256"] = digest(manifest)
    (source / "suite-manifest.json").write_text(json.dumps(manifest))
    source_case = source / "old-case"
    source_case.mkdir()
    (source_case / "manifest.json").write_text('{}')
    scenario = {**original, "id": "E4-new-unobserved-smoke"}
    value = {"schema_version": 1, "source_study": "source",
             "source_frozen_sha256": manifest["frozen_sha256"], "scenarios": [scenario],
             "cases": [{"case_id": scenario["id"], "repetitions": [1], "source_case": "source/old-case"}]}
    path = tmp_path / "execution-plan.json"
    path.write_text(json.dumps(value))
    return path, value


def test_explicit_plan_uses_fresh_identities_and_resolves_sources_from_plan(tmp_path, monkeypatch):
    path, value = _explicit_plan(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    selected, plan = reviewed_execution_plan(path)
    assert selected == value["scenarios"]
    assert plan["source_study"] == str(tmp_path / "source")
    assert plan["cases"][0]["source_case"] == str(tmp_path / "source/old-case")
    value["scenarios"][0]["id"] = "E4-smoke-r1"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="fresh case identities"):
        reviewed_execution_plan(path)


@pytest.mark.parametrize("change", ["bool_repetition", "missing_source", "missing_case", "bad_schedule", "long_case"])
def test_explicit_plan_rejects_bad_selection_before_any_subprocess(tmp_path, monkeypatch, change):
    path, value = _explicit_plan(tmp_path)
    if change == "bool_repetition":
        value["cases"][0]["repetitions"] = [True]
    elif change == "missing_source":
        del value["cases"][0]["source_case"]
    elif change == "missing_case":
        del value["cases"][0]["case_id"]
    elif change == "bad_schedule":
        value["observer_calibration_schedule"] = None
    else:
        value["scenarios"][0]["active_seconds"] = 1800
    path.write_text(json.dumps(value))
    monkeypatch.setattr("validation_suite.driver.subprocess.Popen", lambda *args, **kwargs: pytest.fail("subprocess started"))
    with pytest.raises(ValueError):
        reviewed_execution_plan(path)


def test_explicit_plan_can_project_from_a_preceding_new_case_but_not_a_future_one(tmp_path):
    path, value = _explicit_plan(tmp_path)
    second = {**value["scenarios"][0], "id": "E4-second-case"}
    value["scenarios"].append(second)
    value["cases"].append({"case_id": second["id"], "repetitions": [1],
                           "source_case": "current-python/E4-new-unobserved-smoke-r1"})
    path.write_text(json.dumps(value))
    assert len(reviewed_execution_plan(path)[0]) == 2
    value["cases"].reverse()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="projection source is unavailable"):
        reviewed_execution_plan(path)


def test_setup_failure_stops_before_the_next_reviewed_case(tmp_path, monkeypatch):
    first = {**cases()["E4-smoke-r1"], "id": "E4-setup"}
    second = {**first, "id": "E4-following"}
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text('{}')
    suite = {"cases": [first, second], "baseline": {"checkout": str(tmp_path), "python_executable": "python"},
             "execution_selection": {"remediation": {"cases": [
                 {"case_id": first["id"], "repetitions": [1], "source_case": str(source), "gate_after": "healthy_setup"},
                 {"case_id": second["id"], "repetitions": [1], "source_case": str(source)}]}}}
    launched = []
    monkeypatch.setattr("validation_suite.driver.projected_feasibility", lambda *args, **kwargs: {"feasible": True})
    monkeypatch.setattr("validation_suite.driver.write_summary", lambda *args: None)
    monkeypatch.setattr("validation_suite.driver.baseline_snapshot", lambda *args: {})
    monkeypatch.setattr("validation_suite.driver.run_case", lambda study, suite, case, repetition, **kwargs:
        launched.append(case["id"]) or {"execution_status": "completed", "verdicts": {
            "reference_test_valid": "yes", "response_accounting_correctness": "pass",
            "evaluation_correctness": "pass", "requested_load_delivered": "fail"}})
    campaign(tmp_path, suite, stage="remediation")
    assert launched == [first["id"]]
    assert json.loads((tmp_path / "campaign-status.json").read_text())["status"] == "setup_blocked"


def test_calibration_stage_exits_for_review_and_keeps_overhead_failure_separate(tmp_path, monkeypatch):
    base = cases()["E1-paced-5ms"]
    controls = [{**base, "id": "E1-new-" + state, "repetitions": 3} for state in ["unobserved", "observed"]]
    following = {**cases()["E4-smoke-r1"], "id": "E4-following"}
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text('{}')
    schedule = [{"case_id": case["id"], "repetition": repetition, "observer_enabled": "-unobserved" not in case["id"],
                 "timing_profile": "E1-paced-5ms", "source_case": str(source)}
                for repetition in [1, 2, 3] for case in controls]
    suite = {"cases": [*controls, following], "baseline": {"checkout": str(tmp_path), "python_executable": "python", "sha": "pinned"},
             "observation_policy": {"observer_source_sha256": "frozen", "observer_version": "v2", "uncertainty_budget_ns": 100000, "scope": "controls"},
             "execution_selection": {"remediation": {"observer_calibration_schedule": schedule,
                 "require_calibration_integrity": True, "require_calibration_pass": False,
                 "cases": [{"case_id": following["id"], "repetitions": [1], "source_case": str(source)}]}}}
    launched = []
    monkeypatch.setattr("validation_suite.driver.projected_feasibility", lambda *args, **kwargs: {"feasible": True})
    monkeypatch.setattr("validation_suite.driver.write_summary", lambda *args: None)
    monkeypatch.setattr("validation_suite.driver.baseline_snapshot", lambda *args: {})
    monkeypatch.setattr("validation_suite.driver.run_case", lambda study, suite, case, repetition, **kwargs:
        launched.append((case["id"], repetition)) or {"execution_status": "completed", "verdicts": {
            "reference_test_valid": "yes", "response_accounting_correctness": "pass", "evaluation_correctness": "pass"}})

    def fail_overhead(profile, pairs, output, **kwargs):
        assert len(pairs) == 3
        assert kwargs["pace_ms"] == 5
        output.write_text(json.dumps({"profiles": {profile: {"overhead_gate_pass": False}}}))

    monkeypatch.setattr("validation_suite.timing_analysis.analyze_overhead_pairs", fail_overhead)
    campaign(tmp_path, suite, stage="calibration")
    assert len(launched) == 6
    assert not any(name == following["id"] for name, _ in launched)
    assert json.loads((tmp_path / "campaign-status.json").read_text())["status"] == "setup_review_required"


def test_periodic_burst_window_uses_exported_schedule_not_last_completion():
    from validation_suite.baseline import exported_phases
    report = {'validation_arrival_origin_ms': 125.0,
              'observations': {'targets': {'reference': {'arrivals': {'issued': 75000}}}}}
    scenario = {'mode': 'periodic_batch', 'active_seconds': 300}
    phase = exported_phases(report, scenario, {'case_id': 'burst'})
    assert phase['active_start_ns'] == 125_000_000
    assert phase['active_end_ns'] == 300_125_000_000
    assert phase['arrival_schedule']['issued'] == 75000
    assert 'not an observed process lifetime' in phase['active_end_boundary']
