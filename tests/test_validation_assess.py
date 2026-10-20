"""Offline assessment preserves its execution inputs and rejects unrelated changes."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.test_validation_verifier import known_case
from validation_suite import assess
from validation_suite.reporting import get_metric, render_markdown, set_metric
from validation_suite.verifier import verify_case


def _study(tmp_path):
    study = tmp_path / "study"
    source = study / "current-python" / "known"
    manifest = known_case(source)
    manifest.update(candidate="current-python", study_id="study", suite_version="0.1.1",
                    criteria_version="fixed", suite_frozen_sha256="frozen")
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "stdout.log").write_text("retained execution output\n")
    verify_case(source, manifest)
    archive = study / "frozen-source-v0.1.1"
    archive.mkdir()
    (archive / "verifier.py").write_text("preserved frozen source\n")
    frozen = {"study_id": "study", "suite_version": "0.1.1", "frozen_sha256": "frozen",
              "criteria_version": "fixed", "thresholds": manifest["thresholds"],
              "suite_source_hashes": {"verifier.py": assess.file_hash(archive / "verifier.py")}}
    (study / "suite-manifest.json").write_text(json.dumps(frozen))
    selection = {"original_report_count": 1, "frozen_repetitions": 2,
                 "rows": [{"case_id": "E4-unrun", "report_case_id": "E4-unrun-r1", "repetition": 1,
                           "case_directory": "current-python/E4-unrun-r1", "execution_status": "not_run",
                           "reason": "Frozen prerequisite did not qualify.", "evidence": ["campaign-decisions.json"],
                           "scenario": {"id": "E4-unrun", "experiment_id": "E4", "mode": "fixed_rate", "rate_rps": 1}}]}
    (study / "unrun-selection-original.json").write_text(json.dumps(selection))
    return study, source


def test_separate_assessment_has_original_hashes_links_exact_reports_and_no_fake_not_run_counts(tmp_path):
    study, source = _study(tmp_path)
    original = assess.input_snapshot(study)
    result = assess.assess_study(study)
    assert result["derived_reports_validated"] == 2 and result["not_run_reports"] == 1
    assert result["input_bytes_unchanged"] is True and result["per_case_invariants"] is True
    assert assess.input_snapshot(study) == original
    output = Path(result["output"])
    assert (output / "current-python/known/planned.jsonl").resolve() == source / "planned.jsonl"
    assert (output / "current-python/known/original-report.json").resolve() == source / "report.json"
    provenance = json.loads((output / "current-python/known/assessment.json").read_text())
    assert provenance["execution_suite_version"] == "0.1.1"
    assert provenance["assessor_version"] == "0.3.0"
    for name in ("timing_analysis.py", "scenarios.py", "baseline.py", "timing_observer.py", "verifier.py"):
        archived = Path(provenance["source_archive"]) / name
        assert archived.is_file() and assess.file_hash(archived) == provenance["assessor_source_hashes"][name]
    assert provenance["original_input_sha256"]["current-python/known/stdout.log"] == hashlib.sha256(b"retained execution output\n").hexdigest()
    assert provenance["source_inputs"]["planned.jsonl"] == str(source / "planned.jsonl")
    assert Path(provenance["input_hashes"]).is_file()
    not_run = json.loads((output / "current-python/E4-unrun-r1/report.json").read_text())
    assert not_run["execution_status"] == "not_run"
    assert not_run["verdicts"]["overall_experiment_result"] == "not run"
    assert get_metric(not_run, "Requests planned")["value"] is None
    assert "Frozen prerequisite" in get_metric(not_run, "Requests planned")["reason"]
    assert (output / "current-python/E4-unrun-r1/report.md").read_text() == render_markdown(not_run)
    assert not (study / "current-python/E4-unrun-r1").exists()
    with pytest.raises(FileExistsError):
        assess.assess_study(study)


def test_frozen_source_archive_must_match_before_assessment(tmp_path):
    study, _ = _study(tmp_path)
    (study / "frozen-source-v0.1.1/verifier.py").write_text("changed source\n")
    with pytest.raises(ValueError, match="Frozen source archive hash mismatch"):
        assess.assess_study(study)
    assert not (study / "offline-assessments/0.3.0").exists()


def test_bounded_evidence_null_issues_remain_unmeasured_during_assessment(tmp_path, monkeypatch):
    from validation_suite import verifier

    study, source = _study(tmp_path)
    monkeypatch.setattr(verifier, "MAX_ATTEMPT_BYTES", 1)
    verify_case(source)
    result = assess.assess_study(study)
    report = json.loads((Path(result["output"]) / "current-python/known/report.json").read_text())
    assert get_metric(report, "Unreconciled response differences")["value"] is None
    assert get_metric(report, "Warm-up, probe, reference and retry attempt counts")["value"] is None
    assert result["input_bytes_unchanged"] is True


def test_unrelated_delivery_change_is_rejected(tmp_path, monkeypatch):
    study, _ = _study(tmp_path)
    actual = assess.verify_case

    def changed(*args, **kwargs):
        report = actual(*args, **kwargs)
        report["verdicts"]["requested_load_delivered"] = "fail"
        item = get_metric(report, "requested_load_delivered")
        item["value"], item["verdict"] = "fail", "fail"
        set_metric(report, "requested_load_delivered", item)
        return report

    monkeypatch.setattr(assess, "verify_case", changed)
    with pytest.raises(ValueError, match="Delivery/timing/selection verdict changed"):
        assess.assess_study(study)


def test_changed_original_input_is_detected_after_assessment(tmp_path, monkeypatch):
    study, source = _study(tmp_path)
    actual = assess.verify_case

    def changed(*args, **kwargs):
        report = actual(*args, **kwargs)
        (source / "stdout.log").write_text("deliberate test corruption\n")
        return report

    monkeypatch.setattr(assess, "verify_case", changed)
    with pytest.raises(ValueError, match="Original inputs changed"):
        assess.assess_study(study)
    proof = json.loads((study / "offline-assessments/0.3.0/immutability-proof.json").read_text())
    assert proof["input_bytes_unchanged"] is False


def test_previously_bounded_saved_evidence_is_recovered_in_new_output(tmp_path, monkeypatch):
    from validation_suite import verifier

    study, source = _study(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(verifier, "MAX_COMPACT_BYTES", 1)
        verifier.verify_case(source)
    result = assess.assess_study(study)
    output = Path(result["output"]) / "current-python/known"
    diff = json.loads((output / "original-v-derived.json").read_text())
    report = json.loads((output / "report.json").read_text())
    assert diff["previously_bounded_evidence_recovered"] is True
    assert get_metric(report, "Requests confirmed received by endpoint")["value"] == 2
    assert report["verdicts"]["response_accounting_correctness"] == "pass"
    assert result["input_bytes_unchanged"] is True


def test_confirmed_refusal_reassessment_changes_only_approved_fault_scope(tmp_path, monkeypatch):
    from tests.test_validation_verifier import _refused_connection_control
    from validation_suite import verifier

    study, source = _study(tmp_path)
    manifest = _refused_connection_control(source)
    manifest.update(candidate="current-python", study_id="study", suite_version="0.1.1",
                    criteria_version="fixed", suite_frozen_sha256="frozen")
    (source / "manifest.json").write_text(json.dumps(manifest))
    actual_fault = verifier._fault_behavior
    with monkeypatch.context() as patch:
        patch.setattr(verifier, "_refusal_injected", lambda scenario, client: False)
        patch.setattr(verifier, "_fault_behavior", lambda *args: ("inconclusive", actual_fault(*args)[1]))
        verifier.verify_case(source)
    result = assess.assess_study(study)
    report = json.loads((Path(result["output"]) / "current-python/known/report.json").read_text())
    assert report["verdicts"]["requested_load_delivered"] == "fail"
    assert report["verdicts"]["expected_fault_behavior"] == "pass"
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    assert result["input_bytes_unchanged"] is True


def test_derived_timing_output_is_regenerated_without_linking_original(tmp_path, monkeypatch):
    study, source = _study(tmp_path)
    original = b'{"preserved_original_assessment":true}\n'
    (source / "timing-verification.json").write_bytes(original)
    actual = assess.verify_case

    def produce_timing_output(case_dir, *args, **kwargs):
        report = actual(case_dir, *args, **kwargs)
        (Path(case_dir) / "timing-verification.json").write_text('{"new_derived_assessment":true}\n')
        return report

    monkeypatch.setattr(assess, "verify_case", produce_timing_output)
    result = assess.assess_study(study)
    derived = Path(result["output"]) / "current-python/known/timing-verification.json"
    assert derived.is_file() and not derived.is_symlink()
    assert json.loads(derived.read_text()) == {"new_derived_assessment": True}
    assert (source / "timing-verification.json").read_bytes() == original
    assert result["input_bytes_unchanged"] is True


def test_remediation_archive_selection_and_user_stop_are_preserved(tmp_path):
    import shutil

    study, source = _study(tmp_path)
    frozen = json.loads((study / "suite-manifest.json").read_text())
    frozen["suite_version"] = "0.2.0"
    target = study / "frozen-source/validation_suite"
    target.mkdir(parents=True)
    shutil.move(study / "frozen-source-v0.1.1/verifier.py", target / "verifier.py")
    (study / "suite-manifest.json").write_text(json.dumps(frozen))
    selection = json.loads((study / "unrun-selection-original.json").read_text())
    row = selection["rows"][0]
    row.update(execution_status="user_stopped", report_execution_status="aborted",
               reason="User explicitly stopped this partial execution.", selection_details={"partial_raw_inputs_preserved": True})
    (study / "assessment-selection-original.json").write_text(json.dumps(selection))
    (study / "unrun-selection-original.json").unlink()
    partial = study / row["case_directory"]
    partial.mkdir()
    (partial / "partial.jsonl").write_text('{"preserved":"partial"}\n')
    original = assess.input_snapshot(study)
    result = assess.assess_study(study)
    assert result["execution_status_counts"] == {"completed": 1, "aborted": 1}
    assert assess.input_snapshot(study) == original
    case = Path(result["output"]) / row["case_directory"]
    report = json.loads((case / "report.json").read_text())
    assert report["execution_status"] == "aborted"
    assert report["manifest"]["not_run_selection"]["execution_status"] == "user_stopped"
    assert report["verdicts"]["overall_experiment_result"] == "inconclusive"
    assert get_metric(report, "Requests planned")["value"] is None
    assert (case / "original-partial-evidence/partial.jsonl").resolve() == partial / "partial.jsonl"
    assert not (partial / "report.json").exists() and (source / "report.json").exists()


def test_prior_assessment_symlink_target_content_is_part_of_input_proof(tmp_path):
    study, _ = _study(tmp_path)
    prior = study / "offline-assessments/0.2.0"
    prior.mkdir(parents=True)
    external = tmp_path / "external-retained-input.txt"
    external.write_text("before")
    (prior / "retained-input.txt").symlink_to(external)
    before = assess.input_snapshot(study)
    external.write_text("after")
    after = assess.input_snapshot(study)
    assert before["offline-assessments/0.2.0/retained-input.txt"] != after["offline-assessments/0.2.0/retained-input.txt"]


@pytest.mark.parametrize("timing_shift", [0, 5])
def test_source_identity_correction_can_qualify_or_expose_timing_failure(tmp_path, monkeypatch, timing_shift):
    from tests.test_validation_verifier import _observed_timing_control
    from validation_suite import verifier

    study, source = _study(tmp_path)
    manifest = _observed_timing_control(source)
    manifest.update(candidate="current-python", study_id="study", suite_version="0.1.1",
                    criteria_version="fixed", suite_frozen_sha256="frozen")
    if timing_shift:
        from tests.test_validation_verifier import rows, write_rows
        records = rows(source, "results")
        records[0]["timing"]["first_answer_ms"] += timing_shift
        write_rows(source / "results.jsonl", records)
    (source / "manifest.json").write_text(json.dumps(manifest))
    with monkeypatch.context() as patch:
        patch.setattr(verifier, "validate_client_source_identity", lambda *args: {"valid": False, "issues": ["old missing manifest field"]})
        old = verify_case(source, manifest)
    assert old["verdicts"]["measurement_accuracy"] == "unreliable"
    result = assess.assess_study(study)
    report = json.loads((Path(result["output"]) / "current-python/known/report.json").read_text())
    assert report["verdicts"]["measurement_accuracy"] == ("unreliable" if timing_shift else "qualified")
    assert report["verdicts"]["requested_load_delivered"] == old["verdicts"]["requested_load_delivered"]
    assert report["verdicts"]["capacity_contribution"] == ("disqualifies" if timing_shift else "inconclusive")
    assert report["verdicts"]["overall_experiment_result"] == ("fail" if timing_shift else "pass")
    assert result["input_bytes_unchanged"] is True
