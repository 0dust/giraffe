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

from giraffe.models import RequestRecord, RequestSpec, RunConfig, Target
from giraffe.logprobs import LogprobBudget

# A non-cooperative endpoint must not grow the test process's memory indefinitely.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_RESPONSE_EVENTS = 256
_RESERVED_OPTIONS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}


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
    if target.api_key_env:
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


async def _limited_body(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise _ProtocolError("response exceeded the 4 MiB safety limit")
        body.extend(chunk)
    return bytes(body)


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str, float]]:
    """Decode SSE independently of TCP boundaries, including CRLF and multiline data."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    data: list[str] = []
    event_type = "message"
    consumed = 0

    async def chunks_with_eof() -> AsyncIterator[tuple[bytes, bool]]:
        async for chunk in response.aiter_bytes():
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


class LLMClient:
    def __init__(self, target: Target, config: RunConfig):
        self.target = target
        self.config = config
        self.logprob_budget = LogprobBudget()
        self._client: httpx.AsyncClient | None = None

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
            limits=httpx.Limits(max_connections=self.config.concurrency),
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
                response = await self._client.get(completion_url(self.target.url).removesuffix("/chat/completions") + "/models",
                                                  timeout=min(5, remaining))
                if response.status_code == 404:
                    origin = httpx.URL(self.target.url).copy_with(path="/health")
                    response = await self._client.get(origin, timeout=min(5, remaining))
                return response.is_success
        except (httpx.HTTPError, OSError, TimeoutError):
            return False

    async def execute(
        self, spec: RequestSpec, *, first_output_event: asyncio.Event | None = None,
    ) -> RequestRecord:
        if self._client is None:
            raise RuntimeError("Use LLMClient as an async context manager")
        preparation_start = time.perf_counter()
        stream = self.config.stream and spec.stream
        budget = min(spec.max_tokens or self.config.max_output_tokens, self.config.max_output_tokens)
        record = RequestRecord(
            id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids,
            started_at=datetime.now(timezone.utc).isoformat(), status="failed",
            input_chars=spec.input_chars, requested_max_tokens=budget, stream=stream,
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
        record.request_hash = hashlib.sha256(json.dumps({
            "messages": spec.messages, "options": {k: v for k, v in payload.items() if k not in {"model", "messages"}},
        }, sort_keys=True).encode()).hexdigest()
        record.history_hash = hashlib.sha256(json.dumps(spec.messages[:-1], sort_keys=True).encode()).hexdigest()
        start = time.perf_counter()
        record._timing_started_monotonic = monotonic()
        record.client_preparation_ms = (start - preparation_start) * 1000
        tool_parts: dict[int, dict] = {}
        last_answer: float | None = None
        response_sequence = -1
        conversation = spec.workload.get("selection") == "conversation"
        assistant_content_present = False
        assistant_extras: dict[str, list[str]] = {}

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
            nonlocal last_answer, response_sequence, assistant_content_present
            server_id = data.get("id")
            if (spec.cancel_after_chunks is not None and isinstance(server_id, str)
                    and len(server_id) > 4096):
                raise _ProtocolError("completion request ID exceeded the safety limit")
            if isinstance(server_id, str) and 0 < len(server_id) <= 4096:
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
            calls = content.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list) or len(calls) > 16:
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
                    if record.first_output_ms is None and (function.get("name") or function.get("arguments")):
                        record.first_output_ms = (now-start)*1000
                        if first_output_event is not None:
                            first_output_event.set()
            if answer:
                if record.finish_reason is not None:
                    raise _ProtocolError("answer content arrived after the finish reason")
                trace("content", offset=len(record.output), length=len(answer))
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
                if record.first_output_ms is not None:
                    record.last_output_ms = (now - start) * 1000
            if reasoning:
                record.reasoning += reasoning
                if record.first_reasoning_ms is None:
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

        request_timeout = min(self.config.request_timeout_seconds,
                              spec.request_timeout_seconds or self.config.request_timeout_seconds)
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
                        if spec.workload.get("logprobs"):
                            from giraffe.models import LogprobEvidence
                            error_body = await _limited_body(response)
                            try:
                                error_data = json.loads(error_body).get("error", {})
                                rejected = response.status_code in {400, 422} and isinstance(error_data, dict) and error_data.get("param") in {"logprobs", "top_logprobs"}
                            except (ValueError, AttributeError, TypeError):
                                rejected = False
                            record.logprob_evidence = LogprobEvidence(status="rejected" if rejected else "incomplete",
                                reason="Logprob request field rejected." if rejected else "HTTP request failed; logprob support unverified.",
                                requested_top_logprobs=spec.workload["top_logprobs"])
                        if response.status_code in {400, 413, 422} and spec.scenario.startswith("context"):
                            # Classify only documented engine rejections (vLLM/SGLang, TGI, llama.cpp);
                            # never retain arbitrary server error text or echoed request content.
                            body = (await _limited_body(response)).decode("utf-8", errors="replace").lower()
                            if any(term in body for term in (
                                    "context length", "context_length", "maximum context", "too many tokens",
                                    "context size", "context window", "max_model_len",
                                    "inputs` tokens + `max_new_tokens`", "inputs` must have less than")):
                                raise _ProtocolError(f"HTTP {response.status_code}: context length rejection")
                        raise _ProtocolError(f"HTTP {response.status_code}")
                    if stream:
                        if "application/json" in response.headers.get("content-type", ""):
                            _object(await _limited_body(response))
                            raise _ProtocolError("endpoint returned JSON instead of an SSE stream")
                        async for event_type, event, observed_at in _sse_events(response):
                            if event_type == "error":
                                raise _ProtocolError("endpoint returned an SSE error event")
                            if event.strip() == "[DONE]":
                                record.stream_terminated = True
                                break
                            receive(_object(event), streaming=True, observed_at=observed_at)
                            if (spec.cancel_after_chunks is not None
                                    and record.answer_chunks >= spec.cancel_after_chunks
                                    and record.finish_reason is None):
                                record.client_cancelled_at_ns = time.time_ns()
                                record.client_cancelled_monotonic_ns = time.monotonic_ns()
                                raise _IntentionalCancellation
                        if not record.stream_terminated:
                            raise _ProtocolError("SSE stream ended without [DONE]")
                    else:
                        body = await _limited_body(response)
                        observed_at = time.perf_counter()
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
                    if record.first_output_ms is None or (not record.output.strip() and spec.scorer != "tool"):
                        raise _ProtocolError("response contained no useful answer output")
                    if conversation and stream:
                        record.assistant_message = {"role": "assistant",
                            "content": record.output if assistant_content_present else None,
                            **{key: "".join(parts) for key, parts in assistant_extras.items()}}
                    if conversation and record.tool_calls:
                        record.assistant_message["tool_calls"] = record.tool_calls
                    record.status, record.valid = "completed", True
        except _IntentionalCancellation:
            record.status, record.error = "cancelled", "intentional cancellation after answer chunks"
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
            if record.valid and record.status == "completed":
                from .fixtures import final_answer
                answer, _, _ = final_answer(record.output)
                if record.workload.get("equality") == "whitespace":
                    answer = " ".join(answer.split())
                record.answer_hash = hashlib.sha256(answer.encode()).hexdigest()
        return record
