"""Condition details retain readable context and exclude setup work."""

from tests.test_ui_conditions import DIALOG_SETUP
from tests.test_ux_results import run_render_checks


def test_setup_and_calibration_are_not_conditions_but_request_evidence_is_untouched():
    run_render_checks(r"""
state.detail={id:'setup',state:'running'};
const rows=['load_calibration_v45_short_short_answer','load_calibration_long_long_output','stress_calibration_large','warm','recovery_prime','context_calibration','setup_connection','load_primary_combinations_short_short_concurrent_c4'].map(id=>({target:'local',condition:{id,label:id,check:'load'},planned:4,started:4,completed:4,status:'finished'}));
const records=[{id:'setup-request',scenario:'warm',status:'completed'}];
const html=renderTests(records,{live:true,conditions:rows});
assert.equal(conditionViews.size,1);assert.match(html,/1 test condition/);assert.doesNotMatch(html,/Calibrating|calibration|Warm-up|setup_connection/);
assert.equal(records.length,1);assert.equal(records[0].scenario,'warm');
const fallback=renderTests([{id:'warm',scenario:'warm'},{id:'short',scenario:'short',status:'completed'}],{live:true});assert.equal(conditionViews.size,1);assert.match(fallback,/Short requests/);
renderTests([],{live:true,conditions:[{condition:{id:'opaque-setup',label:'Preparation',load_type:'calibration'}},{condition:{id:'opaque-warmup',phase:'warmup'}},{condition:{id:'short',label:'Short requests',check:'serving'}}]});assert.equal(conditionViews.size,1);
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


def test_page_enter_shares_validation_with_go_and_does_not_open_a_fake_cell():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 openConditionDialog([...conditionViews.keys()][0]);await settle();
 let prevented=0;const key=()=>handleConditionKeydown({target:$('#condition-page-number'),key:'Enter',preventDefault(){prevented++;}});
 $('#condition-page-number').value='76';key();await settle();assert.equal(conditionDialog.page,76);assert.match($('#test-dialog').textContent,/751–760 of 1,000 requests/);
 for(const invalid of ['', 'no', '0', '101', '2.5']){$('#condition-page-number').value=invalid;key();assert.equal(conditionDialog.page,76);assert.match($('#condition-page-error').textContent,/Enter a page from 1 to 100/);}
 assert.equal(prevented,6);assert($('#test-dialog').open);
})()
""", dom=True)


def test_dialog_title_tooltip_and_focused_strip_share_readable_shape():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 const id='load_primary_combinations_short_short_concurrent_c4';
 aggregate={...aggregate,condition:{id,label:'Combinations short short concurrent c4',check:'load',level:4},condition_id:id,check_id:'load',name:'Combinations short short concurrent c4',concurrency_description:'4 concurrent'};
 draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 assert.equal($('#test-title').textContent,'Short input / short output');assert.match($('#test-dialog').textContent,/Load test · 4 at once/);
 assert.match($('#condition-selection').textContent,/Short input \/ short output · 4 at once/);
 assert.match($('.condition-spot').getAttribute('aria-label'),/Short input \/ short output · 4 at once/);
 assert.doesNotMatch($('#test-dialog').textContent,/Combinations short short concurrent|load_primary/);
})()
""", dom=True)


def test_request_context_keeps_whole_condition_number_on_later_filtered_page():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 remaining=757;aggregate={...aggregate,planned:757,started:757,running:757};draw();openConditionDialog([...conditionViews.keys()][0]);await settle();
 $('#condition-page-number').value='76';click('go');await settle();
 const context=conditionRequestContext('r751');assert.equal(context.number,751);assert.equal(context.total,757);assert.equal(context.condition_id,condition.id);assert.equal(context.target,'local');assert.match(context.label,/Short input \/ short output/);
 conditionDialog.filter='running';conditionDialog.payload={...conditionDialog.payload,total:7,totals:{running:7,passed:750}};
 assert.equal(conditionRequestContext('r751').total,757);assert.equal(conditionRequestContext('absent'),null);
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
