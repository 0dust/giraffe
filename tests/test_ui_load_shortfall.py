"""Load warning actions and placement exercised in the Node VM UI harness."""

import json

import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_load_test_phase_a import values
from tests.test_run_limits_phase6_ui import Markup
from tests.test_ux_catalogue import browser_logic


def test_field_warning_suppresses_only_self_pointing_action_and_keeps_value_actions():
    browser_logic(r'''
const path='load_test.schedule.duration_seconds',name='load_schedule_duration_seconds';
const attrs={};nodes['#field-'+name]={getAttribute:key=>attrs[key]||'',setAttribute:(key,value)=>attrs[key]=value,removeAttribute:key=>delete attrs[key]};
const message=nodes['#field-'+name+'-warning']={innerHTML:''};
patchHTML=(node,html)=>node.innerHTML=html;
const warning={msg:'Sample shortfall',fields:[path],actions:[{field:path,label:'Edit settings'}]};
const plan={load_warnings:[warning]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
syncInlineErrors();assert.match(message.innerHTML,/Sample shortfall/);assert.doesNotMatch(message.innerHTML,/<button/);
warning.actions=[{fields:[path],label:'Edit settings'}];syncInlineErrors();assert.doesNotMatch(message.innerHTML,/<button/);
warning.actions=[{field:path,label:'Set to 26 s',value:26}];syncInlineErrors();
assert.match(message.innerHTML,/Set to 26 s/);assert.match(message.innerHTML,/data-setting-value="26"/);
assert.match(message.innerHTML,/data-action="use-setting-correction"/);
warning.actions=[{field:'load_test.request_timeout_seconds',label:'Edit timeout'}];syncInlineErrors();
assert.match(message.innerHTML,/<button/);assert.match(message.innerHTML,/load_test.request_timeout_seconds/);
warning.actions=[{field:path,label:'Edit duration'},{field:'load_test.request_timeout_seconds',label:'Edit timeout'}];
syncInlineErrors();assert.equal((message.innerHTML.match(/<button/g)||[]).length,2);
''')


def test_setup_keeps_warning_beside_field_and_launch_keeps_its_copy_and_fix():
    config = RunConfig.model_validate(values())
    plan = request_plan(config)
    browser_logic('const plan=' + json.dumps(plan) + r''';
const path='load_test.schedule.duration_seconds',name='load_schedule_duration_seconds';
const warning={msg:'Unique sample shortfall',fields:[path],actions:[{label:'Set to 26 s',field:path,value:26}]};
plan.load_warnings=[warning];plan.warnings=[warning.msg];
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
state.configView='form';state.setupStep=2;
nodes['#field-'+name]={getAttribute:()=>'',setAttribute(){},removeAttribute(){}};
const message=nodes['#field-'+name+'-warning']={innerHTML:''};
patchHTML=(node,html)=>node.innerHTML=html;
nodes['#request-plan']={};
syncInlineErrors();
const blueprint=renderRunBlueprint(config,plan);
assert.equal((message.innerHTML.match(/Unique sample shortfall/g)||[]).length,1);
assert.doesNotMatch(blueprint,/Unique sample shortfall/);
assert.match(message.innerHTML,/Set to 26 s/);
state.setupStep=3;renderRequestPlan(config);
const launch=nodes['#request-plan'].innerHTML;
assert.equal((launch.match(/Unique sample shortfall/g)||[]).length,1);
assert.match(launch,/Set to 26 s/);assert.match(launch,/data-setting-value="26"/);
state.configView='json';assert.match(renderRunBlueprint(config,plan),/Unique sample shortfall/);
delete nodes['#field-'+name];state.configView='form';
assert.match(renderRunBlueprint(config,plan),/Unique sample shortfall/);
''', config)


@pytest.mark.parametrize('field,value', [
    ('load_test.schedule.duration_seconds', 26),
    ('load_test.request_timeout_seconds', 8),
    ('load_test.migrated_tests.0.settings.request_timeout_seconds', 8),
])
def test_load_warning_value_action_applies_offered_setting(field, value):
    config = RunConfig.model_validate(values())
    browser_logic('const field=' + json.dumps(field) + ',value=' + str(value) + r''';
const plan={load_warnings:[{fields:[field],actions:[{field,value,label:'Set value'}]}]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
let focused=null,renders=0;
captureFormDraft=()=>{};configChanged=()=>{};renderNew=()=>renders++;focusSetting=path=>focused=path;
configError=error=>{if(error)throw error;};
const button={dataset:{action:'use-setting-correction',settingPath:field,settingValue:String(value)}};
await listeners.click({target:{closest:q=>['[data-action]','[data-setting-value]'].includes(q)?button:null}});
assert.equal(field.split('.').reduce((obj,key)=>obj[key],state.config),value);
assert.equal(focused,field);assert.equal(renders,1);
''', config)


def test_sample_explanation_stays_in_duration_info_button():
    result = browser_logic('console.log(JSON.stringify({html:renderLoadEditor(config)}));', RunConfig.model_validate(values()))
    tree = Markup(result['html'])
    help_text, = [node for node in tree.nodes if node.get('id') == 'field-load_schedule_duration_seconds-help-text']
    sentence = 'If fewer than the required valid responses can start in this time, Giraffe keeps sending until they have started, within Maximum duration. Mixed traffic isn’t extended.'
    assert sentence in help_text['text']
    assert result['html'].count('Mixed traffic isn’t extended.') == 1
    assert any(node.get('id') == 'field-load_schedule_duration_seconds-help' for node in help_text['parents'])


def test_extension_reason_renders_in_condition_dialog_and_point():
    from tests.test_ui_conditions import DIALOG_SETUP
    from tests.test_ux_results import run_render_checks

    run_render_checks(DIALOG_SETUP + r'''
(async()=>{
 const reason='Sending continued 0.2 s past the 1 s window so 5 requests could start.';
 aggregate={...aggregate,reason,reasons:[reason],status:'passed',aggregate_status:'passed',running:0,completed:5,started:5};
 draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 assert($('#test-dialog').textContent.includes(reason));
 const html=renderLoadTestResults({points:[{id:'load',type:'combinations',pattern:'concurrent',level:1,status:'pass',reason,groups:[]}]});
 assert(html.includes(reason));
})()
''', dom=True)


@pytest.mark.parametrize('predicted', [False, True])
def test_upper_allowance_renders_extension_label_and_help(predicted):
    config = RunConfig.model_validate(values())
    config.load_test.schedule.duration_seconds = 20
    config.load_test.request_timeout_seconds = 7
    config.load_test.pass_criteria.min_valid_responses = 5
    observations = None
    if predicted:
        from giraffe.latency import input_bucket
        observations = {'local': [{
            'family': 'load.combinations.concurrent', 'concurrency': 1,
            'stream': config.stream, 'output_cap': config.load_test.profile.short.output_tokens,
            'input_bucket': input_bucket(config.load_test.profile.short.input_tokens * 4),
            'n': 4, 'p50_ms': 6400, 'p90_ms': 6400,
        }]}
    plan = request_plan(config, observations=observations)
    label = ('Upper allowance including request deadlines' if predicted
             else 'Upper allowance before any window extension')
    assert plan['load_test']['time_with_allowances_seconds'] == (33 if predicted else 27)
    browser_logic('const plan=' + json.dumps(plan) + ',label=' + json.dumps(label) + r''';
const html=renderLoadPlan(config,plan);
assert(html.includes(label));
assert(html.includes(loadElapsed(plan.load_test.time_with_allowances_seconds)));
assert(html.includes('If fewer than the required valid responses start in a window, sending continues until they have, up to one request timeout per remaining round and within Maximum duration.'));
assert(html.includes('id="field-load_upper_allowance-info"'));
''', config)
