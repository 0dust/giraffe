"""Exercise keyboard setup, reloads, and hash navigation without local sockets."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.models import RunConfig


STATIC = Path(__file__).parents[1] / "src/giraffe/static"


def run_browser_script(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise browser navigation")
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost:8000/v1",
                                "model": "test-model"}]).model_dump(mode="json")
    harness = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as formConfig from FORM_PATH;
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
const defaults=CONFIG;
const storage=new Map();
function browser(config=defaults,hash='#new'){
  const nodes={},listeners={},windowListeners={},paths=[];
  for(const id of ['main','connection-error','suite-version','runner-location','active-run-link','run-count','nav-runs','nav-new','breadcrumb']){
    nodes['#'+id]={innerHTML:'',textContent:'',classList:{toggle(){}},setAttribute(){},querySelectorAll:()=>[]};
  }
  const context={assert,structuredClone,...formConfig,nodes,listeners,windowListeners,paths,
    location:{hash},setTimeout(){},clearTimeout(){},
    sessionStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    document:{body:{dataset:{}},querySelector:q=>nodes[q]||null,querySelectorAll:()=>[],addEventListener:(kind,fn)=>listeners[kind]=fn},
    window:{addEventListener:(kind,fn)=>windowListeners[kind]=fn},
    fetch:async(path)=>{
      paths.push(path);
      const body=path==='/api/bootstrap'?{config:structuredClone(config),suite_version:'0.3.0'}:
        path==='/api/baselines'?{baselines:[]}:path==='/api/runs'?{runs:[]}:
        {id:decodeURIComponent(path.slice('/api/runs/'.length)),state:'completed',report:{run_id:'suite-run',overall:'inconclusive',checks:[],requests:[],manifest:{config}}};
      return {ok:true,json:async()=>body};
    }};
  vm.createContext(context);
  vm.runInContext(source+`\ninitReticulate=()=>{};renderNew=()=>{nodes['#main'].innerHTML='New run: '+state.config.targets[0].url;};`,context);
  return {...context,run:script=>vm.runInContext(script,context)};
}
""".replace("FORM_PATH", json.dumps((STATIC / "form-config.mjs").as_uri())).replace(
        "APP_PATH", json.dumps(str(STATIC / "app.js"))).replace("CONFIG", json.dumps(config))
    result = subprocess.run([node, "--input-type=module", "-"], input=harness + script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_archived_run_keeps_polling_active_status_without_replacing_the_report():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  let callback;
  schedulePoll=fn=>{callback=fn;};
  location.hash='#runs/archived';await route();
  assert.equal(callback,pollRunStatus);
  assert.match(document.title,/Run detail/);
  const markup=nodes['#main'].innerHTML;
  api=async()=>({runs:[{id:'live',state:'running',label:'Another run'}],active_run_id:'live'});
  await callback(state.routeVersion);assert.match(document.title,/In progress · Another run/);
  assert.equal(nodes['#main'].innerHTML,markup);
  api=async()=>({runs:[]});await callback(state.routeVersion);
  assert.equal(document.title,'Run detail · Giraffe');
})()
`);
""")


def test_live_detail_keeps_1800ms_polling_and_archived_status_uses_3000ms():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  const delays=[];schedulePoll=(fn,version,delay=1800)=>delays.push([fn,delay]);
  api=async()=>({id:'live',state:'running',config:state.config,progress:{},events:[]});
  location.hash='#runs/live';await route();assert.equal(delays.at(-1)[0],pollDetail);assert.equal(delays.at(-1)[1],1800);
  api=async()=>({id:'old',state:'completed',config:state.config,report:null});
  location.hash='#runs/old';await route();assert.equal(delays.at(-1)[0],pollRunStatus);assert.equal(delays.at(-1)[1],3000);
})()
`);
""")


def test_enter_in_connection_fields_uses_connect_and_respects_disabled_button():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
let connected=0,prevented=0,launched=0;
testConnection=async()=>{connected++;};startRun=()=>{launched++;};
const button={disabled:false,click:()=>listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'test-connection'}}:null}})};
nodes['[data-action="test-connection"]']=button;
const event=name=>({key:'Enter',target:{name,closest:q=>q==='#new-run-form'?{}:null},preventDefault(){prevented++;}});
for(const name of ['url','model']){
  listeners.keydown(event(name));await Promise.resolve();
}
assert.equal(connected,2);assert.equal(prevented,2);assert.equal(launched,0);
button.disabled=true;listeners.keydown(event('url'));assert.equal(connected,2);assert.equal(prevented,3);
button.disabled=false;listeners.keydown({...event('model'),repeat:true});assert.equal(connected,2);assert.equal(prevented,4);
listeners.keydown({...event('model'),isComposing:true});assert.equal(connected,2);
})()
`);
""")


def test_live_mosaic_paging_pauses_follow_and_returning_to_live_resumes_it():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
state.detail={id:'live',state:'running',config:state.config,
 live_requests:Array.from({length:254},(_,slot)=>({slot,status:slot<250?'completed':'running'}))};
state.detail.config.max_requests=600;renderDetail();assert.equal(state.mosaicPage,1);
let focused=0;
nodes['.request-mosaic [tabindex="0"]']={focus(options){assert.equal(options.preventScroll,true);focused++;}};
const click=action=>listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action}}:null}});
await click('mosaic-next');assert.equal(state.mosaicPage,2);assert.equal(state.mosaicFollow,false);
renderDetail();assert.equal(state.mosaicPage,2);
await click('mosaic-live');assert.equal(state.mosaicPage,1);assert.equal(state.mosaicFollow,true);
await click('mosaic-prev');assert.equal(state.mosaicPage,0);assert.equal(state.mosaicFollow,false);
await click('mosaic-next');assert.equal(state.mosaicPage,1);assert.equal(state.mosaicFollow,true);
assert.equal(focused,4);
state.mosaicFollow=false;location.hash='#runs/another';await route();
assert.equal(state.mosaicFollow,true);assert.equal(state.mosaicPage,0);
})()
`);
""")


def test_live_mosaic_controls_keep_following_after_programmatic_focus():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
const spots={};
for(const slot of [240,480,481,720]){
 const spot={dataset:{liveSlot:String(slot)},closest:()=>spot,getAttribute:()=>null,getBoundingClientRect:()=>({left:0,top:0,width:10}),
  focus(options){assert.equal(options.preventScroll,true);document.activeElement=this;listeners.focusin({target:this});}};
 spots[slot]=spot;
 Object.defineProperty(nodes,'[data-live-slot="'+slot+'"]',{get:()=>Math.floor(slot/240)===state.mosaicPage?spot:null});
}
Object.defineProperty(nodes,'.request-mosaic [tabindex="0"]',{get:()=>spots[state.mosaicPage*240]});
const click=action=>listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action}}:null}});
for(const action of ['mosaic-live','mosaic-next','mosaic-prev']){
 document.activeElement=null;state.mosaicFollow=false;state.mosaicPage=action==='mosaic-prev'?2:0;
 state.detail={id:'live',state:'running',config:{...state.config,max_requests:960},
  live_requests:Array.from({length:254},(_,slot)=>({slot,status:slot<250?'completed':'running'}))};
 renderDetail();await click(action);
 assert.equal(state.mosaicPage,1);assert.equal(state.mosaicFollow,true);assert.equal(document.activeElement,spots[240]);
 let next={...state.detail,live_requests:[...state.detail.live_requests,{slot:480,status:'running'}]};
 api=async()=>next;refreshRuns=async()=>{};
 await pollDetail(state.routeVersion);
 assert.equal(state.mosaicPage,2);assert.equal(state.mosaicFollow,true);
 assert.equal(document.activeElement,spots[480]);assert.equal(state.mosaicAutoFocus,'480');
 // The transferred focus must also follow the next page boundary.
 next={...next,live_requests:[...next.live_requests,{slot:720,status:'running'}]};
 await pollDetail(state.routeVersion);
 assert.equal(state.mosaicPage,3);assert.equal(state.mosaicFollow,true);assert.equal(document.activeElement,spots[720]);
 // A user focus change clears the exemption, even if they return to that spot.
 state.mosaicPage=2;state.mosaicFollow=true;state.mosaicAutoFocus='480';
 spots[481].focus({preventScroll:true});spots[480].focus({preventScroll:true});
 await pollDetail(state.routeVersion);
 assert.equal(state.mosaicPage,2);assert.equal(state.mosaicFollow,false);assert.equal(document.activeElement,spots[480]);
}
})()
`);
""")


def test_live_mosaic_poll_preserves_keyboard_inspection_and_page_at_run_end():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
state.detail={id:'live',state:'running',config:{...state.config,max_requests:600},
 live_requests:Array.from({length:240},(_,slot)=>({slot,status:slot===239?'running':'completed'}))};
renderDetail();assert.equal(state.mosaicPage,0);
let restored=0;
const spot={dataset:{liveSlot:'239'},closest:q=>q==='.request-mosaic'?{}:null,focus(options){assert.equal(options.preventScroll,true);document.activeElement=this;restored++;}};
document.activeElement=spot;nodes['[data-live-slot="239"]']=spot;
let next={...state.detail,live_requests:[...state.detail.live_requests,{slot:240,status:'running'}]};
api=async()=>next;refreshRuns=async()=>{};
await pollDetail(state.routeVersion);
assert.equal(state.mosaicFollow,false);assert.equal(state.mosaicPage,0);assert.equal(state.mosaicLivePage,1);
assert.equal(document.activeElement,spot);assert.equal(restored,1);assert.match(nodes['#main'].innerHTML,/data-live-slot="239"/);
next={...next,live_requests:[...next.live_requests,{slot:490,status:'running'}]};
await pollDetail(state.routeVersion);assert.equal(state.mosaicPage,0);assert.equal(state.mosaicLivePage,2);
// Explicitly returning to live overrides inspection of an earlier spot.
nodes['.request-mosaic [tabindex="0"]']={focus(options){assert.equal(options.preventScroll,true);document.activeElement={dataset:{liveSlot:'480'}};}};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'mosaic-live'}}:null}});
assert.equal(state.mosaicPage,2);assert.equal(state.mosaicFollow,true);
// With no spot focused, polls follow the frontier across a page boundary.
document.activeElement=null;state.mosaicPage=0;
await pollDetail(state.routeVersion);assert.equal(state.mosaicPage,2);assert.equal(state.mosaicFollow,true);
// Ending a run keeps the shown page even when following was enabled.
state.mosaicPage=0;
next={id:'live',state:'completed',report:{run_id:'live',overall:'inconclusive',checks:[],
 requests:Array.from({length:600},(_,i)=>({id:'request-'+i,status:'completed'})),manifest:{config:state.config}}};
await pollDetail(state.routeVersion);assert.equal(state.mosaicPage,0);
assert.doesNotMatch(nodes['#main'].innerHTML,/mosaic-live|Following live/);
})()
`);
""")


def test_bootstrap_defaults_scope_remembered_form_drafts_across_reloads():
    run_browser_script(r"""
const first=browser();await first.run('init()');
first.run(`
nodes['#new-run-form']={elements:{baseline_id:{value:''},mode:{value:'normal'},restart_target:{value:''}}};
readForm=()=>({config:{...state.config,targets:[{...state.config.targets[0],url:'invalid stale address',model:'draft-model'}]}});
windowListeners.pagehide();
`);
assert(storage.size>0);
const reload=browser();await reload.run('init()');
assert.match(reload.nodes['#main'].innerHTML,/invalid stale address/);
assert.equal(reload.run('state.config.targets[0].model'),'draft-model');
// Object key order is immaterial; changed server defaults take precedence.
const reordered=Object.fromEntries(Object.entries(defaults).reverse());
const same=browser(reordered);await same.run('init()');
assert.match(same.nodes['#main'].innerHTML,/invalid stale address/);
const changed=structuredClone(defaults);changed.targets[0].url='http://localhost:9000/v1';
const fresh=browser(changed);await fresh.run('init()');
assert.match(fresh.nodes['#main'].innerHTML,/http:\/\/localhost:9000\/v1/);
assert.equal(fresh.run('state.config.targets[0].model'),'test-model');
// Storage failures and malformed drafts must leave server defaults usable.
fresh.run('windowListeners.pagehide()');
for(const key of storage.keys())storage.set(key,'{broken');
const malformed=browser(changed);await malformed.run('init()');
assert.match(malformed.nodes['#main'].innerHTML,/localhost:9000/);
const blocked=browser();blocked.sessionStorage.getItem=()=>{throw Error('blocked');};
blocked.sessionStorage.setItem=()=>{throw Error('blocked');};
await blocked.run('init()');assert.match(blocked.nodes['#main'].innerHTML,/localhost:8000/);
blocked.run('windowListeners.pagehide()');
""")


def test_initial_and_changed_hash_load_run_details_without_waiting_for_history():
    run_browser_script(r"""
const page=browser(defaults,'#runs/saved%20run');
page.run(`var fetchBefore=fetch;fetch=async(path,...args)=>path==='/api/runs'?new Promise(()=>{}):fetchBefore(path,...args);`);
await Promise.race([page.run('init()'),new Promise((_,reject)=>setTimeout(()=>reject(Error('Detail waited for Runs history')),2000))]);
assert(page.paths.includes('/api/runs/saved%20run'));
assert.match(page.nodes['#main'].innerHTML,/suite-run/);
page.location.hash='#runs/next';await page.windowListeners.hashchange();
assert(page.paths.includes('/api/runs/next'));assert.equal(page.run('state.detail.id'),'next');
// A stale response must not replace the page selected by a newer hash.
page.run(`var originalApi=api,finishOld;api=path=>path==='/api/runs/old'?new Promise(resolve=>finishOld=resolve):originalApi(path);`);
page.location.hash='#runs/old';const old=page.windowListeners.hashchange();
page.location.hash='#runs/new';await page.windowListeners.hashchange();
page.run(`finishOld({id:'old',state:'error',error:'Old response'});`);await old;
assert.equal(page.run('state.detail.id'),'new');
""")


def test_hash_changes_during_bootstrap_wait_for_config_then_open_latest_run():
    run_browser_script(r"""
const page=browser(defaults,'#new');
page.run(`var originalFetch=fetch,finishBootstrap;fetch=path=>path==='/api/bootstrap'?new Promise(resolve=>finishBootstrap=resolve):originalFetch(path);`);
const initializing=page.run('init()');
page.location.hash='#runs/latest';await page.windowListeners.hashchange();
assert(!page.paths.includes('/api/runs/latest'));
page.run(`finishBootstrap({ok:true,json:async()=>({config:${JSON.stringify(defaults)},suite_version:'0.3.0'})});`);
await initializing;assert(page.paths.includes('/api/runs/latest'));
assert.match(page.nodes['#main'].innerHTML,/suite-run/);
""")


def test_unknown_run_has_clear_state_and_link_back_without_hiding_server_errors():
    run_browser_script(r"""
const page=browser(defaults,'#runs/missing');
page.run(`var originalFetch=fetch;fetch=async(path,...args)=>path.startsWith('/api/runs/')?{ok:false,status:404,json:async()=>({detail:'Not Found'})}:originalFetch(path,...args);`);
await page.run('init()');
assert.match(page.nodes['#main'].innerHTML,/<h1>Run not found<\/h1>/);
assert.match(page.nodes['#main'].innerHTML,/href="#runs"/);
page.run(`fetch=async()=>({ok:false,status:500,json:async()=>({detail:'Disk unavailable'})});`);
page.location.hash='#runs/other';await page.windowListeners.hashchange();
assert.match(page.nodes['#main'].innerHTML,/Could not open this view/);
assert.match(page.nodes['#main'].innerHTML,/Disk unavailable/);
""")


def test_suite_label_and_validation_errors_are_readable_at_api_boundary():
    run_browser_script(r"""
const page=browser();await page.run('init()');
assert.equal(page.nodes['#suite-version'].textContent,'suite 0.3.0');
assert.match(fs.readFileSync(INDEX_PATH,'utf8'),/id="suite-version">suite/);
await page.run(`
(async()=>{
const message='target url must be an http(s) endpoint';
for(const detail of ['body: Value error, '+message,[{loc:['body'],msg:'Value error, '+message}],['body: Value error, '+message]]){
  fetch=async()=>({ok:false,status:422,json:async()=>({detail})});
  await assert.rejects(api('/api/targets/test-connection'),error=>error.status===422&&error.message===message);
}
fetch=async()=>({ok:false,status:422,json:async()=>({detail:[{loc:['body','targets',0,'url'],msg:'Value error, '+message},{loc:['body','max_requests'],msg:'Input should be greater than 0'}]})});
await assert.rejects(api('/api/config/validate'),error=>error.message==='targets.0.url: '+message+'\\nmax_requests: Input should be greater than 0');
})()
`);
""".replace("INDEX_PATH", json.dumps(str(STATIC / "index.html"))))


def test_run_summary_heads_connection_failures_without_capacity_wording():
    run_browser_script(r"""
const page=browser(defaults,'#runs/connection-failure');await page.run('init()');
page.run(`
const report=state.detail.report;
report.checks=[{id:'capacity',title:'Capacity',target:'local',status:'inconclusive',required:true,
  summary:'Highest tested acceptable arrival rate: not established',metrics:{highest_acceptable_rate_rps:null}}];
report.requests=[{id:'failed-request',fixture_id:'short',target:'local',scenario:'initial',status:'failed',error:'ConnectError',elapsed_ms:1}];
const summary='Could not connect to <local> (ConnectError); no checks could run';
report.observations={summary};
const before=JSON.stringify(report);
renderDetail();
const markup=nodes['#main'].innerHTML,heading=markup.split('<div class="report-toolbar')[0];
assert.match(heading,/<h1>Could not connect to &lt;local&gt; \\(ConnectError\\); no checks could run<\\/h1>/);
assert.doesNotMatch(heading,/capacity|highest tested acceptable arrival rate|not established/i);
// Keep diagnostic detail and verdicts intact beneath the overall summary.
assert.match(markup,/Highest tested acceptable arrival rate: not established/);
assert.equal(report.checks[0].status,'inconclusive');assert.equal(report.overall,'inconclusive');
assert.equal(JSON.stringify(report),before);
// Reports written before observations.summary retain their existing headline and guidance.
for(const observations of [undefined,{}, {summary:''}, {summary:'  '}, {summary:null}]){
  report.observations=observations;renderDetail();
  const fallback=nodes['#main'].innerHTML.split('<div class="report-toolbar')[0];
  assert.match(fallback,/<h1>Capacity under load is inconclusive<\\/h1>/);
  assert.match(fallback,/Highest tested acceptable arrival rate: not established/);
}
`);
""")
