import asyncio
import random
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from giraffe import load_test as load
from giraffe.fixtures import score_response
from giraffe.models import RequestRecord
from giraffe.planning import request_plan
from giraffe.runner import _Run
from tests.test_concurrent_load import Client, configuration, perform, point, short_windows


def record(name='r', role='short_stream', sub=0, start=0, first=10, last=30, chunks=2, **updates):
    values = dict(id=name, target='local', fixture_id='x', scenario='x', check_ids=['load'], started_at='now',
        status='completed', valid=True, output='K7P4', score=True, first_output_ms=first, last_output_ms=last,
        elapsed_ms=last+5, stream_end_ms=last+5, stream=True, answer_chunks=chunks, timing_start_ms=start,
        dispatch_ms=start, completed_ms=start+last+5, max_stream_gap_ms=2, input_tokens=64, completion_tokens=8,
        load_point={'role': role, 'sub_window': sub, 'shape': 'short_short'})
    values.update(updates)
    return RequestRecord(**values)


@pytest.mark.parametrize('answer', ['K7P4', '**K7P4**', '`K7P4`', '"K7P4"', 'Label: K7P4',
    'box label: **K7P4**', '**K7P4**\n1. First item', 'K7P4.\n1. First item'])
def test_label_first_accepts_builtin_exact_leniency(answer):
    spec = load.make_spec(configuration().load_test, 'short_short', 'load_combo_short_short_c1', {}, 0)
    assert score_response(spec, record(output=answer)).score is True


@pytest.mark.parametrize('answer', ['wrong', 'Here is K7P4', 'WRONG K7P4', 'Label: WRONG K7P4',
    'K7P4wrong', '**WRONG** K7P4', '**`K7P4`**'])
def test_label_first_still_rejects_wrong_or_late_labels(answer):
    spec = load.make_spec(configuration().load_test, 'short_short', 'load_combo_short_short_c1', {}, 0)
    assert score_response(spec, record(output=answer)).score is False


async def test_blank_calibration_output_uses_received_prompt_usage():
    class Blank(Client):
        async def execute(self, spec, **kw):
            r = await super().execute(spec, **kw)
            if spec.scenario.startswith('load_calibration_'):
                r.status, r.valid, r.output, r.error, r.http_status = 'failed', False, '', 'response contained no useful answer output', 200
            return r
    run, check, _, _ = await perform(configuration(checks=["access", "serving", "concurrency"], stop_after_errors=1), Blank())
    assert run.errors == 0
    assert all(r.check_ids == ["load"] for r in run.records)
    assert check.metrics['calibrations'][0]['status'] == 'calibrated'
    assert point(check)['dispatched'] > 0
    assert check.status == 'pass'


async def test_below_minimum_prompt_length_calibrates_short_and_warns_in_plan():
    c = configuration(load={'profile': {'short': {'input_tokens': 16, 'output_tokens': 8},
        'medium': {'input_tokens': 96, 'output_tokens': 16}, 'long': {'input_tokens': 128, 'output_tokens': 32}}})
    class Floor(Client):
        async def execute(self, spec, **kw):
            r = await super().execute(spec, **kw)
            r.input_tokens = 52
            return r
    _, check, _, _ = await perform(c, Floor())
    calibration = check.metrics['calibrations'][0]
    assert calibration['status'] == 'calibrated' and calibration['input_tokens'] == 52
    assert calibration['filler_chars'] == 0
    assert calibration['reason'] == 'Short input target 16 is below the minimum prompt length (≈52 tokens); measured at 52'
    assert point(check)['dispatched'] > 0 and check.status == 'pass'
    from giraffe.reporting import _load_test_html
    assert calibration['reason'] in _load_test_html(check.metrics)
    warnings = request_plan(configuration(load={'profile': c.load_test.profile.model_dump()}))['load_warnings']
    assert any('minimum prompt length' in w['msg'] and w['fields'] == ['load_test.profile.short.input_tokens'] for w in warnings)


async def test_derived_mixed_seed_roundtrips_through_javascript_integer_range():
    _, check, _, _ = await perform(configuration(load={'mixed': {'enabled': True}}))
    seed = check.metrics['seed']
    assert 0 <= seed <= 2**53-1 and int(float(seed)) == seed


def scored_point():
    c = configuration()
    run = _Run(c, None, None)
    p = load.test_points(c.load_test)[0]
    p.update(sending_start_ms=0, sending_end_ms=500, configured_window_seconds=1,
        delivered_window_seconds=.5, observation_seconds=.5, drain_seconds=0, cut_short=True, not_run=False,
        window_reason='point request allowance used')
    load.assess_point(run, c.targets[0], c.load_test, p, [record('a'),record('b', start=50)])
    return c,p


@pytest.mark.parametrize('reason,code', [('point request allowance used', 'budget_limited'),
    ('request budget reached', 'budget_limited'), ('maximum request budget reached', 'budget_limited'),
    ('time limit reached', 'time_limited'), ('maximum run duration reached', 'time_limited'),
    ('run stopped', 'run_stopped'), ('error threshold reached', 'run_stopped')])
@pytest.mark.parametrize('enough', [False, True])
def test_window_reason_codes_and_remedies_are_cause_specific(reason, code, enough):
    c,p = scored_point()
    p['window_reason'] = reason
    g = p['groups'][0]
    g['valid'] = 100 if enough else 0
    load.score_group(c.load_test,p,g,[])
    assert g['reason_code'] == code
    # Issue 45: every truncated point remains inconclusive even with samples.
    assert g['status'] == 'inconclusive'
    assert reason in g['reason']


def test_failed_reasoning_exhaustion_is_not_an_error():
    c,p = scored_point(); p['cut_short'] = False
    run = _Run(c,None,None)
    r = record(status='failed',valid=False,score=None,output='',reasoning='thinking',finish_reason='length',error='response contained no useful answer output')
    load.assess_point(run,c.targets[0],c.load_test,p,[r])
    g = p['groups'][0]
    assert g['reasoning_budget_exhausted'] == 1 and g['failed'] == g['error_percent'] == g['valid'] == 0
    assert g['status'] == 'inconclusive'


def test_request_warning_reserves_later_consistency_budget():
    c = configuration(max_requests=16,checks=['load','consistency'], consistency={'repetitions':2,'levels':[1],'shapes':['short'],'prompts_per_shape':2},load={'schedule':{'duration_seconds':10}})
    assert load.later_reservation(c) == 4
    plan = request_plan(c); run = _Run(c,None,None)
    assert run.quotas == plan['target_quotas'] == {'local':16}
    assert run.quotas['local'] - load.later_reservation(c) - len(load.calibration_keys(c.load_test)) == 11
    warning, = plan['request_warnings']
    assert warning['kind'] == 'adaptive_risk' and warning['fields'] == ['max_requests']
    assert plan['known_per_target'] == 4


@pytest.mark.parametrize('buffered,total,expected', [(1, 4, False), (2, 4, False), (3, 4, True)])
def test_buffering_requires_majority_and_is_counted(buffered,total,expected):
    c,p = scored_point(); p.update(type='interference',pattern='interference',level=2,shape=None)
    records = [record(str(i),chunks=1 if i < buffered else 2,load_point={'role':'short_stream','phase':'probe'}) for i in range(total)]
    windows=[{'phase':'baseline','sending_start_ms':0},{'phase':'probe','sending_start_ms':0,'triggered':1}]
    data=load.interference_measurements(records,windows)
    assert data['buffered_stream'] is expected
    assert data['buffered_short_streams'] == buffered and data['valid_short_streams'] == total


def test_cross_boundary_short_streams_are_excluded_from_both_probe_groups():
    short=record('ambiguous',first=10,last=60,load_point={'role':'short_stream','phase':'probe'})
    probe=record('probe','probe',start=61,first=20,last=40,load_point={'role':'probe','phase':'probe'})
    windows=[{'phase':'baseline','sending_start_ms':0},{'phase':'probe','sending_start_ms':0,'triggered':1}]
    data=load.interference_measurements([short,probe],windows)
    assert data['baseline']['matched_count'] == data['probe']['matched_count'] == 0


async def test_interference_qualifies_short_streams_without_probe_trigger_idle_time():
    run,check,_,_=await perform(configuration(load={'combinations':{'enabled':False},'interference':{'enabled':True},'schedule':{'levels':[2]}}))
    p=point(check)
    assert len(p['interference']['short_stream_load']) == 2
    assert all(w['mean_inflight'] >= .9 and w['evidenced_concurrency'] == 1 for w in p['interference']['short_stream_load'])
    assert p['interference']['baseline']['matched_count'] and p['interference']['probe']['matched_count']
    assert p['status'] == 'pass'


async def test_interference_role_scenarios_keep_baseline_groups_comparable():
    run,check,_,_=await perform(configuration(load={'combinations':{'enabled':False},'interference':{'enabled':True},'schedule':{'levels':[2]}}))
    p=point(check)
    for r in run.records:
        if r.load_point.get('type') != 'interference': continue
        assert r.scenario == p['id']+'.'+r.load_point['phase']
        assert r.condition_id == r.scenario
    assert {g['shape'] for g in p['groups']} == {'short_short','long_short'}


async def test_waiting_for_shared_slots_ends_with_point_window(monkeypatch):
    c = short_windows(configuration(load={'schedule': {'levels': [2]}}), .03)
    run = _Run(c, None, None)
    admitted = 0
    @asynccontextmanager
    async def shared_slot(reserved, deadline=None):
        nonlocal admitted
        if reserved:
            yield True
            return
        admitted += 1
        if admitted == 1:
            yield True
            return
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.Event().wait()
        except TimeoutError:
            yield False
    request = run.request
    async def delayed_dispatch(*args, **kw):
        await asyncio.sleep(0)
        return await request(*args, **kw)
    monkeypatch.setattr(run, 'request', delayed_dispatch)
    monkeypatch.setattr(run, 'slot', shared_slot)
    p = load.test_points(c.load_test)[0]
    calibration = {('short', 'short_answer'): {'filler_chars': 64}}
    before = time.monotonic()
    try:
        await asyncio.wait_for(load.concurrent_window(run, c.targets[0], Client(), c.load_test, p, calibration), .15)
    except TimeoutError:
        pytest.fail('slot waits outlived the point sending window')
    assert time.monotonic() - before < .1
    assert p['drain_seconds'] < .03


@pytest.mark.parametrize('other_aborted', [False, True])
async def test_unexpected_executor_cancellation_is_propagated(other_aborted):
    class Paused(Client):
        def __init__(self):
            super().__init__()
            self.sent = asyncio.Event()
        async def execute(self, spec, **kw):
            if not spec.scenario.startswith('load_calibration_'):
                self.sent.set()
                await asyncio.Event().wait()
            return await super().execute(spec, **kw)
    targets = [{'name': 'local', 'url': 'http://localhost:1/v1', 'model': 'test'}, {'name': 'other', 'url': 'http://localhost:2/v1', 'model': 'test'}]
    c = short_windows(configuration(targets=targets))
    run, client = _Run(c, None, None), Paused()
    if other_aborted:
        run.endpoint_health['other']['abort_reason'] = 'other target aborted'
    task = asyncio.create_task(load.execute(run, c.targets[0], client))
    await asyncio.wait_for(client.sent.wait(), .5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .5)
    assert len([r for r in run.records if r.load_point and r.load_point.get('type') != 'calibration']) == 1


def test_large_input_stress_still_requires_target_after_minimum_floor():
    c=configuration(load={'profile':{'short':{'input_tokens':16,'output_tokens':8},'medium':{'input_tokens':32,'output_tokens':16},'long':{'input_tokens':48,'output_tokens':32}},'combinations':{'enabled':False},'stress':{'enabled':True,'large_output':False}})
    run=_Run(c,None,None); p=load.test_points(c.load_test)[0]
    p.update(sending_start_ms=0,sending_end_ms=200,delivered_window_seconds=.2,observation_seconds=.2)
    rows=[record(str(i),input_tokens=60,load_point={'shape':'long_short'}) for i in range(2)]
    load.assess_point(run,c.targets[0],c.load_test,p,rows)
    assert p['reason_code'] == 'input_not_verified'


@pytest.mark.parametrize('status,http,tokens,error', [('timeout', None, 52, 'request timed out'),
    ('failed', 400, 52, 'context window exceeded'), ('failed', None, None, 'missing response')])
async def test_calibration_failures_remain_failures_despite_floor(status, http, tokens, error):
    class Failed(Client):
        async def execute(self, spec, **kw):
            r = await super().execute(spec, **kw)
            r.status, r.http_status, r.input_tokens, r.error = status, http, tokens, error
            return r
    _, check, client, _ = await perform(client=Failed())
    assert check.status == 'fail' and point(check)['reason_code'] == 'calibration_failed'
    assert all(s.scenario.startswith('load_calibration_') for s in client.specs)


@pytest.mark.parametrize('buffered,expected', [(1, 'pass'), (4, 'inconclusive')])
async def test_interference_buffering_scoring_uses_majority(buffered,expected):
    class Buffered(Client):
        async def execute(self,spec,**kw):
            r=await super().execute(spec,**kw)
            if spec.load_point.get('role') == 'short_stream':
                r.answer_chunks = 1 if buffered == 4 else 5
            return r
    _,check,_,_=await perform(configuration(load={'combinations':{'enabled':False},'interference':{'enabled':True},'schedule':{'levels':[2]}}),Buffered())
    p=point(check)
    assert p['status'] == expected
    assert p['interference']['buffered_stream'] is (buffered == 4)


async def test_known_run_stop_cancellation_finalizes_without_reraising():
    class Paused(Client):
        def __init__(self): super().__init__(); self.sent=asyncio.Event()
        async def execute(self,spec,**kw):
            if not spec.scenario.startswith('load_calibration_'):
                self.sent.set(); await asyncio.Event().wait()
            return await super().execute(spec,**kw)
    c=short_windows(configuration()); run,client=_Run(c,None,None),Paused()
    task=asyncio.create_task(load.execute(run,c.targets[0],client))
    await asyncio.wait_for(client.sent.wait(),.5)
    run.stop.set(); task.cancel(); await asyncio.wait_for(task,.5)
    p=point(load.result(run,c.targets[0]))
    assert p['reason_code'] == 'run_stopped' and p['cancelled'] == 1


async def test_real_client_blank_one_token_calibration_preserves_usage(monkeypatch):
    import json
    from giraffe.client import LLMClient
    calls = 0
    class Stream(httpx.AsyncByteStream):
        def __init__(self, payload, blank):
            self.payload, self.blank = payload, blank
        async def __aiter__(self):
            for text in (' ',) if self.blank else ('K7', 'P4'):
                yield ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]}) + '\n\n').encode()
            yield ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'length'}],
                'usage': {'prompt_tokens': len(self.payload['messages'][0]['content']) // 4, 'completion_tokens': 1}}) + '\n\n').encode()
            yield b'data: [DONE]\n\n'
    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            stream=Stream(json.loads(request.content), calls == 1))
    monkeypatch.setattr(LLMClient, '_create_client', lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    c = short_windows(configuration(checks=['access', 'serving', 'concurrency'], max_output_tokens=1, max_requests=12,
        stop_after_errors=1, load={'profile': {s: {'input_tokens': n, 'output_tokens': o}
            for s, n, o in [('short', 64, 1), ('medium', 96, 2), ('long', 128, 3)]}}))
    run = _Run(c, None, None)
    async with LLMClient(c.targets[0], c) as client:
        await load.execute(run, c.targets[0], client)
    check = load.result(run, c.targets[0])
    assert check.metrics['calibrations'][0]['status'] == 'calibrated'
    assert run.records[0].status == 'failed' and run.records[0].input_tokens == 64
    assert run.errors == 0 and point(check)['dispatched'] > 0
    assert run.records[0].check_ids == ['load']


@pytest.mark.parametrize('overlap,allowance', [(False, 1), (True, 1)])
def test_plan_warning_uses_executor_target_quota(overlap,allowance):
    targets=[{'name':name,'url':'http://localhost:1/v1','model':'test'} for name in ('local','other')]
    c=configuration(targets=targets,overlap_models=overlap,max_requests=16,checks=['load','consistency'],consistency={'repetitions':2,'levels':[1],'shapes':['short'],'prompts_per_shape':2},load={'schedule':{'duration_seconds':10}})
    run=_Run(c,None,None); plan=request_plan(c)
    assert plan['target_quotas'] == run.quotas == {'local':8,'other':8}
    assert min(run.quotas.values()) - load.later_reservation(c) - len(load.calibration_keys(c.load_test)) == 3
    warning,=plan['request_warnings']
    assert warning['kind'] == 'adaptive_risk' and warning['fields'] == ['max_requests']
    assert all(name in warning['msg'] for name in ('local','other'))


@pytest.mark.parametrize('scores,expected,correct,scored', [
    ([False] + [None] * 9, 'fail', 0, 1),
    ([True] * 9 + [None], 'inconclusive', 9, 9),
    ([True] * 10, 'pass', 10, 10),
])
def test_load_correctness_uses_scorable_denominator_and_keeps_missing_unknown(scores, expected, correct, scored):
    c, p = scored_point()
    p['cut_short'] = False
    run = _Run(c, None, None)
    rows = [record(str(i), score=value) for i, value in enumerate(scores)]
    load.assess_point(run, c.targets[0], c.load_test, p, rows)
    g = p['groups'][0]
    assert g['scored'] == scored and g['correct'] == correct
    assert g['unscored'] == len(scores) - scored
    assert g['correct_percent'] == correct / scored * 100
    assert p['status'] == expected


async def test_minimum_valid_count_never_stops_a_maintained_window_early():
    c = configuration(load={'pass_criteria': {'min_valid_responses': 1}})
    _, check, _, _ = await perform(c)
    p = point(check)
    assert p['dispatched'] > 1
    assert p['delivered_window_seconds'] >= .199
    assert not p['cut_short'] and p['status'] == 'pass'


@pytest.mark.parametrize('rates,bursts,duration,possible', [
    ([250], None, 3600, True),
    ([100, 250], None, 3600, True),
    ([100, 250], {'size': 32, 'first_seconds': 0, 'gap_seconds': 1}, 3600, True),
    (None, {'size': 1024, 'first_seconds': 0, 'gap_seconds': .001}, 1, False),
    ([1], {'size': 1024, 'first_seconds': 0, 'gap_seconds': .001}, 1, False),
    ([250], {'size': 1, 'first_seconds': 400, 'gap_seconds': 1}, 401, False),
])
def test_finite_arrival_corrections_revalidate_or_explain_impossible_minimum(rates, bursts, duration, possible):
    from copy import deepcopy
    from pydantic import ValidationError
    from giraffe.models import RunConfig
    values = configuration().model_dump()
    values.update(concurrency=None, max_requests=None, max_duration_seconds=None)
    settings = values['load_test']
    settings['schedule']['duration_seconds'] = duration
    settings['patterns']['concurrent']['enabled'] = False
    settings['patterns']['constant']['enabled'] = rates is not None
    if rates is not None:
        settings['patterns']['rates'] = rates
    if bursts is not None:
        settings['patterns']['bursts'].update(enabled=True, **bursts)
    with pytest.raises(ValidationError) as caught:
        RunConfig.model_validate(values)
    issue, = caught.value.errors()
    actions = issue['ctx']['actions']
    numeric = [a for a in actions if 'value' in a]
    if rates is not None:
        assert {'label': 'Edit Rates (requests/second)', 'field': 'load_test.patterns.rates'} in actions
    if bursts is not None:
        assert {'label': 'Edit Time between burst starts (seconds)', 'field': 'load_test.patterns.bursts.gap_seconds'} in actions
    assert bool(numeric) is possible
    for action in numeric:
        corrected = deepcopy(values)
        owner = corrected
        *parents, field = action['field'].split('.')
        for parent in parents:
            owner = owner[parent]
        owner[field] = action['value']
        RunConfig.model_validate(corrected)
    if not possible:
        assert 'minimum valid sending window' in issue['msg']
        assert all('value' not in action for action in actions)
        assert any(a['field'] == 'load_test.patterns.bursts.gap_seconds' for a in actions)
