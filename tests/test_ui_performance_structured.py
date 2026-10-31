"""Measured nested evidence stays readable in both report renderers."""
import pytest

from giraffe.performance_html import _table
from tests.test_ux_results import run_render_checks


ROWS = [
    dict(id='latency_attainment', label='Latency-limit attainment', unit='responses',
         value=[{'valid': True, 'timing_violations': ['first_output_ms'], 'missing_timing': []}]),
    dict(id='drops', label='Scheduler drops', unit='requests',
         value=[{'local_drops': 0, 'late_drops': 2, 'not_offered': 5}]),
    dict(id='nested', label='Nested observation', unit='ms', value={'probe': {'elapsed_ms': 12, 'note': '<unsafe>'}}),
]
for row in ROWS:
    row.update(state='measured', source='Recorded evidence', sample_count=1, eligible_count=1)


def test_app_structured_summary_and_disclosure():
    import json
    run_render_checks('const rows=' + json.dumps(ROWS) + r''';
const markup=performanceMetricTable(rows);
document.querySelector('#main').innerHTML=markup;
assert(!markup.includes('Recorded; see evidence'));
assert.equal($$('.performance-metric-value details').length,3);
assert.match($('#main').textContent,/First useful output limit/);
assert.match($('#main').textContent,/Local drops 0 · Late 2 · Not offered 5/);
assert.match($('#main').textContent,/Elapsed time.*12 ms/);
assert(markup.includes('&lt;unsafe&gt;'));
assert(!markup.includes('<unsafe>'));
''', dom=True)


@pytest.mark.parametrize('row', ROWS)
def test_html_structured_summary_and_disclosure(row):
    html = _table([row])
    assert '<details' in html and '<summary>Underlying values</summary>' in html
    assert '<pre>' not in html
    if row['id'] == 'latency_attainment':
        assert 'First useful output limit' in html
    elif row['id'] == 'drops':
        assert 'Local drops 0 · Late 2 · Not offered 5' in html
    else:
        assert '12 ms' in html and '&lt;unsafe&gt;' in html and '<unsafe>' not in html


def test_html_structured_evidence_is_bounded():
    row = dict(ROWS[2], value={'samples': [{'elapsed_ms': n} for n in range(1000)]})
    html = _table([row])
    assert html.count('<tr>') < 60
    assert 'Preview limited' in html


def test_app_structured_evidence_is_bounded():
    run_render_checks(r'''
const value={samples:Array.from({length:1000},(_,elapsed_ms)=>({elapsed_ms}))};
const markup=performanceMetricTable([{id:'samples',label:'Samples',value,state:'measured'}]);
document.querySelector('#main').innerHTML=markup;
assert($$('.performance-evidence tbody tr').length<=40);
assert.match($('#main').textContent,/Preview limited/);
''', dom=True)


def test_standalone_report_shows_recorded_latency_violation():
    from giraffe.reporting import _render
    from tests.test_performance import record, report

    saved = report([record(load_point={'response_criteria': ROWS[0]['value'][0]})])
    html = _render(saved)
    assert 'First useful output limit' in html
    assert '<summary>Underlying values</summary>' in html
    assert 'Recorded; see evidence' not in html


ENUM_ROW = {"id": "latency_samples", "label": "Latency samples affected", "state": "measured",
            "value": {"status": "client_delayed", "scope": "request_intervals", "threshold_ms": 100}}


def test_structured_enum_values_are_readable_in_app_and_html():
    import json
    run_render_checks('const rows=' + json.dumps([ENUM_ROW]) + r''';
document.querySelector('#main').innerHTML=performanceMetricTable(rows);
const text=$('#main').textContent;
assert.match(text,/Status: Client delayed · Scope: Request intervals/);
assert(!/client_delayed|request_intervals/.test(text));
''', dom=True)
    html = _table([ENUM_ROW])
    assert "Status: Client delayed · Scope: Request intervals" in html
    assert "client_delayed" not in html and "request_intervals" not in html
