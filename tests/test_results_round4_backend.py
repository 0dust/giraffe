"""Additive timing evidence and compact measurements, without network sockets."""
import pytest

from giraffe import runner, traffic
from giraffe.models import Limits, RequestRecord, RunConfig, Target
from giraffe.web import _api_report
from tests.test_reporting import make_report


def record(identifier='slow', check='capacity', scenario='traffic_short', elapsed=300, **extra):
    return RequestRecord(id=identifier, target='local', fixture_id='arithmetic', scenario=scenario,
                         check_ids=[check], started_at='now', status='completed', valid=True, score=True,
                         elapsed_ms=elapsed, first_output_ms=5, last_output_ms=elapsed-1,
                         output='42', output_chars=2, output_tokens=10, answer_reads=2,
                         first_read_answer_chars=1, max_stream_gap_ms=20,
                         arrival_elapsed_ms=elapsed, arrival_first_output_ms=5, **extra)


def stage(records, stalls=()):
    return traffic.finalize_stage(traffic.StageEvidence(
        stage_index=0, rate_rps=2, duration_seconds=1, observation_seconds=1,
        schedule=((0, 'short'), (.5, 'short')), outcomes=('started', 'started'),
        records=tuple(enumerate(records)), lifetimes=((0, .3), (.5, .55)), reasons=(),
        peak=1, execution_errors=0, overflow=False, interrupted=False, drain_expired=False,
        case_scorers=None, stalls=stalls), Limits(latency_ms=100, min_samples=2))


@pytest.mark.parametrize('stalls,expected', [((), ['slow']), (((0, .3),), [])])
def test_traffic_violation_ids_are_qualified_without_changing_raw_count(stalls, expected):
    result = stage([record(), record('good', elapsed=50)], stalls)
    assert result['timing_violations'] == 1
    assert result['violations'] == expected
    assert sum(window['timing_violations'] for window in result['windows']) == 1
    assert all(set(window.get('violations', [])).issubset(expected) for window in result['windows'])


@pytest.mark.parametrize('check', ['capacity', 'recovery'])
def test_timing_only_runner_checks_record_failing_request_ids(check, monkeypatch):
    config = RunConfig(targets=[Target(name='local', url='http://localhost/v1', model='test')],
                       checks=[check], samples=2, concurrency=2, max_requests=200,
                       limits=Limits(latency_ms=100, min_samples=2),
                       traffic=dict(rates=[2], duration_seconds=1, max_in_flight=2))
    run = runner._Run(config, None, None)
    if check == 'capacity':
        # Public configs now migrate capacity to load; exercise the retained legacy branch.
        monkeypatch.setattr(runner, 'CORE_CHECKS', ('capacity',))
        monkeypatch.setitem(runner.CHECK_NAMES, 'capacity', 'Arrival traffic capacity')
        run.config = config.model_copy(update={'checks': ['capacity']})
        run.verdict_budget_guidance = 'Use the recorded request budget.'
        run.records = [record(traffic_stage=0), record('good', elapsed=50, traffic_stage=0)]
        run.observations['local']['capacity'] = [stage(run.records)]
    else:
        run.records = [record('slow', check, 'recovery'), record('good', check, 'recovery', 50)]
        run.records += [record(f'baseline-{i}', check, 'recovery_baseline', 50) for i in range(2)]
        run.records += [record(f'sustained-{i}', check, 'sustained', 50) for i in range(2)]
        run.observations['local']['sustained_complete'] = True
    result = next(row for row in runner._checks(run, config.targets[0]) if row.id == check)
    assert result.status == 'fail'
    if check == 'capacity':
        assert 'violations' not in result.metrics
        assert result.metrics['traffic_stages'][0]['violations'] == ['slow']
    else:
        assert result.metrics['violations'] == ['slow']
    assert all(row.score is True and row.valid for row in run.records)


def test_compact_report_keeps_pause_and_generation_measurements():
    report = make_report(count=1)
    report.requests = [record()]
    report.requests[0].max_stream_gap_ms = 6000
    row = _api_report(report)['requests'][0]
    assert row['max_stream_gap_ms'] == 6000
    assert row['arrival_elapsed_ms'] == 300
    assert row['arrival_first_output_ms'] == 5
    assert row['generation_tokens_per_second'] == report.requests[0].generation_tokens_per_second
    assert row['generation_tokens_per_second'] > 0
    assert 'output' not in row and 'evaluation' not in row


def test_recovery_ratio_only_failure_records_the_slow_probe():
    config = RunConfig(targets=[Target(name='local', url='http://localhost/v1', model='test')],
                       checks=['recovery'], samples=2, concurrency=2, max_requests=200,
                       limits=Limits(recovery_max_ratio=2, min_samples=2))
    run = runner._Run(config, None, None)
    run.records = [record('slow', 'recovery', 'recovery'), record('good', 'recovery', 'recovery', 50)]
    run.records += [record(f'baseline-{i}', 'recovery', 'recovery_baseline', 50) for i in range(2)]
    run.records += [record(f'sustained-{i}', 'recovery', 'sustained', 50) for i in range(2)]
    result = next(row for row in runner._checks(run, config.targets[0]) if row.id == 'recovery')
    assert result.status == 'fail' and result.metrics['latency_ratio'] > 2
    assert result.metrics['violations'] == ['slow']
