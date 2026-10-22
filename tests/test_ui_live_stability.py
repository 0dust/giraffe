"""Live rendering must keep terminal evidence visible without replaying motion."""

from pathlib import Path

from tests.test_ux_results import run_render_checks, stylesheet_rules


def test_landings_are_visible_one_shot_web_animations_without_dom_classes():
    run_render_checks(r"""
(async()=>{
state.detail={id:'landing',state:'running',config:{targets:[],max_requests:2},live_requests:[{slot:0,id:'r',status:'running'}]};
renderDetail();const disclosure=$('.all-requests');disclosure.open=true;listeners.toggle({target:disclosure});
const spot=$('.request-mosaic [data-live-slot="0"]');
state.detail.live_requests[0]={slot:0,id:'r',status:'completed',valid:true,score:true};renderDetail();
assert.equal($$('.is-landing').length,0);assert.equal(spot.animationCalls.length,1);
assert.deepEqual(spot.animationCalls[0].keyframes,[{opacity:0},{opacity:1}]);assert.equal(spot.animationCalls[0].options.duration,400);assert.equal(spot.animationCalls[0].options.fill,'none');
const first=spot.animationCalls[0];renderDetail();assert.equal(spot.animationCalls.length,1);assert.equal(spot.getAnimations()[0],first);
first.finish();await Promise.resolve();renderDetail();
assert.equal(spot.getAnimations().length,0);assert.equal(spot.opacity,1);assert.equal(spot.animationCalls.length,1);assert.equal($$('.is-landing').length,0);
assert(spot.equalChecks>0,'exercise unchanged-subtree fast path');
for(const terminal of $$('.request-spot'))if(!inProgress(terminal.dataset.state)&&terminal.dataset.state!=='idle'){assert.equal(terminal.getAnimations().length,0);assert.equal(terminal.opacity,1);assert(!terminal.classList.contains('is-landing'));}
})()
""", dom=True)


def test_closed_disclosures_consume_terminal_landings_and_never_replay_on_open():
    run_render_checks(r"""
state.detail={id:'closed',state:'running',config:{targets:[],max_requests:2},live_requests:[{slot:0,id:'r',scenario:'warm',status:'running'}]};renderDetail();
state.detail.live_requests[0].status='timeout';renderDetail();const consumed=landedItems.size;assert(consumed>=1);assert.equal($$('.condition-spot').length,1);assert.equal($('.condition-requests'),null);
for(const details of [$('.all-requests')]){details.open=true;listeners.toggle({target:details});}
renderDetail();
for(const spot of $$('.request-spot')){assert.equal(spot.animationCalls.length,0);assert.equal(spot.getAnimations().length,0);assert.equal(spot.opacity,1);assert(!spot.classList.contains('is-landing'));}
assert.equal(landedItems.size,consumed);
""", dom=True)


def test_reserved_budget_is_never_terminal_or_pulsing_before_dispatch():
    run_render_checks(r"""
state.detail={id:'reserved',state:'running',config:{targets:[],max_requests:4},progress:{budget_used:3,attempted:0}};renderDetail();
const reserved=$('.request-mosaic [data-live-key="slot:0"]');assert(reserved.classList.contains('reserved'));assert.match(reserved.getAttribute('aria-label'),/Reserved/);assert.equal(reserved.animationCalls.length,0);assert.equal(landedItems.size,0);
state.detail.live_requests=[{slot:0,id:'r',status:'running'}];renderDetail();assert.equal($('.request-mosaic [data-live-slot="0"]'),reserved);assert(reserved.classList.contains('running'));assert.equal(reserved.animationCalls.length,0);assert.equal(landedItems.size,0);
state.detail.live_requests[0].status='completed';renderDetail();assert.equal(reserved.animationCalls.length,0,'closed disclosure consumes rather than animates');assert(landedItems.size>0);
state.detail.state='completed';assert.equal(displayStatus('reserved'),'inconclusive');state.detail.state='cancelled';assert.equal(displayStatus('reserved'),'cancelled');
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


def test_unchanged_polls_never_move_nodes_or_rewrite_classes_even_during_landing():
    run_render_checks(r"""
state.detail={id:'no-moves',state:'running',config:{targets:[],max_requests:3},live_requests:[{slot:0,id:'done',status:'completed',valid:true,score:true},{slot:1,id:'r',status:'running'}]};renderDetail();
$('.all-requests').open=true;listeners.toggle({target:$('.all-requests')});
const nodesBefore=$$('*'),classes=nodesBefore.map(node=>node.getAttribute('class'));for(const node of nodesBefore)node.moves=0;
const focused=$('.request-mosaic [data-live-slot="0"]');focused.focus({preventScroll:true});
for(let i=0;i<10;i++)renderDetail();
assert.deepEqual($$('*'),nodesBefore);assert.deepEqual(nodesBefore.map(node=>node.getAttribute('class')),classes);assert(nodesBefore.every(node=>node.moves===0));assert.equal(document.activeElement,focused);
state.detail.live_requests[1].status='timeout';renderDetail();const pending=$('.request-mosaic [data-live-slot="1"]');const animation=pending.animationCalls[0];
for(let i=0;i<10;i++)renderDetail();assert.equal(pending.animationCalls.length,1);assert.equal(pending.getAnimations()[0],animation);assert.equal(pending.moves,0);assert.equal($$('.is-landing').length,0);
""", dom=True)


def test_tooltip_refreshes_for_retained_keyboard_and_pointer_targets():
    run_render_checks(r"""
for(const mode of ['keyboard','pointer']){
  state.detail={id:mode,state:'running',config:{targets:[],max_requests:2},live_requests:[{slot:0,id:'r',status:'running'}]};$('#main').innerHTML='';renderDetail();$('.all-requests').open=true;
  const spot=$('.request-mosaic [data-live-slot="0"]');
  if(mode==='keyboard'){spot.focus({preventScroll:true});listeners.focusin({target:spot});}else listeners.pointerover({target:spot.querySelector('polygon'),clientX:10,clientY:10});
  assert.match($('#spot-tip').textContent,/Running/);assert(!$('#spot-tip').hidden);
  state.detail.live_requests[0].status='timeout';renderDetail();assert.equal($('.request-mosaic [data-live-slot="0"]'),spot);assert.match($('#spot-tip').textContent,/Timed out/);assert.doesNotMatch($('#spot-tip').textContent,/Running|pending/i);
  spot.parentNode.removeChild(spot);refreshSpotTip();assert($('#spot-tip').hidden);
}
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


def test_terminal_slot_guard_and_landing_ids_survive_structure_changes_and_slot_reuse():
    run_render_checks(r"""
(async()=>{
state.detail={id:'guard',state:'running',config:{targets:[],max_requests:2},live_requests:[{slot:0,id:'first',scenario:'warm',status:'running'}]};renderDetail();$('.all-requests').open=true;listeners.toggle({target:$('.all-requests')});
const spot=$('.request-mosaic [data-live-slot="0"]');
state.detail.live_requests[0]={...state.detail.live_requests[0],status:'completed',valid:true,score:true};renderDetail();assert.equal(spot.animationCalls.length,1);
const ids=[...landedItems];assert(ids.every(id=>!/#text|:0\/|:1\//.test(id)));
spot.animationCalls[0].finish();await Promise.resolve();
state.detail.events=[{event:'run_stopping',abort_reason:'New notice before the cards'}];state.detail.live_requests[0].status='running';renderDetail();
assert.equal($('.request-mosaic [data-live-slot="0"]'),spot);assert(spot.classList.contains('pass'));assert.equal(warnings.length,1);assert.match(warnings[0][0],/regressed/);assert.equal(spot.animationCalls.length,1);assert.deepEqual([...landedItems],ids);
state.detail.events=[];state.detail.live_requests[0]={slot:0,id:'second',scenario:'warm',status:'running'};renderDetail();assert(spot.classList.contains('running'));
state.detail.live_requests[0].status='timeout';renderDetail();assert(spot.classList.contains('timeout'));assert.equal(spot.animationCalls.length,2);assert(landedItems.size>ids.length);
state.detail.live_requests[0]={slot:0,id:'third',scenario:'warm',status:'running'};renderDetail();assert.equal(spot.getAnimations().length,0,'slot reuse cancels motion for the former request');
state.detail.id='different-run';state.detail.live_requests[0].status='running';renderDetail();assert(spot.classList.contains('running'));assert.equal(landedItems.size,0);
})()
""", dom=True)


def test_terminal_timing_neutral_condition_finish_and_visible_sidebar_glyphs():
    run_render_checks(r"""
state.detail={id:'saved-labels',state:'completed'};
for(const status of ['completed','failed','timeout','cancelled','skipped','queued','reserved','running']){
  const record={id:status,status};assert.match(requestLabel(record,[record]),/No timing recorded/);assert.doesNotMatch(requestLabel(record,[record]),/pending/i);assert.doesNotMatch(renderRequestContext(record,[record]),/Timing pending/);
}
const html=renderTests([{id:'r',scenario:'warm',status:'failed'}],{live:false});assert.match(html,/✕ Failed/);assert.doesNotMatch(html,/✓.*Finished|✓.*Stopped early|condition-card/);
state.runs=['completed','failed','cancelled','interrupted','error'].map(state=>({id:state,state,targets:[{model:'m'}]}));renderRailRuns();
for(const run of state.runs){const row=$(`[data-live-key="${run.id}"]`);assert.equal($('.status-spot',row).textContent,statusIcons[runOutcome(run)]);}
assert.notEqual(statusIcons.queued,statusIcons.running);assert.equal(statusIcons.reserved,'○');assert.equal(statusLabels.reserved,'Reserved');
""", dom=True)


def test_hidden_disconnected_and_idle_items_never_land_later():
    run_render_checks(r"""
state.detail={id:'visibility',state:'running'};
const detached=document.createElement('div');detached.innerHTML='<span data-live-key="detached" data-state="running">Running</span>';
patchHTML(detached,'<span data-live-key="detached" data-state="completed">Finished</span>',{landing:true});const hidden=detached.firstChild;assert.equal(hidden.animationCalls.length,0);const count=landedItems.size;assert.equal(count,1);
$('#main').insertBefore(detached,null);patchHTML(detached,'<span data-live-key="detached" data-state="completed">Finished</span>',{landing:true});assert.equal(hidden.animationCalls.length,0);assert.equal(landedItems.size,count);
const parent=$('#main');parent.innerHTML='<span data-live-key="idle" data-state="idle">Idle</span><span data-live-key="hidden" data-state="running">Running</span>';
const invisible=$('[data-live-key="hidden"]');invisible.visible=false;
patchHTML(parent,'<span data-live-key="idle" data-state="completed">Finished</span><span data-live-key="hidden" data-state="completed">Finished</span>',{landing:true});
assert.equal($('[data-live-key="idle"]').animationCalls.length,0);assert.equal(invisible.animationCalls.length,0);invisible.visible=true;
patchHTML(parent,'<span data-live-key="idle" data-state="completed">Finished</span><span data-live-key="hidden" data-state="completed">Finished</span>',{landing:true});assert.equal(invisible.animationCalls.length,0);
""", dom=True)


def test_reduced_motion_cancels_pending_waapi_and_never_replays_consumed_landings():
    run_render_checks(r"""
state.detail={id:'reduced-pending',state:'running',config:{targets:[],max_requests:2},live_requests:[{slot:0,id:'r',status:'running'}]};renderDetail();$('.all-requests').open=true;
state.detail.live_requests[0].status='timeout';renderDetail();const spot=$('.request-mosaic [data-live-slot="0"]');assert.equal(spot.getAnimations().length,1);
window.matchMedia=()=>({matches:true});renderDetail();assert.equal(spot.getAnimations().length,0);assert.equal(spot.opacity,1);
window.matchMedia=()=>({matches:false});renderDetail();assert.equal(spot.animationCalls.length,1);assert.equal(spot.getAnimations().length,0);
""", dom=True)


def test_terminal_palette_and_static_queued_spots_match_their_meaning():
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    rules = list(stylesheet_rules(css))
    by_selector = {selector: declarations for selector, declarations, _ in rules}
    assert by_selector['.request-spot.timeout']['--spot'] == 'var(--fail)'
    assert by_selector['.legend-dot.timeout']['--spot'] == 'var(--fail)'
    assert by_selector[':is(.status, .status-spot).timeout']['--s'] == 'var(--fail)'
    assert by_selector['.request-spot.cancelled']['--spot'] == 'var(--skip)'
    assert by_selector['.legend-dot.cancelled']['--spot'] == 'var(--skip)'
    assert any('.cancelled' in selector and declarations.get('--s') == 'var(--skip)'
               for selector, declarations, _ in rules)
    assert 'stroke-dasharray' in by_selector['.request-spot.cancelled polygon']
    assert by_selector['.request-spot']['opacity'] == '1'
    assert by_selector['.request-spot polygon']['opacity'] == '1'
    assert by_selector['.rail-recent .status-spot']['transform'] == 'none'
    for selector, declarations, _ in rules:
        if 'infinite' in declarations.get('animation', ''):
            assert '.queued' not in selector and '.reserved' not in selector


def test_closing_then_reopening_a_disclosure_cancels_an_unfinished_landing():
    run_render_checks(r"""
state.detail={id:'close-mid-landing',state:'running',config:{targets:[],max_requests:1},live_requests:[{slot:0,id:'r',status:'running'}]};renderDetail();
const details=$('.all-requests');details.open=true;listeners.toggle({target:details});
state.detail.live_requests[0].status='timeout';renderDetail();const spot=$('.request-mosaic [data-live-slot="0"]');assert.equal(spot.getAnimations().length,1);
details.open=false;listeners.toggle({target:details});assert.equal(spot.getAnimations().length,0);
details.open=true;listeners.toggle({target:details});renderDetail();assert.equal(spot.getAnimations().length,0);assert.equal(spot.animationCalls.length,1);assert.equal(spot.opacity,1);
""", dom=True)


def test_dropped_offers_are_not_pending_reservations_after_traffic_ends():
    run_render_checks(r"""
state.detail={id:'drops',state:'running',config:{targets:[],max_requests:8},progress:{budget_used:6,attempted:2,dropped_offers:3},live_requests:[{slot:0,id:'a',status:'completed'},{slot:1,id:'b',status:'completed'}]};renderDetail();
assert.equal($$('.request-spot.skipped').length,3);assert.equal($$('.request-spot.reserved').length,1);
for(const spot of $$('.request-spot.skipped')){assert.match(spot.getAttribute('aria-label'),/Not run.*dropped before sending/);assert.equal(spot.getAnimations().length,0);}
// A new reservation must not turn an existing dropped marker back into Reserved.
const dropped=$$('.request-spot.skipped');state.detail.progress.budget_used=7;renderDetail();
assert(dropped.every(spot=>spot.classList.contains('skipped')));assert.equal($$('.request-spot.reserved').length,2);
state.detail.progress={budget_used:7,attempted:4,dropped_offers:3,completed:4,phase:'judging',event:'judging_started'};state.detail.live_requests.push({slot:2,id:'c',status:'completed'},{slot:3,id:'d',status:'completed'});renderDetail();
assert.equal($$('.request-spot.skipped').length,3);assert.equal($$('.request-spot.reserved').length,0);assert.match($('#main').textContent,/3 dropped before sending/);
// Capped evidence for dispatched requests must not look like pending reservations.
state.detail.live_requests=[{slot:3,id:'d',status:'completed'}];renderDetail();assert.equal($$('.request-mosaic .request-spot.reserved').length,0);assert.equal($$('.request-mosaic .request-spot.skipped').length,3);
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


def test_budget_free_live_window_keeps_keyed_terminal_states_without_filler():
    run_render_checks(r"""
state.detail={id:'no-budget',state:'running',config:{targets:[],max_requests:null,concurrency:null,max_duration_seconds:null},
 progress:{attempted:503,budget_used:506,dropped_offers:3},
 live_request_summary:{evicted:500,statuses:{completed:500}},
 live_requests:[{slot:500,id:'timeout',status:'timeout'},{slot:501,id:'pending',status:'running'},{slot:502,id:'cancelled',status:'cancelled'}]};
renderDetail();const disclosure=$('.all-requests');disclosure.open=true;listeners.toggle({target:disclosure});
const mosaic=$('.request-mosaic'),pending=$('[data-live-slot="501"]',mosaic);
assert.equal($$('.request-spot',mosaic).length,3);assert.equal($('[tabindex="0"]',mosaic).dataset.liveSlot,'500');
assert.equal($('[data-live-slot="500"]',mosaic).dataset.state,'timeout');assert.equal($('[data-live-slot="502"]',mosaic).dataset.state,'cancelled');
assert.equal($('[data-live-slot="500"] .spot-glyph',mosaic).textContent,statusIcons.timeout);
assert.match($('#main').textContent,/503 started · 3 in live window/);assert.match($('#main').textContent,/3 dropped before sending/);
assert.match($('#main').textContent,/500 earlier requests outside the live window/);assert.match($('#main').textContent,/No request limit set/);
assert.equal($('.budget-track'),null);assert.equal($('.request-spot.idle, .request-spot.reserved, .request-spot.skipped',mosaic),null);
assert.doesNotMatch($('.mosaic-legend').textContent,/Unused|Reserved/);
state.detail.live_requests[1]={slot:501,id:'pending',status:'completed',valid:true,score:true};renderDetail();
assert.equal($('.request-mosaic'),mosaic);assert.equal($('[data-live-slot="501"]',mosaic),pending);assert.equal(pending.dataset.state,'pass');assert.equal(pending.animationCalls.length,1);
renderDetail();assert.equal(pending.animationCalls.length,1);assert(disclosure.open);
state.mosaicFollow=false;state.mosaicPage=0;renderDetail();
assert.match($('.all-requests').textContent,/Earlier request evidence was evicted from the live window/);assert.equal($('.request-mosaic'),null);
""", dom=True)
