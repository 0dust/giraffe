"""Exact owned HTTP/SSE byte correlation with independent packet timestamps.

Only bounded plaintext HTTP/1.1 connection-close fixtures are supported. TLS,
HTTP/2, truncated streams and ambiguous identities fail closed. Analysis is offline.
"""
from __future__ import annotations

from array import array
import base64
from bisect import bisect_left
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

from .packet_index import PacketIndex
from .packet_capture import WIRESHARK, clock_envelope, file_hash
from .timing_analysis import _owned_response_id
from .timing_observer import validate_timing_health

MAX_WIRE = 1024 * 1024
MAX_STREAMS = 100000
MAX_PACKETS = 2000000


def packet_flag(value: str) -> bool:
    if value.lower() not in {'true', 'false', '1', '0'}:
        raise ValueError('Unknown tshark flag representation')
    return value.lower() in {'true', '1'}


def reconstruct(packets: list[dict]) -> tuple[bytes, array]:
    """Reassemble from SYN sequence; earliest observation per byte, then prefix max.

    Prefix max includes any preceding TCP gap: the final byte alone is not proof
    the complete semantic event was available. Conflicting retransmissions fail.
    """
    bases = {(p['seq'] + 1) % 2**32 for p in packets if p['syn']}
    if len(bases) != 1 or any(p['rst'] for p in packets):
        raise ValueError('Missing/ambiguous SYN or reset stream')
    base = next(iter(bases))
    fins = {(p['seq'] - base + len(p['data'])) % 2**32 for p in packets if p['fin']}
    if len(fins) != 1:
        raise ValueError('Missing/ambiguous FIN')
    size = next(iter(fins))
    if not 0 < size <= MAX_WIRE:
        raise ValueError('Invalid bounded wire size')
    wire = bytearray(size)
    times = array('q', [-1]) * size
    for packet in packets:
        data = packet['data']
        if not data:
            continue
        offset = (packet['seq'] + int(packet['syn']) - base) % 2**32
        if offset + len(data) > size:
            raise ValueError('Payload beyond final sequence')
        for index, byte in enumerate(data, offset):
            if times[index] >= 0 and wire[index] != byte:
                raise ValueError('Conflicting TCP overlap')
            wire[index] = byte
            times[index] = packet['ns'] if times[index] < 0 else min(times[index], packet['ns'])
    if -1 in times:
        raise ValueError('TCP sequence gap')
    latest = 0
    for index, stamp in enumerate(times):
        latest = max(latest, stamp)
        times[index] = latest
    return bytes(wire), times


def semantic_events(wire: bytes) -> tuple[str, list[dict]]:
    response_id = _owned_response_id(wire)
    header, _, body = wire.partition(b'\r\n\r\n')
    start = len(header) + 4
    events = []
    if b'text/event-stream' not in header.lower():
        raise ValueError('This semantic adapter requires streaming fixtures')
    position = 0
    for boundary in re.finditer(rb'\r?\n\r?\n', body):
        block = body[position:boundary.start()]
        data = b'\n'.join(line[5:].removeprefix(b' ') for line in block.splitlines() if line.startswith(b'data:'))
        if data and data != b'[DONE]':
            item = json.loads(data)
            for choice in item.get('choices', []):
                delta = choice.get('delta', {})
                for field, key in (('content', 'content'), ('reasoning', 'reasoning_content')):
                    content = delta.get(key)
                    if content:
                        events.append({'field': field, 'content': content, 'begin': start + position,
                                       'end': start + boundary.end(),
                                       'event_sha256': hashlib.sha256(body[position:boundary.end()]).hexdigest()})
        position = boundary.end()
    return response_id, events


def decode_payload(payload: str, declared_length: str) -> bytes:
    data = bytes.fromhex(payload.replace(':', ''))
    if len(data) != int(declared_length):
        raise ValueError('TCP payload extraction length mismatch')
    return data


def capture_mapping(directory: Path, health: dict) -> dict:
    mapping = dict(health['clock_mapping'])
    path = directory / 'capture-clocks.json'
    if 'clock_samples_sha256' in health:
        if file_hash(path) != health['clock_samples_sha256']:
            raise ValueError('Capture clock samples hash mismatch')
        samples = json.loads(path.read_text())
        mapping = clock_envelope(samples)
        mapping['samples'] = samples
        mapping['wall_times'] = [s['wall'] for s in samples]
    return mapping


def decode_capture(directory: Path) -> dict:
    health = json.loads((directory / 'capture-health.json').read_text())
    port = health['port']
    capture = directory / 'packets.pcapng'
    if capture.stat().st_size > 1_000_000_000 or file_hash(capture) != health['capture_sha256']:
        raise ValueError('Capture size/hash invalid')
    output = directory / 'packet-fields.tsv'
    fields = ['tcp.stream', 'tcp.seq_raw', 'tcp.flags.syn', 'tcp.flags.fin', 'tcp.flags.reset',
              'tcp.srcport', 'tcp.dstport', 'tcp.payload', 'frame.time_epoch', 'frame.cap_len', 'frame.len', 'tcp.len']
    # HTTP reassembly can consume/hide tcp.payload on data-bearing FINs.
    # Extract raw TCP segments and independently enforce the declared length.
    command = [str(WIRESHARK / 'tshark'), '-n', '-o', 'tcp.desegment_tcp_streams:FALSE',
               '--disable-protocol', 'http', '-r', str(capture), '-Y', 'tcp', '-T', 'fields']
    for field in fields:
        command += ['-e', field]
    with output.open('x') as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, check=True, timeout=300)
    streams = PacketIndex(directory / 'packet-stream-index.sqlite')
    client_prefix = {}
    with output.open() as stream:
        for number, line in enumerate(stream, 1):
            if number > MAX_PACKETS or len(line) > MAX_WIRE * 3:
                raise ValueError('Decoded packet bound exceeded')
            parts = line.rstrip('\n').split('\t')
            if len(parts) != len(fields) or parts[9] != parts[10]:
                raise ValueError('Truncated/invalid packet')
            sid, seq, syn, fin, rst, source, destination, payload, stamp, _, _, length = parts
            source, destination = int(source), int(destination)
            if port not in (source, destination):
                raise ValueError('Capture escaped owned port')
            data = decode_payload(payload, length)
            if source != port:
                if data and sid not in client_prefix:
                    client_prefix[sid] = data
                continue
            streams.append(sid, {'seq': int(seq), 'syn': packet_flag(syn), 'fin': packet_flag(fin),
                                 'rst': packet_flag(rst), 'data': data,
                                 'ns': int(Decimal(stamp) * 1_000_000_000)})
            if len(streams) > MAX_STREAMS:
                raise ValueError('Connection bound exceeded')
    matched = PacketIndex(directory / 'packet-response-index.sqlite')
    issues, auxiliary = [], 0
    for sid, packets in streams.items():
        # Owned probes and model discovery are not measured completion requests.
        if client_prefix.get(sid, b'').startswith(b'GET '):
            auxiliary += 1
            continue
        try:
            wire, times = reconstruct(packets)
            identity, events = semantic_events(wire)
            if identity in matched:
                raise ValueError('Duplicate response identity')
            matched[identity] = {'wire': wire, 'times': times, 'events': events, 'stream': sid}
        except (ValueError, KeyError, UnicodeError) as error:
            issues.append({'stream': sid, 'reason': str(error)})
    streams.close()
    return {'responses': matched, 'issues': issues, 'auxiliary_connections': auxiliary}


def map_arrival(stamp: int, mapping: dict) -> tuple[int, int]:
    if not mapping['first_wall_ns'] <= stamp <= mapping['last_wall_ns']:
        raise ValueError('Packet outside clock observation window')
    lower, upper = mapping['lower_ns'], mapping['upper_ns']
    if 'samples' in mapping:
        index = max(1, min(len(mapping['samples'])-1, bisect_left(mapping['wall_times'], stamp)))
        neighbors = mapping['samples'][index-1:index+1]
        lower = min(s['before']-s['wall'] for s in neighbors)
        upper = max(s['after']-s['wall'] for s in neighbors)
    return stamp + lower - 1000, stamp + upper + 1000


def difference(after: tuple, before: tuple) -> list[float]:
    return [(after[0] - before[1]) / 1e6, (after[1] - before[0]) / 1e6]


def distribution(values: list[float]) -> dict:
    if not values:
        return {'n': 0, 'unit': 'ms', 'state': 'unknown'}
    values = sorted(values)
    def pick(q):
        return values[max(0, math.ceil(q * len(values)) - 1)]
    return {'n': len(values), 'unit': 'ms', 'min': values[0], 'median': pick(.5),
            'p95': pick(.95), 'p99': pick(.99), 'max': values[-1],
            'percentile_method': 'nearest rank', 'tails_supported': len(values) >= 100}


def precision_by_metric(record: dict) -> dict:
    """Each difference must meet 0.1ms; a wide R bracket does not widen G-A."""
    return {key: value is not None and value[1]-value[0] <= .1
            for key, value in record.items() if key.endswith('_ms') and isinstance(value, (list, type(None)))}


def analyze_case(directory: Path) -> dict:
    """Preserve observed delays even when independent qualification gates fail."""
    output = directory / 'packet-timing.json'
    if output.exists():
        raise ValueError('Packet timing analysis already exists')
    decoded = decode_capture(directory)
    capture_health = json.loads((directory / 'capture-health.json').read_text())
    mapping = capture_mapping(directory, capture_health)
    timing_health = json.loads((directory / 'timing-health.json').read_text())
    observer_health = validate_timing_health(directory)
    calibrations = [timing_health['clock_calibration_before'], timing_health['clock_calibration_after']]
    offset_lo = min(c['offset_lower_ns'] for c in calibrations)
    offset_hi = max(c['offset_upper_ns'] for c in calibrations)
    def clock_value(value):
        clock = float.fromhex(value)
        rounding = math.ceil(math.ulp(clock) * 1e9) + 1
        stamp = int(clock * 1e9)
        return stamp + offset_lo - rounding, stamp + offset_hi + rounding
    starts, ends, semantics, sockets = [PacketIndex(directory / f'packet-{name}-index.sqlite')
                                      for name in ('starts', 'ends', 'semantics', 'sockets')]
    with (directory / 'timing-events.jsonl').open() as stream:
        for line in stream:
            if len(line) > 2 * MAX_WIRE:
                raise ValueError('Observer line bound exceeded')
            row = json.loads(line)
            meta = row['metadata']
            kind, identity = row['kind'], meta.get('record_id')
            if kind == 'client_timer_start':
                if identity in starts:
                    raise ValueError('Duplicate timer start')
                starts[identity] = row
            elif kind == 'client_timer_end':
                if identity in ends:
                    raise ValueError('Duplicate timer end')
                ends[identity] = row
            elif kind == 'client_semantic_ready':
                semantics.append(identity, row)
            elif kind == 'client_socket_recv':
                key = json.dumps([meta.get('socket_fd'), meta.get('local'), meta.get('peer')])
                sockets.append(key, row)
            if len(starts) > MAX_STREAMS or len(sockets) > MAX_STREAMS:
                raise ValueError('Observer state bound exceeded')
    reads = PacketIndex(directory / 'packet-reads-index.sqlite')
    for rows in sockets.values():
        wire, spans = bytearray(), []
        for row in rows:
            meta = row['metadata']
            if meta.get('wire_capture_omitted') or meta.get('wire_b64') is None:
                break
            data = base64.b64decode(meta['wire_b64'], validate=True)
            if len(data) != meta['byte_count'] or hashlib.sha256(data).hexdigest() != meta['wire_sha256']:
                raise ValueError('Socket observation bytes/hash mismatch')
            if len(wire) + len(data) > MAX_WIRE:
                raise ValueError('Socket wire bound exceeded')
            wire.extend(data)
            if data:
                lo, hi = row['lower_ns'], meta['native_return_upper_ns']
                if lo > hi:
                    raise ValueError('Invalid socket return interval')
                spans.append((len(wire), (lo, hi)))
            # A file descriptor and port tuple can recur in a five-minute run.
            # Split only at the complete owned response, never join two sessions.
            try:
                response = _owned_response_id(bytes(wire))
            except ValueError:
                continue
            if response in reads:
                raise ValueError('Duplicate socket response identity')
            reads[response] = (bytes(wire), spans)
            wire, spans = bytearray(), []
    records, issues = [], list(decoded['issues'])
    planned = json.loads((directory / 'manifest.json').read_text())['scenario']['request_count']
    def measured_results():
        with (directory / 'results.jsonl').open() as stream:
            for line in stream:
                row = json.loads(line)
                if row['phase'] == 'measured':
                    yield row
    exported_count = 0
    for result in measured_results():
        exported_count += 1
        response, identity = result['response_id'], result['raw']['id']
        try:
            packet = decoded['responses'][response]
            events = packet['events']
            observed = semantics[identity]
            if [(e['field'], e['content']) for e in events] != [
                    (r['metadata']['field'], r['metadata']['content']) for r in observed]:
                raise ValueError('Semantic content/order mismatch')
            wire, spans = reads[response]
            if wire != packet['wire']:
                raise ValueError('Packet/socket complete response bytes differ')
            first = next(i for i, event in enumerate(events) if event['field'] == 'content')
            event, semantic = events[first], observed[first]
            if len(wire) < event['end']:
                raise ValueError('Socket evidence does not contain complete first answer event')
            a = map_arrival(packet['times'][event['end'] - 1], mapping)
            r = next(interval for end, interval in spans if end >= event['end'])
            g = clock_value(semantic['metadata']['read_clock_hex'])
            start = starts[identity]
            s = (start['lower_ns'], start['upper_ns'])
            completion = clock_value(ends[identity]['metadata']['clock_hex'])
            complete_a = map_arrival(packet['times'][-1], mapping)
            done_present = b'data: [DONE]\n\n' in wire.replace(b'\r\n', b'\n')
            reported = result['timing']['first_answer_ms']
            recomputed = difference(g, s)
            arithmetic = [reported - recomputed[1], reported - recomputed[0]]
            if r[1] < a[0] or g[1] < a[0]:
                raise ValueError('Causal order violates clock/reference bounds')
            # Bound the differences we report, not only each individual anchor.
            widths = [hi - lo for lo, hi in (a, r, g, s)]
            precision = sum(sorted(widths, reverse=True)[:2]) <= 100_000
            record = {'response_id': response, 'record_id': identity, 'tcp_stream': packet['stream'],
                            'wire_sha256': hashlib.sha256(wire).hexdigest(),
                            'captured_wire_sha256': hashlib.sha256(packet['wire']).hexdigest(),
                            'unread_captured_tail_bytes': len(packet['wire'])-len(wire), 'first_answer_event': event,
                            'anchors_monotonic_ns': {'A': a, 'R': r, 'G': g, 'S': s},
                            'arrival_to_read_ms': difference(r, a), 'read_to_giraffe_ms': difference(g, r),
                            'arrival_to_giraffe_ms': difference(g, a),
                            'done_marker_captured': done_present,
                            'completion_after_body_available_ms': difference(completion, complete_a) if done_present and wire == packet['wire'] else None,
                            'reported_minus_arrival_latency_ms': [reported - x for x in reversed(difference(a, s))],
                            'arithmetic_error_ms': arithmetic, 'all_boundary_precision_pass': precision}
            record['metric_precision'] = precision_by_metric(record)
            record['precision_pass'] = (record['metric_precision']['arrival_to_giraffe_ms'] and
                                        record['metric_precision']['reported_minus_arrival_latency_ms'])
            records.append(record)
        except (KeyError, ValueError, StopIteration) as error:
            issues.append({'response_id': response, 'reason': str(error)})
    precise = [r for r in records if r['precision_pass']]
    summary = {'planned': planned, 'exported_measured': exported_count, 'matched': len(records),
               'precise': len(precise), 'planned_precision_coverage': len(precise) / planned if planned else 0,
               'exported_precision_coverage': len(precise) / exported_count if exported_count else 0,
               'arithmetic_intervals_containing_zero': sum(r['arithmetic_error_ms'][0] <= 0 <= r['arithmetic_error_ms'][1] for r in records),
               'without_done_marker': sum(not r['done_marker_captured'] for r in records),
               'with_unread_captured_tail': sum(r['unread_captured_tail_bytes'] > 0 for r in records),
               'capture_recording_pass': capture_health['recording_gate_pass'],
               'clock_mapping_pass': (mapping.get('ordered', mapping['gate_pass']) and
                                      mapping.get('max_sample_gap_ns', 0) <= 100_000_000),
               'clock_mapping_precision_policy': 'Continuity checked globally; 0.1ms uncertainty checked per metric and response with 99% coverage required.',
               'observer_health_pass': observer_health['reference_valid_for_observed_run'],
               'issues': issues, 'auxiliary_connections': decoded['auxiliary_connections'],
               'scope': 'Observed host loopback capture-point to client timing. No sole-code attribution, physical NIC timestamp claim, or unobserved-run transfer.',
               'overhead_qualified': False, 'actual_giraffe_run_qualified': False,
               'qualification_pending': 'Positive clock/delay controls and three matched overhead pairs required.',
               'distributions': {}, 'precise_distributions': {}, 'metric_precision_coverage': {}}
    for field in ('arrival_to_read_ms', 'read_to_giraffe_ms', 'arrival_to_giraffe_ms',
                  'completion_after_body_available_ms', 'reported_minus_arrival_latency_ms'):
        summary['distributions'][field] = {
            'lower_bound': distribution([r[field][0] for r in records if r[field] is not None]),
            'upper_bound': distribution([r[field][1] for r in records if r[field] is not None])}
        summary['precise_distributions'][field] = {
            'lower_bound': distribution([r[field][0] for r in records if r['metric_precision'][field]]),
            'upper_bound': distribution([r[field][1] for r in records if r['metric_precision'][field]])}
        summary['metric_precision_coverage'][field] = sum(r['metric_precision'][field] for r in records) / exported_count if exported_count else 0
    with (directory / 'packet-timing-records.jsonl').open('x') as stream:
        for row in records:
            stream.write(json.dumps(row) + '\n')
    for index in (starts, ends, semantics, sockets, reads, decoded['responses']):
        if isinstance(index, PacketIndex):
            index.close()
    output.write_text(json.dumps(summary, indent=2) + '\n')
    return summary
