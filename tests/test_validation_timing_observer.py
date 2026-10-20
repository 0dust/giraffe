"""Independent bounds, socket-return controls and deliberately broken evidence."""

from __future__ import annotations

import asyncio.selector_events
import copy
import hashlib
import json
import socket
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

import giraffe.client as product_client
from validation_suite.timing_observer import (Observer, TimingLog, clock_calibration, offset_bounds,
                                             reference_intervals, reference_intervals_file,
                                             validate_timing_health)


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_clock_mapping_has_actual_outward_bounds():
    measured = clock_calibration(64)
    assert measured["samples"] == 64 and measured["consistent"]
    assert measured["offset_lower_ns"] <= measured["offset_upper_ns"]
    assert measured["clock_read_bracket_max_ns"] >= measured["clock_read_bracket_min_ns"] >= 0
    assert measured["offset_interval_width_ns"] >= 0


def test_monitor_observes_real_socket_recv_without_replacing_functions(tmp_path):
    source = product_client.LLMClient.execute
    original_recv = socket.socket.recv
    log = TimingLog(tmp_path / "timing-events.jsonl", tmp_path / "timing-health.json", {})
    observer = Observer(product_client, log)
    left, right = socket.socketpair()
    received = []
    transport = SimpleNamespace(_conn_lost=False, _sock=right, max_size=4096,
                                _protocol=SimpleNamespace(data_received=received.append))
    payload = 'numbered-01 café 🦒'.encode()
    before = time.monotonic_ns()
    try:
        observer.install()
        left.sendall(payload)
        asyncio.selector_events._SelectorSocketTransport._read_ready__data_received(transport)
    finally:
        observer.uninstall()
        left.close()
        right.close()
    after = time.monotonic_ns()
    health = log.close({})
    assert source is product_client.LLMClient.execute and original_recv is socket.socket.recv
    assert received == [payload]
    rows = read_rows(log.events)
    assert len(rows) == 1 and rows[0]["kind"] == "client_socket_recv"
    row = rows[0]
    assert before <= row["lower_ns"] <= row["metadata"]["native_return_upper_ns"] <= row["upper_ns"] <= after
    assert row["metadata"]["byte_count"] == len(payload)
    assert row["metadata"]["wire_sha256"] == hashlib.sha256(payload).hexdigest()
    assert health["complete"] and health["callback_count"] >= 3
    assert not health["packet_arrival_observed"]


class BytesControl(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize("mode", ["bundled", "fragmented_utf8", "reasoning", "error"])
async def test_body_read_brackets_cover_actual_unchanged_sse_reader(tmp_path, mode):
    data = ('data: {"id":"known","choices":[{"delta":{"reasoning_content":"think"}}]}\r\n\r\n'
            'data: {"id":"known","choices":[{"delta":{"content":"café 🦒"}}]}\r\n\r\n'
            'data: [DONE]\r\n\r\n').encode()
    if mode == "error":
        data = b'data: {"error":{"message":"expected synthetic error"}}\n\n'
    chunks = [bytes([value]) for value in data] if mode == "fragmented_utf8" else [data]
    response = httpx.Response(200, stream=BytesControl(chunks),
                             request=httpx.Request("POST", "http://synthetic/v1/chat/completions"))
    log = TimingLog(tmp_path / "events.jsonl", tmp_path / "health.json", {})
    observer = Observer(product_client, log)
    observer.responses[response] = "independent-control"
    try:
        observer.install()
        parsed = [value async for value in product_client._sse_events(response)]
    finally:
        observer.uninstall()
    health = log.close({})
    assert health["complete"] and not health["callback_errors"]
    rows = read_rows(log.events)
    bodies = [r for r in rows if r["kind"] == "client_body_chunk" and r["metadata"]["byte_count"]]
    assert sum(r["metadata"]["byte_count"] for r in bodies) == len(data)
    assert len(bodies) == len(chunks)
    assert all(r["lower_ns"] <= r["upper_ns"] for r in bodies)
    assert {r["metadata"]["record_id"] for r in bodies} == {"independent-control"}
    if mode != "error":
        assert len(parsed) == 3
        assert json.loads(parsed[1][1])["choices"][0]["delta"]["content"] == "café 🦒"
    else:
        assert json.loads(parsed[0][1])["error"]["message"] == "expected synthetic error"


def fixture_rows():
    # Independently specified milliseconds: start100, reasoning110, answers120/125,
    # completion130. Each actual clock read is bracketed by 20 nanoseconds.
    events = [
        ("client_timer_start", 100, {"record_id": "r", "clock_hex": "unused-start"}),
        ("client_body_chunk", 110, {"record_id": "r", "read_clock_hex": "reason", "byte_count": 10}),
        ("client_semantic_ready", 111, {"record_id": "r", "read_clock_hex": "reason", "field": "reasoning", "content": "think"}),
        ("client_body_chunk", 120, {"record_id": "r", "read_clock_hex": "a", "byte_count": 10}),
        ("client_semantic_ready", 121, {"record_id": "r", "read_clock_hex": "a", "field": "content", "content": "4"}),
        ("client_body_chunk", 125, {"record_id": "r", "read_clock_hex": "b", "byte_count": 10}),
        ("client_semantic_ready", 126, {"record_id": "r", "read_clock_hex": "b", "field": "content", "content": "2"}),
        ("client_timer_end", 130, {"record_id": "r", "clock_hex": "unused-end"}),
    ]
    rows = [{"sequence": sequence, "kind": kind, "lower_ns": tick * 1_000_000 - 10,
             "upper_ns": tick * 1_000_000 + 10, "metadata": meta}
            for sequence, (kind, tick, meta) in enumerate(events)]
    for sequence, row in enumerate(row for row in rows if row["kind"] == "client_semantic_ready"):
        row["metadata"]["semantic_seq"] = sequence
    return rows


def test_reference_arithmetic_uses_observer_bounds_and_correct_semantic_kind():
    rows = fixture_rows()
    result = reference_intervals(rows)["requests"]["r"]
    assert result["first_reasoning_ms"]["lower_ms"] == pytest.approx(9.99998)
    assert result["first_answer_ms"]["lower_ms"] == pytest.approx(19.99998)
    assert result["completion_ms"]["upper_ms"] == pytest.approx(30.00002)
    assert result["max_answer_event_gap_ms"]["lower_ms"] == pytest.approx(4.99998)
    assert result["first_answer_ms"]["uncertainty_width_ns"] == 40
    assert result["first_answer_ms"]["qualified_for_observed_run"]
    # Product clock values can be arbitrarily corrupted without shifting these
    # independent reference intervals; their only role is read-event correlation.
    changed = copy.deepcopy(rows)
    changed[0]["metadata"]["clock_hex"] = "arbitrarily-wrong"
    assert reference_intervals(changed)["requests"]["r"]["first_answer_ms"] == result["first_answer_ms"]


@pytest.mark.parametrize("fault", ["omit", "duplicate", "reverse_bounds", "shift_read", "hide_read", "ambiguous_read"])
def test_broken_observer_controls_never_qualify(fault):
    rows = fixture_rows()
    if fault == "omit":
        rows.pop(3)
    elif fault == "duplicate":
        rows.insert(3, copy.deepcopy(rows[3]))
    elif fault == "reverse_bounds":
        rows[3]["upper_ns"] = rows[3]["lower_ns"] - 1
    elif fault == "shift_read":
        rows[3]["lower_ns"] += 100_000_000
        rows[3]["upper_ns"] += 100_000_000
    elif fault == "hide_read":
        rows[3]["metadata"]["read_clock_hex"] = "hidden"
    else:
        duplicate = copy.deepcopy(rows[3])
        rows.append(duplicate)
        for index, row in enumerate(rows):
            row["sequence"] = index
    result = reference_intervals(rows)
    request = result["requests"]["r"]
    assert request["issues"]
    assert not request["completion_ms"]["qualified_for_observed_run"]


def test_wide_observation_interval_is_retained_but_unqualified():
    rows = fixture_rows()
    rows[3]["lower_ns"] -= 200_000
    result = reference_intervals(rows, uncertainty_budget_ns=100_000)["requests"]["r"]
    assert result["first_answer_ms"]["uncertainty_width_ns"] == 200_040
    assert not result["first_answer_ms"]["qualified_for_observed_run"]


def test_bundled_answer_events_have_zero_gap_with_nonzero_uncertainty():
    rows = fixture_rows()
    rows.pop(5)
    rows[5]["metadata"]["read_clock_hex"] = "a"
    for sequence, row in enumerate(rows):
        row["sequence"] = sequence
    result = reference_intervals(rows)["requests"]["r"]["max_answer_event_gap_ms"]
    assert result["lower_ms"] == 0 and result["upper_ms"] == pytest.approx(.00002)
    assert result["uncertainty_width_ns"] == 40


def test_reversed_body_reads_and_all_nested_metrics_fail_closed():
    rows = fixture_rows()
    rows[5]["lower_ns"] -= 8_000_000
    rows[5]["upper_ns"] -= 8_000_000
    result = reference_intervals(rows)["requests"]["r"]
    assert "nonmonotonic_body_read_order" in result["issues"]
    assert not result["max_answer_event_gap_ms"]["qualified_for_observed_run"]
    assert all(not item["qualified_for_observed_run"] for item in result["answer_event_gaps_ms"])
    assert all(not item["qualified_for_observed_run"] for item in result["read_to_semantic_ready_ms"])


def test_clock_offset_round_trip_bound_does_not_assume_symmetric_delay():
    result = offset_bounds([{"before_ns": 100, "remote_ns": 135, "after_ns": 140},
                            {"before_ns": 200, "remote_ns": 220, "after_ns": 230}])
    assert result["consistent"]
    assert result["offset_lower_ns"] == -5 and result["offset_upper_ns"] == 20
    assert not offset_bounds([{"before_ns": 100, "remote_ns": 10, "after_ns": 110},
                              {"before_ns": 200, "remote_ns": 1000, "after_ns": 210}])["consistent"]


async def test_many_sequential_and_concurrent_sse_frames_leave_no_observer_state(tmp_path):
    log = TimingLog(tmp_path / "events.jsonl", tmp_path / "health.json", {})
    observer = Observer(product_client, log)
    async def one(index):
        payload = f'data: {{"id":"r{index}","choices":[]}}\n\ndata: [DONE]\n\n'.encode()
        response = httpx.Response(200, stream=BytesControl([payload]),
                                  request=httpx.Request("POST", "http://synthetic"))
        observer._put(observer.responses, response, f"r{index}")
        return [event async for event in product_client._sse_events(response)]
    observer.install()
    try:
        for index in range(100):
            await one(index)
        await __import__("asyncio").gather(*(one(index) for index in range(100, 120)))
        assert not observer.pending and not observer.responses and not observer.body_sequences
    finally:
        observer.uninstall()
    assert log.close({})["complete"]


async def test_cancelled_sse_unwind_releases_frame_state(tmp_path):
    import asyncio
    waiting = asyncio.Event()
    class HangingBytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'
            waiting.set()
            await asyncio.Future()
    response = httpx.Response(200, stream=HangingBytes(),
                              request=httpx.Request("POST", "http://synthetic"))
    log = TimingLog(tmp_path / "events.jsonl", tmp_path / "health.json", {})
    observer = Observer(product_client, log)
    observer._put(observer.responses, response, "cancelled")
    async def consume():
        return [event async for event in product_client._sse_events(response)]
    observer.install()
    try:
        task = asyncio.create_task(consume())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not observer.pending and not observer.responses and not observer.body_sequences
    finally:
        observer.uninstall()
    assert log.close({})["complete"]


def test_observer_state_has_explicit_bound(tmp_path):
    log = TimingLog(tmp_path / "events.jsonl", tmp_path / "health.json", {})
    observer = Observer(product_client, log, state_bound=1)
    assert observer._put(observer.responses, "one", "first")
    assert not observer._put(observer.responses, "two", "second")
    observer.responses.clear()
    assert not log.close({})["complete"]


def test_disk_backed_analysis_handles_interleaving_and_explicit_request_bounds(tmp_path):
    first, second = fixture_rows(), fixture_rows()
    for row in second:
        row["metadata"]["record_id"] = "second"
    interleaved = [row for pair in zip(first, second) for row in pair]
    for sequence, row in enumerate(interleaved):
        row["sequence"] = sequence
    events = tmp_path / "events.jsonl"
    events.write_text("".join(json.dumps(row) + "\n" for row in interleaved))
    output = tmp_path / "reference.jsonl"
    summary = reference_intervals_file(events, output)
    assert summary["complete"] and summary["requests"] == 2
    assert summary["peak_request_rows"] == 8
    assert all(row["first_answer_ms"]["qualified_for_observed_run"] for row in read_rows(output))
    bounded = reference_intervals_file(events, tmp_path / "bounded.jsonl", max_request_rows=2)
    assert not bounded["complete"] and bounded["request_bound_exceeded"] == 2
    assert all("per_request_analysis_bound_exceeded" in row["issues"]
               for row in read_rows(tmp_path / "bounded.jsonl"))


def test_streaming_analysis_rejects_empty_oversized_and_missing_timer_evidence(tmp_path):
    events = tmp_path / "empty.jsonl"
    events.write_text("")
    assert not reference_intervals_file(events, tmp_path / "empty-output.jsonl")["complete"]
    events = tmp_path / "oversized.jsonl"
    events.write_text('x' * 256 + '\n')
    result = reference_intervals_file(events, tmp_path / "oversized-output.jsonl", max_line_bytes=64)
    assert result["oversized_lines"] == 1 and not result["complete"]
    events = tmp_path / "missing.jsonl"
    row = fixture_rows()[0]
    events.write_text(json.dumps(row) + "\n")
    result = reference_intervals_file(events, tmp_path / "missing-output.jsonl")
    assert result["requests_with_issues"] == 1 and not result["complete"]


def test_timing_health_validates_actual_hash_clock_mapping_and_keeps_overhead_separate(tmp_path):
    log = TimingLog(tmp_path / "timing-events.jsonl", tmp_path / "timing-health.json", {})
    log.emit("client_timer_start", 1, 2, record_id="r")
    calibration = clock_calibration(64)
    log.close({"source_unchanged": True, "source_sha256_before": "known", "source_sha256_after": "known",
               "clock_calibration_before": calibration, "clock_calibration_after": calibration})
    result = validate_timing_health(tmp_path)
    assert result["reference_valid_for_observed_run"]
    assert not result["qualifies_unobserved_run"]
    assert result["overhead_gate_state"] == "requires separate matched-control evidence"
    with log.events.open("ab") as output:
        output.write(b'{}\n')
    assert not validate_timing_health(tmp_path)["reference_valid_for_observed_run"]


def test_bounded_timing_evidence_overflow_invalidates_health(tmp_path):
    log = TimingLog(tmp_path / "events.jsonl", tmp_path / "health.json", {}, queue_bytes=1)
    log.emit("known", 1, 2, value="safe")
    health = log.close({})
    assert health["overflow_count"] == 1 and not health["complete"]
    assert health["queue_high_water_bytes"] == 0


def test_uninstall_releases_only_its_own_monitoring_tool(tmp_path):
    log = TimingLog(tmp_path / "events.jsonl", tmp_path / "health.json", {})
    observer = Observer(product_client, log)
    before = [sys.monitoring.get_tool(i) for i in range(6)]
    observer.install()
    identifier = observer.tool_id
    observer.uninstall()
    assert [sys.monitoring.get_tool(i) for i in range(6)] == before
    assert sys.monitoring.get_local_events(identifier, observer.execute) == 0
    assert log.close({})["complete"]
