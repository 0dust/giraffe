"""Answer correctness explains its actual workload and inherited request settings."""

import json

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_concurrent_load_ui_review import FORM
from tests.test_run_limits_phase6_ui import Markup
from tests.test_ux_catalogue import browser_logic


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
const loadOnly={...config,checks:['load'],load_test:structuredClone(bootstrap.load_test.defaults)};
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
    inheritance = 'Leave blank to use the shown default. Enter a value to override it.'
    assert result['markup'].count(inheritance) == 1
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
