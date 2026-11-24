"""Exercise the rendered result states without requiring a running LLM."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities


def run_render_checks(assertions: str, *, dom: bool = False, timeout_seconds: int = 60) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser result rendering")
    app = Path(__file__).parents[1] / "src/giraffe/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as formConfig from FORM_PATH;
import {browserDOM} from DOM_PATH;
const bootstrap=BOOTSTRAP;
const nodes={'#main':{innerHTML:''},'#run-filter':{value:'all'}},listeners={},warnings=[];
const context={assert,structuredClone,...formConfig,bootstrap,nodes,listeners,warnings,console:{warn:(...args)=>warnings.push(args)},
  document:{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener:(kind,fn)=>listeners[kind]=fn},
  window:{addEventListener(kind,fn){listeners['window:'+kind]=fn;}}};
if(USE_DOM){context.document=browserDOM(listeners);context.location={hash:'#runs/live'};context.setTimeout=()=>{};context.clearTimeout=()=>{};context.window.innerWidth=1280;context.window.innerHeight=800;context.window.matchMedia=()=>({matches:false});nodes['#main']=context.document.querySelector('#main');}
const source=fs.readFileSync(APP_PATH,'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
vm.runInNewContext(source+`
state.bootstrap=bootstrap;
function check(id,status,extra={}) {
  return {id,target:'local',title:id+' check',status,required:true,
    summary:'Recorded '+status+' reason',metrics:{},evidence_ids:[],...extra};
}
function report(checks,extra={}) {
  return {run_id:'saved-run',overall:'pass',started_at:'2026-10-15T10:00:00Z',
    finished_at:'2026-10-15T10:00:10Z',requests:[],checks,
    manifest:{config:{targets:[{name:'local',model:'test-model',url:'http://localhost:8000/v1'}],
      checks:checks.filter(c=>c.status!=='skipped').map(c=>c.id),limits:{}}},...extra};
}
function detail(value,extra={}) {
  state.detail={id:'saved-run',state:'completed',report:value,...extra};
  state.tab='checks';state.selectedCheck=null;renderDetail();return nodes['#main'].innerHTML;
}
`+ASSERTIONS,context);
""".replace("DOM_PATH", json.dumps((Path(__file__).parent / "ui_dom.mjs").as_uri())).replace("USE_DOM", "true" if dom else "false").replace("APP_PATH", json.dumps(str(app))).replace(
        "ASSERTIONS", json.dumps(assertions)
    ).replace("BOOTSTRAP", json.dumps(capabilities())).replace("FORM_PATH", json.dumps(app.with_name("form-config.mjs").as_uri()))
    result = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    assert result.returncode == 0, result.stderr


def test_post_run_baseline_picker_filters_self_and_ignores_stale_navigation():
    run_render_checks(r"""
(async()=>{
  const dialog=document.createElement('dialog');dialog.setAttribute('id','comparison-dialog');document.body.insertBefore(dialog,null);
  toast=message=>{throw Error(message);};
  detail(report([check('serving','pass')]));
  assert(document.querySelector('[data-action="compare-saved"]'));
  let calls=[];
  api=async path=>{calls.push(path);return {baselines:[
    {id:'self',run_id:'saved-run',name:'Own snapshot'},
    {id:'reference',run_id:'other-run',name:'Before <upgrade>',saved_at:'2026-10-15T09:00:00Z'}
  ]};};
  await openComparisonDialog();
  assert(dialog.open);
  assert.deepEqual(calls,['/api/baselines']);
  const options=dialog.querySelectorAll('option');
  assert.equal(options.length,1);assert.equal(options[0].getAttribute('value'),'reference');
  assert.match(dialog.textContent,/Before <upgrade>/);
  assert.equal(dialog.querySelector('upgrade'),null);
  dialog.close();
  api=async()=>({baselines:[{id:'self',run_id:'saved-run',name:'Own snapshot'}]});
  await openComparisonDialog();
  assert(dialog.querySelector('[type="submit"]').hasAttribute('disabled'));
  assert.match(dialog.textContent,/No other saved baselines/);
  dialog.close();
  let resolve;
  api=()=>new Promise(done=>{resolve=done;});
  const opening=openComparisonDialog();state.routeVersion++;
  resolve({baselines:[]});await opening;
  assert.equal(dialog.open,false);
})();
""", dom=True)


def test_saved_comparison_preserves_findings_and_grouped_comparison_results():
    run_render_checks(r"""
(async()=>{
  const dialog=document.createElement('dialog');dialog.setAttribute('id','comparison-dialog');document.body.insertBefore(dialog,null);
  globalThis.FormData=class{get(){return 'reference';}};
  const messages=[];toast=message=>messages.push(message);
  const failed=report([check('correctness','fail',{metrics:{correct:0,scored:1},evidence_ids:['bad']})],{
    overall:'fail',requests:[{id:'bad',target:'local',scenario:'question',status:'completed',valid:true,
      score:false,output:'Lyon',evaluation:{expected:'Paris',expected_provided:true}}]
  });
  detail(failed);
  const heading=()=>document.querySelector('.page-heading');
  assert.equal(heading().querySelector('.button.primary').dataset.action,'show-failing');
  const compare=heading().querySelector('[data-action="compare-saved"]');
  assert(compare);assert.equal(compare.classList.contains('primary'),false);
  const baseline={run_id:'reference-run',status:'inconclusive',acceptance:{baseline:'pass',current:'fail'},targets:[{target:'local',status:'pass',comparisons:[{
    check:'correctness',metric:'latency_ms',scenario:'question',baseline:100,current:100,
    baseline_samples:6,current_samples:6,status:'inconclusive',warnings:[{message:'Client scheduling delay affects attribution'}],reason:'Timing warning'
  }]}]};
  const calls=[];
  api=async(path,options)=>{
    calls.push([path,options?.method||'GET']);
    if(path==='/api/baselines')return {baselines:[{id:'reference',run_id:'reference-run',name:'Before upgrade'}]};
    if(path==='/api/runs/saved-run/compare'){
      assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{baseline_id:'reference'});return {};
    }
    assert.equal(path,'/api/runs/saved-run');
    return {id:'saved-run',state:'completed',baseline_id:'reference',report:{...failed,baseline}};
  };
  await listeners.click({target:compare,preventDefault(){}});
  assert(dialog.open);
  await compareSavedRun({preventDefault(){},target:dialog.querySelector('form')});
  assert.deepEqual(calls,[['/api/baselines','GET'],['/api/runs/saved-run/compare','POST'],['/api/runs/saved-run','GET']]);
  assert.equal(dialog.open,false);assert.equal(state.detail.baseline_id,'reference');
  assert.equal(heading().querySelector('.button.primary').dataset.action,'show-failing');
  assert.equal(heading().querySelector('[data-action="compare-saved"]').textContent,'Change baseline');
  const comparison=document.querySelector('#report-baseline-reference');
  assert(comparison.open);assert.equal(comparison.querySelectorAll('[data-comparison-group]').length,1);
  assert.match(comparison.textContent,/Before upgrade/);assert.match(comparison.textContent,/100 ms/);
  assert.match(comparison.textContent,/Baseline: Passed/);assert.match(comparison.textContent,/Current: Failed/);
  assert.match(comparison.textContent,/1 measurements · 1 with warnings/);
  assert.match(comparison.textContent,/Client scheduling delay affects attribution/);
  assert.equal(comparison.querySelectorAll('[data-metric]').length,1);
  assert.doesNotMatch(comparison.querySelector('[data-metric]').textContent,/Regression detected/);
  await listeners.click({target:heading().querySelector('.button.primary'),preventDefault(){}});
  const evidence=document.querySelector('.check-detail .evidence-table');
  assert(evidence);assert.match(evidence.textContent,/Paris/);assert.match(evidence.textContent,/Lyon/);
  assert.match(messages.at(-1),/No new requests were sent/);
})();
""", dom=True)

def test_saved_comparison_stays_pending_across_close_and_reopen():
    run_render_checks(r"""
(async()=>{
  const dialog=document.createElement('dialog');dialog.setAttribute('id','comparison-dialog');document.body.insertBefore(dialog,null);
  const messages=[];toast=message=>messages.push(message);
  globalThis.FormData=class{constructor(form){this.form=form;}get(){return this.form.baselineChoice;}};
  detail(report([check('serving','pass')]));
  const initial=structuredClone(state.detail),pending=[];
  let writes=0,persisted=initial;
  api=async(path,options)=>{
    if(path==='/api/baselines')return {baselines:[{id:'a',name:'Reference A'},{id:'b',name:'Reference B'}]};
    if(options?.method==='POST'){
      writes++;persisted={...initial,baseline_id:JSON.parse(options.body).baseline_id};return {};
    }
    const snapshot=structuredClone(persisted);
    return new Promise(resolve=>pending.push(()=>resolve(snapshot)));
  };
  await openComparisonDialog();
  const form=dialog.querySelector('form');form.baselineChoice='a';
  const first=compareSavedRun({preventDefault(){},target:form});
  await Promise.resolve();await Promise.resolve();
  assert.equal(pending.length,1);
  await listeners.click({target:form.querySelector('[data-action="close-dialog"]'),preventDefault(){}});
  assert.equal(dialog.open,false);
  await openComparisonDialog();
  assert.equal(dialog.open,false);
  assert.match(messages.at(-1),/still being saved/);
  form.baselineChoice='b';
  await compareSavedRun({preventDefault(){},target:form});
  assert.equal(writes,1);assert.equal(pending.length,1);
  pending[0]();await first;
  assert.equal(state.detail.baseline_id,'a');
  await openComparisonDialog();
  const next=dialog.querySelector('form');next.baselineChoice='b';
  const second=compareSavedRun({preventDefault(){},target:next});
  await Promise.resolve();await Promise.resolve();
  assert.equal(writes,2);pending[1]();await second;
  assert.equal(state.detail.baseline_id,'b');assert.equal(persisted.baseline_id,'b');
  assert.equal(dialog.open,false);
})();
""", dom=True)


@pytest.mark.parametrize("phase", ["post", "detail"])
@pytest.mark.parametrize("navigate", [False, True])
def test_saved_comparison_failure_releases_pending_and_respects_navigation(phase, navigate):
    run_render_checks(r"""
(async()=>{
  const phase=PHASE,navigate=NAVIGATE;
  const dialog=document.createElement('dialog');dialog.setAttribute('id','comparison-dialog');document.body.insertBefore(dialog,null);
  globalThis.FormData=class{get(){return 'reference';}};
  toast=()=>{};detail(report([check('serving','pass')]));
  const initial=structuredClone(state.detail);let reject;
  api=async(path,options)=>{
    if(path==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
    if(options?.method==='POST'&&phase==='detail')return {};
    return new Promise((_,fail)=>{reject=fail;});
  };
  await openComparisonDialog();
  const form=dialog.querySelector('form'),error=dialog.querySelector('#comparison-error');
  const saving=compareSavedRun({preventDefault(){},target:form});
  await Promise.resolve();await Promise.resolve();
  if(navigate){state.routeVersion++;state.detail={...initial,id:'another-run'};}
  reject(Error('Could not '+phase));await saving;
  assert.equal(error.hidden,navigate);
  assert.equal(state.detail.id,navigate?'another-run':'saved-run');
  assert.equal(form.querySelector('[type="submit"]').disabled,false);
  dialog.close();state.routeVersion++;state.detail=initial;
  api=async()=>({baselines:[{id:'reference',name:'Reference'}]});
  await openComparisonDialog();assert.equal(dialog.open,true);
})();
""".replace("PHASE", json.dumps(phase)).replace("NAVIGATE", json.dumps(navigate)), dom=True)


@pytest.mark.parametrize("phase", ["post", "detail"])
def test_saved_comparison_ignores_success_after_navigation(phase):
    run_render_checks(r"""
(async()=>{
  const phase=PHASE;
  const dialog=document.createElement('dialog');dialog.setAttribute('id','comparison-dialog');document.body.insertBefore(dialog,null);
  globalThis.FormData=class{get(){return 'reference';}};
  toast=()=>{};detail(report([check('serving','pass')]));
  const initial=structuredClone(state.detail);let resolve,detailReads=0;
  api=async(path,options)=>{
    if(path==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
    if(options?.method==='POST'&&phase==='detail')return {};
    if(!options)detailReads++;
    return new Promise(done=>{resolve=done;});
  };
  await openComparisonDialog();
  const saving=compareSavedRun({preventDefault(){},target:dialog.querySelector('form')});
  await Promise.resolve();await Promise.resolve();
  state.routeVersion++;state.detail={...initial,id:'another-run'};
  resolve({...initial,baseline_id:'reference'});await saving;
  assert.equal(state.detail.id,'another-run');
  assert.equal(detailReads,phase==='post'?0:1);
  dialog.close();state.routeVersion++;state.detail=initial;
  api=async()=>({baselines:[{id:'reference',name:'Reference'}]});
  await openComparisonDialog();assert.equal(dialog.open,true);
})();
""".replace("PHASE", json.dumps(phase)), dom=True)


def test_live_view_retains_stop_reasons_during_stopping_and_judging():
    run_render_checks(r"""
const config={targets:[{name:'local',model:'m'}],max_requests:50,concurrency:4,max_duration_seconds:120,request_timeout_seconds:30,max_output_tokens:128};
state.detail={id:'live',state:'stopping',config,progress:{event:'run_stopping',abort_reason:'Endpoint <local> unresponsive'},events:[]};
renderLive();let markup=nodes['#main'].innerHTML;
assert.match(markup,/class="notice warning live-stop-reason"/);assert.match(markup,/Endpoint &lt;local&gt; unresponsive/);
assert(markup.indexOf('Endpoint &lt;local&gt; unresponsive')<markup.indexOf('class="live-layout"'));
state.detail.state='running';state.detail.progress={event:'judging_progress',phase:'judging'};
state.detail.events=[{event:'run_stopping',target:'local',abort_reason:'A generic stop reason.'},{event:'judging_started'}];
renderLive();markup=nodes['#main'].innerHTML;assert.match(markup,/Evaluating answers/);assert.match(markup,/A generic stop reason/);
state.detail.events=[{event:'run_stopping',abort_reason:'Shared reason',target_abort_reasons:{alpha:'First target reason',beta:'Second target reason'}},{event:'judging_progress'}];
renderLive();markup=nodes['#main'].innerHTML;assert.match(markup,/alpha: First target reason/);assert.match(markup,/beta: Second target reason/);
state.detail.events=[];state.detail.progress={abort_reason:'Reason from latest progress',abort_reasons:[{target:'gamma',abort_reason:'Third target reason'}]};
renderLive();markup=nodes['#main'].innerHTML;assert.match(markup,/Reason from latest progress/);assert.match(markup,/gamma: Third target reason/);
state.detail.progress={event:'judging_progress'};renderLive();assert.match(nodes['#main'].innerHTML,/Third target reason/); // The 100-event history can rotate during evaluation.
state.detail.id='another-run';state.detail.progress={};renderLive();assert.doesNotMatch(nodes['#main'].innerHTML,/live-stop-reason/);
""")


def test_history_prioritizes_models_and_preserves_escaping_filtering_and_search():
    run_render_checks(r"""
state.bootstrap={checks:[{title:'Do not show full catalogue'}]};
renderRuns();
let html=nodes['#main'].innerHTML;
assert.equal((html.match(/class="button primary"/g)||[]).length,1);
assert.match(html,/<h1>Run history<\/h1>/);assert.match(html,/No runs yet/);
assert.match(html,/> New run<\/a>/);
assert.doesNotMatch(html,/Do not show full catalogue|run-filter|suite-list|first-run-steps/);
state.runs=[{id:'failed',state:'completed',overall:'fail',targets:[{name:'<target>',model:'<model>'}],
  started_at:'2026-10-15T10:00:00Z',check_counts:{fail:1,blocked:2,inconclusive:3},baseline_id:'base'},
  {id:'cancelled',state:'cancelled',overall:'fail',targets:[{name:'local',model:'second-model'}],check_counts:{}}];
renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/&lt;target&gt;/);assert.match(html,/&lt;model&gt;/);
assert.doesNotMatch(html,/<target>|<model>/);
assert.match(html,/class="history-identity"/);assert.match(html,/class="history-result"/);
assert.match(html,/class="history-mobile-date"/);assert.match(html,/class="history-date"/);
assert.match(html,/aria-label="Open run for &lt;model&gt; from/);
assert.match(html,/<table aria-label="Saved endpoint runs">/);
assert.match(html,/placeholder="Search runs"/);
assert.doesNotMatch(html,/Latest run|history-overview|What gets checked|suite-list|2 saved runs|table-note|run-id|Baseline compared|Duration|1 failed|2 blocked|3 inconclusive/);
state.filter='pass';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/No matching runs/);assert.match(html,/data-action="clear-filter"/);
state.filter='stopped';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/#runs\/cancelled/);assert.match(html,/Stopped/);
assert.doesNotMatch(html.split('class="history-table"')[1],/#runs\/failed/);
state.filter='all';state.historySearch='<target>';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/#runs\/failed/);assert.doesNotMatch(html.split('class="history-table"')[1],/#runs\/cancelled/);
state.historySearch='second-model';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/#runs\/cancelled/);assert.doesNotMatch(html.split('class="history-table"')[1],/#runs\/failed/);
state.runs.push({id:'live',state:'running',overall:'fail',targets:[{model:'active-model'}]});
state.filter='active';state.historySearch='';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/#runs\/live/);assert.match(html,/Running/);assert.doesNotMatch(html.split('class="history-layout')[1],/#runs\/failed|#runs\/cancelled/);
""")


def test_history_refresh_preserves_search_focus_and_caret():
    run_render_checks(r"""
state.runs=[{id:'saved',state:'completed',overall:'pass',targets:[{model:'test'}]}];
state.historySearch='test';
const search={id:'run-search',selectionStart:1,selectionEnd:3,focus(options){assert(options.preventScroll);this.focused=true;},setSelectionRange(start,end){this.start=start;this.end=end;}};
nodes['#run-search']=search;document.activeElement=search;
renderRuns();assert(search.focused);assert.equal(search.start,1);assert.equal(search.end,3);
""")


def test_failures_and_partial_results_do_not_promote_saving_a_baseline():
    run_render_checks(r"""
const failed=report([check('correctness','fail',{
  summary:'Expected <Paris>; inspect the returned answer.',metrics:{correct:1,scored:3},evidence_ids:['bad']})],{overall:'fail',requests:[{id:'bad',target:'local',status:'completed',score:false,output:'Lyon',evaluation:{expected:'Paris',expected_provided:true}}]});
let html=detail(failed);
assert.match(html,/<h1>Answer correctness: 1 of 3 correct<\/h1>/);
assert.match(html,/Expected &lt;Paris&gt;/);
assert.match(html,/class="button primary" data-action="show-failing">Show failing requests/);
assert.doesNotMatch(html,/inspect-finding|Inspect failed answer|Inspect evidence|Inspect check/);
assert.doesNotMatch(html,/class="button primary" data-action="baseline-dialog"/);
assert.match(html,/Save as reference baseline/);
assert.match(html,/report\.html" download/);
assert.match(html,/report\.json" download/);
assert.match(html,/http:\/\/localhost:8000\/v1/);
const passed=report([check('access','pass')]);
html=detail(passed);
assert.match(html,/class="button primary" data-action="baseline-dialog"/);
assert.match(html,/applies to the endpoint and traffic tested here/);
html=detail({...passed,abort_reason:'maximum request budget reached',overall:'inconclusive'});
assert.match(html,/Run stopped with partial results/);
assert.doesNotMatch(html,/class="button primary" data-action="baseline-dialog"/);
html=detail(passed,{state:'error',error:'Could not save all artifacts'});
assert.doesNotMatch(html,/class="button primary" data-action="baseline-dialog"/);
html=detail({...passed,checks:[...passed.checks,check('gpu','inconclusive',{required:false})]});
assert.match(html,/<h1>Selected acceptance checks passed<\/h1>/);
assert.match(html,/class="button primary" data-action="baseline-dialog"/);
html=detail({...passed,overall:'fail',baseline:{status:'fail'}});
assert.match(html,/A material regression was detected/);
assert.match(html,/class="button primary" data-action="show-comparison">Show comparison/);
assert.doesNotMatch(html,/data-action="show-failing"/);
html=detail(report([]));
assert.match(html,/<h1>No checks ran<\/h1>/);
assert.doesNotMatch(html,/Selected acceptance checks passed|checks met their configured limits/);
assert.doesNotMatch(html,/class="button primary" data-action="baseline-dialog"/);
""")


def test_skipped_checks_are_disclosed_without_misreporting_selection():
    run_render_checks(r"""
const value=report([
  check('json','skipped',{summary:'Not selected for this run.'}),
  check('access','pass'),check('generation','inconclusive'),check('correctness','fail'),
  check('tools','skipped',{summary:'Server did not expose <tools>.'})
],{overall:'fail'});
value.manifest.config.checks.push('tools');
let html=detail(value);
assert(html.indexOf('data-check="local:correctness"')<html.indexOf('data-check="local:access"'));
assert.match(html,/<details class="checks-not-selected">/);
const disclosure=html.match(/<details class="checks-not-selected"[^>]*>([\s\S]*?)<\/details>/)[1];
assert.match(disclosure,/Not selected: JSON correctness/);
assert.doesNotMatch(html,/data-check="local:json"/);
assert.match(html,/Server did not expose &lt;tools&gt;/);
assert.doesNotMatch(disclosure,/data-check="local:access"|data-check="local:correctness"/);
state.selectedCheck='local:tools';html=renderChecks(value);
assert.match(html,/This check was skipped/);
state.selectedCheck='local:json';html=renderChecks(value);
assert.doesNotMatch(html,/data-check="local:json"/);
const allSkipped=report([check('json','skipped',{summary:'Not selected for this run.'})],{overall:'skipped'});
html=detail(allSkipped);
assert.match(html,/<h1>No checks ran<\/h1>/);
assert.match(html,/<details class="checks-not-selected">/);
assert.equal(state.selectedCheck,null);
const selectedSkipped=report([check('access','pass'),check('tools','skipped',{
  summary:'The server did not expose <tools>.'})]);
selectedSkipped.manifest.config.checks.push('tools');
html=detail(selectedSkipped);
assert.match(html,/<h1>Some selected checks did not run<\/h1>/);
assert.match(html,/result-heading inconclusive/);
assert.doesNotMatch(html,/Acceptance passed|Selected acceptance checks passed/);
assert.doesNotMatch(html,/class="button primary" data-action="baseline-dialog"/);
""")


def test_live_counters_distinguish_started_requests_from_budget_and_completion():
    run_render_checks(r"""
state.detail={id:'live',state:'running',config:{targets:[{name:'local',model:'test'}],
  max_requests:100,concurrency:4,max_duration_seconds:60,request_timeout_seconds:10,max_output_tokens:32},
  progress:{attempted:12,completed:9,elapsed_seconds:5},events:[]};
renderLive();let html=nodes['#main'].innerHTML;
assert.match(html,/<h1>Running<\/h1>/);
assert.match(html,/9 requests finished · 5s · limit 100/);
assert.doesNotMatch(html,/Requests started ·|Active requests<\/dt>/);
assert.match(html,/Scheduled requests can also use budget without reaching the server/);
assert.doesNotMatch(html,/Request budget used/);
state.detail.state='stopping';renderLive();html=nodes['#main'].innerHTML;
assert.match(html,/data-action="stop"[^>]* disabled/);
assert.match(html,/saving the evidence collected so far/);
state.detail.state='running';state.detail.progress={phase:'judging',attempted:12,completed:12,
  judge_calls_completed:3,judge_calls_total:8};
renderLive();html=nodes['#main'].innerHTML;
assert.match(html,/Target traffic finished/);
assert.match(html,/Evaluation calls · 3 of 8/);
assert.doesNotMatch(html,/Requests started ·/);
""")


def test_grouped_comparison_preserves_measurements_filters_and_uncertainty():
    run_render_checks(r"""
const row=(check,metric,status,reason)=>({check,metric,status,reason,scenario:'question <one>',
  baseline:100,current:125,baseline_samples:6,current_samples:6,worsening:25,threshold:20});
const baseline={run_id:'reference-id',status:'inconclusive',targets:[{target:'local <gpu>',status:'inconclusive',
  comparisons:[row('context','latency_ms','fail','Threshold exceeded'),
    row('context','first_output_ms','inconclusive','Sample variation'),
    row('correctness','correctness_rate','pass','No material regression')]}]};
const value=report([check('context','fail')],{baseline});state.detail={report:value,baseline_id:'chosen'};
state.baselines=[{id:'chosen',name:'Before <upgrade>'}];const original=JSON.stringify(value);
let html=renderComparison(value);
assert.match(html,/measurements/);assert.match(html,/Pass \/ fail/);assert.match(html,/Why this warning/);
assert.match(html,/Before &lt;upgrade&gt;/);assert.match(html,/local &lt;gpu&gt;/);
assert.equal((html.match(/data-comparison-group=/g)||[]).length,2);
assert.match(html,/data-comparison-group="context"/);assert.match(html,/Context recall/);
assert.match(html,/Regression detected/);assert.match(html,/Change/);assert.match(html,/125 ms/);assert.match(html,/n=6/);
assert.match(html,/Question &lt;one&gt;/);assert.doesNotMatch(html,/<one>|<upgrade>|<gpu>/);
assert.doesNotMatch(html,/<details class="comparison-check-group"[^>]* open/);
state.comparisonFilter='all';html=renderComparison(value);
assert.equal((html.match(/data-comparison-group=/g)||[]).length,2);
assert.equal((html.match(/data-metric=/g)||[]).length,3); // All measured differences are visible by default.
state.comparisonFilter='with_warnings';state.comparisonCheck='correctness';html=renderComparison(value);
assert.match(html,/No measurements match/);
assert.equal(JSON.stringify(value),original);
""")


def test_load_comparison_shows_acceptance_changes_and_groups_measurements_by_tested_load():
    run_render_checks(r"""
const point={source:'primary',type:'combinations',workload:'short_short',shape:'short_short',pattern:'concurrent',level:4,rate:null,bursts:false};
const previous={...point,status:'inconclusive',reason_code:'load_not_delivered',valid:8,dispatched:10,required:5,server_evidenced_concurrency:1,client_mean_inflight:3.9},current={...point,status:'pass',valid:12,dispatched:12,required:5};
const rows=[['latency_ms','raw-one'],['first_output_ms','raw-two'],['valid_completion_rate','raw-three']].map(([metric,scenario])=>({check:'load',metric,scenario,load_point:point,baseline:100,current:80,baseline_samples:6,current_samples:8,status:'pass',warnings:[{code:'load_not_delivered',message:'Baseline delivered concurrency 1 < 4.'}]}));
const changes=['gained','lost','unchanged_accepted','unchanged_not_accepted','not_established_in_baseline','accepted_baseline_not_established','not_established_in_current','not_established','not_tested_in_baseline','not_tested_in_current'];
const load_test={status:'pass',reason:'One newly accepted load.',baseline_highest_accepted:[{...point,value:1}],current_highest_accepted:[{...point,value:4}],points:changes.map(acceptance_change=>({baseline:previous,current,status:['gained','unchanged_accepted'].includes(acceptance_change)?'pass':acceptance_change==='lost'?'fail':'inconclusive',acceptance_change,reason:'Requested concurrency <4> was not delivered.'}))};
const value=report([],{baseline:{targets:[{target:'local <gpu>',comparisons:rows,observed_outputs:[{...rows[0],scenario:'raw-output',metric:'output_tokens',descriptive:true}],load_test}]}});
state.detail={report:value};const original=JSON.stringify(value);
let html=renderComparison(value);
assert.match(html,/id="results-load-acceptance"/);assert.match(html,/Tested load acceptance/);
for(const text of ['Newly accepted','Lost','Still accepted','Still not accepted','Not established in baseline','Accepted now; not established in baseline','Not established in current','Not established','Not tested in baseline','Not tested in current'])assert(html.includes(text),text);
assert.match(html,/Highest accepted concurrency \(tested\) · Baseline: <strong>1<\/strong> · Current: <strong>4<\/strong>/);
assert.match(html,/Short input → Short output · maintained concurrency 4/);
assert.match(html,/Baseline delivered concurrency 1 &lt; 4/);
assert.match(html,/Inconclusive · 8 valid/);assert.match(html,/Passed · 12 valid/);
assert.match(html,/10 dispatched · 5 required/);assert.match(html,/Server concurrency 1 · Client inflight 3.9/);
assert.doesNotMatch(html.match(/<tbody>[\s\S]*?<\/tbody>/)[0],/class="status pass"/);
assert.match(html,/<strong>Accepted now; not established in baseline<\/strong>/);
assert.match(html,/Requested concurrency &lt;4&gt; was not delivered/);
assert.doesNotMatch(html,/raw-one|raw-two|raw-three|raw-output|<gpu>|<4>/);
assert.equal((html.match(/data-comparison-group="load"/g)||[]).length,3,'same tested load shares one group within each measurement section');
assert(resultsSections().some(([id,label,parent])=>id==='load-acceptance'&&label==='Tested load acceptance'&&parent==='comparison'));
assert.match(renderResultsJump(),/value="load-acceptance"/);
assert.match(html,/class="metric-table load-acceptance-table" tabindex="0" role="region"/);
state.comparisonFilter='without_warnings';html=renderComparison(value);
assert.match(html,/Newly accepted/);assert.doesNotMatch(html,/data-metric="latency_ms"/);
assert.equal(JSON.stringify(value),original);
const fallback=comparisonOutputs({comparisons:[{...rows[0],output_work:{baseline:{reported_output_tokens_p50:10,reported_token_samples:6},current:{reported_output_tokens_p50:12,reported_token_samples:8}}}]});
assert.equal(fallback[0].load_point,point);
""")


def test_load_comparison_exclusions_render_without_acceptance_points():
    run_render_checks(r"""
const point={source:'primary',type:'combinations',workload:'short_short',shape:'short_short',pattern:'concurrent',level:4};
const value=report([],{baseline:{targets:[{target:'local <gpu>',comparisons:[],load_exclusions:[{scenario:'raw-scenario',load_point:point,reason:'Input target changed <128>.'}]}]}});
state.detail={report:value};const html=renderComparison(value);
assert.match(html,/Tested load acceptance/);assert.match(html,/Load measurements not compared/);
assert.match(html,/Short input → Short output · maintained concurrency 4/);
assert.match(html,/Input target changed &lt;128&gt;/);assert.doesNotMatch(html,/raw-scenario|<gpu>|<128>/);
assert(resultsSections().some(([id])=>id==='load-acceptance'));
assert.match(renderResultsJump(),/value="load-acceptance"/);
""")


def test_interference_comparison_keeps_control_and_probe_measurements_separate():
    run_render_checks(r"""
const point={source:'primary',type:'interference',workload:'interference',shape:'short_short',pattern:'interference',level:4,rate:null,bursts:false,role:'short_stream'};
const work={baseline:{reported_output_tokens_p50:10,reported_token_samples:6},current:{reported_output_tokens_p50:12,reported_token_samples:8}};
const rows=['baseline','probe'].map((phase,i)=>({check:'load',metric:'latency_ms',scenario:'load_interference.'+phase+'.short_short.short_stream',load_point:{...point,phase},baseline:100+i*100,current:80+i*100,baseline_samples:6,current_samples:8,status:'pass',warnings:[],output_work:work}));
const html=renderComparison(report([],{baseline:{targets:[{target:'local',comparisons:rows,observed_outputs:[]}]}}));
assert.equal((html.match(/data-comparison-group="load"/g)||[]).length,2);
assert.match(html,/Short-only control/);assert.match(html,/During long-input probe/);
assert.equal(comparisonOutputs({comparisons:rows}).length,4);
""")


def test_checks_expose_all_requests_with_incorrect_and_failed_first():
    run_render_checks(r"""
const request=(id,score,status='completed')=>({id,score,status,target:'local',scenario:id,elapsed_ms:12,output:'Answer '+id,output_chars:8,valid:true});
const value=report([
  check('correctness','fail',{evidence_ids:['correct-good','correct-bad','correct-timeout']}),
  check('json','fail',{evidence_ids:['json-good','json-failed','json-bad']})
],{overall:'fail',requests:[request('correct-good',true),request('correct-bad',false),request('correct-timeout',null,'timed_out'),request('json-good',true),request('json-failed',null,'timeout'),request('json-bad',false)]});
let html=detail(value);
assert.match(html,/data-check="local:correctness"/);assert.match(html,/data-check="local:json"/);
assert.doesNotMatch(html,/inspect-finding|Inspect failed answer|Inspect evidence|Inspect check/);
nodes['#result-content']={innerHTML:''};
nodes['#request-dialog']={innerHTML:'',showModal(){this.open=true;}};
for(const [id,bad,failed,good] of [['correctness','correct-bad','correct-timeout','correct-good'],['json','json-bad','json-failed','json-good']]){
  const checkNode={dataset:{check:'local:'+id}};
  listeners.click({target:{closest:q=>q==='[data-check]'?checkNode:null}});
  html=nodes['#result-content'].innerHTML;
  assert.match(html,new RegExp('data-check="local:'+id+'" aria-pressed="true"'));
  const list=html.slice(html.indexOf('class="evidence-table"'));
  for(const requestId of [bad,failed,good])assert.match(list,new RegExp('data-request="'+requestId+'"'));
  assert(list.indexOf('data-request="'+bad+'"')<list.indexOf('data-request="'+failed+'"'));
  assert(list.indexOf('data-request="'+failed+'"')<list.indexOf('data-request="'+good+'"'));
  for(const requestId of [bad,failed,good]){
    listeners.click({target:{closest:q=>q==='[data-request]'?{dataset:{request:requestId}}:null}});
    assert(nodes['#request-dialog'].open);assert.match(nodes['#request-dialog'].innerHTML,new RegExp('Answer '+requestId));
  }
}
""")


def test_checks_without_request_evidence_show_explanation_and_measurements():
    run_render_checks(r"""
const value=report([check('first_output','blocked',{summary:'Endpoint &lt;local&gt; did not become ready.',metrics:{attempted:3,completed:0,failed:2,timed_out:1,first_output_p95_ms:321}}),check('generation','inconclusive',{summary:'No measurable generation samples.',metrics:{attempted:2,completed:0,generation_rate_samples:0}})],{overall:'inconclusive'});
state.selectedCheck='local:first_output';state.detail={report:value};
let html=renderChecks(value);
assert.match(html,/Endpoint &amp;lt;local&amp;gt; did not become ready/);
assert.match(html,/321 ms/);assert.match(html,/2 failed · 1 timed out/);
assert.match(html,/No request evidence was recorded for this check/);
state.selectedCheck='local:generation';html=renderChecks(value);
assert.match(html,/No measurable generation samples/);assert.match(html,/0 measured samples/);
assert.match(html,/No request evidence was recorded for this check/);
""")


def test_report_tabs_keep_one_tab_stop_and_support_keyboard_navigation():
    run_render_checks(r"""
const value=report([check('access','pass')]);detail(value);
assert.equal($$('[role="tab"]').length,2);
assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
assert.equal($('#tab-performance').getAttribute('tabindex'),'-1');
let prevented=0;
const key=key=>listeners.keydown({target:document.activeElement,key,preventDefault(){prevented++;}});
$('#tab-performance').focus({preventScroll:true});key('ArrowRight');
assert.equal(reportView.tab,'results');assert.equal(document.activeElement,$('#tab-results'));
assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
assert.equal($('#tab-performance').getAttribute('tabindex'),'-1');
key('Home');assert.equal(reportView.tab,'results');
key('ArrowLeft');assert.equal(reportView.tab,'performance');
assert.equal(prevented,3);assert.equal($$('[role="tab"][tabindex="0"]').length,1);
""", dom=True)


def test_tool_request_evidence_separates_outcomes_and_escapes_bodies():
    run_render_checks(r"""
const r={id:'tool',fixture_id:'mixed',target:'local',scenario:'tool_auto_complete',status:'ok',score:false,output_chars:9,output:'<actual>',valid:true,
  evaluation:{messages:[{role:'user',content:'<prompt>'}],tool_expectation:{tools:[{description:'<definition>'}],expected_calls:[{name:'weather',arguments:{city:'<Delhi>'}}],expected_content:{mode:'exact',value:'<expected>',normalization:'none'}}},
  tool_evaluation:{protocol:{status:'pass',message:'Response completed'},content:{status:'fail',message:'Missing <sentence>'},calls:{status:'pass',message:'Calls match'}},
  tool_calls:[{id:'call',function:{name:'weather',arguments:'{"city":"<Delhi>"}'}}],response_events:[{kind:'<content>',sequence:1}],response_events_truncated:true};
state.detail={report:{manifest:{config:{retention:'all'}},requests:[r]}};
nodes['#request-dialog']={innerHTML:'',showModal(){this.shown=true;}};
openRequest('tool');let html=nodes['#request-dialog'].innerHTML;
assert(nodes['#request-dialog'].shown);assert.match(html,/Response protocol/);assert.match(html,/Assistant text/);assert.match(html,/Tool calls/);
assert.match(html,/Missing &lt;sentence&gt;/);assert.match(html,/Expected assistant text/);assert.match(html,/&lt;expected&gt;/);
assert.match(html,/Actual tool calls/);assert.match(html,/&lt;Delhi&gt;/);assert.match(html,/event list was truncated/);
assert.match(html,/&lt;content&gt;/);assert.doesNotMatch(html,/<actual>|<expected>|<Delhi>|<prompt>|<content>|<definition>/);
r.tool_calls=[];r.evaluation.tool_expectation.expected_content=null;r.tool_evaluation.content={status:'not_checked',message:'No requirement'};
openRequest('tool');html=nodes['#request-dialog'].innerHTML;assert.match(html,/Not checked/);assert.match(html,/Tool-only responses are allowed/);assert.match(html,/No tool-call body retained or recorded/);
assert.equal(renderToolEvaluation({}), '');assert.equal(renderActualToolCalls({}), '');assert.equal(renderResponseEvents({}), '');
assert.match(renderEvaluationEvidence({evaluation:{messages:[],scorer:'exact',expected:'legacy',expected_provided:true}},{}),/legacy/);
""")


def test_terminal_run_clears_stale_request_and_hidden_active_indicators():
    run_render_checks(r"""
nodes['#active-run-link']={innerHTML:''};
state.detail={id:'ended',state:'cancelled'};
setActive('ended');setActive(null);
assert.equal(nodes['#active-run-link'].innerHTML,'','hidden active link must not retain a running badge');
const html=renderMosaic([{id:'stale',slot:0,status:'running'}]);
assert.doesNotMatch(html,/request-spot running/,'saved terminal run cannot pulse stale unfinished evidence');
assert.match(html,/Cancelled/);
""")


def test_request_terminal_display_states_have_distinct_labels_and_glyphs():
    run_render_checks(r"""
const statuses=['pass','fail','timeout','cancelled','skipped','inconclusive'];
assert.equal(new Set(statuses.map(status=>statusLabels[status])).size,statuses.length);
assert.equal(new Set(statuses.map(status=>statusIcons[status])).size,statuses.length);
for(const status of statuses){assert(statusLabels[status]);assert.match(badge(status),new RegExp(statusLabels[status]));}
const records=['completed','failed','timeout','cancelled','skipped','completed'].map((status,i)=>({id:'r'+i,status,valid:true,score:i===0?true:null}));
const html=renderMosaic(records);
for(const status of statuses){assert(html.includes('request-spot '+status));assert(html.includes(statusLabels[status]));}
""")


def stylesheet_rules(css, parents=()):
    """Walk nested style rules, retaining media conditions for cascade assertions."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    cursor = 0
    while (opening := css.find("{", cursor)) >= 0:
        header = css[cursor:opening].strip()
        depth, end = 1, opening + 1
        while depth and end < len(css):
            depth += (css[end] == "{") - (css[end] == "}")
            end += 1
        assert depth == 0, "Unbalanced stylesheet block"
        body = css[opening + 1:end - 1]
        if header.startswith("@"):
            if "keyframes" not in header:
                yield from stylesheet_rules(body, parents + (header,))
        else:
            declarations = dict(item.strip().split(":", 1) for item in body.split(";")
                                if ":" in item)
            yield header, {key.strip(): value.strip() for key, value in declarations.items()}, parents
        cursor = end


def test_reduced_motion_disables_animations_instead_of_shortening_them():
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    rules = list(stylesheet_rules(css))
    reduced = [declarations for _, declarations, parents in rules
               if '@media (prefers-reduced-motion: reduce)' in parents]
    assert any(row.get('animation') == 'none !important' for row in reduced)
    assert any(row.get('transition') == 'none !important' for row in reduced)
    animated = []
    for selector, declarations, parents in rules:
        animation = declarations.get('animation', declarations.get('animation-name', 'none'))
        if animation.startswith('none'):
            continue
        animated.append(selector)
        assert '@media (prefers-reduced-motion: no-preference)' in parents, selector
        if 'infinite' in animation:
            assert '.queued' not in selector and '.reserved' not in selector
    assert animated, 'The parser must inspect real animation rules'
    assert '.is-landing' not in css


def test_load_acceptance_table_contains_overflow_on_narrow_screens():
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    rules = list(stylesheet_rules(css))
    for selector in ('.load-acceptance', '.load-acceptance-target', '.load-acceptance-table'):
        assert any(selector in group.split(', ') and declarations.get('min-width') == '0'
                   for group, declarations, _ in rules)
    assert any('.metric-table' in group.split(', ') and declarations.get('overflow-x') == 'auto'
               and declarations.get('max-width') == '100%' for group, declarations, _ in rules)
    assert any(group == '.load-acceptance-table table' and declarations.get('min-width') == '680px'
               for group, declarations, _ in rules)
    for selector in ('.load-acceptance-table th:nth-child(2)', '.load-acceptance-table th:nth-child(3)',
                     '.load-acceptance-table td:nth-child(2)', '.load-acceptance-table td:nth-child(3)'):
        assert any(selector in group.split(', ') and declarations.get('white-space') == 'nowrap'
                   and declarations.get('min-width') == '150px' for group, declarations, _ in rules)


def test_comparison_changes_are_directional_and_multiple_warnings_are_visible():
    run_render_checks(r"""
assert.equal(comparisonChange({metric:'correctness_rate',baseline:.4,current:.5}),'+10 percentage points');
assert.equal(comparisonChange({metric:'generation_tokens_per_second',baseline:20,current:10}),'-50%');
assert.equal(comparisonChange({metric:'latency_ms',baseline:0,current:10}),'+10 ms · from zero');
assert.equal(comparisonChange({metric:'latency_ms',baseline:null,current:10}),'Not measured');
const rows=[{check:'load',scenario:'same',metric:'latency_ms',baseline:10,current:20,baseline_samples:5,current_samples:5,status:'inconclusive',warnings:[{message:'Answers are longer'},{message:'Client delayed <8/10>'}]}];
const html=comparisonTable(rows);
assert.match(html,/Answers are longer/);assert.match(html,/Client delayed &lt;8\/10&gt;/);
assert.match(html,/Baseline/);assert.match(html,/Current/);assert.match(html,/Change/);
assert.doesNotMatch(html,/Regression detected/);
""")


def test_comparison_warning_filter_is_independent_of_regression_status():
    run_render_checks(r"""
const row=(metric,status,warnings=[])=>({check:'load',scenario:'same',metric,status,warnings,baseline:10,current:20});
const value=report([],{baseline:{targets:[{target:'local',comparisons:[
  row('latency_ms','fail'),row('first_output_ms','pass',[{message:'Partial timing evidence'}]),
  row('correctness_rate','inconclusive')],observed_outputs:[{...row('output_tokens',null),descriptive:true}]}]}});
state.detail={report:value};state.comparisonFilter='with_warnings';
let html=renderComparison(value);
assert.match(html,/data-metric="first_output_ms"/);assert.match(html,/data-metric="correctness_rate"/);
assert.doesNotMatch(html,/data-metric="latency_ms"|data-metric="output_tokens"/);
state.comparisonFilter='without_warnings';html=renderComparison(value);
assert.match(html,/data-metric="latency_ms"/);assert.match(html,/Regression detected/);
assert.match(html,/data-metric="output_tokens"/);
assert.doesNotMatch(html,/data-metric="first_output_ms"|data-metric="correctness_rate"/);
state.comparisonFilter='all';html=renderComparison(value);
assert.equal((html.match(/data-metric=/g)||[]).length,4);
""")


def test_comparison_timing_summary_distinguishes_legacy_and_observed_overlap():
    run_render_checks(r"""
const row={metric:'latency_ms',status:'inconclusive',baseline:100,current:100,
  warnings:[{code:'client_delayed',message:'Timing attribution is uncertain.'}],
  timing_quality:{Current:{status:'client_delayed',scope:'legacy_run_maximum',affected_requests:8,total_requests:8}}};
let html=comparisonTable([row]);
assert.match(html,/run-wide Giraffe delay; request-level overlap was not recorded/);
assert.doesNotMatch(html,/affected 8\/8/);
row.timing_quality.Current={status:'client_delayed',scope:'request_intervals',affected_requests:1,total_requests:8};
html=comparisonTable([row]);
assert.match(html,/Giraffe delays affected 1\/8 current requests/);
assert.doesNotMatch(html,/run-wide/);
""")


def test_comparison_preserves_deployment_and_confounding_disclosure_under_filters():
    run_render_checks(r"""
const target={target:'GPU <blue>',comparisons:[{check:'serving',scenario:'warm',metric:'latency_ms',status:'pass',baseline:10,current:10}],
  configuration_diff:{confounded:true,intended_change:'runtime.version',fields:[{field:'runtime.version',baseline:{value:'v1'},current:{value:'v2'}},{field:'hardware.gpu_count',baseline:{value:1},current:{value:2}}],unknown_fields:['serving.scheduler']},
  identity_changes:{image:{baseline:'image-before',current:'image-after'}},
  consistency_changes:[{scenario:'same-prompt',baseline_agreement:.8,current_agreement:.9}],
  telemetry:{baseline:{kind:'before-telemetry'},current:{kind:'after-telemetry'}}};
const value=report([],{baseline:{targets:[target]}});
for(const filter of ['all','with_warnings','without_warnings']){
  state.comparisonFilter=filter;state.comparisonCheck='correctness';
  const html=renderComparison(value);
  assert.match(html,/Deployment and comparison details/);
  assert.match(html,/GPU &lt;blue&gt;/);assert.doesNotMatch(html,/<blue>/);
  assert.match(html,/Confounded experiment/);assert.match(html,/not proof of causality/);
  for(const text of ['runtime.version','hardware.gpu_count','serving.scheduler','image-before','image-after','baseline_agreement','current_agreement','before-telemetry','after-telemetry'])assert(html.includes(text),text);
}
target.configuration_diff={unknown_baseline:true,unknown_fields:['runtime.version']};
assert.match(renderComparison(value),/Causality unverified/);
""")


def test_traffic_only_comparison_preserves_accepted_rate_goodput_and_stage_loss():
    run_render_checks(r"""
const value=report([],{baseline:{targets:[{target:'arrival <gpu>',comparisons:[],traffic_capacity:{
  status:'fail',reason:'Previously accepted arrival rate was lost.',
  baseline_highest_acceptable_rate_rps:50,current_highest_acceptable_rate_rps:10,
  delta_rps:-40,change_percent:-80,stages:[{rate_rps:50,status:'fail',
    baseline:{goodput_rps:49},current:{goodput_rps:8},
    reason:'This previously acceptable arrival stage now fails its configured criteria.'}]
}}]}});
state.detail={report:value};
for(const filter of ['all','with_warnings','without_warnings']){
  state.comparisonFilter=filter;
  const html=renderComparison(value);
  assert.match(html,/arrival &lt;gpu&gt;/);
  assert.match(html,/Tested arrival capacity/);
  assert.match(html,/Baseline accepted rate<\/dt><dd>50 req\/s/);
  assert.match(html,/Current accepted rate<\/dt><dd>10 req\/s/);
  assert.match(html,/-40 req\/s/);assert.match(html,/-80%/);
  assert.match(html,/Baseline goodput/);assert.match(html,/49 req\/s/);
  assert.match(html,/Current goodput/);assert.match(html,/8 req\/s/);
  assert.match(html,/Previously accepted arrival rate was lost\./);
  assert.match(html,/This previously acceptable arrival stage now fails its configured criteria\./);
}
""")


def test_check_details_show_recorded_effective_limits_and_default_labels():
    run_render_checks(r"""
const first=check('first_output','pass'),generation=check('generation','pass');
const value=report([first,generation]);
value.manifest.effective_checks={first_output:{limits:{first_output_ms:10000}},generation:{limits:{stream_gap_ms:5000}}};
value.manifest.default_check_limits={first_output:['first_output_ms'],generation:['stream_gap_ms']};
const original=JSON.stringify(value);
let markup=renderCheck(first,value);
assert.match(markup,/Limit: ≤ 10 s \(default\)/);
assert.doesNotMatch(markup,/No acceptance limit set/);
markup=renderCheck(generation,value);
assert.match(markup,/Limit: ≤ 5 s \(default\)/);
assert.doesNotMatch(markup,/No stream-pause limit set/);
assert.equal(JSON.stringify(value),original);
value.manifest.config.limits.first_output_ms=123;
value.manifest.effective_checks.first_output.limits.first_output_ms=800;
value.manifest.default_check_limits.first_output=[];
markup=renderCheck(first,value);
assert.match(markup,/Limit: ≤ 800 ms/);assert.doesNotMatch(markup,/\(default\)|123 ms/);
value.manifest.effective_checks.first_output.limits.first_output_ms=null;
markup=renderCheck(first,value);assert.match(markup,/No acceptance limit set/);
delete value.manifest.effective_checks;delete value.manifest.default_check_limits;
markup=renderCheck(first,value);assert.match(markup,/Limit: 123 ms/);assert.doesNotMatch(markup,/\(default\)/);
""")


@pytest.mark.parametrize("check_id", ["first_output", "generation"])
def test_failed_checks_keep_applied_default_limits_and_qualified_evidence(check_id):
    run_render_checks(r"""
(async()=>{
 const id=CHECK_ID,first=id==='first_output';
 const limitField=first?'first_output_ms':'stream_gap_ms',observedField=first?'first_output_ms':'max_stream_gap_ms';
 const limit=first?10000:5000,slow=first?12300:6000;
 const records=[['slow',slow],['good',200],['client',20000]].map(([id,time])=>({id,target:'local',status:'completed',valid:true,score:true,output:'Correct',[observedField]:time}));
 const failed=check(id,'fail',{evidence_ids:['slow','good','client'],metrics:{violations:['slow'],unattributed_timing_violations:['client']}});
 const value=report([failed],{overall:'fail',requests:records});
 value.manifest.config.limits={[limitField]:60000};
 value.manifest.effective_checks={[id]:{limits:{[limitField]:limit}}};
 value.manifest.default_check_limits={[id]:[limitField]};
 const original=JSON.stringify(value);
 detail(value);const action=$('[data-action="show-failing"]');assert(action);await listeners.click({target:action});
 const panel=$('.check-detail'),table=panel.querySelector('.evidence-table');
 assert(panel.textContent.includes(`Limit: ≤ ${limit/1000} s (default)`));
 assert(!panel.textContent.includes('60 s'));
 assert(table.querySelector('[data-request="slow"]'));
 assert.equal(table.querySelector('[data-request="good"]'),null);
 assert.equal(table.querySelector('[data-request="client"]'),null);
 assert(table.textContent.includes(first?'Too slow: first output 12.3 s > 10 s':'Too slow: stream pause 6 s > 5 s'));
 assert.equal(JSON.stringify(value),original);
})()
""".replace('CHECK_ID', json.dumps(check_id)), dom=True)


def test_live_timeout_warning_notice_and_event_updates():
    run_render_checks(r"""
const warning={key:'local|30|run',target:'local <server>',deadline_seconds:30,deadline_source:'run',setting:'request_timeout_seconds',finished:10,timed_out:3,answer_started:1,reasoning_only:1,no_output:1,checks:['serving']};
state.detail={id:'live',state:'running',config:{targets:[{name:'local',model:'m'}],request_timeout_seconds:30,max_output_tokens:32},progress:{},live_requests:[{status:'timeout'}],events:[{event:'timeout_warning'}],timeout_warnings:[warning]};
renderLive();let html=nodes['#main'].innerHTML;
assert.match(html,/notice warning live-timeout-warning/);assert.match(html,/<strong>Requests are timing out<\/strong>/);assert.match(html,/role="status" aria-live="polite"/);
assert.match(html,/local &lt;server&gt; · 3 of 10 requests hit the 30s request timeout/);
assert.match(html,/1 had started answering · 1 produced reasoning only · 1 received no output/);
assert.match(html,/data-action="show-timeouts"/);assert.match(html,/data-action="timeout-setting"/);
assert.match(html,/Each request may take up to 30s\. Giraffe will not change this during the run\./);
assert.match(html,/Intentional timeout tests are not counted here\./);assert.match(html,/Requests timing out/);
state.detail.timeout_warnings=[{...warning,deadline_source:'load',setting:'load_test.request_timeout_seconds'}];renderLive();html=nodes['#main'].innerHTML;
assert.match(html,/30s load request timeout/);assert.match(html,/higher Load request timeout/);
state.detail.timeout_warnings=[{...warning,deadline_source:'check',setting:'test_options.serving.request_timeout_seconds'}];renderLive();html=nodes['#main'].innerHTML;
assert(html.includes(`${catalogTitle('serving').toLowerCase()} request timeout`));
state.detail.timeout_warnings=[{...warning,settings:['test_options.serving.request_timeout_seconds','test_options.generation.request_timeout_seconds']}];renderLive();html=nodes['#main'].innerHTML;
assert(html.includes(`${catalogTitle('serving').toLowerCase()} request timeout and ${catalogTitle('generation').toLowerCase()} request timeout`));
state.detail.timeout_warnings=[{...warning,setting:'custom_fixtures (cases.json, fixture slow).request_timeout_seconds',setting_field:'custom_fixtures'}];renderLive();html=nodes['#main'].innerHTML;
assert.match(html,/30s fixture request timeout/);
state.detail.timeout_warnings=Array.from({length:6},(_,i)=>({...warning,key:String(i)}));renderLive();html=nodes['#main'].innerHTML;
assert.match(html,/and 2 more targets\/settings/);
state.detail.timeout_warnings=[];renderLive();assert.doesNotMatch(nodes['#main'].innerHTML,/live-timeout-warning/);
mergeRunEvents(state.detail,{seq:1,events:[],timeout_warnings:[warning]});assert.equal(state.detail.timeout_warnings[0].timed_out,3);
""")



def test_live_timeout_actions_filter_all_run_requests_and_rerun_settings():
    run_render_checks(r"""
(async()=>{
const dialog=document.querySelector('#test-dialog');
const warning={key:'local|30|run',target:'local',deadline_seconds:30,deadline_source:'run',setting:'request_timeout_seconds',finished:10,timed_out:3,no_output:3,checks:['serving']};
state.detail={id:'live',state:'running',config:{...structuredClone(bootstrap.config),targets:[{name:'local',model:'m'}]},progress:{},events:[],timeout_warnings:[warning],live_conditions:[{target:'local',condition:{id:'other',check:'serving',label:'Other'},started:1,completed:1,failed:1,request_statuses:{failed:1}},{target:'local',condition:{id:'answer',check:'serving',label:'Answer'},started:3,completed:3,failed:3,request_statuses:{timed_out:3}}]};
api=async path=>({rows:[],totals:{timed_out:3},page:1,pages:1,total:3});
state.detail.timeout_warnings.push({...warning,key:'other|30|run',target:'other'});
state.detail.live_requests=Array.from({length:10},(_,i)=>({id:'timeout-'+i,slot:i*30,target:i<5?'local':'other',scenario:i===0?'initial':'warm',condition_id:i===0?'other':'answer',status:'timeout',valid:false,elapsed_ms:2000,check_ids:['serving']}));
state.detail.live_requests.push({id:'good',slot:301,target:'local',scenario:'warm',status:'completed',valid:true,score:true,check_ids:['serving']});
renderLive();state.mosaicPage=2;showTimedOutRequests();await Promise.resolve();await Promise.resolve();
assert.equal(reportView.tab,'results');assert.equal(state.requestStatusFilter,'timed_out');assert.equal(state.mosaicPage,0);
assert.equal(conditionDialog,null);assert.equal(dialog.open,false);assert.equal(document.querySelector('#all-requests-filter').value,'timed_out');
const requests=document.querySelector('.all-requests');assert.equal(requests.open,true);
assert.equal(requests.querySelectorAll('.request-spot').length,10);
for(let i=0;i<10;i++)assert(requests.querySelector('[data-request-id="timeout-'+i+'"]'));
assert.equal(requests.querySelector('[data-request-id="good"]'),null);
renderLive();assert.equal(state.requestStatusFilter,'timed_out');assert.equal(document.querySelector('.all-requests').querySelectorAll('.request-spot').length,10);
const filter=document.querySelector('#all-requests-filter');filter.value='passed';await listeners.change({target:filter});
assert.equal(state.requestStatusFilter,'passed');assert.equal(state.mosaicPage,0);
assert.equal(document.querySelector('.all-requests').querySelectorAll('.request-spot').length,1);
let passed;
rerun=async options=>{passed=options;};
for(const setting of ['request_timeout_seconds','load_test.request_timeout_seconds','test_options.serving.request_timeout_seconds']){
  state.detail.timeout_warnings=[{...warning,setting}];renderLive();
  const button=document.querySelector('[data-action="timeout-setting"]');
  await listeners.click({target:button,preventDefault(){}});
  assert.equal(passed.focusPath,setting);
}
state.detail.timeout_warnings=[{...warning,setting:'custom_fixtures (cases.json, fixture slow).request_timeout_seconds',setting_field:'custom_fixtures'}];renderLive();
await listeners.click({target:document.querySelector('[data-action="timeout-setting"]'),preventDefault(){}});
assert.equal(passed.focusPath,'custom_fixtures');
})();
""", dom=True)


@pytest.mark.parametrize("setting,field", [
    ("request_timeout_seconds", "request_timeout_seconds"),
    ("load_test.request_timeout_seconds", "load_request_timeout_seconds"),
    ("test_options.serving.request_timeout_seconds", "test_serving_request_timeout_seconds"),
])
def test_timeout_rerun_opens_and_focuses_actual_setting(setting, field):
    run_render_checks(r"""
(async()=>{
state.config={...structuredClone(bootstrap.defaults),targets:[{name:'local',url:'http://localhost:8000/v1',model:'m'}]};state.bootstrap.config=structuredClone(state.config);
state.config.checks=['serving','load'];
state.detail={id:'live',state:'running',config:structuredClone(state.config)};
api=async(path,options)=>path==='/api/config/draft'?{config:JSON.parse(options.body),migration_notes:[]}:{};
updateNav=()=>{};
Object.defineProperty(Object.getPrototypeOf(document.body),'parentElement',{get(){return this.parentNode;}});
let revealedStep,revealedCheck;
revealSetupStep=step=>{revealedStep=step;};revealCatalogueCheck=check=>{revealedCheck=check;};
renderNew=()=>{document.querySelector('#main').innerHTML='<form id="new-run-form"><details id="timeout-details"><input id="field-'+FIELD+'" name="'+FIELD+'"></details></form>';};
await rerun({focusPath:SETTING});await route();
assert.equal(location.hash,'#new');
assert.equal(document.activeElement.getAttribute('name'),FIELD,nodes['#main'].innerHTML);
assert.equal(state.detail.state,'running');
assert.equal(revealedStep,2);assert.equal(document.querySelector('#timeout-details').open,true);
assert.equal(revealedCheck,SETTING.startsWith('load_test.')?'load':SETTING.startsWith('test_options.')?'serving':undefined);
})();
""".replace("SETTING", json.dumps(setting)).replace("FIELD", json.dumps(field)), dom=True)


def test_timeout_setting_buttons_select_their_warning_group():
    run_render_checks(r"""
(async()=>{
const warnings=[{key:'first|30|run',target:'first',deadline_seconds:30,deadline_source:'run',setting:'request_timeout_seconds',finished:10,timed_out:3,no_output:3},{key:'second|45|load',target:'second',deadline_seconds:45,deadline_source:'load',setting:'load_test.request_timeout_seconds',finished:8,timed_out:3,no_output:3}];
state.detail={id:'live',state:'running',config:{targets:[{name:'local',model:'m'}]},events:[],progress:{},timeout_warnings:warnings};
renderLive();
const buttons=document.querySelectorAll('[data-action="timeout-setting"]');
assert.equal(buttons.length,2);
let passed;rerun=async options=>{passed=options;};
for(let i=0;i<buttons.length;i++){
  assert.equal(buttons[i].dataset.timeoutKey,warnings[i].key);
  await listeners.click({target:buttons[i],preventDefault(){}});
  assert.equal(passed.focusPath,warnings[i].setting);
}
})();
""", dom=True)


def test_finished_timeout_notice_and_actions_use_saved_target_warnings():
    run_render_checks(r"""
(async()=>{
const warning={key:'local|30|run',target:'local',deadline_seconds:30,deadline_source:'run',setting:'request_timeout_seconds',finished:10,timed_out:3,answer_started:1,reasoning_only:1,no_output:1};
const other={...warning,key:'other|45|load',target:'other',deadline_seconds:45,deadline_source:'load',setting:'load_test.request_timeout_seconds'};
const value=report([check('serving','fail')],{requests:[
 {id:'first-timeout',target:'local',scenario:'initial',status:'timeout',valid:false,elapsed_ms:30000,check_ids:['serving']},
 {id:'other-timeout',target:'other',scenario:'warm',status:'timeout',valid:false,elapsed_ms:45000,check_ids:['serving']},
 {id:'good',target:'local',scenario:'warm',status:'completed',valid:true,score:true,elapsed_ms:1,check_ids:['serving']}
],observations:{targets:{local:{timeout_warnings:[warning]},other:{timeout_warnings:[other]}}}});
detail(value);chooseReportTab('results');
const panel=document.querySelector('#report-results'),notice=document.querySelector('.report-timeout-warning');assert(notice);
assert.equal(notice.querySelector('strong').textContent,'Requests timed out');
assert.equal(notice.getAttribute('role'),'status');assert.equal(notice.getAttribute('aria-live'),null);
assert.equal(panel.querySelector('.live-timeout-warning'),null);
assert.match(notice.textContent,/local · 3 of 10 requests hit the 30s request timeout/);
assert.match(notice.innerHTML,/Each request could take up to 30s\. To give the server more time, run again with a higher Request timeout\. Keep it if responses slower than 30s should count as failures\. Intentional timeout tests are not counted here\./);
assert.equal(panel.querySelector('.report-timeout-warning'),null);assert.equal(document.querySelector('#report-performance').querySelector('.report-timeout-warning'),null);
const siblings=notice.parentNode.childNodes.filter(node=>node.nodeType===1);assert.equal(siblings[siblings.indexOf(notice)+1],document.querySelector('.report-toolbar'));
assert.equal(document.querySelectorAll('.report-timeout-warning').length,1);chooseReportTab('performance');assert.equal(document.querySelectorAll('.report-timeout-warning').length,1);
assert(nodes['#main'].innerHTML.indexOf('report-timeout-warning')<nodes['#main'].innerHTML.indexOf('condition-mosaic'));
const buttons=notice.querySelectorAll('[data-action="timeout-setting"]');assert.equal(buttons.length,2);
let options;rerun=async value=>{options=value;};await listeners.click({target:buttons[1],preventDefault(){}});assert.equal(options.focusPath,'load_test.request_timeout_seconds');
state.mosaicPage=2;await listeners.click({target:notice.querySelector('[data-action="show-timeouts"]'),preventDefault(){}});
assert.equal(state.requestStatusFilter,'timed_out');assert.equal(state.mosaicPage,0);assert.equal(conditionDialog,null);assert.equal(document.querySelector('#test-dialog').open,false);
const requests=document.querySelector('.all-requests');assert(requests.open);assert.equal(requests.querySelectorAll('.request-spot').length,2);assert.equal(requests.querySelector('[data-request-id="good"]'),null);
})();
""", dom=True)


def test_old_reports_without_timeout_warning_observations_render_no_notice():
    run_render_checks(r"""
for(const observations of [undefined,{}, {targets:{local:{}}}, {targets:{local:{timeout_warnings:[]}}}]){
  detail(report([check('serving','pass')],{observations}));
  assert.doesNotMatch(nodes['#main'].innerHTML,/report-timeout-warning|live-timeout-warning/);
}
""")


@pytest.mark.parametrize("live", [False, True])
def test_run_timeout_filter_paginates_all_conditions_and_keeps_request_links(live):
    run_render_checks(r"""
(async()=>{
const live=LIVE;
const records=Array.from({length:241},(_,i)=>({id:'timeout-'+i,slot:i*2,target:'local',scenario:i%2?'warm':'initial',status:'timeout',valid:false,elapsed_ms:2000,check_ids:['serving']}));
records.push({id:'good',slot:999,target:'local',scenario:'warm',status:'completed',valid:true,score:true,elapsed_ms:1,check_ids:['serving']});
const config={targets:[{name:'local',model:'m'}],max_requests:1000};
state.detail=live?{id:'live',state:'running',config,events:[],progress:{},live_requests:records}:{id:'saved',state:'completed',report:report([check('serving','fail')],{requests:records})};
renderDetail();showTimedOutRequests();
let requests=document.querySelector('.all-requests');
assert.equal(requests.querySelectorAll('.request-spot').length,240);assert.equal(state.mosaicPage,0);
await listeners.click({target:requests.querySelector('[data-action="mosaic-next"]'),preventDefault(){}});
requests=document.querySelector('.all-requests');assert.equal(state.mosaicPage,1);assert.equal(requests.querySelectorAll('.request-spot').length,1);
const last=requests.querySelector('[data-request-id="timeout-240"]');assert(last);assert.equal(last.getAttribute(live?'data-live-slot':'data-request'),live?'480':'timeout-240');
assert.equal(requests.querySelector('[data-request-id="good"]'),null);assert.equal(document.querySelector('#test-dialog').open,false);
renderDetail();assert.equal(state.mosaicPage,1);assert.equal(document.querySelector('#all-requests-filter').value,'timed_out');
showTimedOutRequests();assert.equal(state.mosaicPage,0);
})();
""".replace("LIVE", "true" if live else "false"), dom=True)


@pytest.mark.parametrize("live", [False, True])
def test_timeout_notice_shared_above_tabs_and_grouped_setting_actions(live):
    run_render_checks(r"""
(async()=>{
const settings=['test_options.serving.request_timeout_seconds','test_options.generation.request_timeout_seconds'];
const warning={key:'local|2|check',target:'local',deadline_seconds:2,setting:settings[0],settings,finished:10,timed_out:3,no_output:3};
if(LIVE){state.detail={id:'live',state:'running',config:{targets:[{name:'local',model:'m'}]},events:[],progress:{},timeout_warnings:[warning],live_requests:[]};renderLive();}
else detail(report([check('serving','fail')],{requests:[],observations:{targets:{local:{timeout_warnings:[warning]}}}}));
const selector=LIVE?'.live-timeout-warning':'.report-timeout-warning';
let notice=document.querySelector(selector);assert(notice);
assert.equal(notice.querySelector('strong').textContent,LIVE?'Requests are timing out':'Requests timed out');
assert.equal(notice.querySelector('[data-action="show-timeouts"]'),null);
assert(notice.textContent.includes(LIVE?'Timed-out requests have left the live request list. The saved report lists all of them.':'Timed-out request details were not kept in this report.'));
assert(!notice.textContent.includes('Timed-out requests appear here when the report is saved.'));
assert(notice.textContent.includes(`${catalogTitle('serving').toLowerCase()} request timeout and ${catalogTitle('generation').toLowerCase()} request timeout`));
let passed;rerun=async options=>{passed=options;};
const buttons=notice.querySelectorAll('[data-action="timeout-setting"]');assert.equal(buttons.length,2);
for(let i=0;i<2;i++){
 assert.equal(buttons[i].dataset.timeoutKey,warning.key);assert.equal(buttons[i].dataset.timeoutSetting,settings[i]);
 assert.equal(buttons[i].textContent,`Change ${catalogTitle(settings[i].split('.')[1])} request timeout`);
 await listeners.click({target:buttons[i],preventDefault(){}});assert.equal(passed.focusPath,settings[i]);
}
chooseReportTab('performance');
notice=document.querySelector(selector);assert.equal(document.querySelectorAll(selector).length,1);
assert.equal(document.querySelector('#report-results').querySelector(selector),null);
assert.equal(document.querySelector('#report-performance').querySelector(selector),null);
const siblings=notice.parentNode.childNodes.filter(node=>node.nodeType===1);
assert.equal(siblings[siblings.indexOf(notice)+1],document.querySelector('.report-toolbar'));
if(LIVE){state.detail.live_requests=[{id:'timeout',slot:0,target:'local',scenario:'warm',status:'timeout',valid:false}];renderLive();}
else {state.detail.report.requests=[{id:'timeout',target:'local',scenario:'warm',status:'timeout',valid:false}];renderDetail();}
const show=document.querySelector(selector).querySelector('[data-action="show-timeouts"]');assert(show);
await listeners.click({target:show,preventDefault(){}});assert.equal(reportView.tab,'results');assert.equal(state.requestStatusFilter,'timed_out');
})();
""".replace('LIVE', 'true' if live else 'false'), dom=True)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("rows", [0, 4096])
def test_timeout_notice_without_available_timeouts_uses_one_view_specific_hint(live, rows):
    run_render_checks(r"""
{
const warning={key:'local|2|run',target:'local',deadline_seconds:2,setting:'request_timeout_seconds',finished:4099,timed_out:3,no_output:3};
const records=Array.from({length:ROWS},(_,i)=>({id:'success-'+i,slot:i+3,target:'local',scenario:'warm',status:'completed',valid:true,score:true}));
state.detail=LIVE?{id:'live',state:'running',config:{targets:[{name:'local',model:'m'}]},events:[],progress:{},timeout_warnings:[warning],live_requests:records}:{id:'saved',state:'completed',report:report([check('serving','fail')],{requests:records,observations:{targets:{local:{timeout_warnings:[warning]}}}})};
renderDetail();
const notice=document.querySelector(LIVE?'.live-timeout-warning':'.report-timeout-warning');assert(notice);
assert.equal(notice.querySelector('[data-action="show-timeouts"]'),null);
const hints=notice.querySelectorAll('p.hint');assert.equal(hints.length,1);
assert.equal(hints[0].textContent,LIVE?'Timed-out requests have left the live request list. The saved report lists all of them.':'Timed-out request details were not kept in this report.');
assert(!notice.textContent.includes('Timed-out requests appear here when the report is saved.'));
(LIVE?state.detail.live_requests:state.detail.report.requests).push({id:'timeout',slot:5000,target:'local',scenario:'warm',status:'timeout',valid:false});renderDetail();
const updated=document.querySelector(LIVE?'.live-timeout-warning':'.report-timeout-warning');
assert(updated.querySelector('[data-action="show-timeouts"]'));assert.equal(updated.querySelectorAll('p.hint').length,0);
}
""".replace('LIVE', 'true' if live else 'false').replace('ROWS', str(rows)), dom=True)


def test_cli_live_request_deltas_keep_cursor_and_merge_existing_rows():
    run_render_checks(r"""
{
const original={id:'first',slot:0,target:'local',status:'timeout'};
const detail={id:'cli',state:'running',stoppable:false,seq:100,live_requests:[original]};
mergeRunEvents(detail,{seq:102,records:[{id:'second',slot:1,target:'local',status:'completed'},{id:'third',slot:2,target:'local',status:'completed'}],live_request_count:3});
assert.equal(detail.seq,102);assert.deepEqual(detail.live_requests.map(row=>row.id),['first','second','third']);
mergeRunEvents(detail,{seq:102,idle:true,records:[]});
assert.equal(detail.seq,102);assert.equal(detail.live_requests.length,3);
mergeRunEvents(detail,{seq:103,records:[{id:'third',slot:2,target:'local',status:'timeout'}]});
assert.equal(detail.live_requests.length,3);assert.equal(detail.live_requests[2].status,'timeout');
}
""")
