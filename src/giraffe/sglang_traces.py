"""Optional loopback OTLP/HTTP receiver for SGLang terminal request metadata."""

from __future__ import annotations

import json
import os
import stat
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024


def terminal_spans(body: bytes) -> list[dict]:
    # Keep protobuf optional: ordinary Giraffe runs do not need tracing packages.
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    request = ExportTraceServiceRequest.FromString(body)
    rows = []
    for resource in request.resource_spans:
        for scope in resource.scope_spans:
            for span in scope.spans:
                attrs = {a.key: a.value.string_value for a in span.attributes
                         if a.value.WhichOneof("value") == "string_value"}
                rid = attrs.get("gen_ai.request.id")
                if (attrs.get("module") != "sglang::request" or not rid
                        or len(rid) > 4096 or rid != attrs.get("rid")):
                    continue
                try:
                    reasons = json.loads(attrs.get("gen_ai.response.finish_reasons", "null"))
                except ValueError:
                    continue
                model = attrs.get("gen_ai.response.model", "")
                if (reasons not in (["abort"], ["stop"], ["length"])
                        or not model or len(model) > 4096
                        or not 0 < span.start_time_unix_nano <= span.end_time_unix_nano):
                    continue
                # Never persist prompts, outputs, headers, or arbitrary attributes.
                rows.append({
                    "schema_version": 1, "source": "sglang::request",
                    "request_id": rid, "model": model, "finish_reasons": reasons,
                    "trace_id": span.trace_id.hex(),
                    "started_at_ns": span.start_time_unix_nano,
                    "finished_at_ns": span.end_time_unix_nano,
                    "received_at_ns": time.time_ns(),
                })
    return rows


def serve(output: Path, port: int = 4318) -> None:
    try:
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2  # noqa: F401
    except ImportError as exc:
        raise ValueError("Install Giraffe's sglang-tracing extra to collect OTLP traces") from exc
    if not 1 <= port <= 65535:
        raise ValueError("trace receiver port must be in 1..65535")
    output.parent.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/v1/traces":
                self.send_error(404)
                return
            if (self.headers.get("Content-Encoding", "identity") != "identity"
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/x-protobuf"):
                self.send_error(415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY_BYTES:
                self.send_error(413)
                return
            self.connection.settimeout(5)
            body = self.rfile.read(length)
            try:
                rows = terminal_spans(body)
            except Exception:
                self.send_error(400)
                return
            encoded = b"".join(line for row in rows
                               if len(line := json.dumps(row).encode() + b"\n") <= MAX_LINE_BYTES)
            if sink.tell() + len(encoded) > MAX_FILE_BYTES:
                self.send_error(507)
                return
            sink.write(encoded)
            sink.flush()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

    # A fresh file prevents accidental reuse of evidence from a previous server.
    with HTTPServer(("127.0.0.1", port), Handler) as server, output.open("xb") as sink:
        print(f"SGLang traces: http://127.0.0.1:{port}/v1/traces -> {output}", flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


class TraceTail:
    """Bounded incremental reads; rotation and truncation invalidate evidence."""

    def __init__(self, path: str):
        self.path = Path(path)
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError("trace evidence must be a regular file of at most 16 MiB")
        self.identity = (info.st_dev, info.st_ino)
        self.offset = info.st_size
        self.pending = b""

    def read(self) -> list[dict]:
        with self.path.open("rb") as source:
            info = os.fstat(source.fileno())
            if (info.st_dev, info.st_ino) != self.identity or info.st_size < self.offset:
                raise ValueError("trace file rotated or truncated")
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError("trace file exceeded the 16 MiB limit")
            source.seek(self.offset)
            data = source.read(64 * 1024)
            self.offset += len(data)
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        if any(len(line) > MAX_LINE_BYTES for line in lines + [self.pending]):
            raise ValueError("trace record exceeded the 16 KiB limit")
        rows = [json.loads(line) for line in lines if line.strip()]
        return [row for row in rows if isinstance(row, dict)]
