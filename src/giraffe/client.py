"""Bounded OpenAI-compatible requests and observable answer-stream measurements."""

from __future__ import annotations

import asyncio
import codecs
import json
import hashlib
import os
import re
import ssl
import time
import uuid
from time import monotonic
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from .caps import (
    MAX_SERVER_ID_CHARS, MAX_TOOL_CALLS,
)
from giraffe.models import RequestRecord, RequestSpec, RunConfig, Target
from giraffe.logprobs import LogprobBudget

# A non-cooperative endpoint must not grow the test process's memory indefinitely.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_RESPONSE_EVENTS = 256
_RESERVED_OPTIONS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}
_MAX_CONTENT_READS = 65536
_MAX_OUTPUT_EVENTS = 4096
_REASONING_TAG = re.compile(r"<(/?)(think|thinking|reasoning)>", re.IGNORECASE)
REASONING_BUDGET_ERROR = "reasoning exhausted the output budget before an answer"


def no_response_failure(record: RequestRecord, *, include_timeouts=True) -> bool:
    """Headers do not prove the engine responded; body activity does."""
    if record.response_body_bytes or record.chunks or record.output or record.reasoning:
        return False
    if record.status == "timeout":
        return include_timeouts and record.error != "request timeout (PoolTimeout)"
    network_errors = (httpx.NetworkError, httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                      httpx.CloseError, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                      OSError, ConnectionError, ConnectionResetError, ConnectionRefusedError,
                      BrokenPipeError)
    return record.status == "failed" and record.error in {
        f"request failed ({kind.__name__})" for kind in network_errors}


class _ProtocolError(Exception):
    pass


class _IntentionalCancellation(Exception):
    pass


def completion_url(url: str) -> str:
    """Accept a server origin, API base, or complete chat-completions endpoint."""
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if httpx.URL(url).path in {"", "/"}:
        url += "/v1"
    return url + "/chat/completions"


def _headers(target: Target) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream"}
    if target._api_key is not None:
        headers["Authorization"] = f"Bearer {target._api_key.get_secret_value()}"
    elif target.api_key_env:
        value = os.environ.get(target.api_key_env)
        if not value:
            raise ValueError(f"Missing API key environment variable: {target.api_key_env}")
        headers["Authorization"] = f"Bearer {value}"
    for header, name in target.headers_env.items():
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"Missing header environment variable: {name}")
        headers[header] = value
    return headers


async def _limited_body(response: httpx.Response, *, on_read=None, on_body=None) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if chunk and on_body is not None:
            on_body(len(chunk))
        if on_read is not None:
            on_read(time.perf_counter())
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise _ProtocolError("response exceeded the 4 MiB safety limit")
        body.extend(chunk)
    return bytes(body)


async def _sse_events(response: httpx.Response, *, on_read=None) -> AsyncIterator[tuple[str, str, float]]:
    """Decode SSE independently of TCP boundaries, including CRLF and multiline data."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    data: list[str] = []
    event_type = "message"
    consumed = 0

    async def chunks_with_eof() -> AsyncIterator[tuple[bytes, bool]]:
        async for chunk in response.aiter_bytes():
            if chunk and on_read is not None:
                on_read(len(chunk))
            yield chunk, False
        yield b"", True

    async for chunk, final in chunks_with_eof():
        observed_at = time.perf_counter()
        consumed += len(chunk)
        if consumed > _MAX_RESPONSE_BYTES:
            raise _ProtocolError("response exceeded the 4 MiB safety limit")
        buffer += decoder.decode(chunk, final=final)
        while (match := re.search(r"[\r\n]", buffer)) is not None:
            end = match.start()
            # A CR at the end might be the first half of a fragmented CRLF.
            if buffer[end] == "\r" and end == len(buffer) - 1 and not final:
                break
            width = 2 if buffer[end:end + 2] == "\r\n" else 1
            line, buffer = buffer[:end], buffer[end + width:]
            if not line:
                if data:
                    yield event_type, "\n".join(data), observed_at
                data, event_type = [], "message"
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event_type = value
    if buffer or data:
        raise _ProtocolError("unterminated SSE event at end of response")


def _object(payload: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _ProtocolError("malformed JSON in response") from exc
    if not isinstance(value, dict):
        raise _ProtocolError("response must contain a JSON object")
    if value.get("error") is not None:
        # Arbitrary endpoint messages can include prompt content or credentials.
        raise _ProtocolError("endpoint returned an API error in the response body")
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _ProtocolError("response content must be text")
    return value


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _answer_spans(output: str) -> list[tuple[int, int]] | None:
    """Raw-output spans that fixtures.final_answer keeps; None when reasoning is unclosed."""
    cursor, spans, stack = 0, [], []
    for tag in _REASONING_TAG.finditer(output):
        name = tag[2].lower()
        if not tag[1]:
            if not stack:
                spans.append((cursor, tag.start()))
            stack.append(name)
        elif stack:
            if stack[-1] == name:
                stack.pop()
                if not stack:
                    cursor = tag.end()
        else:
            spans, cursor = [], tag.end()
    return None if stack else [*spans, (cursor, len(output))]


def _reasoning_spans(spans: list[tuple[int, int]] | None, length: int) -> list[tuple[int, int]]:
    start, result = 0, []
    for a, b in spans or ():
        result.append((start, a))
        start = b
    return [*result, (start, length)]


class LLMClient:
    reports_response_activity = True

    def __init__(self, target: Target, config: RunConfig, *, max_connections: int | None = None):
        self.target = target
        self.config = config
        if max_connections is None:
            from .models import required_concurrency
            required = required_concurrency(config)
            pool = min(required["peak"], config.concurrency) if config.concurrency is not None else required["peak"]
            max_connections = min(required["per_target_peak"], pool)
        self.max_connections = max_connections
        self.logprob_budget = LogprobBudget()
        self._client: httpx.AsyncClient | None = None
        self.on_response = None

    def _create_client(self) -> httpx.AsyncClient:
        # HTTPX loads TLS certificates even for HTTP endpoints. Keep that
        # synchronous setup away from concurrent inference and the heartbeat.
        headers = _headers(self.target)
        verify: bool | ssl.SSLContext = True
        if self.config.ca_bundle:
            verify = ssl.create_default_context(cafile=self.config.ca_bundle)
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(None, connect=min(self.config.request_timeout_seconds * .9, 10)),
            verify=verify, proxy=self.config.proxy,
            trust_env=True, follow_redirects=False,
            limits=httpx.Limits(max_connections=self.max_connections,
                                max_keepalive_connections=self.max_connections),
        )

    async def __aenter__(self) -> LLMClient:
        self._client = await asyncio.to_thread(self._create_client)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ready(self, deadline: float) -> bool:
        """Poll the advertised model route without spending an inference request."""
        if self._client is None:
            raise RuntimeError("Use LLMClient as an async context manager")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(remaining):
                url = completion_url(self.target.url).removesuffix("/chat/completions") + "/models"
                async with self._client.stream("GET", url, timeout=min(5, remaining)) as response:
                    status = response.status_code
                if status == 404:
                    origin = httpx.URL(self.target.url).copy_with(path="/health")
                    async with self._client.stream("GET", origin, timeout=min(5, remaining)) as response:
                        status = response.status_code
                return 200 <= status < 300
        except (httpx.HTTPError, OSError, TimeoutError):
            return False

    async def execute(
        self, spec: RequestSpec, *, first_output_event: asyncio.Event | None = None,
    ) -> RequestRecord:
        if self._client is None:
            raise RuntimeError("Use LLMClient as an async context manager")
        preparation_start = time.perf_counter()
        stream = self.config.stream and spec.stream
        budget = spec.max_tokens if spec.max_tokens is not None else self.config.max_output_tokens
        record = RequestRecord(
            id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids,
            started_at=datetime.now(timezone.utc).isoformat(), status="failed",
            input_chars=spec.input_chars, requested_max_tokens=budget, stream=stream, answer_reads=0,
            timing_schema=1, first_token_reason=(
                "No qualifying streamed generated-content event was observed." if stream else
                "Non-streamed replies do not provide first-token arrival timing."),
        )
        payload = {
            key: value for key, value in (self.config.request_options | spec.options).items()
            if key not in _RESERVED_OPTIONS and value is not None
        }
        payload.update(model=self.target.model, messages=spec.messages, stream=stream,
                       max_tokens=budget, n=1)
        if spec.scorer == "tool":
            if spec.tool_expectation and spec.tool_expectation.choice:
                payload["tools"] = spec.tool_expectation.tools
                payload.pop("parallel_tool_calls", None)
                payload.update(spec.tool_expectation.choice.request_options())
            record.tool_request_options = {key: payload[key] for key in
                ("tools", "tool_choice", "parallel_tool_calls") if key in payload}
        if stream:
            payload.setdefault("stream_options", {"include_usage": True})
        record.workload = dict(spec.workload)

        def body_received(size):
            record.response_body_bytes += size
            if self.on_response is not None:
                self.on_response()
        record.request_hash = hashlib.sha256(json.dumps({
            "messages": spec.messages, "options": {k: v for k, v in payload.items() if k not in {"model", "messages"}},
        }, sort_keys=True).encode()).hexdigest()
        record.history_hash = hashlib.sha256(json.dumps(spec.messages[:-1], sort_keys=True).encode()).hexdigest()
        start = time.perf_counter()
        record._timing_started_monotonic = monotonic()
        record.client_preparation_ms = (start - preparation_start) * 1000
        tool_parts: dict[int, dict] = {}
        last_answer: float | None = None
        tool_output: float | None = None
        # (output offset, read time) per network read carrying content; bounded.
        content_reads: list[tuple[int, float, int]] = []
        reads_truncated = False
        read_id = 0
        reasoning_read_ids: set[int] = set()
        content_event_spans: list[tuple[dict[str, Any], int, int]] = []
        response_sequence = -1
        conversation = spec.workload.get("selection") == "conversation"
        assistant_content_present = False
        assistant_extras: dict[str, list[str]] = {}

        def stream_body_received(size):
            nonlocal read_id
            read_id += 1
            body_received(size)

        def output_event(kind: str, text: str, at: float, *, tokens=None):
            # Client receipt time, not server generation time. Tool-only TTFT
            # starts at generated argument content, not ID/type/function names.
            # No tokenization or text retention is added to the stream reader.
            # A nonempty whitespace delta is still generated content for TTFT;
            # useful-answer timing continues to require non-whitespace output.
            if not stream or not text:
                return
            if record.first_token_ms is None:
                record.first_token_ms = (at - start) * 1000
                record.first_token_reason = None
            if kind == "reasoning" and read_id not in reasoning_read_ids:
                if len(record.reasoning_read_timestamps_ms) < _MAX_CONTENT_READS:
                    record.reasoning_read_timestamps_ms.append((at - start) * 1000)
                    reasoning_read_ids.add(read_id)
                else:
                    record.read_timestamps_truncated = True
            if len(record.output_events) >= _MAX_OUTPUT_EVENTS:
                record.output_events_truncated = True
                return
            event = {"sequence": response_sequence, "read_id": read_id,
                "at_ms": (at - start) * 1000, "kind": kind, "chars": len(text),
                "token_count": tokens,
                "token_count_source": "choice.logprobs.content" if tokens is not None else None}
            record.output_events.append(event)
            return event

        def trace(kind: str, **metadata: int | bool) -> None:
            # Sequence identifies a response choice, not field order within its JSON.
            # Text and calls in the same delta have the same sequence. Retain only
            # bounded structural metadata; prompt/answer fragments and IDs stay out.
            if spec.scorer != "tool":
                return
            if len(record.response_events) >= _MAX_RESPONSE_EVENTS:
                record.response_events_truncated = True
                return
            record.response_events.append({"sequence": response_sequence, "kind": kind, **metadata})

        def receive(data: dict[str, Any], *, streaming: bool, observed_at: float) -> None:
            nonlocal last_answer, response_sequence, assistant_content_present, tool_output, reads_truncated
            server_id = data.get("id")
            if (spec.cancel_after_chunks is not None and isinstance(server_id, str)
                    and len(server_id) > MAX_SERVER_ID_CHARS):
                raise _ProtocolError("completion request ID exceeded the safety limit")
            if isinstance(server_id, str) and 0 < len(server_id) <= MAX_SERVER_ID_CHARS:
                if (spec.cancel_after_chunks is not None and record.server_request_id is not None
                        and server_id != record.server_request_id):
                    raise _ProtocolError("completion request ID changed during the response")
                record.server_request_id = server_id
            model = data.get("model")
            if isinstance(model, str) and model:
                record.observed_model = model
            backend = data.get("backend_id")
            if isinstance(backend, str) and backend:
                record.backend_id = backend
            usage = data.get("usage")
            if isinstance(usage, dict):
                record.input_tokens = _token_count(usage.get("prompt_tokens"))
                record.completion_tokens = _token_count(usage.get("completion_tokens"))
                record.output_tokens = record.completion_tokens
                cached = usage.get("prompt_tokens_details")
                if isinstance(cached, dict):
                    record.cached_prompt_tokens = _token_count(cached.get("cached_tokens"))
                    if record.cached_prompt_tokens is not None:
                        record.cache_source = "usage.prompt_tokens_details.cached_tokens"
                details = usage.get("completion_tokens_details")
                reasoning_tokens = _token_count(details.get("reasoning_tokens")) if isinstance(
                    details, dict,
                ) else None
                record.reasoning_tokens = reasoning_tokens
                if record.output_tokens is not None and reasoning_tokens is not None:
                    record.output_tokens = max(0, record.output_tokens - reasoning_tokens)
            choices = data.get("choices")
            if not isinstance(choices, list) or (not choices and not isinstance(usage, dict)):
                raise _ProtocolError("response is missing completion choices")
            if not choices:
                return
            if len(choices) != 1 or not isinstance(choices[0], dict):
                raise _ProtocolError("response must contain one completion choice")
            choice = choices[0]
            if "index" in choice and (type(choice["index"]) is not int or choice["index"] != 0):
                raise _ProtocolError("invalid completion choice index")
            response_sequence += 1
            content = choice.get("delta" if streaming else "message")
            if not isinstance(content, dict):
                raise _ProtocolError("response is missing assistant content")
            if conversation:
                if content.get("role", "assistant") != "assistant":
                    raise _ProtocolError("conversation response must have assistant role")
                if set(content) - {"role", "content", "tool_calls", "reasoning_content", "reasoning", "refusal"}:
                    raise _ProtocolError("unsupported assistant field in conversation")
                for key in ("reasoning_content", "reasoning", "refusal"):
                    if content.get(key) is not None:
                        value = _text(content[key])
                        if streaming:
                            assistant_extras.setdefault(key, []).append(value)
                if not streaming:
                    record.assistant_message = {"role": "assistant", **content}
                else:
                    assistant_content_present |= isinstance(content.get("content"), str)
            answer = _text(content.get("content"))
            if spec.workload.get("logprobs") and not streaming:
                record.logprob_evidence = self.logprob_budget.collect(choice.get("logprobs"), answer,
                                                          spec.workload["top_logprobs"])
            reasoning = _text(content.get("reasoning_content") or content.get("reasoning"))
            now = observed_at
            if streaming:
                token_evidence = choice.get("logprobs")
                token_parts = token_evidence.get("content") if isinstance(token_evidence, dict) else None
                tokens = None
                if (isinstance(token_parts, list) and token_parts
                        and all(isinstance(part, dict) and isinstance(part.get("token"), str)
                                for part in token_parts)
                        and "".join(part["token"] for part in token_parts) == answer):
                    tokens = len(token_parts)
                content_event = output_event("answer", _REASONING_TAG.sub("", answer), now, tokens=tokens)
                if content_event is not None:
                    content_event_spans.append((content_event, len(record.output), len(record.output) + len(answer)))
                output_event("reasoning", reasoning, now)
            if streaming and (_REASONING_TAG.sub("", answer).strip() or reasoning.strip()):
                record.generation_chunks += 1
            calls = content.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
                    raise _ProtocolError("invalid tool-call structure or count")
                for ordinal, call in enumerate(calls):
                    if not isinstance(call, dict):
                        raise _ProtocolError("invalid tool-call structure")
                    if conversation and set(call) - {"id", "type", "index", "function"}:
                        raise _ProtocolError("unsupported tool-call field in conversation")
                    index = call.get("index") if streaming else ordinal
                    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 16:
                        raise _ProtocolError("invalid tool-call index")
                    part = tool_parts.setdefault(index, {"id": "", "type": "", "function": {"name": "", "arguments": ""}})
                    if record.finish_reason is not None:
                        raise _ProtocolError("tool-call content arrived after finish reason")
                    for key in ("id", "type"):
                        if call.get(key) is not None:
                            value = _text(call[key])
                            if part[key] and part[key] != value:
                                raise _ProtocolError("conflicting tool-call ID/type")
                            part[key] = value
                    function = call.get("function", {})
                    if not isinstance(function, dict):
                        raise _ProtocolError("invalid tool-call function")
                    if conversation and set(function) - {"name", "arguments"}:
                        raise _ProtocolError("unsupported tool function field in conversation")
                    name = _text(function.get("name"))
                    arguments = _text(function.get("arguments"))
                    trace("tool_call", index=index, name_length=len(name),
                          arguments_length=len(arguments), id_present=call.get("id") is not None,
                          type_present=call.get("type") is not None)
                    part["function"]["name"] += name
                    part["function"]["arguments"] += arguments
                    if streaming:
                        output_event("tool_arguments", arguments, now)
                    if tool_output is None and (function.get("name") or function.get("arguments")):
                        tool_output = now
                    if record.first_output_ms is None and (function.get("name") or function.get("arguments")):
                        record.first_output_ms = (now-start)*1000
                        if first_output_event is not None:
                            first_output_event.set()
            if answer:
                if record.finish_reason is not None:
                    raise _ProtocolError("answer content arrived after the finish reason")
                trace("content", offset=len(record.output), length=len(answer))
                if not content_reads or content_reads[-1][2] != read_id:
                    if len(content_reads) < _MAX_CONTENT_READS:
                        content_reads.append((len(record.output), now, read_id))
                    else:
                        reads_truncated = True
                record.output += answer
                if answer.strip():
                    if record.first_output_ms is None:
                        record.first_output_ms = (now - start) * 1000
                        if first_output_event is not None:
                            first_output_event.set()
                    if streaming and last_answer is not None:
                        record.max_stream_gap_ms = max(
                            record.max_stream_gap_ms or 0, (now - last_answer) * 1000,
                        )
                    last_answer = now
                    if streaming:
                        record.answer_chunks += 1
                        if first_output_event is not None and hasattr(first_output_event, "on_answer_chunk"):
                            first_output_event.on_answer_chunk()
                if record.first_output_ms is not None:
                    record.last_output_ms = (now - start) * 1000
            if reasoning:
                record.reasoning += reasoning
                if reasoning.strip() and record.first_reasoning_ms is None:
                    record.first_reasoning_ms = (now - start) * 1000
            if streaming:
                record.chunks += 1
            finish = choice.get("finish_reason")
            if finish is not None:
                if not isinstance(finish, str) or not finish:
                    raise _ProtocolError("invalid completion finish reason")
                if record.finish_reason is not None:
                    raise _ProtocolError("repeated completion finish reason")
                record.finish_reason = finish

        request_timeout = (spec.request_timeout_seconds if spec.request_timeout_seconds is not None
                           else self.config.request_timeout_seconds)
        intentional = spec.cancel_after_ms is not None and (
            spec.cancel_after_ms / 1000 < request_timeout
        )
        timeout = min(request_timeout,
                      spec.cancel_after_ms / 1000 if spec.cancel_after_ms else float("inf"))
        try:
            async with asyncio.timeout(timeout):
                async with self._client.stream("POST", completion_url(self.target.url),
                                               json=payload, timeout=httpx.Timeout(
                                                   None, connect=min(request_timeout * .9, 10))) as response:
                    record.http_status = response.status_code
                    record.backend_id = response.headers.get("x-backend-id") or response.headers.get(
                        "x-replica-id",
                    )
                    if not 200 <= response.status_code < 300:
                        error_body = await _limited_body(response, on_body=body_received)
                        if spec.workload.get("logprobs"):
                            from giraffe.models import LogprobEvidence
                            try:
                                error_data = json.loads(error_body).get("error", {})
                                rejected = response.status_code in {400, 422} and isinstance(error_data, dict) and error_data.get("param") in {"logprobs", "top_logprobs"}
                            except (ValueError, AttributeError, TypeError):
                                rejected = False
                            record.logprob_evidence = LogprobEvidence(status="rejected" if rejected else "incomplete",
                                reason="Logprob request field rejected." if rejected else "HTTP request failed; logprob support unverified.",
                                requested_top_logprobs=spec.workload["top_logprobs"])
                        if response.status_code in {400, 413, 422} and spec.scenario.startswith(("context", "prefix_")):
                            # Classify only documented engine rejections (vLLM/SGLang, TGI, llama.cpp);
                            # never retain arbitrary server error text or echoed request content.
                            body = error_body.decode("utf-8", errors="replace").lower()
                            if any(term in body for term in (
                                    "context length", "context_length", "maximum context", "too many tokens",
                                    "context size", "context window", "max_model_len",
                                    "inputs` tokens + `max_new_tokens`", "inputs` must have less than")):
                                raise _ProtocolError(f"HTTP {response.status_code}: context length rejection")
                        raise _ProtocolError(f"HTTP {response.status_code}")
                    if stream:
                        if "application/json" in response.headers.get("content-type", ""):
                            _object(await _limited_body(response, on_body=body_received))
                            raise _ProtocolError("endpoint returned JSON instead of an SSE stream")
                        async for event_type, event, observed_at in _sse_events(response, on_read=stream_body_received):
                            if event_type == "error":
                                raise _ProtocolError("endpoint returned an SSE error event")
                            if event.strip() == "[DONE]":
                                record.stream_end_ms = (observed_at - start) * 1000
                                record.stream_terminated = True
                                break
                            receive(_object(event), streaming=True, observed_at=observed_at)
                            if (spec.cancel_after_chunks is not None
                                    and record.generation_chunks >= spec.cancel_after_chunks
                                    and record.finish_reason is None):
                                record.client_cancelled_ms = (time.perf_counter() - start) * 1000
                                record.client_cancelled_at_ns = time.time_ns()
                                record.client_cancelled_monotonic_ns = time.monotonic_ns()
                                raise _IntentionalCancellation
                        if not record.stream_terminated:
                            raise _ProtocolError("SSE stream ended without [DONE]")
                    else:
                        def body_read(at):
                            record.stream_end_ms = (at - start) * 1000
                        body = await _limited_body(response, on_read=body_read, on_body=body_received)
                        observed_at = start + record.stream_end_ms / 1000 if record.stream_end_ms is not None else time.perf_counter()
                        receive(_object(body), streaming=False, observed_at=observed_at)
                        record.stream_terminated = True
                    if record.finish_reason is None:
                        raise _ProtocolError("response ended without a completion finish reason")
                    if record.finish_reason in {"error", "content_filter"}:
                        raise _ProtocolError("endpoint did not finish a usable answer")
                    record.tool_calls = [tool_parts[index] for index in sorted(tool_parts)]
                    if spec.scorer == "tool" and record.tool_calls:
                        ids = [call["id"] for call in record.tool_calls]
                        if any(not call["id"] or call["type"] != "function" or not call["function"]["name"] for call in record.tool_calls) or len(set(ids)) != len(ids):
                            raise _ProtocolError("incomplete tool-call ID/type/function structure")
                    from .fixtures import final_answer
                    final, stripped, _ = final_answer(record.output)
                    if spec.scorer != "tool" and not final.strip():
                        if record.finish_reason == "length" and (record.reasoning.strip() or stripped
                                                                  or (record.reasoning_tokens or 0) > 0):
                            # The endpoint honoured Giraffe's own output budget: not a
                            # serving failure, but also not an answer.
                            record.status, record.error = "completed", REASONING_BUDGET_ERROR
                            return record
                        raise _ProtocolError("response contained no useful answer output")
                    if record.first_output_ms is None:
                        raise _ProtocolError("response contained no useful answer output")
                    if conversation and stream:
                        record.assistant_message = {"role": "assistant",
                            "content": record.output if assistant_content_present else None,
                            **{key: "".join(parts) for key, parts in assistant_extras.items()}}
                    if conversation and record.tool_calls:
                        record.assistant_message["tool_calls"] = record.tool_calls
                    record.status, record.valid = "completed", True
        except _IntentionalCancellation:
            record.status, record.error = "cancelled", "intentional cancellation after generation chunks"
        except TimeoutError:
            record.status = "cancelled" if intentional else "timeout"
            record.error = "intentional cancellation" if intentional else "request deadline exceeded"
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            record.status, record.error = "failed", f"request failed ({type(exc).__name__})"
        except httpx.TimeoutException as exc:
            record.status, record.error = "timeout", f"request timeout ({type(exc).__name__})"
        except asyncio.CancelledError:
            record.status, record.error = "cancelled", "run cancelled"
        except _ProtocolError as exc:
            record.error = str(exc)
        except UnicodeDecodeError:
            record.error = "response contains invalid UTF-8"
        except (httpx.HTTPError, OSError, ValueError) as exc:
            record.error = f"request failed ({type(exc).__name__})"
        finally:
            now = time.perf_counter()
            record.elapsed_ms = (now - start) * 1000
            record.output_chars = len(record.output)
            record.tool_calls = [tool_parts[index] for index in sorted(tool_parts)]
            # Answer timing excludes inline reasoning and counts network reads,
            # not SSE events: one buffered read carries no gap or pace evidence.
            from .fixtures import final_answer
            final, stripped, _ = final_answer(record.output)
            record.answer_chars = len(final)
            spans = _answer_spans(record.output)
            if (record.reasoning.strip() or stripped) and not record.reasoning_tokens:
                record.output_tokens = None
            record.reasoning_stripped = record.reasoning_stripped or stripped
            reasoning_spans = _reasoning_spans(spans, len(record.output))
            # Inline reasoning is known only once all fragments/tags have been
            # parsed. Reclassify bounded metadata without retaining extra text.
            if stripped:
                for event, offset, end in content_event_spans:
                    answer_text = "".join(record.output[max(a, offset):min(b, end)] for a, b in spans or ())
                    reasoning_text = "".join(record.output[max(a, offset):min(b, end)] for a, b in reasoning_spans)
                    has_answer = bool(answer_text.strip())
                    has_reasoning = bool(_REASONING_TAG.sub("", reasoning_text).strip())
                    event["kind"] = ("answer_and_reasoning" if has_answer and has_reasoning else
                                     "answer" if has_answer else "reasoning")
                    event["token_count"] = event["token_count_source"] = None
            first_read = last_read = None
            gap = None
            for index, (offset, at, content_read_id) in enumerate(content_reads):
                end = content_reads[index + 1][0] if index + 1 < len(content_reads) else len(record.output)
                text = "".join(record.output[max(a, offset):min(b, end)] for a, b in spans or ())
                has_reasoning = stripped and any(
                    _REASONING_TAG.sub("", record.output[max(offset, a):min(end, b)]).strip()
                    for a, b in reasoning_spans)
                if has_reasoning and stream and content_read_id not in reasoning_read_ids:
                    if len(record.reasoning_read_timestamps_ms) < _MAX_CONTENT_READS:
                        record.reasoning_read_timestamps_ms.append((at - start) * 1000)
                        reasoning_read_ids.add(content_read_id)
                    else:
                        record.read_timestamps_truncated = True
                if (stripped and record.first_reasoning_ms is None
                        and has_reasoning):
                    record.first_reasoning_ms = (at - start) * 1000
                if not text.strip():
                    continue
                if stream:
                    record.answer_read_timestamps_ms.append((at - start) * 1000)
                record.answer_reads += 1
                if first_read is None:
                    first_read, record.first_read_answer_chars = at, len(text)
                else:
                    gap = max(gap or 0, (at - last_read) * 1000)
                last_read = at
            if reads_truncated:
                record.read_timestamps_truncated = True
                record.answer_reads = 0
                record.first_read_answer_chars = 0
                record.last_output_ms = record.max_stream_gap_ms = None
                if stripped:
                    record.first_output_ms = None
            else:
                if stream:
                    record.max_stream_gap_ms = gap
                if stripped:
                    candidates = [value for value in (tool_output, first_read) if value is not None]
                    record.first_output_ms = (min(candidates) - start) * 1000 if candidates else None
                if tool_output is None:
                    record.last_output_ms = (last_read - start) * 1000 if last_read is not None else None
            record.reasoning_read_timestamps_ms.sort()
            if record.valid and record.status == "completed":
                answer = final
                if record.workload.get("equality") == "whitespace":
                    answer = " ".join(answer.split())
                record.answer_hash = hashlib.sha256(answer.encode()).hexdigest()
        return record
