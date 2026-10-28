"""SGLang scheduler gauges are observations, not vLLM counter totals."""
import json

import pytest

from giraffe import telemetry
from giraffe.cli import load_report
from giraffe.models import RunConfig, Target
from giraffe.reporting import write_report
from tests.test_performance import record, report, summary
from tests.test_ux_results import run_render_checks

LENGTH = 'spec_decode_sglang_acceptance_length'
RATE = 'spec_decode_sglang_acceptance_ratio'


def scrape(length=2.5, rate=.75, *, rank='0', elapsed=1, profile='sglang', body=None, cap=128):
    target = Target(name='t', model='m', url='http://localhost:30000/v1', metrics_profile=profile)
    if body is None:
        body = (f'sglang:spec_accept_length{{model_name="m",tp_rank="{rank}"}} {length}\n'
                f'sglang:spec_accept_rate{{model_name="m",tp_rank="{rank}"}} {rate}\n')
    return telemetry.parse(body, target, RunConfig(targets=[target], metrics_max_series=cap)) | {
        'elapsed_seconds': elapsed, 'collected_at': '2026-10-27T10:00:01+00:00', 'phase': 'load'}


def derived(*snapshots):
    return telemetry.derive_server_metrics({'snapshots': list(snapshots)})


def test_latest_gauges_keep_bonus_token_and_scheduler_interval_semantics():
    snapshots = [scrape(4, .9), scrape(2.5, .75, elapsed=2)]
    rows = derived(*snapshots)
    assert rows[LENGTH]['value'] == 2.5
    assert rows[RATE]['value'] == .75  # not mean, difference, or a pooled acceptance rate
    assert rows[LENGTH]['unit'] == 'tokens/forward'
    assert 'bonus token' in rows[LENGTH]['source']
    for key in (LENGTH, RATE):
        assert rows[key]['state'] == 'measured'
        assert rows[key]['details']['activity'] == 'unknown'
        assert rows[key]['details']['elapsed_seconds'] == 2
        assert 'earlier scheduler interval' in rows[key]['details']['freshness']
    assert rows['spec_decode_drafts']['value'] is None
    assert rows['spec_decode_accepted_tokens']['value'] is None
    assert rows['spec_decode_acceptance_ratio']['value'] is None
    raw = telemetry.summarize({'snapshots': snapshots}, .9)
    assert all(r['kind'] == 'gauge' and r.get('delta') is None for r in raw['series'])


def test_multiple_scheduler_labels_are_not_averaged_or_summed():
    a, b = scrape(), scrape(1, 0, rank='1')
    a['samples'] += b['samples']
    rows = derived(a)
    assert rows[RATE]['value'] == [
        {'labels': {'model_name': 'm', 'tp_rank': '0'}, 'acceptance_ratio': .75},
        {'labels': {'model_name': 'm', 'tp_rank': '1'}, 'acceptance_ratio': 0.0}]
    assert rows[LENGTH]['value'][1]['acceptance_length'] == 1


@pytest.mark.parametrize('problem', ['missing', 'failed', 'stale', 'duplicate', 'truncated', 'invalid'])
def test_latest_invalid_gauge_does_not_resurrect_older_evidence(problem):
    first, last = scrape(), scrape(elapsed=2)
    if problem == 'missing':
        last['samples'] = []
    elif problem == 'failed':
        last['status'] = 'connection_failure'
    elif problem == 'stale':
        for sample in last['samples']:
            sample['stale'] = True
    elif problem == 'duplicate':
        last['samples'] *= 2
    elif problem == 'truncated':
        last.update(series_limit_reached=True, truncated_families=[])
    else:
        last['samples'][0]['value'] = -1
        last['samples'][1]['value'] = 1.5
    rows = derived(first, last)
    assert rows[LENGTH]['value'] is None
    assert rows[RATE]['value'] is None


def test_zero_absent_and_unsupported_are_distinct_without_an_enabled_claim():
    zero = derived(scrape(0, 0))
    assert zero[LENGTH]['value'] == zero[RATE]['value'] == 0
    assert zero[LENGTH]['details']['activity'] == 'unknown'
    missing = derived(scrape(body='sglang:num_running_reqs 0\n'))
    assert missing[LENGTH]['value'] is None
    assert missing[LENGTH]['state'] == 'insufficient_evidence'
    wrong = derived(scrape(body='vllm:num_requests_running 0\n', profile='vllm-v1'))
    assert wrong[LENGTH]['state'] == 'not_supported'
    assert derived()[LENGTH]['state'] == 'not_collected'


def test_optional_gauges_autodetect_without_displacing_serving_or_gpu_series():
    assert scrape(profile='unknown')['profile'] == 'sglang'
    body = ('sglang:spec_accept_length 2.5\nsglang:spec_accept_rate .75\n'
            'sglang:num_running_reqs 1\nDCGM_FI_DEV_XID_ERRORS{gpu="7"} 79\n')
    parsed = scrape(body=body, cap=2)
    assert {s['family'] for s in parsed['samples']} == {'running', 'device_errors'}
    assert parsed['series_limit_reached']
    assert derived(parsed)[RATE]['value'] is None


def saved_gauge_report(*, multi=False):
    snap = scrape()
    if multi:
        snap['samples'] += scrape(1, 0, rank='1')['samples']
    # Older caches must be refreshed from raw samples for newly added fields.
    data = {'snapshots': [snap], 'derived': {k: v for k, v in derived().items() if k not in {LENGTH, RATE}}}
    return report([record()], observations={'targets': {'t': {'serving_telemetry': data}}})


def test_gauges_survive_json_html_and_ui_with_qualifications(tmp_path):
    value = saved_gauge_report()
    path, html_path = write_report(value, tmp_path, html=True)
    rows = summary(load_report(path))['sections']['server']
    selected = {r['id']: r for r in rows}
    assert selected[RATE]['value'] == .75
    assert selected[RATE]['sample_unit'] == 'label series in latest scrape'
    assert selected[RATE]['details']['selected_window_attribution'] == 'not established'
    html = html_path.read_text()
    assert 'SGLang reported acceptance rate' in html
    assert '75' in html and '<small>%</small>' in html
    assert 'bonus token' in html and 'earlier scheduler interval' in html
    assert 'not a run-window total' in html
    run_render_checks('const rows=' + json.dumps(rows) + r''';
const markup=performanceMetricTable(rows);
document.querySelector('#main').innerHTML=markup;
const text=$('#main').textContent;
assert.match(text,/SGLang reported acceptance rate/);
assert.match(text,/75.*%/);
assert.match(text,/bonus token/);
assert.match(text,/earlier scheduler interval/);
assert.match(text,/not a run-window total/);
''', dom=True)


def test_replica_values_remain_readable_in_both_renderers(tmp_path):
    value = saved_gauge_report(multi=True)
    _, html_path = write_report(value, tmp_path, html=True)
    assert '75%' in html_path.read_text()
    assert 'Tp rank' in html_path.read_text()
    rows = summary(value)['sections']['server']
    run_render_checks('const rows=' + json.dumps(rows) + r''';
const markup=performanceMetricTable(rows);
assert.match(markup,/75%/);
assert.match(markup,/Tp rank/);
''', dom=True)


@pytest.mark.parametrize('raw_value', ['NaN', '+Inf', '-Inf', '1e999'])
@pytest.mark.parametrize('name,family,other', [
    ('sglang:spec_accept_length', LENGTH, RATE),
    ('sglang:spec_accept_rate', RATE, LENGTH),
])
@pytest.mark.parametrize('rank', ['0', '1'])
def test_nonfinite_raw_gauge_invalidates_latest_family(raw_value, name, family, other, rank):
    body = ('sglang:spec_accept_length{tp_rank="0"} 2.5\n'
            'sglang:spec_accept_rate{tp_rank="0"} .75\n'
            f'{name}{{tp_rank="{rank}"}} {raw_value}\n'
            'sglang:num_running_reqs 3\n')
    latest = scrape(body=body, elapsed=2)
    rows = derived(scrape(), latest)
    assert rows[family]['value'] is None
    assert rows[other]['state'] == 'measured'
    assert any(s['family'] == 'running' and s['value'] == 3 for s in latest['samples'])
    json.dumps(latest, allow_nan=False)
