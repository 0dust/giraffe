import copy
import json

import jsonschema
import pytest

from validation_suite.reporting import (SECTIONS, get_metric, render_markdown, render_summary,
                                        validate_report)
from validation_suite.verifier import verify_case
from tests.test_validation_verifier import known_case


def test_every_exact_section_and_label_once_and_ordered(tmp_path):
    known_case(tmp_path)
    report = verify_case(tmp_path)
    markdown = render_markdown(report)
    previous = -1
    for section in SECTIONS:
        position = markdown.index(f"## {section['title']}\n\n")
        assert position > previous
        previous = position
        for field in section["fields"]:
            label = f"- {field['label']}:"
            assert markdown.count(label) == 1
            position = markdown.index(label)
            assert position > previous
            previous = position
    assert len([field for section in SECTIONS for field in section["fields"]]) == 99
    assert json.loads((tmp_path / "report.json").read_text()) == report
    assert (tmp_path / "report.md").read_text() == markdown


@pytest.mark.parametrize("mutation", ["extra_field", "missing_field", "empty_evidence", "missing_reason",
                                      "bad_state", "pass_missing", "distribution_missing"])
def test_schema_rejects_missing_fields_evidence_and_unqualified_passes(tmp_path, mutation):
    known_case(tmp_path)
    report = copy.deepcopy(verify_case(tmp_path))
    item = get_metric(report, "Requests planned")
    if mutation == "extra_field":
        item["invented"] = 1
    elif mutation == "missing_field":
        item.pop("population")
    elif mutation == "empty_evidence":
        item["evidence"] = []
    elif mutation == "missing_reason":
        item["reason"] = ""
    elif mutation == "bad_state":
        item["state"] = "available"
    elif mutation == "pass_missing":
        item["state"] = "not_measured"
    else:
        item["value"] = {"sample_count": 1, "percentile_method": "nearest_rank"}
    with pytest.raises(jsonschema.ValidationError):
        validate_report(report)


def test_verdict_mismatch_is_rejected_before_markdown(tmp_path):
    known_case(tmp_path)
    report = verify_case(tmp_path)
    report["verdicts"]["overall_experiment_result"] = "fail"
    with pytest.raises(ValueError, match="Final verdict differs"):
        render_markdown(report)


def test_exact_summary_headers_and_links(tmp_path):
    known_case(tmp_path)
    report = verify_case(tmp_path)
    summary = render_summary({"study_id": "live-study", "baseline_sha": "abc", "suite_version": "1", "criteria_version": "1"}, [report])
    assert summary.startswith("# Giraffe reliability validation — live-study\n")
    assert "| Candidate | Case / repeat | Experiment | Requested | Observed delivery | Response/accounting correctness | Timing qualification | Reference valid | Result |" in summary
    assert "[known/1](control/known/report.md)" in summary
    assert "2 requests" in summary
    assert "scenario:" not in summary


@pytest.mark.parametrize("status", ["aborted", "blocked", "not_run"])
def test_incomplete_and_unavailable_runs_never_pass(tmp_path, status):
    manifest = known_case(tmp_path)
    manifest["execution_status"] = status
    report = verify_case(tmp_path, manifest)
    assert report["verdicts"]["overall_experiment_result"] != "pass"
    render_markdown(report)
