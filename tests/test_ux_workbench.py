"""Behavior contracts for the redesigned endpoint-check workbench."""

from tests.test_ux_catalogue import browser_logic
from tests.test_ux_setup import run_browser_logic


def test_editing_from_review_returns_directly_after_validation():
    run_browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';state.setupStep=3;
tokenizerSetupReady=()=>true;
nodes['#new-run-form']={};
const validations=[];
validateRunForm=(form,step)=>{validations.push(step);return true;};
validateSetupChecks=()=>validations.push('checks');
captureFormDraft=updateBounds=()=>{};
await listeners.click({target:{closest:q=>q==='[data-setup-step]'?{dataset:{setupStep:'1',reviewEdit:'true'}}:null}});
assert.equal(state.setupStep,1);assert.equal(state.returnToReview,true);
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'setup-next'}}:null}});
assert.equal(validations.join(','),'1,2,checks');
assert.equal(state.setupStep,3);assert.equal(state.returnToReview,false);
state.setupStep=2;state.returnToReview=true;validateRunForm=()=>false;
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'setup-next'}}:null}});
assert.equal(state.setupStep,2);assert.equal(state.returnToReview,true);
""")


def test_return_from_advanced_editor_at_launch_keeps_step_and_reveals_form():
    run_browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';state.setupStep=3;state.configView='json';
nodes['#config-panel-form']=element();
syncConfigView=syncSetupStep=()=>{};
for(const returnToReview of [false,true]){
 state.returnToReview=returnToReview;state.configView='json';
 await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'setup-next'}}:null}});
 assert.equal(state.setupStep,3);assert.equal(state.configView,'form');assert.equal(state.returnToReview,false);
}
""")


def test_run_blueprint_uses_current_allocations_and_never_invents_results():
    run_browser_logic(r"""
state.config={checks:['access','correctness'],targets:[{model:'<private>',url:'http://local/v1'}],max_requests:12,max_duration_seconds:30,concurrency:2};
state.jsonDraft=state.jsonBase='synced';
const original=JSON.stringify(state.config);
const plan={count_kind:'exact',planned_total:4,request_limit:12,required_request_limit:4,
 rows:[{id:'initial',title:'Connection check',requests_per_target:1,kind:'exact',summary:'Shared connection request.',source:'generated'},
 {id:'access',title:'Endpoint response',requests_per_target:0,kind:'exact',summary:'Reused.',source:'generated'},
 {id:'correctness',title:'Known answers',requests_per_target:3,kind:'exact',summary:'Three questions.',source:'generated'}]};
api=()=>{throw new Error('Preview rendering must not send traffic');};
let html=renderRunBlueprint(state.config,plan);
assert.match(html,/4 requests planned/);assert.match(html,/Maximum requests/);
assert.match(html,/data-plan-row="initial"/);assert.doesNotMatch(html,/data-plan-row="access"/);
assert.doesNotMatch(html,/data-plan-check=/);assert.match(html,/per endpoint/);
assert.doesNotMatch(html,/Play example|800 ms|Passed/);
html=renderRunBlueprint(state.config,null);assert.doesNotMatch(html,/4 requests planned|data-plan-row=/);
assert.match(html,/Calculating traffic/);
plan.rows[0].requests_per_target=null;plan.rows[0].kind='bounded';
plan.rows[2].kind='estimate';html=renderRunBlueprint(state.config,plan);
assert.match(html,/Adaptive/);assert.match(html,/~ 3/);
assert.equal(JSON.stringify(state.config),original);
""")


def test_check_choices_precede_settings_without_changing_the_run():
    browser_logic(r"""
config.test_options.correctness={samples:11,limits:{min_correctness:.8}};
const original=JSON.stringify(config),markup=renderSharedRunSettings(config)+renderCheckSelection(config);
assert(markup.indexOf('id="shared-settings"')<markup.indexOf('id="test-selection"'));
assert.equal((markup.match(/name="check"/g)||[]).length,bootstrap.checks.length);
assert.equal((markup.match(/id="shared-settings"/g)||[]).length,1);
assert.equal(JSON.stringify(config),original);
""")


def test_intent_browsing_never_selects_checks_or_sends_requests():
    browser_logic(r"""
config.test_options.correctness={samples:11};
const original=JSON.stringify(config),revision=state.configRevision;
nodes['#check-search']={value:'cache'};
let rendered=0;syncTestControls=()=>rendered++;
api=()=>{throw new Error('Browsing must not send traffic');};
for(const category of ['answers','speed','reliability']){
  state.catalogueSearch='cache';
  await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'check-intent'}}:
    q==='[data-category]'?{dataset:{category}}:null}});
  assert.equal(state.catalogueCategory,category);assert.equal(state.catalogueFilter,'all');
  assert.equal(state.catalogueSearch,'');assert.equal(nodes['#check-search'].value,'');
  assert.equal(JSON.stringify(config),original);assert.equal(state.configRevision,revision);
}
assert.equal(rendered,3);browseCheckIntent('unknown');assert.equal(rendered,3);
""")


def test_runtime_shortcuts_preserve_manual_configuration_and_invalidate_discovery():
    run_browser_logic(r"""
const form={elements:{
  url:element({value:'http://remote/v1',setCustomValidity(){}}),
  model:element({value:'manual-model'}),api_key_env:element({value:'MY_KEY'}),
  baseline_id:element({value:'baseline'}),mode:element({value:'normal'}),restart_target:element({value:'primary'})}};
nodes['#new-run-form']=form;nodes['#config-error']=element();
state.config={targets:[{name:'primary',url:'http://remote/v1',model:'manual-model',api_key_env:'MY_KEY',headers_env:{'X-Auth':'HEADER_KEY'}},
 {name:'secondary',url:'http://second/v1',model:'other'}],checks:['access','correctness'],max_requests:123,
 test_options:{correctness:{samples:17}},request_options:{temperature:.3}};
const original=structuredClone(state.config);
state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'current'};state.modelDiscovery={status:'connected'};
renderModelDiscovery=renderRequestPreview=updateBounds=()=>{};
readForm=()=>{const draft=structuredClone(state.config);draft.targets[0].url=form.elements.url.value;
 draft.targets[0].model=form.elements.model.value;return {config:draft};};
api=()=>{throw new Error('A runtime shortcut must not send traffic');};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'runtime-shortcut'}}:
 q==='[data-runtime]'?{dataset:{runtime:'Ollama'}}:null}});
assert.equal(form.elements.url.value,'http://localhost:11434/v1');
assert.equal(form.elements.model.value,'manual-model');assert.equal(form.elements.api_key_env.value,'MY_KEY');
assert.equal(state.modelDiscovery,null);assert.equal(state.requestPreview.status,'stale');
const expected=structuredClone(original);expected.targets[0].url=form.elements.url.value;
assert.equal(JSON.stringify(state.config),JSON.stringify(expected));assert(form.elements.url.focused);
assert.equal(state.prefillBaseline,'baseline');
const revision=state.configRevision;useRuntimeShortcut('Ollama');assert.equal(state.configRevision,revision);
state.autoModel='manual-model';state.modelDiscovery={status:'connected'};useRuntimeShortcut('vLLM');
assert.equal(form.elements.url.value,'http://localhost:8000/v1');assert.equal(form.elements.model.value,'');
assert.equal(state.autoModel,null);assert.equal(state.modelDiscovery,null);
state.jsonDraft='unapplied';const before=JSON.stringify(state.config);useRuntimeShortcut('SGLang');
assert.equal(JSON.stringify(state.config),before);assert.match(nodes['#config-error'].textContent,/Apply or discard/);
""")


def test_history_search_combines_with_outcome_and_escapes_terms():
    run_browser_logic(r"""
nodes['#main']=element();nodes['#run-filter']=element();nodes['#run-search']=element();
state.runs=[{id:'alpha',run_id:'external-one',state:'completed',overall:'fail',targets:[{name:'Production',model:'Llama',url:'http://gpu-a/v1'}]},
 {id:'beta',state:'completed',overall:'pass',targets:[{name:'Staging',model:'Llama',url:'http://gpu-b/v1'}]},
 {id:'gamma',state:'running',targets:[{name:'Canary',model:'Qwen'}]}];
for(const query of [' ALPHA ','external-one','Production','GPU-A']){
 state.historySearch=query;assert.equal(filteredRuns().map(run=>run.id).join(','),'alpha');
}
state.historySearch='llama';state.filter='pass';assert.equal(filteredRuns().map(run=>run.id).join(','),'beta');
state.filter='active';state.historySearch='qWen';assert.equal(filteredRuns().map(run=>run.id).join(','),'gamma');
state.filter='pass';renderRuns();assert(!nodes['#main'].innerHTML.split('class="history-layout')[1].includes('href="#runs/alpha"'));
state.historySearch='"><img src=x>';renderRuns();
assert.match(nodes['#main'].innerHTML,/&quot;&gt;&lt;img src=x&gt;/);assert.doesNotMatch(nodes['#main'].innerHTML,/<img src=x>/);
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'clear-filter'}}:null}});
assert.equal(state.filter,'all');assert.equal(state.historySearch,'');assert.equal(filteredRuns().length,3);
""")


def test_history_poll_redraw_keeps_search_selection_and_filter_focus():
    run_browser_logic(r"""
nodes['#main']=element();nodes['#run-filter']=element({id:'run-filter'});
nodes['#run-search']=element({id:'run-search',selectionStart:1,selectionEnd:3,
 setSelectionRange(start,end){this.restored=[start,end];}});
state.runs=[{id:'first',state:'completed',overall:'pass',targets:[]}];state.historySearch='test';
document.activeElement=nodes['#run-search'];
refreshRuns=async()=>{state.runs.push({id:'second',state:'completed',overall:'fail',targets:[]});};
schedulePoll=connectionError=()=>{};
await pollRuns(state.routeVersion);assert(nodes['#run-search'].focused);
assert.equal(nodes['#run-search'].restored.join(','),'1,3');
document.activeElement=nodes['#run-filter'];await pollRuns(state.routeVersion);
assert(nodes['#run-filter'].focused);
""")


def test_single_navigator_preserves_target_identity_and_focuses_selected_evidence():
    browser_logic(r"""
config.targets=[{name:'local',model:'one'},{name:'staging <b>',model:'two'}];config.checks=['access','correctness','tools'];
const check=(target,id,status)=>({target,id,status,title:id,required:true,summary:'Recorded '+status,metrics:{},evidence_ids:[]});
const report={checks:[check('local','access','pass'),check('staging <b>','access','fail'),
 check('local','tools','skipped'),check('local','json','skipped'),check('local','baseline','fail')],
 manifest:{config},requests:[]};
state.detail={report};state.selectedCheck='staging <b>:access';
const markup=renderChecks(report);
assert.match(markup,/data-check="local:access"/);assert.match(markup,/data-check="staging &lt;b&gt;:access"/);
assert.doesNotMatch(markup,/result-check-strip/);
assert.match(markup,/data-check="local:tools"/);assert.doesNotMatch(markup,/data-check="local:baseline"/);
assert.match(markup,/>[^<]*staging &lt;b&gt;/);
nodes['#result-content']={};let selected;
renderCheck=(value)=>{selected=value;return 'Evidence for '+value.target;};
let focused=false;
nodes['.check-detail h2']={setAttribute(){},focus(){focused=true;}};
const clicked={dataset:{check:'local:access'},closest:q=>q==='.result-check-strip'?{}:null};
await listeners.click({target:{closest:q=>q==='[data-check]'?clicked:null}});
assert.equal(state.selectedCheck,'local:access');assert.equal(selected.target,'local');
assert.match(nodes['#result-content'].innerHTML,/Evidence for local/);assert(focused);
""")


def test_request_inspector_prioritizes_answers_without_losing_measurements():
    browser_logic(r"""
nodes['#request-dialog']={showModal(){this.shown=true;}};
const record={id:'request-one',fixture_id:'example',target:'local',scenario:'correctness',status:'completed',
 score:false,score_message:'Expected <Paris>',output:'Actual <London>',output_chars:15,reasoning:'Reason <text>',
 evaluation:{scorer:'exact',expected:'Reference <Paris>',expected_provided:true,messages:[{role:'user',content:'Capital?'}]},
 traffic_stage:0,traffic_rate_rps:3,traffic_class:'short',dispatch_lag_ms:7,elapsed_ms:123,first_output_ms:11,
 last_output_ms:119,first_reasoning_ms:9,max_stream_gap_ms:14,input_tokens:22,output_tokens:33,
 http_status:200,finish_reason:'stop',valid:true,stream:true,stream_terminated:true};
state.detail={report:{manifest:{config},requests:[record]}};const before=JSON.stringify(record);
openRequest('request-one');assert(nodes['#request-dialog'].shown);
const markup=nodes['#request-dialog'].innerHTML;
const timing=markup.indexOf('<details class="request-timing">');assert(timing>0);
assert(markup.indexOf('Expected answer')<timing);assert(markup.indexOf('Actual answer')<timing);
assert.match(markup,/Reference &lt;Paris&gt;/);assert.match(markup,/Actual &lt;London&gt;/);
assert.match(markup,/<details class="request-timing"><summary>Timing & transport details<\/summary>/);
const measurements=markup.slice(timing,markup.indexOf('</details>',timing));
for(const label of ['Arrival rate','Local dispatch delay','Elapsed','First answer','Last answer',
 'First reasoning','Longest stream gap','Input / output tokens','HTTP / finish reason'])assert(measurements.includes(label));
for(const value of ['3 requests/s','7 ms','123 ms','11 ms','119 ms','9 ms','14 ms','22 / 33','200 / stop'])assert(measurements.includes(value));
assert.match(markup,/<details class="request-reasoning"><summary>Reasoning output<\/summary>/);
assert.match(markup,/Reason &lt;text&gt;/);assert.equal(JSON.stringify(record),before);
delete record.output;openRequest('request-one');
assert.match(nodes['#request-dialog'].innerHTML,/Body not retained/);
assert.match(nodes['#request-dialog'].innerHTML,/123 ms/);
""")


def test_default_route_opens_setup_and_history_is_explicit():
    run_browser_logic(r"""
nodes['#main']=element();let rendered=0,scheduled=0,requests=0;
clearPoll=updateNav=()=>{};renderRuns=()=>rendered++;schedulePoll=()=>scheduled++;
refreshRuns=async()=>{};api=()=>{requests++;throw new Error('Navigation must not send endpoint traffic');};
location.hash='';state.runs=[];state.active=null;await route();
assert.equal(location.hash,'#new');assert.equal(rendered,0);assert.equal(scheduled,0);
location.hash='#runs';await route();assert.equal(location.hash,'#runs');assert.equal(rendered,1);
location.hash='';state.runs=[{id:'saved'}];await route();assert.equal(location.hash,'#new');assert.equal(rendered,1);
location.hash='';state.runs=[];state.active='active-run';await route();assert.equal(location.hash,'#runs/active-run');assert.equal(rendered,1);
assert.equal(requests,0);
""")
