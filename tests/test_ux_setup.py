"""Behavior checks for progressive setup and explicit endpoint discovery."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.models import ConcurrentLoad
from giraffe.capabilities import capabilities


APP = Path(__file__).parents[1] / "src/giraffe/static/app.js"


def run_browser_logic(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise browser setup behavior")
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {positiveBoundErrors} from MODULE_PATH;
const nodes={},listeners={},groups={};
const loadDefaults=LOAD_DEFAULTS;const bootstrap=BOOTSTRAP;
function element(extra={}) {
  return {hidden:false,disabled:false,innerHTML:'',textContent:'',value:'',dataset:{},
    attributes:{},classList:{toggle(){}},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];},
    focus(){this.focused=true;},...extra};
}
const document={querySelector:q=>nodes[q],querySelectorAll:q=>groups[q] || [],addEventListener:(kind,fn)=>listeners[kind]=fn};
const context={assert,positiveBoundErrors,nodes,listeners,groups,element,loadDefaults,bootstrap,structuredClone,document,window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+'\nstate.bootstrap=bootstrap;\n(async()=>{'+BODY+'\n})()',context);
""".replace("MODULE_PATH", json.dumps(APP.with_name("form-config.mjs").as_uri())).replace("APP_PATH", json.dumps(str(APP))).replace("BODY", json.dumps(body)).replace("LOAD_DEFAULTS", json.dumps(ConcurrentLoad().model_dump())).replace("BOOTSTRAP", json.dumps(capabilities()))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_structured_conflict_links_reveal_focus_and_clear_inline_errors():
    run_browser_logic(r"""
state.config={checks:['concurrency'],targets:[{}],concurrency:40};state.jsonDraft=state.jsonBase='synced';
syncConfigView=syncSetupStep=syncTestControls=()=>{};
const shared=element({tagName:'DETAILS',open:false}),nested=element({tagName:'DETAILS',open:false,parentElement:shared});
const levels=element({name:'load_schedule_levels',parentElement:nested,scrollIntoView(){this.scrolled=true;}});
levels.attributes['aria-describedby']='field-load_schedule_levels-hint';levels.getAttribute=name=>levels.attributes[name];
const ceiling=element({name:'concurrency',parentElement:shared,scrollIntoView(){this.scrolled=true;}});
for(const [name,input] of [['load_schedule_levels',levels],['concurrency',ceiling]]){
  nodes['#field-'+name]=input;nodes['#field-'+name+'-error']=element();
}
nodes['#new-run-form']={elements:{load_schedule_levels:levels,concurrency:ceiling}};
nodes['[data-test-card="concurrency"]']=element();
const errors=[{field:'concurrent_load.schedule.levels',fields:['concurrent_load.schedule.levels','concurrency'],msg:'Concurrency level 64 exceeds Limit simultaneous requests 40.',values:{concurrency:40}}];
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors,error:errors[0].msg};
syncInlineErrors();
assert.equal(levels.attributes['aria-invalid'],'true');assert.match(levels.attributes['aria-describedby'],/field-load_schedule_levels-error/);
assert.match(levels.attributes['aria-describedby'],/field-load_schedule_levels-hint/);
assert.match(nodes['#field-concurrency-error'].textContent,/64.*40/);
let markup=renderRunBlueprint(state.config);
assert.match(markup,/Concurrency level 64 exceeds Limit simultaneous requests 40/);
assert.match(markup,/data-setting-path="concurrent_load.schedule.levels"[^>]*>Edit concurrency levels/);
assert.match(markup,/data-setting-path="concurrency"[^>]*>Edit Limit simultaneous requests/);
state.setupStep=3;await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'edit-setting'}}:q==='[data-setting-path]'?{dataset:{settingPath:'concurrent_load.schedule.levels'}}:null}});
assert.equal(state.setupStep,2);assert.equal(state.openTest,'concurrency');assert(shared.open&&nested.open);assert(levels.focused&&levels.scrolled);
assert.equal(levels.attributes['data-setting-highlight'],'true');
focusSetting('concurrency');assert(ceiling.focused&&ceiling.scrolled);
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{}};
syncInlineErrors();assert.equal(levels.attributes['aria-invalid'],undefined);assert(nodes['#field-load_schedule_levels-error'].hidden);
assert.equal(levels.attributes['aria-describedby'],'field-load_schedule_levels-hint');
assert.doesNotMatch(renderRunBlueprint(state.config),/Concurrency level 64 exceeds/);
""")


def test_checks_footer_reports_validation_errors_then_returns_to_a_current_plan():
    run_browser_logic(r"""
state.config={checks:['access','concurrency','correctness'],targets:[],concurrency:40,max_requests:300,max_duration_seconds:120};
state.jsonDraft=state.jsonBase='synced';state.setupStep=2;
nodes['#request-plan']=element();nodes['#dock-summary']=element();
const errors=[{field:'concurrent_load.schedule.levels',fields:['concurrent_load.schedule.levels','concurrency'],msg:'Concurrency level 64 exceeds Limit simultaneous requests 40.'}];
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors,error:errors[0].msg};
renderRequestPlan();assert.match(nodes['#dock-summary'].innerHTML,/3<\/strong> checks.*1 setting to fix/);assert.doesNotMatch(nodes['#dock-summary'].innerHTML,/Planning/);
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{errors,count_kind:'exact',planned_total:30,rows:[],warnings:[]}};
renderRequestPlan();assert.match(nodes['#dock-summary'].innerHTML,/1 setting to fix/); // Draft plans can carry blocking errors with HTTP 200.
state.requestPreview.errors=errors;state.requestPreview.status='error';state.requestPreview.error='Conflict';
state.requestPreview.errors.push({field:'max_duration_seconds',msg:'Stop test traffic after is too short.'});renderRequestPlan();assert.match(nodes['#dock-summary'].innerHTML,/2 settings to fix/);
configChanged();assert.match(nodes['#dock-summary'].innerHTML,/Planning/);
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{count_kind:'exact',planned_total:30,rows:[],warnings:[]}};
renderRequestPlan();assert.match(nodes['#dock-summary'].innerHTML,/30 requests planned/);assert.doesNotMatch(nodes['#dock-summary'].innerHTML,/to fix|Planning/);
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,error:'Server unavailable'};
renderRequestPlan();assert.match(nodes['#dock-summary'].innerHTML,/Plan unavailable/);
""")


def test_edit_setting_scrolls_the_field_and_error_clear_of_the_narrow_footer():
    run_browser_logic(r"""
state.config={checks:['concurrency']};state.jsonDraft=state.jsonBase='synced';state.setupStep=2;
syncConfigView=syncSetupStep=syncTestControls=()=>{};window.innerWidth=375;
const error=element({hidden:false,textContent:'Concurrency level 64 exceeds Limit simultaneous requests 40.'});
const group=element({style:{},scrollIntoView(options){assert.equal(options.block,'center');assert(!error.hidden);this.scrolled=true;}});
const input=element({parentElement:group,closest(q){return q==='.form-field'?group:null;},scrollIntoView(){throw Error('Scroll the complete field group, not only its input');},focus(options){assert(options.preventScroll);this.focused=true;}});
nodes['#new-run-form']={elements:{load_schedule_levels:input}};nodes['[data-test-card="concurrency"]']=element();
nodes['.setup-navigation']=element({getBoundingClientRect(){return {height:140};}});
focusSetting('concurrent_load.schedule.levels');assert(group.scrolled);assert(input.focused);assert.equal(group.style.scrollMarginBottom,'172px');
assert.equal(input.attributes['data-setting-highlight'],'true');
""")


def test_indexed_and_section_errors_reveal_check_editors_instead_of_json():
    run_browser_logic(r"""
state.config={checks:['concurrency']};state.jsonDraft=state.jsonBase='synced';
syncConfigView=syncSetupStep=syncTestControls=()=>{};
switchConfigView=()=>{throw Error('Should stay in the form');};
const levels=element({name:'load_schedule_levels'}),heading=element();
nodes['#new-run-form']={elements:{load_schedule_levels:levels}};
nodes['[data-test-card="concurrency"]']=element();nodes['#settings-concurrency [data-editor-title]']=heading;
assert.equal(settingDestination('concurrent_load.schedule.levels.2').name,'load_schedule_levels');
focusSetting('concurrent_load.schedule.levels.2');assert(levels.focused);assert.equal(state.openTest,'concurrency');
focusSetting('concurrent_load');assert(heading.focused);assert.equal(state.setupStep,2);
""")


def test_performance_session_conflicts_focus_visible_caps_and_selected_workload_rows():
    run_browser_logic(r"""
state.config={checks:['sessions'],sessions:{profile:'performance'}};state.jsonDraft=state.jsonBase='synced';
syncConfigView=syncSetupStep=syncTestControls=()=>{};
switchConfigView=()=>{throw Error('Should stay in the form');};
const cap=element(),ceiling=element(),workload=element(),details=element({tagName:'DETAILS',open:false});workload.parentElement=details;
nodes['#new-run-form']={elements:{sessions_profile:{value:'performance'},profile_sessions_max_tokens:cap,profile_sessions_concurrency:ceiling,
  session_cap_1:workload,session_workload_count:{value:'2'},session_enabled_0:{checked:false},session_enabled_1:{checked:true}}};
nodes['[data-test-card="sessions"]']=element();
focusSetting('sessions.max_tokens');assert(cap.focused);
focusSetting('sessions.concurrency');assert(ceiling.focused);
focusSetting('sessions.workloads.0.max_tokens');assert(workload.focused);assert(details.open);
assert.equal(settingDestination('sessions.workloads.0.max_tokens').name,'session_cap_1');
nodes['#field-session_cap_1']=workload;nodes['#field-session_cap_1-error']=element();
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors:[{
 field:'sessions.workloads.0.max_tokens',fields:['sessions.workloads.0.max_tokens','max_output_tokens'],msg:'Task cap exceeds output cap.'}]};
syncInlineErrors();assert.equal(workload.attributes['aria-invalid'],'true');
""")


def test_draft_plan_errors_block_next_and_start_without_blocking_draft_operations():
    run_browser_logic(r"""
state.config={checks:['capacity'],targets:[{model:'m'}],max_duration_seconds:120};state.jsonDraft=state.jsonBase='synced';state.setupStep=2;
syncConfigView=syncSetupStep=captureFormDraft=()=>{};validateRunForm=()=>true;
const input=element();nodes['#new-run-form']={elements:{max_duration_seconds:input}};nodes['#form-error']=element();nodes['#start-run']=element();nodes['#request-plan']=element();
readForm=()=>({config:structuredClone(state.config)});
const errors=[{field:'max_duration_seconds',fields:['max_duration_seconds'],msg:'Fixed windows need 180 seconds; limit is 120.'}];
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{errors}};
assert(!canStartRun());navigateSetup(3);assert.equal(state.setupStep,2);assert(input.focused);
let calls=[];api=async(path,options)=>{calls.push(path);return {config:JSON.parse(options.body),request_plan:{errors}};};
// Apply/import, dataset edits and export snapshots deliberately use the editable draft endpoint.
renderNew=toast=invalidateModelDiscovery=configChanged=()=>{};
await applyConfig(JSON.stringify(state.config),{importing:true});assert.equal(calls.at(-1),'/api/config/draft');
datasetEditingAllowed=()=>{};state.datasetSelection=[];
assert(await applyDataset({version:1,cases:[]}));assert.equal(calls.at(-1),'/api/config/draft');
state.jsonDraft=state.jsonBase='synced';const saved=await validatedSnapshot({draft:true});assert.equal(saved.config.max_duration_seconds,120);
calls=[];state.setupStep=3;await startRun({preventDefault(){},target:{}});assert.equal(calls.length,0);assert(input.focused);
""")


def test_preview_conflict_blocks_next_and_clears_after_edit_without_review():
    run_browser_logic(r"""
state.config={checks:['concurrency'],targets:[{}],concurrency:40,max_requests:100,max_duration_seconds:300};
state.jsonDraft=state.jsonBase='synced';state.setupStep=2;
syncConfigView=syncSetupStep=syncTestControls=captureFormDraft=()=>{};validateRunForm=()=>true;
nodes['#request-plan']=element();nodes['#form-error']=element();
const input=element({name:'load_schedule_levels'});nodes['#field-load_schedule_levels']=input;nodes['#field-load_schedule_levels-error']=element();
nodes['#new-run-form']={elements:{load_schedule_levels:input}};nodes['[data-test-card="concurrency"]']=element();
readForm=()=>({config:structuredClone(state.config)});
let fixed=false,calls=0;
const errors=[{field:'concurrent_load.schedule.levels',fields:['concurrent_load.schedule.levels','concurrency'],msg:'Concurrency level 64 exceeds Limit simultaneous requests 40.'}];
api=async()=>{calls++;if(!fixed)throw Object.assign(new Error(errors[0].msg),{errors});return {config:state.config,request_plan:{rows:[],warnings:[],count_kind:'exact',planned_total:10}};};
navigateSetup(3);await Promise.resolve();await Promise.resolve();await Promise.resolve();
assert.equal(calls,1);assert.equal(state.setupStep,2);assert(input.focused);
assert.equal(input.attributes['aria-invalid'],'true');assert.match(nodes['#request-plan'].innerHTML,/Edit concurrency levels/);
// Editing invalidates old errors immediately; the debounced response confirms the fix.
configChanged();assert.equal(input.attributes['aria-invalid'],undefined);
fixed=true;await refreshRequestPreview(undefined,{draft:true});
assert.equal(state.setupStep,2);assert(currentRequestPlan());assert.equal(currentSettingErrors().length,0);
assert.doesNotMatch(nodes['#request-plan'].innerHTML,/Concurrency level 64 exceeds/);
""")


def test_time_estimate_and_warning_have_distinct_copy_and_edit_links_in_both_views():
    run_browser_logic(r"""
state.config={checks:['access'],targets:[{}],concurrency:4,max_requests:100,max_duration_seconds:20};
state.jsonDraft=state.jsonBase='synced';
const plan={count_kind:'exact',planned_total:4,request_limit:100,rows:[],warnings:[],estimated_seconds:4,
 estimated_low_seconds:3,estimated_high_seconds:5,confidence:'medium',estimate_exclusions:['setup/connection','AI evaluation','report generation'],
 time_warnings:[{msg:'The limit may leave checks unfinished.',fields:['max_duration_seconds','request_timeout_seconds'],actions:[{label:'Use 30 s',field:'max_duration_seconds',value:30},{label:'Edit request timeout',field:'request_timeout_seconds'}]}],
 request_warnings:[{msg:'Answer correctness will be cut.',fields:['max_requests'],actions:[{label:'Use 120 total requests',field:'max_requests',value:120}]}]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan};nodes['#request-plan']=element();
renderRequestPlan();
for(const markup of [nodes['#request-plan'].innerHTML,renderRunBlueprint(state.config,plan)]){
  assert.equal((markup.match(/Estimated duration:/g)||[]).length,1);
  assert.match(markup,/about 3 s–5 s · test traffic only/);assert.match(markup,/Confidence: medium/);
  assert.match(markup,/setup\/connection, AI evaluation, report generation/);
  assert.match(markup,/Use 30 s/);assert.match(markup,/Edit request timeout/);
  assert.match(markup,/Use 120 total requests/);assert.match(markup,/Answer correctness will be cut/);
  assert.doesNotMatch(markup,/Usually about|one second per request/);
}
assert(canStartRun());
plan.errors=[{field:'max_duration_seconds',fields:['max_duration_seconds','traffic.rates'],msg:'Fixed windows exceed Stop test traffic after.'}];
assert(!canStartRun());renderRequestPlan();assert.match(nodes['#request-plan'].innerHTML,/data-setting-path="traffic.rates"/);
plan.estimated_seconds=plan.estimated_low_seconds=plan.estimated_high_seconds=null;plan.time_bound_reasons=['+ 120 requests at unknown speed (up to 1 h if every request hits its timeout).'];
assert.match(renderPlanTime(plan),/awaiting comparable measurements/);assert.match(renderPlanTime(plan),/120 requests at unknown speed/);
""")


def test_budget_actions_apply_current_offer_and_enable_limit_without_changing_workload():
    run_browser_logic(r"""
state.config={checks:['capacity'],targets:[{}],max_requests:2,max_duration_seconds:9,traffic:{rates:[25],duration_seconds:20}};
state.jsonDraft=state.jsonBase='synced';
const elements={max_requests:element({value:'2'}),max_duration_seconds:element({value:'9'}),max_requests_enabled:{checked:false},max_duration_seconds_enabled:{checked:false}};
nodes['#new-run-form']={elements};nodes['#config-error']=element();
updateBounds=()=>{};configChanged=()=>{};
const plan={request_limit:2,request_warnings:[{actions:[{field:'max_requests',value:500}]}],time_warnings:[{actions:[{field:'max_duration_seconds',value:20}]}]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan};
async function use(action,budget){await listeners.click({target:{closest(selector){if(selector==='[data-action]')return {dataset:{action}};if(selector==='[data-budget]')return {dataset:{budget:String(budget)}};return null;}}});}
await use('use-request-budget',500);assert.equal(elements.max_requests.value,500);assert(elements.max_requests_enabled.checked);
await use('use-time-budget',20);assert.equal(elements.max_duration_seconds.value,20);assert(elements.max_duration_seconds_enabled.checked);
assert.deepEqual(state.config.traffic,{rates:[25],duration_seconds:20});
await use('use-time-budget',30);assert.equal(elements.max_duration_seconds.value,20);assert.match(nodes['#config-error'].textContent,/plan changed/);
""")


def test_tab_title_tracks_active_run_across_pages_and_completion():
    run_browser_logic(r"""
for(const id of ['active-run-link','nav-new','nav-runs','breadcrumb'])nodes['#'+id]=element();
updateNav('new','New run');assert.equal(document.title,'New run · Giraffe');
state.runs=[{id:'live',state:'running',label:'Model test'}];setActive('live');
assert.match(document.title,/● In progress · Model test · Giraffe/);
updateNav('runs','History');assert.match(document.title,/In progress/);
for(const status of ['completed','error','cancelled']){
  state.runs[0].state='running';setActive('live');
  state.runs[0].state=status;setActive(null);assert.equal(document.title,'History · Giraffe');
}
nodes['#run-count']=element();
api=async()=>({active_run_id:'live',runs:[{id:'live',state:'running',label:'Polled run'}]});
await refreshRuns();assert.equal(document.title,'● In progress · Polled run · Giraffe');
api=async()=>({runs:[{id:'live',state:'completed'}]});
await refreshRuns();assert.equal(document.title,'History · Giraffe');
""")


def test_connection_is_explicit_and_rejects_stale_endpoint_responses():
    run_browser_logic(r"""
const form={elements:{url:element({value:'http://first/v1',checkValidity(){return true;},setCustomValidity(){}}),model:element({value:'manual-model',setCustomValidity(){}}),api_key_env:element({value:'FIRST_KEY'})}};
nodes['#new-run-form']=form;
for(const id of ['model-discovery-status','config-error','manual-model-field','model-options','model-options-label'])nodes['#'+id]=element();
nodes['#connection-model-choice']=element({previousElementSibling:element()});
nodes['[data-action="test-connection"]']=element();
state.config={targets:[{headers_env:{'X-Gateway-Key':'GATEWAY_KEY'}}],ca_bundle:'/ca.pem',proxy:'http://proxy:80',request_options:{temperature:0}};
state.jsonDraft=state.jsonBase='synced';
syncSetupStep=updateBounds=()=>{};
const calls=[],pending=[];
api=(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return new Promise((resolve,reject)=>pending.push({resolve,reject}));};
renderModelDiscovery();assert.equal(calls.length,0);
const first=testConnection();
assert.equal(calls[0].path,'/api/targets/test-connection');
assert.equal(calls[0].body.api_key_env,'FIRST_KEY');
assert.equal(calls[0].body.headers_env['X-Gateway-Key'],'GATEWAY_KEY');
assert.equal(calls[0].body.model,'manual-model');assert.equal(calls[0].body.ca_bundle,'/ca.pem');
assert.equal(calls[0].body.proxy,'http://proxy:80');assert.equal(calls[0].body.request_options.temperature,0);
assert(nodes['[data-action="test-connection"]'].disabled);
form.elements.url.value='http://second/v1';invalidateModelDiscovery();
const second=testConnection();pending[1].resolve({status:'needs_model',models:['second-model','<odd-model>'],message:'Choose a model'});await second;
assert.match(nodes['#connection-model-choice'].innerHTML,/second-model/);
assert.match(nodes['#connection-model-choice'].innerHTML,/&lt;odd-model&gt;/);
assert(nodes['#model-options'].open);
pending[0].resolve({status:'connected',model:'old-model',latency_ms:12});await first;
assert.equal(form.elements.model.value,'manual-model'); // Stale verification cannot override the endpoint's model.
const obsolete=testConnection();form.elements.api_key_env.value='NEW_KEY';invalidateModelDiscovery();
pending[2].reject(new Error('old credentials failed'));await obsolete;
assert.equal(nodes['#model-discovery-status'].textContent,'');
const current=testConnection();pending[3].reject(new Error('Environment variable NEW_KEY is not set.'));await current;
assert.equal(nodes['#model-discovery-status'].textContent,'Environment variable NEW_KEY is not set.');
const departed=testConnection();state.routeVersion++;pending[4].resolve({status:'connected',model:'late-model'});await departed;
assert.equal(form.elements.model.value,'manual-model');
state.routeVersion++;renderModelDiscovery();assert(!nodes['[data-action="test-connection"]'].disabled);
assert.equal(state.modelDiscovery,null);
form.elements.model.value='';
const retry=testConnection();pending[5].resolve({status:'connected',model:'only-model',latency_ms:42});await retry;
assert.equal(form.elements.model.value,'only-model');assert.equal(state.config.targets[0].model,'only-model');
assert.equal(nodes['#model-discovery-status'].textContent,'Connected · only-model · 42 ms');
assert.equal(state.modelDiscovery.key,modelDiscoveryKey());
assert.equal(state.autoModel,'only-model');
// Editing an endpoint or credentials invalidates inferred model choice, not just the green status.
nodes['#cli-fallback']=element();
const urlEdit={name:'url',closest(q){return q==='#new-run-form'?form:null;},matches(){return false;},setCustomValidity(){}};
form.elements.url.value='http://third/v1';listeners.input({target:urlEdit});
assert.equal(form.elements.model.value,'');assert.equal(state.autoModel,null);
assert.equal(nodes['#model-discovery-status'].textContent,'');
form.elements.model.value='explicit-model';listeners.input({target:urlEdit});assert.equal(form.elements.model.value,'explicit-model');
state.autoModel='explicit-model';listeners.input({target:{...urlEdit,name:'api_key_env'}});assert.equal(form.elements.model.value,'');
state.jsonDraft='unapplied';await testConnection();assert.equal(calls.length,6);
assert.match(nodes['#config-error'].textContent,/Apply or discard/);
""")


def test_step_validation_reveals_invalid_advanced_controls_before_focus():
    run_browser_logic(r"""
state.config={request_source:'builtin'};
syncConfigView=syncSetupStep=()=>{};
const panels=[element({dataset:{setupPanel:'1'}}),element({dataset:{setupPanel:'2'}})];
const shared={tagName:'DETAILS',id:'shared-settings',open:false,parentElement:null};
const details={tagName:'DETAILS',id:'shared-run-limits',open:false,parentElement:shared},fieldset={disabled:true};
let checked=[];
function control(name,panel,valid,parent=null){return element({name,parentElement:parent,
 closest(q){return q==='[data-setup-panel]'?panels[panel-1]:q==='fieldset'?fieldset:null;},
 checkValidity(){checked.push(name);assert.equal(this.disabled,false);assert.equal(fieldset.disabled,false);return valid;},
 reportValidity(){assert(this.focused);assert(details.open);assert(shared.open);assert.equal(state.setupStep,2);this.reported=true;}});}
const model=control('model',1,true),advanced=control('max_output_tokens',2,false,details);advanced.disabled=true;
const form={querySelectorAll(){return [model,advanced];}};
assert(validateRunForm(form,1));assert.equal(checked.join(','),'model');assert(!details.open);assert(!shared.open);
assert(!validateRunForm(form,2));assert.equal(checked.join(','),'model,max_output_tokens');
assert(advanced.reported);assert(details.open);assert(advanced.disabled);assert(fieldset.disabled);
// An unselected test with an invalid saved override cannot block review.
const card={dataset:{testCard:'specialist'},querySelector(){return {checked:false};}};
advanced.closest=q=>q==='[data-test-card]'?card:q==='[data-setup-panel]'?panels[1]:null;
assert(validateRunForm(form,2));
""")


def test_progressive_navigation_keeps_drafts_and_enter_never_starts_early():
    run_browser_logic(r"""
for(const id of ['setup-panel-1','form-error','start-run','setup-navigation-hint','run-bar-summary','run-blueprint','setup-title','setup-description'])nodes['#'+id]=element();
for(const action of ['setup-next','setup-back'])nodes['[data-action="'+action+'"]']=element();
nodes['#test-selection h2']=element({focus(options){assert(options.preventScroll);this.focused=true;}});
let scrolledToTop=false;window.scrollTo=options=>{assert.equal(options.top,0);scrolledToTop=true;};
const panels=[1,2,3].map(step=>element({dataset:{setupPanel:String(step)}}));
const buttons=[1,2,3].map(step=>element({dataset:{setupStep:String(step)}}));
groups['[data-setup-panel]']=panels;groups['.setup-stepper [data-setup-step]']=buttons;
state.jsonDraft=state.jsonBase='synced';state.configView='form';state.setupStep=1;
const form={elements:{model:{value:'edited-model'},baseline_id:{value:'reference'},mode:{value:'normal'},restart_target:{value:'first'}}};nodes['#new-run-form']=form;
const draft={targets:[{name:'first',model:'edited-model'},{name:'other',model:'kept'}],checks:['access'],request_pack:{cases:[{fixture_id:'kept'}]},test_options:{prefix:{repeats:7}}};
readForm=()=>({config:structuredClone(draft)});updateBounds=()=>{};
const validated=[];validateRunForm=(_,step)=>{validated.push(step);return true;};validateSetupChecks=()=>{};
let calls=0;api=async()=>{calls++;throw Error('No target requests allowed');};
syncSetupStep();assert(!panels[0].hidden);assert(panels[1].hidden);assert(nodes['#start-run'].hidden);
assert.equal(nodes['#setup-title'].textContent,'Point Giraffe at an endpoint.');
assert(nodes['#run-blueprint'].hidden);assert(nodes['#run-bar-summary'].hidden);
await startRun({preventDefault(){},target:form});
assert.equal(state.setupStep,2);assert.equal(calls,0);assert.equal(validated.join(','),'1');
assert.equal(JSON.stringify(state.config),JSON.stringify(draft));assert.equal(state.prefillBaseline,'reference');
assert(panels[0].hidden);assert(!panels[1].hidden);assert(nodes['#start-run'].hidden);
assert(nodes['#test-selection h2'].focused);assert(scrolledToTop);assert(nodes['#setup-navigation-hint'].hidden);
assert.equal(nodes['#setup-title'].textContent,'Set limits and choose checks.');
assert(!nodes['#run-blueprint'].hidden);assert(nodes['#run-bar-summary'].hidden);
navigateSetup(1);assert.equal(state.setupStep,1);assert.equal(JSON.stringify(state.config),JSON.stringify(draft));
validated.length=0;navigateSetup(3);assert.equal(validated.join(','),'1,2');assert.equal(state.setupStep,3);
assert(!nodes['#start-run'].hidden);assert(nodes['[data-action="setup-next"]'].hidden);
assert(nodes['#run-bar-summary'].hidden);assert(!nodes['#setup-navigation-hint'].hidden);
assert.equal(nodes['#setup-title'].textContent,'Ready for launch.');assert(nodes['#run-blueprint'].hidden);
assert.equal(buttons[2].attributes['aria-current'],'step');
navigateSetup(1);validateRunForm=()=>false;navigateSetup(2);assert.equal(state.setupStep,1);
state.jsonDraft='unapplied';navigateSetup(2);assert.equal(state.setupStep,1);assert.match(nodes['#form-error'].textContent,/Apply or discard/);
""")


def test_current_plan_is_required_to_start_and_polling_cannot_bypass_it():
    run_browser_logic(r"""
for(const id of ['form-error','start-run','request-plan','checks-plan-summary'])nodes['#'+id]=element();
state.setupStep=3;state.jsonDraft=state.jsonBase='synced';
state.config={request_source:'builtin',checks:['access'],targets:[{name:'local',model:'m'}]};
validateRunForm=()=>true;readForm=()=>({config:structuredClone(state.config)});
refreshRuns=async()=>{};connectionError=schedulePoll=()=>{};
const calls=[];api=async(path)=>{calls.push(path);return {id:'saved'};};
const plan={count_kind:'exact',planned_total:7,planned_per_target:7,request_limit:160,rows:[],warnings:[]};
for(const status of [null,'pending','error','stale','current']){
  state.requestPreview=status?{status,error:'test failure',revision:status==='current'?state.configRevision-1:state.configRevision,route:state.routeVersion,plan}:null;
  renderRequestPlan();assert(nodes['#start-run'].disabled);
  await pollNew(state.routeVersion);assert(nodes['#start-run'].disabled);
  await startRun({preventDefault(){},target:{}});assert.equal(calls.length,0);
  assert.match(nodes['#form-error'].textContent,/current request plan/);
}
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan};
renderRequestPlan();assert(!nodes['#start-run'].disabled);
assert.equal(nodes['#checks-plan-summary'].textContent,'7 requests planned');
plan.required_request_limit=7;plan.request_limit=4;renderRequestPlan();assert.match(nodes['#checks-plan-summary'].textContent,/budget is too small/);
plan.request_limit=160;renderRequestPlan();assert.equal(nodes['#checks-plan-summary'].textContent,'7 requests planned');
for(const key of ['configBusy','submitting','active']){state[key]=true;renderRequestPlan();assert(nodes['#start-run'].disabled);state[key]=false;}
state.jsonDraft='dirty';renderRequestPlan();assert(nodes['#start-run'].disabled);state.jsonDraft=state.jsonBase;
validatedSnapshot=async()=>({config:structuredClone(state.config),mode:'normal'});refreshRequestPreview=async()=>{};setActive=()=>{};
await startRun({preventDefault(){},target:{}});assert.equal(calls.join(','),'/api/runs');
""")


def test_editing_blank_connection_clears_validation_error_before_model_lookup():
    run_browser_logic(r"""
const form={elements:{url:element({name:'url',value:'',custom:'',setCustomValidity(value){this.custom=value;},checkValidity(){return !this.custom&&Boolean(this.value);},reportValidity(){},matches(){return false;},closest(q){return q==='#new-run-form'?form:q==='[data-setup-panel]'?{dataset:{setupPanel:'1'}}:null;}}),model:element({value:'manual'}),api_key_env:element()}};
form.querySelectorAll=()=>[form.elements.url];nodes['#new-run-form']=form;
for(const id of ['model-discovery-status','discovered-models','cli-fallback'])nodes['#'+id]=element();
nodes['[data-action="test-connection"]']=element();
state.config={request_source:'builtin',targets:[{}]};state.jsonDraft=state.jsonBase='synced';
syncConfigView=syncSetupStep=updateBounds=()=>{};
assert(!validateRunForm(form,1));assert.match(form.elements.url.custom,/Enter an endpoint URL/);
form.elements.url.value='http://localhost/v1';listeners.input({target:form.elements.url});
assert.equal(form.elements.url.custom,'');
let calls=0;api=async()=>{calls++;return {status:'needs_model',models:['one','two'],message:'Choose a model'};};await testConnection();
assert.equal(calls,1);assert.match(nodes['#model-discovery-status'].textContent,/Choose a model/);
""")


def test_json_transition_focuses_editor_and_preserves_step_and_unsaved_draft():
    run_browser_logic(r"""
const calls=[];
for(const id of ['config-panel-form','config-panel-json','config-editor','config-tab-form','config-tab-json','config-draft-notice'])nodes['#'+id]=element({scrollIntoView(options){calls.push('scroll:'+id);assert.equal(options.block,'start');},focus(options){calls.push('focus:'+id);assert(options.preventScroll);}});
nodes['[data-setup-panel="2"]']=element({scrollIntoView(){calls.push('scroll:step2');}});
nodes['[data-setup-panel="2"] h2']=element({focus(options){calls.push('focus:step2');assert(options.preventScroll);}});
state.setupStep=2;state.configView='form';state.jsonDraft=state.jsonBase=null;
const draft={config:{targets:[{model:'edited'}],checks:['access'],max_requests:11},mode:'normal'};
captureFormDraft=syncConfigView=()=>{};readForm=()=>structuredClone(draft);
switchConfigView('json');
assert.equal(calls.join(','),'scroll:config-panel-json,focus:config-editor');
assert.equal(JSON.parse(nodes['#config-editor'].value).max_requests,11);assert.equal(state.setupStep,2);
calls.length=0;switchConfigView('form');
assert.equal(calls.join(','),'scroll:step2,focus:step2');assert.equal(state.setupStep,2);
state.jsonDraft='{ unfinished';state.configView='form';calls.length=0;
switchConfigView('json');assert.equal(state.jsonDraft,'{ unfinished');
assert.equal(calls.join(','),'scroll:config-panel-json,focus:config-editor');
""")


def test_skip_link_focuses_content_without_changing_the_current_route():
    run_browser_logic(r"""
let scrolled=false,prevented=false;
nodes['#main']=element({focus(options){assert(options.preventScroll);this.focused=true;},scrollIntoView(options){assert.equal(options.block,'start');scrolled=true;}});
state.setupStep=2;state.config={targets:[{model:'draft-model'}]};
for(const hash of ['#new','#runs/saved-run']){
  location.hash=hash;nodes['#main'].focused=false;
  await listeners.click({target:{closest(q){assert.equal(q,'.skip-link');return this;}},preventDefault(){prevented=true;}});
  assert.equal(location.hash,hash);assert.equal(state.setupStep,2);
  assert.equal(state.config.targets[0].model,'draft-model');
  assert.equal(nodes['#main'].attributes.tabindex,'-1');assert(nodes['#main'].focused);
}
assert(prevented);assert(scrolled);
""")


def test_connect_has_one_primary_action_and_manual_model_bypass():
    run_browser_logic(r"""
for(const id of ['setup-panel-1','start-run','setup-navigation-hint','run-bar-summary','model-discovery-status','model-options','model-options-label','connection-options'])nodes['#'+id]=element();
for(const action of ['setup-next','setup-back','test-connection'])nodes['[data-action="'+action+'"]']=element();
const primary=new Set();nodes['[data-action="test-connection"]'].classList={toggle(name,value){if(value)primary.add(name);else primary.delete(name);}};
nodes['#new-run-form']={elements:{model:element(),url:element({value:'http://local/v1'}),api_key_env:element()}};
state.config={targets:[{}]};state.jsonDraft=state.jsonBase='synced';state.setupStep=1;
renderModelDiscovery();
assert(nodes['[data-action="setup-next"]'].hidden);assert(primary.has('primary'));
assert.equal(nodes['[data-action="test-connection"]'].textContent,'Connect');
assert(nodes['#setup-navigation-hint'].hidden);
// A manual model permits continuing without endpoint traffic.
nodes['#new-run-form'].elements.model.value='manual-model';renderModelDiscovery();
assert(!nodes['[data-action="setup-next"]'].hidden);assert.match(nodes['[data-action="setup-next"]'].innerHTML,/Choose checks/);
assert(!primary.has('primary'));assert(primary.has('small'));
assert.equal(nodes['[data-action="test-connection"]'].textContent,'Connect');
assert.equal(nodes['[data-action="test-connection"]'].title,'Sends one short request (32-token limit).');
// Successful discovery selects the same next action, while a reset restores testing.
state.modelDiscovery={key:modelDiscoveryKey(),route:state.routeVersion,status:'connected',message:'Connected'};renderModelDiscovery();
assert(!nodes['[data-action="setup-next"]'].hidden);assert(!primary.has('primary'));
nodes['#new-run-form'].elements.model.value='';invalidateModelDiscovery();
assert(nodes['[data-action="setup-next"]'].hidden);assert(primary.has('primary'));
// Model selection and connection problems reveal their controls automatically.
for(const status of ['needs_model','error']){
  nodes['#connection-options'].open=false;
  state.modelDiscovery={key:modelDiscoveryKey(),route:state.routeVersion,status,message:'Configure connection'};renderModelDiscovery();
  assert(nodes['#connection-options'].open);
}
assert(nodes['#model-options'].open);
""")


def test_shell_preserves_new_run_navigation_on_each_screen():
    run_browser_logic(r"""
for(const id of ['nav-new','nav-runs','breadcrumb'])nodes['#'+id]=element();
updateNav('new','New run');assert(!nodes['#nav-new'].hidden);assert.equal(nodes['#nav-new'].attributes['aria-current'],'page');
updateNav('runs','Run history');assert(!nodes['#nav-new'].hidden);
""")
