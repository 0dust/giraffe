"""Run-plan presentation follows selected checks without changing physical work."""

import json

import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_run_limits_phase6_ui import Markup
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


def configuration(**settings):
    return RunConfig(targets=[{'name': 'local', 'url': 'http://unused.test/v1', 'model': 'test'}], **settings)


@pytest.mark.parametrize('checks,summary', [
    (['first_output'], '7 requests per endpoint'),
    (['generation'], '7 requests per endpoint'),
    (['access', 'generation'], '6 requests per endpoint'),
    (['gpu'], 'No inference requests'),
])
def test_blueprint_and_review_have_one_disclosure_per_selected_check(checks, summary):
    config = configuration(checks=checks)
    plan = request_plan(config)
    result = browser_logic('const plan=' + json.dumps(plan) + r''';
const original=JSON.stringify({config,plan});
nodes['#request-plan']={};state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
renderRequestPlan();
console.log(JSON.stringify({blueprint:renderRunBlueprint(config,plan),review:nodes['#request-plan'].innerHTML,groups:selectedCheckPlan(config,plan)}));
assert.equal(JSON.stringify({config,plan}),original);
''', config)
    for surface in ('blueprint', 'review'):
        tree = Markup(result[surface])
        rows = [node for node in tree.nodes if 'data-plan-row' in node]
        assert [node['data-plan-row'] for node in rows] == checks
        assert summary in rows[-1]['text']
        assert 'Flight plan' not in result[surface]
    if checks == ['first_output']:
        assert 'Endpoint response' not in result['blueprint']
        assert 'Response validity' not in result['blueprint']
        assert {row['id'] for row in result['groups'][0]['rows']} == {'initial', 'warm', 'first_output'}
        for surface in ('blueprint', 'review'):
            assert 'Up to 35m 0s including request deadlines.' in result[surface]
            assert '1 batch × 300 s request timeout' in result[surface]
            assert '6 batches × 300 s request timeout' in result[surface]


def test_shared_phases_have_one_owner_and_custom_answer_checks_add_no_duplicate_requests():
    config = configuration(checks=['access', 'serving', 'first_output', 'correctness', 'load', 'gpu'],
                           request_source='request_pack', request_pack={'version': 1, 'cases': [
                               {'fixture_id': 'one', 'messages': [{'role': 'user', 'content': 'Q'}],
                                'scorer': 'exact', 'expected': 'A'}]})
    browser_logic('const plan=' + json.dumps(request_plan(config)) + r''';
const before=JSON.stringify({config,plan}),groups=selectedCheckPlan(config,plan);
const owned=groups.flatMap(group=>group.rows).map(row=>row.id);
assert.equal(owned.length,new Set(owned).size);
assert.equal(owned.sort().join(','),plan.rows.map(row=>row.id).sort().join(','));
const first=groups.find(group=>group.id==='first_output'),correct=groups.find(group=>group.id==='correctness');
assert.equal(first.rows.reduce((count,row)=>count+(row.requests_per_target||0),0),0);
assert.equal(first.shared.map(row=>row.owner).join(','),'access,serving');
assert.equal(correct.rows.find(row=>row.id==='pack_control').requests_per_target,1);
assert.equal(correct.shared[0].owner,'load');
const markup=renderCheckPlan(config,plan);assert.match(markup,/Shared requests · no additional requests/);
assert.match(markup,/Answers also checked from Load test · no additional requests/);
assert.equal((markup.match(/data-plan-phase="pack_control"/g)||[]).length,1);
assert.match(markup,/No inference requests · observes metrics/);
assert.equal(JSON.stringify({config,plan}),before);
''', config)


def test_endpoint_specific_counts_restart_setup_and_adaptive_upper_bounds_remain_distinct():
    browser_logic(r'''
config.checks=['context'];config.targets=[{name:'alpha'},{name:'beta'}];
const plan={rows:[{id:'restart_readiness',title:'Restart readiness',kind:'bounded',requests_per_target:null,summary:'Readiness probe outside the request budget.'},
 {id:'context',title:'Context',requests_per_target:null,target_request_counts:{alpha:2,beta:5},summary:'Different endpoint admissions.'}]};
let groups=selectedCheckPlan(config,plan),markup=renderCheckPlan(config,plan);
assert.equal(groups[0].rows.length,1);assert.equal(groups[0].setup.length,1);
assert.match(markup,/alpha: 2 requests · beta: 5 requests/);
assert.match(markup,/Setup and readiness · outside the target request budget/);
assert.doesNotMatch(markup,/Request count is adaptive/);
config.checks=['scale_down'];plan.rows=[{id:'scale_down',target:'beta',requests_per_target:null,effective_request_upper_bound:9,effective_duration_upper_bound:12,summary:'Externally remove one replica.'}];
markup=renderCheckPlan(config,plan);
assert.match(markup,/Up to 9 requests on beta · up to 12 seconds/);
assert.equal((markup.match(/Up to 9 requests on beta/g)||[]).length,2);
assert.doesNotMatch(markup,/Request count is adaptive per endpoint/);
config.checks=['context'];plan.rows=[{id:'context',requests_per_target:2},{id:'unexpected_shared_phase',title:'Shared preparation',requests_per_target:3}];
markup=renderCheckPlan(config,plan);assert.match(markup,/5 requests per endpoint/);
assert.match(markup,/Shared work · Shared preparation/);
''')


def test_load_and_multi_user_details_stay_inside_their_selected_rows_with_units():
    config = configuration(checks=['access', 'serving', 'generation', 'load', 'multi_user'])
    result = browser_logic('const plan=' + json.dumps(request_plan(config)) + r''';
console.log(JSON.stringify({markup:renderRunBlueprint(config,plan),summary:loadPlanRequestCount(plan.load_test)}));
''', config)
    tree = Markup(result['markup'])
    rows = {node['data-plan-row']: node for node in tree.nodes if 'data-plan-row' in node}
    assert list(rows) == config.checks
    assert '6 requests per endpoint' in rows['generation']['text']
    assert '135 scheduled requests per endpoint + completion-driven requests' in rows['load']['text']
    assert 'Unknown + 135' not in result['markup']
    details, = [node for node in tree.nodes if node.get('class') == 'configured-load-plan']
    assert rows['load'] in details['parents']
    for phrase in ('Input target → output cap', '1, 2 requests/second', 'Execution order',
                   'Additional requests start as earlier requests finish; their count is unknown.'):
        assert phrase in details['text']
    for users in (1, 5, 20):
        assert f'{users} user{"s" if users != 1 else ""} for 15 seconds per endpoint' in rows['multi_user']['text']


def test_migrated_load_schedules_keep_their_execution_details_in_one_check():
    config = configuration(checks=['capacity', 'arrivals'])
    browser_logic('const plan=' + json.dumps(request_plan(config)) + r''';
const markup=renderCheckPlan(config,plan),groups=selectedCheckPlan(config,plan);
assert.equal(groups.map(group=>group.id).join(','),'load');
assert.equal((markup.match(/data-plan-row="/g)||[]).length,1);
for(const note of plan.load_test.execution_order_notes)assert(markup.includes(escape(note)));
for(const text of plan.load_test.execution_order)assert(markup.includes(escape(text)));
assert.match(markup,/Migrated/);
''', config)


def test_eta_requires_complete_evidence_but_actionable_warnings_and_errors_always_survive():
    browser_logic(r'''
const base={estimated_seconds:5,estimated_low_seconds:3,estimated_high_seconds:7,confidence:'medium',
 time_bound_reasons:['Unknown traffic speed'],time_warnings:[{kind:'estimate_risk',msg:'Requests may reach their deadline.',fields:['max_duration_seconds']}],
 request_warnings:[{msg:'Insufficient request budget.',fields:['max_requests']}],resource_issues:[{blocking:true,msg:'Known context window exceeded.',fields:['context_limit']}]};
for(const extra of [{},{has_complete_time_estimate:false},{has_complete_time_estimate:true,estimated_high_seconds:Infinity},{has_complete_time_estimate:true,estimated_low_seconds:null}]){
 const markup=renderPlanTime({...base,...extra});
 assert.doesNotMatch(markup,/Estimated duration:|Confidence:|Additional work at unknown speed|Unknown traffic speed/);
 for(const message of ['Requests may reach their deadline.','Insufficient request budget.','Known context window exceeded.'])assert(markup.includes(message));
 assert.match(markup,/data-setting-path="max_duration_seconds"/);
}
const complete=renderPlanTime({...base,has_complete_time_estimate:true});
assert.match(complete,/Estimated duration: about 3 s–7 s/);assert.match(complete,/Confidence: medium/);
const groups=[{check_id:'sessions',title:'Concurrent conversations',target:'local',configured_seconds:0,allowance_seconds:3600,total_seconds:3600,phases:[{title:'Conversation requests',kind:'requests',requests:60,batches:12,request_timeout_seconds:300,allowance_seconds:3600}]}];
const warning=renderTimingRisk({msg:'Requests could exceed the limit.',check_time_summaries:groups,fields:[]});
assert.match(warning,/Up to 60m 0s including request deadlines/);assert.match(warning,/12 batches × 300 s request timeout/);
assert.doesNotMatch(warning,/300 s in total/);
const fractional=renderCheckTimeSummaries([{title:'Tiny timeout',total_seconds:.0000003,phases:[{title:'Requests',requests:3,batches:3,request_timeout_seconds:.0000001,allowance_seconds:.0000003}]}]);
assert.match(fractional,/3 batches × 0.0000001 s request timeout/);assert.match(fractional,/Up to 0.0000003 s including request deadlines/);
assert.match(renderTimingRisk({msg:'Sending windows exceed the limit.',schedule_exceeds_limit:true,fields:[]}),/The configured schedule exceeds the time limit/);
''')


def test_arrival_history_requires_matched_run_evidence_and_keeps_labels_and_source_links():
    browser_logic(r'''
const empty=Array.from({length:6},()=>({status:'unknown',message:'No recent run with matching arrival workload, rate and deployment metadata.'}));
assert.equal(renderArrivalHistory({arrival_history:empty}),'');
const rows=[...empty,{status:'observed',run_id:'old/run',message:'local · Short → Long at 2 requests/second: all arrivals dispatched.'},
 {status:'warning',run_id:'warning',message:'staging · Long → Short at 5 requests/second: incomplete delivery.'},
 {status:'observed',message:'Incomplete placeholder'}, {status:'unknown',run_id:'unmatched',message:'No matching workload.'}];
const markup=renderArrivalHistory({arrival_history:rows});
assert.match(markup,/local · Short → Long at 2 requests\/second/);assert.match(markup,/staging · Long → Short at 5 requests\/second/);
assert.match(markup,/#runs\/old%2Frun/);assert.equal((markup.match(/Open source run/g)||[]).length,2);
assert.doesNotMatch(markup,/No recent run|Incomplete placeholder|No matching workload/);
''')


def test_context_advisory_lives_once_beside_load_tokens_and_refreshes_without_hiding_real_errors():
    config = configuration(checks=['load'])
    plan = request_plan(config)
    run_render_checks('const config=' + config.model_dump_json() + ',plan=' + json.dumps(plan) + r''';
state.config=config;state.bootstrap.config=config;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
$('#main').innerHTML='<div id="request-plan"></div>'+renderLoadEditor(config);
const advisory=$('[data-load-context-warning]');assert(advisory);
assert.equal((advisory.textContent.match(/Giraffe can’t verify/g)||[]).length,1);
assert(advisory.closest('.load-section').textContent.includes('Data profile'));
assert(advisory.querySelector('[data-setting-path="load_test.profile.long.input_tokens"]'));
plan.errors=[{msg:'Input exceeds the known endpoint context window.',fields:['load_test.profile.long.input_tokens']}];
const blueprint=renderRunBlueprint(config,plan);assert.doesNotMatch(blueprint,/Giraffe can.t verify|Endpoint context window unknown/);
assert.match(blueprint,/Input exceeds the known endpoint context window/);
renderRequestPlan();assert.doesNotMatch($('#request-plan').textContent,/Giraffe can.t verify|Endpoint context window unknown/);
assert.match($('#request-plan').textContent,/Input exceeds the known endpoint context window/);
state.requestPreview.status='pending';syncLoadDisplay(config);assert.equal(advisory.textContent,'');
state.requestPreview.status='current';syncLoadDisplay(config);assert.match(advisory.textContent,/Giraffe can’t verify/);
plan.load_warnings=plan.load_warnings.filter(row=>row.kind!=='context_window_unknown');syncLoadDisplay(config);assert.equal(advisory.textContent,'');
''', dom=True)


def test_review_selected_check_expansion_and_focus_survive_current_pending_and_changed_phases():
    config = configuration(checks=['first_output'])
    run_render_checks('const config=' + config.model_dump_json() + ',plan=' + json.dumps(request_plan(config)) + r''';
$('#main').innerHTML='<div id="request-plan"></div>';
state.config=config;state.bootstrap.config=config;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
currentSettingErrors=()=>[];localSettingWarnings=()=>[];
renderRequestPlan();$('.workload-breakdown').open=true;
let row=$('[data-plan-row="first_output"]');row.open=true;row.querySelector('summary').focus({preventScroll:true});
plan.rows[1].requests_per_target=9;renderRequestPlan();row=$('[data-plan-row="first_output"]');
assert(row.open);assert($('.workload-breakdown').open);assert.equal(document.activeElement,row.querySelector('summary'));
assert.match(row.querySelector('summary').textContent,/10 requests per endpoint/);
state.requestPreview.status='pending';renderRequestPlan();assert.equal($('[data-plan-row="first_output"]'),null);
state.requestPreview.status='current';renderRequestPlan();assert($('[data-plan-row="first_output"]').open);
state.routeVersion++;state.requestPreview.route=state.routeVersion;renderRequestPlan();assert.equal($('[data-plan-row="first_output"]').open,false);
''', dom=True)


def test_refreshing_a_second_plan_surface_never_steals_disclosure_focus():
    config = configuration(checks=['first_output'])
    run_render_checks('const config=' + config.model_dump_json() + ',plan=' + json.dumps(request_plan(config)) + r''';
$('#main').innerHTML='<aside id="run-blueprint"></aside><div id="request-plan"></div>';
state.config=config;state.bootstrap.config=config;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
currentSettingErrors=()=>[];localSettingWarnings=()=>[];readForm=()=>({config});
renderRequestPlan();
const review=$('#request-plan');review.querySelector('.workload-breakdown').open=true;
let row=review.querySelector('[data-plan-row="first_output"]');row.open=true;
const summary=row.querySelector('summary');summary.focus({preventScroll:true});
syncRunBlueprint(config,plan);assert.equal(document.activeElement,summary);
const blueprint=$('#run-blueprint');blueprint.querySelector('.workload-breakdown').open=true;
row=blueprint.querySelector('[data-plan-row="first_output"]');row.open=true;row.querySelector('summary').focus({preventScroll:true});
renderRequestPlan();
assert(blueprint.contains(document.activeElement));assert.equal(document.activeElement.closest('[data-plan-row]').dataset.planRow,'first_output');
assert(blueprint.querySelector('[data-plan-row="first_output"]').open);
assert(review.querySelector('[data-plan-row="first_output"]').open);
''', dom=True)


@pytest.mark.parametrize('overlap,minimum,exceeds', [
    (False, 360, True),
    (True, 180, False),
])
def test_canonical_duration_warnings_respect_endpoint_overlap_and_preview_freshness(overlap, minimum, exceeds):
    config = RunConfig(targets=[
        {'name': name, 'url': 'http://unused.test/v1', 'model': 'test'}
        for name in ('alpha', 'beta')
    ], checks=['load'], max_duration_seconds=300, overlap_models=overlap)
    plan = request_plan(config)
    assert plan['load_test']['configured_load_seconds'] == 180
    assert plan['minimum_seconds'] == minimum
    assert plan['schedule_exceeds_limit'] is exceeds
    assert plan['could_exceed_limit'] is not exceeds
    assert plan['has_complete_time_estimate'] is False
    run_render_checks('const config=' + config.model_dump_json() + ',plan=' + json.dumps(plan) + r''';
const before=JSON.stringify({config,plan});
$('#main').innerHTML='<aside id="run-blueprint"></aside><div id="request-plan"></div>';
state.config=config;state.bootstrap.config=config;readForm=()=>({config});
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
renderRequestPlan();
const definite='The configured schedule exceeds the time limit.',possible='Some checks may not finish within the time limit.';
const expected=plan.schedule_exceeds_limit?definite:possible,unexpected=plan.schedule_exceeds_limit?possible:definite;
for(const selector of ['#run-blueprint','#request-plan']){
 const text=$(selector).textContent;
 assert.equal(text.split(expected).length-1,1);assert(!text.includes(unexpected));
 assert(!text.includes('Estimated duration:'));
 assert(!text.includes('The configured Load sending schedule exceeds Maximum duration.'));
 assert(!text.includes('Load requests could exceed Maximum duration if they reach their deadlines.'));
}
assert(!runLimitIssues(config).some(issue=>issue.fields.includes('max_duration_seconds')));
for(const status of ['pending','stale']){
 state.requestPreview.status=status==='pending'?'pending':'current';
 state.requestPreview.revision=state.configRevision+(status==='stale'?1:0);
 renderRequestPlan();
 assert.match($('#request-plan').textContent,/Updating request plan|stale · updating/);
 assert.match($('#run-blueprint').textContent,/Calculating traffic/);
 for(const selector of ['#run-blueprint','#request-plan']){
  const text=$(selector).textContent;
  assert(!text.includes(definite));assert(!text.includes(possible));
  assert(!text.includes('Load requests could exceed Maximum duration if they reach their deadlines.'));
 }
}
assert.equal(JSON.stringify({config,plan}),before);
''', dom=True)
