"""Persistent-user setup and results use the same identities as the runner."""
from collections import OrderedDict
import json

from giraffe.models import RunConfig
from giraffe.web import _track_live_request
from tests.test_ux_catalogue import browser_logic


def configuration():
    return RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
                     checks=['multi_user'], concurrency=5, multi_user={'levels': [20]})


def test_multi_user_form_and_drawer_roundtrip():
    result = browser_logic(r'''
const original=JSON.stringify(config),check=bootstrap.checks.find(c=>c.id==='multi_user');
const markup=renderTestCard(check,config);
assert.match(markup,/20 users/);assert.match(markup,/5 shared request slots/);
for(const name of ['levels','duration_seconds','warmup_seconds','seed','think_time_values','think_time_weights','profile_short_input_tokens','criteria_max_completion_ms'])assert(markup.includes('name="multi_user_'+name+'"'));
assert.doesNotMatch(markup,/name="test_multi_user_limit_/);
assert.equal(JSON.stringify(config),original);
const values=new FormData();values.set('multi_user_rendered','1');
values.set('multi_user_levels','1, 5, 20');values.set('multi_user_duration_seconds','2.5');
values.set('multi_user_warmup_seconds','0.2');values.set('multi_user_seed','17');
values.set('multi_user_think_time_values','0, 1');values.set('multi_user_think_time_weights','1, 3');
values.set('multi_user_shape_short_short','on');values.set('multi_user_shape_medium_long','on');
values.set('multi_user_criteria_max_completion_ms','250');values.set('multi_user_criteria_max_error_percent','');
const changed=configureMultiUser(config,values,bootstrap.multi_user.defaults);
assert.equal(changed.concurrency,5);assert.equal(JSON.stringify(changed.multi_user.levels),'[1,5,20]');
assert.equal(changed.multi_user.pass_criteria.max_error_percent,null);
assert.equal(settingDestination('multi_user.pass_criteria.max_completion_ms').name,'multi_user_criteria_max_completion_ms');
assert.equal(settingDestination('multi_user.levels.0').check,'multi_user');
assert.equal(settingPath('multi_user_profile_short_input_tokens'),'multi_user.profile.short.input_tokens');
console.log(JSON.stringify(changed));
''', configuration())
    resolved = RunConfig.model_validate(result)
    assert resolved.multi_user.duration_seconds == 2.5
    assert resolved.multi_user.think_time_seconds.weights == [1, 3]
    assert resolved.multi_user.shapes == ['short_short', 'medium_long']


def test_multi_user_results_retain_unserved_rows_and_request_drilldown():
    from tests.test_multi_user_metrics import observation, record
    from giraffe.multi_user_metrics import summarize
    records = [record('user-01')]
    summary = summarize(observation(records), records)
    browser_logic('const summary='+json.dumps(summary)+r''';
const markup=renderMultiUserResults(summary);
assert.match(markup,/Worst served user/);assert.match(markup,/user-01/);assert.match(markup,/user-02/);
assert.match(markup,/Shared-window output/);assert.match(markup,/unexercised/);
assert.match(markup,/data-request="request-user-01"/);
assert.match(markup,/0 \/ 0/);
assert.match(requestIdentityContent({id:'r',status:'completed',workload:{user_id:'user-01',population:20}},[]),/user-01/);
''', configuration())


def test_live_user_identity_survives_started_and_completed_events():
    job = {'requests': OrderedDict(), 'live_request_count': 0, 'evicted_running': set(),
           'live_request_summary': {'evicted': 0, 'through_slot': None, 'statuses': {}}}
    identity = {'user_id': 'user-01', 'population': 20, 'profile': 'short_short',
                'window_id': 'multi_user_20_measurement', 'phase': 'measurement', 'request_sequence': 2}
    for kind in ['request_started', 'request_completed']:
        event = {'event': kind, 'request_slot': 0, 'target': 'local', 'scenario': 'multi_user_20_measurement',
                 'user': {**identity, 'prompt': 'must not reach live identity'}}
        if kind == 'request_completed':
            event['request'] = {'id': 'r', 'status': 'completed'}
        _track_live_request(job, event, 100)
        assert job['requests'][0]['workload'] == identity


async def test_population_preview_and_bound_label_are_independent_of_slots(tmp_path):
    from tests.test_web import browser
    values = configuration().model_dump(mode='json')
    async with browser(tmp_path) as client:
        preview = await client.post('/api/config/preview', json=values)
        assert preview.status_code == 200
        row = next(row for row in preview.json()['request_plan']['rows'] if row['id'] == 'multi_user_20')
        assert (row['users'], row['simultaneous_limit']) == (20, 5)
        values['multi_user']['levels'] = [33]
        response = await client.post('/api/config/validate', json=values)
        assert response.status_code == 422
        message = response.json()['detail'][0]['msg']
        assert 'User population levels' in message
        assert 'shared request slots' in message
        assert 'measured reliable client-load ceiling' not in message


def test_check_view_does_not_invent_aggregate_zero_counts():
    from tests.test_multi_user_metrics import observation, record
    from giraffe.multi_user_metrics import summarize
    records = [record('user-01')]
    summary = summarize(observation(records), records)
    browser_logic('const summary='+json.dumps(summary)+';const requests='+json.dumps([r.model_dump(mode='json') for r in records])+r''';
const check={id:'multi_user',title:'Multi-user',status:'inconclusive',required:true,summary:'Some users unexercised',metrics:{multi_user:summary},evidence_ids:requests.map(r=>r.id)};
const markup=renderCheck(check,{requests,checks:[check],manifest:{config}});
assert.match(markup,/Worst served user/);assert.doesNotMatch(markup,/<dt>Requests completed<\/dt>/);
assert.doesNotMatch(markup,/0 failed · 0 timed out/);
assert.doesNotMatch(markup,/class="evidence-table"/);
''', configuration())


def test_user_results_keep_horizontal_scroll_inside_the_table():
    from pathlib import Path
    from tests.test_ux_results import stylesheet_rules
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    rules = {selector: declarations for selector, declarations, _ in stylesheet_rules(css)}
    assert rules['.findings'].get('grid-template-columns') == 'minmax(0,1fr)'
    assert rules['.multi-user-results .metric-table'].get('max-width') == '100%'
    assert rules['.multi-user-results .metric-table'].get('overflow-x') == 'auto'
    browser_logic(r'''
const html=renderMultiUserResults({windows:[{phase:'measurement',population:20,users:[]}]});
assert.match(html,/class="metric-table" tabindex="0" role="region" aria-label="Individual outcomes at 20 users"/);
''', configuration())


def test_shared_window_comparison_is_observed_not_a_median_or_confidence_claim():
    browser_logic(r'''
const row={metric:'shared_window_output_tokens_per_second',aggregation:'common_window',status:'pass',baseline:10,current:10,baseline_samples:5,current_samples:5,threshold:20,method:'Observed completed-output delivery over matching common windows; no cross-run confidence interval.'};
let html=comparisonTable([row]);
assert.match(html,/Common window · Observed/);assert.match(html,/no cross-run confidence interval/);
assert.doesNotMatch(html,/Median|No material regression|Statistical details/);
html=comparisonTable([{...row,current:5,status:'fail'}]);
assert.match(html,/Common window · Observed decrease/);
html=comparisonTable([{...row,current:null,status:'inconclusive',reason:'Incomplete coverage.'}]);
assert.match(html,/Common window · Warning/);assert.match(html,/Incomplete coverage/);
assert.match(comparisonTable([{...row,aggregation:'median',metric:'latency_ms'}]),/Median · No material regression/);
''', configuration())


def test_unchecked_shapes_remain_empty_while_omitted_partial_fields_are_preserved():
    result = browser_logic(r'''
const values=new FormData();values.set('multi_user_rendered','1');
assert.equal(JSON.stringify(configureMultiUser(config,values,bootstrap.multi_user.defaults).multi_user.shapes),JSON.stringify(config.multi_user.shapes));
for(const input of ['short','medium','long'])for(const output of ['short','medium','long'])values.set('multi_user_shape_'+input+'_'+output,'off');
const changed=configureMultiUser(config,values,bootstrap.multi_user.defaults);
assert.equal(JSON.stringify(changed.multi_user.shapes),'[]');console.log(JSON.stringify(changed));
''', configuration())
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match='shapes'):
        RunConfig.model_validate(result)
