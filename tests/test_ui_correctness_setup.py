"""Answer correctness explains its actual workload and inherited request settings."""

import json

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_concurrent_load_ui_review import FORM
from tests.test_run_limits_phase6_ui import Markup
from tests.test_ux_catalogue import browser_logic
from tests.test_ui_question_navigation import navigation_browser


CORRECTNESS_FORM = (FORM.replace("config.checks=['load']", "config.checks=['correctness']")
                    .replace("check=>check.id==='load'", "check=>check.id==='correctness'")
                    .replace("testSettings:'load'", "testSettings:'correctness'"))


def test_correctness_controls_keep_setup_compact_and_builtin_preview_read_only():
    result = browser_logic(r'''
const check=bootstrap.checks.find(check=>check.id==='correctness');
config.checks=['correctness'];
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{rows:[{id:'correctness',requests_per_target:6}]}};
const local={...config,custom_fixtures:'local.jsonl'};
state.requestPreview.plan.rows=[{id:'correctness',requests_per_target:null,kind:'bounded'}];
assert.match(effectiveCheckSummary(check,local),/At least 6 requests/);
assert.match(renderBuiltinQuestions(local),/Additional configured cases can increase the request count/);
state.requestPreview.plan.rows=[{id:'correctness',requests_per_target:6}];
const loadOnly={...config,checks:['load'],request_source:'request_pack',load_test:structuredClone(bootstrap.load_test.defaults)};
loadOnly.load_test.pass_criteria.min_correct_percent=85;
assert.match(renderQuestionSetupSummary(loadOnly),/85%/);
console.log(JSON.stringify({markup:renderTestCard(check,config),summary:effectiveCheckSummary(check,config),builtins:bootstrap.builtin_questions,preview:renderBuiltinQuestions(config)}));
''')
    tree = Markup(result['markup'])
    assert result['summary'] == '6 requests · Up to 4 in progress · 100% correct required'
    expected = {
        'samples': ('Minimum requests', 'Repeat the built-in questions until at least this many requests have run. All three questions are covered.'),
        'concurrency': ('Maximum requests in progress', 'Start another request whenever one finishes.'),
        'request_timeout_seconds': ('Timeout per request (seconds)', 'Time allowed for each request, starting when it is sent.'),
        'max_output_tokens': ('Output token limit', 'Maximum generated tokens per request.'),
        'limit_min_correctness': ('Required correct answers (%)', 'Minimum percentage of scored answers that must pass. Missing scores can make the result inconclusive.'),
    }
    for name, (label, help_text) in expected.items():
        assert any(node['tag'] == 'label' and node['text'] == label for node in tree.nodes)
        paragraph, = [node for node in tree.nodes if node.get('id') == f'field-test_correctness_{name}-help-text']
        assert paragraph['text'] == help_text
    percent, = tree.named('test_correctness_limit_min_correctness')
    assert (percent['min'], percent['max']) == ('0', '100')
    assert 'Using run default: 300 seconds' in result['markup']
    inheritance = 'Leave timeout, request count, concurrency or output limit blank to use the shown defaults.'
    assert result['markup'].count(inheritance) == 1
    assert 'Leave blank to use the shown default.' not in result['markup']
    assert 'required' in percent
    for name in ('request_timeout_seconds', 'samples', 'concurrency', 'max_output_tokens'):
        assert result['markup'].index(inheritance) < result['markup'].index(f'name="test_correctness_{name}"')
    assert len(result['builtins']) == 3
    for item in result['builtins']:
        assert item['question'] not in result['markup']
        assert item['question'] in next(node['text'] for node in Markup(result['preview']).nodes if node.get('class') == 'builtin-example')
    assert '3 built-in questions' in result['markup']
    assert 'View examples' in result['markup']
    assert 'data-action="view-builtin-examples"' in result['markup']
    assert 'Manage questions' not in result['markup']
    assert 'View questions and expected answers' not in result['markup']
    assert 'The suite value is a default' not in result['markup']


def test_question_setup_requirements_follow_live_form_without_replacing_controls():
    navigation_browser(r'''
for(const [check,value] of [['correctness',37],['load',85]]){
 state.config.checks=[check];state.openTest=check;renderNew();
 const form=document.querySelector('#new-run-form'),savedPack=JSON.stringify(state.config.request_pack);
 const sources=[...form.querySelectorAll('[name="request_source"]')];
 const name=check==='correctness'?'test_correctness_limit_min_correctness':'load_pass_criteria_min_correct_percent';
 const input=form.elements[name];input.focus({preventScroll:true});window.scrollY=489;
 type(input,String(value));
 assert.match(document.querySelector('[data-question-setup-summary]').textContent,new RegExp(value+'%'));
 assert.equal(form.elements[name],input);assert.equal(document.activeElement,input);assert.equal(window.scrollY,489);
 assert.deepEqual([...form.querySelectorAll('[name="request_source"]')],sources);
 assert.equal(state.config.request_source,'request_pack');assert.equal(JSON.stringify(state.config.request_pack),savedPack);
}
assert.equal(requests.length,0);
''')


def test_builtin_setup_hides_inactive_evaluator_action_and_preserves_configuration():
    navigation_browser(r'''
state.config.request_source='builtin';state.config.judging={...bootstrap.judging.defaults,judges:[{name:'reviewer',url:'http://judge/v1',model:'judge-model',api_key_env:null,headers_env:{}}]};
const judging=JSON.stringify(state.config.judging),pack=JSON.stringify(state.config.request_pack);
renderNew();const form=document.querySelector('#new-run-form'),summary=document.querySelector('[data-question-setup-summary]');
assert.equal(Boolean(summary.querySelector('[data-action="configure-evaluator"]')),false);
assert.match(summary.textContent,/Not needed/);assert(visible('[data-action="view-builtin-examples"]'));
assert.equal(JSON.stringify(state.config.judging),judging);assert.equal(JSON.stringify(state.config.request_pack),pack);
for(const input of form.querySelectorAll('[name="request_source"]'))input.checked=input.value==='request_pack';
change(form.querySelector('[name="request_source"][value="request_pack"]'));
assert.equal(document.querySelector('[data-question-setup-summary] [data-action="configure-evaluator"]').textContent,'Edit evaluator');
assert.equal(JSON.stringify(state.config.judging),judging);assert.equal(JSON.stringify(state.config.request_pack),pack);
assert.equal(requests.length,0);
''')


def test_builtin_preview_belongs_to_correctness_while_load_explains_its_actual_source():
    browser_logic(r'''
const original=JSON.stringify(config),correctness=bootstrap.checks.find(check=>check.id==='correctness'),load=bootstrap.checks.find(check=>check.id==='load');
const generated='Generates prompts using these input token targets and output caps.';
for(const checks of [['load'],['correctness'],['correctness','load']]){
  const draft={...config,checks},correctnessMarkup=renderTestCard(correctness,draft),loadMarkup=renderTestCard(load,draft);
  assert.equal(correctnessMarkup.includes('data-action="view-builtin-examples"'),checks.includes('correctness'));
  assert.doesNotMatch(correctnessMarkup,/class="builtin-example"|Expected answer:<\/strong>/);
  assert.doesNotMatch(loadMarkup,/data-action="view-builtin-examples"|3 built-in questions/);
  assert.doesNotMatch(loadMarkup,/class="builtin-example"|Requests repeat these questions|Expected answer:<\/strong>/);
  assert(!loadMarkup.includes(generated));
  if(checks.includes('correctness')){
    assert.match(correctnessMarkup,/3 built-in questions/);
    const preview=renderBuiltinQuestions(draft,{expanded:true});
    for(const item of bootstrap.builtin_questions)assert(preview.includes('<code>'+escape(item.expected)+'</code>'));
  }
  assert.doesNotMatch(correctnessMarkup,/Load test uses this question source too/);
  const sourceMarkup=checks.includes('correctness')?correctnessMarkup:loadMarkup;
  assert.equal((sourceMarkup.match(/name="request_source"/g)||[]).length,2);
}
const fixtures=renderTestCard(load,{...config,checks:['load'],custom_fixtures:'local.jsonl'});
assert.match(fixtures,/Uses the configured fixture file; its answer limits and checks are preserved/);
assert(!fixtures.includes(generated));
assert.doesNotMatch(fixtures,/class="builtin-example"|Requests repeat these questions/);
const pack={...config,checks:['correctness','load'],request_source:'request_pack',request_pack:{version:1,cases:[{fixture_id:'saved',messages:[{role:'user',content:'My question'}],scorer:'exact',expected:'My answer'}]}};
const packOriginal=JSON.stringify(pack),correctnessMarkup=renderTestCard(correctness,pack),loadMarkup=renderTestCard(load,pack);
assert.match(correctnessMarkup,/These questions and answer checks are shared with Load test/);
assert.match(correctnessMarkup,/1 saved question/);assert.match(correctnessMarkup,/Manage questions/);
assert.doesNotMatch(correctnessMarkup,/>My question<|>My answer</);
assert.doesNotMatch(correctnessMarkup,/class="builtin-example"/);
assert(!loadMarkup.includes(generated));
assert.equal((correctnessMarkup.match(/name="request_source"/g)||[]).length,2);
assert.doesNotMatch(loadMarkup,/name="request_source"/);
assert.equal(JSON.stringify(pack),packOriginal);assert.equal(JSON.stringify(config),original);
''')


def test_only_request_options_inherit_when_blank_and_percentages_keep_numeric_validation():
    browser_logic(r'''
for(const [id,criterion,percentage,expected] of [['serving','max_error_rate','5',.05],['correctness','min_correctness','95',.95]]){
  const check=bootstrap.checks.find(check=>check.id===id),source=structuredClone(config);
  source.checks=[id];source.test_options[id]={request_timeout_seconds:500,max_output_tokens:999,samples:12};
  if(id==='correctness')source.test_options[id].concurrency=2;
  const values=new FormData();values.append('check',id);values.set('test_mode_'+id,'custom');
  for(const name of check.option_fields)values.set('test_'+id+'_'+name,'');
  values.set('test_'+id+'_limit_'+criterion,percentage);
  const read=configureTests(source,values,bootstrap.checks,bootstrap.traffic_defaults,{percentageInputs:true});
  for(const name of check.option_fields)assert.equal(read.test_options[id][name],undefined);
  assert.equal(read.test_options[id].limits[criterion],expected);
  values.set('test_'+id+'_limit_'+criterion,'');
  const blank=configureTests(source,values,bootstrap.checks,bootstrap.traffic_defaults,{percentageInputs:true});
  assert.equal(blank.test_options[id].limits[criterion],null);
  const markup=renderTestCard(check,read),input=markup.match(new RegExp('<input\\b[^>]*name="test_'+id+'_limit_'+criterion+'"[^>]*>'))[0];
  assert.match(input,/\brequired\b/);assert.match(input,/\bmin="0"/);assert.match(input,/\bmax="100"/);
  assert.doesNotMatch(markup,/Leave blank to use the shown default/);
}
''')


def test_reset_one_correctness_setting_preserves_other_overrides_and_percentage_serialization():
    browser_logic(CORRECTNESS_FORM + r'''
state.config.test_options.correctness={request_timeout_seconds:500,max_output_tokens:1000,limits:{min_correctness:.8}};
mount();
const check=bootstrap.checks.find(check=>check.id==='correctness');
assert.equal(checkFieldHint(check,state.config,'request_timeout_seconds'),'Custom: 500 seconds');
const button={dataset:{resetCheckId:'correctness',resetCheckSetting:'request_timeout_seconds',resetCheckKind:'option'}};
await listeners.click({target:{closest:q=>q==='[data-reset-check-setting]'?button:null}});
let read=readForm(false).config;
assert.equal(read.test_options.correctness.request_timeout_seconds,undefined);
assert.equal(read.test_options.correctness.max_output_tokens,1000);
assert.equal(read.test_options.correctness.limits.min_correctness,.8);
assert.equal(form.elements.test_correctness_request_timeout_seconds.value,300);
assert.equal(checkFieldHint(check,read,'request_timeout_seconds'),'Using run default: 300 seconds');
const criterion={dataset:{resetCheckId:'correctness',resetCheckSetting:'min_correctness',resetCheckKind:'limit'}};
await listeners.click({target:{closest:q=>q==='[data-reset-check-setting]'?criterion:null}});
read=readForm(false).config;
assert.equal(read.test_options.correctness.limits,undefined);
assert.equal(read.test_options.correctness.max_output_tokens,1000);
form.elements.test_correctness_max_output_tokens.value='';
listeners.input({target:form.elements.test_correctness_max_output_tokens});
read=readForm(false).config;
assert.equal(read.test_options.correctness,undefined);
''')


def test_question_execution_refreshes_after_edits_success_errors_and_cold_start_normalization():
    config = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
                       checks=['correctness'], request_source='request_pack',
                       request_pack={'version': 1, 'cases': [{'fixture_id': 'one', 'messages': [{'role': 'user', 'content': 'Q'}], 'scorer': 'exact', 'expected': 'A'}]})
    plans = json.dumps({timeout: request_plan(config.model_copy(update={'request_timeout_seconds': timeout}))
                       for timeout in (300, 40, 50)})
    browser_logic('const plans=' + plans + r''';const plan=timeout=>plans[timeout];
const realSyncInlineErrors=syncInlineErrors;
const panel={innerHTML:'',dataset:{},attributes:{},getAttribute(name){return this.attributes[name]||'';},setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];},focus(){this.focused=true;}};
nodes['[data-correctness-execution]']=panel;nodes['#field-request_pack_correctness']=panel;nodes['#field-request_pack_correctness-error']={};
syncInlineErrors=()=>{};renderRequestPlan=()=>{};syncLoadDisplay=()=>{};acceptMigrationNotes=()=>{};localSettingErrors=()=>[];
readForm=()=>({config,mode:'cold-start',restart_target:'local'});
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config:{...config,restart_target:'local'},plan:plan(300)};
renderRequestPreview();assert.match(panel.innerHTML,/300 seconds/);assert.equal(panel.hidden,false);
config.request_timeout_seconds=40;configChanged();
assert.match(panel.innerHTML,/Updating/);assert.doesNotMatch(panel.innerHTML,/300 seconds/);
api=async()=>({config:{...config,restart_target:'local'},request_plan:plan(40)});
await refreshRequestPreview(config,{draft:true});assert.match(panel.innerHTML,/40 seconds/);assert.doesNotMatch(panel.innerHTML,/Updating/);
let finish;api=()=>new Promise(resolve=>finish=resolve);
const previous=refreshRequestPreview(config,{draft:true});
config.request_timeout_seconds=50;configChanged();finish({config,request_plan:plan(40)});
await assert.rejects(previous,/Configuration changed/);
assert.doesNotMatch(panel.innerHTML,/40 seconds/);
api=async()=>{throw new Error('invalid settings');};
await assert.rejects(refreshRequestPreview(config,{draft:true}),/invalid settings/);
assert.match(panel.innerHTML,/plan unavailable/);assert.doesNotMatch(panel.innerHTML,/Updating/);
assert.match(renderCorrectnessExecution({...config,checks:['load']}),/Select Answer correctness/);
assert.equal(renderCorrectnessExecution({...config,request_pack:{cases:[]}}),'');
updateCorrectnessExecution({...config,request_pack:{cases:[]}});assert.equal(panel.hidden,true);
updateCorrectnessExecution(config);assert.equal(panel.hidden,false);assert.match(panel.innerHTML,/plan unavailable/);
updateCorrectnessExecution({...config,request_pack:{cases:[]}});
state.requestPreview.errors=[{fields:['test_options.correctness.samples'],msg:'Per-check request overrides are unavailable.'}];
realSyncInlineErrors();assert.equal(panel.hidden,false);
updateCorrectnessExecution({...config,request_pack:{cases:[]}});assert.equal(panel.hidden,false);
revealSetupStep=revealCatalogueCheck=()=>{};focusSetting('test_options.correctness.samples');assert.equal(panel.focused,true);
state.requestPreview.errors=[];realSyncInlineErrors();assert.equal(panel.hidden,true);
''', config)


def test_my_questions_uses_backend_pass_counts_and_load_timeout_without_claiming_four_in_progress():
    config = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
                       checks=['correctness', 'load'], request_source='request_pack',
                       request_timeout_seconds=19, load_test={'request_timeout_seconds': 73},
                       request_pack={'version': 1, 'cases': [{'fixture_id': 'one', 'messages': [{'role': 'user', 'content': 'Q'}], 'scorer': 'exact', 'expected': 'A'}]})
    plan = json.dumps(request_plan(config))
    result = browser_logic('state.requestPreview={status:"current",revision:state.configRevision,route:state.routeVersion,plan:' + plan + r'''};
const check=bootstrap.checks.find(check=>check.id==='correctness');
console.log(JSON.stringify({execution:renderCorrectnessExecution(config),summary:effectiveCheckSummary(check,config),markup:renderTestCard(check,config),emptyMarkup:renderTestCard(check,{...config,request_pack:{cases:[]}})}));
''', config)
    assert '1 request in an individual-question pass, one at a time' in result['execution']
    assert 'selected Load test traffic' in result['execution']
    assert '73 seconds · Load test settings' in result['execution']
    assert 'uses Load test settings' in result['execution']
    assert '4 in progress' not in result['execution']
    assert 'each once' not in result['summary']
    assert 'These questions and answer checks are shared with Load test.' in result['markup']
    assert 'per-check request-setting overrides' not in result['markup']
    tree = Markup(result['emptyMarkup'])
    execution, = [node for node in tree.nodes if 'data-correctness-execution' in node]
    assert 'hidden' in execution


def test_evaluator_help_distinguishes_its_model_and_limits_without_capacity_policy_copy():
    result = browser_logic(r'''
config.request_source='request_pack';config.request_pack={cases:[{scorer:'judge'}]};
config.judging={judges:[{name:'judge',model:'evaluator-model',url:'http://evaluator/v1'}]};
console.log(JSON.stringify(renderJudging(config)));
''')
    for label in ('Evaluator model', 'Evaluator endpoint URL', 'Evaluator requests in progress', 'Timeout per evaluator response (seconds)', 'Total evaluation duration (seconds)', 'Evaluator output token limit'):
        assert label in result
    assert 'A timeout leaves a missing score rather than an incorrect answer.' in result
    assert 'Questions, expected answers and model answers are sent to your configured evaluators.' in result
    assert 'implementation constraint' not in result
    assert 'demonstrated delivery capacity' not in result
