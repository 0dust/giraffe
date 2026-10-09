"""Deterministic loopback HTTP fixture, not a model or a readiness benchmark.

Token counts are synthetic whitespace counts. This helper exists only to seed
observable completion, semantic and timing faults through the actual HTTP path.
"""

from __future__ import annotations

import json
import re
import uuid
import threading
import time
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeEndpoint:
    def __init__(
        self,
        *,
        mode: str = "healthy",
        first_token_delay: float = 0.005,
        chunk_delay: float = 0.002,
        fault_contains: str | None = None,
        service_slots: int | None = None,
    ):
        self.mode = mode
        self.first_token_delay = first_token_delay
        self.chunk_delay = chunk_delay
        self.fault_contains = fault_contains
        self.requests: list[dict[str, Any]] = []
        self.request_started_at: list[float] = []
        self.request_finished_at: list[float] = []
        self.metrics_text = 'vllm:kv_cache_usage_perc{model_name="fake-model",engine="0"} 0.95\nvllm:num_requests_running{engine="0"} 2\nvllm:num_requests_waiting{engine="0"} 1\nvllm:num_preemptions_total{engine="0"} 5\n'
        self.metrics_status = 200
        self.metrics_reads = 0
        self.collector_reads = 0
        self.discovery_export = {"serving": {"scheduler": "fcfs", "max_num_seqs": 2}}
        self.collector_changes = False
        self.active = 0
        self.peak_active = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        if service_slots is not None and service_slots < 1:
            raise ValueError("service_slots must be positive")
        self._service_slots = threading.Semaphore(service_slots) if service_slots else None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Use FakeEndpoint as a context manager")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __enter__(self) -> FakeEndpoint:
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:
                pass

            def _json(self, status: int, value: dict[str, Any]) -> None:
                content = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(content)
                self.close_connection = True

            def do_GET(self) -> None:
                if self.path == "/metrics":
                    endpoint.metrics_reads += 1
                    content = endpoint.metrics_text.encode()
                    self.send_response(endpoint.metrics_status)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                if self.path == "/configuration":
                    endpoint.collector_reads += 1
                    doc = endpoint.discovery_export
                    if endpoint.collector_changes and endpoint.collector_reads>1:
                        doc = {"serving": {"scheduler": "priority", "max_num_seqs": 4}}
                    self._json(200, doc)
                    return
                if self.path in {"/version", "/api/version"}:
                    self._json(200, {"version": "0.30.0"})
                    return
                if self.path in {"/api/tags", "/api/ps"}:
                    self._json(200, {"models": [{"model": "fake-model", "digest": "model-digest",
                                                "context_length": 2048}]})
                    return
                if self.path == "/v1/models":
                    self._json(200, {"object": "list", "data": [{"id": "fake-model"}]})
                else:
                    self._json(404, {"error": {"message": "unknown fixture path"}})

            def do_POST(self) -> None:
                if self.path == "/api/show":
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self._json(200, {"details": {"quantization_level": "Q4_K_M"}})
                    return
                if self.path != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "unknown fixture path"}})
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with endpoint._lock:
                    endpoint.requests.append(payload)
                    endpoint.request_started_at.append(time.monotonic())
                    endpoint.active += 1
                    endpoint.peak_active = max(endpoint.peak_active, endpoint.active)
                try:
                    # Optional finite service capacity creates genuine HTTP queue
                    # delay, allowing tests to observe overload without a model.
                    with endpoint._service_slots or nullcontext():
                        self._completion(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Intentional client disconnect is an exercised behavior.
                finally:
                    self.close_connection = True
                    with endpoint._lock:
                        endpoint.active -= 1
                        endpoint.request_finished_at.append(time.monotonic())

            def _tools(self, payload, mode):
                function = "wrong" if mode=="tool_wrong" else "get_weather"
                arguments = "{broken" if mode=="tool_malformed" else '{"city":42}' if mode=="tool_schema" else '{"city":"Delhi"}'
                calls = [{"id": "call-1", "type": "function", "function": {"name": function, "arguments": arguments}}]
                if mode=="tool_multiple":
                    calls += [{"id": "call-2", "type": "function", "function": {"name": function, "arguments": arguments}}]
                if mode=="tool_missing":
                    self._json(200, {"choices": [{"message": {"content": arguments}, "finish_reason": "stop"}]})
                    return
                if not payload.get("stream"):
                    self._json(200, {"model": payload["model"], "choices": [{"message": {"content": None, "tool_calls": calls}, "finish_reason": "tool_calls"}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                def event(delta, finish=None):
                    data = {"model": payload["model"], "choices": [{"delta": delta, "finish_reason": finish}]}
                    self.wfile.write(("data: "+json.dumps(data)+"\n\n").encode())
                    self.wfile.flush()
                event({"tool_calls": [dict(index=i, id=c["id"], type="function", function={"name": c["function"]["name"][:4], "arguments": c["function"]["arguments"][:4]}) for i,c in enumerate(calls)]})
                event({"tool_calls": [{"index": i, "function": {"name": c["function"]["name"][4:], "arguments": c["function"]["arguments"][4:]}} for i,c in enumerate(calls)]})
                event({}, "tool_calls")
                if mode!="tool_truncated":
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()

            def _completion(self, payload: dict[str, Any]) -> None:
                prompt = "\n".join(message["content"] for message in payload["messages"])
                applies = endpoint.fault_contains is None or endpoint.fault_contains in prompt
                mode = endpoint.mode if applies else "healthy"
                if mode == "http_error":
                    self._json(503, {"error": {"message": "seeded unavailable response"}})
                    return
                output = endpoint._answer(prompt)
                if payload["messages"][-1]["content"].startswith("Suggest a name for a bakery"):
                    output = "Bakery_" + uuid.uuid4().hex
                if payload["messages"][-1]["content"].startswith("Repeat exactly the bakery name"):
                    output = next(m["content"] for m in reversed(payload["messages"]) if m["role"]=="assistant")
                if mode == "overlap_variation" and endpoint.active>1:
                    output += " "  # Whitespace changes byte equality but keeps the exact task correct.
                if mode == "variable_correct":
                    output += " " * (len(endpoint.requests)%3)
                if mode == "tool_rejection" and payload.get("tools"):
                    self._json(400, {"error": {"message": "unsupported tools"}})
                    return
                if payload.get("tools"):
                    self._tools(payload, mode)
                    return
                if mode == "wrong":
                    output = "INCORRECT"
                elif mode == "empty":
                    output = ""
                stop = payload.get("stop", [])
                if isinstance(stop, str):
                    stop = [stop]
                for marker in stop:
                    output = output.split(marker, 1)[0]
                pieces = re.findall(r"\S+\s*", output)
                budget = payload["max_tokens"]
                finish = "length" if len(pieces) > budget else "stop"
                pieces = pieces[:budget]
                output = "".join(pieces)
                usage = {"prompt_tokens": len(prompt.split()) + 8, "completion_tokens": len(pieces)}
                endpoint._stop.wait(endpoint.first_token_delay)
                if not payload.get("stream"):
                    self._json(200, {
                        "id": "fake-completion", "object": "chat.completion",
                        "model": payload["model"],
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": output},
                                     "finish_reason": finish}],
                        "usage": usage,
                    })
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def event(delta: dict[str, str], reason: str | None = None, **extra: Any) -> None:
                    data = {
                        "id": "fake-completion", "object": "chat.completion.chunk",
                        "model": payload["model"],
                        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                        **extra,
                    }
                    self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
                    self.wfile.flush()

                event({"role": "assistant"})
                for piece in pieces:
                    if endpoint._stop.is_set():
                        return
                    event({"content": piece})
                    endpoint._stop.wait(endpoint.chunk_delay)
                event({}, finish, usage=usage)
                if mode != "truncated":
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    @staticmethod
    def _answer(prompt: str) -> str:
        if "JSON object" in prompt:
            return '{"label":"J6Q2","count":3,"ready":true}'
        if "17 cards" in prompt and "8 cards" in prompt:
            return "25"
        if "Label mip" in prompt:
            return "B"
        if "Copy exactly" in prompt:
            return prompt.split("\n", 1)[1] if "\n" in prompt else prompt.split(":", 1)[1].strip()
        if "numbers from 1 to 100" in prompt:
            return " ".join(str(number) for number in range(1, 101))
        if match := re.search(r"(?:The box label is|Box label:)\s*([A-Z0-9]+)", prompt):
            return match.group(1)
        return "42"
