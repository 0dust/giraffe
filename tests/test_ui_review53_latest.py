"""Latest PR 53 feedback: preparation, limits and identifiable conditions."""

from pathlib import Path

import pytest

from giraffe.models import RunConfig

from tests.test_concurrent_load_ui_review import FORM
from tests.test_run_limits_phase6_ui import Markup, rendered_form
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks
from tests.test_ux_setup import run_browser_logic


def test_shared_timeout_and_load_override_are_visible_and_independent():
    browser_logic(FORM + r'''
const shared=renderSharedRunSettings(config),load=renderLoadEditor(config);
assert.match(shared,/<h3>Request default<\/h3>/);
assert.match(shared,/Request timeout \(seconds\)/);
assert.match(shared,/name="request_timeout_seconds"[^>]*value="300"/);
assert.match(load,/Send new requests for \(seconds\)/);
assert.doesNotMatch(load,/load_schedule_finish_seconds|Wait for unfinished|Duration per workload/);
assert.equal(settingDestination('request_timeout_seconds').name,'request_timeout_seconds');
form.elements.request_timeout_seconds.value='240';
form.elements.load_request_timeout_seconds.value='75';
const saved=readForm(false).config;
assert.equal(saved.request_timeout_seconds,240);assert.equal(saved.load_test.request_timeout_seconds,75);
assert.match(effectiveCheckSummary(bootstrap.checks.find(c=>c.id==='load'),saved),/Request timeout 1m 15s/);
assert.match(effectiveCheckSummary(bootstrap.checks.find(c=>c.id==='access'),saved),/Request timeout/);
''')


def test_preparation_precedes_report_tabs_and_keeps_failure_distinct():
    run_render_checks(r'''
state.detail={id:'preparing',state:'running',config:{targets:[{name:'local',model:'m'}]},events:[],progress:{event:'preparation_started',target:'local',elapsed_seconds:4,preparation:{step:'Loading matching tokenizer',status:'running',started_elapsed_seconds:4,elapsed_seconds:0}}};
renderLive();let html=nodes['#main'].innerHTML;
assert(html.indexOf('run-preparation')<html.indexOf('report-toolbar'));
assert.match(html,/Preparing test traffic/);assert.match(html,/Loading matching tokenizer/);
assert.match(html,/Test traffic for this preparation has not started/);
const clock=[...state.preparationClocks.values.values()][0];clock.at-=9000;
assert.match(renderPreparation(),/9s/);
state.detail.state='completed';state.detail.progress={};state.detail.report={observations:{targets:{local:{preparation:{step:'Local tokenization',status:'unavailable',elapsed_seconds:2,reason:'Matching tokenizer not cached'}}}}};
html=renderPreparation();assert.match(html,/Preparation unavailable/);assert.match(html,/Matching tokenizer not cached/);
assert.match(html,/The affected test traffic did not run/);assert.doesNotMatch(html,/Preparing test traffic/);
state.detail.report.observations.targets.local.preparation={scenario:'performance_evidence_probe',step:'Streamed token and usage capability',status:'unavailable',elapsed_seconds:1,reason:'Endpoint does not provide token boundaries'};
html=renderPreparation();assert.match(html,/Unsupported measurements stay unknown/);assert.doesNotMatch(html,/test traffic did not run/);
''')


def test_condition_identity_effective_limits_and_small_mosaic_are_explicit():
    run_render_checks(r'''
state.detail={id:'small',state:'running',config:{max_duration_seconds:600}};
const row={condition:{id:'load_primary_combinations_short_short_concurrent_c4',check:'load',parameters:{concurrency:4,request_timeout_seconds:75,duration_seconds:20}},planned:null,planned_condition:true,started:0};
const html=renderTests([],{live:true,conditions:[row]});
assert.match(html,/--condition-display-width:84px/);assert.match(html,/>○<\/text>/);
assert.match(html,/Condition #01/);assert.match(html,/Request timeout: 1m 15s/);assert.match(html,/Send new requests for: 20s/);
assert.match(html,/Short input \/ short output · 4 at once/);
renderTests([],{live:true,conditions:[row,{...row,condition:{id:'extra',label:'Extra recorded work'},planned_condition:false}]});
assert.match(conditionIdentity([...conditionViews.values()][1]),/Additional recorded condition/);
''')
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    assert '.condition-spot.current polygon { stroke:var(--spot); }' in css
    assert 'stroke-width:2px; vector-effect:non-scaling-stroke' in css


def test_preparation_history_retains_input_failure_after_later_success_and_feed_eviction():
    run_render_checks(r'''
const failed={scenario:'context_preparation',step:'Preparing context input locally',status:'unavailable',elapsed_seconds:2,reason:'Context input could not be sized'};
const success={scenario:'load_preparation_short_short_answer',step:'Preparing load input locally',status:'completed',elapsed_seconds:1};
const optional={scenario:'runtime_metrics_discovery',step:'Runtime metrics',status:'unavailable',reason:'No metrics exporter'};
state.detail={id:'history',state:'running',events:[],progress:{event:'request_completed'},live_preparations:[{target:'local',...failed},{target:'local',...optional},{target:'local',...success}]};
let html=renderPreparation();
assert.equal((html.match(/class="run-preparation"/g)||[]).length,2);
assert.match(html,/Preparation complete/);assert.match(html,/Earlier preparation/);
assert.match(html,/Context input could not be sized/);assert.match(html,/The affected test traffic did not run/);
assert.doesNotMatch(html,/No metrics exporter/);
state.detail={id:'history',state:'completed',events:[],report:{observations:{targets:{local:{preparations:{context_preparation:failed,runtime_metrics_discovery:optional,load_preparation_short_short_answer:success},preparation:success}}}}};
html=renderPreparation();
assert.equal((html.match(/class="run-preparation"/g)||[]).length,1);
assert.match(html,/<details class="preparation-status">/);
assert.match(html,/Context input could not be sized/);assert.match(html,/Preparation complete/);
state.detail.id='another-run';state.detail.report={};assert.equal(renderPreparation(),'');
''')


def test_recorded_termination_reason_retains_exact_limit_and_detail():
    run_render_checks(r'''
const html=renderRequestTermination({request_timeout_seconds:300,termination_reason:'run_duration_limit',termination_limit_seconds:12.5,error:'Remaining run traffic budget exhausted'});
assert.match(html,/Configured request timeout/);assert.match(html,/5m 0s/);
assert.match(html,/Run traffic duration limit reached/);assert.match(html,/12.5s/);
assert.match(html,/Remaining run traffic budget exhausted/);
assert.equal(renderRequestTermination({}), '');
''')


def test_estimated_input_notice_survives_later_optional_preparation():
    run_render_checks(r'''
state.detail={id:'estimated',state:'completed',live_preparations:[
{target:'fresh',scenario:'load_preparation_short_short_answer',step:'Preparing input locally',status:'completed',estimated:true,reason:'Input size is estimated at 4 characters per token.'},
{target:'fresh',scenario:'runtime_metrics_discovery',step:'Runtime metrics',status:'completed'}]};
const html=renderPreparation();
assert.match(html,/Preparation complete · estimated input size/);
assert.match(html,/Input size is estimated at 4 characters per token/);
assert.doesNotMatch(html,/The affected test traffic did not run/);
''')


@pytest.mark.parametrize("token_id", ["auto", "saved/tokenizer"])
def test_connection_setup_has_one_editable_token_id_box(token_id):
    config = RunConfig(targets=[{"name": "saved", "url": "http://localhost/v1", "model": "test", "tokenizer": token_id}])
    connection = rendered_form(config).split('data-setup-panel="2"', 1)[0]
    assert connection.count('name="tokenizer"') == 1
    assert 'Token ID / tokenizer</label>' in connection
    tree = Markup(connection)
    tokenizer, = tree.named('tokenizer')
    assert any(parent.get('class') == 'form-field' for parent in tokenizer['parents'])
    assert tokenizer['aria-describedby'] == 'tokenizer-status'
    assert 'Token counting' not in connection
    assert 'aria-controls="tokenizer-options"' in connection
    assert 'role="combobox"' in connection
    assert 'role="listbox"' in connection
    assert tokenizer["value"] == ("Automatic" if token_id == "auto" else token_id)
    assert 'data-token-id="builtin"' in connection
    assert 'tokenizer-help' not in connection
    assert 'Tokenizer files download' not in connection
    assert 'Automatic checks the model name' not in connection
    assert 'class="tokenizer-options" role="listbox" aria-label="Token ID options"' in connection
    assert connection.count('role="option" tabindex="-1"') == 2
    assert 'id="tokenizer-status" class="hint" role="status" aria-live="polite"' in connection
    assert 'retry-tokenizer' not in connection
    assert 'automatically collects supported timing' in connection
    assert 'name="metrics_url"' in connection and 'Server metrics URL (optional)' in connection
    assert 'up to 3 one-token compatibility probes sharing the configured request timeout, bounded by remaining run time and spare request budget' in connection
    assert 'separate from the planned test requests' in connection
    browser_logic(r'''
const gpu=renderTestCard(bootstrap.checks.find(c=>c.id==='gpu'),config);
assert.doesNotMatch(gpu,/name="metrics_url"|name="host_metrics_url"/);
assert.match(gpu,/collected automatically during every run/);
assert.match(gpu,/Automatic metrics discovery/);assert.doesNotMatch(gpu,/Metrics URL needed/);
''')


def test_form_preserves_same_box_tokenizer_selection():
    browser_logic(FORM + r'''
form.elements.tokenizer={value:'Automatic'};
assert.equal(readForm(false).config.targets[0].tokenizer,'auto');
form.elements.tokenizer.value='builtin';
assert.equal(readForm(false).config.targets[0].tokenizer,'builtin');
form.elements.tokenizer.value='qwen2.5-7b';
assert.equal(readForm(false).config.targets[0].tokenizer,'qwen2.5-7b');
assert.equal(readForm(false).config.targets[0].model,state.config.targets[0].model);
''')


def test_builtin_notice_survives_later_optional_preparation():
    run_render_checks(r'''
state.detail={id:'builtin',state:'completed',live_preparations:[
{target:'fresh',scenario:'load_preparation_short_short_answer',step:'Preparing input locally',status:'completed',builtin:true,reason:'Using built-in tiktoken o200k_base. Reference counts, not server usage.'},
{target:'fresh',scenario:'runtime_metrics_discovery',step:'Runtime metrics',status:'completed'}]};
const html=renderPreparation();
assert.match(html,/Preparation complete · built-in tokenizer/);
assert.match(html,/Reference counts, not server usage/);
''')



TOKENIZER_FORM = FORM + r'''
form.elements.tokenizer={name:'tokenizer',value:'Automatic'};
nodes['#tokenizer-status']={textContent:'',classList:{toggle(){}}};
syncSetupStep=()=>{};
configChanged=updateBounds=()=>{throw Error('Preview must not change configuration or invalidate the plan');};
'''


@pytest.mark.parametrize("choice", ["Automatic", "builtin", "auto", ""])
def test_default_tokenizer_never_previews_gates_or_changes_the_plan(choice):
    browser_logic(TOKENIZER_FORM + r'''
form.elements.tokenizer.value=CHOICE;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{planned_total:10},config:readForm(true).config};
const savedPlan=state.requestPreview;
api=()=>{throw Error('Automatic and builtin must not send preview requests');};
let timers=0;setTimeout=()=>{timers++;};
scheduleTokenizerSelection({immediate:true});renderTokenizerSelection();
assert.equal(tokenizerSetupReady(),true);assert.equal(state.tokenizerSelection,null);
assert.equal(nodes['#tokenizer-status'].textContent,'');assert.equal(timers,0);
assert.equal(state.requestPreview,savedPlan);assert.equal(state.requestPreview.status,'current');
'''.replace('CHOICE', repr(choice)))


def test_typed_tokenizer_verifies_exact_name_and_does_not_invalidate_plan():
    browser_logic(TOKENIZER_FORM + r'''
form.elements.tokenizer.value='Qwen/Qwen2.5-7B';
let called=null;
api=async(path,options)=>{called={path,body:JSON.parse(options.body)};return {status:'ready',tokenizer:'Qwen/Qwen2.5-7B',message:''};};
scheduleTokenizerSelection({immediate:true});assert.equal(tokenizerSetupReady(),false);
assert.match(nodes['#tokenizer-status'].textContent,/Resolving and loading/);
const pending=state.tokenizerSelection;
await resolveTokenizerSelection(pending.key,state.tokenizerSequence,state.routeVersion);
assert.equal(called.path,'/api/targets/tokenizer');assert.deepEqual(called.body,{token_id:'Qwen/Qwen2.5-7B'});
assert.equal(tokenizerSetupReady(),true);assert.equal(nodes['#tokenizer-status'].textContent,'');
assert.equal(readForm(false).config.targets[0].tokenizer,'Qwen/Qwen2.5-7B');
''')


def test_typed_tokenizer_failure_and_timeout_show_backend_error_and_clear_on_edit():
    browser_logic(TOKENIZER_FORM + r'''
form.elements.tokenizer.value='missing/model';
for(const message of ['Could not load tokenizer "missing/model": Install giraffe-llm[hf-tokenizer] or pick Automatic.',
 'Could not load tokenizer "missing/model": setup timed out.']){
 state.tokenizerSelection=null;
 api=async()=>{throw Error(message);};scheduleTokenizerSelection();
 await resolveTokenizerSelection(state.tokenizerSelection.key,state.tokenizerSequence,state.routeVersion);
 assert.equal(tokenizerSetupReady(),false);assert.equal(state.tokenizerSelection.status,'error');
 assert.equal(nodes['#tokenizer-status'].textContent,message);
 assert.equal(nodes['#tokenizer-status'].className,'notice error');
}
form.elements.tokenizer.value='different/model';scheduleTokenizerSelection();
assert.equal(state.tokenizerSelection.status,'pending');assert.match(nodes['#tokenizer-status'].textContent,/Resolving and loading/);
assert.equal(nodes['#tokenizer-status'].className,'hint');assert.equal(tokenizerSetupReady(),false);
form.elements.tokenizer.value='Automatic';scheduleTokenizerSelection();
assert.equal(tokenizerSetupReady(),true);assert.equal(nodes['#tokenizer-status'].textContent,'');
''')


@pytest.mark.parametrize("replacement", ["builtin", "other/model"])
def test_superseded_tokenizer_response_cannot_overwrite_current_selection(replacement):
    browser_logic(TOKENIZER_FORM + r'''
form.elements.tokenizer.value='first/model';let finish;
api=()=>new Promise(resolve=>{finish=resolve;});scheduleTokenizerSelection();
const old=resolveTokenizerSelection(state.tokenizerSelection.key,state.tokenizerSequence,state.routeVersion);
form.elements.tokenizer.value=REPLACEMENT;scheduleTokenizerSelection();
finish({status:'ready',tokenizer:'first/model',message:'Wrong result'});await old;
assert.doesNotMatch(nodes['#tokenizer-status'].textContent,/Wrong result/);
if(REPLACEMENT==='builtin'){
 assert.equal(state.tokenizerSelection,null);assert.equal(tokenizerSetupReady(),true);
}else{
 assert.equal(state.tokenizerSelection.status,'pending');assert.equal(tokenizerSetupReady(),false);
 api=async()=>({status:'ready',tokenizer:'other/model',message:''});
 await resolveTokenizerSelection(state.tokenizerSelection.key,state.tokenizerSequence,state.routeVersion);
 assert.equal(tokenizerSetupReady(),true);
}
'''.replace('REPLACEMENT', repr(replacement)))


def test_typed_pending_and_error_gate_next_and_launch_but_defaults_never_do():
    run_browser_logic(r"""
const form={elements:{model:element({value:'test-model'}),tokenizer:element({value:'typed/model'})}};
nodes['#new-run-form']=form;
for(const selector of ['#setup-panel-1','#start-run','#setup-navigation-hint','[data-action="setup-next"]','[data-action="setup-back"]'])nodes[selector]=element();
state.jsonDraft=state.jsonBase='synced';
currentRequestPlan=()=>({});currentSettingErrors=currentResourceErrors=()=>[];
for(const status of ['error','pending','ready',null]){
 state.tokenizerSelection=status?{key:tokenizerSelectionKey(),route:state.routeVersion,status}:null;
 state.setupStep=1;syncSetupStep();assert.equal(nodes['[data-action="setup-next"]'].disabled,status!=='ready');
 state.setupStep=3;syncSetupStep();assert.equal(nodes['#start-run'].disabled,status!=='ready');
}
for(const choice of ['Automatic','builtin']){
 form.elements.tokenizer.value=choice;
 state.setupStep=1;syncSetupStep();assert.equal(nodes['[data-action="setup-next"]'].disabled,false);
 state.setupStep=3;syncSetupStep();assert.equal(nodes['#start-run'].disabled,false);
}
form.elements.model.value='';state.setupStep=1;syncSetupStep();
assert.equal(nodes['[data-action="setup-next"]'].disabled,true);assert.equal(nodes['[data-action="setup-next"]'].hidden,true);
""")


def test_typed_error_cannot_bypass_launch_and_default_can_launch():
    browser_logic(r'''
const form={elements:{model:{value:'test-model'},tokenizer:{value:'bad/model'}}};nodes['#new-run-form']=form;
nodes['#form-error']={hidden:true,textContent:''};nodes['#start-run']={};
state.setupStep=3;state.config.targets[0].tokenizer='bad/model';state.config.checks=['access'];
const message='Could not load tokenizer "bad/model": Invalid tokenizer.';
state.tokenizerSelection={key:tokenizerSelectionKey(),route:state.routeVersion,status:'error',message};
readForm=()=>({config:structuredClone(state.config),mode:'normal'});validateRunForm=()=>true;
localSettingErrors=currentSettingErrors=currentResourceErrors=()=>[];currentRequestPlan=()=>({});
refreshRequestPreview=async()=>{};focusFirstSettingError=()=>{};setActive=id=>{state.active=id;};
const paths=[];api=async(path,options)=>{paths.push(path);return path==='/api/config/validate'?{config:JSON.parse(options.body)}:{id:'started'};};
await startRun({preventDefault(){},target:form});
assert(!paths.includes('/api/runs'));assert.equal(nodes['#form-error'].textContent,message);assert.equal(nodes['#start-run'].disabled,true);
form.elements.tokenizer.value='Automatic';state.config.targets[0].tokenizer='auto';
await startRun({preventDefault(){},target:form});assert(paths.includes('/api/runs'));assert.equal(state.active,'started');
''')



def test_typed_tokenizer_request_timeout_ends_pending_and_clears_its_timer():
    browser_logic(TOKENIZER_FORM + r'''
form.elements.tokenizer.value='slow/model';
const timers=new Map();let sequence=0;
setTimeout=(callback,delay)=>{timers.set(++sequence,{callback,delay});return sequence;};clearTimeout=id=>timers.delete(id);
let signal;
fetch=async(path,options)=>{signal=options.signal;return new Promise((resolve,reject)=>{
 signal.addEventListener('abort',()=>reject(Object.assign(Error('aborted'),{name:'AbortError'})),{once:true});
});};
scheduleTokenizerSelection();const debounce=timers.get(sequence);timers.delete(sequence);
const checking=debounce.callback();
assert.equal(timers.size,1);const timeout=timers.get(sequence);
assert.equal(timeout.delay,bootstrap.tokenizer_setup_timeout_seconds*1000);
assert.equal(state.tokenizerSelection.status,'pending');timeout.callback();await checking;
assert.equal(signal.aborted,true);assert.equal(state.tokenizerSelection.status,'error');assert.equal(tokenizerSetupReady(),false);
assert.equal(nodes['#tokenizer-status'].textContent,'Tokenizer check timed out. Edit the Token ID or pick Automatic.');
assert.equal(nodes['#tokenizer-status'].className,'notice error');assert.equal(timers.size,0);
''')


def test_ui_tokenizer_deadline_uses_the_backend_setup_timeout():
    from giraffe.capabilities import capabilities
    from giraffe.local_tokenizer import SETUP_TIMEOUT_SECONDS

    assert capabilities()["tokenizer_setup_timeout_seconds"] == SETUP_TIMEOUT_SECONDS
