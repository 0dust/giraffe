"""Bounded macOS loopback capture. Captures only an owned endpoint TCP port.

Clock bounds are an empirical envelope of bracketed realtime/monotonic reads,
not a hardware timestamp accuracy guarantee. No parsing occurs during traffic.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

WIRESHARK = Path('/Applications/Wireshark.app/Contents/MacOS')


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def clock_envelope(samples: list[dict]) -> dict:
    if len(samples) < 3:
        raise ValueError('Insufficient clock observations')
    for sample in samples:
        if sample['after'] < sample['before']:
            raise ValueError('Reversed monotonic bracket')
    lower = min(s['before'] - s['wall'] for s in samples)
    upper = max(s['after'] - s['wall'] for s in samples)
    gaps = [b['before'] - a['after'] for a, b in zip(samples, samples[1:])]
    local_widths = [max(a['after']-a['wall'], b['after']-b['wall']) -
                    min(a['before']-a['wall'], b['before']-b['wall'])
                    for a, b in zip(samples, samples[1:])]
    ordered = all(b['wall'] > a['wall'] and b['before'] >= a['after']
                  for a, b in zip(samples, samples[1:]))
    return {'lower_ns': lower, 'upper_ns': upper, 'width_ns': upper - lower,
            'max_sample_gap_ns': max(gaps), 'ordered': ordered,
            'first_wall_ns': samples[0]['wall'], 'last_wall_ns': samples[-1]['wall'],
            'max_local_width_ns': max(local_widths),
            'mapping_method': 'envelope of adjacent samples bracketing each packet',
            'gate_pass': ordered and max(local_widths) <= 100_000 and max(gaps) <= 100_000_000,
            'scope': 'Adjacent sampled host-clock envelope accommodates clock slew; unobserved clock excursions and capture timestamp bias remain unbounded.'}


class CaptureSession:
    def __init__(self, directory: Path, port: int, *, maximum_seconds: int = 60):
        if not 1 <= port <= 65535 or not 1 <= maximum_seconds <= 420:
            raise ValueError('Invalid owned port or bounded capture duration')
        self.directory, self.port = directory, port
        self.capture = directory / 'packets.pcapng'
        if self.capture.exists():
            raise ValueError('Capture output already exists')
        self.command = [str(WIRESHARK / 'dumpcap'), '-q', '-p', '-i', 'lo0', '-f',
                        f'tcp port {port}', '-s', '0', '-B', '8', '-a',
                        f'duration:{maximum_seconds}', '-a', 'filesize:1000000',
                        '-w', str(self.capture.resolve())]
        self.samples, self.logs = [], []
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.process = None
        self.finished = None

    def _sample(self):
        while True:
            before = time.monotonic_ns()
            wall = time.time_ns()
            after = time.monotonic_ns()
            self.samples.append({'before': before, 'wall': wall, 'after': after})
            if self.stop.wait(.01):
                return

    def _read(self):
        for line in self.process.stderr:
            self.logs.append(line)
            if 'File:' in line:
                self.ready.set()

    def start(self):
        self.clock_thread = threading.Thread(target=self._sample, daemon=True)
        self.clock_thread.start()
        try:
            self.process = subprocess.Popen(self.command, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, text=True)
            self.log_thread = threading.Thread(target=self._read, daemon=True)
            self.log_thread.start()
            if not self.ready.wait(5) or self.process.poll() is not None:
                raise RuntimeError('Capture startup failed: ' + ''.join(self.logs))
        except BaseException:
            self.finish()
            raise
        return self

    def finish(self) -> dict:
        if self.finished is not None:
            return self.finished
        early_exit = self.process is None or self.process.poll() is not None
        if self.process is not None:
            if self.process.poll() is None:
                # Let the macOS BPF read timeout deliver its final partial buffer.
                # SIGINT immediately after a tiny control can otherwise save zero
                # packets with a misleading zero-drop count.
                time.sleep(1.1)
                if self.process.poll() is None:
                    self.process.send_signal(signal.SIGINT)
                else:
                    early_exit = True
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                early_exit = True
            if hasattr(self, 'log_thread'):
                self.log_thread.join(timeout=2)
        self.stop.set()
        self.clock_thread.join(timeout=2)
        log = ''.join(self.logs)
        (self.directory / 'capture.log').write_text(log)
        (self.directory / 'capture-clocks.json').write_text(json.dumps(self.samples) + '\n')
        counts = re.findall(r'Packets received/dropped on interface .*?: (\d+)/(\d+)', log)
        captured = re.findall(r'Packets captured: (\d+)', log)
        clock = clock_envelope(self.samples) if len(self.samples) >= 3 else {'gate_pass': False}
        clean = bool(len(counts) == 1 and len(captured) == 1 and
                     int(counts[0][1]) == 0 and int(captured[0]) > 0 and
                     int(captured[0]) == int(counts[0][0]))
        self.finished = {'command': self.command, 'pid': self.process.pid if self.process else None,
                         'port': self.port, 'interface': 'lo0', 'early_exit': early_exit,
                         'returncode': self.process.returncode if self.process else None,
                         'received_dropped': counts, 'captured': captured,
                         'recording_gate_pass': clean and not early_exit and self.process.returncode == 0,
                         'clock_mapping': clock, 'capture_sha256': file_hash(self.capture) if self.capture.exists() else None,
                         'clock_samples_sha256': file_hash(self.directory / 'capture-clocks.json'),
                         'timestamp_scope': 'Host software loopback capture point; not physical NIC arrival',
                         'timestamp_quantization_ns': 1000}
        (self.directory / 'capture-health.json').write_text(json.dumps(self.finished, indent=2) + '\n')
        return self.finished
