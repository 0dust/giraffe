"""Disk-owned CLI runs are view-only and do not block web runs."""
from tests.test_ui_navigation import run_browser_script


def test_cli_notice_leaves_start_enabled_and_live_detail_has_no_stop():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  api=async()=>({active_run_id:null,disk_active_id:'cli',runs:[{id:'cli',state:'running'}]});
  await refreshRuns();assert.equal(state.active,null);assert.equal(state.diskActive,'cli');
  currentRequestPlan=()=>({});currentSettingErrors=()=>[];currentResourceErrors=()=>[];tokenizerSetupReady=()=>true;
  assert.equal(canStartRun(),true);
  assert.match(newActiveNotice(),/Running from the CLI/);assert.doesNotMatch(newActiveNotice(),/stop it/);
  state.detail={id:'cli',state:'running',stoppable:false,config:state.config,events:[],live_requests:[]};
  renderDetail();assert.equal(state.active,null);assert.doesNotMatch(nodes['#main'].innerHTML,/data-action="stop"/);
  assert.match(nodes['#main'].innerHTML,/Running from the CLI/);
  let requests=0;api=async()=>{requests++;};await stopRun();assert.equal(requests,0);
})()
`);
""")


def test_memory_panel_discloses_separate_helper_memory():
    run_browser_script(r"""
const page=browser();await page.run('init()');
page.run(`assert.match(renderResources({},{}),/helper.*separate process.*not included/);`);
""")
