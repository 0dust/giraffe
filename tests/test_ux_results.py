"""Exercise the rendered result states without requiring a running LLM."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities


def run_render_checks(assertions: str, *, dom: bool = False) -> None:
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
  window:{addEventListener(){}}};
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
        timeout=20,
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
state.filter='cancelled';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/#runs\/cancelled/);assert.match(html,/Cancelled/);
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
  summary:'Expected <Paris>; inspect the returned answer.',metrics:{correct:1,scored:3}})],{overall:'fail'});
let html=detail(failed);
assert.match(html,/<h1>Answer correctness: 1 of 3 correct<\/h1>/);
assert.match(html,/Expected &lt;Paris&gt;/);
assert.match(html,/class="button primary" data-action="rerun">Review settings/);
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
assert.match(html,/<h1>GPU metrics is inconclusive<\/h1>/);
assert.doesNotMatch(html,/class="button primary" data-action="baseline-dialog"/);
html=detail({...passed,overall:'fail',baseline:{status:'fail'}});
assert.match(html,/A material regression was detected/);
assert.match(html,/class="button primary" data-tab="baseline">Inspect comparison/);
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
assert.match(html,/<details class="checks-not-run"\s*>/);
const disclosure=html.match(/<details class="checks-not-run"[^>]*>([\s\S]*?)<\/details>/)[1];
assert.match(disclosure,/Not run · 2/);
assert.match(disclosure,/data-check="local:json"/);
assert.match(disclosure,/Server did not expose &lt;tools&gt;/);
assert.doesNotMatch(disclosure,/data-check="local:access"|data-check="local:correctness"/);
state.selectedCheck='local:tools';html=renderChecks(value);
assert.match(html,/<details class="checks-not-run" open>/);
assert.match(html,/This check was skipped/);
assert.doesNotMatch(html,/This optional check was not selected/);
state.selectedCheck='local:json';html=renderChecks(value);
assert.match(html,/This check was not selected/);
const allSkipped=report([check('json','skipped',{summary:'Not selected for this run.'})],{overall:'skipped'});
html=detail(allSkipped);
assert.match(html,/<h1>No checks ran<\/h1>/);
assert.match(html,/<details class="checks-not-run"\s*>/);
assert.equal(state.selectedCheck,null);
const selectedSkipped=report([check('access','pass'),check('tools','skipped',{
  summary:'The server did not expose <tools>.'})]);
selectedSkipped.manifest.config.checks.push('tools');
html=detail(selectedSkipped);
assert.match(html,/<h1>Some selected checks did not run<\/h1>/);
assert.match(html,/Acceptance inconclusive/);
assert.doesNotMatch(html,/Acceptance passed|Selected acceptance checks passed/);
assert.doesNotMatch(html,/class="button primary" data-action="baseline-dialog"/);
""")


def test_live_counters_distinguish_started_requests_from_budget_and_completion():
    run_render_checks(r"""
state.detail={id:'live',state:'running',config:{targets:[{name:'local',model:'test'}],
  max_requests:100,concurrency:4,max_duration_seconds:60,request_timeout_seconds:10,max_output_tokens:32},
  progress:{attempted:12,completed:9,elapsed_seconds:5},events:[]};
renderLive();let html=nodes['#main'].innerHTML;
assert.match(html,/Mission control/);
assert.match(html,/Requests finished \/ started/);
assert.match(html,/Active requests<\/dt><dd>3/);
assert.match(html,/Requests started · 12 \/ 100 limit/);
assert.match(html,/Request starts, not completions/);
assert.match(html,/Scheduled requests can also use budget without reaching the server/);
assert.doesNotMatch(html,/Request budget used/);
state.detail.state='stopping';renderLive();html=nodes['#main'].innerHTML;
assert.match(html,/data-action="stop" disabled/);
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
let html=detail(value);
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
assert.equal($('#tab-performance').getAttribute('aria-selected'),'true');
assert.equal($('#tab-results').getAttribute('tabindex'),'-1');
let prevented=0;
const key=key=>listeners.keydown({target:document.activeElement,key,preventDefault(){prevented++;}});
$('#tab-performance').focus({preventScroll:true});key('ArrowRight');
assert.equal(reportView.tab,'results');assert.equal(document.activeElement,$('#tab-results'));
assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
assert.equal($('#tab-performance').getAttribute('tabindex'),'-1');
key('Home');assert.equal(reportView.tab,'performance');
key('ArrowLeft');assert.equal(reportView.tab,'results');
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
