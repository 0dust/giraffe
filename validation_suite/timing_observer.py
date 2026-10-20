"""Read-only interpreter observations around the unchanged public Giraffe CLI.

This is an explicitly instrumented diagnostic. Socket recv return, HTTP body
yield and semantic parse are distinct boundaries; none is packet arrival.
"""

from __future__ import annotations

import argparse
import asyncio.selector_events
import base64
import hashlib
import http.client
import importlib
import json
import math
import os
from pathlib import Path
import queue
import runpy
import shutil
import socket
import sqlite3
import sys
import threading
import time
from types import CodeType, FrameType
from typing import Any
from urllib.parse import urlparse

OBSERVER_VERSION = "observational-timing-2"


def clock_calibration(samples: int = 256) -> dict[str, Any]:
    """Bracket actual perf_counter reads, without a symmetric-delay assumption."""
    if samples < 1:
        raise ValueError("Clock calibration requires positive samples")
    intervals = []
    widths = []
    for _ in range(samples):
        lower = time.monotonic_ns()
        clock = time.perf_counter()
        upper = time.monotonic_ns()
        quantization = math.ceil(math.ulp(clock) * 1e9) + 1
        value = int(clock * 1e9)
        intervals.append((lower - value - quantization, upper - value + quantization))
        widths.append(upper - lower)
    lower = max(item[0] for item in intervals)
    upper = min(item[1] for item in intervals)
    monotonic = time.get_clock_info("monotonic")
    counter = time.get_clock_info("perf_counter")
    return {"samples": samples, "offset_lower_ns": lower, "offset_upper_ns": upper,
            "offset_interval_width_ns": upper - lower,
            "consistent": lower <= upper and monotonic.implementation == counter.implementation,
            "monotonic": vars(monotonic), "perf_counter": vars(counter),
            "clock_read_bracket_min_ns": min(widths), "clock_read_bracket_max_ns": max(widths),
            "method": "perf_counter read bracketed by monotonic_ns; intersect offset intervals; float conversion rounded outward"}


def offset_bounds(samples: list[dict[str, int]]) -> dict[str, Any]:
    """Bound remote-minus-local clock offset using nonnegative round-trip delays."""
    if not samples or any(sample["before_ns"] > sample["after_ns"] for sample in samples):
        return {"consistent": False, "reason": "Missing or reversed round-trip observations"}
    lower = max(sample["remote_ns"] - sample["after_ns"] for sample in samples)
    upper = min(sample["remote_ns"] - sample["before_ns"] for sample in samples)
    return {"offset_lower_ns": lower, "offset_upper_ns": upper,
            "offset_interval_width_ns": upper - lower, "consistent": lower <= upper,
            "method": "remote monotonic timestamp lies between independently observed local send/read bounds; no symmetric latency assumption"}


def endpoint_clock_probe(url: str, *, samples: int = 16, expected_pid: int | None = None) -> dict[str, Any]:
    address = urlparse(url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This clock probe is restricted to the owned local reference endpoint")
    observations = []
    peers = []
    for _ in range(samples):
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=3)
        try:
            before = time.monotonic_ns()
            connection.request("GET", "/validation/clock")
            response = connection.getresponse()
            data = response.read()
            after = time.monotonic_ns()
            if response.status != 200:
                raise ValueError("Reference clock probe was rejected")
            peer = json.loads(data)
            if expected_pid is not None and peer["pid"] != expected_pid:
                raise ValueError("Reference PID differs from the owned endpoint")
            observations.append({"before_ns": before, "remote_ns": peer["monotonic_ns"], "after_ns": after})
            peers.append(peer)
        finally:
            connection.close()
    result = {**offset_bounds(observations), "samples": observations,
              "remote_pids": sorted({peer["pid"] for peer in peers}),
              "remote_clock_info": peers[0]["clock_info"], "remote_clock_domain": peers[0]["clock_domain"],
              "local_clock_info": vars(time.get_clock_info("monotonic"))}
    result["consistent"] = result["consistent"] and len(result["remote_pids"]) == 1
    return result


class TimingLog:
    """Writer work stays off the observed event loop; loss invalidates evidence."""

    def __init__(self, events: Path, health: Path, context: dict[str, Any], *,
                 queue_rows: int = 4096, queue_bytes: int = 16 * 1024**2):
        events.parent.mkdir(parents=True, exist_ok=True)
        self.events, self.health, self.context = events, health, context
        self.queue: queue.Queue[bytes] = queue.Queue(queue_rows)
        self.byte_bound = queue_bytes
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.stats = {"attempted": 0, "written": 0, "queued_bytes": 0, "overflow_count": 0,
                      "write_errors": 0, "callback_errors": 0, "queue_high_water_bytes": 0,
                      "queue_high_water_rows": 0, "queue_bound_rows": queue_rows,
                      "queue_bound_bytes": queue_bytes, "callback_count": 0,
                      "callback_work_ns": 0, "callback_max_work_ns": 0,
                      "state_overflow_count": 0, "state_unresolved_at_uninstall": 0}
        self.thread = threading.Thread(target=self._write, daemon=True, name="timing-observer-writer")
        self.thread.start()

    def emit(self, kind: str, lower_ns: int, upper_ns: int, **metadata: Any) -> None:
        with self.lock:
            sequence = self.stats["attempted"]
            self.stats["attempted"] += 1
        row = {"schema_version": "1", **self.context, "kind": kind, "sequence": sequence,
               "worker_id": "observed-baseline-process", "monotonic_ns": upper_ns,
               "clock_domain": f"host:{socket.gethostname()}:monotonic_ns",
               "source": "sys.monitoring_read_only", "observer_enabled": True,
               "lower_ns": lower_ns, "upper_ns": upper_ns,
               "uncertainty_width_ns": upper_ns - lower_ns, "metadata": metadata}
        encoded = (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        with self.lock:
            if self.stats["queued_bytes"] + len(encoded) > self.byte_bound:
                self.stats["overflow_count"] += 1
                return
            try:
                self.queue.put_nowait(encoded)
            except queue.Full:
                self.stats["overflow_count"] += 1
                return
            self.stats["queued_bytes"] += len(encoded)
            self.stats["queue_high_water_bytes"] = max(self.stats["queue_high_water_bytes"], self.stats["queued_bytes"])
            self.stats["queue_high_water_rows"] = max(self.stats["queue_high_water_rows"], self.queue.qsize())

    def callback_work(self, started: int) -> None:
        duration = time.monotonic_ns() - started
        self.stats["callback_count"] += 1
        self.stats["callback_work_ns"] += duration
        self.stats["callback_max_work_ns"] = max(self.stats["callback_max_work_ns"], duration)

    def _write(self) -> None:
        try:
            with self.events.open("wb") as stream:
                while not self.stopping.is_set() or not self.queue.empty():
                    try:
                        row = self.queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    try:
                        stream.write(row)
                        self.stats["written"] += 1
                    except OSError:
                        self.stats["write_errors"] += 1
                    finally:
                        with self.lock:
                            self.stats["queued_bytes"] -= len(row)
                        self.queue.task_done()
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            self.stats["write_errors"] += 1

    def close(self, provenance: dict[str, Any]) -> dict[str, Any]:
        self.stopping.set()
        self.thread.join(timeout=5)
        complete = (not self.thread.is_alive() and self.stats["attempted"] == self.stats["written"]
                    and not any(self.stats[key] for key in ["overflow_count", "write_errors", "callback_errors",
                                                           "state_overflow_count", "state_unresolved_at_uninstall"]))
        digest = hashlib.sha256()
        if self.events.exists():
            with self.events.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
        health = {**provenance, **self.stats, "complete": complete, "flush_complete": complete,
                  "events_sha256": digest.hexdigest(), "observer_enabled": True, "observer_version": OBSERVER_VERSION,
                  "qualification_scope": "This observer-enabled execution and named application/socket-return boundaries only",
                  "packet_arrival_observed": False, "total_instrumentation_slowdown_measured": False}
        temporary = self.health.with_suffix(".tmp")
        temporary.write_text(json.dumps(health, indent=2) + "\n")
        temporary.replace(self.health)
        return health


def _nested(code: CodeType, name: str) -> CodeType:
    for value in code.co_consts:
        if isinstance(value, CodeType):
            if value.co_name == name:
                return value
            try:
                return _nested(value, name)
            except ValueError:
                pass
    raise ValueError(f"Observed code object {name!r} is unavailable")


def _line_number(code: CodeType, text: str) -> int:
    source = Path(code.co_filename).read_text().splitlines()
    valid = {line for _, _, line in code.co_lines() if line is not None}
    matches = [line for line in valid if source[line - 1].strip().startswith(text)]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one observation site {text!r}; found {matches}")
    return matches[0]


class Observer:
    """PEP 669 callbacks only. No observed function, local, option or clock is assigned."""

    def __init__(self, client: Any, log: TimingLog, *, state_bound: int = 4096):
        if not hasattr(sys, "monitoring"):
            raise RuntimeError("Python 3.12+ sys.monitoring is required")
        self.log = log
        self.execute = client.LLMClient.execute.__code__
        self.sse = client._sse_events.__code__
        self.receive = _nested(self.execute, "receive")
        self.recv = asyncio.selector_events._SelectorSocketTransport._read_ready__data_received.__code__
        self.sites: dict[tuple[CodeType, int], str] = {}
        # Newer clients reuse a recorded body-read timestamp for nonstream
        # responses. That assignment is not a fresh clock observation; omit
        # those two hooks rather than fabricate a bracket around the old time.
        execute_source = Path(self.execute.co_filename).read_text().splitlines()
        fresh_nonstream_clock = any(
            execute_source[line - 1].strip() == "observed_at = time.perf_counter()"
            for _, _, line in self.execute.co_lines() if line is not None
        )
        for code, text, name in [
            (self.execute, "start = time.perf_counter()", "request_clock_before"),
            (self.execute, "record._timing_started_monotonic", "request_clock_after"),
            (self.execute, "async for event_type, event, observed_at", "response_bind"),
            (self.execute, "observed_at = time.perf_counter()", "nonstream_before"),
            (self.execute, "receive(_object(body)", "nonstream_after"),
            (self.execute, "now = time.perf_counter()", "completion_before"),
            (self.execute, "record.elapsed_ms =", "completion_after"),
            (self.sse, "observed_at = time.perf_counter()", "body_clock_before"),
            (self.sse, "consumed += len(chunk)", "body_clock_after"),
            (self.receive, 'trace("content"', "answer_ready"),
            (self.receive, "record.reasoning +=", "reasoning_ready"),
            (self.recv, "if not data:", "socket_bytes"),
        ]:
            if name in {"nonstream_before", "nonstream_after"} and not fresh_nonstream_clock:
                continue
            self.sites[(code, _line_number(code, text))] = name
        self.state_bound = state_bound
        self.pending: dict[tuple[FrameType, str], int] = {}
        self.native_pending: dict[FrameType, dict[str, Any]] = {}
        self.responses: dict[Any, str] = {}
        self.response_sockets: dict[Any, int | None] = {}
        self.body_sequences: dict[Any, int] = {}
        self.semantic_sequences: dict[str, int] = {}
        self.tool_id: int | None = None

    def _put(self, mapping: dict, key: Any, value: Any) -> bool:
        if key not in mapping and len(mapping) >= self.state_bound:
            self.log.stats["state_overflow_count"] += 1
            return False
        mapping[key] = value
        return True

    @staticmethod
    def _frame(code: CodeType) -> FrameType:
        frame = sys._getframe(1)
        while frame and frame.f_code is not code:
            frame = frame.f_back
        if frame is None:
            raise RuntimeError("Monitoring callback cannot locate the observed frame")
        return frame

    def install(self) -> None:
        monitor, events = sys.monitoring, sys.monitoring.events
        for identifier in (4, 3, 2):
            if monitor.get_tool(identifier) is None:
                monitor.use_tool_id(identifier, "giraffe-validation-observer")
                self.tool_id = identifier
                break
        if self.tool_id is None:
            raise RuntimeError("No unused monitoring tool ID; refusing to replace another observer")
        monitor.register_callback(self.tool_id, events.LINE, self._line)
        monitor.register_callback(self.tool_id, events.CALL, self._call)
        monitor.register_callback(self.tool_id, events.C_RETURN, self._c_return)
        monitor.register_callback(self.tool_id, events.C_RAISE, self._c_raise)
        monitor.register_callback(self.tool_id, events.PY_RETURN, self._exit)
        monitor.register_callback(self.tool_id, events.PY_UNWIND, self._exit)
        # CPython 3.12 does not permit a local PY_UNWIND event. This filtered
        # lifecycle hook observes no values outside the exact selected codes.
        monitor.set_events(self.tool_id, events.PY_UNWIND)
        for code in (self.execute, self.sse, self.receive):
            monitor.set_local_events(self.tool_id, code, events.LINE | events.PY_RETURN)
        monitor.set_local_events(self.tool_id, self.recv, events.LINE | events.CALL | events.C_RETURN |
                                 events.C_RAISE | events.PY_RETURN)

    def uninstall(self) -> None:
        if self.tool_id is None:
            return
        sys.monitoring.set_events(self.tool_id, 0)
        for code in (self.execute, self.sse, self.receive, self.recv):
            sys.monitoring.set_local_events(self.tool_id, code, 0)
        for event in (sys.monitoring.events.LINE, sys.monitoring.events.CALL,
                      sys.monitoring.events.C_RETURN, sys.monitoring.events.C_RAISE,
                      sys.monitoring.events.PY_RETURN, sys.monitoring.events.PY_UNWIND):
            sys.monitoring.register_callback(self.tool_id, event, None)
        sys.monitoring.free_tool_id(self.tool_id)
        self.tool_id = None
        for mapping in (self.pending, self.native_pending, self.responses, self.response_sockets,
                        self.body_sequences, self.semantic_sequences):
            self.log.stats["state_unresolved_at_uninstall"] += len(mapping)
            mapping.clear()

    def _exit(self, code: CodeType, offset: int, value: Any) -> None:
        if code not in (self.execute, self.sse, self.receive, self.recv):
            return
        entered = time.monotonic_ns()
        try:
            frame = self._frame(code)
            local = frame.f_locals
            if code is self.execute:
                self.semantic_sequences.pop(getattr(local.get("record"), "id", None), None)
            if code in (self.execute, self.sse):
                response = local.get("response")
                if response is not None:
                    self.responses.pop(response, None)
                    self.response_sockets.pop(response, None)
                    self.body_sequences.pop(response, None)
                for key in [key for key in self.pending if key[0] is frame]:
                    self.pending.pop(key, None)
            elif code is self.recv:
                self.native_pending.pop(frame, None)
        except Exception:
            self.log.stats["callback_errors"] += 1
        finally:
            self.log.callback_work(entered)

    def _line(self, code: CodeType, line: int) -> Any:
        entered = time.monotonic_ns()
        site = self.sites.get((code, line))
        if site is None:
            return sys.monitoring.DISABLE
        if site.endswith("_before"):
            key = None
            try:
                key = (self._frame(code), site.removesuffix("_before"))
                self._put(self.pending, key, entered)
            except Exception:
                self.log.stats["callback_errors"] += 1
            finally:
                self.log.callback_work(entered)
            if key in self.pending:
                # Last observer clock before returning to the unmodified
                # product clock call. No observer bookkeeping precedes it.
                self.pending[key] = time.monotonic_ns()
            return None
        try:
            frame = self._frame(code)
            local = frame.f_locals
            record = local.get("record")
            record_id = getattr(record, "id", None)
            response_id = getattr(record, "server_request_id", None)
            if site == "response_bind":
                response = local["response"]
                self._put(self.responses, response, record_id)
                if response not in self.response_sockets:
                    stream = response.extensions.get("network_stream")
                    sock = stream.get_extra_info("socket") if stream is not None else None
                    self._put(self.response_sockets, response, sock.fileno() if sock is not None else None)
                return None
            if site == "socket_bytes":
                pending = self.native_pending.pop(frame, None)
                data = local.get("data")
                if pending is not None and isinstance(data, bytes):
                    self.log.emit("client_socket_recv", pending["lower_ns"], time.monotonic_ns(),
                                  **{key: value for key, value in pending.items() if key != "lower_ns"},
                                  byte_count=len(data), wire_sha256=hashlib.sha256(data).hexdigest(),
                                  wire_b64=base64.b64encode(data).decode() if len(data) <= 65536 else None,
                                  wire_capture_omitted=len(data) > 65536,
                                  boundary="socket.recv returned bytes in asyncio transport; not packet arrival")
                return None
            if site in {"answer_ready", "reasoning_ready"}:
                content = local["answer"] if site == "answer_ready" else local["reasoning"]
                if not content:
                    return None
                sequence = self.semantic_sequences.get(record_id, 0)
                self._put(self.semantic_sequences, record_id, sequence + 1)
                self.log.emit("client_semantic_ready", entered, time.monotonic_ns(),
                              record_id=record_id, response_id=response_id,
                              semantic_seq=sequence, field="content" if site == "answer_ready" else "reasoning",
                              content=content, read_clock_hex=local["observed_at"].hex(),
                              timer_start_clock_hex=local["start"].hex(),
                              boundary="JSON parsed and semantic field accepted before record append")
                return None
            prefix = site.removesuffix("_after")
            lower = self.pending.pop((frame, prefix), None)
            if lower is None:
                raise RuntimeError(f"Missing before-observation for {site}")
            if site == "body_clock_after":
                response_key = local["response"]
                sequence = self.body_sequences.get(response_key, 0)
                self._put(self.body_sequences, response_key, sequence + 1)
                chunk = local["chunk"]
                self.log.emit("client_body_chunk", lower, entered,
                              record_id=self.responses.get(response_key), body_chunk_seq=sequence,
                              socket_fd=self.response_sockets.get(response_key),
                              byte_count=len(chunk), wire_sha256=hashlib.sha256(chunk).hexdigest(),
                              read_clock_hex=local["observed_at"].hex(), final=local["final"],
                              boundary="HTTP body bytes yielded into unchanged SSE reader before UTF8/SSE parsing")
            elif site == "nonstream_after":
                self.log.emit("client_body_complete", lower, entered, record_id=record_id,
                              read_clock_hex=local["observed_at"].hex(), byte_count=len(local["body"]),
                              boundary="nonstream complete body read before JSON parsing")
            else:
                value = local["start"] if site == "request_clock_after" else local["now"]
                self.log.emit("client_timer_start" if site == "request_clock_after" else "client_timer_end",
                              lower, entered, record_id=record_id, response_id=response_id,
                              clock_hex=value.hex(), boundary="existing product timer clock assignment")
        except Exception:
            self.log.stats["callback_errors"] += 1
        finally:
            self.log.callback_work(entered)
        return None

    def _call(self, code: CodeType, offset: int, function: Any, argument: Any) -> Any:
        entered = time.monotonic_ns()
        sock = getattr(function, "__self__", None)
        if not isinstance(sock, socket.socket) and isinstance(argument, socket.socket):
            sock = argument
        if getattr(function, "__name__", None) != "recv" or not isinstance(sock, socket.socket):
            return sys.monitoring.DISABLE
        try:
            frame = self._frame(code)
            self._put(self.native_pending, frame, {"lower_ns": entered, "socket_fd": sock.fileno(),
                                                   "peer": sock.getpeername(), "local": sock.getsockname()})
        except Exception:
            self.log.stats["callback_errors"] += 1
        finally:
            self.log.callback_work(entered)
        return None

    def _c_return(self, code: CodeType, offset: int, function: Any, argument: Any) -> None:
        if getattr(function, "__name__", None) != "recv":
            return
        entered = time.monotonic_ns()
        try:
            pending = self.native_pending.get(self._frame(code))
            if pending is not None:
                pending["native_return_upper_ns"] = entered
        except Exception:
            self.log.stats["callback_errors"] += 1
        finally:
            self.log.callback_work(entered)

    def _c_raise(self, code: CodeType, offset: int, function: Any, argument: Any) -> None:
        if getattr(function, "__name__", None) == "recv":
            entered = time.monotonic_ns()
            try:
                self.native_pending.pop(self._frame(code), None)
            except Exception:
                self.log.stats["callback_errors"] += 1
            finally:
                self.log.callback_work(entered)


def reference_intervals(rows: list[dict[str, Any]], *, uncertainty_budget_ns: int = 100_000) -> dict[str, Any]:
    """Derive references only from observer bounds, never reported timer values.

    Existing clock-value hex strings select which byte read supplied a semantic
    event. They are correlation keys only; no product timestamp supplies the
    reference duration. Missing/ambiguous links remain unqualified.
    """
    starts: dict[str, list[dict]] = {}
    ends: dict[str, list[dict]] = {}
    bodies: dict[tuple[str, str], list[dict]] = {}
    semantics: dict[str, list[dict]] = {}
    ordered_bodies: dict[str, list[dict]] = {}
    issues = []
    for sequence, row in enumerate(rows):
        if row.get("sequence") != sequence:
            issues.append("missing_or_duplicate_observer_sequence")
        lower, upper = row.get("lower_ns"), row.get("upper_ns")
        if not isinstance(lower, int) or not isinstance(upper, int) or lower > upper:
            issues.append("invalid_timestamp_interval")
            continue
        meta = row.get("metadata", {})
        identity = meta.get("record_id")
        if not identity:
            continue
        kind = row["kind"]
        if kind == "client_timer_start":
            starts.setdefault(identity, []).append(row)
        elif kind == "client_timer_end":
            ends.setdefault(identity, []).append(row)
        elif kind in {"client_body_chunk", "client_body_complete"} and meta.get("byte_count"):
            bodies.setdefault((identity, meta["read_clock_hex"]), []).append(row)
            ordered_bodies.setdefault(identity, []).append(row)
        elif kind == "client_semantic_ready":
            semantics.setdefault(identity, []).append(row)

    def delta(after: dict, before: dict) -> dict:
        low = after["lower_ns"] - before["upper_ns"]
        high = after["upper_ns"] - before["lower_ns"]
        width = high - low
        return {"lower_ms": low / 1e6, "upper_ms": high / 1e6,
                "uncertainty_width_ns": width,
                "qualified_for_observed_run": not issues and low >= 0 and width <= uncertainty_budget_ns,
                "uncertainty_budget_ns": uncertainty_budget_ns,
                "boundary": "independently bracketed body-read/product-timer boundaries in the observer-enabled run"}

    output = {}
    for identity in starts.keys() | ends.keys() | semantics.keys():
        result: dict[str, Any] = {"record_id": identity, "observer_enabled": True, "issues": list(issues)}
        if len(starts.get(identity, [])) != 1 or len(ends.get(identity, [])) != 1:
            result["issues"].append("missing_or_duplicate_timer_boundary")
            output[identity] = result
            continue
        start, end = starts[identity][0], ends[identity][0]
        if end["upper_ns"] < start["lower_ns"]:
            result["issues"].append("completion_precedes_start")
        body_rows = ordered_bodies.get(identity, [])
        for before, after in zip(body_rows, body_rows[1:]):
            if after["lower_ns"] < before["lower_ns"]:
                result["issues"].append("nonmonotonic_body_read_order")
        semantic_rows = semantics.get(identity, [])
        sequences = [row["metadata"].get("semantic_seq") for row in semantic_rows]
        if sequences != list(range(len(sequences))):
            result["issues"].append("missing_or_duplicate_semantic_sequence")
        for before, after in zip(semantic_rows, semantic_rows[1:]):
            if after["lower_ns"] < before["lower_ns"]:
                result["issues"].append("nonmonotonic_semantic_order")
        result["completion_ms"] = delta(end, start)
        answers = []
        for semantic in semantics.get(identity, []):
            meta = semantic["metadata"]
            candidates = bodies.get((identity, meta["read_clock_hex"]), [])
            if len(candidates) != 1:
                result["issues"].append("missing_or_ambiguous_body_read")
                continue
            body = candidates[0]
            if body["upper_ns"] < start["lower_ns"] or body["lower_ns"] > end["upper_ns"]:
                result["issues"].append("body_read_outside_request_interval")
                continue
            if semantic["upper_ns"] < body["lower_ns"]:
                result["issues"].append("semantic_parse_precedes_body_read")
            if meta["field"] == "reasoning" and "first_reasoning_ms" not in result:
                result["first_reasoning_ms"] = delta(body, start)
            if meta["field"] == "content" and meta["content"].strip():
                answers.append(body)
                if "first_answer_ms" not in result:
                    result["first_answer_ms"] = delta(body, start)
            result.setdefault("read_to_semantic_ready_ms", []).append(delta(semantic, body))
        if len(answers) > 1:
            gaps = [delta(after, before) for before, after in zip(answers, answers[1:])]
            result["answer_event_gaps_ms"] = gaps
            result["max_answer_event_gap_ms"] = {
                "lower_ms": max(0, max(gap["lower_ms"] for gap in gaps)),
                "upper_ms": max(gap["upper_ms"] for gap in gaps),
                "uncertainty_width_ns": max(gap["uncertainty_width_ns"] for gap in gaps),
                "qualified_for_observed_run": not result["issues"] and
                    all(gap["uncertainty_width_ns"] <= uncertainty_budget_ns for gap in gaps),
            }
        if result["issues"]:
            def invalidate(value: Any) -> None:
                if isinstance(value, dict):
                    if "qualified_for_observed_run" in value:
                        value["qualified_for_observed_run"] = False
                    for nested in value.values():
                        invalidate(nested)
                elif isinstance(value, list):
                    for nested in value:
                        invalidate(nested)
            invalidate(result)
        output[identity] = result
    return {"observer_version": OBSERVER_VERSION, "issues": issues, "requests": output,
            "qualification_scope": "observed execution only; body-yield timing is not packet arrival or unobserved-run timing"}


def reference_intervals_file(events: Path, output: Path, *, uncertainty_budget_ns: int = 100_000,
                             max_request_rows: int = 16384, max_request_bytes: int = 16 * 1024**2,
                             max_line_bytes: int = 2 * 1024**2, max_index_bytes: int = 256 * 1024**2,
                             min_free_headroom_bytes: int = 2 * 1024**3) -> dict[str, Any]:
    """Disk-backed grouping, streaming output, and explicit per-request RAM bounds.

    The SQLite index retains original offsets, not copies of event payloads.
    No full-log or full-result materialization occurs. A request that
    exceeds its analysis bound is retained as an unqualified explicit result.
    """
    database = output.with_suffix(output.suffix + ".sqlite")
    if database.exists() or output.exists():
        raise ValueError("Timing analysis destination exists; preserve previous evidence")
    if shutil.disk_usage(output.parent).free < min_free_headroom_bytes + max_index_bytes:
        raise ValueError("Insufficient free headroom for the bounded timing offset index")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA page_size=4096")
    connection.execute(f"PRAGMA max_page_count={max(1, max_index_bytes // 4096)}")
    connection.execute("PRAGMA cache_size=-2048")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("CREATE TABLE observations (record_id TEXT, sequence INTEGER, offset INTEGER, length INTEGER)")
    summary = {"input_rows": 0, "requests": 0, "global_sequence_errors": 0,
               "invalid_timestamp_intervals": 0, "request_bound_exceeded": 0,
               "requests_with_issues": 0, "oversized_lines": 0, "malformed_json_rows": 0,
               "peak_request_rows": 0, "peak_request_bytes": 0,
               "max_request_rows": max_request_rows, "max_request_bytes": max_request_bytes,
               "sqlite_cache_bytes": 2 * 1024**2, "sqlite_bound_bytes": max_index_bytes,
               "max_line_bytes": max_line_bytes, "complete": False}
    try:
        with events.open("rb") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline(max_line_bytes + 1)
                if not line:
                    break
                if not line.strip():
                    continue
                sequence = summary["input_rows"]
                summary["input_rows"] += 1
                if len(line) > max_line_bytes:
                    summary["oversized_lines"] += 1
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(max_line_bytes + 1)
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    summary["malformed_json_rows"] += 1
                    continue
                if row.get("sequence") != sequence:
                    summary["global_sequence_errors"] += 1
                if (not isinstance(row.get("lower_ns"), int) or not isinstance(row.get("upper_ns"), int)
                        or row["lower_ns"] > row["upper_ns"]):
                    summary["invalid_timestamp_intervals"] += 1
                identity = row.get("metadata", {}).get("record_id")
                if identity:
                    connection.execute("INSERT INTO observations VALUES (?,?,?,?)", (identity, sequence, offset, len(line)))
                if sequence % 4096 == 0:
                    connection.commit()
                    if shutil.disk_usage(output.parent).free < min_free_headroom_bytes:
                        raise ValueError("Timing offset index reached free-disk headroom")
        connection.commit()
        connection.execute("CREATE INDEX request_sequence ON observations(record_id, sequence)")
        connection.commit()
        with output.open("w") as destination, events.open("rb") as source:
            identities = connection.execute("SELECT DISTINCT record_id FROM observations ORDER BY record_id")
            for (identity,) in identities:
                rows = []
                byte_count = 0
                exceeded = False
                for (offset, length) in connection.execute(
                        "SELECT offset,length FROM observations WHERE record_id=? ORDER BY sequence", (identity,)):
                    byte_count += length
                    if len(rows) >= max_request_rows or byte_count > max_request_bytes:
                        exceeded = True
                        break
                    source.seek(offset)
                    row = json.loads(source.read(length))
                    row["sequence"] = len(rows)
                    rows.append(row)
                summary["peak_request_rows"] = max(summary["peak_request_rows"], len(rows))
                summary["peak_request_bytes"] = max(summary["peak_request_bytes"], min(byte_count, max_request_bytes))
                if exceeded:
                    summary["request_bound_exceeded"] += 1
                    result = {"record_id": identity, "observer_enabled": True,
                              "issues": ["per_request_analysis_bound_exceeded"],
                              "qualified_for_observed_run": False}
                else:
                    reference = reference_intervals(rows, uncertainty_budget_ns=uncertainty_budget_ns)
                    result = reference["requests"].get(identity, {"record_id": identity,
                              "issues": ["missing_timer_boundaries"], "qualified_for_observed_run": False})
                if any(summary[key] for key in ("global_sequence_errors", "invalid_timestamp_intervals",
                                                "oversized_lines", "malformed_json_rows")):
                    result["issues"].append("global_observation_evidence_invalid")
                    def invalidate(value: Any) -> None:
                        if isinstance(value, dict):
                            if "qualified_for_observed_run" in value:
                                value["qualified_for_observed_run"] = False
                            for nested in value.values():
                                invalidate(nested)
                        elif isinstance(value, list):
                            for nested in value:
                                invalidate(nested)
                    invalidate(result)
                destination.write(json.dumps(result, separators=(",", ":")) + "\n")
                summary["requests"] += 1
                summary["requests_with_issues"] += bool(result.get("issues"))
        summary["complete"] = not any(summary[key] for key in
            ("global_sequence_errors", "invalid_timestamp_intervals", "request_bound_exceeded",
             "requests_with_issues", "oversized_lines", "malformed_json_rows")) and summary["requests"] > 0
    except (ValueError, OSError, sqlite3.Error) as error:
        summary["complete"] = False
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        connection.close()
    summary["index_bytes"] = database.stat().st_size
    output.with_suffix(output.suffix + ".analysis-health.json").write_text(json.dumps(summary, indent=2) + "\n")
    if summary["complete"]:
        database.unlink()
    return summary


def validate_timing_health(directory: Path, *, expected_observer_sha256: str | None = None) -> dict[str, Any]:
    """Validate observed evidence independently of perturbation/overhead gates."""
    reasons = []
    path = directory / "timing-health.json"
    if not path.exists():
        return {"reference_valid_for_observed_run": False, "reasons": ["timing_health_missing"]}
    health = json.loads(path.read_text())
    events = directory / "timing-events.jsonl"
    digest = hashlib.sha256()
    rows = 0
    if events.exists():
        with events.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
                rows += chunk.count(b"\n")
    else:
        reasons.append("timing_events_missing")
    if not health.get("complete") or not health.get("flush_complete"):
        reasons.append("incomplete_timing_recorder")
    for key in ("overflow_count", "write_errors", "callback_errors", "state_overflow_count",
                "state_unresolved_at_uninstall"):
        if health.get(key) != 0:
            reasons.append(key)
    if rows != health.get("written") or rows != health.get("attempted"):
        reasons.append("timing_row_count_mismatch")
    if digest.hexdigest() != health.get("events_sha256"):
        reasons.append("timing_events_hash_mismatch")
    if not rows:
        reasons.append("no_timing_observations")
    if not health.get("source_unchanged") or health.get("source_sha256_before") != health.get("source_sha256_after"):
        reasons.append("product_source_identity_unconfirmed")
    if expected_observer_sha256 and health.get("observer_source_sha256") != expected_observer_sha256:
        reasons.append("observer_source_hash_mismatch")
    calibrations = [health.get(name, {}) for name in ("clock_calibration_before", "clock_calibration_after")]
    if not all(item.get("consistent") for item in calibrations):
        reasons.append("same_process_clock_calibration_missing_or_inconsistent")
        mapping = None
    else:
        lower = max(item["offset_lower_ns"] for item in calibrations)
        upper = min(item["offset_upper_ns"] for item in calibrations)
        mapping = {"offset_lower_ns": lower, "offset_upper_ns": upper, "width_ns": upper - lower}
        if lower > upper:
            reasons.append("same_process_clock_mapping_shifted")
    probes = [health.get(name, {}) for name in ("endpoint_clock_probe_before", "endpoint_clock_probe_after")]
    cross_process = {"valid": False, "reason": "No complete bounded endpoint round-trip clock observation"}
    if all(probe.get("consistent") for probe in probes) and probes[0].get("remote_pids") == probes[1].get("remote_pids"):
        lower = max(probe["offset_lower_ns"] for probe in probes)
        upper = min(probe["offset_upper_ns"] for probe in probes)
        cross_process = {"valid": lower <= upper, "offset_lower_ns": lower, "offset_upper_ns": upper,
                         "uncertainty_width_ns": upper - lower, "remote_pids": probes[0]["remote_pids"]}
    return {"reference_valid_for_observed_run": not reasons, "reasons": reasons,
            "rows": rows, "same_process_clock_mapping": mapping, "cross_process_clock_mapping": cross_process,
            "observer_enabled": True, "overhead_gate_state": "requires separate matched-control evidence",
            "qualifies_unobserved_run": False,
            "scope": "Specific independently bracketed metrics in this instrumented run; never packet arrival or retroactive old-run qualification"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--health", type=Path, required=True)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--reference-url")
    parser.add_argument("--reference-pid", type=int)
    parser.add_argument("cli", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    baseline = args.baseline.resolve()
    sys.path.insert(0, str(baseline / "src"))
    client = importlib.import_module("giraffe.client")
    source = Path(client.__file__).resolve()
    if not source.is_relative_to(baseline / "src"):
        raise RuntimeError("Observed product import escaped the pinned baseline")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    context = json.loads(args.context.read_text()) if args.context else {}
    log = TimingLog(args.events, args.health, context)
    observer = Observer(client, log)
    provenance = {"baseline": str(baseline), "import_proof": str(source), "source_sha256_before": before,
                  "observer_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "clock_calibration_before": clock_calibration(),
                  "entrypoint": "runpy.run_module('giraffe.cli',run_name='__main__')",
                  "cli_argv": args.cli[1:] if args.cli[:1] == ["--"] else args.cli}
    if args.reference_url:
        provenance["endpoint_clock_probe_before"] = endpoint_clock_probe(
            args.reference_url, expected_pid=args.reference_pid)
    sys.argv = ["giraffe", *provenance["cli_argv"]]
    try:
        observer.install()
        runpy.run_module("giraffe.cli", run_name="__main__")
    finally:
        observer.uninstall()
        provenance["clock_calibration_after"] = clock_calibration()
        if args.reference_url:
            try:
                provenance["endpoint_clock_probe_after"] = endpoint_clock_probe(
                    args.reference_url, expected_pid=args.reference_pid)
            except Exception as error:
                provenance["endpoint_clock_probe_after"] = {"consistent": False, "error": str(error)}
        provenance["source_sha256_after"] = hashlib.sha256(source.read_bytes()).hexdigest()
        provenance["source_unchanged"] = provenance["source_sha256_after"] == before
        log.close(provenance)


if __name__ == "__main__":
    main()
