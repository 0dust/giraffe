"""Behavior checks for progressive setup and explicit endpoint discovery."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path(__file__).parents[1] / "src/giraffe/static/app.js"


def run_browser_logic(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise browser setup behavior")
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes={},listeners={},groups={};
function element(extra={}) {
  return {hidden:false,disabled:false,innerHTML:'',textContent:'',value:'',dataset:{},
    attributes:{},classList:{toggle(){}},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];},
    focus(){this.focused=true;},...extra};
}
const document={querySelector:q=>nodes[q],querySelectorAll:q=>groups[q] || [],addEventListener:(kind,fn)=>listeners[kind]=fn};
const context={assert,nodes,listeners,groups,element,structuredClone,document,window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+'\n(async()=>{'+BODY+'\n})()',context);
""".replace("APP_PATH", json.dumps(str(APP))).replace("BODY", json.dumps(body))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


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
assert.match(nodes['#model-discovery-status'].textContent,/Connected · only-model/);
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
for(const id of ['setup-panel-1','form-error','start-run','setup-navigation-hint','run-bar-summary'])nodes['#'+id]=element();
for(const action of ['setup-next','setup-back'])nodes['[data-action="'+action+'"]']=element();
nodes['#shared-settings > summary']=element();
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
await startRun({preventDefault(){},target:form});
assert.equal(state.setupStep,2);assert.equal(calls,0);assert.equal(validated.join(','),'1');
assert.equal(JSON.stringify(state.config),JSON.stringify(draft));assert.equal(state.prefillBaseline,'reference');
assert(panels[0].hidden);assert(!panels[1].hidden);assert(nodes['#start-run'].hidden);
assert(nodes['#shared-settings > summary'].focused);assert(nodes['#setup-navigation-hint'].hidden);
navigateSetup(1);assert.equal(state.setupStep,1);assert.equal(JSON.stringify(state.config),JSON.stringify(draft));
validated.length=0;navigateSetup(3);assert.equal(validated.join(','),'1,2');assert.equal(state.setupStep,3);
assert(!nodes['#start-run'].hidden);assert(nodes['[data-action="setup-next"]'].hidden);
assert(nodes['#run-bar-summary'].hidden);assert(!nodes['#setup-navigation-hint'].hidden);
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
