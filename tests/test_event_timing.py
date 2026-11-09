"""Socket-free, synthetic SSE endpoint with deterministic receipt timestamps."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.concurrent_load import OutputProgress
from giraffe.models import RequestRecord, RequestSpec, RunConfig, Target


def sse(delta=None, *, finish=None, usage=None, tokens=None):
    choice = {"delta": delta or {}, "finish_reason": finish}
    if tokens is not None:
        choice["logprobs"] = {"content": [{"token": token} for token in tokens]}
    value = {"choices": [choice]}
    if usage is not None:
        value["usage"] = usage
    return ("data: " + json.dumps(value) + "\n\n").encode()


class TimedStream(httpx.AsyncByteStream):
    """Fake endpoint: each pair is a receipt time and one network read.

    Combining several SSE events in one byte string models transport buffering.
    Exceptions model partial read failures without a socket or wall-clock sleep.
    """
    def __init__(self, events, clock):
        self.events, self.clock = events, clock
        self.closed = False

    async def __aiter__(self):
        for at, value in self.events:
            self.clock[0] = at
            if isinstance(value, BaseException):
                raise value
            yield value

    async def aclose(self):
        self.closed = True


async def execute(monkeypatch, events, *, stream=True, body=None, progress=None, **spec_options):
    from giraffe import client as module

    clock = [0.0]
    source = TimedStream(events, clock)
    target = Target(name="fake", url="http://synthetic.invalid", model="synthetic")
    client = LLMClient(target, RunConfig(targets=[target], stream=stream))
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body) if body else
        httpx.Response(200, stream=source, headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    async with httpx.AsyncClient(transport=transport) as http:
        client._client = http
        try:
            record = await client.execute(RequestSpec(fixture_id="synthetic", scenario="serving",
                check_ids=["serving"], messages=[{"role": "user", "content": "synthetic"}], **spec_options),
                first_output_event=progress)
        finally:
            await client.__aexit__()  # Join transport cleanup as a real client owner does.
    return record, source


async def test_ttft_ignores_metadata_and_keeps_reasoning_separate(monkeypatch):
    record, source = await execute(monkeypatch, [
        (.1, sse({"role": "assistant"})), (.2, sse({"content": ""})),
        (.3, sse({"reasoning_content": "thinking"})),
        (.5, sse({"content": "A"}, tokens=["A"])),
        (.7, sse({"content": "B"}, tokens=["B"])),
        (.8, sse(finish="stop", usage={"completion_tokens": 3,
            "completion_tokens_details": {"reasoning_tokens": 1}})), (.9, b"data: [DONE]\n\n")])
    assert record.valid and source.closed
    assert record.timing_schema == 1
    assert record.first_token_ms == record.first_reasoning_ms == 300
    assert record.first_output_ms == 500
    assert record.read_gaps_ms == pytest.approx([200])
    assert record.reasoning_read_timestamps_ms == [300]
    assert [event["kind"] for event in record.output_events] == ["reasoning", "answer", "answer"]
    assert record.tpot_direct_ms == pytest.approx(200)
    assert record.tpot_estimate_ms == pytest.approx(200)
    assert record.read_output_tokens_source == "logprobs"
    assert record.itl_ms == pytest.approx([200])
    assert all("thinking" not in str(event) for event in record.output_events)


@pytest.mark.parametrize("continuous_usage", [True, False])
async def test_multitoken_chunk_is_read_gap_but_not_itl_or_direct_tpot(monkeypatch, continuous_usage):
    record, _ = await execute(monkeypatch, [
        (.1, sse({"content": "AB"}, tokens=["A", "B"],
            usage={"completion_tokens": 2} if continuous_usage else None)),
        (.4, sse({"content": "CD"}, tokens=["C", "D"],
            usage={"completion_tokens": 4} if continuous_usage else None)),
        (.5, sse(finish="stop", usage={"completion_tokens": 4})), (.6, b"data: [DONE]\n\n")])
    assert record.read_gaps_ms == pytest.approx([300])
    assert record.tpot_estimate_ms == pytest.approx(150)
    assert record.read_output_tokens_source == ("usage" if continuous_usage else "logprobs")
    assert record.tpot_direct_ms is None and "first" in record.tpot_direct_reason.lower()
    assert record.itl_ms is None and "token" in record.itl_reason.lower()


async def test_buffered_single_token_events_do_not_supply_individual_arrivals(monkeypatch):
    record, _ = await execute(monkeypatch, [(.1,
        sse({"content": "A"}, tokens=["A"]) + sse({"content": "B"}, tokens=["B"])),
        (.2, sse(finish="stop", usage={"completion_tokens": 2})), (.3, b"data: [DONE]\n\n")])
    assert record.first_token_ms == 100
    assert len(record.output_events) == 2
    assert len({event["read_id"] for event in record.output_events}) == 1
    assert record.itl_ms is None and record.tpot_direct_ms is None
    assert record.read_gaps_ms == []


@pytest.mark.parametrize("usage", [None, {"completion_tokens": 1}, {"completion_tokens": 3}])
async def test_missing_single_or_unproven_tokens_do_not_produce_direct_timing(monkeypatch, usage):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"})),
        (.2, sse(finish="stop", usage=usage)), (.3, b"data: [DONE]\n\n")])
    assert record.first_token_ms == 100
    assert record.tpot_direct_ms is None and record.itl_ms is None
    assert record.tpot_estimate_ms is None


async def test_tool_name_is_not_first_token_but_tool_arguments_are(monkeypatch):
    record, _ = await execute(monkeypatch, [
        (.1, sse({"tool_calls": [{"index": 0, "id": "call", "type": "function",
            "function": {"name": "test"}}]})),
        (.4, sse({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]})),
        (.5, sse(finish="tool_calls")), (.6, b"data: [DONE]\n\n")], scorer="tool")
    assert record.valid
    assert record.first_output_ms == 100  # Existing useful-tool-output convention.
    assert record.first_token_ms == 400
    assert [event["kind"] for event in record.output_events] == ["tool_arguments"]
    assert record.itl_ms is None


@pytest.mark.parametrize("failure,status", [(httpx.ReadError("synthetic"), "failed"),
    (__import__("asyncio").CancelledError(), "cancelled")])
async def test_partial_failure_and_cancel_preserve_observations(monkeypatch, failure, status):
    record, source = await execute(monkeypatch, [(.1, sse({"reasoning_content": "think"})),
        (.2, sse({"content": "A"})), (.3, failure)])
    assert record.status == status and not record.valid and source.closed
    assert record.first_token_ms == 100 and record.first_output_ms == 200
    assert len(record.output_events) == 2
    assert record.itl_ms is None and record.tpot_direct_ms is None


async def test_reasoning_only_and_nonstreamed_are_honest(monkeypatch):
    record, _ = await execute(monkeypatch, [(.1, sse({"reasoning_content": "think"})),
        (.2, sse(finish="length")), (.3, b"data: [DONE]\n\n")])
    assert record.first_token_ms == 100 and record.first_output_ms is None
    assert record.reasoning_read_timestamps_ms == [100]
    assert record.tpot_direct_ms is None
    record, _ = await execute(monkeypatch, [], stream=False, body={"choices": [{
        "message": {"content": "AB"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}})
    assert record.valid and record.first_token_ms is None
    assert "non-streamed" in record.first_token_reason.lower()
    assert record.output_events == [] and record.itl_ms is None and record.tpot_direct_ms is None


@pytest.mark.parametrize(("event_cap", "second_answer"), [(1, None), (2, 200)])
async def test_bounded_evidence_discloses_loss_and_invalidates_distribution(monkeypatch, event_cap, second_answer):
    monkeypatch.setattr("giraffe.client._MAX_OUTPUT_EVENTS", event_cap)
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"}, tokens=["A"])),
        (.2, sse({"content": "B"}, tokens=["B"])), (.3, sse({"content": "C"}, tokens=["C"])),
        (.4, sse(finish="stop", usage={"completion_tokens": 3})), (.5, b"data: [DONE]\n\n")])
    assert len(record.output_events) == event_cap and record.output_events_truncated
    assert record.second_answer_event_ms == second_answer
    assert record.first_token_ms == 100
    assert record.itl_ms is None and record.tpot_direct_ms is None


def test_legacy_records_do_not_gain_new_timing_evidence():
    record = RequestRecord(id="old", target="fake", fixture_id="old", scenario="serving",
        check_ids=[], started_at="2026-01-01", status="completed", first_output_ms=50,
        last_output_ms=100, answer_reads=2, output_tokens=2, valid=True)
    assert record.timing_schema == 0 and record.first_token_ms is None
    assert record.itl_ms is None and record.tpot_direct_ms is None and record.tpot_estimate_ms is None
    assert record.read_gaps_ms is None


@pytest.mark.parametrize("intervening_reasoning", [False, True])
async def test_literal_tag_prefix_keeps_first_token_receipt_time(monkeypatch, intervening_reasoning):
    events = [(.1, sse({"content": "<"}, tokens=["<"]))]
    if intervening_reasoning:
        events.append((.2, sse({"reasoning_content": "thinking"})))
    events += [(.4, sse({"content": "3"}, tokens=["3"])), (.5, sse(finish="stop")),
               (.6, b"data: [DONE]\n\n")]
    record, _ = await execute(monkeypatch, events)
    assert record.valid and record.output == "<3"
    assert record.first_token_ms == record.first_output_ms == 100
    assert record.answer_read_timestamps_ms == [100, 400]
    assert [e["at_ms"] for e in record.output_events if e["kind"] == "answer"] == [100, 400]


@pytest.mark.parametrize("suffix", [None, "3"])
async def test_load_progress_reports_literal_prefix_on_its_original_delta(monkeypatch, suffix):
    progress = OutputProgress(asyncio.Event())

    def events():
        yield .1, sse({"content": "<"})
        assert progress.first and progress.chunks == 1
        if suffix is not None:
            yield .4, sse({"content": suffix})
            assert progress.chunks == 2
        yield .5, sse(finish="stop")
        assert progress.chunks == (1 if suffix is None else 2)
        yield .6, b"data: [DONE]\n\n"

    record, _ = await execute(monkeypatch, events(), progress=progress)
    assert record.valid and record.first_token_ms == 100
    assert record.answer_chunks == progress.chunks


@pytest.mark.parametrize("split_prefix", [False, True])
async def test_multi_user_progress_does_not_signal_answer_on_finish(monkeypatch, split_prefix):
    from giraffe.multi_user import _UserOutputProgress

    progress = _UserOutputProgress(asyncio.Event(), lambda: None)

    def events():
        if split_prefix:
            yield .1, sse({"content": "<"})
            assert not progress.first_token and not progress.first and progress.chunks == 0
            yield .2, sse(finish="stop")
        else:
            yield .1, sse({"content": "A"}) + sse({"content": "B"}) + sse(finish="stop")
        assert progress.finished and not progress.first and progress.chunks == 0
        yield .3, b"data: [DONE]\n\n"

    record, _ = await execute(monkeypatch, events(), progress=progress)
    assert record.valid and record.first_token_ms == 100
    assert record.answer_chunks == (1 if split_prefix else 2)


async def test_inline_reasoning_classifies_reads_and_keeps_ttft(monkeypatch):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "<think>thought"})),
        (.3, sse({"content": "</think>A"})), (.5, sse({"content": "B"})),
        (.6, sse(finish="stop", usage={"completion_tokens": 3,
            "completion_tokens_details": {"reasoning_tokens": 1}})), (.7, b"data: [DONE]\n\n")])
    assert record.valid and record.first_token_ms == 100
    assert record.first_output_ms == 300
    assert record.first_reasoning_ms == 100
    assert record.reasoning_read_timestamps_ms == [100]
    assert record.answer_read_timestamps_ms == [300, 500]
    assert [event["kind"] for event in record.output_events] == ["reasoning", "answer", "answer"]
    assert record.itl_ms is None and record.tpot_direct_ms is None


@pytest.mark.parametrize("same_read", [True, False])
async def test_orphan_reasoning_close_reclassifies_live_answer_progress(monkeypatch, same_read):
    from giraffe.multi_user import _UserOutputProgress

    progress = _UserOutputProgress(asyncio.Event(), lambda: None)
    hidden = sse({"content": "thought A"}) + sse({"content": "thought B</th"})
    first_answer = sse({"content": "ink>K7P4"})
    second_answer = sse({"content": " 42"})

    def events():
        if same_read:
            yield .1, hidden + first_answer
        else:
            yield .1, hidden
            assert progress.first and progress.chunks == 2
            yield .2, first_answer
        assert progress.first_token and progress.first and progress.chunks == 1
        yield .4, second_answer
        assert progress.chunks == 2
        yield .5, sse(finish="stop")
        yield .6, b"data: [DONE]\n\n"

    record, _ = await execute(monkeypatch, events(), progress=progress)
    assert record.valid
    assert record.generation_chunks == 4 and record.answer_chunks == 2
    assert record.first_token_ms == record.first_reasoning_ms == 100
    assert record.first_output_ms == (100 if same_read else 200)
    assert record.second_answer_event_ms == 400


@pytest.mark.parametrize(("completion", "expected"), [(0, 0), (3, None), (None, None)])
async def test_explicit_reasoning_preserves_only_known_zero_completion_usage(monkeypatch, completion, expected):
    record, _ = await execute(monkeypatch, [
        (.1, sse({"content": "A", "reasoning_content": "thinking"})),
        (.2, sse({"content": "B"})),
        (.3, sse(finish="stop", usage={"completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": 0}})),
        (.4, b"data: [DONE]\n\n"),
    ])
    assert record.valid and record.answer_chunks == 2
    assert record.output_tokens == expected


async def test_later_multitoken_event_supports_mean_tpot_but_not_itl(monkeypatch):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"}, tokens=["A"])),
        (.5, sse({"content": "BC"}, tokens=["B", "C"])),
        (.6, sse(finish="stop", usage={"completion_tokens": 3})), (.7, b"data: [DONE]\n\n")])
    assert record.tpot_direct_ms == pytest.approx(200)
    assert record.itl_ms is None and "Multi-token" in record.itl_reason


@pytest.mark.parametrize("finish,count", [("length", 3), ("stop", 4), ("stop", 1)])
async def test_usage_must_match_proven_token_events(monkeypatch, finish, count):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"}, tokens=["A"])),
        (.5, sse({"content": "B"}, tokens=["B"])),
        (.6, sse(finish=finish, usage={"completion_tokens": count})), (.7, b"data: [DONE]\n\n")])
    assert record.streamed_answer_tokens is None
    assert record.tpot_direct_ms is None
    if count > 1:
        assert record.tpot_direct_reason == "Streamed token-boundary evidence does not match answer usage."
    assert record.itl_ms is None


@pytest.mark.parametrize("stop_tokens", [None, [""]])
async def test_one_unstreamed_stop_token_keeps_direct_timing_and_pace(monkeypatch, stop_tokens):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"}, tokens=["A"])),
        (.3, sse({"content": "B"}, tokens=["B"])), (.5, sse({"content": "C"}, tokens=["C"])),
        (.6, sse({"content": ""}, finish="stop", usage={"completion_tokens": 4}, tokens=stop_tokens)),
        (.7, b"data: [DONE]\n\n")])
    assert record.tpot_direct_ms == pytest.approx(400 / (3 - 1))
    assert record.output_tokens == 4 and record.streamed_answer_tokens == 3
    assert record.itl_ms == pytest.approx([200, 200])
    assert record.generation_tokens_per_second == pytest.approx(5)
    assert record.read_output_tokens_source == "logprobs"


async def test_one_streamed_token_and_unstreamed_stop_have_no_interval(monkeypatch):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"}, tokens=["A"])),
        (.2, sse(finish="stop", usage={"completion_tokens": 2})), (.3, b"data: [DONE]\n\n")])
    assert record.streamed_answer_tokens == 1
    assert record.tpot_direct_reason == "Single-token answers have no subsequent-token interval."
    assert record.tpot_direct_ms is None and record.itl_ms is None


async def test_whitespace_generated_content_precedes_useful_output(monkeypatch):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": " "})),
        (.4, sse({"content": "A"})), (.5, sse(finish="stop")), (.6, b"data: [DONE]\n\n")])
    assert record.first_token_ms == 100 and record.first_output_ms == 400
    assert record.second_answer_event_ms is None


async def test_read_evidence_cap_marks_read_distribution_unavailable(monkeypatch):
    monkeypatch.setattr("giraffe.client._MAX_CONTENT_READS", 2)
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"})),
        (.2, sse({"content": "B"})), (.3, sse({"content": "C"})),
        (.4, sse(finish="stop", usage={"completion_tokens": 3})), (.5, b"data: [DONE]\n\n")])
    assert record.read_timestamps_truncated and len(record.answer_read_timestamps_ms) == 2
    assert record.read_gaps_ms is None and record.tpot_direct_ms is None


async def test_saved_record_roundtrip_keeps_token_arrival_evidence(monkeypatch):
    record, _ = await execute(monkeypatch, [(.1, sse({"content": "A"}, tokens=["A"])),
        (.5, sse({"content": "B"}, tokens=["B"])),
        (.6, sse(finish="stop", usage={"completion_tokens": 2})), (.7, b"data: [DONE]\n\n")])
    saved = RequestRecord.model_validate_json(record.model_dump_json())
    assert saved.first_token_ms == 100 and saved.second_answer_event_ms == 500
    assert saved.itl_ms == [400] and saved.tpot_direct_ms == 400
    assert saved.read_output_tokens_source == "logprobs"
    assert saved.output_events[0]["token_count_source"] == "choice.logprobs.content"
