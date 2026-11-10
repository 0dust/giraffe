"""Batched SSE delivery rates use reported counts at observed read boundaries."""

import json
from types import SimpleNamespace

import pytest

from giraffe.models import Limits, RequestRecord
from giraffe.runner import _missing_timing
from tests.test_client import setup_client, spec


DONE = b"data: [DONE]\n\n"
_UNSET = object()


def chunk(text=None, count=_UNSET, *, reasoning=None, reasoning_tokens=_UNSET,
          finish=None, usage_only=False, tokens=None):
    delta = {}
    if text is not None:
        delta["content"] = text
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    data = {"choices": [] if usage_only else [{"delta": delta, "finish_reason": finish}]}
    if tokens is not None:
        data["choices"][0]["logprobs"] = {"content": [{"token": token} for token in tokens]}
    if count is not _UNSET:
        data["usage"] = {"completion_tokens": count}
        if reasoning_tokens is not _UNSET:
            data["usage"]["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return ("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


async def execute(monkeypatch, reads, *, options=None):
    client, _, calls = setup_client(monkeypatch, reads)
    # Preparation, request start, one timestamp per read, cleanup.
    times = iter([0, 0, *range(1, len(reads) + 1), len(reads) + 10])
    monkeypatch.setattr("giraffe.client.time", SimpleNamespace(perf_counter=lambda: next(times)))
    async with client:
        result = await client.execute(spec(options=options or {}))
    assert result.valid
    return result, json.loads(calls[0].content)


async def test_reported_batch_counts_replace_character_estimate_and_batch_size_cutoff(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk("x", 20), chunk("long second answer", 100),
        chunk(count=101, finish="stop"), DONE])
    assert result.first_read_output_tokens == 20
    assert result.last_read_output_tokens == 100
    assert result.generation_tokens_per_second == 80
    assert result.output_tokens == 101  # Trailing EOS count isn't timed answer delivery.
    assert result.first_output_ms == 1000 and result.last_output_ms == 2000
    assert result.elapsed_ms == 14000


async def test_events_coalesced_into_one_read_use_last_content_event_count(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk("a", 2) + chunk("b", 7),
        chunk("c", 9) + chunk("d", 20), chunk(finish="stop"), DONE])
    assert result.answer_chunks == 4 and result.answer_reads == 2
    assert result.first_read_output_tokens == 7 and result.last_read_output_tokens == 20
    assert result.generation_tokens_per_second == 13


async def test_fragmented_event_uses_read_that_completes_event(monkeypatch):
    first = chunk("🦒", 4)
    cut = first.index("🦒".encode()) + 1
    result, _ = await execute(monkeypatch, [first[:cut], first[cut:], chunk("yes", 10),
        chunk(finish="stop"), DONE])
    assert result.first_output_ms == 2000
    assert result.generation_tokens_per_second == 6


@pytest.mark.parametrize("reads", [
    [chunk("a"), chunk("b"), chunk(count=10, finish="stop"), DONE],
    [chunk("a", 2) + chunk("b"), chunk("c", 10), chunk(finish="stop"), DONE],
    [chunk("a", 2), chunk("b", 5) + chunk("c"), chunk(count=10, finish="stop"), DONE],
    [chunk("a") + chunk(count=2, usage_only=True), chunk("b", 10), chunk(finish="stop"), DONE],
    [chunk("a", 2), chunk("b") + chunk(count=10, usage_only=True), chunk(finish="stop"), DONE],
    [chunk("a", 2) + chunk("b", 10) + chunk(finish="stop") + DONE],
])
async def test_missing_boundaries_and_single_read_do_not_establish_rate(monkeypatch, reads):
    result, _ = await execute(monkeypatch, reads)
    assert result.generation_tokens_per_second is None
    assert _missing_timing([result], Limits(min_output_tokens_per_second=1)) == [
        "generation_tokens_per_second"]


@pytest.mark.parametrize("counts", [(4, 3, 5), (2, 10, 9), (True, 10, 10),
    (2, "10", 10), (-1, 10, 10), (2, 10.0, 10), (2, None, 10)])
async def test_invalid_or_decreasing_usage_cannot_establish_rate(monkeypatch, counts):
    first, last, final = counts
    result, _ = await execute(monkeypatch, [chunk("a", first), chunk("b", last),
        chunk(count=final, finish="stop"), DONE])
    assert result.generation_tokens_per_second is None
    assert result.max_stream_gap_ms == 1000


async def test_reasoning_breakdown_at_boundaries_scopes_reported_counts(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk(reasoning="think", count=10, reasoning_tokens=10),
        chunk("a", 14, reasoning_tokens=10), chunk("b", 20, reasoning_tokens=12),
        chunk(count=21, reasoning_tokens=12, finish="stop"), DONE])
    assert result.first_read_output_tokens == 4 and result.last_read_output_tokens == 8
    assert result.generation_tokens_per_second == 4


@pytest.mark.parametrize("first_reasoning,last_reasoning,final_reasoning", [
    (_UNSET, 10, 10), (10, _UNSET, 10), (10, 9, 10), (15, 10, 10), (True, 10, 10),
])
async def test_missing_or_invalid_reasoning_scope_is_not_estimated(monkeypatch,
        first_reasoning, last_reasoning, final_reasoning):
    result, _ = await execute(monkeypatch, [chunk(reasoning="think", count=10, reasoning_tokens=10),
        chunk("a", 14, reasoning_tokens=first_reasoning), chunk("b", 20, reasoning_tokens=last_reasoning),
        chunk(count=20, reasoning_tokens=final_reasoning, finish="stop"), DONE])
    assert result.generation_tokens_per_second is None


async def test_stream_gap_is_measurable_without_usage(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk("a"), chunk("b"), chunk(finish="stop"), DONE])
    assert result.measured_stream_gap
    assert result.generation_tokens_per_second is None


async def test_continuous_usage_is_explicit_and_merges_include_usage_default(monkeypatch):
    result, payload = await execute(monkeypatch, [chunk("a", 1), chunk("b", 5),
        chunk(finish="stop"), DONE], options={"stream_options": {"continuous_usage_stats": True}})
    assert payload["stream_options"] == {"continuous_usage_stats": True, "include_usage": True}
    assert result.generation_tokens_per_second == 4


async def test_unsupported_continuous_usage_keeps_response_but_no_rate(monkeypatch):
    result, payload = await execute(monkeypatch, [chunk("a"), chunk("b"),
        chunk(count=10, finish="stop"), DONE], options={"stream_options": {"continuous_usage_stats": True}})
    assert result.output_tokens == 10 and result.generation_tokens_per_second is None


async def test_default_request_does_not_enable_server_specific_extension(monkeypatch):
    _, payload = await execute(monkeypatch, [chunk("a"), chunk(finish="stop"), DONE])
    assert "continuous_usage_stats" not in payload.get("stream_options", {})


def test_legacy_serialized_counts_do_not_reconstruct_batch_boundaries():
    sample = RequestRecord(id="old", target="t", fixture_id="f", scenario="generation",
        check_ids=["generation"], started_at="now", status="completed", valid=True,
        stream=True, output_tokens=10, completion_tokens=10, answer_reads=2,
        first_output_ms=1000, last_output_ms=2000, answer_chars=10, first_read_answer_chars=8)
    assert RequestRecord.model_validate(sample.model_dump()).generation_tokens_per_second is None


async def test_hidden_reasoning_scope_cannot_be_forgotten_by_later_usage(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk("a", 14, reasoning_tokens=10),
        chunk("b", 20), chunk(count=20, finish="stop"), DONE])
    assert result.generation_tokens_per_second is None
    assert result.output_tokens is None


async def test_explicit_zero_reasoning_breakdown_is_valid_for_plain_output(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk("a", 2, reasoning_tokens=0),
        chunk("b", 10, reasoning_tokens=0), chunk(count=10, reasoning_tokens=0, finish="stop"), DONE])
    assert result.generation_tokens_per_second == 8


async def test_malformed_declared_reasoning_breakdown_invalidates_rate(monkeypatch):
    bad = chunk("a", 2).replace(b'"completion_tokens": 2',
        b'"completion_tokens": 2, "completion_tokens_details": "unknown"')
    result, _ = await execute(monkeypatch, [bad, chunk("b", 10), chunk(finish="stop"), DONE])
    assert result.generation_tokens_per_second is None


async def test_mixed_tool_and_text_counts_cannot_establish_text_pace(monkeypatch):
    tool = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","type":"function","function":{"name":"f","arguments":"{}"}}]}}]}\n\n'
    result, _ = await execute(monkeypatch, [tool, chunk("a", 10), chunk("b", 20),
        chunk(finish="stop"), DONE])
    assert result.tool_calls and result.generation_tokens_per_second is None


async def test_explicit_usage_disable_is_preserved_without_mutating_request_options(monkeypatch):
    options = {"stream_options": {"continuous_usage_stats": True, "include_usage": False}}
    result, payload = await execute(monkeypatch, [chunk("a"), chunk("b"),
        chunk(finish="stop"), DONE], options=options)
    assert payload["stream_options"] == options["stream_options"]
    assert options == {"stream_options": {"continuous_usage_stats": True, "include_usage": False}}
    assert result.generation_tokens_per_second is None


def test_legacy_cached_rate_does_not_qualify_baseline_comparison():
    from giraffe.reporting import compare_baseline
    from tests.test_reporting import make_report

    current, old = make_report(), make_report("old")
    old.requests = [RequestRecord.model_validate(row.model_dump(exclude={
        "first_read_output_tokens", "last_read_output_tokens"})) for row in old.requests]
    old.checks[0].metrics.update(generation_tokens_per_second_p50=999, generation_rate_samples=100)
    result = compare_baseline(current, old)
    rows = {row["metric"]: row for row in result.baseline["targets"][0]["comparisons"]}
    assert rows["generation_tokens_per_second"]["baseline_samples"] == 0
    assert rows["generation_tokens_per_second"]["status"] == "inconclusive"
    assert rows["latency_ms"]["status"] == "pass"


async def test_distinct_reads_with_same_timestamp_keep_distinct_boundaries(monkeypatch):
    reads = [chunk("a", 2), chunk("b", 5), chunk("c", 10), chunk(finish="stop"), DONE]
    client, _, _ = setup_client(monkeypatch, reads)
    times = iter([0, 0, 1, 1, 2, 3, 4, 5])
    monkeypatch.setattr("giraffe.client.time", SimpleNamespace(perf_counter=lambda: next(times)))
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.answer_reads == 3
    assert result.first_read_output_tokens == 2 and result.last_read_output_tokens == 10
    assert result.generation_tokens_per_second == 8


@pytest.mark.parametrize("coalesced", [False, True])
async def test_logprob_counts_establish_pace_with_final_only_usage(monkeypatch, coalesced):
    first = (chunk("abc", tokens=["a", "b", "c"]) if not coalesced else
        chunk("a", tokens=["a"]) + chunk("b", tokens=["b"]) + chunk("c", tokens=["c"]))
    result, _ = await execute(monkeypatch, [first, chunk("d", tokens=["d"]),
        chunk("e", tokens=["e"]), chunk(count=6, finish="stop"), DONE])
    assert result.first_read_output_tokens == 3 and result.last_read_output_tokens == 5
    assert result.read_output_tokens_source == "logprobs"
    assert result.generation_tokens_per_second == (5 - 3) * 1000 / 2000


async def test_continuous_usage_is_preferred_to_logprob_counts(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk("a", 10, tokens=["a"]),
        chunk("b", 20, tokens=["b"]), chunk(count=20, finish="stop"), DONE])
    assert result.first_read_output_tokens == 10 and result.last_read_output_tokens == 20
    assert result.read_output_tokens_source == "usage"
    assert result.generation_tokens_per_second == 10


@pytest.mark.parametrize("first,last", [(2, _UNSET), (_UNSET, 10)])
async def test_partial_usage_uses_logprobs_for_both_boundaries(monkeypatch, first, last):
    result, _ = await execute(monkeypatch, [chunk("abc", first, tokens=["a", "b", "c"]),
        chunk("d", last, tokens=["d"]), chunk(count=10, finish="stop"), DONE])
    assert result.first_read_output_tokens == 3 and result.last_read_output_tokens == 4
    assert result.read_output_tokens_source == "logprobs"
    assert result.generation_tokens_per_second == 1


@pytest.mark.parametrize("missing", [0, 1, 2])
async def test_missing_answer_logprobs_leave_both_boundaries_unmeasured(monkeypatch, missing):
    reads = [chunk(text, 1 if index == 0 else _UNSET,
        tokens=None if index == missing else [text]) for index, text in enumerate("abc")]
    result, _ = await execute(monkeypatch, [*reads, chunk(count=3, finish="stop"), DONE])
    assert result.first_read_output_tokens is None and result.last_read_output_tokens is None
    assert result.read_output_tokens_source is None and result.generation_tokens_per_second is None


async def test_tool_calls_exclude_logprob_pace(monkeypatch):
    tool = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","type":"function","function":{"name":"f","arguments":"{}"}}]}}]}\n\n'
    result, _ = await execute(monkeypatch, [tool, chunk("a", tokens=["a"]),
        chunk("b", tokens=["b"]), chunk(count=3, finish="stop"), DONE])
    assert result.tool_calls and result.generation_tokens_per_second is None
    assert result.read_output_tokens_source is None


async def test_logprob_boundaries_include_leading_but_not_trailing_whitespace(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk(" ", tokens=[" "]),
        chunk("a", tokens=["a"]), chunk("b", tokens=["b"]), chunk(" ", tokens=[" "]),
        chunk(count=5, finish="stop"), DONE])
    assert result.first_read_output_tokens == 2 and result.last_read_output_tokens == 3
    assert result.generation_tokens_per_second == 1


@pytest.mark.parametrize("cap", ["_MAX_CONTENT_READS", "_MAX_OUTPUT_EVENTS"])
async def test_truncated_evidence_cannot_supply_logprob_boundaries(monkeypatch, cap):
    monkeypatch.setattr("giraffe.client." + cap, 2)
    result, _ = await execute(monkeypatch, [chunk(text, tokens=[text]) for text in "abc"] +
        [chunk(count=4, finish="stop"), DONE])
    assert result.first_read_output_tokens is None and result.last_read_output_tokens is None
    assert result.read_output_tokens_source is None and result.generation_tokens_per_second is None


async def test_inline_reasoning_reclassification_cannot_supply_logprob_boundaries(monkeypatch):
    result, _ = await execute(monkeypatch, [chunk("<think>x</think>a", tokens=["x", "a"]),
        chunk("b", tokens=["b"]), chunk(count=3, reasoning_tokens=1, finish="stop"), DONE])
    assert result.reasoning_stripped and result.generation_tokens_per_second is None
    assert result.read_output_tokens_source is None


def test_saved_record_without_read_token_source_loads():
    sample = RequestRecord(id="old", target="t", fixture_id="f", scenario="generation",
        check_ids=["generation"], started_at="now", status="completed")
    saved = RequestRecord.model_validate(sample.model_dump(exclude={"read_output_tokens_source"}))
    assert saved.read_output_tokens_source is None


async def test_aligned_reasoning_logprobs_preserve_evidence_without_inventing_usage(monkeypatch):
    result, _ = await execute(monkeypatch, [
        chunk(reasoning="think more", tokens=["think", " more"]),
        chunk("a", tokens=["a"]), chunk("b", tokens=["b"]),
        chunk(count=9, finish="stop"), DONE])
    assert result.output_events[0]["token_count"] == 2
    assert result.output_events[0]["token_count_source"] == "choice.logprobs.content"
    assert result.completion_tokens == 9
    assert result.reasoning_tokens is None and result.output_tokens is None
    assert result.read_output_tokens_source == "logprobs"
    assert result.generation_tokens_per_second is None


@pytest.mark.parametrize("tokens,answer", [
    (["think", "a"], None),  # Ollama can attach a following answer's tokens here.
    (["<think>", "think"], None),  # Hidden tags have no classified text boundary.
    (["think"], "think"),  # Identical answer/reasoning text cannot identify ownership.
    (["think", "a"], "a"),
    (["different"], None),
])
async def test_ambiguous_reasoning_logprobs_do_not_get_attributed(monkeypatch, tokens, answer):
    result, _ = await execute(monkeypatch, [
        chunk(answer, reasoning="think", tokens=tokens),
        chunk("b", tokens=["b"]), chunk(count=9, finish="stop"), DONE])
    assert all(event["token_count"] is None for event in result.output_events
               if event["sequence"] == 0)
    assert result.reasoning_tokens is None and result.output_tokens is None
    assert result.generation_tokens_per_second is None
