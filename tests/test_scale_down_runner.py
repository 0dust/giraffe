"""Runner ownership and finite externally controlled traffic."""
import asyncio
import time

import pytest

from giraffe.models import RunConfig, required_concurrency
from giraffe.scale_down import ScaleDownController


def test_controller_acceptance_is_bound_idempotent_and_copied():
    control = ScaleDownController('run', 'service', run_start=time.monotonic(), stop=asyncio.Event())
    marker = dict(event_id='operator', nonce=control.status()['nonce'], target='service', source='test')
    assert not control.accept_marker(marker)['accepted']
    control.set_phase('armed')
    assert not control.accept_marker({**marker, 'nonce': 'old'})['accepted']
    accepted = control.accept_marker(marker)
    assert accepted['accepted'] and accepted['accepted_ms'] >= accepted['received_ms']
    accepted['marker']['source'] = 'mutated'
    assert control.accept_marker(marker)['marker']['source'] == 'test'
    assert not control.accept_marker({**marker, 'source': 'different'})['accepted']
    status = control.status()
    status['acknowledgment']['marker']['source'] = 'mutated'
    assert control.status()['acknowledgment']['marker']['source'] == 'test'
    control.set_phase('finished')
    assert not control.accept_marker(marker)['accepted']


def test_controller_stop_rejects_without_consuming_marker():
    stop = asyncio.Event()
    control = ScaleDownController('run', 'service', run_start=time.monotonic(), stop=stop)
    control.set_phase('armed')
    stop.set()
    assert not control.accept_marker(dict(event_id='e', nonce=control.nonce, target='service', source='test'))['accepted']
    assert control.status()['acknowledgment'] is None


def config(**changes):
    return RunConfig(targets=[dict(name='service', url='http://localhost:1234/v1', model='model')],
                     checks=['scale_down'], scale_down=dict(target='service'), **changes)


def test_selected_scenario_registers_concurrency():
    assert required_concurrency(config())['peak'] == 4


@pytest.mark.parametrize('changes', [dict(stream=False), dict(concurrency=2)])
def test_selected_scenario_rejects_missing_stream_or_reserved_slot(changes):
    with pytest.raises(ValueError, match='scale_down'):
        config(**changes)


class ObservedClient:
    def __init__(self, target, *, answer=True, long_seconds=.15):
        self.target = target
        self.answer = answer
        self.long_seconds = long_seconds
        self.active_long = 0
        self.followups_while_long = 0

    async def execute(self, spec, *, first_output_event=None, lifecycle_observer=None):
        import uuid
        from giraffe.client import RequestLifecycleEvent
        from giraffe.models import RequestRecord
        identifier = uuid.uuid4().hex
        started = time.monotonic()
        is_long = spec.scenario == 'scale_down_long'
        def emit(kind):
            if lifecycle_observer:
                lifecycle_observer(RequestLifecycleEvent(kind=kind, request_id=identifier,
                    target=self.target.name, monotonic_seconds=time.monotonic(), stream=True))
        emit('started')
        if is_long:
            self.active_long += 1
        elif spec.scenario == 'scale_down_followup' and self.active_long:
            self.followups_while_long += 1
        status = 'completed'
        try:
            await asyncio.sleep(.001)
            if self.answer or not is_long:
                emit('answer')
            await asyncio.sleep(self.long_seconds if is_long else .001)
        except asyncio.CancelledError:
            status = 'cancelled'
        finally:
            if is_long:
                self.active_long -= 1
            emit('terminal')
        return RequestRecord(id=identifier, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at='2026-11-05T00:00:00Z',
            status=status, valid=status == 'completed', output='ok', score=True,
            elapsed_ms=(time.monotonic()-started)*1000, first_output_ms=1,
            answer_read_timestamps_ms=[1], answer_chunks=1, answer_chars=2,
            finish_reason='stop', stream_terminated=status == 'completed', stream=True)


def make_run(*, answer=True, long_seconds=.15, global_limits=None):
    from giraffe.models import RequestSpec
    from giraffe.runner import _Run
    settings = dict(target='service', long_fixture='long', followup_fixture='short',
                    long_cohort_size=1, concurrency=2, min_baseline_samples=1,
                    min_overlap_samples=1, min_post_samples=1, requests_per_second=100,
                    baseline_timeout_seconds=.04, event_timeout_seconds=.05,
                    grace_seconds=.03, post_seconds=.02, completion_seconds=.02)
    cfg = RunConfig(targets=[dict(name='service', url='http://localhost:1234/v1', model='model')],
        checks=['scale_down'], scale_down=settings, max_memory_mb=0, **(global_limits or {}))
    events = []
    run = _Run(cfg, events.append, asyncio.Event())
    run.run_id = 'run'
    run.scale_down_control = ScaleDownController('run', 'service', run_start=run.start, stop=run.stop)
    target = cfg.targets[0]
    fixtures = {'custom': [RequestSpec(fixture_id=identifier, scenario='custom', check_ids=[],
                messages=[dict(role='user', content='say ok')], scorer='exact', expected='ok')
                for identifier in ('short', 'long')]}
    return run, target, ObservedClient(target, answer=answer, long_seconds=long_seconds), fixtures, events


@pytest.mark.asyncio
async def test_active_long_answer_and_followups_overlap_until_finite_missing_event_exit():
    from giraffe.scale_down import execute, result
    run, target, client, fixtures, events = make_run()
    await execute(run, target, client, fixtures)
    evidence = run.observations[target.name]['scale_down']
    assert evidence['armed_ms'] is not None
    assert evidence['marker'] is None
    assert client.followups_while_long >= 2
    assert any(record.status == 'cancelled' for record in run.records)
    assert len(run.records) == run.attempted
    assert run.slots.qsize() == run.slots.maxsize
    assert run.scale_down_control.status()['phase'] == 'finished'
    assert result(run, target).status == 'inconclusive'
    assert any('event deadline' in item for item in evidence['limitations'])
    assert not any(not task.done() for task in asyncio.all_tasks() if task is not asyncio.current_task())


@pytest.mark.asyncio
@pytest.mark.parametrize('answer,long_seconds', [(False, .15), (True, 0)])
async def test_no_active_real_answer_cannot_arm(answer, long_seconds):
    from giraffe.scale_down import execute
    run, target, client, fixtures, _ = make_run(answer=answer, long_seconds=long_seconds)
    await execute(run, target, client, fixtures)
    assert run.observations[target.name]['scale_down']['armed_ms'] is None
    assert run.slots.qsize() == run.slots.maxsize


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['requests', 'duration', 'stop', 'memory'])
async def test_global_limits_join_tasks_and_retain_records(limit):
    from giraffe.scale_down import execute
    limits = {'max_requests': 4} if limit == 'requests' else {'max_duration_seconds': .025} if limit == 'duration' else {}
    run, target, client, fixtures, _ = make_run(global_limits=limits)
    async def interrupt():
        await asyncio.sleep(.025)
        if limit == 'stop':
            run.stop.set()
        elif limit == 'memory':
            run.config.max_memory_mb = 1
            run.rss_at_start_bytes = 100
            run.memory_sample(3*1024**2)
    interruption = asyncio.create_task(interrupt())
    await execute(run, target, client, fixtures)
    await interruption
    assert client.active_long == 0
    assert run.slots.qsize() == run.slots.maxsize
    assert len(run.records) == run.attempted
    assert run.budget_used <= limits.get('max_requests', 1000)
    assert run.observations[target.name]['scale_down']['stop_reason']


def test_fixture_resolution_rejects_missing_cancelled_or_nonstreaming():
    from giraffe.scale_down import resolve_fixtures
    from giraffe.models import RequestSpec, ScaleDownConfig
    settings = ScaleDownConfig(target='service', long_fixture='f', followup_fixture='f')
    with pytest.raises(ValueError, match='not found'):
        resolve_fixtures(settings, {})
    spec = RequestSpec(fixture_id='f', scenario='x', check_ids=[], messages=[])
    for changes in (dict(stream=False), dict(cancel_after_chunks=1), dict(options={'tools': [{}]})):
        with pytest.raises(ValueError, match='uninterrupted text stream'):
            resolve_fixtures(settings, {'custom': [spec.model_copy(update=changes)]})


@pytest.mark.asyncio
async def test_marker_and_confirmed_removal_keep_one_followup_stage_through_post_window():
    from giraffe.models import ScaleDownSnapshot
    from giraffe.scale_down import execute
    run, target, client, fixtures, events = make_run(long_seconds=.035)
    control = run.scale_down_control
    def capture(sequence, terminated=False):
        now = (time.monotonic()-run.start)*1000
        return ScaleDownSnapshot(collected_start_ms=now, collected_end_ms=now,
            capture_start_ms=now, capture_end_ms=now,
            export=dict(target=target.name, source='test-backend', clock_id='test-clock', through_sequence=sequence,
                inventories=[dict(event_id=f'inventory-{sequence}', sequence=sequence, time_ms=now,
                                  replicas=['a'] if terminated else ['a', 'b'])],
                events=[dict(event_id='terminated', sequence=2, time_ms=now,
                             kind='replica_terminated', replica_id='b')] if terminated else [])).model_dump(mode='json')
    async def operator():
        while control.phase != 'armed':
            await asyncio.sleep(.001)
        run.observations[target.name]['scale_down_deployment_history'] = [dict(scale_down_lifecycle=capture(1))]
        assert control.accept_marker(dict(event_id='event', nonce=control.nonce, target=target.name,
                                          replica_id='b', source='test'))['accepted']
        await asyncio.sleep(.004)
        run.observations[target.name]['scale_down_deployment_history'].append(dict(scale_down_lifecycle=capture(3, True)))
    operator_task = asyncio.create_task(operator())
    await execute(run, target, client, fixtures)
    await operator_task
    evidence = run.observations[target.name]['scale_down']
    assert evidence['marker_received_ms'] <= evidence['marker_accepted_ms']
    assert evidence['post_ended_ms']-evidence['post_started_ms'] >= run.config.scale_down.post_seconds*1000
    assert any(r.dispatch_ms > evidence['post_started_ms'] for r in run.records)
    assert client.followups_while_long >= 2
    assert sum(row['kind'] == 'arrival_rate' for row in run.observations[target.name]['schedule_transitions']) == 1
    assert run.slots.qsize() == run.slots.maxsize
    updates = [event for event in events if event['event'] == 'scale_down_updated']
    assert all('snapshots' not in event['scale_down'] for event in updates)
    assert sum(len(event['scale_down_snapshots']) for event in updates) == len(evidence['snapshots']) == 2


@pytest.mark.asyncio
async def test_wrong_marker_does_not_end_event_wait_or_arm_next_run():
    from giraffe.scale_down import execute
    run, target, client, fixtures, _ = make_run()
    async def operator():
        while run.scale_down_control.phase != 'armed':
            await asyncio.sleep(.001)
        ack = run.scale_down_control.accept_marker(dict(event_id='wrong', nonce='other-run',
                                                        target=target.name, source='test'))
        assert not ack['accepted']
    operator_task = asyncio.create_task(operator())
    await execute(run, target, client, fixtures)
    await operator_task
    assert run.observations[target.name]['scale_down']['marker'] is None
    assert client.followups_while_long >= 2
    assert run.slots.qsize() == run.slots.maxsize


@pytest.mark.asyncio
async def test_late_record_scoring_recomputes_the_scale_down_verdict():
    from giraffe.models import ScaleDownEvidence
    from giraffe.scale_down import result
    from tests.test_scale_down import scenario
    run, target, _, _, _ = make_run()
    settings, evidence, records = scenario()
    run.config.scale_down = settings
    run.observations[target.name]['scale_down'] = ScaleDownEvidence.model_validate(evidence).model_dump(mode='json')
    run.records = records
    assert result(run, target).status == 'pass'
    run.records[1].score = False
    assert result(run, target).metrics['results']['continuity']['status'] == 'fail'


@pytest.mark.asyncio
async def test_suite_loads_custom_fixtures_for_selected_target_and_binds_control(monkeypatch, tmp_path):
    import json
    from giraffe import runner, telemetry
    run, target, _, fixtures, _ = make_run()
    path = tmp_path / 'fixtures.json'
    path.write_text(json.dumps([spec.model_dump(by_alias=True) | {'check_ids': ['scale_down']}
                               for spec in fixtures['custom']]))
    cfg = run.config.model_copy(update={'custom_fixtures': str(path),
        'targets': [target, target.model_copy(update={'name': 'other'})]})
    class Client(ObservedClient):
        def __init__(self, target, config, **kwargs):
            super().__init__(target, long_seconds=.025)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
    async def no_metrics(*args, **kwargs):
        return {'status': 'unavailable', 'samples': [], 'unavailable': []}
    monkeypatch.setattr(runner, 'LLMClient', Client)
    monkeypatch.setattr(runner, '_metrics', no_metrics)
    monkeypatch.setattr(telemetry, 'scrape', no_metrics)
    controls = []
    report = await runner.run_suite(cfg, on_scale_down_control=controls.append)
    assert len(controls) == 1 and controls[0].phase == 'finished'
    assert report.requests and {record.target for record in report.requests} == {'service'}
    assert {record.fixture_id for record in report.requests} == {'short', 'long'}
    checks = {check.target: check for check in report.checks if check.id == 'scale_down'}
    assert checks['service'].status == 'inconclusive' and checks['other'].status == 'skipped'
    assert not controls[0].accept_marker(dict(event_id='late', nonce=controls[0].nonce,
                                             target='service', source='test'))['accepted']


@pytest.mark.asyncio
async def test_control_closes_when_fixture_preparation_fails(monkeypatch):
    from giraffe import runner
    run, _, _, _, _ = make_run()
    controls = []
    with pytest.raises(ValueError, match='was not found'):
        await runner.run_suite(run.config, on_scale_down_control=controls.append)
    assert len(controls) == 1 and controls[0].phase == 'finished'


@pytest.mark.asyncio
async def test_ordinary_suite_does_not_add_scale_down_result_rows(monkeypatch):
    from giraffe import runner, telemetry
    from tests.test_runner import FakeClient
    async def no_metrics(*args, **kwargs):
        return {'status': 'unavailable', 'samples': [], 'unavailable': []}
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    monkeypatch.setattr(runner, '_metrics', no_metrics)
    monkeypatch.setattr(telemetry, 'scrape', no_metrics)
    cfg = RunConfig(targets=[dict(name='service', url='http://localhost/v1', model='model')],
                    checks=['access'], max_memory_mb=0)
    report = await runner.run_suite(cfg)
    assert report.requests
    assert all(check.id != 'scale_down' for check in report.checks)


@pytest.mark.asyncio
@pytest.mark.parametrize('later_change', ['limitation', 'another_removal'])
async def test_finished_scenario_ignores_later_collector_observations(later_change):
    from copy import deepcopy
    from giraffe.models import ScaleDownEvidence
    from giraffe.scale_down import result
    from tests.test_scale_down import scenario
    run, target, _, _, _ = make_run()
    settings, evidence, records = scenario()
    run.config.scale_down = settings
    run.records = records
    observation = run.observations[target.name]
    observation['scale_down'] = ScaleDownEvidence.model_validate(evidence).model_dump(mode='json')
    run.scale_down_control.set_phase('finished')
    original = result(run, target)
    assert original.status == 'pass'
    if later_change == 'limitation':
        later = {'scale_down_limitations': ['Collector timed out during a later workload.']}
    else:
        snapshot = deepcopy(evidence['snapshots'][-1])
        snapshot.update(collected_start_ms=1500, collected_end_ms=1510,
                        capture_start_ms=1500, capture_end_ms=1510)
        snapshot['export']['through_sequence'] = 8
        snapshot['export']['events'].append(dict(event_id='another-termination', sequence=8,
            time_ms=1500, kind='replica_terminated', replica_id='b'))
        later = {'scale_down_lifecycle': snapshot}
    observation.setdefault('scale_down_deployment_history', []).append(later)
    assert result(run, target) == original
    assert observation['scale_down'] == ScaleDownEvidence.model_validate(evidence).model_dump(mode='json')
    # Scoring still operates on the final records, after source evidence freezes.
    records[1].score = False
    assert result(run, target).metrics['results']['continuity']['status'] == 'fail'


@pytest.mark.asyncio
async def test_unstarted_scenario_cannot_adopt_later_collector_history():
    from giraffe.scale_down import result
    from tests.test_scale_down import scenario
    run, target, _, _, _ = make_run()
    run.scale_down_control.set_phase('finished')
    run.observations[target.name]['scale_down_deployment_history'] = [
        {'scale_down_lifecycle': snapshot} for snapshot in scenario()[1]['snapshots']]
    assert result(run, target).status == 'inconclusive'
    evidence = run.observations[target.name]['scale_down']
    assert evidence['snapshots'] == []
    assert 'Scale-down traffic did not start.' in evidence['limitations']


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['no_collector', 'sample_cap', 'finished_sampler', 'failed_sampler'])
async def test_final_observation_does_not_wait_for_unavailable_collector(unavailable):
    from giraffe.scale_down import execute
    run, target, client, fixtures, _ = make_run(long_seconds=.015)
    run.config.scale_down.completion_seconds = .5
    if unavailable != 'no_collector':
        target.deployment.collector_url = 'http://localhost/collector'
    if unavailable == 'sample_cap':
        run.observations[target.name]['serving_telemetry']['sample_limit_reached'] = True
    async def collector():
        if unavailable == 'failed_sampler':
            raise RuntimeError('collector stopped')
        if unavailable != 'finished_sampler':
            await asyncio.Event().wait()
    collector_task = asyncio.create_task(collector())
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(execute(run, target, client, fixtures, collector_task=collector_task), timeout=.2)
        assert run.scale_down_control.phase == 'finished'
    finally:
        collector_task.cancel()
        await asyncio.gather(collector_task, return_exceptions=True)


@pytest.mark.parametrize('stop_at', ['completion_deadline', 'run_deadline', 'scheduler_resume', 'sample_cap', 'user_stop'])
def test_stale_final_polls_keep_existing_cleanup_boundaries(monkeypatch, stop_at):
    from giraffe import scale_down
    from giraffe.models import ScaleDownSnapshot
    from tests.virtual_clock import run_virtual

    async def scenario():
        monkeypatch.setattr('tests.test_scale_down_runner.time', scale_down.time)
        limits = {'max_duration_seconds': .3} if stop_at == 'run_deadline' else {}
        run, target, client, fixtures, _ = make_run(global_limits=limits)
        run.config.scale_down.completion_seconds = .6
        run.config.metrics_max_samples = 16 if stop_at == 'sample_cap' else 256
        target.deployment.collector_url = 'http://localhost/collector'
        history = run.observations[target.name]['scale_down_deployment_history'] = []
        phases, boundary = {}, {}
        original_phase = scale_down.ScaleDownController.set_phase
        loop = asyncio.get_running_loop()

        def observed_phase(control, phase):
            phases[phase] = loop.time()
            if phase == 'finalizing':
                if stop_at == 'user_stop':
                    boundary['expected'] = loop.time() + .3
                    loop.call_at(boundary['expected'], run.stop.set)
                elif stop_at == 'scheduler_resume':
                    boundary['expected'] = loop.time() + .58 + .337
                    def scheduler_pause():
                        loop.now += .337
                    loop.call_at(loop.time() + .58, scheduler_pause)
            return original_phase(control, phase)

        monkeypatch.setattr(scale_down.ScaleDownController, 'set_phase', observed_phase)

        async def collector():
            while run.scale_down_control.phase != 'finalizing':
                await asyncio.sleep(.001)
            if stop_at == 'sample_cap':
                boundary['expected'] = loop.time() + .02 * run.config.metrics_max_samples
            # Keep delivering old captures after clients settle. Neither their
            # arrival nor their repetition can reset the existing finish time.
            while True:
                await asyncio.sleep(.02)
                history.append({'scale_down_lifecycle': ScaleDownSnapshot(
                    collected_start_ms=0, collected_end_ms=(loop.time()-run.start)*1000,
                    capture_start_ms=0, capture_end_ms=0,
                    export=dict(target=target.name, source='test-backend', clock_id='test-clock',
                                through_sequence=1, events=[], inventories=[])).model_dump(mode='json')})
                if len(history) == run.config.metrics_max_samples:
                    boundary['expected'] = loop.time()
                    await asyncio.Event().wait()

        sampler = asyncio.create_task(collector())
        try:
            await asyncio.wait_for(scale_down.execute(run, target, client, fixtures,
                                                     collector_task=sampler), timeout=2)
            evidence = run.observations[target.name]['scale_down']
            assert all(snapshot['collected_start_ms'] == 0 for snapshot in evidence['snapshots'])
            assert scale_down.result(run, target).status == 'inconclusive'
            assert run.slots.qsize() == run.slots.maxsize
            if stop_at in {'completion_deadline', 'run_deadline'}:
                boundary['expected'] = min(run.deadline, phases['finalizing'] + .6)
            return dict(stop_at=stop_at, started=phases['finalizing'], finished=phases['finished'],
                        expected=boundary['expected'], snapshots=len(history),
                        sample_limit=run.config.metrics_max_samples,
                        client_completed=max(record.completed_ms for record in run.records)/1000)
        finally:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)

    observed = run_virtual(monkeypatch, scenario())
    assert observed['client_completed'] < observed['expected'], observed
    assert observed['snapshots'] > 2, observed
    assert observed['expected'] <= observed['finished'] < observed['expected'] + .01 + 1e-9, observed
    assert observed['snapshots'] <= observed['sample_limit'], observed
    if stop_at == 'sample_cap':
        assert observed['snapshots'] == observed['sample_limit'], observed
