"""Saved generation estimates remain useful without new token-event evidence."""

import pytest

from giraffe.models import RunReport
from tests.test_performance import record, report, summary


def legacy_record(**changes):
    values = dict(timing_schema=0, stream=True, output_tokens=10,
                  completion_tokens=10, reasoning_tokens=0, answer_chars=100,
                  first_read_answer_chars=10, answer_reads=3,
                  first_output_ms=100, last_output_ms=900)
    return record(**values).model_copy(update=changes)


def test_saved_generation_estimate_keeps_pace_reciprocal_and_coverage():
    # Round-trip the saved-report fields rather than injecting new timing evidence.
    saved = report([legacy_record()]).model_dump(exclude={"performance"})
    restored = RunReport.model_validate(saved)
    metrics = summary(restored)["metrics"]
    for key, expected, unit in (("generation_pace", 11.25, "tok/s"),
                                ("tpot_estimate", 1000 / 11.25, "ms/token"),
                                ("tpot", 1000 / 11.25, "ms/token")):
        metric = metrics[key]
        assert metric["value"] == pytest.approx(expected)
        assert metric["unit"] == unit
        assert metric["state"] == "estimated"
        assert "from saved generation-rate estimate; older run" in metric["source"]
        assert metric["counts"] == {"sample": 1, "eligible": 1, "missing": 0}
    for key in ("tpot_direct", "itl", "client_ttft", "answer_read_gap"):
        assert metrics[key]["value"] is None
        assert metrics[key]["state"] != "measured"
    assert restored.performance["targets"][0]["request_series"][0]["values"]["tpot_estimate_ms"] == pytest.approx(1000 / 11.25)


@pytest.mark.parametrize("changes", [{"stream": False}, {"valid": False},
    {"status": "timeout"}, {"answer_reads": 1}, {"last_output_ms": None}])
def test_legacy_estimates_keep_existing_generation_eligibility(changes):
    metrics = summary(report([legacy_record(**changes)]))["metrics"]
    for key in ("generation_pace", "tpot_estimate", "tpot"):
        assert metrics[key]["value"] is None
        assert metrics[key]["counts"] == {"sample": 0, "eligible": 1, "missing": 1}


def test_mixed_saved_generation_estimates_report_partial_coverage():
    metrics = summary(report([legacy_record(), record("without-rate")]))["metrics"]
    for key in ("generation_pace", "tpot_estimate", "tpot"):
        assert metrics[key]["counts"] == {"sample": 1, "eligible": 2, "missing": 1}
        assert "from saved generation-rate estimate; older run" in metrics[key]["source"]


def test_new_timing_estimates_keep_their_existing_provenance():
    metrics = summary(report([legacy_record(timing_schema=1)]))["metrics"]
    assert metrics["generation_pace"]["value"] == pytest.approx(11.25)
    assert metrics["tpot_estimate"]["value"] == pytest.approx(1000 / 11.25)
    assert "older run" not in metrics["generation_pace"]["source"]
    assert "older run" not in metrics["tpot_estimate"]["source"]
