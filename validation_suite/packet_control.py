"""Short independent delayed-read control for the packet/clock comparison."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import time

from .packet_analysis import capture_mapping, decode_capture, difference, map_arrival
from .packet_capture import CaptureSession


def delayed_read_control(directory: Path) -> dict:
    """Send known SSE bytes, signal after sendall, then defer read by 0/50 ms.

    The delay is injected in a standalone client, never in Giraffe. The measured
    sleep and packet/read difference are retained; configured sleep is not truth.
    """
    directory.mkdir(parents=True, exist_ok=False)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(5)
    sent = threading.Event()
    acknowledged = threading.Event()
    errors, observations = [], []
    delays = [0, .05, 0, .05, 0, .05]

    def serve():
        try:
            for number in range(len(delays)):
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    request = b''
                    while b'\r\n\r\n' not in request:
                        part = conn.recv(4096)
                        if not part or len(request) > 16384:
                            raise ValueError('Invalid control request')
                        request += part
                    data = json.dumps({'id': f'control-{number}', 'choices': [{'delta': {'content': '42'}}]})
                    wire = ('HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/event-stream\r\n\r\n'
                            f'data: {data}\n\ndata: [DONE]\n\n').encode()
                    conn.sendall(wire)
                    sent.set()
                if not acknowledged.wait(5):
                    raise TimeoutError('Control reader did not finish')
                acknowledged.clear()
        except Exception as error:
            errors.append(str(error))

    capture = CaptureSession(directory, listener.getsockname()[1]).start()
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        for number, delay in enumerate(delays):
            with socket.create_connection(listener.getsockname(), timeout=5) as client:
                client.sendall(b'POST /control HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n')
                if not sent.wait(5):
                    raise TimeoutError('Control server did not send')
                sent.clear()
                before_sleep = time.monotonic_ns()
                time.sleep(delay)
                after_sleep = time.monotonic_ns()
                wire, spans = bytearray(), []
                while True:
                    before = time.monotonic_ns()
                    data = client.recv(65536)
                    after = time.monotonic_ns()
                    if not data:
                        break
                    wire.extend(data)
                    spans.append((len(wire), (before, after)))
                observations.append(dict(response_id=f'control-{number}', delay_seconds=delay,
                    sleep_measured_ns=after_sleep-before_sleep, spans=spans, wire_hex=wire.hex()))
            acknowledged.set()
        worker.join(5)
        if worker.is_alive() or errors:
            raise RuntimeError(f'Control server incomplete: {errors}')
    finally:
        listener.close()
        health = capture.finish()
        (directory / 'control-observations.json').write_text(json.dumps(observations, indent=2)+'\n')
    decoded = decode_capture(directory)
    mapping = capture_mapping(directory, health)
    rows = []
    for observation in observations:
        packet = decoded['responses'][observation['response_id']]
        if packet['wire'].hex() != observation['wire_hex']:
            raise ValueError('Control bytes did not match packet capture')
        end = packet['events'][0]['end']
        arrival = map_arrival(packet['times'][end-1], mapping)
        read = next(interval for length, interval in observation['spans'] if length >= end)
        delta = difference(read, arrival)
        # Deliberately broad, predeclared control bounds; not an accuracy budget.
        passed = (40 <= delta[0] <= delta[1] <= 150) if observation['delay_seconds'] else (-.1 <= delta[0] <= delta[1] <= 20)
        rows.append(dict(response_id=observation['response_id'], injected_seconds=observation['delay_seconds'],
                         measured_sleep_ms=observation['sleep_measured_ns']/1e6,
                         arrival_to_read_ms=delta, passed=passed))
    result = dict(rows=rows, issues=decoded['issues'],
                  passed=health['recording_gate_pass'] and health['clock_mapping']['gate_pass'] and
                  not decoded['issues'] and len(rows) == 6 and all(r['passed'] for r in rows))
    (directory / 'control-result.json').write_text(json.dumps(result, indent=2)+'\n')
    return result
