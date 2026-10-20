import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import RequestSpec, RunConfig, Target


def spec(**changes):
    return RequestSpec(
        fixture_id="answer", scenario="serving", check_ids=["serving"],
        messages=[{"role": "user", "content": "Return 42"}], **changes,
    )


async def test_client_setup_keeps_event_loop_responsive(monkeypatch):
    import asyncio
    import threading

    started, release = threading.Event(), threading.Event()
    actual = httpx.AsyncClient

    def slow_client(**kwargs):
        started.set()
        if not release.wait(timeout=1):
            raise AssertionError("Inference client setup blocked the event loop")
        return actual(**kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    monkeypatch.setattr(httpx, "AsyncClient", slow_client)
    target = Target(name="local", url="http://local", model="test")

    async def setup():
        async with LLMClient(target, RunConfig(targets=[target])) as client:
            assert client._client is not None
        assert client._client is None

    async def heartbeat():
        while not started.is_set():
            await asyncio.sleep(0)
        release.set()

    await asyncio.gather(setup(), heartbeat())


@pytest.mark.asyncio
async def test_answer_stream_counts_usage_and_preserves_finish(monkeypatch):
    target = Target(name="local", url="http://local/v1", model="test")
    config = RunConfig(targets=[target], max_output_tokens=20)
    received = []

    def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, text=(
            'data: {"model":"actual","choices":[{"delta":{"content":"42"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":1,"prompt_tokens":8}}\n\n'
            'data: [DONE]\n\n'
        ))

    original = httpx.AsyncClient
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    async with LLMClient(target, config) as client:
        record = await client.execute(spec(max_tokens=7))

    assert record.valid and record.status == "completed"
    assert record.output == "42"
    assert record.output_tokens == 1 and record.input_tokens == 8
    assert record.observed_model == "actual"
    assert record.finish_reason == "stop" and record.stream_terminated
    assert record.first_output_ms is not None
    assert received[0]["max_tokens"] == 7


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.parts = chunks
        self.closed = False

    async def __aiter__(self):
        import asyncio

        for item in self.parts:
            if isinstance(item, float):
                await asyncio.sleep(item)
            else:
                yield item

    async def aclose(self):
        self.closed = True


def setup_client(monkeypatch, chunks=None, *, body=None, timeout=1, stream=True, **kwargs):
    target = Target(name="local", url="http://local", model="test", **kwargs)
    config = RunConfig(targets=[target], request_timeout_seconds=timeout, stream=stream)
    transport_stream = Chunks(chunks or [])
    calls = []

    def handler(request):
        calls.append(request)
        if body is not None:
            return httpx.Response(200, json=body)
        return httpx.Response(200, stream=transport_stream,
                              headers={"content-type": "text/event-stream"})

    original = httpx.AsyncClient
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **options: original(
        transport=httpx.MockTransport(handler), **options,
    ))
    return LLMClient(target, config), transport_stream, calls


def event(content=None, *, reasoning=None, finish=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return ("data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
            + "\n\n").encode()


@pytest.mark.asyncio
async def test_sse_handles_fragmented_utf8_crlf_and_multiline_json(monkeypatch):
    raw = ('data: {"choices":\r\n'
           'data: [{"delta":{"content":"🦒42"},"finish_reason":"stop"}]}\r\n\r\n'
           'data: [DONE]\r\n\r\n').encode()
    client, response, calls = setup_client(monkeypatch, [bytes([byte]) for byte in raw])
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.output == "🦒42"
    assert result.output_tokens is None  # A chunk or character is not a token.
    assert len(calls) == 1 and response.closed
    assert str(calls[0].url) == "http://local/v1/chat/completions"


@pytest.mark.asyncio
async def test_first_answer_waits_for_reasoning_and_excludes_trailing_stall(monkeypatch):
    import asyncio

    client, _, _ = setup_client(monkeypatch, [
        event(reasoning="thinking"), 0.04, event("42"), 0.06,
        event(finish="stop"), b"data: [DONE]\n\n",
    ])
    signal = asyncio.Event()
    async with client:
        task = asyncio.create_task(client.execute(spec(), first_output_event=signal))
        await asyncio.sleep(0.02)
        assert not signal.is_set()
        result = await task
    assert signal.is_set()
    assert result.first_output_ms >= 35
    assert result.first_reasoning_ms < result.first_output_ms
    assert result.max_stream_gap_ms is None
    assert result.output == "42" and result.reasoning == "thinking"


async def test_generation_timing_ends_at_answer_not_finish_or_usage(monkeypatch):
    from types import SimpleNamespace

    client, _, _ = setup_client(monkeypatch, [
        event("33"), event("692F"),
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"completion_tokens":4}}\n\n',
        b'data: [DONE]\n\n',
    ])
    timestamps = iter([0, 0, 39, 39.25, 44, 44.5, 45])
    monkeypatch.setattr("giraffe.client.time", SimpleNamespace(perf_counter=lambda: next(timestamps)))
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.output == "33692F"
    assert result.first_output_ms == 39000
    assert result.last_output_ms == 39250
    assert result.elapsed_ms == 45000
    assert result.answer_chunks == 2
    assert result.generation_tokens_per_second == pytest.approx((4 - 4 * 2 / 6) / .25)


async def test_first_output_timestamp_precedes_json_processing(monkeypatch):
    from types import SimpleNamespace
    from giraffe import client as module

    clock = [0.0]
    client, _, _ = setup_client(monkeypatch, [event("42"), event(finish="stop"),
                                           b"data: [DONE]\n\n"])
    original = module._object

    def slow_parse(payload):
        clock[0] += .2
        return original(payload)

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(module, "_object", slow_parse)
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.first_output_ms == 0
    assert result.elapsed_ms == 400  # Processing remains in client completion time.


async def test_payload_hash_preparation_is_outside_inference_timer(monkeypatch):
    from types import SimpleNamespace
    from giraffe import client as module

    clock = [0.0]
    client, _, _ = setup_client(monkeypatch, [event("42", finish="stop"), b"data: [DONE]\n\n"])
    original = module.hashlib.sha256

    def slow_hash(*args, **kwargs):
        clock[0] += .25
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(module.hashlib, "sha256", slow_hash)
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.client_preparation_ms == 500
    assert result.first_output_ms == result.elapsed_ms == 0


@pytest.mark.parametrize("trailer", [[], [event("   ")]])
async def test_single_answer_chunk_is_not_a_generation_speed_measurement(monkeypatch, trailer):
    client, _, _ = setup_client(monkeypatch, [
        event("a complete answer"), *trailer,
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"completion_tokens":4}}\n\n',
        b'data: [DONE]\n\n',
    ])
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.answer_chunks == 1
    if not trailer:
        assert result.last_output_ms == result.first_output_ms
    assert result.generation_tokens_per_second is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("chunks", "error"), [
    ([event("42"), event(finish="stop")], "without [DONE]"),
    ([event("42"), b"data: [DONE]\n\n"], "without a completion finish"),
    ([b"data: {oops}\n\n"], "malformed JSON"),
    ([b"data: {}"], "unterminated SSE"),
    ([event(reasoning="42"), event(finish="stop"), b"data: [DONE]\n\n"], "no useful answer"),
    ([event("  "), event(finish="stop"), b"data: [DONE]\n\n"], "no useful answer"),
    ([b'data: {"error":{"message":"SECRET"}}\n\n'], "API error"),
    ([b'event: error\ndata: {"message":"SECRET"}\n\n'], "SSE error"),
])
async def test_incomplete_or_invalid_stream_cannot_pass(monkeypatch, chunks, error):
    client, response, calls = setup_client(monkeypatch, chunks)
    async with client:
        result = await client.execute(spec())
    assert result.status == "failed" and not result.valid
    assert error in result.error
    assert "SECRET" not in result.error
    assert response.closed and len(calls) == 1


@pytest.mark.asyncio
async def test_entire_request_deadline_closes_stalled_stream_with_partial_output(monkeypatch):
    client, response, _ = setup_client(monkeypatch, [event("42"), 0.2], timeout=0.04)
    async with client:
        result = await client.execute(spec())
    assert result.status == "timeout" and not result.valid
    assert result.output == "42"
    assert 30 <= result.elapsed_ms < 150
    assert result.max_stream_gap_ms is None
    assert response.closed


@pytest.mark.asyncio
async def test_deadline_includes_wait_for_http_headers(monkeypatch):
    import asyncio

    async def handler(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200)

    original = httpx.AsyncClient
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    target = Target(name="local", url="http://local", model="test")
    async with LLMClient(target, RunConfig(targets=[target], request_timeout_seconds=0.03)) as client:
        result = await client.execute(spec())
    assert result.status == "timeout" and result.http_status is None
    assert result.elapsed_ms < 150


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed", ["finish", "usage", "done", "close"])
async def test_stream_gap_measures_only_inter_answer_intervals(monkeypatch, delayed):
    import asyncio

    tail = [
        ("finish", event(finish="stop")),
        ("usage", b'data: {"choices":[],"usage":{"completion_tokens":2}}\n\n'),
        ("done", b"data: [DONE]\n\n"),
    ]
    chunks = [event("4"), event("2")]
    for name, chunk in tail:
        if delayed == name:
            chunks.append(0.04)
        chunks.append(chunk)
    client, response, _ = setup_client(monkeypatch, chunks)
    if delayed == "close":
        async def close():
            await asyncio.sleep(0.04)
            response.closed = True
        response.aclose = close
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.answer_chunks == 2
    assert result.elapsed_ms - result.last_output_ms >= 35
    assert result.max_stream_gap_ms == pytest.approx(result.last_output_ms - result.first_output_ms)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,status", [(httpx.ConnectTimeout, "failed"),
    (httpx.ReadTimeout, "timeout"), (httpx.PoolTimeout, "timeout")])
async def test_httpx_timeouts_before_headers_have_transport_aware_status(monkeypatch, error_type, status):
    def handler(request):
        raise error_type("SECRET", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    target = Target(name="local", url="http://local", model="test")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        result = await client.execute(spec())
    assert result.status == status and result.http_status is None
    category = "request failed" if status == "failed" else "request timeout"
    assert result.error == f"{category} ({error_type.__name__})"
    assert "SECRET" not in result.error


@pytest.mark.asyncio
async def test_connection_failure_remains_distinct_from_timeout_and_sanitized(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("SECRET TLS connection failed", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    target = Target(name="local", url="http://local", model="test")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        result = await client.execute(spec())
    assert result.status == "failed" and result.http_status is None
    assert result.error == "request failed (ConnectError)"


@pytest.mark.asyncio
async def test_intentional_disconnect_differs_from_timeout(monkeypatch):
    client, response, _ = setup_client(monkeypatch, [event("42"), 0.2])
    async with client:
        result = await client.execute(spec(cancel_after_ms=30))
    assert result.status == "cancelled" and result.error == "intentional cancellation"
    assert not result.valid and result.output == "42" and response.closed


@pytest.mark.asyncio
async def test_run_cancellation_returns_partial_record_and_closes_connection(monkeypatch):
    import asyncio

    client, response, _ = setup_client(monkeypatch, [event("42"), 0.2])
    signal = asyncio.Event()
    async with client:
        task = asyncio.create_task(client.execute(spec(), first_output_event=signal))
        await asyncio.wait_for(signal.wait(), timeout=1)
        task.cancel()
        result = await task
    assert result.status == "cancelled" and result.error == "run cancelled"
    assert result.output == "42" and response.closed


@pytest.mark.asyncio
async def test_normal_length_cap_is_a_valid_but_distinct_completion(monkeypatch):
    client, _, calls = setup_client(monkeypatch, [
        event("42", finish="length"), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(max_tokens=999, options={
            "model": "wrong", "max_tokens": 10000, "max_completion_tokens": 10000, "n": 5,
        }))
    assert result.valid and result.finish_reason == "length"
    request = json.loads(calls[0].content)
    assert request["max_tokens"] == result.requested_max_tokens == 128
    assert request["model"] == "test" and request["n"] == 1
    assert "max_completion_tokens" not in request


@pytest.mark.asyncio
async def test_nonstream_completion_uses_only_server_counts_and_identity(monkeypatch):
    client, _, _ = setup_client(monkeypatch, stream=False, body={
        "model": "observed", "backend_id": "worker-3",
        "choices": [{"message": {"content": "42"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 6, "completion_tokens": 21,
                  "completion_tokens_details": {"reasoning_tokens": 20}},
    })
    async with client:
        result = await client.execute(spec())
    assert result.valid and not result.stream
    assert result.output_tokens == 1 and result.input_tokens == 6
    assert result.completion_tokens == 21  # The token cap applies to all generated tokens.
    assert result.observed_model == "observed" and result.backend_id == "worker-3"
    assert result.max_stream_gap_ms is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"error": {"message": "secret-value"}},
    {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
    {"choices": [{"message": {"content": "42"}, "finish_reason": None}]},
])
async def test_http_200_nonstream_errors_and_empty_results_fail(monkeypatch, body):
    client, _, _ = setup_client(monkeypatch, body=body, stream=False)
    async with client:
        result = await client.execute(spec())
    assert not result.valid and result.status == "failed"
    assert "secret-value" not in result.error


@pytest.mark.asyncio
async def test_missing_credentials_fail_before_request(monkeypatch):
    monkeypatch.delenv("GIRAFFE_MISSING_TEST_KEY", raising=False)
    target = Target(name="local", url="http://local", model="test",
                    api_key_env="GIRAFFE_MISSING_TEST_KEY")
    with pytest.raises(ValueError, match="GIRAFFE_MISSING_TEST_KEY"):
        async with LLMClient(target, RunConfig(targets=[target])):
            pass


@pytest.mark.asyncio
async def test_unbounded_response_without_newline_is_capped(monkeypatch):
    monkeypatch.setattr("giraffe.client._MAX_RESPONSE_BYTES", 100)
    client, response, _ = setup_client(monkeypatch, [b"data: " + b"x" * 60, b"x" * 60])
    async with client:
        result = await client.execute(spec())
    assert result.status == "failed" and "safety limit" in result.error
    assert response.closed


@pytest.mark.asyncio
async def test_sse_allows_bare_carriage_return_line_endings(monkeypatch):
    raw = (event("42", finish="stop") + b"data: [DONE]\n\n").replace(b"\n", b"\r")
    client, _, _ = setup_client(monkeypatch, [raw])
    async with client:
        result = await client.execute(spec())
    assert result.valid and result.output == "42"


@pytest.mark.asyncio
async def test_stream_request_json_error_is_not_misclassified_as_empty_sse(monkeypatch):
    client, _, _ = setup_client(monkeypatch, body={"error": {"message": "secret-value"}})
    async with client:
        result = await client.execute(spec())
    assert result.status == "failed" and "API error" in result.error
    assert "secret-value" not in result.error


def tool_event(delta, finish=None, **choice_fields):
    return ("data: " + json.dumps({"choices": [{
        "delta": delta, "finish_reason": finish, **choice_fields,
    }]}, ensure_ascii=False) + "\n\n").encode()


def tool_call(index=0, *, call_id="call-secret", name="get_weather", arguments='{"city":"Delhi"}'):
    return {"index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


@pytest.mark.parametrize("stream", [True, False])
async def test_mixed_content_and_interleaved_calls_reconstruct_identically(monkeypatch, stream):
    expected_calls = [tool_call(), tool_call(1, call_id="second-secret", name="get_time",
                                          arguments='{"zone":"Asia/Kolkata"}')]
    if stream:
        raw = b"".join([
            tool_event({"content": "Before 🦒. "}),
            tool_event({"content": "Between. ", "tool_calls": [
                tool_call(1, call_id="second-secret", name="get_", arguments='{"zone":'),
                tool_call(name="get_", arguments='{"city":'),
            ]}),
            tool_event({"tool_calls": [{"index": 0, "function": {
                "name": "weather", "arguments": '"Delhi"}'}}]}),
            tool_event({"tool_calls": [{"index": 1, "function": {
                "name": "time", "arguments": '"Asia/Kolkata"}'}}]}),
            tool_event({"content": "After."}, "tool_calls"), b"data: [DONE]\n\n",
        ])
        # Split every UTF-8 byte, SSE frame, function name, and JSON argument boundary.
        client, _, _ = setup_client(monkeypatch, [bytes([byte]) for byte in raw])
    else:
        client, _, _ = setup_client(monkeypatch, stream=False, body={"choices": [{
            "message": {"content": "Before 🦒. Between. After.", "tool_calls": expected_calls},
            "finish_reason": "tool_calls", "index": 0,
        }]})
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert result.valid and result.output == "Before 🦒. Between. After."
    assert result.tool_calls == [{k: v for k, v in call.items() if k != "index"}
                                 for call in expected_calls]
    assert result.finish_reason == "tool_calls" and result.stream_terminated
    assert result.response_events
    trace = json.dumps(result.response_events)
    for secret in ("Before", "Between", "After", "Delhi", "Kolkata", "get_", "secret"):
        assert secret not in trace
    content_events = [item for item in result.response_events if item["kind"] == "content"]
    assert sum(item["length"] for item in content_events) == len(result.output)
    if stream:
        assert [(item["offset"], item["length"]) for item in content_events] == [(0, 10), (10, 9), (19, 6)]
        assert [item["sequence"] for item in content_events] == [0, 1, 4]
        tool_events = [item for item in result.response_events if item["kind"] == "tool_call"]
        assert [item["index"] for item in tool_events] == [1, 0, 0, 1]
        assert tool_events[0]["sequence"] == content_events[1]["sequence"]
        assert tool_events[0]["id_present"] and not tool_events[2]["id_present"]


@pytest.mark.parametrize("suffixes,expected", [
    ([" trailing", " trailing"], "prefix trailing trailing"),
    ([], "prefix"),
])
async def test_transport_preserves_missing_and_duplicate_content_for_scoring(monkeypatch, suffixes, expected):
    client, _, _ = setup_client(monkeypatch, [
        tool_event({"content": "prefix", "tool_calls": [tool_call()]}),
        *[tool_event({"content": suffix}) for suffix in suffixes],
        tool_event({}, "tool_calls"), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert result.valid and result.output == expected
    assert sum(item["length"] for item in result.response_events if item["kind"] == "content") == len(expected)


@pytest.mark.parametrize("terminal", ["length", "tool_calls"])
async def test_repeated_terminal_reason_cannot_erase_truncation(monkeypatch, terminal):
    client, _, _ = setup_client(monkeypatch, [
        tool_event({"tool_calls": [tool_call()]}, "length"),
        tool_event({}, terminal), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert not result.valid and result.finish_reason == "length"
    assert "repeated completion finish reason" in result.error
    assert result.tool_calls[0]["function"]["name"] == "get_weather"


@pytest.mark.parametrize("index", [None, True, -1, 16, "0"])
async def test_stream_tool_index_is_required_and_bounded(monkeypatch, index):
    call = tool_call(index)
    if index is None:
        del call["index"]
    client, _, _ = setup_client(monkeypatch, [
        tool_event({"tool_calls": [call]}, "tool_calls"), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert not result.valid and "invalid tool-call index" in result.error


@pytest.mark.parametrize("index", [1, -1, True, "0", None])
async def test_explicit_choice_index_must_be_zero(monkeypatch, index):
    client, _, _ = setup_client(monkeypatch, [
        tool_event({"content": "42"}, "stop", index=index), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec())
    assert not result.valid and "invalid completion choice index" in result.error


@pytest.mark.parametrize("calls", [
    [tool_call(call_id="")],
    [tool_call(), tool_call(1)],
    [{k: v for k, v in tool_call().items() if k != "type"}],
])
async def test_incomplete_or_duplicate_call_identity_fails_with_content(monkeypatch, calls):
    client, _, _ = setup_client(monkeypatch, [
        tool_event({"content": "visible", "tool_calls": calls}, "tool_calls"), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert not result.valid and "incomplete tool-call" in result.error
    assert result.output == "visible" and result.tool_calls


@pytest.mark.parametrize("trailer,error", [
    ([tool_event({}, "tool_calls")], "without [DONE]"),
    ([b"data: [DONE]\n\n"], "without a completion finish"),
    ([tool_event({}, "tool_calls"), tool_event({"content": "late"})], "after the finish reason"),
    ([tool_event({"tool_calls": [{"index": 0, "id": "conflicting"}]})], "conflicting tool-call"),
])
async def test_invalid_mixed_stream_preserves_partial_evidence(monkeypatch, trailer, error):
    client, _, _ = setup_client(monkeypatch, [
        tool_event({"content": "before", "tool_calls": [tool_call()]}), *trailer,
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert not result.valid and error in result.error
    assert result.output == "before" and result.tool_calls[0]["id"] == "call-secret"
    assert result.response_events


async def test_response_event_trace_is_bounded_without_truncating_answer(monkeypatch):
    client, _, _ = setup_client(monkeypatch, [
        *[tool_event({"content": "x"}) for _ in range(300)],
        tool_event({"tool_calls": [tool_call()]}, "tool_calls"), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert result.valid and result.output == "x" * 300
    assert len(result.response_events) == 256 and result.response_events_truncated
    assert result.tool_calls[0]["function"]["name"] == "get_weather"


async def test_response_event_trace_is_absent_for_non_tool_scorers(monkeypatch):
    client, _, _ = setup_client(monkeypatch, [event("42", finish="stop"), b"data: [DONE]\n\n"])
    async with client:
        result = await client.execute(spec())
    assert result.valid and not result.response_events and not result.response_events_truncated


@pytest.mark.parametrize("arguments,expected", [
    ([], '{"city":'),
    (['"Delhi"}', '"Delhi"}'], '{"city":"Delhi"}"Delhi"}'),
])
async def test_transport_preserves_missing_and_duplicate_tool_fragments(monkeypatch, arguments, expected):
    client, _, _ = setup_client(monkeypatch, [
        tool_event({"tool_calls": [tool_call(arguments='{"city":')]}),
        *[tool_event({"tool_calls": [{"index": 0, "function": {"arguments": part}}]})
          for part in arguments],
        tool_event({}, "tool_calls"), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    # Structural transport validity does not pretend to establish semantic validity.
    assert result.valid and result.tool_calls[0]["function"]["arguments"] == expected
    assert sum(item["arguments_length"] for item in result.response_events) == len(expected)


@pytest.mark.parametrize("stream", [True, False])
async def test_null_content_remains_valid_for_tool_only_responses(monkeypatch, stream):
    content = {"content": None, "tool_calls": [tool_call()]}
    if stream:
        client, _, _ = setup_client(monkeypatch, [
            tool_event(content, "tool_calls"), b"data: [DONE]\n\n",
        ])
    else:
        client, _, _ = setup_client(monkeypatch, stream=False, body={"choices": [{
            "message": content, "finish_reason": "tool_calls",
        }]})
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert result.valid and not result.output
    assert [item["kind"] for item in result.response_events] == ["tool_call"]


async def test_response_trace_limit_is_not_truncation_without_an_omitted_event(monkeypatch):
    client, _, _ = setup_client(monkeypatch, [
        *[tool_event({"content": "x"}) for _ in range(255)],
        tool_event({"tool_calls": [tool_call()]}, "tool_calls"), b"data: [DONE]\n\n",
    ])
    async with client:
        result = await client.execute(spec(scorer="tool"))
    assert result.valid and len(result.response_events) == 256
    assert not result.response_events_truncated


async def test_response_activity_requires_body_including_comments_and_partial_sse_bytes(monkeypatch):
    import asyncio
    from giraffe.client import no_response_failure

    client, transport, _ = setup_client(monkeypatch, [
        b": heartbeat\n\n", .005, b"data: {", .005, b'"incomplete"', .1], timeout=.03)
    activity = []
    client.on_response = lambda: activity.append(asyncio.get_running_loop().time())
    async with client:
        record = await client.execute(spec())
    assert record.status == "timeout" and record.http_status == 200
    assert record.output == "" and record.chunks == 0
    assert len(activity) == 3  # Only raw body reads, before SSE parsing.
    assert record.response_body_bytes > 0
    assert not record.workload
    assert not no_response_failure(record)
    assert transport.closed


@pytest.mark.parametrize("status", [200, 503])
async def test_readiness_uses_headers_without_updating_watchdog(monkeypatch, status):
    import time

    target = Target(name="local", url="http://local", model="test")
    transport = Chunks([1.0, b"body"])
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, stream=transport))))
    responses = []
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        client.on_response = lambda: responses.append(True)
        assert await client.ready(time.monotonic() + .02) == (status == 200)
    assert responses == [] and transport.closed


@pytest.mark.parametrize("kind", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                                  httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
                                  httpx.PoolTimeout, ValueError])
async def test_only_network_no_response_failures_qualify(monkeypatch, kind):
    from giraffe.client import no_response_failure

    target = Target(name="local", url="http://local", model="test")
    original = httpx.AsyncClient

    def failed(_):
        raise kind("unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs,
        transport=httpx.MockTransport(failed)))
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        record = await client.execute(spec())
    assert no_response_failure(record) == (kind not in {httpx.PoolTimeout, ValueError})
