"""Deterministic loopback HTTP fixture, not a model or a readiness benchmark.

Token counts are synthetic whitespace counts. This helper exists only to seed
observable completion, semantic and timing faults through the actual HTTP path.
"""

from __future__ import annotations

import json
import re
import threading
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
    ):
        self.mode = mode
        self.first_token_delay = first_token_delay
        self.chunk_delay = chunk_delay
        self.fault_contains = fault_contains
        self.requests: list[dict[str, Any]] = []
        self.active = 0
        self.peak_active = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
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
                if self.path == "/v1/models":
                    self._json(200, {"object": "list", "data": [{"id": "fake-model"}]})
                else:
                    self._json(404, {"error": {"message": "unknown fixture path"}})

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "unknown fixture path"}})
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with endpoint._lock:
                    endpoint.requests.append(payload)
                    endpoint.active += 1
                    endpoint.peak_active = max(endpoint.peak_active, endpoint.active)
                try:
                    self._completion(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Intentional client disconnect is an exercised behavior.
                finally:
                    self.close_connection = True
                    with endpoint._lock:
                        endpoint.active -= 1

            def _completion(self, payload: dict[str, Any]) -> None:
                prompt = "\n".join(message["content"] for message in payload["messages"])
                applies = endpoint.fault_contains is None or endpoint.fault_contains in prompt
                mode = endpoint.mode if applies else "healthy"
                if mode == "http_error":
                    self._json(503, {"error": {"message": "seeded unavailable response"}})
                    return
                output = endpoint._answer(prompt)
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
            return '{"code":"J6Q2","count":3,"ready":true}'
        if "17 cards" in prompt and "8 cards" in prompt:
            return "25"
        if "Label mip" in prompt:
            return "B"
        if "Copy exactly" in prompt:
            return prompt.split("\n", 1)[1] if "\n" in prompt else prompt.split(":", 1)[1].strip()
        if "numbers from 1 to 100" in prompt:
            return " ".join(str(number) for number in range(1, 101))
        if match := re.search(r"Code:\s*([A-Z0-9]+)", prompt):
            return match.group(1)
        return "42"
