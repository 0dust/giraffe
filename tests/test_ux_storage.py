"""Storage controls remain explicit and usable without a browser server."""

from tests.test_ux_results import run_render_checks


def test_history_exposes_disk_totals_run_sizes_and_kept_marker():
    run_render_checks(r"""
state.storage={runs_bytes:1.25*1024**3,baselines_bytes:40*1024**2,free_bytes:38*1024**3,limits:{max_total_storage:5*1024**3}};
state.runs=[{id:'retained',state:'completed',overall:'pass',bytes:2.5*1024**2,kept:true,targets:[{model:'test'}]}];
renderRuns();const html=nodes['#main'].innerHTML;
assert.match(html,/Saved runs on disk: 1.3 GiB of 5 GiB limit · Baselines 40 MiB · Automatic cleanup: off · Free disk 38 GiB/);
assert.match(html,/2.5 MiB on disk/);assert.match(html,/class="kept-marker">Kept/);
assert.match(html,/<button type="button" class="button small" data-action="storage-dialog">Clean up…/);
assert.doesNotMatch(html,/memory|RAM/);
""")


def test_disk_formatter_uses_binary_units_at_each_boundary():
    run_render_checks(r"""
for(const [bytes,expected] of [[0,'0 B'],[1,'1 B'],[1023,'1,023 B'],[1024,'1 KiB'],[1536,'1.5 KiB'],[1024**2,'1 MiB'],[5*1024**3,'5 GiB'],[1024**4,'1 TiB']])assert.equal(diskSize(bytes),expected);
""")


def test_cleanup_requires_preview_and_explicit_confirmation_with_same_ids_and_criteria():
    run_render_checks(r"""
(async()=>{
const dialog=nodes['#storage-dialog']={innerHTML:'',open:false,showModal(){this.open=true;},close(){this.open=false;}};
nodes['#cleanup-age']={focus(){this.focused=true;}};
openStorageDialog();assert(dialog.open);assert(nodes['#cleanup-age'].focused);
assert.match(dialog.innerHTML,/for="cleanup-age"/);assert.match(dialog.innerHTML,/for="cleanup-latest"/);assert.match(dialog.innerHTML,/for="cleanup-total"/);
assert.match(dialog.innerHTML,/Maximum saved runs on disk \(MiB\)/);
assert.match(dialog.innerHTML,/id="cleanup-confirm" data-action="cleanup-confirm" disabled/);
const form=nodes['#cleanup-form']={reportValidity:()=>true,elements:{older_than_days:{value:'7'},keep_latest:{value:'2'},max_total_mib:{value:'50'}}};
for(const id of ['cleanup-error','cleanup-preview-button','cleanup-confirm','cleanup-preview'])nodes['#'+id]={innerHTML:'',disabled:false,hidden:false};
const calls=[];
api=async(path,options)=>{calls.push([path,options&&JSON.parse(options.body)]);if(path.endsWith('cleanup-preview'))return {candidates:[{id:'old <run>',bytes:2*1024**2,started_at:'2026-01-01T00:00:00Z',criterion:'older_than_days'}],protected:[{id:'keep',bytes:100,protected_reason:'marked kept'}],bytes_freed:2*1024**2,runs_bytes_after:100};if(path.endsWith('/cleanup'))return {deleted:['old <run>'],skipped:[],bytes_freed:2*1024**2};if(path==='/api/runs')return {runs:[]};if(path==='/api/storage')return {runs_bytes:100,limits:{}};throw Error(path);};
await confirmCleanup();assert.equal(calls.length,0);
await previewCleanup({preventDefault(){}});
assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/storage/cleanup-preview');
assert.match(nodes['#cleanup-preview'].innerHTML,/old &lt;run&gt;/);assert.match(nodes['#cleanup-preview'].innerHTML,/2 MiB on disk/);assert.match(nodes['#cleanup-preview'].innerHTML,/Older than 7 days/);assert.match(nodes['#cleanup-preview'].innerHTML,/marked kept/);
assert.equal(nodes['#cleanup-confirm'].textContent,'Delete 1 run (2 MiB)');assert.equal(nodes['#cleanup-confirm'].disabled,false);
const first=JSON.stringify(calls[0][1]);
form.elements.older_than_days.value='10';
listeners.input({target:{closest:q=>q==='#cleanup-form'?form:null}});
assert(nodes['#cleanup-confirm'].disabled);await confirmCleanup();assert.equal(calls.length,1);
await previewCleanup({preventDefault(){}});assert.equal(calls.length,2);assert.notEqual(JSON.stringify(calls[1][1]),first);
setActive=()=>{};nodes['#run-count']={};toast=()=>{};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'cleanup-confirm'}}:null}});
assert.equal(calls[2][0],'/api/storage/cleanup');assert.deepEqual(calls[2][1],{older_than_days:10,keep_latest:2,max_total_bytes:50*1024**2,ids:['old <run>']});
assert(!dialog.open);assert.match(nodes['#main'].innerHTML,/role="status">Deleted 1 run, freed 2 MiB on disk/);
assert(calls.some(([path])=>path==='/api/runs'));assert(calls.some(([path])=>path==='/api/storage'));
})()
""")


def test_cleanup_pluralization_and_persistent_skipped_results():
    run_render_checks(r"""
(async()=>{
const dialog=nodes['#storage-dialog']={open:true,close(){this.open=false;}};
const form=nodes['#cleanup-form']={reportValidity:()=>true,elements:{older_than_days:{value:'1'},keep_latest:{value:'1'},max_total_mib:{value:''}}};
for(const id of ['cleanup-error','cleanup-preview-button','cleanup-confirm','cleanup-preview'])nodes['#'+id]={};
state.cleanup={revision:0,preview:null};
let count=1,skipped=true;
api=async path=>{
 if(path.endsWith('cleanup-preview'))return {candidates:Array.from({length:count},(_,i)=>({id:'run-'+i,bytes:1024,criterion:'older_than_days, keep_latest'})),protected:[{id:'kept',protected_reason:'run is kept'}],bytes_freed:count*1024,runs_bytes_after:0};
 if(path.endsWith('/cleanup'))return skipped?{deleted:[],skipped:[{id:'run-0<script>',reason:'run is kept <safe>'}],bytes_freed:0}:{deleted:['run-0','run-1'],skipped:[],bytes_freed:3.125*1024**2};
 if(path==='/api/runs')return {runs:[]};if(path==='/api/storage')return {runs_bytes:0,limits:{}};throw Error(path);
};
setActive=()=>{};nodes['#run-count']={};
await previewCleanup({preventDefault(){}});
let html=nodes['#cleanup-preview'].innerHTML;
assert.match(html,/<h3>1 run to delete<\/h3>/);assert.match(html,/Beyond the newest run/);assert.match(html,/Older than 1 day/);assert.match(html,/<h3>1 protected run<\/h3>/);
assert.equal(nodes['#cleanup-confirm'].textContent,'Delete 1 run (1 KiB)');
await confirmCleanup();html=nodes['#main'].innerHTML;
assert(!dialog.open);assert.match(html,/role="status">Deleted 0 runs, freed 0 B on disk\. Skipped 1: run-0&lt;script&gt; — run is kept &lt;safe&gt;/);
renderRuns();assert.match(nodes['#main'].innerHTML,/Skipped 1:/);
state.runs=[{id:'kept',state:'completed',overall:'pass'}];renderRuns();assert.match(nodes['#main'].innerHTML,/Skipped 1:/);
count=3;form.elements.keep_latest.value='3';form.elements.older_than_days.value='2';dialog.open=true;skipped=false;
await previewCleanup({preventDefault(){}});html=nodes['#cleanup-preview'].innerHTML;
assert.match(html,/<h3>3 runs to delete<\/h3>/);assert.match(html,/Beyond the newest 3 runs/);assert.match(html,/Older than 2 days/);
assert.equal(nodes['#cleanup-confirm'].textContent,'Delete 3 runs (3 KiB)');
await confirmCleanup();assert.match(nodes['#main'].innerHTML,/role="status">Deleted 2 runs, freed 3.1 MiB on disk/);assert.doesNotMatch(nodes['#main'].innerHTML,/Skipped 1:/);
})()
""")


def test_cleanup_edit_while_preview_pending_discards_stale_result():
    run_render_checks(r"""
(async()=>{
state.cleanup={revision:0,preview:null};nodes['#storage-dialog']={open:true};
nodes['#cleanup-form']={reportValidity:()=>true,elements:{older_than_days:{value:'1'},keep_latest:{value:''},max_total_mib:{value:''}}};
for(const id of ['cleanup-error','cleanup-preview-button','cleanup-confirm','cleanup-preview'])nodes['#'+id]={innerHTML:''};
let resolve;api=()=>new Promise(done=>resolve=done);
const pending=previewCleanup({preventDefault(){}});invalidateCleanup();
resolve({candidates:[{id:'stale'}],protected:[],bytes_freed:1,runs_bytes_after:0});await pending;
assert.equal(state.cleanup.preview,null);assert(nodes['#cleanup-confirm'].disabled);
assert.match(nodes['#cleanup-preview'].innerHTML,/Preview again/);
})()
""")


def test_cleanup_result_survives_history_refresh_failure():
    run_render_checks(r"""
(async()=>{
nodes['#storage-dialog']={open:true,close(){this.open=false;}};
for(const id of ['cleanup-error','cleanup-preview-button','cleanup-confirm'])nodes['#'+id]={};
state.cleanup={preview:{criteria:{older_than_days:0},ids:['kept']}};
let message;toast=value=>message=value;
api=async path=>{if(path==='/api/storage/cleanup')return {deleted:[],bytes_freed:0,skipped:[{id:'kept',reason:'run is active'}]};throw Error('History unavailable');};
await confirmCleanup();
assert(!nodes['#storage-dialog'].open);
assert.match(nodes['#main'].innerHTML,/role="status">Deleted 0 runs, freed 0 B on disk\. Skipped 1: kept — run is active/);
assert.match(message,/Cleanup finished, but run history could not be refreshed/);
assert.equal(state.cleanup.preview,null);
})()
""")


def test_keep_toggle_posts_boolean_and_refreshes_detail_markup():
    run_render_checks(r"""
(async()=>{
let html=detail(report([check('access','pass')]));assert.match(html,/data-action="keep-run" aria-pressed="false">Keep run/);
nodes['[data-action="keep-run"]']={disabled:false};
const calls=[];api=async(path,options)=>{calls.push([path,JSON.parse(options.body)]);return {kept:JSON.parse(options.body).keep};};
for(const expected of [true,false]){
  await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'keep-run'}}:null}});
  assert.equal(state.detail.kept,expected);assert.equal(calls.at(-1)[0],'/api/runs/saved-run/keep');assert.equal(calls.at(-1)[1].keep,expected);
  assert.match(nodes['#main'].innerHTML,expected?/aria-pressed="true">Kept — protected from cleanup/:/aria-pressed="false">Keep run/);
}
})()
""")


def test_summary_only_error_and_storage_omissions_remain_visible():
    run_render_checks(r"""
let html=detail(null,{state:'error',error:'Report could not be saved (disk full). Request evidence for 3 requests was lost; a summary was saved.',summary:{overall:'pass',request_count:3,checks:[{id:'access',title:'Access check',status:'pass',summary:'Endpoint responded <quickly>'}]}});
assert.match(html,/<h1>Run could not finish/);assert.match(html,/class="notice error"/);assert.match(html,/disk full/);
assert.match(html,/Only a summary was saved/);assert.match(html,/Recorded overall: Passed · 3 requests/);assert.match(html,/Endpoint response/);assert.match(html,/Endpoint responded &lt;quickly&gt;/);
assert.doesNotMatch(html,/Acceptance passed|Save baseline/);assert.match(html,/data-action="keep-run"/);
html=detail(report([check('access','pass')]),{state:'error',error:'Export failed'});assert.match(html,/<h1>Run could not finish/);assert.doesNotMatch(html,/Acceptance passed|Selected acceptance checks passed/);
const value=report([check('access','pass')]);value.manifest.storage={limit_bytes:1000,response_bodies_omitted:0};html=detail(value);assert.doesNotMatch(html,/Storage limit on disk/);
value.manifest.storage={limit_bytes:1000,response_bodies_omitted:2,reason:'Response text omitted to fit on disk.',limit_exceeded:true};
html=detail(value);assert.match(html,/class="notice warning"/);assert.match(html,/2 response bodies omitted/);assert.match(html,/Request metadata alone exceeds/);
assert.match(inputRetentionNotice,/all \/ failures \/ none/);assert.match(inputRetentionNotice,/Request metadata \(timings, status, scores\) is always saved/);assert.match(inputRetentionNotice,/Retention does not cap disk usage; storage limits do/);
""")


def test_history_shows_shared_baseline_bytes_policy_and_automatic_deletions():
    run_render_checks(r"""
state.storage={runs_bytes:1024,baselines_bytes:100,baselines_shared_bytes:2048,free_bytes:4096,limits:{},auto_cleanup:{keep_latest:5},last_auto_cleanup:{deleted:['old-a','old-b']}};
const html=storageLine();
assert.match(html,/2 KiB shared with source run/);
assert.match(html,/Automatic cleanup: keep newest 5 runs/);
assert.match(html,/Automatically deleted: old-a, old-b/);
""")
