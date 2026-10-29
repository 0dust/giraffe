"""Shared condition skin, bounded dialog pages and stable live evidence."""


from tests.test_ux_results import run_render_checks


def test_condition_rows_show_counts_and_canonical_names_at_large_concurrency():
    run_render_checks(r"""
for(const count of [32,1000])for(const status of ['waiting','running','passed']){
 state.detail={id:count+'-'+status,state:'running'};
 const condition={id:'json_c'+count,label:'Saved obsolete title',check:'json',parameters:{concurrency:count}};
 const html=renderTests([],{live:true,conditions:[{target:'local',condition,aggregate_status:status,status,planned:count,started:status==='waiting'?0:count,running:status==='running'?count:0,completed:status==='passed'?count:0}]});
 assert.equal((html.match(/class="condition-strip-row"/g)||[]).length,1);assert.match(html,/JSON correctness/);assert.match(html,new RegExp('/'+number(count)+' requests'));
 assert.match(html,/p95 —/);assert.doesNotMatch(html,/condition-mosaic|request-spot|polygon|idle/);
}
""")


def test_condition_rows_open_request_pages_and_return_focus_on_close():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 const row=$('.condition-strip-row');row.focus({preventScroll:true});
 await listeners.click({target:row});await settle();assert($('#test-dialog').open);assert.match(calls.at(-1),/conditions\/.+\/requests/);
 closeConditionDialog();assert.equal(document.activeElement,row);
})()
""", dom=True)


def test_saved_and_legacy_condition_rows_do_not_promote_finished_to_passed():
    run_render_checks(r"""
const conditions=[{target:'local',condition:{id:'a',label:'Explicit pass',check:'load'},status:'finished',planned:32,started:32,completed:32},{target:'local',condition:{id:'b',label:'Unscored',check:'load'},status:'finished',planned:null,started:32,completed:32}];
const saved=report([check('load','fail',{metrics:{points:[{id:'a',status:'pass'}]}})],{conditions});
$('#main').innerHTML=renderTests([],{report:saved});const rows=$$('.condition-strip-row');assert.equal(rows.length,2);assert.match(rows[0].textContent,/Passed/);assert.match(rows[1].textContent,/Inconclusive/);
assert.equal(conditionViews.get(JSON.stringify(['local','b'])).summary.planned,null);
const legacy=[{id:'one',target:'local',scenario:'short',status:'completed'},{id:'two',target:'local',scenario:'short',status:'timeout'},{id:'three',target:'other',scenario:'short',status:'completed'}];
$('#main').innerHTML=renderTests(legacy,{report:report([],{requests:legacy})});assert.equal($$('.condition-strip-row').length,2);assert.match($('.condition-strip').textContent,/Failed/);assert.match($('.condition-strip').textContent,/Unscored/);assert.doesNotMatch($('.condition-strip').textContent,/Not run|\/\? requests/);
""", dom=True)


DIALOG_SETUP = r"""
state.detail={id:'condition-run',state:'running',config:{targets:[{name:'local'}]}};
if(!$('#test-dialog')){const dialog=document.createElement('dialog');dialog.setAttribute('id','test-dialog');document.body.insertBefore(dialog,null);}
const condition={id:'bucket_c1000_short_short',label:'Short input / short output · 1,000 at once',check:'buckets',parameters:{concurrency:1000}};
let aggregate={target:'local',condition,condition_id:condition.id,check_id:'buckets',name:condition.label,concurrency_description:'1,000 at once',aggregate_status:'running',status:'running',planned:1000,planned_kind:'exact',started:1000,running:1000,completed:0,failed:0,not_started:0};
let remaining=1000,empty=false,calls=[];
state.detail.live_requests=Array.from({length:1000},(_,index)=>({id:'r'+(index+1),slot:index,target:'local',scenario:condition.id,condition_id:condition.id,status:'running'}));
const remoteEvidence=()=>{state.detail.state='completed';state.detail.report={requests_compact:true,requests:[]};aggregate={...aggregate,aggregate_status:'passed',status:'passed',running:0,completed:1000};};
api=async path=>{
 calls.push(path);const query=Object.fromEntries(path.split('?')[1].split('&').map(part=>part.split('=').map(decodeURIComponent)));
 const size=Number(query.page_size);assert(size<=50,'existing API caps pages at 50');const total=empty?0:remaining,pages=Math.max(1,Math.ceil(total/size)),page=Math.min(Number(query.page),pages),start=(page-1)*size;
 return {aggregate,condition_id:condition.id,target:'local',page,page_size:size,pages,total,range_start:total?start+1:0,range_end:Math.min(start+size,total),number_width:4,totals:{running:activeState(state.detail)?remaining:0,passed:activeState(state.detail)?0:remaining,failed:0,timed_out:0,cancelled:0,unscored:0,not_started:0},rows:Array.from({length:Math.min(size,Math.max(0,total-start))},(_,i)=>({number:start+i+1,request_id:'r'+(start+i+1),status:query.status==='all'?(activeState(state.detail)?'running':'passed'):query.status,result_text:'Awaiting response'}))};
};
const draw=()=>patchHTML($('#main'),renderTests([],{live:true,conditions:[aggregate]}));
const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const click=name=>handleConditionClick({target:$('[data-condition-action="'+name+'"]')});
const pageTo=async value=>{while(conditionDialog.page!==value){const next=conditionDialog.page+(conditionDialog.page<value?1:-1);const button=$('#test-dialog [data-evidence-page="'+next+'"]');assert(button,'page '+next+' has a visible action');await listeners.click({target:button});await settle();}};
const statusFilter=()=>$('#test-dialog [data-evidence-filter="status"]');
const setStatus=async value=>{const input=statusFilter();input.value=value;listeners.change({target:input});await settle();};
draw();
"""


def test_aggregate_progress_remains_authoritative_over_retained_request_counts():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 const requests=[{id:'partial',target:'local',condition_id:condition.id,scenario:condition.id,status:'completed',elapsed_ms:12}];
 state.detail.live_requests=requests;renderTests(requests,{live:true,conditions:[aggregate]});
 openConditionDialog([...conditionViews.keys()][0]);await settle();
 const dialog=$('#test-dialog');assert(dialog.open);assert.match(dialog.textContent,/Running/);
 for(const label of ['Planned','Started','Running','Completed','Failed / timed out','Not started'])assert(dialog.textContent.includes(label));
 assert.equal($$('.request-evidence-row',dialog).length,200);assert.match(dialog.textContent,/200 of 1,000 requests/);assert.equal($('.request-evidence-row',dialog).dataset.request,'r1');
 assert.equal(conditionDialog.localEvidence,false);assert.match(dialog.textContent,/Page 1 of 5/);
 assert.match(dialog.textContent,/1,000/);assert.match(calls.at(-1),/status=all&page=4&page_size=50/);
 assert.match(dialog.textContent,/Back to conditions/);
})()
""", dom=True)


def test_dialog_shared_pagination_filters_empty_state_and_bounds():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 remoteEvidence();draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 assert.equal(conditionDialog.size,200);assert.equal($('tbody tr').dataset.request,'r1');
 assert.notEqual($('#test-dialog [data-evidence-page="0"]').getAttribute('disabled'),null);
 await pageTo(5);assert.equal(conditionDialog.page,5);assert.match($('#test-dialog').textContent,/801–1,000 · 200 of 1,000 requests/);assert.equal($$('tbody tr').length,200);
 assert.notEqual($('#test-dialog [data-evidence-page="6"]').getAttribute('disabled'),null);
 await pageTo(4);assert.equal(conditionDialog.page,4);await pageTo(1);assert.equal(conditionDialog.page,1);
 assert.equal($('#condition-page-number'),null);assert.equal($('#condition-page-size'),null);
 await pageTo(2);empty=true;await setStatus('fail');
 assert.equal(conditionDialog.filter,'failed');assert.equal(conditionDialog.page,1);assert.equal(conditionDialog.table.page,1);
 assert.match($('#test-dialog').textContent,/No requests match these filters/);assert.match($('#test-dialog').textContent,/0 of 0 requests/);assert.equal($$('.request-evidence-row').length,0);
 assert.match($('#test-dialog').textContent,/1,000/,'whole-condition counts survive empty filter');
 empty=false;await setStatus('all');assert.equal(conditionDialog.page,1);assert.equal($$('.request-evidence-row').length,200);
 assert.match(calls.at(-1),/status=all&page=4&page_size=50/);
})()
""", dom=True)


def test_dialog_live_updates_preserve_filter_focus_scroll_and_clamp_shrinking_page():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 openConditionDialog([...conditionViews.keys()][0]);await settle();await setStatus('running');await pageTo(5);
 const focused=$('#test-dialog [data-evidence-search]');focused.value='not submitted';focused.focus({preventScroll:true});
 const scroll=$('#test-dialog .request-table-scroll');scroll.scrollTop=80;scroll.scrollLeft=30;
 let release;const normal=api;api=path=>new Promise(resolve=>release=async()=>resolve(await normal(path)));
 const pending=refreshConditionDialog({force:true});draw();await refreshConditionDialog();assert.equal(focused.value,'not submitted');release();await pending;await settle();api=normal;
 assert.equal(focused.value,'not submitted');assert.equal(conditionDialog.page,5);assert.equal(document.activeElement,focused);
 remaining=27;state.detail.live_requests.forEach((record,index)=>{record.status=index<27?'running':'completed';record.valid=true;record.score=true;});aggregate={...aggregate,running:27,completed:973};draw();await refreshConditionDialog({force:true});
 assert.equal(conditionDialog.page,1);assert.equal(conditionDialog.table.page,1);assert.equal(conditionDialog.size,200);assert.equal(conditionDialog.filter,'running');assert.equal(document.activeElement,focused);
 assert.match($('#test-dialog').textContent,/1–27 · 27 of 27 requests/);assert.equal($$('.request-evidence-row').length,27);assert.match($('#test-dialog').textContent,/973/);
 assert.equal($('#test-dialog .request-table-scroll'),scroll);assert.equal(scroll.scrollTop,80);assert.equal(scroll.scrollLeft,30);
 closeConditionDialog();assert(!$('#test-dialog').open);assert.equal(document.activeElement,$('.condition-strip-row'));
})()
""", dom=True)


def test_partial_unknown_upper_bound_and_failed_counts_are_honest_in_dialog():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 aggregate={...aggregate,aggregate_status:'stopped',status:'stopped',planned:32,planned_kind:'upper_bound',started:24,running:0,completed:24,failed:3,not_started:8,reasons:['sending window ended']};remaining=24;draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 assert.match($('#test-dialog').textContent,/up to 32/);assert.match($('#test-dialog').innerHTML,/<dt>Not started<\/dt><dd>8<\/dd>/);assert.match($('#test-dialog').textContent,/sending window ended/);assert.match($('#test-dialog').textContent,/Stopped/);
 assert.match($('#test-dialog').innerHTML,/<dt>Completed<\/dt><dd>24<\/dd>/);assert.match($('#test-dialog').innerHTML,/<dt>Failed \/ timed out<\/dt><dd>3<\/dd>/);
 aggregate={...aggregate,planned:null,planned_kind:'unknown',not_started:null};draw();await refreshConditionDialog({force:true});assert.match($('#test-dialog').innerHTML,/<dt>Planned<\/dt><dd>—<\/dd>/);assert.match($('#test-dialog').innerHTML,/<dt>Not started<\/dt><dd>—<\/dd>/);
})()
""", dom=True)


def test_condition_events_do_not_render_in_activity_or_replace_live_phase():
    run_render_checks(r"""
state.detail={id:'events',state:'running',config:{targets:[],max_requests:32},events:[{event:'request_completed',completed:1},{event:'condition_started',condition:{id:'secret-start'}},{event:'condition_finished',condition:{id:'secret-finish'}}]};
renderLive();const html=nodes['#main'].innerHTML;
assert.match(html,/Request completed/);assert.doesNotMatch(html,/Condition started|Condition finished|secret-start|secret-finish/);
""")


def test_final_poll_and_saved_reopen_leave_no_active_or_animation_classes():
    run_render_checks(r"""
(async()=>{
const config={targets:[{name:'local',model:'test'}],max_requests:4};
for(const terminal of ['completed','failed','cancelled','interrupted','error']){
  const id='ended-'+terminal,stale={id:'stale',slot:0,target:'local',scenario:'warm',status:'running'};
  state.detail={id,state:'running',config,live_requests:[stale]};state.runs=[{id,state:'running',targets:config.targets}];renderRailRuns();renderDetail();openLiveRequest(0);
  const result=report([check('serving','running')],{requests:[stale]});
  api=async path=>path==='/api/runs'?{runs:state.runs,active_run_id:id}:path==='/api/storage'?{}:{id,state:terminal,report:result};
  await pollDetail(state.routeVersion);
  for(const name of ['running','queued','stopping','is-live','is-landing','is-entering'])assert.equal($$('.'+name).length,0,terminal+' retains '+name);
  assert.equal(document.body.getAnimations({subtree:true}).length,0);assert.equal(state.active,null);assert($('#active-run-link').hidden);assert.equal($('#active-run-link').innerHTML,'');assert.doesNotMatch(document.title,/In progress/);assert(!$('#request-dialog').open);
  renderDetail();assert.equal($$('.is-entering').length,0);
  // Fresh saved view, including malformed unfinished evidence.
  state.enteredRun=null;state.liveRendered=null;renderDetail();
  for(const name of ['running','queued','stopping','is-live','is-landing'])assert.equal($$('.'+name).length,0);
  renderDetail();assert.equal($$('.is-entering').length,0);
}
})()
""", dom=True)


def test_run_finishing_before_first_live_request_snapshot_has_no_entrance_replay():
    run_render_checks(r"""
state.detail={id:'quick',state:'running',config:{targets:[],max_requests:4}};renderDetail();
state.detail={id:'quick',state:'completed',report:report([check('serving','pass')],{requests:[{id:'r',status:'completed',valid:true,score:true}]})};renderDetail();
for(const name of ['running','queued','stopping','is-live','is-landing','is-entering'])assert.equal($$('.'+name).length,0,name);
""", dom=True)


def test_open_live_request_status_settles_while_other_requests_continue():
    run_render_checks(r"""
state.detail={id:'dialog-live',state:'running',config:{targets:[],max_requests:2},live_requests:[{slot:0,status:'running'},{slot:1,status:'running'}]};
renderDetail();openLiveRequest(0);const dialog=$('#request-dialog'),status=$('.live-request-status .status');
renderDetail();assert.equal($('.live-request-status .status'),status);
state.detail.live_requests[0].status='cancelled';renderDetail();
assert(dialog.open);assert.equal($('.live-request-status .status'),status);assert(status.classList.contains('cancelled'));assert(!status.classList.contains('running'));assert.match(status.textContent,/Cancelled/);
status.animationCalls[0].finish();renderDetail();assert.equal(status.animationCalls.length,1);assert(!status.classList.contains('is-landing'));assert.equal(status.opacity,1);
""", dom=True)


def test_queued_and_stopping_badges_keep_their_elements_between_polls():
    run_render_checks(r"""
for(const status of ['queued','stopping']){
  state.detail={id:status,state:status,config:{targets:[],max_requests:1}};renderDetail();
  const badgeNode=$('.live-state .status'),icon=$('.status-icon',badgeNode),heading=$('h1');
  renderDetail();assert.equal($('.live-state .status'),badgeNode);assert.equal($('.status-icon',badgeNode),icon);assert.equal($('h1'),heading);assert(badgeNode.classList.contains(status));assert.equal($$('.is-landing').length,0);
}
""", dom=True)


def test_pending_dialog_fetches_are_bounded_and_stale_pages_do_not_overwrite_filter():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 remoteEvidence();draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 let release,pending=0,peak=0;const normal=api;
 api=async path=>{pending++;peak=Math.max(peak,pending);if(!release)await new Promise(resolve=>release=resolve);const result=await normal(path);pending--;return result;};
 const refresh=refreshConditionDialog({force:true});assert.equal(pending,1);
 statusFilter().value='pass';listeners.change({target:statusFilter()});
 const sort=$('#test-dialog [data-evidence-sort="elapsed_ms"]');await listeners.click({target:sort});
 await refreshConditionDialog({force:true});assert.equal(pending,1);release();await refresh;for(let i=0;i<12;i++)await settle();
 assert(peak<=3,'at most one 200-row batch of API pages is active');assert.equal(conditionDialog.filter,'passed');assert.equal(conditionDialog.size,200);assert.equal(conditionDialog.table.sort,'elapsed_ms');
 assert.match(calls.at(-1),/status=passed&page=4&page_size=50/);assert.equal(conditionDialog.payload.page_size,50);assert.equal(statusFilter().value,'pass');assert(conditionDialog.payload.rows.every(row=>row.status==='passed'));
})()
""", dom=True)


def test_nonstandard_filter_remains_selected_when_live_population_reaches_zero():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 const normal=api;api=async path=>({...await normal(path),totals:{cancelled:remaining}});
 state.detail.live_requests=state.detail.live_requests.map(record=>({...record,status:'cancelled'}));
 openConditionDialog([...conditionViews.keys()][0]);await settle();assert.match(statusFilter().textContent,/Cancelled/);
 await setStatus('cancelled');remaining=0;empty=true;state.detail.live_requests=state.detail.live_requests.map(record=>({...record,status:'running'}));await refreshConditionDialog({force:true});
 assert.equal(statusFilter().value,'cancelled');assert.match(statusFilter().textContent,/Cancelled/);assert.match($('#test-dialog').textContent,/No requests match these filters/);
})()
""", dom=True)


def test_dialog_keyboard_navigation_request_evidence_and_escape_focus_return():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
aggregate={...aggregate,running:0,completed:2,planned:2,started:2,aggregate_status:'passed',status:'passed'};remaining=2;
state.detail.live_requests=state.detail.live_requests.slice(0,2).map((record,index)=>({...record,status:'completed',valid:true,score:true,elapsed_ms:index+3,evaluation:{messages:[{role:'user',content:'Question '+index}],expected:'answer'},output:'answer'}));
const second={...aggregate,condition:{id:'second',check:'json',label:'Another condition'}};
patchHTML($('#main'),renderTests(state.detail.live_requests,{live:true,conditions:[aggregate,second]}));
const conditionRows=$$('.condition-strip-row');conditionRows[0].focus({preventScroll:true});let prevented=0;
const key=(target,key)=>listeners.keydown({target,key,preventDefault(){prevented++;}});
key(conditionRows[0],'ArrowDown');assert.equal(document.activeElement,conditionRows[1]);key(conditionRows[1],'Home');assert.equal(document.activeElement,conditionRows[0]);
key(conditionRows[0],'Enter');await settle();assert($('#test-dialog').open);
const requests=$$('#test-dialog .request-evidence-row');assert.equal(requests.length,2);
assert.equal(requests[0].querySelector('[data-label="Question"] [title]').getAttribute('title'),'Question 0');
requests[0].focus({preventScroll:true});key(requests[0],'ArrowDown');assert.equal(document.activeElement,requests[1]);key(requests[1],'Enter');await settle();assert($('#request-dialog').open);
listeners.cancel({target:$('#request-dialog'),preventDefault(){prevented++;}});assert(!$('#request-dialog').open);assert($('#test-dialog').open);assert.equal(document.activeElement,requests[1]);
key($('#test-dialog'),'Escape');assert(!$('#test-dialog').open);assert.equal(document.activeElement,conditionRows[0]);assert(prevented>=6);
})()
""", dom=True)
