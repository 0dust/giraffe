"""Live request rows retain identity, focus and state across delta updates."""

from tests.test_ux_results import run_render_checks


def test_live_table_keeps_rows_focus_scroll_and_sort_across_delta_appends():
    run_render_checks(r"""
state.detail={id:'table-stability',state:'running',config:{targets:[],max_requests:1000},live_requests:[{slot:0,id:'old',scenario:'short',status:'completed',valid:true,score:true,elapsed_ms:90},{slot:1,scenario:'short',status:'running',elapsed_ms:20}]};
renderDetail();$('.all-requests').open=true;listeners.toggle({target:$('.all-requests')});
updateRequestTableState(evidenceTableState(),'sort','elapsed_ms');updateRequestTableState(evidenceTableState(),'sort','elapsed_ms');renderDetail();
const first=$('[data-live-key="request:slot-0"]'),pending=$('[data-live-key="request:slot-1"]'),scroll=$('.request-evidence .request-table-scroll');
scroll.scrollTop=170;scroll.scrollLeft=90;first.focus({preventScroll:true});
const nodesBefore=$$('.request-evidence-row');renderDetail();assert.deepEqual($$('.request-evidence-row'),nodesBefore);
state.detail.live_requests[1]={slot:1,id:'finished',scenario:'short',status:'completed',valid:true,score:true,elapsed_ms:30};
state.detail.live_requests.push({slot:2,id:'appended',scenario:'short',status:'running',elapsed_ms:10});renderDetail();
assert.equal($('[data-live-key="request:slot-0"]'),first);assert.equal($('[data-live-key="request:slot-1"]'),pending);
assert.equal($('.request-evidence .request-table-scroll'),scroll);assert.equal(scroll.scrollTop,170);assert.equal(scroll.scrollLeft,90);assert.equal(document.activeElement,first);
assert.equal(evidenceTableState().sort,'elapsed_ms');assert.equal(evidenceTableState().direction,'desc');
assert.deepEqual($$('.request-evidence-row').map(row=>row.dataset.liveSlot),['0','1','2']);assert.match(pending.textContent,/Passed/);
assert.equal($$('.request-mosaic, .condition-mosaic, .request-spot, .hero-coat').length,0);
""", dom=True)


def test_only_recorded_requests_have_rows_and_budget_does_not_create_cells():
    run_render_checks(r"""
state.detail={id:'bounded',state:'running',config:{targets:[],max_requests:9999},live_request_count:300,live_requests:[{slot:299,id:'last',scenario:'short',status:'completed'}],progress:{budget_used:500,attempted:300,dropped_offers:200}};
renderDetail();assert.equal($$('.request-evidence-row').length,1);assert.match($('.request-evidence').textContent,/1 of 300 requests available/);
assert.equal($$('.request-spot, .request-mosaic, .condition-mosaic').length,0);
assert.equal($('.request-evidence-row').getAttribute('data-live-key'),'request:slot-299');
""", dom=True)


def test_terminal_slot_guard_survives_notice_changes_and_slot_reuse():
    run_render_checks(r"""
state.detail={id:'terminal-guard',state:'running',config:{targets:[]},live_requests:[{slot:0,id:'first',scenario:'short',status:'completed',valid:true,score:true}]};renderDetail();
const row=$('[data-live-key="request:slot-0"]');
state.detail.events=[{event:'run_stopping',abort_reason:'Notice before the table'}];state.detail.live_requests[0].status='running';renderDetail();
assert.equal($('[data-live-key="request:slot-0"]'),row);assert.match(row.textContent,/Passed/);assert.equal(warnings.length,1);assert.match(warnings[0][0],/regressed/);
state.detail.live_requests[0]={slot:0,id:'second',scenario:'short',status:'running'};renderDetail();
assert.equal($('[data-live-key="request:slot-0"]'),row);assert.match(row.textContent,/Running/);assert.doesNotMatch(row.textContent,/Passed/);
state.detail.id='another';state.detail.live_requests[0].status='running';renderDetail();assert.match($('.request-evidence-row').textContent,/Running/);
""", dom=True)


def test_table_keyboard_moves_between_rows_and_opens_existing_live_detail():
    run_render_checks(r"""
state.detail={id:'keyboard-table',state:'running',config:{targets:[]},live_requests:[{slot:0,id:'one',scenario:'short',status:'completed'},{slot:1,id:'two',scenario:'short',status:'running'}]};renderDetail();
const rows=$$('.request-evidence-row');let prevented=0,opened=null;openLiveRequest=slot=>opened=slot;
const key=(target,key)=>listeners.keydown({target,key,preventDefault(){prevented++;}});
rows[0].focus({preventScroll:true});key(rows[0],'ArrowDown');assert.equal(document.activeElement,rows[1]);
key(rows[1],'Enter');assert.equal(opened,1);key(rows[1],'Home');assert.equal(document.activeElement,rows[0]);assert.equal(prevented,3);
""", dom=True)


def test_removing_siblings_keeps_retained_nodes_in_place_and_actual_moves_preserve_focus():
    run_render_checks(r"""
const parent=$('#main');parent.innerHTML='<p data-live-key="removed">old</p><button data-live-key="stable">same</button>';
const stable=$('button');stable.focus({preventScroll:true});
patchHTML(parent,'<button data-live-key="stable">same</button>');
assert.equal($('button'),stable);assert.equal(stable.moves,0);assert.equal(document.activeElement,stable);
patchHTML(parent,'<button data-live-key="new">new</button><button data-live-key="stable">same</button>');
assert.equal(stable.moves,0);patchHTML(parent,'<button data-live-key="stable">same</button><button data-live-key="new">new</button>');
assert.equal(stable.stateMoves,1);assert.equal(document.activeElement,stable);
for(const node of $$('*'))node.moves=0;
patchHTML(parent,'<button data-live-key="stable">same</button><button data-live-key="new">new</button>');assert.equal(stable.moves,0);
parent.moveBefore=undefined;parent.innerHTML='<p data-live-key="old">old</p><button data-live-key="retained">same</button>';const fallback=$('button');fallback.focus({preventScroll:true});patchHTML(parent,'<button data-live-key="retained">same</button>');assert.equal(fallback.moves,0);assert.equal(document.activeElement,fallback);
""", dom=True)


def test_dialog_status_click_does_not_reopen_and_identity_updates_with_badge():
    run_render_checks(r"""
(async()=>{
state.detail={id:'dialog',state:'running',config:{targets:[],max_requests:2},live_requests:[{slot:0,id:'r',status:'running'}]};renderDetail();openLiveRequest(0);
const dialog=$('#request-dialog'),badgeNode=$('.live-request-status .status');let reopened=0;openLiveRequest=()=>{reopened++;};
await listeners.click({target:badgeNode.querySelector('.status-icon'),preventDefault(){}});assert.equal(reopened,0);assert.equal($('[data-live-slot]',dialog),null);
state.detail.live_requests[0].status='cancelled';renderDetail();assert.equal($('.live-request-status .status'),badgeNode);assert.match($('.request-identity',dialog).textContent,/Cancelled/);assert.doesNotMatch($('.request-identity',dialog).textContent,/Running|pending/i);assert(dialog.open);
})()
""", dom=True)


def test_stop_error_survives_live_patch_and_is_scoped_to_its_run():
    run_render_checks(r"""
(async()=>{
state.detail={id:'stop-error',state:'running',config:{targets:[],max_requests:1}};renderDetail();api=async()=>{throw Error('Stop could not be saved');};await stopRun();
const error=$('#live-error');assert(!error.hidden);assert.match(error.textContent,/Stop could not be saved/);
renderDetail();assert.equal($('#live-error'),error);assert(!error.hidden);assert.match(error.textContent,/Stop could not be saved/);
state.detail.id='next-run';renderDetail();assert($('#live-error').hidden);assert.equal($('#live-error').textContent,'');
})()
""", dom=True)


def test_stop_failure_clears_on_confirmed_stopping_terminal_or_a_newer_snapshot():
    run_render_checks(r"""
(async()=>{
for(const nextState of ['running','stopping','cancelled','completed','error']){
  state.detail={id:'error-'+nextState,state:'running',config:{targets:[],max_requests:1},progress:{attempted:0}};renderDetail();api=async()=>{throw Error('Stop failed');};await stopRun();assert(state.liveError);
  const snapshot={...state.detail,state:nextState};api=async path=>path==='/api/runs'?{runs:[]}:path==='/api/storage'?{}:snapshot;
  await pollDetail(state.routeVersion);assert.equal(state.liveError,null,nextState);assert(!$('#live-error')||$('#live-error').hidden);
}
state.detail={id:'other-path',state:'running',config:{targets:[],max_requests:1}};renderDetail();api=async()=>{throw Error('Stop failed');};await stopRun();state.detail.state='stopping';renderDetail();assert.equal(state.liveError,null);assert($('#live-error').hidden);
})()
""", dom=True)


def test_pre_stop_poll_cannot_undo_stop_and_fresh_active_snapshots_cannot_regress_it():
    run_render_checks(r"""
(async()=>{
state.detail={id:'stop-race',state:'running',config:{targets:[],max_requests:1}};renderDetail();
let finishPoll,finishStop;api=path=>new Promise(resolve=>{if(path.endsWith('/cancel'))finishStop=resolve;else finishPoll=resolve;});
const old=pollDetail(state.routeVersion),stop=stopRun();finishStop({state:'stopping'});await stop;
const heading=$('h1').textContent;assert.match(heading,/Stopping/);assert($('[data-action="stop"]').hasAttribute('disabled'));
finishPoll({id:'stop-race',state:'running',config:{targets:[],max_requests:1},progress:{attempted:0}});await old;
assert.equal(state.detail.state,'stopping');assert.equal($('h1').textContent,heading);assert($('[data-action="stop"]').hasAttribute('disabled'));
api=async()=>({id:'stop-race',state:'running',config:{targets:[],max_requests:1},progress:{attempted:1}});await pollDetail(state.routeVersion);assert.equal(state.detail.state,'stopping');assert.equal(state.detail.progress.attempted,1);assert.equal($('h1').textContent,heading);
})()
""", dom=True)


def test_stop_pending_and_old_snapshots_do_not_restore_controls_or_clear_a_new_error():
    run_render_checks(r"""
(async()=>{
state.detail={id:'pending-stop',state:'running',config:{targets:[],max_requests:1}};renderDetail();
const snapshot={...state.detail};let finishPoll,rejectStop;
api=path=>new Promise((resolve,reject)=>{if(path.endsWith('/cancel'))rejectStop=reject;else finishPoll=resolve;});
const old=pollDetail(state.routeVersion),stop=stopRun();assert($('[data-action="stop"]').disabled);
// Another render during the pending POST must retain the disabled control.
renderDetail();assert($('[data-action="stop"]').hasAttribute('disabled'));
rejectStop(Error('Stop failed'));await stop;const failure=state.liveError;assert(failure);
finishPoll(snapshot);await old;assert.equal(state.liveError,failure);assert.match($('#live-error').textContent,/Stop failed/);assert(!$('[data-action="stop"]').hasAttribute('disabled'));
api=async()=>({...snapshot});await pollDetail(state.routeVersion);assert.equal(state.liveError,null);assert($('#live-error').hidden);
})()
""", dom=True)


def test_poll_confirmed_stop_or_completion_wins_over_delayed_cancel_replies():
    run_render_checks(r"""
(async()=>{
for(const finalState of ['stopping','completed','cancelled'])for(const fails of [false,true]){
  state.detail={id:finalState+'-'+fails,state:'running',config:{targets:[],max_requests:1}};renderDetail();
  let reply,fail;api=()=>new Promise((resolve,reject)=>{reply=resolve;fail=reject;});const stop=stopRun();
  const snapshot={...state.detail,state:finalState};api=async path=>path==='/api/runs'?{runs:[]}:path==='/api/storage'?{}:snapshot;
  await pollDetail(state.routeVersion);assert.equal(state.detail.state,finalState);
  if(fails)fail(Error('Too late'));else reply({state:'stopping'});await stop;
  assert.equal(state.detail.state,finalState);assert.equal(state.liveError,null);assert.equal(state.stopPending,null);
}
})()
""", dom=True)


def test_out_of_order_live_snapshots_do_not_replace_newer_progress_or_errors():
    run_render_checks(r"""
(async()=>{
state.detail={id:'ordering',state:'running',config:{targets:[],max_requests:3}};renderDetail();
const snapshot={...state.detail};const replies=[];api=()=>new Promise(resolve=>replies.push(resolve));
const old=pollDetail(state.routeVersion),fresh=pollDetail(state.routeVersion);
replies[1]({...snapshot,progress:{attempted:2}});await fresh;
replies[0]({...snapshot,progress:{attempted:1}});await old;assert.equal(state.detail.progress.attempted,2);
// A stale failure must not replace a newer successful snapshot with a connection error.
let reject;api=()=>new Promise((resolve,fail)=>{reject=fail;});const stale=pollDetail(state.routeVersion);
api=async()=>({...snapshot,progress:{attempted:3}});await pollDetail(state.routeVersion);
reject(Error('Old connection failure'));await stale;assert.equal(state.detail.progress.attempted,3);
assert(!$('#connection-error')||$('#connection-error').hidden);
})()
""", dom=True)


def test_pending_stop_is_released_when_its_reply_is_discarded_after_navigation():
    run_render_checks(r"""
(async()=>{
for(const fails of [false,true]){
  state.detail={id:'navigation-'+fails,state:'running',config:{targets:[],max_requests:1}};renderDetail();
  let reply,fail;api=()=>new Promise((resolve,reject)=>{reply=resolve;fail=reject;});const stop=stopRun();
  state.routeVersion++; // Leaving and returning to the same run discards the old handler.
  if(fails)fail(Error('Old Stop failure'));else reply({state:'stopping'});await stop;
  assert.equal(state.stopPending,null);assert.equal(state.liveError,null);
  api=async()=>({...state.detail,state:fails?'running':'stopping'});await pollDetail(state.routeVersion);
  assert.equal(state.detail.state,fails?'running':'stopping');assert.equal($('[data-action="stop"]').hasAttribute('disabled'),!fails);
}
})()
""", dom=True)


def test_dropped_offers_are_not_pending_reservations_after_traffic_ends():
    run_render_checks(r"""
const records=[{id:'first',slot:0,condition_id:'access',scenario:'access',status:'completed',valid:true,elapsed_ms:3},{id:'second',slot:1,condition_id:'access',scenario:'access',status:'completed',valid:true,elapsed_ms:4}];
const condition={target:'local',condition:{id:'access',check:'access'},status:'passed',aggregate_status:'passed',planned:null,started:2,completed:2,running:0,failed:0};
state.detail={id:'dropped',state:'running',config:{targets:[],max_requests:4},live_requests:records,live_conditions:[condition],progress:{event:'condition_finished',budget_used:4,attempted:2,completed:2,dropped_offers:2}};
renderDetail();
assert.equal($$('.request-evidence-row').length,2);assert.equal($$('.request-evidence-row.running,.request-evidence-row.queued').length,0);
assert.match($('.condition-strip').textContent,/Passed/);assert.doesNotMatch($('.condition-strip').textContent,/Running|Queued|pending/i);
assert.equal($('.request-evidence').querySelectorAll('[data-live-slot]').length,2);
state.detail={...state.detail,state:'completed',report:report([check('access','pass')],{requests:records,conditions:[condition]})};renderDetail();
assert.equal($$('.request-evidence-row').length,2);assert.equal($$('.request-evidence-row.running,.request-evidence-row.queued').length,0);
assert.doesNotMatch($('.request-evidence').textContent,/pending|reservation/i);
""", dom=True)


def test_unchanged_delta_polls_never_rewrite_request_rows_or_condition_strip_dom():
    run_render_checks(r"""
(async()=>{
state.detail={id:'unchanged-delta',seq:7,state:'running',config:{targets:[]},live_requests:[{slot:0,id:'one',condition_id:'access',scenario:'access',status:'completed',valid:true,elapsed_ms:5}],live_conditions:[{target:'local',condition:{id:'access',check:'access'},aggregate_status:'passed',status:'passed',planned:1,started:1,completed:1}]};
renderDetail();
const tracked=[$('.condition-strip'),...$$('.condition-strip *'),$('.request-evidence-row'),...$$('.request-evidence-row *')];
const before=tracked.map(node=>({node,html:node.outerHTML,writes:node.writes,moves:node.moves}));
let classWrites=0;for(const node of tracked){const original=node.setAttribute.bind(node);node.setAttribute=(name,value)=>{if(name==='class')classWrites++;original(name,value);};}
api=async path=>{assert.match(path,/\/events\?after=7$/);return {seq:7,state:'running',events:[],records:[],checks:[],idle:true};};
for(let poll=0;poll<3;poll++)await pollDetail(state.routeVersion);
for(const item of before){assert(item.node.isConnected);assert.equal(item.node.outerHTML,item.html);assert.equal(item.node.writes,item.writes);assert.equal(item.node.moves,item.moves);}
assert.equal(classWrites,0);assert.equal($('.request-evidence-row'),before.find(item=>item.node.matches('.request-evidence-row')).node);
})()
""", dom=True)
