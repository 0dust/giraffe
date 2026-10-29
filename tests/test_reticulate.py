"""Request evidence preserves identity, uncertainty and inspection state."""

from tests.test_ux_results import run_render_checks


def test_request_colors_do_not_promote_unscored_or_probe_requests_to_pass():
    run_render_checks(r"""
const completed={id:'one',status:'completed',valid:true,score:true};
assert.equal(requestOutcome(completed),'pass');
for(const extra of [{score:null},{score:undefined},{valid:undefined}])assert.equal(requestOutcome({...completed,...extra}),'unscored');
for(const extra of [{intentional:true},{scenario:'client_cancel'},{status:'cancelled'}]){
 assert.equal(requestOutcome({...completed,...extra}),'inconclusive');
}
for(const extra of [{score:false},{valid:false},{status:'failed'},{status:'timeout'}]){
 assert.equal(requestOutcome({...completed,...extra}),'fail');
}
assert.equal(requestOutcome({status:'running'}),'running');
assert.equal(requestOutcome({}),'inconclusive');
""")


def test_request_table_has_one_row_per_record_with_stable_paginated_identity():
    run_render_checks(r"""
const records=Array.from({length:245},(_,i)=>({id:'request-'+i,question:'case <'+i+'>',scenario:'bucket_c32_short_short',status:'completed',valid:true,score:i%2===0,elapsed_ms:i}));
const table=createRequestTableState();let html=renderRequestTable(records,table);
assert.equal((html.match(/class="request-evidence-row"/g)||[]).length,200);
assert.match(html,/data-request="request-0"/);assert.match(html,/data-request="request-199"/);
assert.doesNotMatch(html,/data-request="request-200"|request-spot|<svg/);assert.match(html,/case &lt;0&gt;/);
assert.equal((html.match(/tabindex="0"/g)||[]).length,200);
updateRequestTableState(table,'page',2);html=renderRequestTable(records,table);
assert.equal((html.match(/class="request-evidence-row"/g)||[]).length,45);
assert.match(html,/data-request="request-244"/);assert.doesNotMatch(html,/data-request="request-0"/);
assert.equal(new Set(requestTablePage(records,table).rows.map(row=>row.id)).size,45);
assert.equal(records[0].id,'request-0');
""")


def test_live_request_table_tracks_start_slots_without_unused_budget_rows():
    run_render_checks(r"""
const records=[{slot:0,status:'running'},{slot:1,id:'slow',status:'completed',score:null,valid:true},{slot:2,id:'fast',status:'completed',score:true,valid:true}];
const html=renderRequestTable(records,createRequestTableState(),{live:true});
assert(html.indexOf('data-live-slot="0"')<html.indexOf('data-live-slot="1"'));
assert(html.indexOf('data-live-slot="1"')<html.indexOf('data-live-slot="2"'));
assert.equal((html.match(/class="request-evidence-row"/g)||[]).length,3);
assert.match(html,/data-live-key="request:slot-0"/);assert.match(html,/data-request="slow"/);assert.match(html,/data-request="fast"/);
assert.doesNotMatch(html,/Budget slot|Unused|request-spot/);
""")


def test_live_request_table_preserves_manual_page_and_sort_when_rows_append():
    run_render_checks(r"""
const records=Array.from({length:254},(_,slot)=>({id:'request-'+slot,slot,status:slot<250?'completed':'running',elapsed_ms:slot}));
const table=createRequestTableState();updateRequestTableState(table,'sort','elapsed_ms');updateRequestTableState(table,'page',2);
let html=renderRequestTable(records,table,{live:true});assert.match(html,/data-live-slot="250"/);assert.doesNotMatch(html,/data-live-slot="0"/);
records.push({id:'new',slot:254,status:'running',elapsed_ms:254});html=renderRequestTable(records,table,{live:true});
assert.equal(table.page,2);assert.equal(table.sort,'elapsed_ms');assert.equal(table.direction,'asc');
assert.match(html,/data-live-slot="254"/);assert.match(html,/Page 2 of 2/);assert.doesNotMatch(html,/Following live|mosaic-live/);
""")


def test_request_table_filters_real_evidence_and_preserves_status_separation():
    run_render_checks(r"""
const records=[{id:'completed',status:'completed',valid:true,score:true},{id:'running',status:'running'},{id:'unscored',status:'completed',valid:true},{id:'failed',status:'failed',error:'HTTP 500'}],table=createRequestTableState();
updateRequestTableState(table,'status','running');let page=requestTablePage(records,table);assert.equal(page.rows.length,1);assert.equal(page.rows[0].id,'running');
updateRequestTableState(table,'status','unscored');assert.equal(requestTablePage(records,table).rows[0].id,'unscored');
updateRequestTableState(table,'status','all');updateRequestTableState(table,'search','HTTP 500');page=requestTablePage(records,table);assert.equal(page.rows.length,1);assert.equal(page.rows[0].id,'failed');
assert.match(renderRequestTable([],createRequestTableState()),/No requests match these filters/);
""")


def test_report_request_table_preserves_selected_page_and_scroll_during_append():
    run_render_checks(r"""
const records=Array.from({length:600},(_,i)=>({id:'request-'+i,status:'completed',elapsed_ms:i})),table=createRequestTableState();
table.page=2;table.sort='elapsed_ms';table.direction='asc';
const parent=$('#main');patchHTML(parent,renderRequestTable(records,table));const scroll=$('.request-table-scroll');scroll.scrollTop=120;scroll.scrollLeft=40;
const first=$('[data-request="request-200"]');first.focus({preventScroll:true});records.push({id:'request-600',status:'running'});
patchHTML(parent,renderRequestTable(records,table));
assert.equal($('.request-table-scroll'),scroll);assert.equal(scroll.scrollTop,120);assert.equal(scroll.scrollLeft,40);
assert.equal($('[data-request="request-200"]'),first);assert.equal(document.activeElement,first);assert.equal(table.page,2);
assert.doesNotMatch(parent.innerHTML,/data-request="request-0"|request-spot|condition-mosaic/);
""",dom=True)


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


def test_report_requests_order_by_dispatch_without_mutating_saved_evidence():
    run_render_checks(r"""
const value={requests:[{id:'finished-first',dispatch_ms:20},{id:'started-first',dispatch_ms:10}]};
assert.equal(orderedRequests(value).map(r=>r.id).join(','),'started-first,finished-first');
assert.equal(value.requests[0].id,'finished-first');
""")
