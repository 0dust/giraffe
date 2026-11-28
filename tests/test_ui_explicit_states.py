"""Explicit UI lifecycle transitions and superseded loaders, without sockets."""

from pathlib import Path

import pytest

from tests.test_ui_navigation import run_browser_script
from tests.test_ui_question_navigation import navigation_browser


def test_live_run_transitions_preserve_terminal_and_stopping_states():
    run_browser_script(r"""
const page=browser();
page.run(`
const incoming=['queued','running','stopping','completed','interrupted','error'];
const stopping=['stopping','stopping','stopping','completed','interrupted','error'];
for(const previous of [undefined,'queued','running']){
  incoming.forEach((value,i)=>{
    assert.equal(nextLiveRunState(previous,value),value);
    assert.equal(nextLiveRunState(previous,value,{stopRequested:true}),stopping[i]);
  });
}
for(const stopRequested of [false,true]){
  incoming.forEach((value,i)=>assert.equal(nextLiveRunState('stopping',value,{stopRequested}),stopping[i]));
  for(const previous of ['completed','interrupted','error']){
    const expected=[previous,previous,previous,'completed','interrupted','error'];
    incoming.forEach((value,i)=>assert.equal(nextLiveRunState(previous,value,{stopRequested}),expected[i]));
  }
}
assert.equal(nextLiveRunState('running','unknown'),'unknown');
assert.equal(nextLiveRunState('completed','unknown'),'completed');
`);
""")


def test_current_discovery_requires_matching_key_and_pending_route():
    run_browser_script(r"""
const page=browser();await page.run('init()');
page.run(`
nodes['#new-run-form']={elements:{url:{value:'http://localhost:8000/v1'},model:{value:'test-model'}}};
assert.equal(currentDiscovery(),null);
const discovery={key:modelDiscoveryKey(),route:state.routeVersion,status:'connected'};
state.modelDiscovery=discovery;assert.equal(currentDiscovery(),discovery);
nodes['#new-run-form'].elements.model.value='different-model';
assert.equal(currentDiscovery(),null);assert.equal(state.modelDiscovery,discovery);
nodes['#new-run-form'].elements.model.value='test-model';
state.routeVersion++;assert.equal(currentDiscovery(),discovery);
state.modelDiscovery={...discovery,status:'pending'};assert.equal(currentDiscovery(),null);
state.modelDiscovery={...state.modelDiscovery,route:state.routeVersion};
assert.equal(currentDiscovery(),state.modelDiscovery);
`);
""")


def test_settled_discovery_survives_navigation_and_stale_result_alone_does_not_block_launch():
    run_browser_script(r"""
const page=browser();await page.run('init()');
page.run(`
nodes['#new-run-form']={elements:{url:{value:'http://localhost:8000/v1'},model:{value:'test-model'}}};
const status=nodes['#model-discovery-status']={textContent:'',classList:{toggle(){}}};
const button=nodes['[data-action="test-connection"]']={textContent:'',classList:{toggle(){}}};
syncSetupStep=()=>{};
currentRequestPlan=()=>({});currentSettingErrors=()=>[];currentResourceErrors=()=>[];tokenizerSetupReady=()=>true;
const discoveryRoute=state.routeVersion;state.routeVersion+=2;
for(const value of ['connected','unverified','needs_model','error']){
  const discovery={key:modelDiscoveryKey(),route:discoveryRoute,status:value,message:value==='connected'?'Connected · test-model':value};
  state.modelDiscovery=discovery;
  assert.equal(currentDiscovery(),discovery);
  renderModelDiscovery();
  assert.equal(status.textContent,discovery.message);
  assert.equal(button.textContent,value==='connected'?'Reconnect':'Connect');
  assert.equal(button.disabled,false);
}
state.modelDiscovery={key:modelDiscoveryKey(),route:discoveryRoute,status:'pending',message:'Checking your endpoint.'};
assert.equal(currentDiscovery(),null);assert.equal(canStartRun(),true);
renderModelDiscovery();
assert.equal(state.modelDiscovery,null);assert.equal(status.textContent,'');
assert.equal(button.textContent,'Connect');assert.equal(button.disabled,false);
assert.equal(canStartRun(),true);
`);
""")


@pytest.mark.parametrize('change', ["nodes['#new-run-form'].elements.model.value='other-model'",
                                  'state.routeVersion++'])
@pytest.mark.parametrize('outcome', ['resolve', 'reject'])
def test_superseded_connection_fetch_blocks_launch_until_settled(change, outcome):
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
currentRequestPlan=()=>({});currentSettingErrors=()=>[];currentResourceErrors=()=>[];tokenizerSetupReady=()=>true;
nodes['#new-run-form']={elements:{
  url:{value:'http://localhost:8000/v1',setCustomValidity(){},checkValidity:()=>true},
  model:{value:'test-model'}
}};
let requests=0,resolve,reject;fetch=()=>{requests++;return new Promise((done,fail)=>{resolve=done;reject=fail;});};
assert.equal(canStartRun(),true);
const pending=testConnection();
assert.equal(requests,1);assert.equal(state.modelDiscovery.status,'pending');
assert.equal(canStartRun(),false);
CHANGE;
assert.equal(state.modelDiscovery.status,'pending');
assert.equal(canStartRun(),false);
if('OUTCOME'==='resolve')resolve({ok:true,json:async()=>({status:'unverified',model:'test-model'})});
else reject(Error('Connection failed'));
await pending;
assert.equal(canStartRun(),true);
})()
`);
""".replace('CHANGE', change).replace('OUTCOME', outcome))


@pytest.mark.parametrize('outcome', ['resolve', 'reject'])
def test_navigation_keeps_launch_disabled_until_connection_request_settles(outcome):
    navigation_browser(r'''
currentRequestPlan=()=>({});currentSettingErrors=()=>[];currentResourceErrors=()=>[];tokenizerSetupReady=()=>true;
renderModelDiscovery=()=>syncSetupStep();
refreshRuns=async()=>{};
updateNav=()=>{};
let resolve,reject;api=()=>new Promise((done,fail)=>{resolve=done;reject=fail;});
location.hash='#new';await route();
assert(document.querySelector('#start-run'),document.querySelector('#main').textContent);
state.setupStep=3;syncSetupStep();
assert.equal(document.querySelector('#start-run').disabled,false);
const pending=testConnection();
assert.equal(document.querySelector('#start-run').disabled,true);
location.hash='#runs';await route();
location.hash='#new';await route();
state.setupStep=3;syncSetupStep();
assert.equal(currentDiscovery(),null);
assert.equal(document.querySelector('#start-run').disabled,true);
if('OUTCOME'==='resolve')resolve({status:'unverified',model:'test'});
else reject(Error('Connection failed'));
await pending;
assert.equal(document.querySelector('#start-run').disabled,false);
'''.replace('OUTCOME', outcome))


def test_new_performance_load_clears_error_but_dropped_load_keeps_it():
    navigation_browser(r"""
  state.detail={id:'saved',state:'completed'};syncReportView();
  renderDetail();
  api=async()=>{throw Error('Old performance failure');};
  await loadPerformance({force:true});
  assert.equal(reportView.error,'Old performance failure');assert.equal(reportView.pending,false);
  assert(document.querySelector('#report-performance').textContent.includes('Old performance failure'));
  await loadPerformance();assert.equal(reportView.error,'Old performance failure');
  reportView.target='other-target';reportView.window='other-window';
  let resolve;api=()=>new Promise(done=>{resolve=done;});
  const pending=loadPerformance({force:true});
  assert.equal(reportView.pending,true);assert.equal(reportView.error,'');
  assert(!renderPerformance().includes('Old performance failure'));
  assert(!document.querySelector('#report-performance').textContent.includes('Old performance failure'));
  resolve({targets:[]});await pending;assert.equal(reportView.pending,false);
""")


@pytest.mark.parametrize('supersede', ['run', 'terminal', 'comparison'])
def test_performance_invalidation_resets_pending_and_ignores_old_finally(supersede):
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  const transition='SUPERSEDE';
  state.detail={id:'saved',state:transition==='terminal'?'running':'completed'};
  syncReportView();
  let finishOld;api=()=>new Promise(resolve=>{finishOld=resolve;});
  const old=loadPerformance({force:true}),sequence=reportView.sequence;
  assert.equal(reportView.pending,true);
  if(transition==='run'){
    state.detail={id:'different',state:'completed'};syncReportView();
  }else if(transition==='terminal'){
    state.detail={id:'saved',state:'completed'};syncReportView();
  }else{
    const button={disabled:false};
    nodes['#comparison-error']={};nodes['#comparison-dialog']={close(){}};
    FormData=class{get(){return 'baseline';}};
    chooseReportTab=()=>{};renderDetail=()=>{};toast=()=>{};
    location.hash='#runs/saved';state.reportRouteActive=true;
    api=async path=>path.endsWith('/compare')?{}:{id:'saved',state:'completed'};
    await compareSavedRun({preventDefault(){},target:{dataset:{runId:'saved'},querySelector:()=>button}});
    assert.equal(pendingComparisons.size,0);assert.equal(button.disabled,false);
  }
  assert(reportView.sequence>sequence);assert.equal(reportView.pending,false);
  let finishNew;api=()=>new Promise(resolve=>{finishNew=resolve;});
  const fresh=loadPerformance({force:true});assert.equal(reportView.pending,true);
  finishOld({targets:[{name:'obsolete'}]});await old;
  assert.equal(reportView.pending,true);assert.equal(reportView.performance,null);
  const result={targets:[]};finishNew(result);await fresh;
  assert.equal(reportView.pending,false);assert.equal(reportView.performance,result);
})()
`);
""".replace('SUPERSEDE', supersede))


def test_run_state_pair_is_not_redeclared():
    source = (Path(__file__).parents[1] / 'src/giraffe/static/app.js').read_text()
    assert "['running','queued']" not in source
