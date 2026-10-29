"""Round-two live-review regressions, exercised without sockets."""
from tests.test_ux_setup import run_browser_logic
from tests.test_ux_results import run_render_checks
from tests.test_ui_request_evidence import run_script


def test_compare_choice_and_clear_keep_current_plan_launchable():
    run_browser_logic(r'''
state.config=structuredClone(bootstrap.defaults);state.jsonDraft=state.jsonBase=null;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{planned_total:19,rows:[]}};
readForm=()=>({config:state.config});updateBaselineDrift=saveDraft=()=>{};renderRequestPreview=()=>{};
currentSettingErrors=currentResourceErrors=()=>[];tokenizerSetupReady=()=>true;
for(const value of ['baseline','']){
 assert(canStartRun());listeners.change({target:{name:'baseline_id',value}});
 assert.equal(state.prefillBaseline,value);assert(canStartRun(),'comparison choice must not strand Launch behind stale traffic planning');
 assert.equal(currentRequestPlan().planned_total,19);
}
''')


def test_limit_summary_shows_cap_and_plan_once_and_one_empty_hint():
    run_render_checks(r'''
const cfg={...bootstrap.defaults,max_requests:10,request_timeout_seconds:300,concurrency:4};
currentRequestPlan=()=>({planned_total:19,count_kind:'exact',rows:[]});
assert.equal(sharedLimitsSummary(cfg),'≤ 10 requests · 19 planned, budget too small · ≤ 5 m per request · 4 at once');
const html=renderSharedRunSettings(cfg);assert.equal((html.match(/19 planned/g)||[]).length,1);
assert.equal((html.match(/Empty = off/g)||[]).length,1);assert.doesNotMatch(html,/checks-plan-summary/);
''')


def test_pass_reasons_use_the_check_measurements_not_incidental_scores():
    run_render_checks(r'''
const generation=check('generation','pass',{metrics:{scored:0,correct:0,generation_tokens_per_second_p50:42.5,max_stream_gap_ms:12}});
assert.equal(measuredCheckReason(generation,report([generation])),'42.5 tokens/s · longest stream gap 12 ms');
const serving=check('serving','pass',{metrics:{scored:11,correct:11,attempted:11,completed:11}});
assert.equal(measuredCheckReason(serving,report([serving])),'11 of 11 valid responses');
''')


def test_baseline_drift_compares_check_sets_and_prioritizes_target_identity():
    run_render_checks(r'''
const cfg={...bootstrap.defaults,checks:['serving','access'],targets:[{name:'local',url:'http://new/v1',model:'llama3.1'}]};
state.baselineConfigs=new Map([['base',{...cfg,checks:['access','serving'],targets:[{name:'local',url:'http://old/v1',model:'fake-model'}]}]]);
let html=renderBaselineDrift('base',cfg);assert.match(html,/fake-model/);assert.match(html,/llama3.1/);assert.match(html,/http:\/\/old\/v1/);assert.doesNotMatch(html,/2 checks/);
state.baselineConfigs.set('base',{...cfg,checks:['access','serving']});assert.equal(renderBaselineDrift('base',cfg),'');
''')


def test_scoped_condition_table_has_only_relevant_controls():
    run_script(r'''
const html=evidence.renderRequestTable([],evidence.createRequestTableState(),{scoped:true});
assert.doesNotMatch(html,/All conditions|data-evidence-filter="condition"|request-sort-control/);
assert.match(html,/data-evidence-sort="elapsed_ms"/);
''')


def test_unknown_condition_total_and_probe_criterion_are_honest():
    run_render_checks(r'''
const r={id:'probe',target:'local',scenario:'performance_evidence_probe',condition_id:'performance_evidence_probe',status:'completed',valid:true,score:null};
state.detail={id:'r',state:'completed',report:report([check('generation','pass')],{requests:[r]})};
const html=renderTests([r],{report:state.detail.report,conditions:[{target:'local',condition:{id:r.condition_id,check:'serving'},status:'skipped',planned:null,started:1,completed:1}]});
assert.doesNotMatch(html,/1\/\?|Not run/);assert.match(renderPreparation(state.detail),/1 request/);assert.equal(requestStatus(r),'unscored');assert.doesNotMatch(html,/data-condition-id="performance_evidence_probe"/);
''')


def test_generation_row_uses_its_timing_criterion():
    run_render_checks(r'''
const r={id:'g',target:'local',scenario:'generation',condition_id:'generation',check_ids:['generation'],status:'completed',valid:true,score:null,elapsed_ms:40,first_output_ms:9,generation_tokens_per_second:50,max_stream_gap_ms:2};
const value=report([check('generation','pass',{metrics:{generation_tokens_per_second_p50:50}})],{requests:[r]});state.detail={id:'r',state:'completed',report:value};
assert.equal(requestStatus(r),'pass');
''')


def test_completed_load_preparations_have_one_summary():
    run_render_checks(r'''
const preparations=Object.fromEntries(['short','medium','long'].map(size=>[`load_preparation_${size}_primary`,{status:'completed',builtin:true}]));
const html=renderPreparation({id:'r',state:'completed',report:{observations:{targets:{local:{preparations}}}}});
assert.match(html,/<summary>Preparation complete · 3 workloads<\/summary>/);
assert.equal((html.match(/<summary>/g)||[]).length,1);for(const size of ['Short','Medium','Long'])assert(html.includes(size));
''')


def test_structured_remediation_renders_ui_controls_and_keeps_reason_concrete():
    run_render_checks(r'''
const c=check('load','inconclusive',{summary:'Need --max-duration 60 and --max-requests off.',finding_summary:'The request budget ended the load window.',remediation:{action:'set_limit',limit:'max_duration',clear:'max_requests'}});
const html=detail(report([c]));assert.match(html,/Set Max duration and turn off Max requests in Limits/);assert.match(html,/>Open Limits</);assert.doesNotMatch(html,/--max-|Need --/);
const correctness=check('correctness','fail',{metrics:{correct:0,scored:5},summary:'scoring failed the configured correctness limit; rerun the same fixtures'});
assert.equal(measuredCheckReason(correctness,report([correctness])),correctness.summary);
assert.match(renderScoredCheckEvidence(correctness),/0 of 5 answers matched the expected answer/);
assert.equal(renderGuidance(report([correctness]),state.detail),'');
''')


def test_comparison_reason_uses_human_setting_name_without_prose_rewrites():
    run_render_checks(r'''
const value=report([],{baseline:{targets:[{target:'local',reasons:['Run setting checks differs.'],reason_details:[{code:'setting_differs',key:'checks',text:'Run setting checks differs.'}]}],status:'inconclusive'}});
state.detail={id:'r',report:value};const html=renderComparison(value);
assert.match(html,/Selected checks differ from baseline/);assert.doesNotMatch(html,/Run setting checks differs/);
''')


def test_request_rail_focuses_heading_after_opening_disclosure():
    run_render_checks(r'''
const records=[{id:'r',target:'local',scenario:'initial',status:'completed',valid:true,score:true}];detail(report([check('access','pass')],{requests:records}));
reportView.tab='results';revealReportSection('requests');assert($('.all-requests').open);
assert.equal(document.activeElement,$('.all-requests h2'));assert.equal(document.activeElement.tagName,'H2');
''',dom=True)


def test_enter_in_setup_fields_never_launches_and_modified_enter_does():
    run_render_checks(r'''
$('#main').innerHTML='<form id="new-run-form"><input name="api_key"><input name="max_requests"><input name="model"><input name="url"><button type="button" data-action="test-connection">Connect</button><button id="start-run" type="submit">Launch</button></form>';
let launch=0,connect=0,prevented=0;primaryAction=()=>launch++;$('[data-action="test-connection"]').click=()=>connect++;
for(const name of ['api_key','max_requests','model','url']){const input=$(`[name="${name}"]`);input.name=name;listeners.keydown({key:'Enter',target:input,preventDefault(){prevented++;}});}
assert.equal(prevented,4);assert.equal(launch,0);assert.equal(connect,2);
listeners.keydown({key:'Enter',ctrlKey:true,target:$('[name="api_key"]'),preventDefault(){}});assert.equal(launch,1);
''',dom=True)


def test_condition_api_aggregate_cannot_mark_recorded_probe_not_run():
    run_render_checks(r'''
(async()=>{
const r={id:'probe',target:'local',scenario:'performance_evidence_probe',condition_id:'performance_evidence_probe',status:'completed',valid:true,score:null};
state.detail={id:'r',state:'completed',report:report([check('generation','pass')],{requests:[r]})};
renderTests([r],{report:state.detail.report,conditions:[{target:'local',condition:{id:r.condition_id,check:'serving'},status:'skipped',planned:null,started:1,completed:1}]});
api=async()=>({page:1,pages:1,total:1,rows:[{request_id:'probe',number:1,status:'unscored'}],aggregate:{aggregate_status:'skipped',started:1,completed:1}});
openConditionDialog([...conditionViews.keys()][0]);for(let i=0;i<12;i++)await Promise.resolve();
assert.equal([...conditionViews.values()][0].status,'unscored');assert.doesNotMatch($('#test-dialog').textContent,/Not run/);
})()
''',dom=True)


def test_recovery_answer_failure_is_part_of_its_condition_criterion():
    run_render_checks(r'''
const r={id:'bad-recovery',target:'local',scenario:'recovery',condition_id:'recovery',check_ids:['recovery'],status:'completed',valid:true,score:false};
assert.equal(requestStatus(r,report([check('recovery','fail')],{requests:[r]})),'fail');
''')


def test_scoped_mobile_cards_keep_visible_sortable_headers():
    from pathlib import Path
    import re
    css=(Path(__file__).parents[1]/'src/giraffe/static/styles.css').read_text()
    mobile=css.split('@media (max-width: 599px)',1)[1]
    assert re.search(r'\.scoped-request-evidence\s+\.request-table thead\s*\{[^}]*position:\s*static',mobile)
    assert re.search(r'\.scoped-request-evidence\s+\.request-table thead\s*\{[^}]*clip-path:\s*none',mobile)
    run_script(r'''
const html=evidence.renderRequestTable([],evidence.createRequestTableState(),{scoped:true});
assert.match(html,/class="request-evidence scoped-request-evidence"/);assert.doesNotMatch(html,/data-evidence-sort-order/);
''')


def test_live_generation_criterion_uses_live_summary_without_final_report():
    run_render_checks(r'''
const r={id:'g',target:'local',scenario:'generation',condition_id:'generation',check_ids:['generation'],status:'completed',valid:true,score:null,generation_tokens_per_second:42};
state.detail={id:'r',state:'running',live_requests:[r],summary:{checks:[check('generation','pass')]}};
assert.equal(requestStatus(r),'pass');
''')


def test_comparison_display_uses_structured_setting_codes_and_legacy_text_stays_intact():
    run_render_checks(r'''
for(const [reason,text] of [[{code:'setting_unknown',key:'effective_checks'},'Check settings differ or were not recorded in both runs.'],[{code:'setting_differs',key:'request_source'},'Questions differ from baseline.'],[{code:'setting_differs',key:'sessions'},'Multi-turn sessions differs from baseline.']])assert.equal(comparisonReasonText(reason),text);
assert.equal(comparisonReasonText('Archived diagnostic text.'),'Archived diagnostic text.');
''')


def test_comparison_multiple_structured_diagnostics_keep_human_names():
    run_render_checks(r'''
const target={reason_details:[{code:'setting_differs',key:'checks',text:'New prose.'},{code:'setting_unknown',key:'effective_checks',text:'Other prose.'}]};
assert.equal(comparisonTargetReasons(target).join(' '),'Selected checks differ from baseline. Check settings differ or were not recorded in both runs.');
''')


def test_remediation_click_survives_hash_route_and_focuses_requested_editor():
    run_render_checks(r'''
(async()=>{
state.config=structuredClone(bootstrap.defaults);state.detail={id:'r',state:'completed',report:report([check('first_output','inconclusive')])};
updateNav=schedulePoll=updateBounds=()=>{};readForm=()=>({config:state.config});
renderNew=()=>{$('#main').innerHTML='<h1>New run</h1><form id="new-run-form"><input name="baseline_id"><input name="mode"><input name="restart_target"><details id="shared-settings"><input id="field-max_duration_seconds"></details><details id="setup-advanced"><article data-test-card="first_output"><button data-configure-test="first_output">Configure</button><div id="settings-first_output"><input name="test_first_output_limit_first_output_ms"></div></article></details></form>';};
syncTestControls=()=>{$('#settings-first_output').hidden=state.openTest!=='first_output';};
rerun=async()=>{state.openTest=null;location.hash='#new';};
for(const [action,selector] of [['edit-result-check','[name="test_first_output_limit_first_output_ms"]'],['edit-result-limits','#field-max_duration_seconds']]){
 location.hash='#runs/r';$('#main').innerHTML=`<button data-action="${action}" data-check-id="first_output">Open</button>`;
 const button=$('#main [data-action]');assert.equal(button.dataset.action,action);await listeners.click({target:button,preventDefault(){}});assert(state.pendingSetupEdit,action+':pending destination');await listeners['window:hashchange']();
 if(action==='edit-result-check')assert.equal(state.openTest,'first_output',action+':editor after route');else assert($('#shared-settings').open,action+':limits after route');
 const field=$(selector);assert(field,$('#main').innerHTML);assert.equal(document.activeElement?.getAttribute('name') || document.activeElement?.id,field?.getAttribute('name') || field?.id,action+':focus after route');assert.equal(document.activeElement,field);
 if(action==='edit-result-check'){assert.equal(state.openTest,'first_output');assert($('#setup-advanced').open);assert(!$('#settings-first_output').hidden);}else assert($('#shared-settings').open);
}
})()
''',dom=True)


def test_compact_condition_uses_available_criterion_records_before_hydration():
    run_render_checks(r'''
const r={id:'g',target:'local',scenario:'generation',condition_id:'generation',check_ids:['generation'],status:'completed',valid:true,score:null};
const value=report([check('generation','pass')],{requests:[r],requests_compact:true});state.detail={id:'r',state:'completed',report:value};
renderTests([r],{report:value,conditions:[{target:'local',condition:{id:'generation',check:'generation'},started:1,completed:1}]});
const view=[...conditionViews.values()][0],d={table:createRequestTableState(),payload:{rows:[],total:0}};
d.table.status='pass';const records=conditionTableRecords(view,d);assert(d.localEvidence);assert.equal(requestTablePage(records,d.table,{status:requestStatus}).rows.length,1);
''')


def test_remote_condition_rows_keep_status_until_source_evidence_arrives():
    run_render_checks(r'''
state.detail={id:'r',state:'completed',report:report([],{requests:[],requests_compact:true})};
const view={target:'local',condition:{id:'initial',check:'access'},summary:{started:2},records:[]};
const records=conditionTableRecords(view,{payload:{rows:[{request_id:'p',status:'passed'},{request_id:'u',status:'unscored'}]}});
assert.equal(requestStatus(records[0]),'pass');assert.equal(requestStatus(records[1]),'unscored');
''')


def test_structured_review_actions_keep_load_preparation_guidance():
    run_render_checks(r'''
for(const [code,word] of [['input_not_calibrated','tokenizer'],['client_delay','load'],['missing_timing','streaming']]){
 const html=renderRemediation(check('load','inconclusive',{remediation:{action:'review_check',check:'load',code}}));
 assert(html.includes(word));assert.match(html,/data-action="edit-result-check"/);assert.doesNotMatch(html,/--|targets\[\]|load_test\./);
}
''')
