"""Independent, bounded application-boundary HTTP reference process.

Run with ``python -m validation_suite.endpoint --help``. Drain returning is an
application observation; it never proves packet transmission or client receipt.
This deliberately small server accepts one request per HTTP/1.1 connection.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from validation_suite.wire import FIXTURE_VERSION, build_response

SCHEMA_VERSION = 1


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


class Recorder:
    """Bound rows AND queued bytes; overflow is evidence invalidation, never silent."""

    def __init__(self, path: Path, context: dict[str, Any], options: dict[str, Any] | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.health_path = path.with_name("receiver-health.json")
        self.context = context
        self.options = options or {}
        self.clock_domain = f"host:{socket.gethostname()}:monotonic_ns"
        self.rows: queue.Queue[tuple[int, bytes]] = queue.Queue(
            maxsize=int(self.options.get("queue_rows", 2048)))
        self.byte_limit = int(self.options.get("queue_bytes", 16 * 1024 * 1024))
        self.record_limit = int(self.options.get("max_record_bytes", 256 * 1024))
        if min(self.rows.maxsize, self.byte_limit, self.record_limit) <= 0:
            raise ValueError("Recorder bounds must be positive")
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.closed = False
        self.stats: dict[str, Any] = {
            "attempted": 0, "written": 0, "overflow_count": 0, "truncated_count": 0,
            "omitted_count": 0, "write_errors": 0, "queued_bytes": 0,
            "queue_high_water_rows": 0, "queue_high_water_bytes": 0,
            "bytes_written": 0, "last_sequence": -1, "last_checkpoint_sequence": -1,
            "flush_complete": False, "complete": False, "shutdown_complete": False,
            "recording_overhead_ns": 0, "recording_max_overhead_ns": 0,
            "queue_bound_rows": self.rows.maxsize, "queue_bound_bytes": self.byte_limit,
            "record_bound_bytes": self.record_limit, "clock_domain": self.clock_domain,
            "reference_boundary": "application_write_and_drain",
            "cross_process_clock_offset_uncertainty_ns": None,
            "network_observation_uncertainty_ns": None,
        }
        self.thread = threading.Thread(target=self._write, name="reference-recorder", daemon=True)
        self.thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.stats)

    def emit(self, kind: str, *, ids: dict[str, Any] | None = None,
             timestamp_ns: int | None = None, **metadata: Any) -> None:
        start = time.monotonic_ns()
        with self.lock:
            sequence = self.stats["attempted"]
            self.stats["attempted"] += 1
        row = {"schema_version": SCHEMA_VERSION, "study_id": self.context.get("study_id", "local"),
               "case_id": self.context.get("case_id", "reference"),
               "candidate": self.context.get("candidate", "independent-client"),
               "repetition": self.context.get("repetition", 1), "logical_request_id": None,
               "attempt_id": None, "worker_id": "reference",
               "phase": self.context.get("phase", "measured"),
               **(ids or {}), "kind": kind, "sequence": sequence,
               "monotonic_ns": start if timestamp_ns is None else timestamp_ns,
               "clock_domain": self.clock_domain, "source": "endpoint_application",
               "metadata": metadata}
        if kind in self.options.get("omit_kinds", []):
            with self.lock:
                self.stats["omitted_count"] += 1
            return
        encoded = (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        if len(encoded) > self.record_limit:
            row["metadata"] = {"truncated": True, "original_record_bytes": len(encoded),
                               "original_record_sha256": hashlib.sha256(encoded).hexdigest()}
            encoded = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            with self.lock:
                self.stats["truncated_count"] += 1
        with self.lock:
            if len(encoded) > self.record_limit or self.stats["queued_bytes"] + len(encoded) > self.byte_limit:
                self.stats["overflow_count"] += 1
            else:
                try:
                    self.rows.put_nowait((sequence, encoded))
                except queue.Full:
                    self.stats["overflow_count"] += 1
                else:
                    self.stats["queued_bytes"] += len(encoded)
                    self.stats["queue_high_water_rows"] = max(
                        self.stats["queue_high_water_rows"], self.rows.qsize())
                    self.stats["queue_high_water_bytes"] = max(
                        self.stats["queue_high_water_bytes"], self.stats["queued_bytes"])
            overhead = time.monotonic_ns() - start
            self.stats["recording_overhead_ns"] += overhead
            self.stats["recording_max_overhead_ns"] = max(
                self.stats["recording_max_overhead_ns"], overhead)

    def _checkpoint(self, output: Any) -> None:
        try:
            output.flush()
            os.fsync(output.fileno())
            with self.lock:
                self.stats["last_checkpoint_sequence"] = self.stats["last_sequence"]
            write_json(self.health_path, self.snapshot())
        except OSError:
            with self.lock:
                self.stats["write_errors"] += 1

    def _write(self) -> None:
        try:
            with self.path.open("wb") as output:
                last_checkpoint = time.monotonic()
                while not self.stop.is_set() or not self.rows.empty():
                    try:
                        sequence, encoded = self.rows.get(timeout=.05)
                    except queue.Empty:
                        if time.monotonic() - last_checkpoint >= .25:
                            self._checkpoint(output)
                            last_checkpoint = time.monotonic()
                        continue
                    try:
                        delay = float(self.options.get("write_delay_ms", 0)) / 1000
                        if delay:
                            time.sleep(delay)
                        maximum = self.options.get("max_log_bytes")
                        fail_after = self.options.get("fail_after_records")
                        if maximum is not None and self.stats["bytes_written"] + len(encoded) > maximum:
                            raise OSError("injected bounded test storage full")
                        if fail_after is not None and self.stats["written"] >= fail_after:
                            raise OSError("injected recording failure")
                        output.write(encoded)
                        with self.lock:
                            self.stats["written"] += 1
                            self.stats["bytes_written"] += len(encoded)
                            self.stats["last_sequence"] = sequence
                    except OSError:
                        with self.lock:
                            self.stats["write_errors"] += 1
                    finally:
                        with self.lock:
                            self.stats["queued_bytes"] -= len(encoded)
                        self.rows.task_done()
                    if time.monotonic() - last_checkpoint >= .25:
                        self._checkpoint(output)
                        last_checkpoint = time.monotonic()
                self._checkpoint(output)
        except OSError:
            with self.lock:
                self.stats["write_errors"] += 1

    def close(self, *, shutdown_complete: bool = True, deadline: float = 5) -> dict[str, Any]:
        if self.closed:
            return self.snapshot()
        self.closed = True
        # Drain before the final status so its anticipated count includes itself.
        end = time.monotonic() + deadline
        while self.rows.unfinished_tasks and self.thread.is_alive() and time.monotonic() < end:
            time.sleep(.005)
        snapshot = self.snapshot()
        clean = not any(snapshot[k] for k in
                        ("overflow_count", "truncated_count", "omitted_count", "write_errors"))
        anticipated = {**snapshot, "written": snapshot["written"] + 1,
                       "attempted": snapshot["attempted"] + 1,
                       "last_sequence": snapshot["attempted"], "flush_complete": True,
                       "shutdown_complete": shutdown_complete,
                       "complete": clean and shutdown_complete and not self.rows.unfinished_tasks}
        self.emit("recorder_status", **anticipated)
        self.stop.set()
        self.thread.join(timeout=max(.01, end - time.monotonic()))
        with self.lock:
            self.stats["shutdown_complete"] = shutdown_complete
            self.stats["flush_complete"] = (not self.thread.is_alive() and
                                              not self.rows.unfinished_tasks and
                                              self.stats["write_errors"] == 0)
            self.stats["complete"] = (self.stats["flush_complete"] and shutdown_complete and
                self.stats["attempted"] == self.stats["written"] and
                not any(self.stats[k] for k in
                        ("overflow_count", "truncated_count", "omitted_count", "write_errors")))
        health = self.snapshot()
        if self.path.exists():
            digest = hashlib.sha256()
            with self.path.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
            health["events_sha256"] = digest.hexdigest()
        write_json(self.health_path, health)
        return health


class ReferenceServer:
    def __init__(self, recorder: Recorder, scenario: dict[str, Any]):
        self.recorder = recorder
        self.scenario = scenario
        self.counter = 0
        self.tasks: set[asyncio.Task[Any]] = set()
        self.peak_active = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        ids: dict[str, Any] = {}
        response = None
        outcome, terminal = "request_error", False
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = header.decode("iso-8859-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {key.lower().strip(): value.strip() for key, value in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            if method == "GET":
                self.recorder.emit("probe_received", ids={"phase": "probe"}, method=method,
                                   path=path, observation_boundary="parsed_http_headers")
                if path == "/validation/clock":
                    raw = json.dumps({"monotonic_ns": time.monotonic_ns(), "pid": os.getpid(),
                                      "clock_domain": self.recorder.clock_domain,
                                      "clock_info": vars(time.get_clock_info("monotonic"))}).encode()
                    content_type = "application/json"
                elif path == "/metrics":
                    await asyncio.sleep(float(self.scenario.get("metrics_delay_ms", 0)) / 1000)
                    raw, content_type = b"giraffe_reference_up 1\n", "text/plain"
                else:
                    raw = json.dumps({"object": "list", "data": [{"id": "reference"}]}).encode()
                    content_type = "application/json"
                writer.write(self.headers(200, content_type, len(raw)) + raw)
                await writer.drain()
                return
            if method != "POST" or path != "/v1/chat/completions":
                writer.write(self.headers(404, "application/json", 2) + b"{}")
                await writer.drain()
                return
            size = int(headers.get("content-length", "0"))
            if size < 0 or size > int(self.scenario.get("max_request_bytes", 2 * 1024 * 1024)):
                raise ValueError("Reference request byte bound exceeded")
            body = await asyncio.wait_for(reader.readexactly(size), timeout=5)
            received_ns = time.monotonic_ns()
            self.counter += 1
            response_id = f"reference-{self.counter:08d}"
            logical = headers.get("x-validation-logical-id", "").strip() or response_id
            attempt = headers.get("x-validation-attempt-id", "").strip() or response_id
            ids = {"logical_request_id": logical, "attempt_id": attempt,
                   "phase": headers.get("x-validation-phase", "measured").strip()}
            self.recorder.emit("request_received", ids=ids, timestamp_ns=received_ns,
                               request_bytes=size, body_sha256=hashlib.sha256(body).hexdigest(),
                               response_id=response_id, request_index=self.counter,
                               correlation_confidence=("explicit_header" if logical != response_id
                                                       else "response_id_only"),
                               request=json.loads(body))
            payload = json.loads(body)
            response = build_response(payload, self.scenario, response_id)
            self.recorder.emit("response_prepared", ids=ids, response_id=response_id,
                               expected=response.expected, http_status=response.status)
            self.peak_active = max(self.peak_active, len(self.tasks))
            fixture = response.expected["fixture"]
            delay = float(self.scenario.get("first_delay_ms", 0))
            every = int(self.scenario.get("straggler_every", 0))
            if every and self.counter % every == 0:
                delay += float(self.scenario.get("straggler_delay_ms", 500))
            await asyncio.sleep(delay / 1000)
            if fixture == "disconnect":
                outcome = "transport_error"
                writer.transport.abort()
                return
            if fixture == "hang":
                # Remain silent until the client closes or shutdown cancels us.
                await reader.read(1)
                outcome = "peer_closed"
                return
            length = sum(len(event.raw) for event in response.events)
            head = self.headers(response.status, response.content_type,
                                length if response.content_type == "application/json" else None)
            self.recorder.emit("response_headers_start", ids=ids, http_status=response.status)
            writer.write(head)
            await writer.drain()
            self.recorder.emit("response_headers_end", ids=ids, http_status=response.status)
            groups = [response.events] if fixture == "bundled" else [[event] for event in response.events]
            write_sequence = 0
            for index, group in enumerate(groups):
                if index:
                    await asyncio.sleep(float(self.scenario.get("event_delay_ms", 0)) / 1000)
                raw = b"".join(event.raw for event in group)
                fragments = [raw[i:i + 1] for i in range(len(raw))] if fixture == "fragmented_utf8" else [raw]
                for fragment in fragments:
                    self.recorder.emit("response_write_start", ids=ids, write_seq=write_sequence,
                                       event_seqs=[event.sequence for event in group], wire_bytes=len(fragment))
                    writer.write(fragment)
                    await writer.drain()
                    observed = time.monotonic_ns()
                    self.recorder.emit("response_write_end", ids=ids, timestamp_ns=observed,
                                       write_seq=write_sequence, wire_bytes=len(fragment))
                    write_sequence += 1
                for event in group:
                    self.recorder.emit("response_event", ids=ids, timestamp_ns=observed,
                                       event_seq=event.sequence, response_id=response_id,
                                       wire_b64=base64.b64encode(event.raw).decode(),
                                       wire_bytes=len(event.raw),
                                       wire_sha256=hashlib.sha256(event.raw).hexdigest(),
                                       bundled=len(group) > 1, fragmented=len(fragments) > 1,
                                       observation_boundary="application_drain_return",
                                       **event.metadata)
            outcome, terminal = response.outcome, response.terminal
        except asyncio.CancelledError:
            outcome = "server_shutdown"
            raise
        except (ConnectionError, asyncio.IncompleteReadError):
            outcome = "peer_disconnected"
        except (ValueError, TimeoutError, asyncio.LimitOverrunError) as exc:
            outcome = "request_error"
            self.recorder.emit("request_error", ids=ids, error=type(exc).__name__)
        except Exception as exc:
            outcome = "reference_error"
            self.recorder.emit("reference_error", ids=ids, error=type(exc).__name__)
        finally:
            if ids:
                self.recorder.emit("response_finished", ids=ids, outcome=outcome, terminal=terminal,
                                   http_status=response.status if response else None,
                                   expected=response.expected if response else None)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=.5)
            except (ConnectionError, asyncio.CancelledError, TimeoutError):
                writer.transport.abort()
            self.tasks.discard(task)

    @staticmethod
    def headers(status: int, content_type: str, length: int | None = None) -> bytes:
        value = (f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
                 f"Content-Type: {content_type}\r\nConnection: close\r\n")
        if length is not None:
            value += f"Content-Length: {length}\r\n"
        return (value + "\r\n").encode()


async def serve(args: argparse.Namespace) -> None:
    context = json.loads(Path(args.context).read_text()) if args.context else {}
    scenario = json.loads(Path(args.scenario).read_text()) if args.scenario else {}
    recorder = Recorder(Path(args.events), context, scenario.get("recording"))
    endpoint = ReferenceServer(recorder, scenario)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    server = await asyncio.start_server(endpoint.handle, args.host, args.port, limit=65536)
    port = server.sockets[0].getsockname()[1]
    recorder.emit("reference_started", pid=os.getpid(), fixture_version=FIXTURE_VERSION,
                  clock_resolution_seconds=time.get_clock_info("monotonic").resolution,
                  reference_boundary="application_write_and_drain", network_timing_qualified=False)
    write_json(Path(args.ready), {"base_url": f"http://{args.host}:{port}/v1", "pid": os.getpid(),
        "clock_domain": recorder.clock_domain, "fixture_version": FIXTURE_VERSION,
        "reference_boundary": "application_write_and_drain", "network_timing_qualified": False})
    await stopping.wait()
    start = time.monotonic_ns()
    server.close()
    pending = list(endpoint.tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await server.wait_closed()
    recorder.emit("reference_stopped", active_at_shutdown=len(pending), requests_received=endpoint.counter,
                  peak_active=endpoint.peak_active, shutdown_ns=time.monotonic_ns() - start)
    await asyncio.to_thread(recorder.close, shutdown_complete=True)


class EndpointProcess:
    """Own exactly one local reference subprocess and its private artifacts."""

    def __init__(self, directory: Path | str, *, scenario: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None):
        self.directory = Path(directory)
        self.scenario, self.context = scenario or {}, context or {}
        self.process: subprocess.Popen[bytes] | None = None
        self.url = ""
        self.ready: dict[str, Any] = {}
        self._logs: list[Any] = []

    def start(self, timeout: float = 10) -> EndpointProcess:
        self.directory.mkdir(parents=True, exist_ok=True)
        for filename, value in (("endpoint-scenario.json", self.scenario),
                                ("endpoint-context.json", self.context)):
            write_json(self.directory / filename, value)
        ready_path = self.directory / "endpoint-ready.json"
        ready_path.unlink(missing_ok=True)
        command = [sys.executable, "-m", "validation_suite.endpoint", "--events",
                   str(self.directory / "receiver-events.jsonl"), "--ready", str(ready_path),
                   "--scenario", str(self.directory / "endpoint-scenario.json"),
                   "--context", str(self.directory / "endpoint-context.json")]
        self._logs = [(self.directory / name).open("wb") for name in
                      ("endpoint-stdout.log", "endpoint-stderr.log")]
        self.process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                        stdout=self._logs[0], stderr=self._logs[1])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready_path.exists():
                self.ready = json.loads(ready_path.read_text())
                self.url = self.ready["base_url"]
                return self
            if self.process.poll() is not None:
                self.close()
                raise RuntimeError((self.directory / "endpoint-stderr.log").read_text())
            time.sleep(.01)
        self.close()
        raise TimeoutError("Independent reference endpoint did not start")

    @property
    def health(self) -> dict[str, Any]:
        path = self.directory / "receiver-health.json"
        return json.loads(path.read_text()) if path.exists() else {"complete": False,
                                                                 "reason": "No health checkpoint"}

    def close(self, timeout: float = 8) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for output in self._logs:
            output.close()

    def __enter__(self) -> EndpointProcess:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--events", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--context")
    parser.add_argument("--scenario")
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
