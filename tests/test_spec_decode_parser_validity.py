"""Rejected exporter samples must not make speculative coverage look complete."""
import json

import pytest

from giraffe import telemetry
from tests.test_sglang_spec_decode import LENGTH, RATE, derived, scrape
from tests.test_spec_decode_telemetry import NAMES, POSITION, body, derive, snapshot


@pytest.mark.parametrize('labels', [
    'tp_rank=1', 'tp_rank="0",tp_rank="1"', 'tp_rank="0",tp_rank="0"',
    'tp_rank="0" garbage', 'garbage,tp_rank="0"', 'tp_rank="0" engine="1"',
    'tp_rank="0",,engine="1"', ',tp_rank="0"', 'tp_rank="unterminated',
    r'tp_rank="bad\q"',
])
@pytest.mark.parametrize('name,family,other', [
    ('sglang:spec_accept_length', LENGTH, RATE),
    ('sglang:spec_accept_rate', RATE, LENGTH),
])
def test_malformed_sglang_labels_invalidate_latest_family(labels, name, family, other):
    text = ('sglang:spec_accept_length{tp_rank="healthy"} 2.5\n'
            'sglang:spec_accept_rate{tp_rank="healthy"} .75\n'
            f'{name}{{{labels}}} .5\n')
    latest = scrape(body=text)
    rows = derived(scrape(), latest)
    assert family in latest['invalid_families']
    assert rows[family]['value'] is None
    assert rows[other]['state'] == 'measured'
    assert all(s['labels'] == {'tp_rank': 'healthy'} for s in latest['samples'])


@pytest.mark.parametrize('labels,expected', [
    ('', {}), (' \t ', {}), ('tp_rank="0",', {'tp_rank': '0'}),
    (' tp_rank = "0" , engine = "worker,1" , ', {'tp_rank': '0', 'engine': 'worker,1'}),
    (r'tp_rank="0",model="name\"with\\escapes\n雪}"',
     {'tp_rank': '0', 'model': r'name\"with\\escapes\n雪}'}),
])
def test_valid_label_blocks_preserve_worker_identity(labels, expected):
    for snap, key in [
        (scrape(body=f'sglang:spec_accept_rate{{{labels}}} .75\n'), RATE),
        (snapshot(f'{NAMES["drafts"]}{{{labels}}} 1\n', 0), 'spec_decode_drafts'),
    ]:
        assert snap['invalid_families'] == []
        assert len(snap['samples']) == 1
        assert snap['samples'][0]['family'] == key
        assert snap['samples'][0]['labels'] == expected


@pytest.mark.parametrize('raw', ['NaN', '+Inf', '-Inf', '1e999'])
@pytest.mark.parametrize('bad_family', list(NAMES))
def test_invalid_vllm_worker_excludes_interval_and_raw_delta(raw, bad_family):
    text = f'{NAMES[bad_family]}{{engine="bad"}} {raw}\n'
    first = snapshot(body(0, 0, 0) + text, 0)
    last = snapshot(body(1, 3, 2) + text, 1)
    result = telemetry.summarize({'snapshots': [first, last]}, .9)
    for field in ['drafts', 'draft_tokens', 'accepted_tokens', 'proposal_length', 'acceptance_ratio']:
        row = result['derived']['spec_decode_' + field]
        assert row['value'] is None
        assert row['details']['observed_intervals'] == 0
        assert row['details']['excluded_intervals'] == 1
    bad = next(r for r in result['series'] if r['family'] == 'spec_decode_' + bad_family)
    assert bad['delta'] is None
    json.dumps(result, allow_nan=False)


def test_invalid_middle_worker_cannot_bridge_but_later_windows_recover():
    snaps = [snapshot(body(i, i*3, i*2), i) for i in range(4)]
    snaps[1] = snapshot(body(1, 3, 2) + body('NaN', 'NaN', 'NaN', engine='bad'), 1)
    row = derive(*snaps)['spec_decode_acceptance_ratio']
    assert row['value'] == pytest.approx(2/3)
    assert row['details']['observed_intervals'] == 1
    assert row['details']['excluded_intervals'] == 2
    assert row['details']['partial_coverage'] is True
    assert row['details']['duration_seconds'] == 1


@pytest.mark.parametrize('labels', ['engine=1', 'engine="0",engine="1"'])
def test_malformed_vllm_labels_cannot_hide_worker_identity(labels):
    bad = '\n'.join(f'{name}{{{labels}}} 0' for name in NAMES.values()) + '\n'
    row = derive(snapshot(body(0, 0, 0)+bad, 0), snapshot(body(1, 3, 2)+bad, 1))
    assert row['spec_decode_acceptance_ratio']['value'] is None


def test_bad_position_evidence_does_not_remove_valid_aggregate_evidence():
    bad = f'{POSITION}{{engine="bad",position="0"}} NaN\n'
    snaps = [snapshot(body(0, 0, 0, positions=(0, 0, 0))+bad, 0),
             snapshot(body(1, 3, 2, positions=(1, 1, 0))+bad, 1)]
    rows = derive(*snaps)
    assert rows['spec_decode_acceptance_ratio']['value'] == pytest.approx(2/3)
    assert rows['spec_decode_accepted_tokens']['details']['accepted_tokens_by_position'] == []
    assert rows['spec_decode_accepted_tokens']['details']['position_coverage']['excluded_intervals'] == 1
