"""Independent hand-checkable TCP and timing vectors; no Wireshark required."""
import json
import base64
import hashlib
from array import array
import socket
import struct

import pytest

from validation_suite.packet_analysis import (difference, distribution, map_arrival,
                                              packet_flag, reconstruct, semantic_events)
from validation_suite.packet_capture import clock_envelope


def packet(seq, data=b'', ns=100, **flags):
    return dict(seq=seq, data=data, ns=ns, syn=False, fin=False, rst=False) | flags


def test_reassembly_out_of_order_waits_for_preceding_gap():
    wire, times = reconstruct([packet(10, syn=True), packet(13, b'cd', ns=200),
                               packet(11, b'ab', ns=300), packet(15, fin=True)])
    assert wire == b'abcd'
    assert list(times) == [300, 300, 300, 300]


def test_retransmission_uses_first_arrival_and_sequence_wrap():
    wire, times = reconstruct([packet(2**32-2, syn=True),
        packet(2**32-1, b'ab', ns=100), packet(2**32-1, b'ab', ns=900),
        packet(1, b'c', ns=200), packet(2, fin=True)])
    assert wire == b'abc' and list(times) == [100, 100, 200]


@pytest.mark.parametrize('packets', [
    [packet(11, b'a'), packet(12, fin=True)],
    [packet(10, syn=True), packet(11, b'a')],
    [packet(10, syn=True), packet(12, b'b'), packet(13, fin=True)],
    [packet(10, syn=True), packet(11, b'a'), packet(11, b'b'), packet(12, fin=True)],
    [packet(10, syn=True), packet(11, b'a'), packet(12, fin=True, rst=True)],
])
def test_incomplete_or_ambiguous_stream_rejected(packets):
    with pytest.raises(ValueError):
        reconstruct(packets)


def sse_wire():
    header = b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n'
    data = json.dumps({'id': 'known', 'choices': [{'delta': {'content': '42'}}]}).encode()
    event = b'data: ' + data + b'\n\n'
    return header, event, header + event + b'data: [DONE]\n\n'


def test_semantic_end_requires_complete_sse_delimiter():
    header, event, wire = sse_wire()
    identity, events = semantic_events(wire)
    assert identity == 'known'
    assert [(e['field'], e['content'], e['begin'], e['end']) for e in events] == [
        ('content', '42', len(header), len(header) + len(event))]


def test_missing_termination_or_partial_event_rejected():
    header, event, _ = sse_wire()
    with pytest.raises(ValueError):
        semantic_events(header+event)
    with pytest.raises(ValueError):
        semantic_events(header + event[:-2])


def test_known_delay_and_clock_uncertainty():
    mapping = dict(lower_ns=-1000, upper_ns=-900, first_wall_ns=0, last_wall_ns=10**9)
    arrival = map_arrival(1_001_000, mapping)
    assert arrival == (999000, 1001100)
    assert difference((51_000_000, 51_000_100), arrival) == pytest.approx([49.9989, 50.0011])
    with pytest.raises(ValueError):
        map_arrival(10**9+1, mapping)


def test_clock_step_and_sampling_gap_fail_gate():
    samples = [dict(before=n*10_000_000, wall=n*10_000_000+1000, after=n*10_000_000+100)
               for n in range(3)]
    assert clock_envelope(samples)['gate_pass']
    samples[-1]['wall'] += 1_000_000
    assert not clock_envelope(samples)['gate_pass']
    samples[-1] = dict(before=200_000_000, wall=200_001_000, after=200_000_100)
    assert not clock_envelope(samples)['gate_pass']


def test_clock_slew_uses_neighboring_samples_not_whole_run_offset():
    samples = [dict(before=n*10_000_000, wall=n*10_000_000+1000+n*500,
                    after=n*10_000_000+100) for n in range(1000)]
    mapping = clock_envelope(samples)
    assert mapping['width_ns'] > 100_000 and mapping['gate_pass']
    mapping.update(samples=samples, wall_times=[s['wall'] for s in samples])
    lo, hi = map_arrival(samples[500]['wall'], mapping)
    assert lo <= samples[500]['before'] <= hi
    assert hi-lo < 3000


def test_finish_without_done_does_not_prove_complete_wire():
    header, event, _ = sse_wire()
    finish = b'data: {"id":"known","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    with pytest.raises(ValueError):
        semantic_events(header+event+finish)


def test_percentiles_and_small_population_are_explicit():
    assert distribution([])['state'] == 'unknown'
    result = distribution(list(range(1, 101)))
    assert (result['median'], result['p95'], result['p99']) == (50, 95, 99)
    assert not distribution([1, 2])['tails_supported']


def test_tshark_boolean_formats():
    assert packet_flag('True') and packet_flag('1')
    assert not packet_flag('False') and not packet_flag('0')
    with pytest.raises(ValueError):
        packet_flag('')


@pytest.mark.parametrize('fault', [None, 'content', 'packet_bytes', 'clock', 'arithmetic', 'reused_socket'])
def test_end_to_end_known_7ms_inflation_and_corruption(tmp_path, monkeypatch, fault):
    import validation_suite.packet_analysis as module
    _, _, wire = sse_wire()
    _, events = semantic_events(wire)
    captured = wire.replace(b'42', b'43') if fault == 'packet_bytes' else wire
    monkeypatch.setattr(module, 'decode_capture', lambda _: {
        'responses': {'known': {'wire': captured, 'events': events, 'stream': '0',
                               'times': array('q', [1_010_000_000])*len(captured)}},
        'issues': [], 'auxiliary_connections': 0})
    monkeypatch.setattr(module, 'validate_timing_health', lambda _: {'reference_valid_for_observed_run': True})
    mapping = dict(lower_ns=0, upper_ns=0, first_wall_ns=0, last_wall_ns=3_000_000_000, gate_pass=True)
    (tmp_path/'capture-health.json').write_text(json.dumps(dict(recording_gate_pass=True, clock_mapping=mapping)))
    calibration = dict(offset_lower_ns=0, offset_upper_ns=0)
    (tmp_path/'timing-health.json').write_text(json.dumps(dict(clock_calibration_before=calibration, clock_calibration_after=calibration)))
    def row(kind, lower, upper, **metadata):
        return dict(kind=kind, lower_ns=lower, upper_ns=upper, metadata=metadata)
    stamp = 1.009 if fault == 'clock' else 1.017
    rows = [row('client_timer_start', 1_000_000_000, 1_000_000_010, record_id='r'),
        row('client_socket_recv', 1_015_000_000, 1_015_000_020,
            native_return_upper_ns=1_015_000_010, socket_fd=1, local=[1], peer=[2],
            wire_b64=base64.b64encode(wire).decode(), byte_count=len(wire),
            wire_sha256=hashlib.sha256(wire).hexdigest()),
        row('client_semantic_ready', 1_018_000_000, 1_018_000_010, record_id='r',
            field='content', content='bad' if fault == 'content' else '42', read_clock_hex=stamp.hex()),
        row('client_timer_end', 1_020_000_000, 1_020_000_010, record_id='r', clock_hex=(1.02).hex())]
    (tmp_path/'timing-events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (tmp_path/'manifest.json').write_text(json.dumps({'scenario': {'request_count': 1}}))
    reported = 27 if fault == 'arithmetic' else 17
    (tmp_path/'results.jsonl').write_text(json.dumps(dict(phase='measured', response_id='known',
        raw={'id': 'r'}, timing={'first_answer_ms': reported}))+'\n')
    if fault == 'reused_socket':
        second_wire = wire.replace(b'known', b'other')
        _, second_events = semantic_events(second_wire)
        monkeypatch.setattr(module, 'decode_capture', lambda _: {
            'responses': {
                'known': {'wire': wire, 'events': events, 'stream': '0', 'times': array('q', [1_010_000_000])*len(wire)},
                'other': {'wire': second_wire, 'events': second_events, 'stream': '1', 'times': array('q', [1_010_000_000])*len(second_wire)}},
            'issues': [], 'auxiliary_connections': 0})
        second_rows = json.loads(json.dumps(rows))
        for second in second_rows:
            meta = second['metadata']
            if 'record_id' in meta:
                meta['record_id'] = 's'
            if 'wire_b64' in meta:
                meta.update(wire_b64=base64.b64encode(second_wire).decode(),
                            byte_count=len(second_wire), wire_sha256=hashlib.sha256(second_wire).hexdigest())
        with (tmp_path/'timing-events.jsonl').open('a') as output:
            output.write(''.join(json.dumps(r)+'\n' for r in second_rows))
        with (tmp_path/'results.jsonl').open('a') as output:
            output.write(json.dumps(dict(phase='measured', response_id='other', raw={'id': 's'},
                                         timing={'first_answer_ms': 17}))+'\n')
    result = module.analyze_case(tmp_path)
    if fault in {'content', 'packet_bytes', 'clock'}:
        assert result['matched'] == 0 and result['issues']
    else:
        assert result['matched'] == (2 if fault == 'reused_socket' else 1) and not result['issues']
        records = json.loads((tmp_path/'packet-timing-records.jsonl').read_text().splitlines()[0])
        assert records['arrival_to_giraffe_ms'][0] < 7 < records['arrival_to_giraffe_ms'][1]
        assert records['arrival_to_giraffe_ms'][1] - records['arrival_to_giraffe_ms'][0] < .003
        assert result['arithmetic_intervals_containing_zero'] == (0 if fault == 'arithmetic' else result['matched'])
        if fault == 'arithmetic':
            assert records['arithmetic_error_ms'][0] > 9.99
        else:
            assert records['reported_minus_arrival_latency_ms'] == pytest.approx([6.999, 7.00101], abs=.000001)


def test_precision_is_specific_to_the_metric():
    from validation_suite.packet_analysis import precision_by_metric
    actual = precision_by_metric({'arrival_to_read_ms': [1, 1.2],
                                  'arrival_to_giraffe_ms': [1.3, 1.31],
                                  'completion_after_body_available_ms': None})
    assert not actual['arrival_to_read_ms']
    assert actual['arrival_to_giraffe_ms']
    assert not actual['completion_after_body_available_ms']


def test_tcp_declared_length_detects_hidden_fin_payload():
    from validation_suite.packet_analysis import decode_payload
    assert decode_payload('6162', '2') == b'ab'
    with pytest.raises(ValueError, match='length mismatch'):
        decode_payload('', '305')


def test_real_tshark_extracts_data_bearing_fin(tmp_path):
    from validation_suite.packet_analysis import decode_capture
    from validation_suite.packet_capture import WIRESHARK, file_hash
    if not (WIRESHARK/'tshark').exists():
        pytest.skip('Wireshark not installed; pure TCP/length tests still run')
    header, _, wire = sse_wire()
    def ipv4(seq, flags, data):
        tcp = struct.pack('!HHIIBBHHH', 8080, 40000, seq, 1, 5 << 4, flags, 4096, 0, 0)+data
        return struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20+len(tcp), 0, 0, 64, 6, 0,
                           socket.inet_aton('127.0.0.1'), socket.inet_aton('127.0.0.1'))+tcp
    # RAW IPv4 pcap, with response header then all SSE data on the FIN.
    pcap = bytearray(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 101))
    for index, packet in enumerate([ipv4(10, 0x12, b''), ipv4(11, 0x18, header),
                                    ipv4(11+len(header), 0x19, wire[len(header):])]):
        pcap.extend(struct.pack('<IIII', 100, index*1000, len(packet), len(packet))+packet)
    path = tmp_path/'packets.pcapng'
    path.write_bytes(pcap)
    (tmp_path/'capture-health.json').write_text(json.dumps({'port': 8080, 'capture_sha256': file_hash(path)}))
    result = decode_capture(tmp_path)
    assert not result['issues']
    assert result['responses']['known']['wire'] == wire
    assert result['responses']['known']['times'][-1] == 100_002_000_000


def test_disk_packet_index_crosses_old_connection_limit_and_refuses_reuse(tmp_path):
    from validation_suite.packet_index import PacketIndex
    path = tmp_path / 'index.sqlite'
    index = PacketIndex(path)
    for n in range(10001):
        index[str(n)] = {'wire': b'42', 'times': array('q', [n])}
    assert len(index) == 10001
    assert list(index['10000']['times']) == [10000]
    index.append('group', {'response': 'first'})
    index.append('group', {'response': 'second'})
    assert index['group'] == [{'response': 'first'}, {'response': 'second'}]
    index.close()
    with pytest.raises(FileExistsError):
        PacketIndex(path)


def test_five_minute_capture_is_bounded(tmp_path):
    from validation_suite.packet_capture import CaptureSession
    capture = CaptureSession(tmp_path, 40000, maximum_seconds=360)
    assert 'duration:360' in capture.command and 'filesize:1000000' in capture.command
    with pytest.raises(ValueError):
        CaptureSession(tmp_path, 40000, maximum_seconds=3600)
