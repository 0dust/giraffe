"""Comparable performance workloads span schedule stages, never quality roles."""
import json
from copy import deepcopy

import pytest

from giraffe.conditions import condition_for
from giraffe.models import CHECK_NAMES
from giraffe.performance import _cohort_label, build_performance
from tests.performance_fixture import synthetic_report_1000
from tests.test_performance import record, report


def selected(value):
    target = build_performance(value)['targets'][0]
    return target, target['summaries'][target['default_window']]


def test_synthetic_full_workload_default_and_own_stage_windows():
    value = synthetic_report_1000()
    target, data = selected(value)
    assert len(target['cohorts']) == 1
    assert data['window']['request_count'] == 1000
    assert data['metrics']['attempted']['value'] == 1000
    assert data['metrics']['request_throughput']['value'] == pytest.approx(1000 / data['window']['duration_s'])
    assert len(data['request_row_indices']) == 1000
    assert sum(bucket['count'] for bucket in data['completion_buckets']['buckets']) == 1000
    stages = [window for window in target['windows'] if window['kind'] == 'stage']
    assert [window['label'] for window in stages] == ['C4', 'C8', 'C16', 'C32', 'C16', 'C8', 'Drain']
    assert [window['request_count'] for window in stages[:-1]] == [80, 140, 200, 300, 180, 100]
    assert [row['intended_concurrency'] for row in data['intended_concurrency']] == [4, 8, 16, 32, 16, 8, 0]
    assert data['window']['stage_count'] == 6
    assert 'six changing concurrency stages' in data['window']['description']
    assert 'synthetic_stage' not in data['window']['description']
    assert 'synthetic_stage' not in ''.join(window['selection_label'] for window in target['windows'])


def load_record(identifier, *, level, start, shape='short_short', source='primary', role='request', phase=None, valid=True):
    result = record(identifier, start=start, end=start + 100)
    point = f'load_{source}_combo_{shape}_concurrent_c{level}'
    result.scenario = point
    result.condition_id = point
    result.check_ids = ['load']
    result.load_point = dict(point_id=point, type='combinations', workload=shape, source=source,
                             shape=shape, role=role, phase=phase, level=level, pattern='concurrent')
    result.valid = valid
    return result


def test_default_prefers_largest_valid_performance_workload_then_earliest():
    quality = [record(f'quality{i}', start=i, end=i + 100) for i in range(10)]
    rows = quality + [load_record('a', level=4, start=1000), load_record('b', level=8, start=2000),
                      load_record('invalid', level=8, start=2100, valid=False),
                      load_record('long-a', level=4, start=3000, shape='long_short')]
    target, data = selected(report(rows))
    assert data['window']['request_count'] == 3
    assert data['metrics']['completed']['value'] == 2
    assert data['window']['workload_label'] == 'Load test · Short → Short'
    assert len(target['cohorts']) == 3
    assert any(window['workload_label'] == 'Generation' for window in target['windows'])
    rows.append(load_record('long-b', level=8, start=4000, shape='long_short'))
    assert selected(report(rows))[1]['window']['request_count'] == 3  # valid-count tie chooses earliest


def test_old_valid_completions_rank_workloads_even_without_lifetime_timing():
    rows = [load_record('first', level=4, start=0),
            load_record('larger-a', level=4, start=1, shape='long_short'),
            load_record('larger-b', level=8, start=2, shape='long_short')]
    for row in rows:
        row.dispatch_ms = row.completed_ms = None
    target, data = selected(report(rows))
    assert data['window']['request_count'] == 2
    assert data['metrics']['request_throughput']['value'] is None
    assert data['window']['workload_label'] == 'Load test · Long → Short'


@pytest.mark.parametrize('check', list(CHECK_NAMES))
def test_whole_check_cohorts_use_current_names_without_rewriting_saved_conditions(check):
    row = record().model_copy(update={'scenario': check, 'check_ids': [check], 'condition_id': check})
    condition = condition_for(check, {})
    value = report([row], observations={'targets': {'t': {'conditions': [{'condition': condition}]}}})
    saved = json.dumps(value.observations, sort_keys=True)
    target, data = selected(value)
    cohort, = target['cohorts']
    expected = CHECK_NAMES[check] + (' · Recovery check' if check == 'recovery' else '')
    assert cohort['label'] == expected
    assert data['window']['workload_label'] == expected
    assert target['default_window'] == 'full:' + cohort['id']
    assert data['metrics']['attempted']['value'] == 1
    assert data['metrics']['completed']['value'] == 1
    assert data['request_row_indices'] == [0]
    assert json.dumps(value.observations, sort_keys=True) == saved
    assert value.requests[0].scenario == check and value.requests[0].condition_id == check
    cached = deepcopy(value.performance)
    cached_target = cached['targets'][0]
    cached_target['cohorts'][0]['label'] = condition['label']
    for window in [*cached_target['windows'], *(summary['window'] for summary in cached_target['summaries'].values())]:
        window['workload_label'] = condition['label']
        if window['selection_label'].startswith(expected + ' · '):
            window['selection_label'] = condition['label'] + window['selection_label'][len(expected):]
    restored = report([row], performance=cached)
    assert restored.performance['targets'][0]['cohorts'][0]['label'] == expected
    assert cached_target['cohorts'][0]['label'] == condition['label']


@pytest.mark.parametrize('scenario,check,saved_label,expected', [
    ('generation', 'generation', None, 'Generation'),
    ('generation', 'generation', 'Generation speed · 8 at once', 'Generation'),
    ('generation', 'generation', 'Custom passage', 'Custom passage'),
    ('first_output', 'first_output', 'Custom startup', 'Custom startup'),
    ('prefix_shared', 'prefix', None, 'Shared prefix'),
    ('client_deadline', 'cancellation', None, 'Client deadline'),
    ('warm', 'serving', None, 'Warm-up'),
])
def test_cohort_names_preserve_custom_labels_and_distinct_phases(scenario, check, saved_label, expected):
    row = record().model_copy(update={'scenario': scenario, 'check_ids': [check], 'condition_id': scenario})
    conditions = [{'condition': {'id': scenario, 'label': saved_label}}] if saved_label else []
    value = report([row], observations={'targets': {'t': {'conditions': conditions}}})
    target, data = selected(value)
    assert target['cohorts'][0]['label'] == expected
    assert data['window']['workload_label'] == expected
    assert value.observations['targets']['t']['conditions'] == conditions


def test_saved_custom_subphase_label_does_not_parse_its_scenario_again():
    scenario = 'load_primary_custom_r1e-'
    row = record().model_copy(update={'scenario': scenario, 'check_ids': ['load'], 'condition_id': scenario})
    observation = {'conditions': [{'condition': {'id': scenario, 'label': 'Imported workload'}}]}
    assert _cohort_label([row], observation) == ('load', 'Load test · Imported workload')


def archived_performance_names():
    rows = [
        ('generation', 'generation', 'Generation speed', ['generation']),
        ('context', 'context', 'Long-context recall', ['context']),
        ('json_c4', 'json', 'Structured JSON', ['json_c4']),
        ('recovery_baseline', 'recovery', 'Recovery baseline', ['recovery_baseline']),
        ('sustained', 'recovery', 'Sustained load', ['sustained']),
        ('recovery', 'recovery', 'Recovery check', ['recovery']),
        ('session_fixed:check=sessions', 'sessions', 'Multi-turn sessions · Fixed sessions', ['session_fixed_turn0', 'session_fixed_turn1']),
        ('session_live:check=sessions', 'sessions', 'Multi-turn sessions · Live sessions', ['session_live_turn0', 'session_live_turn1']),
        ('custom', 'generation', 'My custom workload', ['generation']),
        ('custom_sessions', 'sessions', 'Recorded talks', ['session_fixed_turn0']),
        ('unknown', 'future_check', 'Generation speed', ['generation']),
    ]
    cohorts = [dict(id=identifier, check=check, label=label, scenarios=scenarios) for identifier, check, label, scenarios in rows]
    windows = [dict(id='full:' + row['id'], kind='full', cohort_id=row['id'], label='Full request window',
                    workload_label=row['label'], selection_label=row['label'] + ' · full run (0–1 s, 6 requests)',
                    request_count=6, start_ms=0, end_ms=1000) for row in cohorts]
    windows += [dict(windows[0], id='custom-window', workload_label='Custom window', selection_label='Generation speed · custom label'),
                dict(windows[0], id='custom-selection', selection_label='My selected view'),
                dict(windows[0], id='prefixed-custom-selection', selection_label='Generation speed · my imported selection')]
    summaries = {window['id']: {'window': deepcopy(window), 'metrics': {'throughput': {'value': 3.25, 'sample_count': 6}},
                               'request_row_indices': [0, 1, 2, 3, 4, 5]} for window in windows}
    target = dict(target='t', cohorts=cohorts, windows=windows, summaries=summaries,
                  default_cohort='generation', default_window='full:generation', selected_window='full:generation', request_series=[])
    return dict(performance_schema=1, targets=[target], default_target='t', selected_target='t',
                available_targets=[{'target': 't'}])


def test_archived_performance_names_change_only_display_copies():
    source = archived_performance_names()
    before = deepcopy(source)
    value = report([], performance=source)
    displayed = build_performance(value)
    target = displayed['targets'][0]
    labels = {row['id']: row['label'] for row in target['cohorts']}
    assert labels == {
        'generation': 'Generation', 'context': 'Context recall', 'json_c4': 'JSON correctness',
        'recovery_baseline': 'Recovery baseline', 'sustained': 'Sustained load',
        'recovery': 'Recovery after load · Recovery check',
        'session_fixed:check=sessions': 'Concurrent conversations · Fixed sessions',
        'session_live:check=sessions': 'Concurrent conversations · Live sessions',
        'custom': 'My custom workload', 'custom_sessions': 'Recorded talks', 'unknown': 'Generation speed',
    }
    for window in target['windows']:
        if window['id'] == 'custom-window':
            assert window['workload_label'] == 'Custom window'
            assert window['selection_label'] == 'Generation speed · custom label'
        else:
            assert window['workload_label'] == labels[window['cohort_id']]
            custom = {'custom-selection': 'My selected view', 'prefixed-custom-selection': 'Generation speed · my imported selection'}
            assert window['selection_label'] == custom.get(window['id'], labels[window['cohort_id']] + ' · full run (0–1 s, 6 requests)')
        assert target['summaries'][window['id']]['window'] == window
    def without_names(item):
        if isinstance(item, dict):
            return {key: without_names(value) for key, value in item.items() if key not in {'label', 'workload_label', 'selection_label'}}
        return [without_names(value) for value in item] if isinstance(item, list) else item
    assert without_names(displayed) == without_names(before)
    assert source == before
    assert build_performance(value) == displayed


def test_shape_source_phase_and_role_stay_separate_when_levels_merge():
    rows = [load_record('a', level=4, start=0), load_record('b', level=8, start=1000),
            load_record('shape', level=8, start=2000, shape='long_short'),
            load_record('source', level=8, start=3000, source='saved'),
            load_record('control', level=8, start=4000, phase='control'),
            load_record('probe', level=8, start=5000, role='short_stream', phase='probe')]
    target, _ = selected(report(rows))
    assert sorted(cohort['request_count'] for cohort in target['cohorts']) == [1, 1, 1, 1, 2]


def test_unrelated_stage_does_not_create_carry_over_window_or_intent():
    rows = [load_record('short', level=4, start=0), load_record('long', level=8, start=200, shape='long_short')]
    rows[0].completed_ms = 1000
    rows[0].elapsed_ms = 1000
    transitions = [dict(stage_id=rows[0].scenario, check_id='load', label='C4', kind='concurrency', start_ms=0, end_ms=200, intended_concurrency=4),
                   dict(stage_id=rows[1].scenario, check_id='load', label='C8', kind='concurrency', start_ms=200, end_ms=800, intended_concurrency=8)]
    target, data = selected(report(rows, observations={'targets': {'t': {'schedule_transitions': transitions}}}))
    windows = [window for window in target['windows'] if window['cohort_id'] == target['default_cohort']]
    assert [window['label'] for window in windows if window['kind'] == 'stage'] == ['C4']
    assert [row['intended_concurrency'] for row in data['intended_concurrency']] == [4]


def test_rate_sweep_has_one_cohort_but_different_workload_roles_do_not():
    rows = [record(str(i), start=i * 1000, end=(i + 1) * 1000) for i in range(3)]
    for index, row in enumerate(rows):
        row.scenario = f'traffic_stage_{index}'
        row.condition_id = row.scenario
        row.traffic_stage = index
        row.check_ids = ['capacity']
        row.workload = {'rate_rps': index + 1}
    target, data = selected(report(rows))
    assert len(target['cohorts']) == 1
    assert data['window']['request_count'] == 3


def test_legacy_bucket_levels_are_windows_of_the_same_shape():
    rows = [record(str(level), start=level * 1000, end=level * 1000 + 1000) for level in (4, 8)]
    for level, row in zip((4, 8), rows):
        row.scenario = row.condition_id = f'bucket_c{level}_short_short'
        row.check_ids = ['concurrency']
    target, data = selected(report(rows))
    assert len(target['cohorts']) == 1
    assert data['window']['request_count'] == 2
    assert data['window']['workload_label'] == 'Concurrency · Short input / short output'


def test_legacy_traffic_sweep_is_one_workload_without_inventing_stage_timing():
    from tests.test_traffic_reporting import traffic_report

    target, data = selected(traffic_report())
    assert len(target['cohorts']) == 1
    assert data['window']['request_count'] == 25
    assert data['metrics']['highest_acceptable_rate']['value'] == 4
    assert data['metrics']['request_throughput']['value'] is None
    assert len(target['windows']) == 1  # No monotonic schedule bounds were saved.


def test_actual_request_sources_and_roles_do_not_merge_or_lose_readable_identity():
    rows = [load_record('builtin', level=4, start=0), load_record('custom', level=8, start=1000),
            load_record('pack', level=8, start=2000), load_record('control', level=8, start=3000)]
    rows[1].workload = {'request_source': 'custom_fixtures'}
    rows[2].workload = {'request_source': 'request_pack', 'case_id': 'first-case'}
    rows[3].workload = {'role': 'control'}
    target, _ = selected(report(rows))
    assert len(target['cohorts']) == 4
    labels = [cohort['label'] for cohort in target['cohorts']]
    assert 'Load test · Short → Short · Custom questions' in labels
    assert 'Load test · Short → Short · Request pack' in labels
    assert 'Load test · Short → Short · Control' in labels


def test_live_open_stage_uses_observed_lifetime_end_without_inventing_a_clock():
    row = load_record('observed', level=4, start=0)
    transition = dict(stage_id=row.scenario, check_id='load', kind='concurrency', start_ms=0,
                      end_ms=None, intended_concurrency=4)
    target, data = selected(report([row], observations={'targets': {'t': {'schedule_transitions': [transition]}}}))
    assert data['window']['duration_s'] == .1
    assert data['intended_concurrency'][0]['end_elapsed_s'] == .1
    assert target['windows'][1]['end_ms'] == 100


def test_delivery_pattern_changes_are_windows_of_the_same_workload():
    rows = []
    for index, pattern in enumerate(('concurrent', 'constant', 'poisson', 'constant_bursts', 'bursts')):
        row = load_record(f'pattern-{index}', level=4, start=index * 1000)
        suffix = 'c4' if pattern == 'concurrent' else 'only' if pattern == 'bursts' else 'r2'
        identifier = f'load_primary_combo_short_short_{pattern}_{suffix}'
        row.scenario = row.condition_id = identifier
        row.load_point.update(point_id=identifier, pattern=pattern, rate=None if pattern == 'concurrent' else 2)
        rows.append(row)
    target, data = selected(report(rows))
    assert len(target['cohorts']) == 1
    assert data['window']['request_count'] == 5


def test_existing_qualified_results_follow_the_selected_shape_cohort():
    from giraffe.models import CheckResult

    row = load_record('short', level=4, start=0)
    row.load_point.update(point_id='load_primary_mixed_concurrent_c4', type='mixed', workload='mixed')
    groups = [dict(shape=shape, status='pass', correct_work_meeting_limits=count,
                   timing_quality={'status': 'qualified'}, client_mean_inflight=1)
              for shape, count in [('short_short', 1), ('long_short', 100)]]
    value = report([row], config={'targets': [{'name': 't'}], 'load_test': {'pass_criteria': {'max_completion_ms': 1000}}}, checks=[CheckResult(id='load', target='t', title='Load test', summary='Measured', status='pass',
        metrics={'points': [dict(id=row.load_point['point_id'], pattern='constant', rate=4, status='pass',
                                 observation_seconds=1, groups=groups)]})])
    _, data = selected(value)
    assert data['metrics']['goodput']['value'] == 1
    assert [group['shape'] for group in data['metrics']['latency_attainment']['value']] == ['short_short']
    assert [group['shape'] for group in data['metrics']['workload_occupancy']['value']] == ['short_short']
