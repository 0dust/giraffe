"""Missing answers and timing evidence cannot become passing timing verdicts."""

import pytest

from giraffe import runner
from giraffe.client import REASONING_BUDGET_ERROR
from giraffe.models import CheckResult, Limits, RequestRecord
from tests.test_runner import config


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def answer(identifier, check_id, **updates):
    return RequestRecord(**(dict(id=identifier, target="service", fixture_id="answer",
        scenario="generation" if check_id == "generation" else "warm", check_ids=[check_id],
        started_at="now", status="completed", valid=True, output="answer", output_chars=6,
        answer_chars=6, output_tokens=4, completion_tokens=4, stream=True,
        elapsed_ms=30, first_output_ms=2, last_output_ms=10, stream_end_ms=12,
        answer_reads=4, answer_chunks=4, first_read_answer_chars=1,
        timing_start_ms=0, finish_reason="stop") | updates))


def assess(check_id, records, **limits):
    settings = config(checks=[check_id], limits=Limits(min_samples=2, **limits))
    state = runner._Run(settings, None, None)
    state.records = records
    return next(check for check in runner._checks(state, settings.targets[0]) if check.id == check_id)


@pytest.mark.parametrize("check_id", ["first_output", "generation"])
def test_answer_budget_exhaustion_remains_inconclusive_with_enough_completed_answers(check_id):
    records = [answer(str(index), check_id) for index in range(2)]
    records.append(answer("no-answer", check_id, valid=False, output="", output_chars=0,
        answer_chars=0, reasoning="reasoning only", finish_reason="length", error=REASONING_BUDGET_ERROR))

    result = assess(check_id, records, first_output_ms=100, latency_ms=100)

    assert result.status == "inconclusive"
    assert result.finding_summary == "Reasoning exhausted the output budget before an answer."
    assert result.metrics["completed"] == 2
    assert result.metrics["reasoning_budget_exhausted"] == 1
    assert result.metrics["failed"] == 0


def test_missing_first_answer_timing_is_inconclusive_with_enough_completed_answers():
    records = [answer("measured", "first_output"),
               answer("missing", "first_output", first_output_ms=None)]

    result = assess("first_output", records, first_output_ms=100)

    assert result.status == "inconclusive"
    assert result.finding_summary == "Useful first-output timing unavailable for some responses."
    assert result.metrics["completed"] == 2
    assert result.metrics["first_output_p95_ms"] == 2


def test_single_token_answer_does_not_supply_generation_pace_sample():
    records = [answer("measured", "generation", first_read_output_tokens=1, last_read_output_tokens=4),
               answer("single-token", "generation", output="a", output_chars=1,
                      answer_chars=1, output_tokens=1, completion_tokens=1,
                      answer_reads=1, answer_chunks=1, first_read_answer_chars=1)]

    result = assess("generation", records, min_output_tokens_per_second=1)

    assert result.status == "inconclusive"
    assert result.finding_summary == "Too few measurable streamed answers to assess generation pace."
    assert result.metrics["completed"] == 2
    assert result.metrics["generation_rate_samples"] == 1
    assert "missing_timing_metrics" not in result.metrics


@pytest.mark.parametrize("reasoning_tokens", [None, 0])
@pytest.mark.parametrize("streamed_logprobs", [False, True])
def test_reasoning_without_usage_breakdown_explains_generation_limit(
        reasoning_tokens, streamed_logprobs):
    events = [{"kind": "reasoning", "token_count": 2},
              {"kind": "answer", "token_count": 4}] if streamed_logprobs else []
    records = [answer(str(index), "generation", reasoning="thinking", output_tokens=None,
        completion_tokens=11, reasoning_tokens=reasoning_tokens, output_events=events,
        first_read_output_tokens=1, last_read_output_tokens=4,
        read_output_tokens_source="logprobs" if streamed_logprobs else None)
        for index in range(2)]

    result = assess("generation", records, min_output_tokens_per_second=1)

    assert result.status == "inconclusive"
    assert result.metrics["generation_rate_samples"] == 0
    assert result.metrics["missing_timing_metrics"] == ["generation_tokens_per_second"]
    assert "reasoning-token usage breakdown" in result.finding_summary
    assert "reports reasoning-token usage separately" in result.summary
    # Separated reasoning usage alone cannot yield pace without counts at answer reads.
    assert ("enable streamed logprobs" in result.summary) is not streamed_logprobs
    assert "disable response buffering" not in result.summary
    assert all(record.output_tokens is None for record in records)
    assert result.remediation == {"action": "review_check", "check": "generation",
                                  "code": "unseparated_reasoning_tokens"}
    assert CheckResult.model_validate_json(result.model_dump_json()) == result


def test_reasoning_limit_keeps_remedy_for_other_missing_timing():
    records = [answer(str(index), "generation", reasoning="thinking", output_tokens=None,
        completion_tokens=11, read_output_tokens_source="logprobs", max_stream_gap_ms=None,
        answer_reads=1) for index in range(2)]

    result = assess("generation", records, min_output_tokens_per_second=1, stream_gap_ms=500)

    assert result.metrics["missing_timing_metrics"] == ["generation_tokens_per_second", "stream_gap_ms"]
    assert result.metrics["reason_code"] == "unseparated_reasoning_tokens"
    assert "reports reasoning-token usage separately" in result.summary
    assert "disable response buffering along the route" in result.summary
    assert "enable streamed logprobs" not in result.summary


def test_reasoning_without_any_usage_retains_stream_evidence_remedy():
    records = [answer(str(index), "generation", reasoning="thinking", output_tokens=None,
        completion_tokens=None, reasoning_tokens=None) for index in range(2)]

    result = assess("generation", records, min_output_tokens_per_second=1)

    assert result.status == "inconclusive"
    assert result.metrics.get("reason_code") != "unseparated_reasoning_tokens"
    assert "enable streamed logprobs" in result.summary
    assert "reasoning-token usage breakdown" not in result.finding_summary


def test_mixed_missing_counts_retain_stream_evidence_remedy():
    records = [answer("0", "generation", reasoning="thinking", output_tokens=None, completion_tokens=11),
               answer("1", "generation", output_tokens=None)]

    result = assess("generation", records, min_output_tokens_per_second=1)

    assert result.status == "inconclusive"
    assert result.metrics.get("reason_code") != "unseparated_reasoning_tokens"
    assert "enable streamed logprobs" in result.summary


def test_plain_output_missing_counts_retains_stream_evidence_remedy():
    records = [answer(str(index), "generation", output_tokens=None) for index in range(2)]

    result = assess("generation", records, min_output_tokens_per_second=1)

    assert result.status == "inconclusive"
    assert "enable streamed logprobs" in result.summary
    assert "reasoning-token usage breakdown" not in result.finding_summary
