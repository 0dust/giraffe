"""Scale-down evidence survives report rendering and interrupted-run recovery."""
from copy import deepcopy
import json

from giraffe.evidence import EvidenceWriter, recover_evidence
from giraffe.models import RunReport
from giraffe.reporting import _render, _retained, compare_baseline, reproduction_export
from giraffe.storage import cleanup_plan, write_metadata
from tests.test_scale_down import scenario, evaluate


def report_fixture(*, interrupted=False):
    settings, evidence, records = scenario()
    evidence['marker_received_ms'] = 89
    records[1].output = 'retained answer'
    if interrupted:
        records[1] = records[1].model_copy(update=dict(status='failed', valid=False, stream_terminated=False, error='peer closed'))
    check = evaluate(settings, evidence, records)
    return RunReport(run_id='scale-run', started_at='2026-11-05T00:00:00Z', finished_at='2026-11-05T00:00:10Z',
        overall=check.status, manifest={'config': {'targets':[{'name':'service', 'url':'http://localhost:8000', 'model':'test'}],
        'checks':['scale_down'], 'scale_down':settings.model_dump(mode='json'), 'retention':'none', 'limits':{}}},
        requests=records, checks=[check], observations={'targets':{'service':{'scale_down':evidence}}})


def test_three_results_and_original_failed_request_are_visible_in_html():
    report = report_fixture(interrupted=True)
    html = _render(_retained(report))
    for label in ['Client-visible continuity', 'Backend drain semantics', 'Remaining capacity', 'Event timeline', 'router-boot-1', 'operator']:
        assert label in html
    assert 'peer closed' in html
    assert 'Drain-window requests: pass; post-exit requests: pass.' in html
    assert 'retained answer' not in html
    assert report.checks[0].metrics['results']['continuity']['status'] == 'fail'
    assert report.checks[0].metrics['results']['capacity']['status'] == 'pass'


def test_reproduction_separates_executable_settings_from_historical_event():
    report = report_fixture()
    export = reproduction_export(report)
    assert export['config']['scale_down'] == report.manifest['config']['scale_down']
    assert export['scale_down_history']['service'] == report.observations['targets']['service']['scale_down']
    assert 'nonce' not in json.dumps(export['config'])
    retained = _retained(report)
    assert retained.observations == report.observations
    assert retained.requests[1].output == ''
    assert retained.requests[1].dispatch_ms == report.requests[1].dispatch_ms
    assert retained.requests[1].stream_terminated
    assert RunReport.model_validate_json(retained.model_dump_json()).observations == report.observations
    windows = RunReport.model_validate_json(retained.model_dump_json()).checks[0].metrics['results']['capacity']['metrics']['windows']
    assert windows['drain']['request_ids'] == ['draining']
    assert windows['post_removal']['request_ids'] == ['post']


def test_automatic_comparison_of_distinct_removals_is_unsupported():
    report = report_fixture()
    compared = compare_baseline(report, report.model_copy(update={'run_id':'previous'}))
    assert compared.baseline['status'] == 'inconclusive'
    assert 'external removals' in json.dumps(compared.baseline).lower()
    assert not any(row.get('check') == 'scale_down' for target in compared.baseline['targets'] for row in target.get('metrics', []))


def journal_report(tmp_path, report):
    evidence = deepcopy(report.observations['targets']['service']['scale_down'])
    snapshots = evidence.pop('snapshots')
    writer = EvidenceWriter(tmp_path, report.manifest, report.run_id, report.started_at)
    writer.progress({'event':'scale_down_updated', 'target':'service', 'scale_down':evidence,
                     'scale_down_snapshots':snapshots[:1], 'scale_down_control':{'phase':'armed'}})
    writer.progress({'event':'scale_down_updated', 'target':'service', 'scale_down':evidence,
                     'scale_down_snapshots':snapshots[1:], 'scale_down_control':{'phase':'finalizing'}})
    # The loop may mutate these after handing the writer an event.
    evidence['marker'] = None
    snapshots[0]['export']['source'] = 'mutated'
    for record in report.requests:
        writer.record_finished(record)
    writer.close()
    return recover_evidence(tmp_path, {'evidence_dropped_count':2})


def test_recovery_merges_snapshot_deltas_preserves_marker_and_proven_failure(tmp_path):
    report = report_fixture(interrupted=True)
    recovered = journal_report(tmp_path, report)
    evidence = recovered.observations['targets']['service']['scale_down']
    assert evidence['marker']['event_id'] == 'operator'
    assert evidence['marker_received_ms'] == 89
    assert len(evidence['snapshots']) == 3
    assert evidence['snapshots'][0]['export']['source'] == 'router'
    check = next(c for c in recovered.checks if c.id == 'scale_down')
    assert check.status == 'fail'
    assert check.metrics['results']['continuity']['status'] == 'fail'
    assert check.metrics['results']['capacity']['status'] == 'inconclusive'
    assert recovered.overall == 'fail'
    assert any('recovered' in value.lower() for value in evidence['limitations'])


def test_recovery_never_promotes_incomplete_coverage_to_pass(tmp_path):
    recovered = journal_report(tmp_path, report_fixture())
    assert recovered.overall == 'inconclusive'
    assert {r['status'] for r in recovered.checks[0].metrics['results'].values()} == {'inconclusive'}


def test_cleanup_recognizes_control_artifacts_but_protects_active_run(tmp_path):
    directory = tmp_path / 'scale-run'
    write_metadata(directory, {'id':'scale-run', 'run_id':'scale-run', 'state':'completed', 'started_at':'2026-11-01T00:00:00Z'})
    for name in ['.scale-down-event.json', '.scale-down-status.json', '.scale-down-status.json.abcd.tmp']:
        (directory / name).write_text('{}')
    assert [row['id'] for row in cleanup_plan(tmp_path, older_than_days=0)['candidates']] == ['scale-run']
    assert not cleanup_plan(tmp_path, older_than_days=0, active_id='scale-run')['candidates']


def test_recovery_without_scenario_progress_has_no_stale_passing_subresults(tmp_path):
    report = report_fixture()
    writer = EvidenceWriter(tmp_path, report.manifest)
    writer.progress({'event':'check_finished', 'check':report.checks[0].model_dump(mode='json')})
    writer.close()
    recovered = recover_evidence(tmp_path, {})
    assert recovered.overall == 'inconclusive'
    assert {r['status'] for r in recovered.checks[0].metrics['results'].values()} == {'inconclusive'}


def test_recovery_control_only_marker_and_invalid_json_types_are_safe(tmp_path):
    report = report_fixture(interrupted=True)
    journal_report(tmp_path, report)
    path = tmp_path / 'evidence.jsonl'
    evidence = report.observations['targets']['service']['scale_down']
    marker = evidence['marker']
    with path.open('a') as stream:
        for wrong in [None, 'bad', 1, {'limitations':None}, {'limitations':'bad'}, {'answer_started_ms':[]}]:
            stream.write(json.dumps({'type':'progress','progress':{'event':'scale_down_updated','target':'service',
                'scale_down':wrong,'scale_down_snapshots':None,'scale_down_control':[]}})+'\n')
        stream.write(json.dumps({'type':'progress','progress':{'event':'scale_down_updated','target':'service',
            'scale_down':{}, 'scale_down_control':{'acknowledgment':{'accepted':True,'marker':marker,'received_ms':89,'accepted_ms':90}}}})+'\n')
        stream.write(json.dumps({'type':'progress','progress':{'event':'scale_down_updated','target':'service',
            'scale_down':{'marker':None}, 'scale_down_control':None}})+'\n')
    recovered = recover_evidence(tmp_path, {})
    assert recovered.overall == 'fail'
    assert recovered.observations['targets']['service']['scale_down']['marker'] == {**marker, 'operator_at':None}
    assert any('Invalid recovered' in value for value in recovered.observations['targets']['service']['scale_down']['limitations'])
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'progress','progress':{'event':'scale_down_updated','target':'service','scale_down':{'limitations':None}}})+'\n')
    assert recover_evidence(tmp_path, {}).overall == 'fail'


def test_recovery_preserves_accepted_control_marker_without_scenario_metadata(tmp_path):
    report = report_fixture()
    writer = EvidenceWriter(tmp_path, report.manifest)
    marker = report.observations['targets']['service']['scale_down']['marker']
    writer.progress({'event':'scale_down_updated','target':'service','scale_down':{},'scale_down_snapshots':[],
        'scale_down_control':{'phase':'observing','acknowledgment':{'accepted':True,'marker':marker,'received_ms':89,'accepted_ms':90}}})
    writer.close()
    recovered = recover_evidence(tmp_path, {})
    evidence = recovered.observations['targets']['service']['scale_down']
    assert evidence['marker']['event_id'] == marker['event_id']
    assert evidence['marker_received_ms'] == 89 and evidence['marker_accepted_ms'] == 90
    assert recovered.overall == 'inconclusive'


def test_recovery_snapshot_deltas_are_bounded_and_keep_incomplete_coverage_explicit(tmp_path):
    report = report_fixture()
    snapshot = report.observations['targets']['service']['scale_down']['snapshots'][0]
    from giraffe.evidence import _recover_scale_down_event
    recovered = {}
    _recover_scale_down_event(recovered, {'target':'service','scale_down':{},'scale_down_snapshots':[snapshot] * 4100})
    assert len(recovered['service']['snapshots']) == 4096
    assert any('exceeded' in value for value in recovered['service']['limitations'])


def test_recovery_respects_opt_in_and_single_selected_target(tmp_path):
    report = report_fixture()
    report.manifest['config']['targets'].append({'name':'other','url':'http://localhost:8001','model':'test'})
    writer = EvidenceWriter(tmp_path / 'selected', report.manifest)
    writer.close()
    recovered = recover_evidence(tmp_path / 'selected', {})
    assert [(check.id, check.target) for check in recovered.checks] == [('scale_down','service')]
    report.manifest['config']['checks'] = ['access']
    writer = EvidenceWriter(tmp_path / 'draft', report.manifest)
    writer.close()
    recovered = recover_evidence(tmp_path / 'draft', {})
    assert all(check.id == 'access' for check in recovered.checks)
    assert all('scale_down' not in value for value in recovered.observations['targets'].values())
