"""Round-one UX regressions, executed in the socket-free browser VM."""
from pathlib import Path
from tests.test_ux_results import run_render_checks
from tests.test_ui_request_evidence import run_script


def test_clean_checks_with_incomparable_baseline_keep_truthful_hero_and_action():
    run_render_checks(r'''
const value=report(['access','serving','first_output','generation'].map(id=>check(id,'pass')).concat(check('baseline','inconclusive')),{baseline:{status:'inconclusive',targets:[]}});
assert.equal(mainFinding(value),'4 checks passed · baseline not comparable');
assert.match(resultActions(value),/data-action="show-comparison"/);
assert.doesNotMatch(resultActions(value),/show-inconclusive/);
assert.doesNotMatch(renderGuidance(value,state.detail),/Details/);
''')


def test_evidence_columns_prioritize_timing_and_score_is_separate():
    run_script(r'''
const record={id:'r',status:'completed',valid:true,score:true,elapsed_ms:18,first_token_ms:4,output_tokens:2,evaluation:{expected:'Paris'},output:'Paris'};
const html=evidence.renderRequestTable([record],evidence.createRequestTableState());
const labels=['Status','Condition','Latency','TTFT','Tokens','Question','Expected / received','Error'];
let last=-1;for(const label of labels){const at=html.indexOf(`>${label}`,html.indexOf('<thead>'));assert(at>last,label);last=at;}
assert.match(html,/✓ match/);assert.match(html,/data-label="Latency"/);assert.match(html,/class="request-question"/);
''')


def test_access_row_uses_access_criterion_not_answer_score():
    run_render_checks(r'''
const r={id:'r',scenario:'initial',condition_id:'initial',check_ids:['access'],status:'completed',valid:true,score:false};
state.detail={id:'run',state:'completed',report:report([check('access','pass')],{requests:[r]})};
assert.equal(requestStatus(r),'pass');
const scored={...r,condition_id:'correctness',scenario:'correctness',check_ids:['correctness']};
assert.equal(requestStatus(scored),'fail');
''')


def test_catalog_unselected_and_gpu_fault_appear_in_primary_findings():
    run_render_checks(r'''
const html=detail(report([check('access','pass'),check('gpu','fail',{required:false})]));
assert.match(html,/1 need attention/);assert.match(html,/Not selected:.*Response validity/);
assert(html.indexOf('data-finding="local:gpu"')<html.indexOf('class="server-observations"')||!html.includes('class="server-observations"'));
assert.match(html,/condition-reference" open/);assert.doesNotMatch(html,/<summary>Details<\/summary>/);
''')


def test_setup_limits_have_plan_and_labelled_empty_means_off():
    run_render_checks(r'''
state.config={...bootstrap.config,max_requests:null,max_duration_seconds:null,concurrency:null,request_timeout_seconds:300};
currentRequestPlan=()=>({planned_total:13,known_requests:13,requests_per_target:13,rows:[],checks:[],effective_concurrency:4});
const html=renderSharedRunSettings(state.config);
assert.doesNotMatch(html,/No (?:request|duration|concurrency) limit|type="checkbox"|checks-plan-summary sr-only/);
assert.match(html,/13 requests/);assert.match(html,/≤ 5 m per request/);assert.match(html,/Empty = off/);
assert.match(html,/<label[^>]*for="field-max_requests"[^>]*>Max requests/);
''')


def test_setup_baseline_drift_explicit_choice_and_match_action():
    run_render_checks(r'''
state.config={request_timeout_seconds:300,checks:['access'],limits:{},targets:[],max_requests:null,max_duration_seconds:null,concurrency:null};
state.baselines=[{id:'base',run_id:'older',name:'Before'}];state.baselineConfigs=new Map([['base',{...state.config,request_timeout_seconds:15}]]);
const html=renderBaselineDrift('base',state.config);
assert.match(html,/Baseline used 15 s request timeout/);assert.match(html,/Match baseline settings/);
assert.equal(renderBaselineDrift('',state.config),'');
assert.doesNotMatch(baselineOptions(''),/<option value="base" selected/);
''')


def test_conditions_include_preparation_and_correctness_count():
    run_render_checks(r'''
const records=[{id:'warm',scenario:'warm',condition_id:'warm',status:'completed',valid:true,score:true},...Array.from({length:5},(_,i)=>({id:`c${i}`,scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:false}))];
const html=renderTests(records,{report:report([check('correctness','fail')],{requests:records})});
assert.match(html,/Warm-up/);assert.match(html,/0\/5 correct · 5 failed/);
assert.equal(conditionViews.size,2);
''')


def test_condition_dialog_uses_same_evidence_table_and_200_row_paging():
    run_render_checks(r'''
(async()=>{
const records=[{id:'answer',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:false,elapsed_ms:12,evaluation:{messages:[{role:'user',content:'Capital?'}],expected:'Paris'},output:'Lyon'}];
detail(report([check('correctness','fail')],{requests:records}));
const paths=[];api=async path=>{paths.push(path);return {page:1,pages:1,total:1,rows:[{request_id:'answer',number:1,status:'failed',result_text:'Wrong'}]};};
openConditionDialog([...conditionViews.keys()][0]);await Promise.resolve();await Promise.resolve();
const dialog=$('#test-dialog');assert.match(dialog.innerHTML,/request-evidence-table/);assert.match(dialog.textContent,/Capital\?/);
assert.doesNotMatch(dialog.innerHTML,/condition-page-size|condition-page-number|plan identity/);
assert(paths[0].includes('page_size=50'));
})()
''', dom=True)


def test_first_answer_reason_does_not_claim_it_is_ttft():
    run_render_checks(r'''
const value=report([check('first_output','pass',{metrics:{first_output_p95_ms:9,first_token_p95_ms:3,attempted:2}})]);
const reason=measuredCheckReason(value.checks[0],value);
assert.match(reason,/p95 first answer 9 ms/);assert.doesNotMatch(reason,/TTFT/);
''')


def test_baseline_choice_requires_this_setup_and_survives_profile_change():
    from tests.test_ux_catalogue import browser_logic
    browser_logic(r'''
const elements={baseline_id:{value:'explicit'},mode:{value:'normal'},restart_target:{value:''}};
nodes['#new-run-form']={elements};readForm=()=>({config:structuredClone(config)});configChanged=()=>{};renderNew=()=>{};
state.prefillBaseline='explicit';saveDraft();const persisted=JSON.parse(sessionStorage.getItem('giraffe-draft-v1'));
assert(!Object.hasOwn(persisted,'baseline_id'));
state.prefillBaseline='stale';await restoreDraft();assert.equal(state.prefillBaseline,'');
state.prefillBaseline='explicit';chooseSetupProfile('latency');assert.equal(state.prefillBaseline,'explicit');
api=async()=>({config:structuredClone(config)});await startNewRun();assert.equal(state.prefillBaseline,'');
const endpoint={...config.targets[0],api_key_env:'MY_KEY',metrics_url:'http://private/metrics'};
state.config={...config,targets:[endpoint]};state.baselineConfigs=new Map([['explicit',{...config,request_timeout_seconds:15,targets:[{name:'old',url:'http://old/v1',model:'old-model'}]}]]);
readForm=()=>({config:structuredClone(state.config)});matchBaselineSettings();
assert.equal(JSON.stringify(state.config.targets),JSON.stringify([endpoint]));assert.equal(state.config.request_timeout_seconds,15);assert.equal(state.prefillBaseline,'explicit');
''')


def test_condition_payload_cannot_override_access_criterion_with_answer_score():
    run_render_checks(r'''
const r={id:'access',scenario:'initial',condition_id:'initial',check_ids:['access'],status:'completed',valid:true,score:false};
state.detail={id:'run',state:'completed',report:report([check('access','pass')],{requests:[r]})};
assert.equal(requestStatus({...r,_conditionStatus:'failed'}),'pass');
assert.equal(requestStatus({...r,status:'timeout',_conditionStatus:'timed_out'}),'timeout');
assert.equal(requestStatus({id:'future',condition_id:'initial',_conditionStatus:'not_started'}),'not_started');
''')


def test_observations_do_not_inflate_baseline_headline_check_count():
    run_render_checks(r'''
const value=report([check('access','pass'),check('gpu','pass',{required:false}),check('prefix','pass',{required:false}),check('baseline','inconclusive')],{baseline:{status:'inconclusive',targets:[]}});
assert.equal(mainFinding(value),'1 check passed · baseline not comparable');
''')


def test_condition_sort_search_resets_remote_page_and_keeps_search_count_honest():
    run_render_checks(r'''
(async()=>{
const records=[{id:'x',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:false,question:'Needle'}];
state.detail={id:'run',state:'completed',report:{...report([check('correctness','pass')]),requests:records,requests_compact:true}};
renderTests(records,{live:true,report:report([check('correctness','pass')]),conditions:[{condition:{id:'correctness',check:'correctness'},started:500,completed:500}]});
const key=[...conditionViews.keys()][0];
conditionDialog={key,page:2,size:200,filter:'all',table:{...createRequestTableState(),page:2},payload:{page:2,pages:3,total:500,rows:[{request_id:'x',number:201,status:'failed'}]},updatedAt:0};
const paths=[];api=async path=>{paths.push(path);return {page:1,pages:3,total:500,rows:[{request_id:'x',number:1,status:'failed'}]};};
$('#test-dialog').open=true;
updateConditionEvidence({closest:()=>$('#test-dialog')},'search','Needle');
await Promise.resolve();await Promise.resolve();
assert(paths.some(path=>path.includes('page=1&')));
assert.doesNotMatch($('#test-dialog').textContent,/1 of 500 requests match/);
})()
''',dom=True)


def test_completed_load_preparations_share_one_summary_with_distinct_workload_details():
    run_render_checks(r'''
const preparations=Object.fromEntries(['short','medium','long'].map(size=>[`load_preparation_${size}_primary`,{status:'completed',builtin:true}]));
const html=renderPreparation({id:'r',state:'completed',report:{observations:{targets:{local:{preparations}}}}});
const summaries=[...html.matchAll(/<summary>(.*?)<\/summary>/g)].map(match=>match[1]);
assert.deepEqual(summaries,['Preparation complete · 3 workloads']);
for(const scope of ['Short','Medium','Long'])assert(html.includes(scope),'workload detail remains available: '+scope);
assert.equal((html.match(/class="preparation-status"/g)||[]).length,1);
''')


def test_pass_without_measurements_preserves_its_recorded_evidence():
    run_render_checks(r'''
const c=check('access','pass',{summary:'Endpoint responded <quickly>',metrics:{}});
assert.equal(measuredCheckReason(c,report([c])),'Endpoint responded <quickly>');
''')


def test_condition_page_assembles_supported_api_pages_into_200_rows():
    run_render_checks(r'''
(async()=>{
const calls=[];api=async path=>{calls.push(path);const page=Number(path.match(/page=(\d+)/)[1]);assert(path.includes('page_size=50'));return {page,pages:5,total:205,rows:Array.from({length:page===5?5:50},(_,i)=>({request_id:`r${(page-1)*50+i}`}))};};
const first=await fetchConditionPage('run',{target:'local',condition:{id:'c'}},{page:1,filter:'all'});
assert.equal(first.rows.length,200);assert.equal(first.page,1);assert.equal(first.pages,2);assert.equal(calls.length,4);
const last=await fetchConditionPage('run',{target:'local',condition:{id:'c'}},{page:2,filter:'all'});
assert.equal(last.rows.length,5);assert.equal(last.page,2);assert.equal(last.rows[0].request_id,'r200');
})()
''')


def test_request_detail_keeps_first_answer_separate_from_ttft():
    run_render_checks(r'''
(async()=>{
const r={id:'r',target:'local',scenario:'initial',status:'completed',valid:true,score:true,output:'yes',output_chars:3,first_output_ms:9,first_token_ms:3};
state.detail={id:'run',state:'completed',report:report([check('access','pass')],{requests:[r]})};
await openRequest('r');const text=$('#request-dialog').textContent;
assert.match(text,/First answer9 ms/);assert.match(text,/TTFT3 ms/);assert.doesNotMatch(text,/First answer \/ TTFT/);
})()
''',dom=True)


def test_live_condition_table_uses_retained_evidence_and_counts_the_capped_window():
    run_render_checks(r'''
const records=Array.from({length:205},(_,i)=>({id:`r${i}`,scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:true,elapsed_ms:205-i,question:`Question ${i}`}));
state.detail={id:'run',state:'running',live_requests:records};
renderTests(records,{live:true,conditions:[{condition:{id:'correctness',check:'correctness'},started:500,completed:500}]});
const key=[...conditionViews.keys()][0];conditionDialog={key,page:1,size:200,filter:'all',table:createRequestTableState(),payload:{page:1,pages:10,total:500,rows:[{request_id:'evicted',number:1,status:'passed'}]}};
renderConditionDialog();assert.match($('#test-dialog').textContent,/205 of 500 requests available/);
assert.doesNotMatch($('#test-dialog').innerHTML,/data-request="evicted"/);
updateConditionEvidence({closest:()=>$('#test-dialog')},'sort','elapsed_ms');
assert.equal($('#test-dialog .request-evidence-row').dataset.request,'r204');
updateConditionEvidence({closest:()=>$('#test-dialog')},'search','Question 204');
assert.equal($$('#test-dialog .request-evidence-row').length,1);assert.match($('#test-dialog').textContent,/Question 204/);
''',dom=True)


def test_capped_live_condition_context_does_not_invent_global_ordinal():
    run_render_checks(r'''
const r={id:'late',target:'local',scenario:'correctness',condition_id:'correctness',slot:900,status:'completed',valid:true,score:true};
state.detail={id:'run',state:'running',live_requests:[r]};renderTests([r],{live:true,conditions:[{target:'local',condition:{id:'correctness',check:'correctness'},started:1000,completed:1000}]});
conditionDialog={key:[...conditionViews.keys()][0],table:createRequestTableState(),filter:'all',payload:{rows:[],total:1000}};
const context=conditionRequestContext('late');assert.equal(context.number,null);
assert.doesNotMatch(conditionEvidenceIdentityText(context),/Request #0001|#null/);
assert.match(conditionEvidenceIdentityText(context),/available live evidence/);
''')


def test_saved_replacement_baseline_settings_override_stale_bootstrap_snapshot():
    run_render_checks(r'''
const cfg={...bootstrap.defaults,request_timeout_seconds:300,targets:[]};state.config=cfg;
state.baselines=[{id:'base',name:'Before'}];state.bootstrap={...bootstrap,baseline_configs:{base:{...cfg,request_timeout_seconds:15}}};
state.baselineConfigs=new Map([['base',{...cfg,request_timeout_seconds:300}]]);
assert.equal(renderBaselineDrift('base',cfg),'');assert.equal(state.baselineConfigs.get('base').request_timeout_seconds,300);
''')
