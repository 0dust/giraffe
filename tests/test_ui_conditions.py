"""Shared condition skin, bounded dialog pages and stable live evidence."""

from pathlib import Path

from tests.test_ux_results import run_render_checks


DIALOG_SETUP = r"""
state.detail={id:'condition-run',state:'running',config:{targets:[{name:'local'}]}};
if(!$('#test-dialog')){const dialog=document.createElement('dialog');dialog.setAttribute('id','test-dialog');document.body.insertBefore(dialog,null);}
const condition={id:'bucket_c1000_short_short',label:'Short input / short output · 1,000 at once',check:'buckets',parameters:{concurrency:1000}};
let aggregate={target:'local',condition,condition_id:condition.id,check_id:'buckets',name:condition.label,concurrency_description:'1,000 at once',aggregate_status:'running',status:'running',planned:1000,planned_kind:'exact',started:1000,running:1000,completed:0,failed:0,not_started:0};
let remaining=1000,empty=false,calls=[];
api=async path=>{
 calls.push(path);const query=Object.fromEntries(path.split('?')[1].split('&').map(part=>part.split('=').map(decodeURIComponent)));
 const size=Number(query.page_size),total=empty?0:remaining,pages=Math.max(1,Math.ceil(total/size)),page=Math.min(Number(query.page),pages),start=(page-1)*size;
 return {aggregate,condition_id:condition.id,target:'local',page,page_size:size,pages,total,range_start:total?start+1:0,range_end:Math.min(start+size,total),number_width:4,totals:{running:remaining,passed:0,failed:0,timed_out:0,cancelled:0,unscored:0,not_started:0},rows:Array.from({length:Math.min(size,Math.max(0,total-start))},(_,i)=>({number:start+i+1,request_id:'r'+(start+i+1),status:'running',result_text:'Awaiting response'}))};
};
const draw=()=>patchHTML($('#main'),renderTests([],{live:true,conditions:[aggregate]}));
const settle=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve();};
const click=name=>handleConditionClick({target:$('[data-condition-action="'+name+'"]')});
draw();
"""


def test_shared_skin_has_one_glyph_only_cell_at_32_and_1000_concurrency():
    run_render_checks(r"""
for(const count of [32,1000])for(const status of ['waiting','running','passed']){
 state.detail={id:count+'-'+status,state:'running'};
 const condition={id:'cell',label:'Short input / short output',check:'buckets',parameters:{concurrency:count}};
 const html=renderTests([],{live:true,conditions:[{target:'local',condition,aggregate_status:status,status,planned:count,started:status==='waiting'?0:count,running:status==='running'?count:0,completed:status==='passed'?count:0,failed:0}]});
 assert.equal((html.match(/class="condition-mosaic"/g)||[]).length,1);
 assert.equal((html.match(/class="condition-spot request-spot /g)||[]).length,1);
 assert.equal((html.match(/class="spot-glyph"/g)||[]).length,1);
 const cell=html.match(/<g class="condition-spot[\s\S]*?<\/g>/)[0];
 assert.doesNotMatch(cell.match(/<text[\s\S]*?<\/text>/)[0],/>[^<]*\d/);
 assert.doesNotMatch(html,/test-condition|Inspect test|condition-requests|data-live-slot/);
 assert.match(html,/1 test condition/);assert.match(html,/mosaic-legend/);
}
""")


def test_shared_geometry_identity_and_terminal_animation_survive_live_updates_and_extension():
    run_render_checks(r"""
state.detail={id:'skin',state:'running'};
const summary=(id,status)=>({target:'local',condition:{id,label:id,check:'buckets'},aggregate_status:status,status,planned:32,started:32,running:status==='running'?32:0,completed:status==='running'?0:32,failed:0});
let conditions=[summary('first','passed'),summary('second','running')];
const draw=()=>patchHTML($('#main'),renderTests([],{live:true,conditions}),{landing:true});
draw();const first=$$('.condition-spot')[0],running=$$('.condition-spot')[1],polygon=$('polygon',running),points=polygon.getAttribute('points');
first.focus({preventScroll:true});draw();assert.equal($$('.condition-spot')[0],first);assert.equal(document.activeElement,first);assert.equal($('polygon',running),polygon);
conditions.push(summary('new','waiting'));draw();assert.equal($('polygon',running).getAttribute('points'),points);
conditions[1]=summary('second','passed');draw();assert(!running.classList.contains('running'));const animations=running.animationCalls.length;draw();assert.equal(running.animationCalls.length,animations);
assert.equal($$('.condition-spot.running').length,0);assert.equal($$('.condition-spot').length,3);
assert.equal($$('polygon',first).length,1);
""", dom=True)


def test_css_pulses_only_active_cells_and_respects_reduced_motion():
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    assert '.request-spot:is(.running, .stopping) polygon { animation: spot-live' in css
    assert '@media(prefers-reduced-motion:reduce) { .condition-spot polygon { animation:none!important; } }' in css
    assert '.condition-spot.pass polygon { animation:' not in css
    assert '.test-condition {' not in css


def test_aggregate_progress_never_scans_request_evidence_and_counts_stay_whole_condition():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 const requests=new Proxy([],{get(){throw Error('must use aggregate without scanning requests');}});
 renderTests(requests,{live:true,conditions:[aggregate]});
 openConditionDialog([...conditionViews.keys()][0]);await settle();
 const dialog=$('#test-dialog');assert(dialog.open);assert.match(dialog.textContent,/RUNNING/);
 for(const label of ['Planned','Started','Running','Completed','Failed / timed out','Not started'])assert(dialog.textContent.includes(label));
 assert.equal($$('tbody tr',dialog).length,10);assert.match(dialog.textContent,/1–10 of 1,000 requests/);
 assert.match(dialog.textContent,/1,000/);assert.match(calls.at(-1),/status=all&page=1&page_size=10/);
 assert.match(dialog.textContent,/Back to tests/);
})()
""", dom=True)


def test_dialog_pagination_jump_sizes_filters_empty_state_and_validation():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 openConditionDialog([...conditionViews.keys()][0]);await settle();
 click('last');await settle();assert.equal(conditionDialog.page,100);assert.match($('#test-dialog').textContent,/991–1,000 of 1,000 requests/);assert.equal($$('tbody tr').length,10);
 click('prev');await settle();assert.equal(conditionDialog.page,99);
 click('first');await settle();assert.equal(conditionDialog.page,1);
 const jump=$('#condition-page-number');jump.value='100';click('go');await settle();assert.equal(conditionDialog.page,100);
 jump.value='101';click('go');assert.match($('#condition-page-error').textContent,/Enter a page from 1 to 100/);assert.equal(conditionDialog.page,100);
 for(const size of [25,50,10]){jump.value='2';click('go');await settle();const select=$('#condition-page-size');select.value=String(size);handleConditionChange({target:select});await settle();assert.equal(conditionDialog.page,1);assert.equal(jump.value,'1','row-count reset updates the visible page');assert.equal($$('tbody tr').length,size);assert.match(calls.at(-1),new RegExp('page_size='+size));click('go');await settle();assert.equal(conditionDialog.validation,'');assert.equal(conditionDialog.page,1);}
 jump.value='2';click('go');await settle();empty=true;const filter=$('#condition-filter');filter.value='failed';handleConditionChange({target:filter});await settle();
 assert.equal(conditionDialog.filter,'failed');assert.equal(conditionDialog.page,1);assert.equal(jump.value,'1','empty filter reset updates the visible page');click('go');await settle();assert.equal(conditionDialog.validation,'');assert.match($('#test-dialog').textContent,/No requests match this status/);assert.match($('#test-dialog').textContent,/0–0 of 0 requests/);assert.equal($$('[data-condition-request]').length,0);
 assert.match($('#test-dialog').textContent,/1,000/,'whole-condition counts survive empty filter');
 jump.value='2';empty=false;filter.value='all';handleConditionChange({target:filter});await settle();assert.equal(jump.value,'1','restoring all statuses resets a dirty page');assert.equal(conditionDialog.page,1);assert.equal($$('tbody tr').length,10);
})()
""", dom=True)


def test_dialog_live_updates_preserve_filter_size_focus_and_clamp_shrinking_page():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 openConditionDialog([...conditionViews.keys()][0]);await settle();
 $('#condition-page-size').value='25';handleConditionChange({target:$('#condition-page-size')});await settle();
 $('#condition-filter').value='running';handleConditionChange({target:$('#condition-filter')});await settle();click('last');await settle();assert.equal(conditionDialog.page,40);
 const focused=$('#condition-page-number');focused.value='39';focused.focus({preventScroll:true});
 let release;const normal=api;api=path=>new Promise(resolve=>release=async()=>resolve(await normal(path)));
 const pending=refreshConditionDialog({force:true});draw();await refreshConditionDialog();assert.equal(focused.value,'39','pending live refresh preserves an unsubmitted edit');release();await pending;await settle();api=normal;assert.equal(focused.value,'39');assert.equal(conditionDialog.page,40);assert.equal(document.activeElement,focused);
 remaining=27;aggregate={...aggregate,running:27,completed:973};draw();await refreshConditionDialog({force:true});
 assert.equal(conditionDialog.page,2);assert.equal(conditionDialog.size,25);assert.equal(conditionDialog.filter,'running');assert.equal(document.activeElement,focused);assert.equal(focused.value,'2');assert.match($('#test-dialog').textContent,/26–27 of 27 requests/);
 assert.equal($$('tbody tr').length,2);assert.match($('#test-dialog').textContent,/973/);
 remaining=1000;await refreshConditionDialog({force:true});focused.value='40';click('go');await settle();document.body.focus({preventScroll:true});remaining=27;await refreshConditionDialog({force:true});
 assert.equal(conditionDialog.page,2);assert.equal(focused.value,'2','API clamping updates a blurred dirty page');click('go');await settle();assert.equal(conditionDialog.validation,'');assert.equal(conditionDialog.page,2);
 closeConditionDialog();assert(!$('#test-dialog').open);assert.equal(document.activeElement,$('.condition-spot'));
})()
""", dom=True)


def test_dialog_keyboard_navigation_tooltips_request_evidence_and_escape_focus_return():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 aggregate={...aggregate,condition:{...condition,id:'second'},condition_id:'second'};
 patchHTML($('#main'),renderTests([],{live:true,conditions:[{...aggregate,condition:{...condition,id:'first'}},aggregate]}));
 const cells=$$('.condition-spot');let prevented=0;const key=(target,key)=>handleConditionKeydown({target,key,preventDefault(){prevented++;}});
 cells[0].focus({preventScroll:true});key(cells[0],'ArrowRight');assert.equal(document.activeElement,cells[1]);assert.equal(cells[1].getAttribute('tabindex'),'0');assert.equal(cells[0].getAttribute('tabindex'),'-1');
 assert.match($('title',cells[1]).textContent,/Concurrent load|Input|output|1,000/);
 key(cells[1],'Enter');await settle();assert($('#test-dialog').open);
 let request=null;openRequest=id=>request=id;handleConditionClick({target:$('[data-condition-request]')});assert.equal(request,'r1');assert($('#test-dialog').open);
 key($('#condition-page-number'),'Escape');assert(!$('#test-dialog').open);assert.equal(document.activeElement,cells[1]);assert(prevented>=3);
})()
""", dom=True)


def test_saved_and_legacy_conditions_use_same_skin_without_promoting_finished_to_passed():
    run_render_checks(r"""
const conditions=[{target:'local',condition:{id:'a',label:'Explicit pass',check:'buckets'},status:'finished',planned:32,started:32,completed:32,failed:0},{target:'local',condition:{id:'b',label:'Unscored',check:'buckets'},status:'finished',planned:null,started:32,completed:32,failed:0}];
const saved=report([check('concurrency','fail',{metrics:{scenarios:{size_matrix:{id:'buckets',status:'fail',metrics:{buckets:{a:{status:'pass'}}}}}}})],{conditions});
let html=renderTests([],{report:saved});const cells=html.match(/<g class="condition-spot[\s\S]*?<\/g>/g);assert.equal(cells.length,2);assert.match(cells[0],/request-spot pass/);assert.match(cells[1],/request-spot inconclusive/);
assert.equal(conditionViews.get(JSON.stringify(['local','b'])).summary.planned,null);
const legacy=[{id:'one',target:'local',scenario:'short',status:'completed'},{id:'two',target:'local',scenario:'short',status:'timeout'},{id:'three',target:'other',scenario:'short',status:'completed'}];
html=renderTests(legacy,{report:report([],{requests:legacy})});assert.equal((html.match(/class="condition-spot /g)||[]).length,2);assert.match(html,/request-spot fail/);assert.match(html,/request-spot inconclusive/);
assert.doesNotMatch(html,/test-condition|condition-requests/);
""")


def test_partial_unknown_upper_bound_and_failed_counts_are_honest_in_dialog():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 aggregate={...aggregate,aggregate_status:'stopped',status:'stopped',planned:32,planned_kind:'upper_bound',started:24,running:0,completed:24,failed:3,not_started:8,reasons:['sending window ended']};remaining=24;draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 assert.match($('#test-dialog').textContent,/up to 32/);assert.match($('#test-dialog').textContent,/8 not sent: sending window ended/);assert.match($('#test-dialog').textContent,/STOPPED/);
 assert.match($('#test-dialog').innerHTML,/<dt>Completed<\/dt><dd>24<\/dd>/);assert.match($('#test-dialog').innerHTML,/<dt>Failed \/ timed out<\/dt><dd>3<\/dd>/);
 aggregate={...aggregate,planned:null,planned_kind:'unknown',not_started:null};draw();await refreshConditionDialog({force:true});assert.match($('#test-dialog').innerHTML,/<dt>Planned<\/dt><dd>—<\/dd>/);assert.match($('#test-dialog').innerHTML,/<dt>Not started<\/dt><dd>—<\/dd>/);
})()
""", dom=True)
def test_card_request_focus_does_not_pause_individual_request_mosaic_following():
    run_render_checks(r"""
const records=[{slot:0,status:'completed'},{slot:240,status:'running'}];
state.mosaicPage=0;state.mosaicFollow=true;state.mosaicAutoFocus=null;
document.activeElement={dataset:{liveSlot:'0'},closest:q=>q==='.condition-requests'?{}:null};
renderMosaic(records,{live:true,budget:600});
assert.equal(state.mosaicFollow,true);assert.equal(state.mosaicPage,1);
state.mosaicPage=0;state.mosaicFollow=true;
document.activeElement={dataset:{liveSlot:'0'},closest:q=>q==='.request-mosaic'?{}:null};
renderMosaic(records,{live:true,budget:600});
assert.equal(state.mosaicFollow,false);assert.equal(state.mosaicPage,0);
""")


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
  state.enteredRun=null;state.mosaicAnimated=null;state.liveRendered=null;renderDetail();
  for(const name of ['running','queued','stopping','is-live','is-landing'])assert.equal($$('.'+name).length,0);
  renderDetail();assert.equal($$('.is-entering').length,0);
}
})()
""", dom=True)


def test_cancelled_landing_and_reduced_motion_do_not_replay_terminal_animation():
    run_render_checks(r"""
(async()=>{
state.detail={id:'motion',state:'running',config:{targets:[],max_requests:1},live_requests:[{slot:0,status:'running'}]};
renderLive();$('.all-requests').open=true;listeners.toggle({target:$('.all-requests')});const spot=$('.request-mosaic [data-live-slot="0"]');
state.detail.live_requests[0].status='timeout';renderLive();assert.equal(spot.animationCalls.length,1);assert.equal($$('.is-landing').length,0);
spot.animationCalls[0].cancel();await Promise.resolve();renderLive();assert.equal(spot.getAnimations().length,0);assert.equal(spot.animationCalls.length,1);assert.equal(spot.opacity,1);
$('#main').innerHTML='';state.detail.id='reduced';state.detail.live_requests[0].status='running';window.matchMedia=()=>({matches:true});renderLive();$('.all-requests').open=true;
state.detail.live_requests[0].status='completed';renderLive();assert.equal(document.body.getAnimations({subtree:true}).length,0);
window.matchMedia=()=>({matches:false});renderLive();assert.equal(document.body.getAnimations({subtree:true}).length,0);assert.equal($$('.is-landing').length,0);
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


def test_planned_matrix_is_one_skin_with_54_waiting_cells_and_one_tab_stop():
    run_render_checks(r"""
state.detail={id:'matrix',state:'running'};
const conditions=[1,2,4,8,16,32].flatMap(concurrency=>['short','medium','long'].flatMap(input=>['short','medium','long'].map(output=>({target:'local',condition:{id:`bucket_c${concurrency}_${input}_${output}`,label:`${input} input / ${output} output`,check:'buckets',parameters:{concurrency}},aggregate_status:'waiting',status:'waiting',planned:Math.max(8,concurrency),planned_kind:'upper_bound',started:0,completed:0,running:0,failed:0}))));
const html=renderTests([],{conditions,live:true});
assert.equal((html.match(/class="condition-mosaic"/g)||[]).length,1);assert.equal((html.match(/class="condition-spot /g)||[]).length,54);assert.equal((html.match(/tabindex="0"/g)||[]).length,1);assert.match(html,/54 waiting/);assert.match(html,/54 test conditions/);
const glyphs=html.match(/<text[\s\S]*?<\/text>/g);assert.equal(glyphs.length,54);for(const glyph of glyphs){assert.match(glyph,/>○<\/text>/);assert.doesNotMatch(glyph,/>[^<]*\d/);}
""")


def test_pending_dialog_fetches_are_bounded_and_stale_pages_do_not_overwrite_filter():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 let release,pending=0,peak=0;const normal=api;
 api=async path=>{pending++;peak=Math.max(peak,pending);if(!release)await new Promise(resolve=>release=resolve);const result=await normal(path);pending--;return result;};
 openConditionDialog([...conditionViews.keys()][0]);assert.equal(pending,1);
 $('#condition-filter').value='failed';handleConditionChange({target:$('#condition-filter')});
 $('#condition-page-size').value='50';handleConditionChange({target:$('#condition-page-size')});
 await refreshConditionDialog({force:true});assert.equal(pending,1);release();for(let i=0;i<12;i++)await settle();
 assert.equal(peak,1);assert.equal(conditionDialog.filter,'failed');assert.equal(conditionDialog.size,50);assert.match(calls.at(-1),/status=failed&page=1&page_size=50/);assert.equal(conditionDialog.payload.page_size,50);
})()
""", dom=True)


def test_nonstandard_filter_remains_selected_when_live_population_reaches_zero():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 const normal=api;api=async path=>({...await normal(path),totals:{cancelled:remaining}});
 openConditionDialog([...conditionViews.keys()][0]);await settle();assert.match($('#condition-filter').textContent,/Cancelled/);
 $('#condition-filter').value='cancelled';handleConditionChange({target:$('#condition-filter')});await settle();remaining=0;empty=true;await refreshConditionDialog({force:true});
 assert.equal($('#condition-filter').value,'cancelled');assert.match($('#condition-filter').textContent,/Cancelled/);assert.match($('#test-dialog').textContent,/No requests match this status/);
})()
""", dom=True)


def test_focused_strip_returns_to_running_or_last_condition_after_hover_and_blur():
    run_render_checks(r"""
state.detail={id:'strip',state:'running',config:{targets:[]}};
const conditions=[{target:'local',condition_id:'one',check_id:'access',name:'First condition',aggregate_status:'running',started:1,running:1,completed:0},{target:'local',condition_id:'two',check_id:'access',name:'Second condition',aggregate_status:'waiting',started:0,running:0,completed:0}];
const draw=()=>patchHTML($('#main'),renderTests([],{conditions,live:true}));draw();
const first=$$('.condition-spot')[0],second=$$('.condition-spot')[1];
assert.match($('#condition-selection').textContent,/First condition/);
listeners.pointerover({target:second,clientX:10,clientY:10});assert.match($('#condition-selection').textContent,/Second condition/);
listeners.pointerout({target:second,relatedTarget:document.body});assert.match($('#condition-selection').textContent,/First condition/);
second.focus({preventScroll:true});listeners.focusin({target:second});draw();assert.match($('#condition-selection').textContent,/Second condition/);
document.body.focus({preventScroll:true});listeners.focusin({target:document.body});assert.match($('#condition-selection').textContent,/First condition/);
conditions[0].aggregate_status='passed';conditions[0].running=0;conditions[0].completed=1;conditions[1].aggregate_status='running';conditions[1].started=1;conditions[1].running=1;draw();
assert.match($('#condition-selection').textContent,/Second condition/);assert.equal($$('.condition-spot')[0],first);assert.equal($$('.condition-spot')[1],second);
conditions[1].aggregate_status='passed';conditions[1].running=0;conditions[1].completed=1;draw();assert.match($('#condition-selection').textContent,/Second condition/);
""", dom=True)
