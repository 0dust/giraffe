"""Condition details retain readable context and every recorded condition."""

from tests.test_ui_conditions import DIALOG_SETUP
from tests.test_ux_results import run_render_checks


def test_setup_and_calibration_conditions_remain_visible_with_request_evidence():
    run_render_checks(r"""
state.detail={id:'setup',state:'running'};
const rows=['load_calibration_v45_short_short_answer','load_calibration_long_long_output','stress_calibration_large','warm','recovery_prime','context_calibration','setup_connection','load_primary_combinations_short_short_concurrent_c4'].map(id=>({target:'local',condition:{id,label:id,check:'load'},planned:4,started:4,completed:4,status:'finished'}));
const records=[{id:'setup-request',target:'local',scenario:'warm',status:'completed'}];
const html=renderTests(records,{live:true,conditions:rows});
assert.equal(conditionViews.size,8);assert.equal((html.match(/class="condition-strip-row"/g)||[]).length,8);for(const row of rows)assert([...conditionViews.values()].some(view=>view.condition.id===row.condition.id));
assert.equal(records.length,1);assert.equal(records[0].scenario,'warm');
const fallback=renderTests([{id:'warm',scenario:'warm'},{id:'short',scenario:'short',status:'completed'}],{live:true});assert.equal(conditionViews.size,2);assert.match(fallback,/Short requests/);assert.match(fallback,/Warm-up/);
renderTests([],{live:true,conditions:[{condition:{id:'opaque-setup',label:'Preparation',load_type:'calibration'}},{condition:{id:'opaque-warmup',phase:'warmup'}},{condition:{id:'short',label:'Short requests',check:'serving'}}]});assert.equal(conditionViews.size,3);
""")


def test_primary_load_labels_split_shape_from_pattern_at_32_and_1000():
    run_render_checks(r"""
for(const level of [32,1000]){
 state.detail={id:'readable-'+level,state:'running'};
 const id='load_primary_combinations_short_short_concurrent_c'+level;
 const condition={id,label:'Combinations short short concurrent c'+level,check:'load',level,parameters:{concurrency:level}};
 const html=renderTests([],{live:true,conditions:[{target:'local',condition,planned:null,started:1,running:1,aggregate_status:'running'}]});
 const view=[...conditionViews.values()][0];assert.equal(view.name,'Short input / short output');assert.equal(view.description,number(level)+' at once');
 assert.match(html,/Short input \/ short output/);assert.doesNotMatch(html,/Combinations short short concurrent/);
}
state.detail={id:'readable-rate',state:'running'};
renderTests([],{live:true,conditions:[{condition:{id:'load_primary_combinations_long_short_poisson_r2.5',label:'Internal',check:'load'},started:0}]});
const view=[...conditionViews.values()][0];assert.equal(view.name,'Long input / short output');assert.equal(view.description,'Poisson arrivals · 2.5 requests/s');
""")


def test_shared_condition_rows_support_keyboard_detail_without_page_jump_controls():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 openConditionDialog([...conditionViews.keys()][0]);await settle();await pageTo(5);
 assert.equal($('#condition-page-number'),null);assert.equal($('#condition-page-size'),null);
 const rows=$$('.request-evidence-row'),opened=[];let prevented=0;rows[0].focus({preventScroll:true});
 handleRequestTableKeydown({target:rows[0],key:'ArrowDown',preventDefault(){prevented++;}},id=>opened.push(id));assert.equal(document.activeElement,rows[1]);
 handleRequestTableKeydown({target:rows[1],key:'Enter',preventDefault(){prevented++;}},id=>opened.push(id));assert.deepEqual(opened,['r802']);
 assert.equal(prevented,2);assert.equal(conditionDialog.page,5);assert($('#test-dialog').open);assert.equal($('.request-spot'),null);
})()
""", dom=True)


def test_dialog_title_and_focused_strip_share_readable_shape():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 const id='load_primary_combinations_short_short_concurrent_c4';
 aggregate={...aggregate,condition:{id,label:'Combinations short short concurrent c4',check:'load',level:4},condition_id:id,check_id:'load',name:'Combinations short short concurrent c4',concurrency_description:'4 concurrent'};
 state.detail.live_requests=state.detail.live_requests.slice(0,2).map(record=>({...record,scenario:id,condition_id:id}));draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 assert.equal($('#test-title').textContent,'Short input / short output');assert.match($('#test-dialog').textContent,/Load test · 4 at once/);
 assert.match($('.condition-strip-row').textContent,/Short input \/ short output · 4 at once/);
 assert.equal($$('#test-dialog .request-evidence-row').length,200);assert($$('#test-dialog .request-evidence-row td[data-label="Condition"]').every(cell=>cell.textContent==='Short input / short output'));
 assert.equal(conditionDialog.localEvidence,false);assert.match($('#test-dialog').textContent,/Page 1 of 5/);
 assert.equal($('.condition-plan-identity'),null);assert.equal([...conditionViews.values()][0].condition.id,id);assert.match(calls.at(-1),/conditions\/load_primary_combinations_short_short_concurrent_c4\/requests/);
 assert.doesNotMatch($('#test-title').textContent,/Combinations short short concurrent|load_primary/);
})()
""", dom=True)


def test_request_context_keeps_whole_condition_number_on_later_filtered_page():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 remaining=757;state.detail.live_requests=state.detail.live_requests.slice(0,757);aggregate={...aggregate,planned:757,started:757,running:757};draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 await pageTo(4);
 const context=conditionRequestContext('r701');assert.equal(context.number,701);assert.equal(context.total,757);assert.equal(context.condition_id,condition.id);assert.equal(context.target,'local');assert.match(context.label,/Short input \/ short output/);
 conditionDialog.filter='running';conditionDialog.payload={...conditionDialog.payload,total:7,totals:{running:7,passed:750}};
 assert.equal(conditionRequestContext('r701').total,757);assert.equal(conditionRequestContext('absent'),null);
})()
""", dom=True)


def test_unknown_duration_plan_explains_the_missing_denominator_without_guessing():
    run_render_checks(r"""
state.detail={id:'duration',state:'running'};
renderTests([],{live:true,conditions:[{condition:{id:'load_primary_combinations_short_short_concurrent_c4',check:'load'},planned:null,planned_kind:'unknown',started:4}]});
assert.match(conditionDialogCounts([...conditionViews.values()][0]),/Unknown — duration-based/);
renderTests([],{live:true,conditions:[{condition:{id:'custom',label:'Custom requests',check:'serving'},planned:null,planned_kind:'unknown',started:4}]});
assert.match(conditionDialogCounts([...conditionViews.values()][0]),/Unknown — plan not recorded/);assert.doesNotMatch(conditionDialogCounts([...conditionViews.values()][0]),/duration-based/);
""")


def test_local_condition_context_uses_dispatch_order_after_table_sort():
    run_render_checks(r"""
(async()=>{
 const condition={id:'correctness',check:'correctness',label:'Answer correctness'},aggregate={target:'local',condition,planned:2,planned_kind:'exact',started:2,completed:2,aggregate_status:'passed'};
 const late={id:'late',target:'local',condition_id:'correctness',scenario:'correctness',dispatch_ms:20,elapsed_ms:5,status:'completed',valid:true,score:true},early={...late,id:'early',dispatch_ms:10,elapsed_ms:90};
 detail(report([check('correctness','pass')],{requests:[late,early],conditions:[aggregate]}));
 api=async()=>({aggregate,rows:[],page:1,pages:1,page_size:50,total:2,totals:{passed:2}});
 openConditionDialog([...conditionViews.keys()][0]);for(let i=0;i<12;i++)await Promise.resolve();
 conditionDialog.table.sort='elapsed_ms';conditionDialog.table.direction='desc';renderConditionDialog();
 assert.equal(conditionRequestContext('early').number,1);assert.equal(conditionRequestContext('late').number,2);assert.equal(conditionRequestContext('late').total,2);
 assert.equal(state.detail.report.requests[0].id,'late','inspection must not mutate saved evidence');
})()
""",dom=True)
