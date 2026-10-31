"""Ollama-only cold-start observations; normal checks keep their usual transport."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import math
import time

import httpx

from . import deployment
from .client import LLMClient, _MAX_RESPONSE_BYTES, _ProtocolError, _headers, _object


NATIVE_API_LABEL = "Measured through Ollama's native API"
REASONING_OUTPUT_NOTE = "First output was reasoning; no answer within the output cap."


def output_measurement(record):
    """Summarize restart output without changing ordinary answer/check evidence."""
    candidates = [(value, kind) for value, kind in (
        (record.first_output_ms, "answer"), (record.first_reasoning_ms, "reasoning"))
        if value is not None]
    if record.http_status == 200 and (candidates or record.output.strip() or record.reasoning.strip()):
        first, kind = min(candidates) if candidates else (
            None, "reasoning" if record.reasoning.strip() or record.reasoning_stripped else "answer")
        return {"first_output_ms": first, "first_output_kind": kind,
                "elapsed_ms": record.elapsed_ms}
    return {"first_output_ms": None, "elapsed_ms": None,
            "reason": record.error or f"HTTP {record.http_status}: no generated output"}


def retry_readiness(record):
    """Only startup/transport failures warrant another cold request."""
    if record.status == "timeout" or record.http_status == 429:
        return True
    if record.http_status is not None and 500 <= record.http_status <= 599:
        return True
    if record.http_status == 404:
        return bool(record.workload.get("model_not_served"))
    return record.http_status is None and record.error in {
        f"{prefix} ({kind})"
        for prefix in ("request failed", "client failure")
        for kind in ("ConnectError", "ConnectTimeout", "ReadError", "WriteError", "CloseError",
                     "NetworkError", "ConnectionError", "ConnectionResetError", "ConnectionRefusedError",
                     "BrokenPipeError", "RemoteProtocolError", "OSError")
    }


def _measurement_spec(base, number, scenario):
    # These prompts have seven tokens each in the bundled reference encoding.
    # Different leading digits avoid sharing the complete user-prompt prefix;
    # the served model's tokenizer and implicit chat prefix remain unverified.
    prompt = f"{number}: Reply with {number}."
    return base.model_copy(update={
        "fixture_id": f"cold_start.{number}", "scenario": scenario, "check_ids": [],
        "messages": [{"role": "user", "content": prompt}], "input_chars": len(prompt),
        "scorer": "exact", "expected": str(number),
    })


def cold_spec(base):
    """First of three equivalent, fixed-size cold-start measurement requests."""
    return _measurement_spec(base, 1, "readiness")


def warm_specs(base):
    """Two follow-ups matching cold_spec's reference token count and output cap."""
    return [_measurement_spec(base, number, "cold_start_warm") for number in (2, 3)]


def _native_url(target, path):
    return httpx.URL(target.url).copy_with(path=path, query=None, fragment=None)


async def _observation(operation, *, deadline, stop, default):
    """Bound discovery separately from inference and respond to Stop promptly."""
    timeout = min(2.0, deadline - time.monotonic()) if deadline is not None else 2.0
    if timeout <= 0 or stop is not None and stop.is_set():
        return default
    task = asyncio.create_task(operation())
    stopped = asyncio.create_task(stop.wait()) if stop is not None else None
    try:
        async with asyncio.timeout(timeout):
            if stopped is not None:
                done, _ = await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    return default
            return await task
    except (httpx.HTTPError, ValueError, OSError, TimeoutError):
        return default
    finally:
        for pending in (task, stopped):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(t for t in (task, stopped) if t is not None), return_exceptions=True)


async def detect_ollama(target, config, *, deadline=None, stop=None):
    """Recognize native read-only routes, regardless of deployment.discovery."""
    async def probe():
        client = await asyncio.to_thread(deployment._discovery_client, config, 1.0)
        async with client:
            client.headers.update(_headers(target))
            # Both must have Ollama's response shape; no inference is issued.
            inventory = await deployment._read(client, str(_native_url(target, "/api/ps")))
            version = await deployment._read(client, str(_native_url(target, "/api/version")))
        return (isinstance(inventory, dict) and isinstance(inventory.get("models"), list)
                and isinstance(version, dict) and isinstance(version.get("version"), str)
                and bool(version["version"]))

    return await _observation(probe, deadline=deadline, stop=stop, default=False)


async def residency(target, config, *, deadline=None, stop=None):
    """Observe the requested model without requiring discovery configuration."""
    observed_target = target.model_copy(update={
        "deployment": target.deployment.model_copy(update={"discovery": "ollama"}),
    })
    bounded_config = config.model_copy(update={"metrics_timeout_seconds": 1.0})

    async def observe():
        result = await deployment.model_residency(
            observed_target, bounded_config, deadline=deadline, stop=stop)
        return result["state"]

    return await _observation(observe, deadline=deadline, stop=stop, default="unknown")


def model_load_evidence(before, after, reported_ns):
    """Only inventory transitions plus a server duration establish model loading."""
    loaded = {"resident": "Yes", "not_resident": "No"}.get(before, "Unknown")
    result = {"model_loaded_before_first_request": loaded}
    if before == "resident":
        result["model_load"] = "Model was already loaded"
    elif before == "not_resident":
        valid = _valid_duration(reported_ns)
        if after == "resident" and valid:
            result.update(model_load=f"Model load: {reported_ns / 1e9:.3g} s, reported by Ollama",
                          model_load_ms=reported_ns / 1e6)
        else:
            result["model_load"] = "Model was not loaded before the first request"
    else:
        result["model_load"] = "Slow first answer; model load not confirmed"
    return result


def _valid_duration(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class _NativeStream(httpx.AsyncByteStream):
    """Translate bounded NDJSON into the existing client's measured stream format."""

    def __init__(self, response, evidence):
        self.response, self.evidence = response, evidence

    async def __aiter__(self):
        buffer = bytearray()
        consumed = 0
        async with aclosing(self.response.aiter_bytes()) as chunks:
            async for chunk in chunks:
                consumed += len(chunk)
                if consumed > _MAX_RESPONSE_BYTES:
                    raise _ProtocolError("response exceeded the 4 MiB safety limit")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if not line.strip():
                        continue
                    data = _object(line)
                    yield self._event(data)
                    if data.get("done") is True:
                        yield b"data: [DONE]\n\n"
                        return
        if buffer.strip():
            data = _object(buffer)
            yield self._event(data)
            if data.get("done") is True:
                yield b"data: [DONE]\n\n"

    def _event(self, data):
        done = data.get("done") is True
        message = data.get("message", {})
        if not isinstance(message, dict):
            raise _ProtocolError("response is missing assistant content")
        delta = {"content": message.get("content", ""),
                 "reasoning_content": message.get("thinking", "")}
        finish = data.get("done_reason", "stop") if done else None
        event = {"model": data.get("model"), "choices": [{
            "delta": delta, "finish_reason": finish}]}
        if done:
            self.evidence["reported_load_duration_ns"] = data.get("load_duration")
            event["usage"] = {"prompt_tokens": data.get("prompt_eval_count"),
                              "completion_tokens": data.get("eval_count")}
        return b"data: " + json.dumps(event).encode() + b"\n\n"

    async def aclose(self):
        await self.response.aclose()


class _NativeTransport:
    def __init__(self, client, target, evidence):
        self.client, self.target, self.evidence = client, target, evidence

    def build_request(self, method, url, *, json, **kwargs):
        # The measurement is streamed even if normal checks are configured otherwise.
        options = {key: json[key] for key in (
            "temperature", "seed", "top_p", "top_k", "min_p", "stop",
            "repeat_penalty", "presence_penalty", "frequency_penalty", "num_ctx",
        ) if key in json}
        options["num_predict"] = json["max_tokens"]
        payload = {"model": json["model"], "messages": json["messages"],
                   "stream": True, "options": options}
        for key in ("keep_alive", "think", "format", "tools"):
            if key in json:
                payload[key] = json[key]
        response_format = json.get("response_format")
        if isinstance(response_format, dict):
            if response_format.get("type") == "json_object":
                payload["format"] = "json"
            elif response_format.get("type") == "json_schema":
                schema = response_format.get("json_schema", {})
                if isinstance(schema, dict) and "schema" in schema:
                    payload["format"] = schema["schema"]
        return self.client.build_request(method, _native_url(self.target, "/api/chat"),
                                         json=payload, **kwargs)

    async def send(self, request, *, stream):
        response = await self.client.send(request, stream=stream)
        if not response.is_success:
            return response
        headers = dict(response.headers)
        headers["content-type"] = "text/event-stream"
        headers.pop("content-length", None)
        headers.pop("content-encoding", None)
        return httpx.Response(response.status_code, headers=headers,
                              stream=_NativeStream(response, self.evidence), request=request)


class OllamaColdClient:
    """Native measurements sharing the active client's authenticated HTTP transport."""
    reports_response_activity = True

    def __init__(self, client):
        self.client = client

    async def execute(self, spec, *, first_output_event=None):
        if self.client._client is None:
            raise RuntimeError("Use LLMClient as an async context manager")
        evidence = {}
        measuring = LLMClient(self.client.target, self.client.config.model_copy(update={"stream": True}),
                              max_connections=self.client.max_connections)
        measuring.on_response = self.client.on_response
        measuring._client = _NativeTransport(self.client._client, self.client.target, evidence)
        record = await measuring.execute(spec.model_copy(update={"stream": True}),
                                         first_output_event=first_output_event)
        # The native done chunk completes the request without waiting for socket EOF.
        if measuring._stream_drains:
            await asyncio.gather(*measuring._stream_drains, return_exceptions=True)
        reported = evidence.get("reported_load_duration_ns")
        if _valid_duration(reported):
            record.workload["reported_load_duration_ns"] = reported
        record.workload["measurement_api"] = NATIVE_API_LABEL
        return record
