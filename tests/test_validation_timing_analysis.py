from __future__ import annotations

import json
import hashlib
import base64

import pytest

from validation_suite.timing_analysis import (analyze_overhead_pairs, first_body_intervals_file,
                                             overhead_criteria, owned_connection_first_byte_file)
from validation_suite.timing_observer import TimingLog, clock_calibration


OBSERVER = "e" * 64
BASELINE = "b" * 40


def case(directory, *, observed, latency=100, count=10):
    directory.mkdir()
    scenario = {"mode": "fixed_rate", "active_seconds": 10, "concurrency": 10,
                "rate_rps": 1, "event_count": 1, "event_delay_ms": 0, "stream": True, "fixture": "normal"}
    manifest = {"scenario": scenario, "experiment_id": "E4", "baseline_unchanged": True,
                "baseline": {"sha": BASELINE}, "phases": {"active_start_ns": 0, "active_end_ns": 10_000_000_000,
                    "clock_domain": "product", "dropped_local": 0, "dropped_late": 0, "not_offered": 0},
                "receiver_active_window": {"start_ns": 0, "end_ns": 10_000_000_000,
                    "clock_domain": "endpoint", "boundary": "first receipt plus requested duration"}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "results.jsonl").write_text("".join(json.dumps({"phase": "measured", "clock_domain": "product",
        "response_id": f"response-{i}", "status": "completed", "valid": True,
        "timing": {"latency_ms": latency, "lifecycle_completed_ns": (i + 1) * 100_000_000}}) + "\n" for i in range(count)))
    (directory / "receiver-events.jsonl").write_text("".join(json.dumps({"kind": "request_received", "sequence": i,
        "clock_domain": "endpoint", "monotonic_ns": (i + 1) * 100_000_000,
        "metadata": {"response_id": f"response-{i}"}}) + "\n" for i in range(count)))
    (directory / "receiver-health.json").write_text(json.dumps({"complete": True, "written": count,
        "flush_complete": True, "shutdown_complete": True, "last_sequence": count - 1,
        "last_checkpoint_sequence": count - 1, "overflow_count": 0, "truncated_count": 0,
        "omitted_count": 0, "write_errors": 0,
        "events_sha256": hashlib.sha256((directory / "receiver-events.jsonl").read_bytes()).hexdigest()}))
    (directory / "resources.csv").write_text('monotonic_ns,role,rss_bytes,cpu_percent\n1,candidate,10000,20\n')
    if observed:
        log = TimingLog(directory / "timing-events.jsonl", directory / "timing-health.json", {})
        log.emit("client_timer_start", 1, 2, record_id="r")
        calibration = clock_calibration(64)
        log.close({"source_unchanged": True, "source_sha256_before": "known", "source_sha256_after": "known",
            "observer_source_sha256": OBSERVER, "clock_calibration_before": calibration,
            "clock_calibration_after": calibration})
    return directory


@pytest.mark.parametrize("inflation,loss,passed", [(2, 0, True), (20, 0, False), (2, 2, False)])
def test_overhead_latency_throughput_and_resource_scopes(tmp_path, inflation, loss, passed):
    pairs = []
    for repetition in [1, 2, 3]:
        control = case(tmp_path / f"c{repetition}", observed=False)
        observed = case(tmp_path / f"o{repetition}", observed=True, latency=100 + inflation, count=10 - loss)
        pairs.append({"repetition": repetition, "control_case_dir": str(control), "observed_case_dir": str(observed)})
    result = analyze_overhead_pairs("E4-r1", pairs, tmp_path / "overhead.json",
                                    observer_source_sha256=OBSERVER, baseline_sha=BASELINE)
    profile = result["profiles"]["E4-r1"]
    assert profile["overhead_gate_pass"] is passed
    assert profile["pairs"][0]["control_delivery_resources"]["candidate_sampled_peak_rss_bytes"] == 10000
    assert profile["paired_median_throughput_loss"]["receipt"] == pytest.approx(loss / 10)
    assert profile["workload"]["concurrency"] == 10 and len(profile["workload_fingerprint"]) == 64


def test_overhead_requires_three_real_pairs_and_exact_geometry(tmp_path):
    control = case(tmp_path / "c", observed=False)
    observed = case(tmp_path / "o", observed=True)
    value = analyze_overhead_pairs("E4-r1", [{"repetition": 1, "control_case_dir": str(control),
        "observed_case_dir": str(observed)}], tmp_path / "overhead.json", observer_source_sha256=OBSERVER, baseline_sha=BASELINE)
    assert not value["profiles"]["E4-r1"]["overhead_gate_pass"]
    assert overhead_criteria()["paired_median_throughput_loss_fraction"] == .05


def test_first_body_yield_is_not_first_socket_byte_or_complete_nonstream_body(tmp_path):
    rows = [
        {"sequence": 0, "kind": "client_timer_start", "lower_ns": 1_000_000, "upper_ns": 1_000_010,
         "metadata": {"record_id": "r"}},
        {"sequence": 1, "kind": "client_body_chunk", "lower_ns": 2_000_000, "upper_ns": 2_000_010,
         "metadata": {"record_id": "r", "byte_count": 10, "boundary": "HTTP body yield"}},
        {"sequence": 2, "kind": "client_timer_end", "lower_ns": 3_000_000, "upper_ns": 3_000_010,
         "metadata": {"record_id": "r"}},
    ]
    events = tmp_path / "events.jsonl"
    events.write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = tmp_path / "body.jsonl"
    summary = first_body_intervals_file(events, output)
    row = json.loads(output.read_text())
    assert summary["complete"] and row["first_http_body_yield_ms"]["lower_ms"] == pytest.approx(.99999)
    assert not row["is_raw_socket_first_byte"] and not row["is_packet_arrival"]


@pytest.mark.parametrize("fault", ["receiver_hash", "receiver_sequence", "completion_timestamp", "completion_domain"])
def test_overhead_rejects_untrusted_receiver_or_missing_completion_coverage(tmp_path, fault):
    pairs = []
    for repetition in [1, 2, 3]:
        control = case(tmp_path / f"c{repetition}", observed=False)
        observed = case(tmp_path / f"o{repetition}", observed=True)
        if fault == "receiver_hash":
            (observed / "receiver-events.jsonl").write_text((observed / "receiver-events.jsonl").read_text() + "\n")
        elif fault == "receiver_sequence":
            path = observed / "receiver-events.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["sequence"] = 100
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            health = json.loads((observed / "receiver-health.json").read_text())
            health["events_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            (observed / "receiver-health.json").write_text(json.dumps(health))
        else:
            path = observed / "results.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            if fault == "completion_timestamp":
                rows[0]["timing"].pop("lifecycle_completed_ns")
            else:
                rows[0]["clock_domain"] = "other"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        pairs.append({"repetition": repetition, "control_case_dir": str(control), "observed_case_dir": str(observed)})
    result = analyze_overhead_pairs("E4-r1", pairs, tmp_path / "overhead.json",
                                    observer_source_sha256=OBSERVER, baseline_sha=BASELINE)
    assert not result["profiles"]["E4-r1"]["overhead_gate_pass"]
    hashes = result["input_sha256"][0]
    assert str(observed.parent / "o1" / "receiver-events.jsonl") in hashes
    assert str(observed.parent / "o1" / "resources.csv") in hashes


def socket_rows(payloads, *, fd=7, local_port=1234, timer=True):
    rows = [{"kind": "client_timer_start", "lower_ns": 1000, "upper_ns": 1010,
             "clock_domain": "local", "metadata": {"record_id": "r"}}] if timer else []
    for i, payload in enumerate(payloads):
        lower = 2000 + 100 * i
        rows.append({"kind": "client_socket_recv", "lower_ns": lower, "upper_ns": lower + 20,
            "clock_domain": "local", "metadata": {"socket_fd": fd, "local": ["127.0.0.1", local_port],
                "peer": ["127.0.0.1", 5000], "native_return_upper_ns": lower + 10,
                "byte_count": len(payload), "wire_b64": base64.b64encode(payload).decode(),
                "wire_sha256": hashlib.sha256(payload).hexdigest(), "wire_capture_omitted": False}})
    return rows


def socket_analysis(tmp_path, rows, *, max_connection_bytes=8 * 1024**2):
    events, results, output = (tmp_path / name for name in ("events.jsonl", "results.jsonl", "socket.jsonl"))
    for i, row in enumerate(rows):
        row["sequence"] = i
    events.write_text("".join(json.dumps(row) + "\n" for row in rows))
    results.write_text(json.dumps({"raw": {"id": "r"}, "response_id": "response-1", "phase": "measured"}) + "\n")
    summary = owned_connection_first_byte_file(events, output, results=results,
        max_connection_bytes=max_connection_bytes, min_free_bytes=0)
    return summary, json.loads(output.read_text())


SSE = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n" +
       'data: {"id":"response-1","choices":[{"delta":{"reasoning":"hmm"}}]}\r\n\r\n'.encode() +
       'data: {"id":"response-1","choices":[{"delta":{"content":"café 🦒"}}]}\r\n\r\n'.encode() + b"data: [DONE]\r\n\r\n")


@pytest.mark.parametrize("stream", [True, False])
def test_socket_first_byte_exact_export_join_with_split_headers_utf8_and_coalescing(tmp_path, stream):
    body = b'{"id":"response-1","choices":[]}'
    wire = SSE if stream else b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    # Split a header and the multibyte content independently of socket boundaries.
    summary, row = socket_analysis(tmp_path, socket_rows([wire[:8], wire[8:160], wire[160:]]))
    assert summary["complete"] and summary["precise_requests"] == 1
    assert row["first_application_socket_byte_ms"]["lower_ms"] == pytest.approx(.00099)
    assert row["first_application_socket_byte_ms"]["upper_ms"] == pytest.approx(.00101)
    assert not row["is_packet_arrival"]


@pytest.mark.parametrize("fault", ["omitted", "hash", "truncated", "duplicate", "fd_reuse", "eof_reuse", "nonclose", "header_only", "bound", "clock", "timer"])
def test_socket_first_byte_fails_closed_on_incomplete_or_ambiguous_reference(tmp_path, fault):
    wire = SSE.replace(b"Connection: close", b"Connection: alive") if fault == "nonclose" else SSE
    if fault == "truncated":
        wire = wire[:-10]
    if fault == "header_only":
        wire = wire.partition(b"\r\n\r\n")[0] + b"\r\n\r\n"
    rows = socket_rows([wire], timer=fault != "timer")
    native = rows[-1]
    if fault == "omitted":
        native["metadata"]["wire_capture_omitted"] = True
    elif fault == "hash":
        native["metadata"]["wire_sha256"] = "wrong"
    elif fault == "duplicate":
        second = socket_rows([wire], local_port=1235, timer=False)[0]
        rows.append(second)
    elif fault == "fd_reuse":
        second = socket_rows([wire], timer=False)[0]
        second["lower_ns"], second["upper_ns"] = 3000, 3020
        second["metadata"]["native_return_upper_ns"] = 3010
        rows.append(second)
    elif fault == "eof_reuse":
        rows = socket_rows([b"", wire])
    elif fault == "clock":
        native["clock_domain"] = "different"
    summary, row = socket_analysis(tmp_path, rows, max_connection_bytes=10 if fault == "bound" else 10000)
    assert not summary["complete"] or fault == "clock"
    assert row["first_application_socket_byte_ms"] is None
    assert row["issues"]
