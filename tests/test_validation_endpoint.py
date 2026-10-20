"""Verify the reference using stdlib HTTP, explicit goldens, and recorder faults."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import time
from urllib.parse import urlparse

import pytest

from validation_suite.endpoint import EndpointProcess, Recorder
from validation_suite.wire import build_response


def test_openai_object_metadata_preserves_stream_semantics():
    payload = {"stream": True, "messages": [{"role": "user", "content": "Return 42"}]}
    original = build_response(payload, {"response": "42"}, "same-id")
    compatible = build_response(payload, {"response": "42", "openai_object_metadata": True}, "same-id")
    assert original.expected == compatible.expected
    old = sse_json(b"".join(e.raw for e in original.events))
    new = sse_json(b"".join(e.raw for e in compatible.events))
    for event in new:
        assert event.pop("object") == "chat.completion.chunk"
    assert old == new
    assert compatible.events[-1].raw == b"data: [DONE]\n\n"


def rows(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "receiver-events.jsonl").read_text().splitlines()]


def transact(url: str, *, stream: bool = True, headers: dict | None = None,
             model: str = "reference", timeout: float = 3) -> tuple[int, bytes]:
    address = urlparse(url)
    client = http.client.HTTPConnection(address.hostname, address.port, timeout=timeout)
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": "Return 42"}],
                          "stream": stream, "max_tokens": 128}).encode()
    try:
        client.request("POST", "/v1/chat/completions", body=payload,
                       headers={"Content-Type": "application/json", **(headers or {})})
        response = client.getresponse()
        return response.status, response.read()
    finally:
        client.close()


def sse_json(body: bytes) -> list[dict]:
    # Independently specified SSE decoder for this hand-checkable fixture corpus.
    blocks = body.decode().replace("\r\n", "\n").split("\n\n")
    values = []
    for block in blocks:
        data = "\n".join(line[5:].lstrip(" ") for line in block.splitlines()
                         if line.startswith("data:"))
        if data and data != "[DONE]":
            values.append(json.loads(data))
    return values


def assert_health(directory: Path, endpoint: EndpointProcess) -> list[dict]:
    recorded = rows(directory)
    health = endpoint.health
    assert health["complete"] and health["flush_complete"] and health["shutdown_complete"]
    assert health["written"] == health["attempted"] == len(recorded)
    assert [row["sequence"] for row in recorded] == list(range(len(recorded)))
    assert health["last_sequence"] == health["last_checkpoint_sequence"] == len(recorded) - 1
    assert health["events_sha256"] == hashlib.sha256(
        (directory / "receiver-events.jsonl").read_bytes()).hexdigest()
    assert recorded[-1]["kind"] == "recorder_status"
    assert recorded[-1]["metadata"]["written"] == len(recorded)
    assert health["queue_high_water_bytes"] <= health["queue_bound_bytes"]
    assert health["queue_high_water_rows"] <= health["queue_bound_rows"]
    assert health["recording_overhead_ns"] > 0
    return recorded


@pytest.mark.parametrize("fixture,stream,answer", [
    ("normal", True, "42"), ("normal", False, "42"),
    ("fragmented_utf8", True, "café 🦒"), ("bundled", True, "42"),
    ("reasoning", True, "42"), ("reasoning", False, "42"),
    ("usage_trailer", True, "42"), ("no_usage", True, "42"),
    ("no_usage", False, "42"), ("malformed_usage", True, "42"),
    ("malformed_usage", False, "42"), ("incorrect", True, "INCORRECT"),
])
def test_independent_client_content_and_actual_reference_records(tmp_path, fixture, stream, answer):
    scenario = {"fixture": fixture, "response": "café 🦒" if fixture == "fragmented_utf8" else "42"}
    with EndpointProcess(tmp_path, scenario=scenario) as endpoint:
        assert endpoint.process.pid != os.getpid()
        status, body = transact(endpoint.url, stream=stream, headers={
            "X-Validation-Logical-Id": "Logical-MixedCase", "X-Validation-Attempt-Id": "Attempt-A",
            "X-Validation-Phase": "probe"})
        assert status == 200
    recorded = assert_health(tmp_path, endpoint)
    receipts = [row for row in recorded if row["kind"] == "request_received"]
    assert len(receipts) == 1
    assert receipts[0]["logical_request_id"] == "Logical-MixedCase"
    assert receipts[0]["attempt_id"] == "Attempt-A" and receipts[0]["phase"] == "probe"
    assert receipts[0]["metadata"]["correlation_confidence"] == "explicit_header"
    values = sse_json(body) if stream else [json.loads(body)]
    content = "".join(choice.get("delta", choice.get("message", {})).get("content") or ""
                      for value in values for choice in value.get("choices", []))
    assert content == answer
    assert {value["id"] for value in values} == {"reference-00000001"}
    emitted = [row for row in recorded if row["kind"] == "response_event"]
    assert b"".join(base64.b64decode(row["metadata"]["wire_b64"]) for row in emitted) == body
    assert [row["metadata"]["event_seq"] for row in emitted] == list(range(len(emitted)))
    assert receipts[0]["monotonic_ns"] <= emitted[0]["monotonic_ns"]
    assert all(row["source"] == "endpoint_application" for row in recorded)
    assert all(row["metadata"]["observation_boundary"] == "application_drain_return" for row in emitted)
    if fixture == "reasoning":
        assert values[0]["choices"][0].get("delta", values[0]["choices"][0].get("message"))[
            "reasoning_content"] == "Think first."
    if fixture == "no_usage":
        assert all("usage" not in value for value in values)
    if fixture == "fragmented_utf8":
        assert b"\r\n" in body and b"\xf0\x9f\xa6\x92" in body
        assert emitted[0]["metadata"]["fragmented"]


def test_numbered_bundled_events_preserve_order_and_shared_observation(tmp_path):
    with EndpointProcess(tmp_path, scenario={"fixture": "bundled", "events": ["1|", "2|", "3"]}) as endpoint:
        _, body = transact(endpoint.url)
    recorded = assert_health(tmp_path, endpoint)
    emitted = [r for r in recorded if r["kind"] == "response_event"]
    assert [r["metadata"]["content"] for r in emitted[:3]] == ["1|", "2|", "3"]
    assert len({r["monotonic_ns"] for r in emitted}) == 1
    assert len([r for r in recorded if r["kind"] == "response_write_start"]) == 1
    assert len(sse_json(body)) == 4  # Three answer events and one finish/usage trailer.


def test_tool_call_fragments_reconstruct_hand_written_golden(tmp_path):
    with EndpointProcess(tmp_path, scenario={"fixture": "tool_calls"}) as endpoint:
        _, body = transact(endpoint.url)
    assert_health(tmp_path, endpoint)
    tool_deltas = [v["choices"][0]["delta"]["tool_calls"][0] for v in sse_json(body)
                   if v["choices"][0]["delta"].get("tool_calls")]
    assert tool_deltas[0]["id"] == "call-1"
    assert "".join(t["function"]["name"] for t in tool_deltas) == "get_weather"
    assert json.loads("".join(t["function"]["arguments"] for t in tool_deltas)) == {"city": "Delhi"}


@pytest.mark.parametrize("fixture,status,needle,terminal", [
    ("http_error", 503, b"injected unavailable", True),
    ("stream_error_before", 200, b"injected stream failure", False),
    ("stream_error_after", 200, b"injected after partial answer", False),
    ("malformed_stream", 200, b"{malformed-json", False),
    ("missing_termination", 200, b'"finish_reason":"stop"', False),
    ("truncated", 200, b"unfinished", False),
])
def test_faults_preserve_receipts_partial_wire_and_terminal_outcomes(tmp_path, fixture, status, needle, terminal):
    with EndpointProcess(tmp_path, scenario={"fixture": fixture}) as endpoint:
        actual_status, body = transact(endpoint.url)
    recorded = assert_health(tmp_path, endpoint)
    assert actual_status == status and needle in body
    assert b"[DONE]" not in body
    finished = [row for row in recorded if row["kind"] == "response_finished"]
    assert len(finished) == 1 and finished[0]["metadata"]["terminal"] is terminal
    assert finished[0]["metadata"]["outcome"] in {"http_error", "protocol_error"}
    if fixture == "stream_error_before":
        assert b'"content":"42"' not in body
    if fixture == "stream_error_after":
        assert b'"content":"42"' in body


def test_disconnect_is_observed_without_fabricated_complete_response(tmp_path):
    with EndpointProcess(tmp_path, scenario={"fixture": "disconnect"}) as endpoint:
        with pytest.raises(http.client.RemoteDisconnected):
            transact(endpoint.url)
    recorded = assert_health(tmp_path, endpoint)
    assert not [row for row in recorded if row["kind"] == "response_event"]
    assert next(r for r in recorded if r["kind"] == "response_finished")["metadata"]["terminal"] is False


def test_shutdown_cancels_hanging_reference_and_flushes_partial_evidence(tmp_path):
    endpoint = EndpointProcess(tmp_path, scenario={"fixture": "hang"}).start()
    address = urlparse(endpoint.url)
    client = socket.create_connection((address.hostname, address.port), timeout=2)
    body = b'{"model":"reference","messages":[],"stream":true}'
    client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: " +
                   str(len(body)).encode() + b"\r\n\r\n" + body)
    time.sleep(.05)
    started = time.monotonic()
    endpoint.close()
    client.close()
    assert time.monotonic() - started < 2
    recorded = assert_health(tmp_path, endpoint)
    assert next(r for r in recorded if r["kind"] == "response_finished")["metadata"]["outcome"] == "server_shutdown"
    assert endpoint.process.poll() == 0


def test_forced_shutdown_never_has_a_complete_final_reference(tmp_path):
    endpoint = EndpointProcess(tmp_path).start()
    transact(endpoint.url)
    os.kill(endpoint.process.pid, signal.SIGKILL)
    endpoint.process.wait(timeout=2)
    endpoint.close()
    assert endpoint.health.get("complete") is False


@pytest.mark.parametrize("options,counter", [
    ({"omit_kinds": ["request_received"]}, "omitted_count"),
    ({"max_record_bytes": 800}, "truncated_count"),
    ({"fail_after_records": 1}, "write_errors"),
    ({"max_log_bytes": 1000}, "write_errors"),
])
def test_reference_recording_faults_are_explicit_and_invalidate(tmp_path, options, counter):
    recorder = Recorder(tmp_path / "receiver-events.jsonl", {}, options)
    recorder.emit("request_received", content="x" * 3000)
    recorder.emit("response_finished")
    health = recorder.close()
    assert health[counter] > 0
    assert health["complete"] is False
    if counter == "omitted_count":
        assert rows(tmp_path)[0]["sequence"] == 1


def test_recorder_queue_overflow_is_bounded_and_not_silent(tmp_path):
    recorder = Recorder(tmp_path / "receiver-events.jsonl", {},
                        {"queue_rows": 1, "queue_bytes": 2000, "write_delay_ms": 10})
    for index in range(100):
        recorder.emit("response_event", content=str(index))
    health = recorder.close()
    assert health["overflow_count"] > 0 and health["complete"] is False
    assert health["queue_high_water_rows"] <= 1 and health["queue_high_water_bytes"] <= 2000


def test_recording_byte_bound_is_independent_from_row_count(tmp_path):
    recorder = Recorder(tmp_path / "receiver-events.jsonl", {},
                        {"queue_rows": 100, "queue_bytes": 1})
    recorder.emit("response_event", content="small")
    health = recorder.close()
    assert health["overflow_count"] == 2  # Event and final status both exceed the byte bound.
    assert health["written"] == 0 and not health["complete"]


def test_actual_paced_emission_observations_are_retained(tmp_path):
    with EndpointProcess(tmp_path, scenario={"events": ["one", "two", "three"],
                                            "first_delay_ms": 20, "event_delay_ms": 10}) as endpoint:
        transact(endpoint.url)
    recorded = assert_health(tmp_path, endpoint)
    received = next(r for r in recorded if r["kind"] == "request_received")
    answers = [r for r in recorded if r["kind"] == "response_event" and r["metadata"]["content"]]
    assert (answers[0]["monotonic_ns"] - received["monotonic_ns"]) / 1e6 >= 15
    gaps = [(b["monotonic_ns"] - a["monotonic_ns"]) / 1e6 for a, b in zip(answers, answers[1:])]
    assert len(gaps) == 2 and min(gaps) >= 8
    assert endpoint.ready["network_timing_qualified"] is False
    assert endpoint.health["network_observation_uncertainty_ns"] is None


def test_auto_fixture_tokens_and_event_fragmentation_are_independent():
    payload = {"model": "reference", "stream": True, "max_tokens": 128,
               "messages": [{"role": "user", "content": "Copy exactly:\nThe blue box"}]}
    response = build_response(payload, {"response": "auto", "event_count": 64}, "known-1")
    assert response.expected["content"] == "The blue box"
    assert response.expected["completion_tokens"] == 3
    assert response.expected["input_tokens"] == 13
    assert "".join(e.metadata["content"] for e in response.events) == "The blue box"
    assert len(response.events) == 66


def test_auto_answer_stop_and_output_cap_have_explicit_independent_expectations():
    payload = {"model": "reference", "stream": True, "max_tokens": 128,
               "stop": ["[END]"], "messages": [{"role": "user",
               "content": "Copy exactly: blue [END] green"}]}
    response = build_response(payload, {"response": "auto"}, "known-1")
    assert response.expected["content"] == "blue " and response.expected["score"] is True
    payload.update(stop=[], max_tokens=1)
    response = build_response(payload, {"response": "auto"}, "known-2")
    assert response.expected["content"] == "blue "
    assert response.expected["finish_reason"] == "length" and response.expected["score"] is None


def test_readiness_and_metrics_probes_have_separate_population(tmp_path):
    with EndpointProcess(tmp_path, scenario={"metrics_delay_ms": 20}) as endpoint:
        address = urlparse(endpoint.url)
        client = http.client.HTTPConnection(address.hostname, address.port, timeout=3)
        started = time.monotonic()
        client.request("GET", "/metrics")
        response = client.getresponse()
        assert response.read() == b"giraffe_reference_up 1\n"
        assert time.monotonic() - started >= .015
        client.close()
    recorded = assert_health(tmp_path, endpoint)
    probes = [row for row in recorded if row["kind"] == "probe_received"]
    assert len(probes) == 1 and probes[0]["phase"] == "probe"
    assert not [row for row in recorded if row["kind"] == "request_received"]


def test_wrong_fixture_cannot_be_overridden_by_healthy_events():
    response = build_response({"stream": True}, {"fixture": "incorrect", "response": "42",
                                                "events": ["4", "2"]}, "known-1")
    assert response.expected["content"] == "INCORRECT" and response.expected["score"] is False


@pytest.mark.parametrize("fixture", ["stream_error_before", "stream_error_after", "missing_termination"])
def test_stream_only_faults_reject_nonstream_semantics(fixture):
    with pytest.raises(ValueError, match="requires stream=true"):
        build_response({"stream": False}, {"fixture": fixture}, "known-1")
