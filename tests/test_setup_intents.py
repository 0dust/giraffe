"""The latest accepted setup action owns every asynchronous continuation."""

import itertools
import json
import re

import pytest

from tests.test_ux_catalogue import browser_logic


ACTIONS = ('new', 'rerun', 'compare-run', 'choose-next-checks')

HARNESS = r'''
const initialState=clone(state),initialConfig=clone(config);
let requests=[],toasts=[],renders=0;
renderRequestPreview=()=>{};
renderNew=()=>{renders++;};
toast=message=>toasts.push(message);
function reset(){
  Object.assign(state,clone(initialState));
  for(const key of Object.keys(nodes))delete nodes[key];
  state.config=clone(initialConfig);state.config.targets[0].model='current-model';
  const saved=clone(initialConfig);saved.targets[0].model='saved-model';
  state.detail={id:'saved',report:{manifest:{config:saved,config_migration_warnings:[]}},baseline_id:'saved-parent'};
  state.baselines=[{id:'saved-baseline',run_id:'saved'}];
  state.prefillBaseline='current-baseline';state.setupStep=3;state.focusSetupOnEntry=true;
  state.migrationNotes=['initial-note'];state.gatewayAccessDraft={};
  location.hash='#runs/saved';requests=[];toasts=[];renders=0;
  nodes['#config-error']={hidden:true,textContent:''};
  nodes['#form-error']={hidden:true,textContent:''};
}
api=(path,options)=>new Promise((resolve,reject)=>{
  const body=JSON.parse(options.body);
  requests.push({path,body,
    succeed:()=>resolve({config:clone(body),migration_notes:[body.targets[0].model+'-note'],migration_warnings:[],migration_limit_notes:[]}),
    fail:()=>reject(new Error(body.targets[0].model+'-error'))});
});
function click(action){return listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action}}:null}});}
function act(action){return action==='new'?startNewRun():action==='apply'?click('apply-config'):click(action);}
async function flush(){for(let i=0;i<12;i++)await Promise.resolve();}
async function settle(index,success){requests[index][success?'succeed':'fail']();await flush();}
function snapshot(){return JSON.stringify({
  model:state.config.targets[0].model,baseline:state.prefillBaseline,step:state.setupStep,
  focus:state.focusSetupOnEntry,notes:state.migrationNotes,hash:location.hash,toasts,
  jsonDraft:state.jsonDraft,jsonValidation:state.jsonValidation,configError:nodes['#config-error'],
  formError:nodes['#form-error'],renders
});}
function prepareJson(){const edited=clone(initialConfig);edited.targets[0].model='json-model';state.jsonDraft=JSON.stringify(edited);state.jsonBase='old-json';}
reset();
'''


def run_script(body, **values):
    if values:
        body = re.sub(r"\b(?:" + "|".join(values) + r")\b",
                      lambda match: json.dumps(values[match[0]]), body)
    browser_logic(HARNESS + body)


@pytest.mark.parametrize('first,second', list(itertools.product(ACTIONS, repeat=2)))
def test_pair_matrix(first, second):
    run_script(r'''
for(const firstReply of [0,1])for(const firstOK of [false,true])for(const secondOK of [false,true])for(const delivered of [false,true]){
  reset();const alone=act(SECOND);await settle(0,secondOK);await alone;const expected=snapshot();
  reset();const first=act(FIRST),second=act(SECOND);assert.equal(requests.length,2);
  await settle(firstReply,firstReply===0?firstOK:secondOK);
  if(firstReply===1&&secondOK&&delivered)state.routeVersion++;
  await settle(1-firstReply,firstReply===0?secondOK:firstOK);await Promise.all([first,second]);
  assert.equal(snapshot(),expected,JSON.stringify({firstReply,firstOK,secondOK,delivered}));
}
''', FIRST=first, SECOND=second)


@pytest.mark.parametrize('action', ACTIONS)
@pytest.mark.parametrize('success', [False, True])
@pytest.mark.parametrize('hash_value,delivered', [('#runs', False), ('#runs', True), ('#runs/saved/results/checks', False)])
def test_leaving_page_drops_reply(action, success, hash_value, delivered):
    run_script(r'''
const pending=act(ACTION);location.hash=HASH;if(DELIVERED)state.routeVersion++;
const before=snapshot();await settle(0,SUCCESS);await pending;assert.equal(snapshot(),before);
''', ACTION=action, SUCCESS=success, HASH=hash_value, DELIVERED=delivered)


@pytest.mark.parametrize('noop', ['missing-config', 'hero-guard', 'busy-apply', 'invalid-json'])
def test_noop_does_not_cancel_new_run(noop):
    run_script(r'''
const pending=startNewRun();const sequence=state.setupNavigationSequence;
if(NOOP==='missing-config'){state.detail={id:'saved'};await click('rerun');assert.deepEqual(toasts,['No configuration was saved for this run.']);}
if(NOOP==='hero-guard'){state.heroActionGuard={id:'saved',until:Date.now()+10000};await click('choose-next-checks');}
if(NOOP==='busy-apply'){state.configBusy=true;await click('apply-config');state.configBusy=false;}
if(NOOP==='invalid-json'){state.jsonDraft='{ invalid';await click('apply-config');assert.equal(nodes['#config-error'].hidden,false);}
assert.equal(state.setupNavigationSequence,sequence);assert.equal(requests.length,1);
await settle(0,true);await pending;
assert.equal(state.config.targets[0].model,'current-model');assert.equal(location.hash,'#new');assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
''', NOOP=noop)


def test_busy_import_does_not_cancel_pending_import():
    run_script(r"""
const firstConfig=clone(initialConfig);firstConfig.targets[0].model='first-import';
const pending=importConfig({text:async()=>JSON.stringify(firstConfig)});await flush();
assert.equal(state.configBusy,true);assert.equal(requests.length,1);
const sequence=state.setupNavigationSequence;let reads=0;
await importConfig({text:async()=>{reads++;return JSON.stringify(initialConfig);}});
assert.equal(reads,0);assert.equal(requests.length,1);assert.equal(state.setupNavigationSequence,sequence);
await settle(0,true);await pending;
assert.equal(state.config.targets[0].model,'first-import');assert.equal(state.configBusy,false);
assert.deepEqual(toasts,['Configuration imported.']);
""")


def test_import_after_completed_import_applies_normally():
    run_script(r"""
for(const model of ['first-import','second-import']){
  const imported=clone(initialConfig);imported.targets[0].model=model;
  const sequence=state.setupNavigationSequence;
  const pending=importConfig({text:async()=>JSON.stringify(imported)});await flush();
  assert.equal(state.setupNavigationSequence,sequence+1);
  await settle(requests.length-1,true);await pending;
  assert.equal(state.config.targets[0].model,model);assert.equal(state.configBusy,false);
}
assert.equal(requests.length,2);assert.deepEqual(toasts,['Configuration imported.','Configuration imported.']);
""")


@pytest.mark.parametrize('action', ACTIONS)
@pytest.mark.parametrize('success', [False, True])
def test_config_edit_drops_reply(action, success):
    run_script(r'''
const pending=act(ACTION);configChanged();const before=snapshot();
await settle(0,SUCCESS);await pending;assert.equal(snapshot(),before);
''', ACTION=action, SUCCESS=success)


@pytest.mark.parametrize('first,second', [('apply', 'new'), ('new', 'apply')])
@pytest.mark.parametrize('first_reply', [0, 1])
@pytest.mark.parametrize('first_ok,second_ok', list(itertools.product([False, True], repeat=2)))
def test_apply_and_new_run(first, second, first_reply, first_ok, second_ok):
    run_script(r'''
reset();prepareJson();const alone=act(SECOND);await settle(0,SECOND_OK);await alone;const expected=snapshot();
reset();prepareJson();const dirty=state.jsonDraft;const first=act(FIRST),second=act(SECOND);
await settle(FIRST_REPLY,FIRST_REPLY===0?FIRST_OK:SECOND_OK);
await settle(1-FIRST_REPLY,FIRST_REPLY===0?SECOND_OK:FIRST_OK);await Promise.all([first,second]);
assert.equal(snapshot(),expected);
if(FIRST==='apply')assert.equal(state.jsonDraft,dirty);
''', FIRST=first, SECOND=second, FIRST_REPLY=first_reply, FIRST_OK=first_ok, SECOND_OK=second_ok)


@pytest.mark.parametrize('first', ['import', 'new'])
@pytest.mark.parametrize('success', [False, True])
def test_import_read_and_new_run(first, success):
    run_script(r'''
let read,readFail;const file={text:()=>new Promise((resolve,reject)=>{read=resolve;readFail=reject;})};
const imported=clone(initialConfig);imported.targets[0].model='import-model';
let older,newer;
if(FIRST==='import'){older=importConfig(file);newer=startNewRun();}
else {older=startNewRun();newer=importConfig(file);}
if(SUCCESS)read(JSON.stringify(imported));else readFail(new Error('file-error'));
await flush();
if(FIRST==='import'){
  assert.equal(requests.length,1);await settle(0,true);
  assert.equal(state.config.targets[0].model,'current-model');assert.equal(location.hash,'#new');assert.deepEqual(toasts,[]);
}else{
  if(SUCCESS){assert.equal(requests.length,2);await settle(1,true);}
  const before=snapshot();await settle(0,true);assert.equal(snapshot(),before);
  assert.equal(state.config.targets[0].model,SUCCESS?'import-model':'current-model');
  assert.equal(nodes['#config-error'].hidden,SUCCESS);
  if(!SUCCESS)assert.equal(nodes['#config-error'].textContent,'file-error');
}
await Promise.all([older,newer]);
''', FIRST=first, SUCCESS=success)


@pytest.mark.parametrize('winner', ['new', 'rerun'])
@pytest.mark.parametrize('first_reply', [0, 1])
@pytest.mark.parametrize('preview_ok', [False, True])
def test_pending_next_is_overtaken(winner, first_reply, preview_ok):
    run_script(r'''
location.hash='#new';state.setupStep=2;nodes['#request-plan']={};
validateRunForm=()=>true;currentRequestPlan=()=>null;currentSettingErrors=()=>[];
revealSetupStep=step=>{state.setupStep=step;};captureFormDraft=()=>{};
let continuations=0;validateSetupChecks=()=>{continuations++;};
refreshRequestPreview=()=>api('/api/config/preview',{body:JSON.stringify(state.config)});
navigateSetup(3);const pending=act(WINNER);assert.equal(requests.length,2);
await settle(FIRST_REPLY,FIRST_REPLY===0?PREVIEW_OK:true);
await settle(1-FIRST_REPLY,FIRST_REPLY===0?true:PREVIEW_OK);await pending;
assert.equal(continuations,0);assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
assert.equal(state.config.targets[0].model,WINNER==='rerun'?'saved-model':'current-model');
assert.equal(nodes['#form-error'].hidden,true);assert.deepEqual(toasts,[]);
''', WINNER=winner, FIRST_REPLY=first_reply, PREVIEW_OK=preview_ok)


@pytest.mark.parametrize('action', ['rerun', 'compare-run'])
@pytest.mark.parametrize('transition', ['open', 'close'])
@pytest.mark.parametrize('success', [False, True])
def test_workspace_transition_drops_saved_run_reply(action, transition, success):
    run_script(r"""
if(TRANSITION==='close')openSetupWorkspace('questions');
const pending=act(ACTION);const sequence=state.setupNavigationSequence;
if(TRANSITION==='open')openSetupWorkspace('questions');else closeSetupWorkspace();
assert.equal(state.setupNavigationSequence,sequence+1);
const workspace=state.setupWorkspace,before=snapshot();
await settle(0,SUCCESS);await pending;
assert.equal(snapshot(),before);assert.equal(state.setupWorkspace,workspace);assert.deepEqual(toasts,[]);
""", ACTION=action, TRANSITION=transition, SUCCESS=success)


FOCUS_HARNESS = r'''
let settingFocus=[],stepFocus=[];
updateNav=()=>{};enterRoute=()=>{};
focusSetting=(path,options)=>settingFocus.push({path,...options});
syncSetupStep=options=>{if(options?.focus)stepFocus.push(state.setupStep);};
nodes['#main']={innerHTML:''};
refreshRuns=async()=>{};renderRuns=()=>{};
'''


def test_timeout_rerun_focuses_setting_once_on_new_entry():
    run_script(FOCUS_HARNESS + r'''
const pending=rerun({focusPath:'load_test.request_timeout_seconds'});
assert.equal(pendingTimeoutSetting,null);
await settle(0,true);await pending;
assert.equal(location.hash,'#new');
assert.equal(pendingTimeoutSetting,'load_test.request_timeout_seconds');
await route();
assert.deepEqual(settingFocus,[{path:'load_test.request_timeout_seconds',explicit:true}]);
assert.deepEqual(stepFocus,[]);assert.equal(pendingTimeoutSetting,null);
await route();assert.equal(settingFocus.length,1);
''')


@pytest.mark.parametrize('replacement', ['new', 'away'])
@pytest.mark.parametrize('success', [False, True])
def test_superseded_timeout_rerun_cannot_focus_later_entry(replacement, success):
    run_script(FOCUS_HARNESS + r'''
const pending=rerun({focusPath:'load_test.request_timeout_seconds'});
let newer;
if(REPLACEMENT==='new')newer=startNewRun();
else {location.hash='#runs';await route();}
await settle(0,SUCCESS);await pending;
assert.equal(pendingTimeoutSetting,null);assert.deepEqual(toasts,[]);
if(newer){await settle(1,true);await newer;}
location.hash='#new';await route();
assert.deepEqual(settingFocus,[]);assert.equal(pendingTimeoutSetting,null);
''', REPLACEMENT=replacement, SUCCESS=success)


def test_rerun_step_two_keeps_normal_entry_focus():
    run_script(FOCUS_HARNESS + r'''
state.focusSetupOnEntry=false;
const pending=rerun({step:2});await settle(0,true);await pending;
assert.equal(location.hash,'#new');assert.equal(state.setupStep,2);
assert.equal(pendingTimeoutSetting,null);assert.equal(state.focusSetupOnEntry,true);
await route();
assert.deepEqual(settingFocus,[]);assert.deepEqual(stepFocus,[2]);
assert.equal(state.focusSetupOnEntry,false);
await route();assert.deepEqual(stepFocus,[2]);
''')


@pytest.mark.parametrize('replacement', [
    'new', 'rerun', 'choose-next-checks', 'apply', 'import',
    'workspace-open', 'workspace-close', 'navigate', 'away',
])
def test_stale_timeout_focus_is_cleared_by_new_setup_owner(replacement):
    run_script(FOCUS_HARNESS + r'''
if(REPLACEMENT==='workspace-close')openSetupWorkspace('questions');
// Seed an unconsumed flag, including the gap before a hashchange is delivered.
pendingTimeoutSetting='load_test.request_timeout_seconds';
let pending;
if(REPLACEMENT==='import'){
  pending=importConfig({text:async()=>JSON.stringify(initialConfig)});await flush();
}else if(REPLACEMENT==='apply'){
  prepareJson();pending=act('apply');
}else if(REPLACEMENT==='workspace-open')openSetupWorkspace('questions');
else if(REPLACEMENT==='workspace-close')closeSetupWorkspace();
else if(REPLACEMENT==='navigate'){
  location.hash='#new';captureFormDraft=()=>{};revealSetupStep=step=>{state.setupStep=step;};
  updateBounds=()=>{};navigateSetup(1);
}else if(REPLACEMENT==='away'){location.hash='#runs';await route();}
else pending=act(REPLACEMENT);
assert.equal(pendingTimeoutSetting,null);
if(requests.length){await settle(0,true);await pending;}
location.hash='#new';await route();assert.deepEqual(settingFocus,[]);
''', REPLACEMENT=replacement)
