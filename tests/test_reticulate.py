"""Reticulate must preserve the identity and uncertainty of real evidence."""

from tests.test_ux_results import run_render_checks


def test_request_colors_do_not_promote_unscored_or_probe_requests_to_pass():
    run_render_checks(r"""
const completed={id:'one',status:'completed',valid:true,score:true};
assert.equal(requestOutcome(completed),'pass');
for(const extra of [{score:null},{score:undefined},{valid:undefined},
 {intentional:true},{scenario:'client_cancel'},{status:'cancelled'}]){
 assert.equal(requestOutcome({...completed,...extra}),'inconclusive');
}
for(const extra of [{score:false},{valid:false},{status:'failed'},{status:'timeout'}]){
 assert.equal(requestOutcome({...completed,...extra}),'fail');
}
assert.equal(requestOutcome({status:'running'}),'running');
assert.equal(requestOutcome({}),'inconclusive');
""")


def test_mosaic_is_one_spot_per_record_with_stable_paginated_identity():
    run_render_checks(r"""
const records=Array.from({length:245},(_,i)=>({id:'request-'+i,fixture_id:'case <'+i+'>',scenario:'bucket_c32_short_short',
 status:'completed',valid:true,score:i%2===0,elapsed_ms:i}));
let html=renderMosaic(records);
assert.equal((html.match(/data-request=/g)||[]).length,240);
assert.match(html,/data-request="request-0"/);assert.match(html,/data-request="request-239"/);
assert.doesNotMatch(html,/data-request="request-240"/);assert.match(html,/Short input \/ short output · 32 at once\. Request 1 of 245/);
assert.equal((html.match(/tabindex="0"/g)||[]).length,1);
state.mosaicPage=1;html=renderMosaic(records);
assert.equal((html.match(/data-request=/g)||[]).length,5);
assert.match(html,/data-request="request-244"/);assert.doesNotMatch(html,/data-request="request-0"/);
const shape=reticulateCells(240);
assert.equal(shape.cells.length,240);assert.equal(new Set(shape.cells).size,240);
assert.equal(JSON.stringify(shape),JSON.stringify(reticulateCells(240)));
for(const polygon of shape.cells)for(const point of polygon.split(' ')){
 const [x,y]=point.split(',').map(Number);assert(Number.isFinite(x)&&Number.isFinite(y));
 assert(x>=0&&x<=shape.width&&y>=0&&y<=shape.height);
}
""")


def test_live_mosaic_tracks_start_slots_when_completion_order_changes():
    run_render_checks(r"""
state.mosaicPage=0;
const records=[{slot:2,id:'fast',status:'completed',score:true,valid:true},
 {slot:0,status:'running'},{slot:1,id:'slow',status:'completed',score:null,valid:true}];
const html=renderMosaic(records,{budget:5,live:true,budgetUsed:4});
assert(html.indexOf('data-live-slot="0"')<html.indexOf('data-live-slot="1"'));
assert(html.indexOf('data-live-slot="1"')<html.indexOf('data-live-slot="2"'));
assert.equal((html.match(/class="request-spot /g)||[]).length,5);
assert.equal((html.match(/class="request-spot idle"/g)||[]).length,1);
assert.match(html,/Budget slot 4: used or reserved, request evidence unavailable/);
assert.match(html,/Budget slot 5: not used/);
assert.doesNotMatch(html,/data-request=/);
""")


def test_live_mosaic_follows_frontier_and_allows_manual_inspection():
    run_render_checks(r"""
const records=Array.from({length:254},(_,slot)=>({slot,status:slot<250?'completed':'running'}));
delete state.mosaicFollow;
let html=renderMosaic(records,{budget:600,live:true});
assert.equal(state.mosaicPage,1);assert.equal(state.mosaicLivePage,1);
assert.match(html,/data-live-slot="250"/);assert.doesNotMatch(html,/data-live-slot="0"/);
assert.match(html,/<g class="request-spot running current"[^>]*data-live-slot="253"[^>]*aria-label="Requests\. Request 254 of 254/);
assert.equal((html.match(/request-spot running current/g)||[]).length,1);
assert.match(html,/Following live/);assert.doesNotMatch(html,/data-action="mosaic-live"/);
state.mosaicFollow=false;state.mosaicPage=0;
html=renderMosaic(records,{budget:600,live:true});
assert.equal(state.mosaicPage,0);assert.equal(state.mosaicLivePage,1);
assert.match(html,/data-live-slot="0"/);assert.doesNotMatch(html,/data-live-slot="250"/);
assert.match(html,/data-action="mosaic-live">Back to live/);
assert.match(html,/Live on requests 241–480/);assert.match(html,/live on another page/);
records.push({slot:490,status:'running'});renderMosaic(records,{budget:600,live:true});
assert.equal(state.mosaicPage,0);assert.equal(state.mosaicLivePage,2);
""")


def test_live_mosaic_frontier_prefers_running_then_latest_record_then_budget():
    run_render_checks(r"""
let html=renderMosaic([{slot:500,status:'completed'},{slot:250,status:'running'}],{budget:600,live:true});
assert.equal(state.mosaicLivePage,1);assert.match(html,/Requests\. Request 1 of 2/);
html=renderMosaic([{slot:500,status:'completed'},{slot:250,status:'completed'}],{budget:600,live:true});
assert.equal(state.mosaicPage,2);assert.doesNotMatch(html,/request-spot \w+ current|Newest running request/);
renderMosaic([],{budget:600,live:true,budgetUsed:481});assert.equal(state.mosaicPage,2);
renderMosaic([],{budget:600,live:true,budgetUsed:0});assert.equal(state.mosaicPage,0);
renderMosaic([],{budget:600,live:true,budgetUsed:999});assert.equal(state.mosaicPage,2);
""")


def test_report_mosaic_ignores_follow_state_and_preserves_selected_page():
    run_render_checks(r"""
const records=Array.from({length:600},(_,i)=>({id:'request-'+i,status:'completed'}));
for(const follow of [false,undefined,true]){
 state.mosaicFollow=follow;state.mosaicPage=1;state.mosaicLivePage=2;
 const html=renderMosaic(records);
 assert.equal(state.mosaicPage,1);assert.match(html,/data-request="request-240"/);
 assert.doesNotMatch(html,/data-request="request-0"|mosaic-live|Following live|live on another page|Newest running request/);
}
""")


def test_beeswarm_excludes_missing_latency_and_preserves_each_measured_request():
    run_render_checks(r"""
const records=[{id:'zero',elapsed_ms:0},{id:'one',elapsed_ms:100},
 {id:'tie',elapsed_ms:100},{id:'missing',elapsed_ms:null},{id:'absent'}];
let html=renderBeeswarm(records);
assert.match(html,/3 measured · 2 missing/);
assert.equal((html.match(/<circle /g)||[]).length,3);
assert.match(html,/data-request="zero"/);assert.doesNotMatch(html,/data-request="missing"/);
const circles=[...html.matchAll(/<circle cx="([^"]+)" cy="([^"]+)"/g)];
assert.equal(circles[1][1],circles[2][1]);assert.notEqual(circles[1][2],circles[2][2]);
assert.match(renderBeeswarm([{id:'missing'}]),/No latency measurements/);
""")


def test_phases_require_real_boundaries_and_keep_targets_separate():
    run_render_checks(r"""
const html=renderPhases([
 {event:'scenario_started',target:'a',scenario:'warm',elapsed_seconds:1},
 {event:'scenario_started',target:'b',scenario:'warm',elapsed_seconds:2},
 {event:'request_completed',target:'b',scenario:'warm',elapsed_seconds:3},
 {event:'scenario_finished',target:'a',scenario:'warm',elapsed_seconds:5},
 {event:'scenario_finished',target:'a',scenario:'unknown',elapsed_seconds:6}]);
assert.match(html,/4s/);assert.match(html,/Running/);assert.match(html,/Start unavailable/);
assert.equal((html.match(/<li /g)||[]).length,3);
assert.match(renderPhases([]),/Waiting for scenario events/);
""")


def test_report_mosaic_orders_by_dispatch_without_mutating_saved_evidence():
    run_render_checks(r"""
const value={requests:[{id:'finished-first',dispatch_ms:20},{id:'started-first',dispatch_ms:10}]};
assert.equal(orderedRequests(value).map(r=>r.id).join(','),'started-first,finished-first');
assert.equal(value.requests[0].id,'finished-first');
""")
