import pytest

from giraffe.models import CheckResult, EvaluationEvidence, Limits, RequestRecord
from giraffe.runner import _verdict_summary
from tests.test_runner import config


ANSWER_REMEDY = ("check the model's answers for the listed fixtures (the received text above); "
                 "a refusal or wrong answer is a model/prompt issue, not a serving slowdown; "
                 "rerun after changing the model or its settings")


@pytest.mark.parametrize("check_id", ["fairness", "mixed", "buckets", "token_stress"])
@pytest.mark.parametrize("cause", ["answers", "timing", "both"])
def test_concurrent_remedy_follows_structured_conditions(check_id, cause):
    answers, timing = cause != "timing", cause != "answers"
    record = RequestRecord(id="request", target="service", fixture_id="long.prefill.v2",
        scenario="fairness_short" if check_id == "fairness" else check_id,
        check_ids=[check_id], started_at="now", status="completed", valid=True,
        score=not answers, output="I cannot answer." if answers else "33692F",
        elapsed_ms=200 if timing else 20, max_stream_gap_ms=200 if timing else 2,
        evaluation=EvaluationEvidence(messages=[], scorer="exact", expected_provided=True, expected="33692F"))
    quality = {"status": "qualified", "affected_metrics": {}}
    metrics = {"accuracy": 0 if answers else 1, "timing_quality": quality}
    row = {"timing_violations": [record.id] if timing else [], "timing_quality": quality}
    if check_id in {"mixed", "buckets"}:
        metrics.update(row)
    elif check_id == "token_stress":
        metrics["cases"] = {"long_input_summary": {"levels": {"2": row}}}
    check = CheckResult(id=check_id, target="service", title=check_id, status="fail",
        summary="Configured timing limits exceeded." if answers else "Answer checks failed.",
        metrics=metrics, evidence_ids=[record.id])
    before = check.model_dump()
    summary = _verdict_summary(check, config(limits=Limits(
        latency_ms=100, stream_gap_ms=50, min_samples=2, fairness_max_ratio=3)), [record], "Raise budgets")
    remedy = summary.split("To conclude, ")[-1]
    assert (ANSWER_REMEDY in remedy) == answers
    assert ("address the slowdown" in remedy) == timing
    if answers:
        assert "long.prefill.v2" in summary
        assert 'Expected: "33692F"; received: "I cannot answer."' in summary
    if cause == "both":
        assert remedy.index(ANSWER_REMEDY) < remedy.index("address the slowdown")
    assert check.model_dump() == before


@pytest.mark.parametrize("check_id", ["fairness", "mixed", "buckets", "token_stress"])
@pytest.mark.parametrize("independent_violation", [False, True])
def test_concurrent_remedy_preserves_timing_attribution(check_id, independent_violation):
    record = RequestRecord(id="answer", target="service", fixture_id="fixture",
        scenario="fairness_short" if check_id == "fairness" else check_id,
        check_ids=[check_id], started_at="now", status="completed", valid=True, score=False,
        elapsed_ms=200, max_stream_gap_ms=200)
    quality = {"status": "client_delayed", "affected_metrics": {
        "latency_ms": [record.id], "max_stream_gap_ms": [] if independent_violation else [record.id]}}
    row = {"timing_quality": quality, "timing_violations": [record.id],
           "qualified_timing_violations": [record.id] if independent_violation else []}
    metrics = {"accuracy": 0, **row}
    if check_id == "token_stress":
        metrics = {"cases": {"long_input_summary": {"levels": {"2": row}}}}
    check = CheckResult(id=check_id, target="service", title=check_id,
        status="fail", summary="Answer fixture rejected.", metrics=metrics)
    summary = _verdict_summary(check, config(limits=Limits(
        latency_ms=100, stream_gap_ms=50, min_samples=2)), [record], "Raise budgets")
    assert ANSWER_REMEDY in summary
    assert ("address the slowdown" in summary) == independent_violation


@pytest.mark.parametrize("check_id", ["fairness", "mixed"])
def test_concurrent_ratio_remedy_uses_qualified_comparison(check_id):
    metrics = {"latency_ratio": 4, "timing_quality": {"status": "qualified"},
               "overlaps": 2, "short_only": {"attempted": 2}}
    if check_id == "mixed":
        metrics = {"comparisons": [{**metrics, "samples_and_overlap_qualified": True}]}
    check = CheckResult(id=check_id, target="service", title=check_id,
        status="fail", summary="Answer checks failed.", metrics=metrics)
    summary = _verdict_summary(check, config(limits=Limits(fairness_max_ratio=3)), [], "Raise budgets")
    assert "address the slowdown" in summary and ANSWER_REMEDY not in summary


@pytest.mark.parametrize("check_id", ["fairness", "mixed"])
def test_unqualified_concurrent_ratio_does_not_add_slowdown_remedy(check_id):
    metrics = {"accuracy": 0, "latency_ratio": 4, "overlaps": 0,
               "short_only": {"attempted": 2}, "timing_quality": {"status": "qualified"}}
    if check_id == "mixed":
        metrics = {"comparisons": [{**metrics, "samples_and_overlap_qualified": False}]}
    record = RequestRecord(id="answer", target="service", fixture_id="fixture",
        scenario="fairness_short" if check_id == "fairness" else "mixed_short",
        check_ids=[check_id], started_at="now", status="completed", valid=True, score=False)
    check = CheckResult(id=check_id, target="service", title=check_id,
        status="fail", summary="Wrong answer and incomplete overlap.", metrics=metrics)
    summary = _verdict_summary(check, config(limits=Limits(fairness_max_ratio=3)), [record], "Raise budgets")
    assert ANSWER_REMEDY in summary and "address the slowdown" not in summary


def test_fairness_allowed_answer_miss_does_not_add_model_remedy():
    record = RequestRecord(id="answer", target="service", fixture_id="fixture",
        scenario="fairness_short", check_ids=["fairness"], started_at="now",
        status="completed", valid=True, score=False, max_stream_gap_ms=200)
    check = CheckResult(id="fairness", target="service", title="Fairness", status="fail",
        summary="Stream gap exceeded.", metrics={"accuracy": .95,
            "answer_failures": [{"fixture_id": "fixture", "failed": 1, "scored": 20}]})
    summary = _verdict_summary(check, config(limits=Limits(
        min_correctness=.9, stream_gap_ms=50)), [record], "Raise budgets")
    assert "address the slowdown" in summary and ANSWER_REMEDY not in summary


def test_mixed_wrong_answers_keep_inconclusive_verdict_and_get_answer_remedy():
    check = CheckResult(id="mixed", target="service", title="Interference",
        status="inconclusive", summary="Comparison lacks qualified light samples.",
        metrics={"unqualified_light_samples": ["answer"], "correctness_observed": 0})
    record = RequestRecord(id="answer", target="service", fixture_id="mixed.light.v1",
        scenario="mixed_short_long_input", check_ids=["mixed"], started_at="now",
        status="completed", valid=True, score=False, output="Wrong answer",
        evaluation=EvaluationEvidence(messages=[], scorer="exact", expected_provided=True, expected="Correct answer"))
    summary = _verdict_summary(check, config(), [record], "Raise budgets")
    assert ANSWER_REMEDY in summary and "mixed.light.v1" in summary
    assert 'received: "Wrong answer"' in summary
    assert "address the slowdown" not in summary
    assert check.status == "inconclusive"
