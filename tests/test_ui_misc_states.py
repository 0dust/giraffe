"""Performance resets and setup landings discard flags from the previous task."""

import pytest

from tests.test_ux_catalogue import browser_logic


PERFORMANCE_SETUP = r"""
location.hash='#runs/saved-run';
state.detail={id:'saved-run',state:'running',config};state.reportRouteActive=true;
renderDetail=updateReportSections=announceRunComplete=finishPendingReportReveal=()=>{};
syncReportView();
const loads=[];
api=path=>{
  if(path.endsWith('/compare'))return {};
  assert(path.endsWith('/performance'),path);
  return new Promise((resolve,reject)=>loads.push({resolve,reject}));
};
async function resetSelection(){
  if(RESET==='terminal'){
    state.detail.state='completed';syncReportView();
  }else{
    globalThis.FormData=class{get(){return 'reference';}};
    const button={},error={},dialog={open:true,close(){this.open=false;}};
    nodes['#comparison-dialog']=dialog;
    const form={dataset:{runId:'saved-run'},isConnected:true,
      querySelector:selector=>selector==='[type="submit"]'?button:error};
    fetchRunDetail=async()=>({...state.detail,state:'completed'});
    chooseReportTab=()=>{reportView.tab='results';};toast=()=>{};
    await compareSavedRun({preventDefault(){},target:form});
  }
}
"""


@pytest.mark.parametrize('reset', ['terminal', 'comparison'])
def test_reset_during_performance_fetch_discards_queued_reload(reset):
    browser_logic(PERFORMANCE_SETUP.replace('RESET', repr(reset)) + r"""
const superseded=loadPerformance({force:true});
await loadPerformance({force:true});
assert.equal(loads.length,1);
await resetSelection();
const current=loadPerformance({force:true});
assert.equal(loads.length,2);
const value={targets:[],available_targets:[]};
loads[1].resolve(value);await current;
assert.equal(loads.length,2,'reset must discard the old queued forced reload');
assert.equal(reportView.pending,false);
loads[0].resolve({stale:true});await superseded;
assert.equal(reportView.performance,value,'the old request must remain invalidated');
assert.equal(loads.length,2);
""")


@pytest.mark.parametrize('reset', ['terminal', 'comparison'])
def test_performance_reset_clears_previous_error(reset):
    browser_logic(PERFORMANCE_SETUP.replace('RESET', repr(reset)) + r"""
const failed=loadPerformance({force:true});
loads[0].reject(new Error('Previous performance error'));await failed;
assert.equal(reportView.error,'Previous performance error');
await resetSelection();
assert.equal(reportView.error,'','reset must clear errors from the old selection');
assert.equal(reportView.performance,null);assert.equal(reportView.pending,false);
assert.equal(reportView.lastFetch,0);
""")


def test_migrated_rerun_landing_on_step_one_clears_return_to_review():
    browser_logic(r"""
const saved=structuredClone(config);delete saved.config_version;
state.detail={config:saved};state.setupStep=2;state.returnToReview=true;
api=async path=>{
  assert.equal(path,'/api/config/draft');
  return {config:structuredClone(config),migration_notes:['Migrated saved configuration.']};
};
await rerun();
assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
assert.equal(state.returnToReview,false,'a fresh step-one landing must not jump back to review');
// A requested checks landing that falls back to connection also clears the flag.
state.returnToReview=true;config.targets[0].model='';
await rerun({step:2});
assert.equal(state.setupStep,1);assert.equal(state.returnToReview,false);
""")
