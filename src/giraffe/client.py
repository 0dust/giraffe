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
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from giraffe.models import RequestRecord, RequestSpec, RunConfig, Target

# A non-cooperative endpoint must not grow the test process's memory indefinitely.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_RESERVED_OPTIONS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}


class _ProtocolError(Exception):
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


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
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
                    yield event_type, "\n".join(data)
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
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LLMClient:
        headers = _headers(self.target)
        verify: bool | ssl.SSLContext = True
        if self.config.ca_bundle:
            verify = ssl.create_default_context(cafile=self.config.ca_bundle)
        self._client = httpx.AsyncClient(
            headers=headers, timeout=None, verify=verify, proxy=self.config.proxy,
            trust_env=True, follow_redirects=False,
            limits=httpx.Limits(max_connections=self.config.concurrency),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def execute(
        self, spec: RequestSpec, *, first_output_event: asyncio.Event | None = None,
    ) -> RequestRecord:
        if self._client is None:
            raise RuntimeError("Use LLMClient as an async context manager")
        start = time.perf_counter()
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
            if key not in _RESERVED_OPTIONS
        }
        payload.update(model=self.target.model, messages=spec.messages, stream=stream,
                       max_tokens=budget, n=1)
        if stream:
            payload.setdefault("stream_options", {"include_usage": True})
        record.workload = dict(spec.workload)
        record.request_hash = hashlib.sha256(json.dumps({
            "messages": spec.messages, "options": {k: v for k, v in payload.items() if k not in {"model", "messages"}},
        }, sort_keys=True).encode()).hexdigest()
        record.history_hash = hashlib.sha256(json.dumps(spec.messages[:-1], sort_keys=True).encode()).hexdigest()
        tool_parts: dict[int, dict] = {}
        last_answer: float | None = None

        def receive(data: dict[str, Any], *, streaming: bool) -> None:
            nonlocal last_answer
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
            content = choice.get("delta" if streaming else "message")
            if not isinstance(content, dict):
                raise _ProtocolError("response is missing assistant content")
            answer = _text(content.get("content"))
            reasoning = _text(content.get("reasoning_content") or content.get("reasoning"))
            now = time.perf_counter()
            calls = content.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list) or len(calls) > 16:
                    raise _ProtocolError("invalid tool-call structure or count")
                for ordinal, call in enumerate(calls):
                    if not isinstance(call, dict):
                        raise _ProtocolError("invalid tool-call structure")
                    index = call.get("index", ordinal) if streaming else ordinal
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
                    for key in ("name", "arguments"):
                        part["function"][key] += _text(function.get(key))
                    if record.first_output_ms is None and (function.get("name") or function.get("arguments")):
                        record.first_output_ms = (now-start)*1000
                        if first_output_event is not None:
                            first_output_event.set()
            if answer:
                if record.finish_reason is not None:
                    raise _ProtocolError("answer content arrived after the finish reason")
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
                record.finish_reason = finish

        intentional = spec.cancel_after_ms is not None and (
            spec.cancel_after_ms / 1000 < self.config.request_timeout_seconds
        )
        timeout = min(self.config.request_timeout_seconds,
                      spec.cancel_after_ms / 1000 if spec.cancel_after_ms else float("inf"))
        try:
            async with asyncio.timeout(timeout):
                async with self._client.stream("POST", completion_url(self.target.url),
                                               json=payload) as response:
                    record.http_status = response.status_code
                    record.backend_id = response.headers.get("x-backend-id") or response.headers.get(
                        "x-replica-id",
                    )
                    if not 200 <= response.status_code < 300:
                        raise _ProtocolError(f"HTTP {response.status_code}")
                    if stream:
                        if "application/json" in response.headers.get("content-type", ""):
                            _object(await _limited_body(response))
                            raise _ProtocolError("endpoint returned JSON instead of an SSE stream")
                        async for event_type, event in _sse_events(response):
                            if event_type == "error":
                                raise _ProtocolError("endpoint returned an SSE error event")
                            if event.strip() == "[DONE]":
                                record.stream_terminated = True
                                break
                            receive(_object(event), streaming=True)
                        if not record.stream_terminated:
                            raise _ProtocolError("SSE stream ended without [DONE]")
                    else:
                        receive(_object(await _limited_body(response)), streaming=False)
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
                    record.status, record.valid = "completed", True
        except TimeoutError:
            record.status = "cancelled" if intentional else "timeout"
            record.error = "intentional cancellation" if intentional else "request deadline exceeded"
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
                answer = record.output
                if record.workload.get("equality") == "whitespace":
                    answer = " ".join(answer.split())
                record.answer_hash = hashlib.sha256(answer.encode()).hexdigest()
            if stream and last_answer is not None:
                record.max_stream_gap_ms = max(
                    record.max_stream_gap_ms or 0, (now - last_answer) * 1000,
                )
        return record
