"""Shared, socket-free measurement support; exercises the real SSE client."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import platform
import sys
import time

import httpx

from giraffe.client import LLMClient


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {"count": len(values), **{name: values[min(len(values) - 1, int((len(values) - 1) * p))]
            for name, p in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}}


def environment():
    return {"python": sys.version, "platform": platform.platform(), "httpx": httpx.__version__,
            "clock": "time.monotonic; CPU=time.process_time (includes in-process fake endpoint)",
            "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


class FakeEndpoint:
    """Deterministic, distinct SSE bodies with real asynchronous chunk spacing."""

    def __init__(self, *, chunks=4, delay=.05, body_bytes=1024, barrier=0, arrivals=False):
        self.chunks, self.delay, self.body_bytes = chunks, delay, body_bytes
        self.barrier, self.arrivals = barrier, arrivals
        self.gate = asyncio.Event()
        if not barrier:
            self.gate.set()
        self.active = self.peak = self.requests = self.completed = 0
        self.chunk_lateness_ms = []

    async def stream(self):
        index = self.requests
        self.requests += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active >= self.barrier:
            self.gate.set()
        try:
            await asyncio.wait_for(self.gate.wait(), 20)
            # Unique text keeps RSS from accidentally benefiting from shared strings.
            text = (f"request-{index:09d}: " + "deterministic measurement content " *
                    (self.body_bytes // 20 + 1))[:self.body_bytes]
            start = time.monotonic()
            for chunk in range(self.chunks):
                due = start + (chunk + 1) * self.delay
                await asyncio.sleep(max(0, due - time.monotonic()))
                self.chunk_lateness_ms.append(max(0, (time.monotonic() - due) * 1000))
                piece = text[chunk * len(text) // self.chunks:(chunk + 1) * len(text) // self.chunks]
                delta = {"reasoning_content" if self.arrivals else "content": piece}
                if self.arrivals and chunk == self.chunks - 1:
                    delta["content"] = "incorrect" if index % 10 == 0 else "K7P4"
                payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            self.completed += 1
            yield b"data: [DONE]\n\n"
        finally:
            self.active -= 1

    async def handle(self, request):
        endpoint = self

        class Stream(httpx.AsyncByteStream):
            def __init__(self):
                self.iterator = endpoint.stream()

            async def __aiter__(self):
                async for chunk in self.iterator:
                    yield chunk

            async def aclose(self):
                await self.iterator.aclose()

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())


def mock_client(target, config, endpoint, maximum):
    client = LLMClient(target, config, max_connections=maximum)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint.handle), trust_env=False)
    return client


class LiveJob:
    """Production web progress tracker and its bounded containers.

    No HTTP app/server is created. Both request eviction and condition/event/phase updates use production code.
    """

    def __init__(self):
        from giraffe.web import _track_live_progress

        self.track = _track_live_progress
        self.job = {"requests": OrderedDict(), "live_request_summary": {
            "evicted": 0, "through_slot": None, "statuses": {}}, "live_request_count": 0,
            "evicted_running": set(), "events": [], "conditions": {}, "phases": []}
        self.peaks = {key: 0 for key in ("requests", "events", "conditions", "phases", "evicted_running")}

    def progress(self, event):
        job = self.job
        self.track(job, event, None)
        for key in self.peaks:
            self.peaks[key] = max(self.peaks[key], len(job[key]))

    def snapshot(self):
        serializable = {**self.job, "requests": list(self.job["requests"].values()),
                        "conditions": list(self.job["conditions"].values()),
                        "evicted_running": list(self.job["evicted_running"])}
        return {"container_peaks": self.peaks,
                "container_final": {key: len(self.job[key]) for key in self.peaks},
                "json_bytes": len(json.dumps(serializable).encode()),
                "summary": self.job["live_request_summary"],
                "scope": "production _track_live_progress (requests, conditions, events, phases)"}
