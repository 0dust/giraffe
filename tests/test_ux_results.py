"""Exercise the rendered result states without requiring a running LLM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def run_render_checks(assertions: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser result rendering")
    app = Path(__file__).parents[1] / "src/giraffe/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes={'#main':{innerHTML:''},'#run-filter':{value:'all'}};
const context={assert,structuredClone,nodes,
  document:{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener(){}},
  window:{addEventListener(){}}};
const source=fs.readFileSync(APP_PATH,'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
vm.runInNewContext(source+`
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
""".replace("APP_PATH", json.dumps(str(app))).replace(
        "ASSERTIONS", json.dumps(assertions)
    )
    result = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_first_run_and_filtered_history_have_distinct_next_actions():
    run_render_checks(r"""
state.bootstrap={checks:[{title:'Do not show full catalogue'}]};
renderRuns();
let html=nodes['#main'].innerHTML;
assert.equal((html.match(/class="button primary"/g)||[]).length,1);
assert.match(html,/Start first check/);
assert.match(html,/Connect your endpoint/);
assert.match(html,/Understand the result/);
assert.doesNotMatch(html,/Do not show full catalogue|run-filter|suite-list/);
state.runs=[{id:'failed',state:'completed',overall:'fail',targets:[{name:'<target>',model:'model'}],
  check_counts:{fail:1,blocked:2,inconclusive:3}},
  {id:'cancelled',state:'cancelled',overall:'fail',check_counts:{}}];
renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/1 failed · 2 blocked · 3 inconclusive/);
assert.match(html,/&lt;target&gt;/);
assert.match(html,/Open result/);
assert.doesNotMatch(html,/What gets checked|suite-list/);
state.filter='pass';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/No runs match this filter/);
assert.match(html,/data-action="clear-filter"/);
assert.doesNotMatch(html,/Start first check/);
state.filter='cancelled';renderRuns();html=nodes['#main'].innerHTML;
assert.match(html,/#runs\/cancelled/);
assert.doesNotMatch(html,/#runs\/failed/);
""")


def test_failures_and_partial_results_do_not_promote_saving_a_baseline():
    run_render_checks(r"""
const failed=report([check('correctness','fail',{
  summary:'Expected <Paris>; inspect the returned answer.',metrics:{correct:1,scored:3}})],{overall:'fail'});
let html=detail(failed);
assert.match(html,/<h1>Answer correctness: 1 of 3 correct<\/h1>/);
assert.match(html,/Expected &lt;Paris&gt;/);
assert.match(html,/class="button primary" data-action="inspect-finding">Inspect check/);
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
assert.match(html,/Checking your endpoint/);
assert.match(html,/Requests finished \/ started/);
assert.match(html,/Active requests<\/dt><dd>3/);
assert.match(html,/Requests started · 12 \/ 100 limit/);
assert.match(html,/Request ceiling, not completion/);
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
assert.match(html,/The comparison is inconclusive/);assert.match(html,/does not establish improvement or regression/);
assert.match(html,/Before &lt;upgrade&gt;/);assert.match(html,/local &lt;gpu&gt;/);
assert.equal((html.match(/data-comparison-group=/g)||[]).length,1);
assert.match(html,/data-comparison-group="context"/);assert.match(html,/Recall in long prompts/);
assert.match(html,/1 regressed · 1 inconclusive/);assert.match(html,/125 ms/);assert.match(html,/n=6/);
assert.match(html,/question &lt;one&gt;/);assert.doesNotMatch(html,/<one>|<upgrade>|<gpu>/);
assert.doesNotMatch(html,/<details class="comparison-check-group"[^>]* open/);
state.comparisonFilter='all';html=renderComparison(value);
assert.equal((html.match(/data-comparison-group=/g)||[]).length,2);
assert.equal((html.match(/<tr>/g)||[]).length,5); // Three measurements and two group headers.
state.comparisonFilter='pass';state.comparisonCheck='context';html=renderComparison(value);
assert.match(html,/No measurements match/);
assert.equal(JSON.stringify(value),original);
""")


def test_failure_action_selects_evidence_from_the_failed_check_only():
    run_render_checks(r"""
const failed=check('correctness','fail',{metrics:{correct:1,scored:2},evidence_ids:['bad','good']});
const value=report([check('access','pass'),failed],{overall:'fail',requests:[
  {id:'unrelated',score:false},{id:'good',score:true},{id:'bad',score:false}]});
let html=detail(value);
assert.match(html,/data-action="inspect-finding">Inspect failed answer/);
assert.match(html,/1 of 2 scored answers did not meet/);
let opened;openRequest=id=>opened=id;inspectFinding();assert.equal(opened,'bad');
assert.equal((html.match(/data-check="local:correctness"/g)||[]).length,1);
assert.doesNotMatch(html,/result-check-strip/);
value.requests=[];detail(value);assert.equal(findingEvidence(value).record,undefined);
assert.match(nodes['#main'].innerHTML,/data-action="inspect-finding">Inspect check/);
""")
