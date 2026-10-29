"""Storage failures and warnings must preserve usable result and cleanup views."""

from tests.test_ux_results import run_render_checks


def test_storage_failure_preserves_runs_and_poll_retries_storage_independently():
    run_render_checks(r"""
(async()=>{
setActive=value=>state.active=value;renderRailRuns=()=>{};schedulePoll=()=>{};connectionError=()=>{};
nodes['#run-count']={};globalThis.location={hash:'#runs'};
const saved=[{id:'available',state:'completed',overall:'pass',targets:[{model:'available-model'}]}];
let failStorage=true,failRuns=false;
api=async path=>{if(path==='/api/runs'){if(failRuns)throw Error('Runs unavailable');return {runs:saved};}if(failStorage)throw Error('Storage unavailable');return {runs_bytes:10,baselines_bytes:0,free_bytes:100,limits:{}};};
await refreshRuns();renderRuns();
assert.equal(state.runs[0].id,'available');assert.equal(state.storage,null);assert(state.storageUnavailable);
assert.match(nodes['#main'].innerHTML,/available-model/);assert.match(nodes['#main'].innerHTML,/Storage information unavailable/);
failStorage=false;await pollRuns(state.routeVersion);
assert.match(nodes['#main'].innerHTML,/Runs: 10 B/);assert(!state.storageUnavailable);
failStorage=true;await pollRuns(state.routeVersion);
assert.match(nodes['#main'].innerHTML,/Storage information unavailable/);
failRuns=true;await assert.rejects(refreshRuns(),/Runs unavailable/);
})()
""")


def test_completed_report_warning_does_not_override_passing_verdict():
    run_render_checks(r"""
const warning='HTML report could not be saved: disk full; report.json is complete';
const html=detail(report([check('access','pass')]),{state:'completed',error:warning});
assert.match(html,/<h1>1 check passed<\/h1>/);assert.doesNotMatch(html,/verdict-word|Acceptance passed/);
assert.match(html,/class="notice warning"/);assert.match(html,/HTML report could not be saved/);
assert.match(html,/data-action="baseline-dialog">Save baseline/);
assert.doesNotMatch(html,/Runner error|Run could not finish|class="notice error"/);
""")


def test_cleanup_zero_negative_or_invalid_total_is_rejected_before_api_request():
    run_render_checks(r"""
(async()=>{
const dialog=nodes['#storage-dialog']={innerHTML:'',showModal(){this.open=true;}};
nodes['#cleanup-age']={focus(){}};openStorageDialog();
assert.match(dialog.innerHTML,/counts protected runs too/);
assert.match(dialog.innerHTML,/<form id="cleanup-form" novalidate>/);
assert.match(dialog.innerHTML,/name="max_total_mib" type="number" min="0.000001"/);
let valid=false;const form=nodes['#cleanup-form']={reportValidity:()=>valid,elements:{older_than_days:{value:''},keep_latest:{value:''},max_total_mib:{value:''}}};
for(const id of ['cleanup-error','cleanup-preview-button','cleanup-confirm','cleanup-preview'])nodes['#'+id]={innerHTML:'',hidden:true};
let calls=0;api=async()=>{calls++;return {candidates:[],protected:[],bytes_freed:0,runs_bytes_after:0};};
for(const value of ['0','-1','NaN','0.00000001']){
  form.elements.max_total_mib.value=value;await previewCleanup({preventDefault(){}});
  assert.equal(calls,0);assert.equal(state.cleanup.preview,null);assert(nodes['#cleanup-confirm'].disabled);
  assert(!nodes['#cleanup-error'].hidden);assert.match(nodes['#cleanup-error'].textContent,/greater than 0/);assert.match(nodes['#cleanup-error'].textContent,/Older than 0 days/);assert.doesNotMatch(nodes['#cleanup-error'].textContent,/--older-than/);
}
form.elements.max_total_mib.value='';await previewCleanup({preventDefault(){}});assert.equal(calls,0);
valid=true;await previewCleanup({preventDefault(){}});assert.equal(calls,1);
})()
""")


def test_check_evidence_prioritizes_invalid_and_evaluation_errors_without_missing_valid_bias():
    run_render_checks(r"""
const request=(id,extra={})=>({id,target:'local',scenario:id,status:'completed',elapsed_ms:1,...extra});
const requests=[request('good',{valid:true}),request('legacy'),request('invalid',{valid:false}),request('evaluation',{valid:true,evaluation_error:'grading unavailable'}),request('legacy-evaluation',{status:'evaluation_error'}),request('failed',{status:'failed'}),request('incorrect',{score:false})];
const value=report([check('correctness','fail',{evidence_ids:requests.map(r=>r.id)})],{overall:'fail',requests});
detail(value);state.selectedCheck='local:correctness';const html=renderChecks(value),list=html.slice(html.indexOf('class="evidence-table"'));
const position=id=>list.indexOf('data-request="'+id+'"');
for(const id of ['incorrect','invalid','evaluation','legacy-evaluation','failed']){
  assert(position(id)>=0);assert(position(id)<position('good'));assert(position(id)<position('legacy'));
}
assert(position('incorrect')<position('invalid'));assert(position('good')<position('legacy'));
""")
