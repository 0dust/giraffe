"""Saved character estimates cannot reconstruct server-count boundaries."""

import pytest

from giraffe.models import RunReport
from tests.test_performance import record, report, summary


def legacy_record(**changes):
    values = dict(timing_schema=0, stream=True, output_tokens=10,
                  completion_tokens=10, reasoning_tokens=0, answer_chars=100,
                  first_read_answer_chars=10, answer_reads=3,
                  first_output_ms=100, last_output_ms=900)
    return record(**values).model_copy(update=changes)


def test_saved_character_estimate_does_not_reconstruct_pace_or_reciprocal():
    saved = report([legacy_record()]).model_dump(exclude={"performance"})
    restored = RunReport.model_validate(saved)
    metrics = summary(restored)["metrics"]
    for key in ("generation_pace", "tpot_estimate", "tpot"):
        metric = metrics[key]
        assert metric["value"] is None
        assert metric["counts"] == {"sample": 0, "eligible": 1, "missing": 1}
    for key in ("tpot_direct", "itl", "client_ttft", "answer_read_gap"):
        assert metrics[key]["value"] is None
        assert metrics[key]["state"] != "measured"
    assert restored.performance["targets"][0]["request_series"][0]["values"]["tpot_estimate_ms"] is None


@pytest.mark.parametrize("changes", [{"stream": False}, {"valid": False},
    {"status": "timeout"}, {"answer_reads": 1}, {"last_output_ms": None}])
def test_reported_boundaries_keep_existing_generation_eligibility(changes):
    sample = legacy_record(timing_schema=1, first_read_output_tokens=1,
        last_read_output_tokens=10, **changes)
    metrics = summary(report([sample]))["metrics"]
    for key in ("generation_pace", "tpot_estimate", "tpot"):
        assert metrics[key]["value"] is None
        assert metrics[key]["counts"] == {"sample": 0, "eligible": 1, "missing": 1}


def test_mixed_current_and_saved_records_report_partial_coverage():
    current = legacy_record(timing_schema=1, first_read_output_tokens=1, last_read_output_tokens=10)
    metrics = summary(report([current, legacy_record(id="old")]))["metrics"]
    for key in ("generation_pace", "tpot_estimate", "tpot"):
        assert metrics[key]["counts"] == {"sample": 1, "eligible": 2, "missing": 1}
        assert "older run" not in metrics[key]["source"]


def test_reported_boundaries_establish_pace_and_reciprocal():
    sample = legacy_record(timing_schema=1, first_read_output_tokens=1, last_read_output_tokens=10)
    metrics = summary(report([sample]))["metrics"]
    assert metrics["generation_pace"]["value"] == pytest.approx(11.25)
    assert metrics["tpot_estimate"]["value"] == pytest.approx(1000 / 11.25)
    assert "older run" not in metrics["generation_pace"]["source"]
    assert "older run" not in metrics["tpot_estimate"]["source"]
