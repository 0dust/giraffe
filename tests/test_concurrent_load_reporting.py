import copy

from giraffe.models import CheckResult
from giraffe.reporting import _concurrent_load_html, _groups, _render, compare_baseline, write_report
from tests.test_concurrent_load import configuration, perform, point
from tests.test_web import report as empty_report


async def saved_report():
    config = configuration(limits={'min_samples': 2}, load={'combinations': {'enabled': False},
        'mixed': {'enabled': True, 'seed': 7, 'shares': {'short_short': 50, 'long_short': 50}}})
    run, check, _, _ = await perform(config)
    report = empty_report(config)
    report.requests, report.checks = run.records, [check]
    report.manifest.update(timing_measurement_version=2,
        fixture_pack={'version': '1', 'builtin_sha256': 'fixed', 'workload_fixture_sha256': 'fixed'},
        effective_checks={'concurrency': config.concurrent_load.model_dump()}, effective_traffic=None, load_shape={}, run_location={})
    return report


async def test_load_report_columns_and_old_scenario_renderer(tmp_path):
    report = await saved_report()
    _, html_path = write_report(report, tmp_path)
    html = html_path.read_text()
    for text in ['How these are measured', 'Client concurrency mean / peak', 'Server-evidenced concurrency',
                 'Input tokens actual', 'Output tokens actual', 'Requested / observed request mix', 'Valid / required']:
        assert text in html
    old = report.model_copy(deep=True)
    old.checks[0].metrics = {'scenarios': {'size_matrix': {'title': 'Old size matrix', 'status': 'pass', 'summary': 'Saved legacy results', 'metrics': {}}}}
    old_html = _render(old)
    assert 'Old size matrix' in old_html and 'Saved legacy results' in old_html
    assert 'How these are measured' not in old_html


async def test_old_and_new_baseline_explicit_format_reason():
    report = await saved_report()
    old = report.model_copy(deep=True)
    old.checks[0].metrics = {'scenarios': {'size_matrix': {}}}
    compared = compare_baseline(report, old)
    assert compared.baseline['status'] == 'inconclusive'
    assert 'baseline uses the previous Concurrent workloads format' in compared.baseline['targets'][0]['reasons']
    assert compare_baseline(old, report).baseline['status'] == 'inconclusive'


async def test_mixed_baseline_compares_each_shape_group_independently():
    report = await saved_report()
    groups = _groups(report.requests, 'local')
    assert ('concurrency', 'load_mixed_c1.short_short') in groups
    assert ('concurrency', 'load_mixed_c1.long_short') in groups
    compared = compare_baseline(report, report.model_copy(deep=True))
    rows = compared.baseline['targets'][0]['comparisons']
    assert rows, compared.baseline
    scenarios = {r['scenario'] for r in rows}
    assert {'load_mixed_c1.short_short', 'load_mixed_c1.long_short'} <= scenarios
    assert not any('different request shapes' in reason for reason in compared.baseline['targets'][0]['reasons'])


async def test_interference_baseline_compares_each_role_independently():
    config = configuration(load={'combinations': {'enabled': False}, 'interference': {'enabled': True},
        'schedule': {'levels': [2]}})
    run, check, _, _ = await perform(config)
    report = await saved_report()
    report.requests, report.checks = run.records, [check]
    report.manifest['config'] = config.model_dump()
    report.manifest['effective_checks'] = {'concurrency': config.concurrent_load.model_dump()}
    groups = _groups(report.requests, 'local')
    roles = {'load_interference_c2.short_stream', 'load_interference_c2.short_probe', 'load_interference_c2.long_probe'}
    assert roles <= {scenario for check, scenario in groups if check == 'concurrency'}
    compared = compare_baseline(report, report.model_copy(deep=True))
    assert roles <= {row['scenario'] for row in compared.baseline['targets'][0]['comparisons']}
    assert not any('different request shapes' in reason for reason in compared.baseline['targets'][0]['reasons'])
    html = _render(report)
    assert 'Buffered short streams excluded from matching:' in html
    assert 'ambiguous A/B overlaps excluded from both groups:' in html
    assert 'Short-stream concurrency mean / peak / server-evidenced:' in html
