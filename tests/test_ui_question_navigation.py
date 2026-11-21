"""Question-management views retain one run draft and reject stale navigation."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities
from giraffe.models import RunConfig


STATIC = Path(__file__).parents[1] / "src/giraffe/static"


def navigation_browser(body, config=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise question navigation")
    config = config or RunConfig(
        targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
        checks=["correctness", "load"], concurrency=8, max_requests=55,
        request_source="request_pack", request_pack={"version": 1, "cases": [
            {"fixture_id": "alpha", "messages": [{"role": "user", "content": "Alpha question"}],
             "scorer": "exact", "expected": "Alpha answer"},
            {"fixture_id": "beta", "messages": [{"role": "user", "content": "Beta question"}],
             "scorer": "exact", "expected": "Beta answer"},
        ]},
    )
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as formConfig from FORM_PATH;
import {browserDOM} from DOM_PATH;
const listeners={},windowListeners={},timers=[],requests=[],messages=[],scrolls=[];
const document=browserDOM(listeners),proto=Object.getPrototypeOf(document.body);
// Supply native form properties to the repository's dependency-free DOM fixture.
for(const name of ['name','type'])Object.defineProperty(proto,name,{
 get(){return this.getAttribute(name)||'';},set(value){this.setAttribute(name,value);}
});
Object.defineProperty(proto,'value',{
 get(){if(this._value!==undefined)return this._value;
  if(this.tagName==='TEXTAREA')return this.textContent;
  if(this.tagName==='SELECT')return (this.querySelector('option[selected]')||this.querySelector('option'))?.getAttribute('value')||'';
  return this.getAttribute('value')??(this.type==='checkbox'?'on':'');},
 set(value){this._value=String(value);}
});
Object.defineProperty(proto,'checked',{
 get(){return this._checked??this.hasAttribute('checked');},set(value){this._checked=Boolean(value);}
});
for(const [name,attribute] of [['disabled','disabled'],['readOnly','readonly']])Object.defineProperty(proto,name,{
 get(){return this.hasAttribute(attribute);},set(value){if(value)this.setAttribute(attribute,'');else this.removeAttribute(attribute);}
});
Object.defineProperty(proto,'elements',{get(){return new Proxy({}, {get:(_,name)=>{
 const found=this.querySelectorAll('[name="'+String(name)+'"]');
 if(found.length<2)return found[0];
 Object.defineProperty(found,'value',{get:()=>found.find(input=>input.checked)?.value||'',
  set:value=>found.forEach(input=>{input.checked=input.value===value;})});return found;
}});}});
Object.defineProperty(proto,'dataset',{get(){return new Proxy({}, {
 get:(_,name)=>this.getAttribute('data-'+String(name).replace(/[A-Z]/g,letter=>'-'+letter.toLowerCase()))??undefined,
 set:(_,name,value)=>{this.setAttribute('data-'+String(name).replace(/[A-Z]/g,letter=>'-'+letter.toLowerCase()),value);return true;}
});}});
proto.setCustomValidity=function(message){this.validationMessage=message;};
proto.checkValidity=function(){return !this.validationMessage;};
proto.scrollIntoView=function(){};
for(const id of ['dataset-dialog','command-dialog']){
 const dialog=document.createElement('dialog');dialog.setAttribute('id',id);document.body.insertBefore(dialog,null);
}
class NativeFormData extends FormData {
 constructor(form){super();if(!form)return;
  for(const input of form.querySelectorAll('input,select,textarea')){
   if(!input.name||input.disabled||input.closest('fieldset[disabled]')||(['checkbox','radio'].includes(input.type)&&!input.checked))continue;
   this.append(input.name,input.value);
  }
 }
}
const window={innerWidth:1280,innerHeight:800,scrollY:0,scrollX:0,
 addEventListener:(kind,fn)=>windowListeners[kind]=fn,matchMedia:()=>({matches:true}),
 scrollTo(x,y){const top=typeof x==='object'?x.top:y;this.scrollY=top??0;scrolls.push(this.scrollY);}};
const context={assert,structuredClone,...formConfig,document,window,listeners,windowListeners,timers,requests,messages,scrolls,
 FormData:NativeFormData,AbortController,URL,console,location:{hash:'#new'},
 requestAnimationFrame:fn=>fn(),cancelAnimationFrame(){},setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},
 fetch:()=>{throw Error('No real network requests are permitted in this fixture');}};
context.bootstrap=BOOTSTRAP;context.config=CONFIG;
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
state.bootstrap=bootstrap;state.bootstrap.config=config;state.config=structuredClone(config);
state.setupStep=2;state.openTest='correctness';state.showAvailableTests=true;
state.jsonDraft=state.jsonBase=null;
watchPlanViewport=scheduleTokenizerSelection=renderTokenizerSelection=renderModelDiscovery=()=>{};
toast=message=>messages.push(message);
api=async(path,options={})=>{
 const body=options.body?JSON.parse(options.body):null;requests.push({path,body});
 if(path==='/api/config/draft'){assert.doesNotThrow(()=>new URL(body.targets[0].url));return {config:body};}
 if(path==='/api/datasets/preview'){
  const pack=JSON.parse(body.text);return {columns:[],preview_rows:pack.cases,request_pack:pack,errors:[],needs_mapping:false};
 }
 throw Error('Unexpected request: '+path);
};
function visible(selector){return document.querySelectorAll(selector).find(node=>!node.closest('[hidden]'));}
async function click(action){const target=visible('[data-action="'+action+'"]');assert(target,'Missing visible action '+action);
 return listeners.click({target,preventDefault(){}});}
function type(input,value){assert(input,'Missing input');input.value=value;listeners.input({target:input});}
async function submit(form){assert(form,'Missing form');return listeners.submit({target:form,preventDefault(){}});}
async function until(condition,message='Navigation did not settle'){
 for(let i=0;i<100;i++){if(condition())return;await Promise.resolve();}
 assert(condition(),message+'; view='+state.setupWorkspace?.view+'; error='+document.querySelector('#dataset-error')?.textContent);
}
function change(input,value){assert(input,'Missing input');if(value!==undefined)input.value=value;listeners.change({target:input});}

(async()=>{`+BODY+`})()`,context);
"""
    values = {
        "FORM_PATH": json.dumps((STATIC / "form-config.mjs").as_uri()),
        "DOM_PATH": json.dumps((Path(__file__).parent / "ui_dom.mjs").as_uri()),
        "APP_PATH": json.dumps(str(STATIC / "app.js")),
        "BOOTSTRAP": json.dumps(capabilities(config)),
        "CONFIG": json.dumps(config.model_dump(mode="json")),
        "BODY": json.dumps(body),
    }
    script = re.sub(r"\b(?:FORM_PATH|DOM_PATH|APP_PATH|BOOTSTRAP|CONFIG|BODY)\b",
                    lambda match: values[match[0]], script)
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


@pytest.mark.parametrize("source", ["builtin", "request_pack"])
def test_setup_routes_each_question_source_to_its_focused_view(source):
    navigation_browser("const source=" + json.dumps(source) + r''';
state.config.request_source=source;state.datasetSelection=['alpha'];renderNew();
const form=document.querySelector('#new-run-form'),endpoint=form.elements.url,original=structuredClone(readForm(true).config);
for(const selector of ['.question-table','.builtin-example','#dataset-example-form','#dataset-import-form','[name="judge_count"]'])assert.equal(form.querySelector(selector),null,selector);
const assertDraft=expected=>{
 assert.equal(state.config.request_source,expected);assert.equal(new FormData(form).get('request_source'),expected);
 assert.equal(JSON.stringify(state.config.request_pack),JSON.stringify(original.request_pack));
 assert.equal(JSON.stringify(state.config.targets),JSON.stringify(original.targets));
 assert.deepEqual(new FormData(form).getAll('check'),original.checks);
 for(const key of ['max_requests','max_duration_seconds','concurrency'])assert.equal(state.config[key],original[key]);
 assert.equal(JSON.stringify(state.config.limits),JSON.stringify(original.limits));
 assert.equal(document.querySelector('#new-run-form'),form);assert.equal(form.elements.url,endpoint);
};
const assertCurrentView=expected=>{
 const workspace=document.querySelector('#setup-workspace');
 assert.equal(document.querySelectorAll('dialog[open]').length,0);
 if(expected==='builtin'){
  assert.equal(state.setupWorkspace.view,'builtin-examples');
  const preview=workspace.querySelector('#builtin-examples-view'),items=preview.querySelectorAll('ol li');
  assert.equal(items.length,3);assert.equal(bootstrap.builtin_questions.length,3);
  bootstrap.builtin_questions.forEach((example,index)=>{
   assert(items[index].textContent.includes(example.question));
   assert(items[index].textContent.includes(String(example.expected)));
   assert(items[index].getClientRects().length,'All built-in questions and expected answers are immediately visible');
  });
  for(const selector of ['#question-manager','#question-manager-list','#question-manager-checks','[data-dataset-select]','#dataset-bulk-scorer','form'])assert.equal(workspace.querySelector(selector),null,selector);
  for(const action of ['add-example','edit-example','remove-example','try-example','import-pack','sample-dataset','download-dataset-sample','apply-answer-check','configure-evaluator'])assert.equal(workspace.querySelector('[data-action="'+action+'"]'),null,action);
 }else{
  assert.equal(state.setupWorkspace.view,'questions');assert(workspace.querySelector('#question-manager'));
  assert.equal(workspace.querySelector('#builtin-examples-view'),null);assert.equal(workspace.querySelector('.builtin-example'),null);
  for(const action of ['add-example','edit-example','remove-example','try-example','import-pack','sample-dataset','apply-answer-check','configure-evaluator'])assert(visible('[data-action="'+action+'"]'),action);
 }
};
const action=source==='builtin'?'view-builtin-examples':'manage-questions';
assert(visible('[data-action="'+action+'"]'));
assert.equal(visible('[data-action="'+(source==='builtin'?'manage-questions':'view-builtin-examples')+'"]'),undefined);
window.scrollY=705;await click(action);assertCurrentView(source);assertDraft(source);
if(source==='builtin'){openQuestionManager();assertCurrentView('builtin');assertDraft('builtin');}
await click('setup-workspace-back');assert.equal(state.setupWorkspace,null);assertDraft(source);
assert.equal(window.scrollY,705);assert.equal(document.activeElement.dataset.action,action);
const other=source==='builtin'?'request_pack':'builtin';
for(const input of form.querySelectorAll('[name="request_source"]'))input.checked=input.value===other;
change(form.querySelector('[name="request_source"][value="'+other+'"]'));assertDraft(other);
const nextAction=other==='builtin'?'view-builtin-examples':'manage-questions';
await click(nextAction);assertCurrentView(other);assertDraft(other);
await click('setup-workspace-back');assertDraft(other);assert.equal(window.scrollY,705);
assert.equal(document.activeElement.dataset.action,nextAction);assert.equal(requests.length,0);
''')

@pytest.mark.parametrize("source", ["builtin", "request_pack"])
def test_typing_and_saving_questions_preserves_raw_setup_and_updates_only_summary(source):
    navigation_browser("state.config.request_source=" + json.dumps(source) + r''';
state.config.test_options.correctness={request_timeout_seconds:125,limits:{min_correctness:.875}};
renderNew();
const form=document.querySelector('#new-run-form'),endpoint=form.elements.url,levels=form.elements.load_patterns_concurrent_levels,maximum=form.elements.max_requests;
type(endpoint,'unfinished endpoint /');type(levels,'1, unfinished');type(maximum,'0055');
const originalChecks=new FormData(form).getAll('check'),options=JSON.stringify(state.config.test_options.correctness),savedPack=JSON.stringify(state.config.request_pack);
if(state.config.request_source==='builtin'){
 await click('view-builtin-examples');assert(document.querySelector('#builtin-examples-view'));await click('setup-workspace-back');
 for(const input of form.querySelectorAll('[name="request_source"]'))input.checked=input.value==='request_pack';
 change(form.querySelector('[name="request_source"][value="request_pack"]'));
 assert.equal(state.config.request_source,'request_pack');assert.equal(JSON.stringify(state.config.request_pack),savedPack);
}
const trigger=visible('[data-action="manage-questions"]');trigger.focus({preventScroll:true});window.scrollY=674;
await click('manage-questions');
assert(form.closest('#run-setup-content').hidden);assert(form.closest('#run-setup-content').inert);assert.equal(form.disabled,false);
await click('add-example');
const editor=document.querySelector('#dataset-example-form'),revision=state.configRevision;
type(editor.elements.message_0_content,'A new AI question');type(editor.elements.example_expected,'Its expected meaning');
assert.equal(state.configRevision,revision,'Typing within an editor must not invalidate its own save');
await submit(editor);await until(()=>state.setupWorkspace?.view==='questions'&&datasetCases().length===3);
assert.equal(datasetCases().at(-1).scorer,'judge');assert.equal(evaluatorReadiness(state.config).label,'Configuration needed');
assert.equal(visible('[name="judge_count"]'),undefined,'AI questions must not open the evaluator form');
assert.equal(document.querySelectorAll('dialog[open]').length,0);
await click('setup-workspace-back');
assert.equal(document.querySelector('#new-run-form'),form);
assert.equal(form.elements.url,endpoint);assert.equal(endpoint.value,'unfinished endpoint /');
assert.equal(form.elements.load_patterns_concurrent_levels,levels);assert.equal(levels.value,'1, unfinished');
assert.equal(form.elements.max_requests,maximum);assert.equal(maximum.value,'0055');
assert.deepEqual(new FormData(form).getAll('check'),originalChecks);
assert.equal(JSON.stringify(state.config.test_options.correctness),options);
assert.equal(form.elements.test_correctness_limit_min_correctness.value,'87.5');
const summary=document.querySelector('[data-question-setup-summary]').textContent;
assert.match(summary,/3 saved questions/);assert.match(summary,/Configuration needed/);
assert.equal(visible('[name="test_correctness_samples"]'),undefined,'Saved questions must not show built-in minimum requests');
assert.equal(window.scrollY,674);assert.equal(document.activeElement.dataset.action,'manage-questions');
assert(requests.every(request=>['/api/datasets/preview','/api/config/draft'].includes(request.path)));
''')


def test_edit_and_import_return_to_the_same_filtered_selected_question_list():
    navigation_browser(r'''
renderNew();await click('manage-questions');
const search=visible('[name="question_search"]');search.focus({preventScroll:true});type(search,'alpha');
change(visible('[name="question_scorer_filter"]'),'exact');
const selected=visible('[data-dataset-select="alpha"]');selected.checked=true;change(selected);
assert.equal(document.activeElement,search,'Filtering should not move keyboard focus');
window.scrollY=640;await click('edit-example');
const editor=document.querySelector('#dataset-example-form'),revision=state.configRevision;
type(editor.elements.example_expected,'Edited alpha answer');assert.equal(state.configRevision,revision);
await submit(editor);await until(()=>state.setupWorkspace?.view==='questions');
assert.equal(datasetCases()[0].expected,'Edited alpha answer');
const assertContext=()=>{
 assert.equal(visible('[name="question_search"]').value,'alpha');
 assert.equal(visible('[name="question_scorer_filter"]').value,'exact');
 assert.equal(window.scrollY,640);
};
assertContext();assert.deepEqual(state.datasetSelection,['alpha']);
await click('import-pack');assert(document.querySelector('#dataset-import-form'));
assert.equal(document.querySelector('#question-manager'),null);assert.equal(document.querySelectorAll('dialog[open]').length,0);
await click('cancel-question-import');assertContext();assert.deepEqual(state.datasetSelection,['alpha']);
const before=JSON.stringify(state.config.request_pack);
await click('import-pack');
const replacement={version:1,cases:[{fixture_id:'alpha-import',messages:[{role:'user',content:'Alpha imported'}],scorer:'exact',expected:'Imported answer'}]};
const file=document.querySelector('#request-pack-file');file.files=[{name:'replacement.json',size:120,text:async()=>JSON.stringify(replacement)}];
change(file);await until(()=>state.datasetImport?.preview?.request_pack&&!state.datasetImport.loading);
assert.equal(JSON.stringify(state.config.request_pack),before,'Preview must not replace the draft');
await submit(document.querySelector('#dataset-import-form'));await until(()=>state.setupWorkspace?.view==='questions'&&datasetCases()[0]?.fixture_id==='alpha-import');
assertContext();assert.deepEqual(state.datasetSelection,[],'Explicit replacement invalidates old selection');
assert.equal(document.querySelector('#dataset-import-form'),null);
assert(requests.every(request=>['/api/datasets/preview','/api/config/draft'].includes(request.path)));
''')


@pytest.mark.parametrize("caller", ["setup", "questions"])
def test_evaluator_cancel_is_local_and_save_returns_to_its_caller(caller):
    navigation_browser("const caller=" + json.dumps(caller) + r''';
state.config.request_pack.cases[0].scorer='judge';
state.config.judging={...state.config.judging,proxy:'http://proxy.local:8080',ca_bundle:'/private/tmp/saved-ca.pem',judges:[{name:'saved',url:'http://judge.local/v1',model:'saved-model',api_key_env:'EVALUATOR_KEY',headers_env:{'X-Team':'TEAM_HEADER'}}]};
renderNew();const setup=document.querySelector('#new-run-form'),endpoint=setup.elements.url;
type(endpoint,'unfinished endpoint /');
if(caller==='questions')await click('manage-questions');
const before=JSON.stringify(state.config.judging),questions=JSON.stringify(state.config.request_pack),checks=JSON.stringify(state.config.checks);
await click('configure-evaluator');
const revision=state.configRevision,form=document.querySelector('#evaluator-form');
type(form.elements.judge_0_model,'discarded-model');
assert.equal(state.configRevision,revision);assert.equal(JSON.stringify(state.config.judging),before);
await click('cancel-evaluator');
assert.equal(JSON.stringify(state.config.judging),before);
assert.equal(state.setupWorkspace?.view||'setup',caller);
await click('configure-evaluator');const saving=document.querySelector('#evaluator-form');
type(saving.elements.judge_0_model,'chosen-model');type(saving.elements.judge_0_url,'http://chosen.local/v1');
await click('save-evaluator');
assert.equal(state.setupWorkspace?.view||'setup',caller);
assert.equal(state.config.judging.judges[0].model,'chosen-model');
assert.equal(state.config.judging.judges[0].url,'http://chosen.local/v1');
assert.equal(state.config.judging.judges[0].api_key_env,'EVALUATOR_KEY');
assert.equal(state.config.judging.judges[0].headers_env['X-Team'],'TEAM_HEADER');
assert.equal(state.config.judging.proxy,'http://proxy.local:8080');assert.equal(state.config.judging.ca_bundle,'/private/tmp/saved-ca.pem');
assert.equal(JSON.stringify(state.config.request_pack),questions);assert.equal(JSON.stringify(state.config.checks),checks);
assert.equal(document.querySelector('#new-run-form'),setup);assert.equal(setup.elements.url,endpoint);assert.equal(endpoint.value,'unfinished endpoint /');
assert.equal(evaluatorReadiness(state.config).label,'Configured');
assert.equal(requests.length,0,'Evaluator configuration must not probe or invoke a provider');
''')


@pytest.mark.parametrize("operation", ["edit", "bulk"])
@pytest.mark.parametrize("old_result", ["success", "error"])
def test_cancelled_save_cannot_overwrite_or_unlock_a_newer_question_save(operation, old_result):
    navigation_browser("const operation=" + json.dumps(operation) + ",oldResult=" + json.dumps(old_result) + r''';
renderNew();await click('manage-questions');
const original=JSON.stringify(state.config.request_pack);
if(operation==='bulk'){
 const selected=visible('[data-dataset-select="alpha"]');selected.checked=true;change(selected);
 visible('#dataset-bulk-scorer').value='none';
}else{
 await click('add-example');type(document.querySelector('#dataset-example-form').elements.message_0_content,'Obsolete edit');
}
const queue=[];api=(path,options)=>{
 assert.equal(path,'/api/config/draft');const pack=JSON.parse(options.body).request_pack;
 return new Promise((resolve,reject)=>queue.push({pack,resolve,reject}));
};
const first=operation==='bulk'?applyAnswerCheck():saveExample({preventDefault(){}});
await until(()=>queue.length===1);assert(state.configBusy);
const cancel=visible('[data-action="'+(operation==='bulk'?'setup-workspace-back':'cancel-question-edit')+'"]');
assert.equal(cancel.disabled,false,'A pending save must remain cancellable');
await click(operation==='bulk'?'setup-workspace-back':'cancel-question-edit');
assert.equal(state.configBusy,false,'Leaving an old save must release only its ownership');
assert.equal(JSON.stringify(state.config.request_pack),original);
if(operation==='bulk')await click('manage-questions');
await click('add-example');
const editor=document.querySelector('#dataset-example-form');type(editor.elements.message_0_content,'Current question');type(editor.elements.example_expected,'Current answer');
const current=state.datasetEditor,second=saveExample({preventDefault(){}});
await until(()=>queue.length===2);assert(state.configBusy);
if(oldResult==='error')queue[0].reject(new Error('Old save failed'));
else queue[0].resolve({config:{request_pack:queue[0].pack}});
await first;
assert.equal(state.configBusy,true,'An old finally must not unlock the newer save');
assert.equal(state.setupWorkspace.view,'question-editor');assert.equal(state.datasetEditor,current);
assert.equal(JSON.stringify(state.config.request_pack),original);
assert.doesNotMatch(document.querySelector('#dataset-error').textContent,/Old save failed/);
queue[1].resolve({config:{request_pack:queue[1].pack}});await second;
assert.equal(state.configBusy,false);assert.equal(state.setupWorkspace.view,'questions');
assert.equal(datasetCases().length,3);assert.equal(datasetCases().at(-1).messages[0].content,'Current question');
''')


@pytest.mark.parametrize("outcome", ["success", "error"])
@pytest.mark.parametrize("return_to_setup", [False, True])
def test_late_review_plan_cannot_navigate_or_focus_after_management_entry(outcome, return_to_setup):
    navigation_browser("const outcome=" + json.dumps(outcome) + ",returnToSetup=" + json.dumps(return_to_setup) + r''';
state.config.request_pack.cases[0].scorer='judge';renderNew();
validateRunForm=()=>true;state.requestPreview=null;
let finish,fail,posted;
api=(path,options)=>{assert.equal(path,'/api/config/draft');posted=JSON.parse(options.body);return new Promise((resolve,reject)=>{finish=resolve;fail=reject;});};
navigateSetup(3);await until(()=>Boolean(finish));
window.scrollY=304;await click('manage-questions');
let expectedFocus;
if(returnToSetup){await click('setup-workspace-back');expectedFocus=document.activeElement;}
else{
 await click('add-example');const input=document.querySelector('#dataset-example-form').elements.message_0_content;
 type(input,'Unsaved editor content');expectedFocus=input;window.scrollY=420;
}
const step=state.setupStep,scroll=window.scrollY,editor=state.datasetEditor;
if(outcome==='success')finish({config:posted,request_plan:{rows:[],errors:[],resource_issues:[]}});
else fail(Object.assign(new Error('Late evaluator error'),{errors:[{fields:['judging.judges.0.model'],msg:'Configure evaluator'}]}));
for(let i=0;i<30;i++)await Promise.resolve();
assert.equal(state.setupWorkspace?.view||'setup',returnToSetup?'setup':'question-editor');
assert.equal(state.setupStep,step);assert.equal(window.scrollY,scroll);assert.equal(document.activeElement,expectedFocus);
assert.equal(state.datasetEditor,editor);assert.equal(document.querySelector('#evaluator-form'),null);
if(!returnToSetup)assert.equal(document.querySelector('#dataset-example-form').elements.message_0_content.value,'Unsaved editor content');
''')


def test_implicit_review_and_launch_keep_missing_evaluator_in_compact_setup():
    navigation_browser(r'''
state.config.request_pack.cases[0].scorer='judge';renderNew();validateRunForm=()=>true;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config:readForm(true).config,plan:{rows:[],errors:[],resource_issues:[]}};
navigateSetup(3);
assert.equal(state.setupWorkspace,null);assert.equal(document.querySelector('#evaluator-form'),null);
assert.match(document.querySelector('[data-question-setup-summary]').textContent,/Configuration needed/);
assert(visible('[data-action="configure-evaluator"]'));
revealSetupStep(3);
await startRun({target:document.querySelector('#new-run-form'),preventDefault(){}});
assert.equal(state.setupWorkspace,null);assert.equal(state.setupStep,2);
assert.equal(document.querySelector('#evaluator-form'),null);
assert.equal(document.activeElement.dataset.action,'configure-evaluator');
assert(document.activeElement.getClientRects().length);
assert.equal(requests.length,0,'Missing evaluator validation must precede any run or provider request');
''')


def test_focused_view_shortcuts_and_primary_commands_cannot_launch_the_hidden_run():
    navigation_browser(r'''
renderNew();await click('manage-questions');
const form=document.querySelector('#new-run-form');
for(const view of ['questions','question-editor','question-import','evaluator']){
 if(view==='question-editor')await click('add-example');
 if(view==='question-import')await click('import-pack');
 if(view==='evaluator')await click('configure-evaluator');
 assert.equal(state.setupWorkspace.view,view);
 const revision=state.configRevision,configBefore=JSON.stringify(state.config),target=document.querySelector('#setup-workspace-title');
 for(const event of [{key:'Enter',ctrlKey:true},{key:'Enter',metaKey:true},{key:'k',ctrlKey:true},{key:'n'},{key:'g'},{key:'r'}])listeners.keydown({target,preventDefault(){},...event});
 commandItems.find(command=>command.label==='Primary action').action();
 await startRun({target:form,preventDefault(){}});
 assert.equal(state.setupWorkspace.view,view);assert.equal(state.configRevision,revision);
 assert.equal(JSON.stringify(state.config),configBefore);assert.equal(location.hash,'#new');
 assert.equal(document.querySelector('#command-dialog').open,false);assert.equal(requests.length,0);
 if(view==='question-editor')await click('cancel-question-edit');
 if(view==='question-import')await click('cancel-question-import');
 if(view==='evaluator')await click('cancel-evaluator');
}
''')


def test_explicit_trial_preserves_its_request_contract_without_late_navigation():
    navigation_browser(r'''
renderNew();await click('manage-questions');await click('try-example');
assert.equal(document.querySelectorAll('dialog[open]').length,1);
let finish,sent;setActive=id=>{state.active=id;};
api=(path,options)=>{assert.equal(path,'/api/runs/try-example');sent=JSON.parse(options.body);return new Promise(resolve=>{finish=resolve;});};
const before=JSON.stringify(state.config.request_pack),retention=state.config.retention;
const pending=tryDatasetExample({preventDefault(){}});await until(()=>Boolean(finish));
assert.equal(sent.fixture_id,'alpha');assert.equal(sent.target_name,'local');assert.equal(sent.config.retention,'all');
assert.equal(state.config.retention,retention);
await click('close-dataset');await click('setup-workspace-back');
finish({id:'trial-result'});await pending;
assert.equal(location.hash,'#new');assert.equal(state.setupWorkspace,null);assert.equal(state.active,'trial-result');
assert.equal(JSON.stringify(state.config.request_pack),before);
assert.equal(document.querySelector('#dataset-dialog').innerHTML,'');
''')


def test_pending_editor_save_preserves_user_content_and_restores_controls_on_failure():
    navigation_browser(r'''
renderNew();await click('manage-questions');await click('add-example');
const form=document.querySelector('#dataset-example-form');
type(form.elements.message_0_content,'Question being validated');type(form.elements.example_expected,'Expected answer');
const controls=form.querySelectorAll('input,select,textarea,button'),disabled=controls.map(input=>input.disabled);
let fail,packet;api=(path,options)=>{assert.equal(path,'/api/config/draft');packet=JSON.parse(options.body);return new Promise((resolve,reject)=>{fail=reject;});};
const pending=saveExample({preventDefault(){}});await until(()=>Boolean(fail));
assert.equal(packet.request_pack.cases.at(-1).messages[0].content,'Question being validated');
assert.equal(visible('[data-action="cancel-question-edit"]').disabled,false);
assert.equal(visible('[data-action="setup-workspace-back"]').disabled,false);
const content=form.elements.message_0_content,locked=content.disabled||content.readOnly||Boolean(content.closest('fieldset[disabled]'));
if(!locked)type(content,'Newer unsaved content');
fail(new Error('Question validation failed'));await pending;
assert.equal(state.setupWorkspace.view,'question-editor');assert.equal(state.configBusy,false);
assert.equal(content.value,locked?'Question being validated':'Newer unsaved content');
assert.deepEqual(controls.map(input=>input.disabled),disabled);
assert.match(document.querySelector('#dataset-error').textContent,/Question validation failed/);
assert.equal(datasetCases().length,2);
''')


def test_import_mapping_refresh_preserves_the_active_control_and_main_scroll():
    navigation_browser(r'''
renderNew();await click('manage-questions');await click('import-pack');
const pack={version:1,cases:[{fixture_id:'mapped',messages:[{role:'user',content:'Mapped question'}],scorer:'exact',expected:'Mapped answer'}]};
api=async()=>({columns:['question','answer'],preview_rows:[{question:'Mapped question',answer:'Mapped answer'}],suggested_mapping:{input_field:'question',expected_field:'answer'},request_pack:null,errors:[],needs_mapping:true});
await importRequestPack({name:'questions.csv',size:60,text:async()=>'question,answer\nMapped question,Mapped answer'});
const form=document.querySelector('#dataset-import-form'),method=form.elements.import_scorer;
method.focus({preventScroll:true});window.scrollY=430;change(method,'exact');
assert.equal(document.querySelector('#dataset-import-form'),form);
assert.equal(document.activeElement,method);assert.equal(window.scrollY,430);
let finish;api=()=>new Promise(resolve=>{finish=resolve;});
const pending=previewDatasetMapping();await until(()=>Boolean(finish));
assert.equal(document.querySelector('#dataset-import-form'),form);assert.equal(document.activeElement,method);assert.equal(window.scrollY,430);
finish({columns:['question','answer'],preview_rows:[{question:'Mapped question',answer:'Mapped answer'}],request_pack:pack,errors:[]});await pending;
assert.equal(document.querySelector('#dataset-import-form'),form);assert.equal(document.activeElement,method);assert.equal(window.scrollY,430);
assert.equal(state.datasetImport.mappingDirty,false);assert.equal(state.config.request_pack.cases[0].fixture_id,'alpha');
''')


@pytest.mark.parametrize("outcome", ["success", "error"])
def test_cancelled_import_preview_cannot_reopen_or_replace_the_question_list(outcome):
    navigation_browser("const outcome=" + json.dumps(outcome) + r''';
renderNew();await click('manage-questions');
type(visible('[name="question_search"]'),'alpha');window.scrollY=310;
const before=JSON.stringify(state.config.request_pack);let finish,fail;
api=()=>new Promise((resolve,reject)=>{finish=resolve;fail=reject;});
const pack={version:1,cases:[{fixture_id:'late',messages:[{role:'user',content:'Late file'}],scorer:'exact',expected:'Late answer'}]};
const pending=importRequestPack({name:'late.json',size:100,text:async()=>JSON.stringify(pack)});
await until(()=>Boolean(finish));await click('cancel-question-import');
if(outcome==='success')finish({request_pack:pack,preview_rows:pack.cases,errors:[]});else fail(new Error('Late import failure'));
await pending;
assert.equal(state.setupWorkspace.view,'questions');assert.equal(state.datasetImport,null);
assert.equal(JSON.stringify(state.config.request_pack),before);
assert.equal(visible('[name="question_search"]').value,'alpha');assert.equal(window.scrollY,310);
assert.equal(document.querySelector('#dataset-import-form'),null);
assert.doesNotMatch(document.querySelector('#question-manager-error').textContent,/Late import failure/);
''')


def test_late_new_run_validation_cannot_replace_a_question_edit():
    navigation_browser(r'''
renderNew();
const setup=document.querySelector('#new-run-form'),endpoint=setup.elements.url;
let finish,posted;
api=(path,options)=>{assert.equal(path,'/api/config/draft');posted=JSON.parse(options.body);return new Promise(resolve=>{finish=resolve;});};
const pending=startNewRun();await until(()=>Boolean(finish));
await click('manage-questions');await click('add-example');
const form=document.querySelector('#dataset-example-form'),editor=state.datasetEditor;
type(form.elements.message_0_content,'Unsaved question after New run');
window.scrollY=418;
const focus=document.activeElement,step=state.setupStep,revision=state.configRevision;
finish({config:{...posted,max_requests:1},migration_notes:['Stale normalization']});await pending;
assert.equal(state.setupWorkspace.view,'question-editor');assert.equal(state.datasetEditor,editor);
assert.equal(document.querySelector('#dataset-example-form'),form);
assert.equal(form.elements.message_0_content.value,'Unsaved question after New run');
assert.equal(document.querySelector('#new-run-form'),setup);assert.equal(setup.elements.url,endpoint);
assert.equal(state.config.max_requests,55);assert.equal(state.setupStep,step);assert.equal(state.configRevision,revision);
assert.equal(window.scrollY,418);assert.equal(document.activeElement,focus);
assert(!state.migrationNotes.includes('Stale normalization'));assert.equal(location.hash,'#new');
''')
