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
async def test_first_answer_waits_for_reasoning_and_counts_trailing_stall(monkeypatch):
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
    assert result.max_stream_gap_ms >= 55
    assert result.output == "42" and result.reasoning == "thinking"


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
    assert result.max_stream_gap_ms >= 30
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
