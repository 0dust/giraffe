"""Final Load test groups, baseline clocks and retained historical rendering."""

import pytest
from giraffe.models import CheckResult
from giraffe.reporting import _groups, _metric_values, _render, compare_baseline, write_report
from tests.test_concurrent_load import configuration, perform
from tests.test_web import report as empty_report


pytestmark = pytest.mark.usefixtures("local_tokenizer")


async def saved_report(interference=False):
    settings = {'combinations': {'enabled': False}, 'pass_criteria': {'max_completion_ms': 1000}}
    settings.update({'interference': {'enabled': True}, 'schedule': {'levels': [2]}}
                    if interference else {'mixed': {'enabled': True, 'shares': {'short_short': 50, 'long_short': 50}}})
    config = configuration(limits={'min_samples': 2}, load=settings)
    run, check, _, _ = await perform(config)
    report = empty_report(config)
    report.requests, report.checks = run.records, [check]
    report.manifest.update(timing_measurement_version=2,
        fixture_pack={'version': '1', 'builtin_sha256': 'fixed', 'workload_fixture_sha256': 'fixed'},
        effective_checks={'load': config.load_test.model_dump()}, effective_traffic=None, load_shape={}, run_location={})
    return report


async def test_final_load_columns_and_old_scenario_renderer(tmp_path):
    report = await saved_report()
    _, html_path = write_report(report, tmp_path, html=True)
    html = html_path.read_text()
    for text in ['Timing clock:', 'Observed concurrency:', 'Actual input tokens',
                 'Observed request shares', 'Correct work meeting limits', 'highest tested']:
        assert text in html
    old = report.model_copy(deep=True)
    old.checks = [CheckResult(id='concurrency', title='Concurrent workloads', target='local', status='pass',
        summary='Saved report', metrics={'scenarios': {'size_matrix': {'title': 'Old size matrix',
        'status': 'pass', 'summary': 'Saved legacy results', 'metrics': {}}}})]
    old_html = _render(old)
    assert 'Old size matrix' in old_html and 'Saved legacy results' in old_html


async def test_changed_load_settings_block_baseline_comparison():
    report = await saved_report()
    old = report.model_copy(deep=True)
    old.manifest['config']['load_test']['patterns']['poisson']['seed'] += 1
    compared = compare_baseline(report, old)
    assert compared.baseline['status'] == 'inconclusive'
    assert 'Run setting load_test differs.' in compared.baseline['targets'][0]['reasons']


async def test_final_mixed_baseline_compares_each_shape_group_independently():
    report = await saved_report()
    groups = _groups(report.requests, 'local')
    shapes = {scenario.rsplit('.', 1)[-1] for check, scenario in groups if check == 'load'}
    assert shapes == {'short_short', 'long_short'}
    compared = compare_baseline(report, report.model_copy(deep=True))
    rows = compared.baseline['targets'][0]['comparisons']
    assert {row['scenario'].rsplit('.', 1)[-1] for row in rows} == shapes
    assert not compared.baseline['targets'][0]['reasons']


async def test_final_interference_baseline_compares_each_role_independently():
    report = await saved_report(interference=True)
    groups = _groups(report.requests, 'local')
    roles = {scenario.rsplit('.', 1)[-1] for check, scenario in groups if check == 'load'}
    assert {'short_stream', 'probe'} == roles
    compared = compare_baseline(report, report.model_copy(deep=True))
    assert {row['scenario'] for row in compared.baseline['targets'][0]['comparisons']} == {s for c, s in groups}
    assert 'Short-only vs long-request comparison' in _render(report)


def test_arrival_baseline_uses_planned_arrival_clock_and_preserves_missing_evidence():
    from tests.test_load_test_phase_a import record
    rows = [record().model_copy(update={'elapsed_ms': 10, 'first_output_ms': 2, 'arrival_elapsed_ms': 50,
                   'arrival_first_output_ms': 42, 'load_point': {'pattern': 'constant'}})]
    assert _metric_values(rows)['latency_ms'] == [50]
    assert _metric_values(rows)['first_output_ms'] == [42]
    rows[0].arrival_elapsed_ms = None
    assert _metric_values(rows)['latency_ms'] == []
    rows[0].load_point['pattern'] = 'concurrent'
    assert _metric_values(rows)['latency_ms'] == [10]


async def test_arrival_baseline_missing_one_response_clock_excludes_only_that_metric():
    report = await saved_report()
    for record in report.requests:
        if (record.load_point or {}).get('point_id'):
            record.load_point['pattern'] = 'constant'
            record.arrival_elapsed_ms = record.elapsed_ms + 50
            record.arrival_first_output_ms = record.first_output_ms + 50
    old = report.model_copy(deep=True)
    changed = next(record for record in report.requests if (record.load_point or {}).get('point_id'))
    changed.arrival_elapsed_ms = None
    compared = compare_baseline(report, old)
    affected = [row for row in compared.baseline['targets'][0]['comparisons']
                if row['metric'] == 'latency_ms' and changed.id in row['evidence_ids']]
    assert affected and all(row['status'] == 'inconclusive' for row in affected)
    # AM9: an undefined latency excludes that metric. Comparable unchanged
    # first-output evidence still covers the selected Load test check.
    assert any(row['metric'] == 'first_output_ms' and row['status'] == 'pass'
               for row in compared.baseline['targets'][0]['comparisons'])
    assert compared.baseline['status'] == 'pass'


# Fake inference clients in this module use the matching local tokenizer.
