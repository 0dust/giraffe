"""Cold measurements use mock transports and never bind a socket."""

import asyncio
import json
import time

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.cold_start import (
    NATIVE_API_LABEL, OllamaColdClient, cold_spec, detect_ollama, model_load_evidence,
    residency, warm_specs,
)
from giraffe.models import RequestSpec, RunConfig, Target


def settings(**changes):
    target = Target(name="local", url="http://local/v1/chat/completions", model="qwen3:0.6b")
    return target, RunConfig(targets=[target], **changes)


def request_spec(**changes):
    return RequestSpec(fixture_id="ready", scenario="connection", check_ids=[],
                       messages=[{"role": "user", "content": "Return 42"}], **changes)


def install_transport(monkeypatch, handler):
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual(
        transport=httpx.MockTransport(handler), **kwargs))


def test_cold_and_two_warm_specs_have_equal_offline_token_counts_and_distinct_prefixes():
    from giraffe.fixtures import builtin_fixtures
    from giraffe.local_tokenizer import _builtin_encoding

    _, config = settings()
    base = builtin_fixtures(config, check_id="serving")["short"][0]
    original = base.model_dump()
    first, *warm = [cold_spec(base), *warm_specs(base)]
    encoding = _builtin_encoding()
    prompts = [spec.messages[0]["content"] for spec in [first, *warm]]
    tokenized = [encoding.encode(prompt, disallowed_special=()) for prompt in prompts]
    assert [len(tokens) for tokens in tokenized] == [7, 7, 7]
    assert len({tokens[0] for tokens in tokenized}) == 3
    assert [prompt[0] for prompt in prompts] == ["1", "2", "3"]
    assert len(warm) == 2 and all(spec.scenario == "cold_start_warm" for spec in warm)
    assert first.scenario == "readiness"
    for number, spec in enumerate([first, *warm], 1):
        assert spec.check_ids == [] and spec.max_tokens == base.max_tokens
        assert spec.messages == [{"role": "user", "content": f"{number}: Reply with {number}."}]
        assert spec.options == base.options and spec.stream == base.stream
        assert spec.request_timeout_seconds == base.request_timeout_seconds
        assert spec.input_chars == len(prompts[number - 1])
        assert spec.expected == str(number) and spec.scorer == "exact"
    assert base.model_dump() == original


@pytest.mark.asyncio
async def test_detection_is_read_only_authenticated_and_ignores_discovery(monkeypatch):
    target, config = settings()
    target.api_key_env = "COLD_TEST_KEY"
    monkeypatch.setenv("COLD_TEST_KEY", "private-test-key")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer private-test-key"
        return httpx.Response(200, json={"models": []} if request.url.path == "/api/ps"
                              else {"version": "0.31"})

    install_transport(monkeypatch, handler)
    assert target.deployment.discovery != "ollama"
    assert await detect_ollama(target, config, deadline=time.monotonic() + 3, stop=asyncio.Event())
    assert [call.url.path for call in calls] == ["/api/ps", "/api/version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inventory,version", [({}, {"version": "0.31"}), ({"models": []}, {}),
                                              ({"models": []}, {"version": 31})])
async def test_detection_rejects_non_ollama_shapes(monkeypatch, inventory, version):
    target, config = settings()
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, json=inventory if request.url.path == "/api/ps" else version))
    assert not await detect_ollama(target, config)


@pytest.mark.asyncio
async def test_detection_stop_and_deadline_make_no_requests(monkeypatch):
    target, config = settings()
    install_transport(monkeypatch, lambda _: pytest.fail("Observation must not run"))
    stopped = asyncio.Event()
    stopped.set()
    assert not await detect_ollama(target, config, stop=stopped)
    assert not await detect_ollama(target, config, deadline=time.monotonic() - 1)
    assert await residency(target, config, stop=stopped) == "unknown"


@pytest.mark.asyncio
async def test_detection_stop_during_probe_cancels_request(monkeypatch):
    target, config = settings()
    started, stopped = asyncio.Event(), asyncio.Event()
    cancelled = []

    async def handler(_):
        started.set()
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.append(True)

    install_transport(monkeypatch, handler)
    task = asyncio.create_task(detect_ollama(target, config, stop=stopped))
    await started.wait()
    stopped.set()
    assert await asyncio.wait_for(task, .5) is False
    assert cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize("models,expected", [([], "not_resident"),
    ([{"name": "qwen3:0.6b"}], "resident"), ([{"model": "other"}], "not_resident"),
    ([{"invalid": "shape"}], "unknown")])
async def test_residency_uses_inventory_and_does_not_modify_discovery(monkeypatch, models, expected):
    target, config = settings()
    previous = target.deployment.discovery

    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": models})

    install_transport(monkeypatch, handler)
    assert await residency(target, config) == expected
    assert target.deployment.discovery == previous


@pytest.mark.parametrize("before,after,duration,wording,loaded,ms", [
    ("not_resident", "resident", 4_200_000_000, "Model load: 4.2 s, reported by Ollama", "No", 4200),
    ("not_resident", "resident", 0, "Model load: 0 s, reported by Ollama", "No", 0),
    ("resident", "resident", 88_000_000, "Model was already loaded", "Yes", None),
    ("resident", "unknown", 4_200_000_000, "Model was already loaded", "Yes", None),
    ("unknown", "resident", 4_200_000_000, "Slow first answer; model load not confirmed", "Unknown", None),
    ("not_resident", "unknown", 4_200_000_000, "Model was not loaded before the first request", "No", None),
    ("not_resident", "not_resident", 4_200_000_000, "Model was not loaded before the first request", "No", None),
])
def test_model_load_requires_inventory_transition(before, after, duration, wording, loaded, ms):
    result = model_load_evidence(before, after, duration)
    assert result["model_load"] == wording
    assert result["model_loaded_before_first_request"] == loaded
    assert result.get("model_load_ms") == ms


@pytest.mark.parametrize("duration", [None, True, False, "4200000000", -1, float("nan"),
                                     float("inf"), 10 ** 400])
def test_invalid_reported_duration_never_claims_measured_load(duration):
    result = model_load_evidence("not_resident", "resident", duration)
    assert result["model_load"] == "Model was not loaded before the first request"
    assert "model_load_ms" not in result


class NativeChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, float):
                await asyncio.sleep(chunk)
            else:
                yield chunk

    async def aclose(self):
        self.closed = True


def native_chunk(content="", *, thinking="", done=False, **extra):
    return (json.dumps({"model": "qwen3:0.6b", "message": {"content": content, "thinking": thinking},
                        "done": done, **extra}) + "\n").encode()


@pytest.mark.asyncio
async def test_native_stream_preserves_messages_caps_options_and_normal_route(monkeypatch):
    target, config = settings(stream=False, max_output_tokens=20,
                              request_options={"temperature": .4, "seed": 1, "top_p": .9})
    calls = []
    chunks = NativeChunks([native_chunk("42"), native_chunk(done=True, load_duration=903_000_000,
                                                          prompt_eval_count=8, eval_count=1), 20.0])

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/chat":
            return httpx.Response(200, stream=chunks)
        return httpx.Response(200, json={"choices": [{"message": {"content": "42"},
                                                       "finish_reason": "stop"}]})

    install_transport(monkeypatch, handler)
    signal = asyncio.Event()
    async with LLMClient(target, config) as client:
        original_transport = client._client
        spec = request_spec(max_tokens=7, stream=False, options={"seed": 3})
        result = await asyncio.wait_for(OllamaColdClient(client).execute(spec, first_output_event=signal), .5)
        assert client._client is original_transport
        normal = await client.execute(spec)
    assert signal.is_set() and chunks.closed
    assert result.valid and result.status == "completed" and result.stream
    assert result.output == "42" and result.output_tokens == 1 and result.input_tokens == 8
    assert result.first_output_ms is not None and result.first_token_ms is not None
    assert result.workload["reported_load_duration_ns"] == 903_000_000
    assert result.workload["measurement_api"] == NATIVE_API_LABEL
    native, usual = [json.loads(call.content) for call in calls]
    assert native["messages"] == spec.messages == usual["messages"]
    assert native["stream"] is True and usual["stream"] is False
    assert native["options"] == {"num_predict": 7, "temperature": .4, "seed": 3, "top_p": .9}
    assert usual["max_tokens"] == 7
    assert [call.url.path for call in calls] == ["/api/chat", "/v1/chat/completions"]
    assert normal.valid


@pytest.mark.asyncio
async def test_native_reasoning_precedes_first_answer_and_fragmented_utf8(monkeypatch):
    target, config = settings()
    answer = native_chunk("🦒42")
    chunks = NativeChunks([native_chunk(thinking="thinking"), .02,
                           *[bytes([byte]) for byte in answer], native_chunk(done=True)])
    install_transport(monkeypatch, lambda _: httpx.Response(200, stream=chunks))
    async with LLMClient(target, config) as client:
        result = await OllamaColdClient(client).execute(request_spec())
    assert result.valid and result.output == "🦒42" and result.reasoning == "thinking"
    assert result.first_reasoning_ms < result.first_output_ms
    assert result.first_output_ms >= 15
    assert "reported_load_duration_ns" not in result.workload


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks,error", [
    ([native_chunk("42")], "SSE stream ended without [DONE]"),
    ([b"invalid\n"], "malformed JSON in response"),
    ([b'{"error":"sensitive error"}\n'], "endpoint returned an API error in the response body"),
    ([b"x" * (4 * 1024 * 1024 + 1)], "response exceeded the 4 MiB safety limit"),
    ([native_chunk(done=True)], "response contained no useful answer output"),
])
async def test_native_stream_failures_are_bounded_without_retry(monkeypatch, chunks, error):
    target, config = settings()
    calls = []
    source = NativeChunks(chunks)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=source)

    install_transport(monkeypatch, handler)
    async with LLMClient(target, config) as client:
        result = await OllamaColdClient(client).execute(request_spec())
    assert result.status == "failed" and not result.valid and result.error == error
    assert len(calls) == 1 and source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expected", [(503, "HTTP 503"), (401, "HTTP 401")])
async def test_native_http_failure_is_not_retried(monkeypatch, response, expected):
    target, config = settings()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(response, text="private diagnostic")

    install_transport(monkeypatch, handler)
    async with LLMClient(target, config) as client:
        result = await OllamaColdClient(client).execute(request_spec())
    assert result.status == "failed" and result.error == expected and len(calls) == 1


@pytest.mark.asyncio
async def test_native_timeout_preserves_existing_request_semantics(monkeypatch):
    target, config = settings()
    source = NativeChunks([native_chunk("42"), 20.0])
    install_transport(monkeypatch, lambda _: httpx.Response(200, stream=source))
    async with LLMClient(target, config) as client:
        result = await OllamaColdClient(client).execute(request_spec(request_timeout_seconds=.02))
    assert result.status == "timeout" and result.error == "request deadline exceeded"
    assert result.termination_reason == "request_deadline" and source.closed


@pytest.mark.asyncio
async def test_native_external_cancellation_closes_transport(monkeypatch):
    target, config = settings()
    source = NativeChunks([native_chunk("42"), 20.0])
    install_transport(monkeypatch, lambda _: httpx.Response(200, stream=source))
    signal = asyncio.Event()
    async with LLMClient(target, config) as client:
        task = asyncio.create_task(OllamaColdClient(client).execute(request_spec(), first_output_event=signal))
        await signal.wait()
        task.cancel()
        result = await task
    assert result.status == "cancelled" and result.error == "run cancelled"
    assert result.termination_reason == "external_cancellation" and source.closed
