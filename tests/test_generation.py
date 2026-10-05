"""Generation pace must distinguish prompt waiting, decoding and stream cleanup."""

import pytest

from giraffe.models import Limits, RequestRecord
from giraffe.runner import _missing_timing, _stats, _timing_violations


def record(**changes):
    return RequestRecord(**{
        "id": "long-input", "target": "local", "fixture_id": "label",
        "scenario": "capacity_c1_long", "check_ids": ["generation", "capacity"],
        "started_at": "2026-10-04T00:00:00Z", "status": "completed", "valid": True,
        "first_output_ms": 39000, "last_output_ms": 39250, "elapsed_ms": 45000,
        "output_tokens": 4, "answer_chunks": 4, "stream": True, **changes,
    })


def test_prefill_and_trailing_protocol_wait_do_not_reduce_generation_pace():
    sample = record()
    assert sample.generation_tokens_per_second == 12
    stats = _stats([sample])
    assert stats["generation_tokens_per_second_p50"] == 12
    assert stats["output_tokens_per_second_p50"] == .089
    limits = Limits(min_output_tokens_per_second=1)
    assert _timing_violations([sample], limits) == []
    assert _missing_timing([sample], limits) == []


def test_slow_decoding_still_fails_and_total_latency_remains_independent():
    slow = record(last_output_ms=44000)
    assert slow.generation_tokens_per_second == .6
    assert _timing_violations([slow], Limits(min_output_tokens_per_second=1)) == [slow.id]
    assert _timing_violations([record()], Limits(latency_ms=30000)) == ["long-input"]


@pytest.mark.parametrize("changes", [
    {"output_tokens": None}, {"output_tokens": 0}, {"output_tokens": 1},
    {"stream": False}, {"answer_chunks": 1}, {"answer_chunks": 0},
    {"first_output_ms": None}, {"last_output_ms": None},
    {"last_output_ms": 39000}, {"last_output_ms": 38000},
    {"valid": False}, {"status": "timeout"},
])
def test_unobservable_or_incomplete_generation_is_not_reported_as_a_rate(changes):
    sample = record(**changes)
    assert sample.generation_tokens_per_second is None
    assert _stats([sample])["generation_rate_samples"] == 0
    assert _missing_timing([sample], Limits(min_output_tokens_per_second=1)) == [
        "generation_tokens_per_second",
    ]


def test_single_token_answers_do_not_hide_or_invalidate_measured_multi_token_answers():
    limits = Limits(min_output_tokens_per_second=1)
    assert not _missing_timing([record(), record(output_tokens=1)], limits)
    assert _missing_timing([record(), record(answer_chunks=1)], limits)


def test_legacy_records_do_not_get_reconstructed_generation_timings():
    data = record().model_dump(exclude={"last_output_ms", "answer_chunks"})
    assert RequestRecord.model_validate(data).generation_tokens_per_second is None
