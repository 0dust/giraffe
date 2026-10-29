"""Round-three live-review behavior, without sockets."""
from tests.test_ux_results import run_render_checks


def test_compared_passing_hero_counts_checks_and_opens_comparison():
    run_render_checks(r'''
const checks=['access','serving','first_output','generation','correctness'].map(id=>check(id,'pass'));
const html=detail(report(checks,{baseline:{run_id:'before',status:'pass',targets:[]}}));
assert.match(html,/<h1>5 checks passed<\/h1>/);
assert.match(html,/class="button primary" data-action="show-comparison">Show comparison/);
assert.doesNotMatch(html,/Save a baseline|data-action="baseline-dialog"|Selected acceptance checks passed/);
const failed=detail(report([check('correctness','fail',{evidence_ids:['bad']})],{overall:'fail',baseline:{status:'fail'},requests:[{id:'bad',score:false,status:'completed'}]}));
assert.match(failed,/class="button primary" data-action="show-failing"/);assert.doesNotMatch(failed,/data-action="baseline-dialog"/);
''')


def test_comparison_groups_conditions_and_uses_structured_reason_values():
    run_render_checks(r'''
const rows=[
 {check:'access',scenario:'warm',metric:'latency_ms',status:'pass',baseline:12,current:13},
 {check:'generation',scenario:'warm',metric:'output_tokens_per_second',status:'pass',baseline:40,current:42},
 {check:'first_output',scenario:'cold',metric:'first_output_ms',status:'inconclusive',reason:'Only one sample.',baseline:5,current:6},
];
const value=report([],{baseline:{status:'pass',targets:[{target:'local',comparisons:rows,reason_details:[{code:'setting_differs',key:'request_timeout_seconds',baseline:15,current:300,text:'Changed backend wording.'}]}]}});
state.detail={id:'r',report:value};const html=renderComparison(value);
assert.equal((html.match(/data-comparison-group=/g)||[]).length,3);
assert.match(html,/<th[^>]*>Metric<\/th>/);assert.match(html,/data-label="Verdict">not comparable/);
assert.match(html,/Only one sample/);assert.match(html,/Request timeout differs from baseline \(5 m vs 15 s\)/);
assert.doesNotMatch(html,/Endpoint access · Generation speed|Changed backend wording|Run setting|request_timeout_seconds/);
value.baseline.targets[0].comparisons=[];
value.baseline.targets[0].load_test={status:'inconclusive',reason:'Load test comparison is unavailable: Run setting request_timeout_seconds differs.',points:[]};
value.baseline.targets[0].traffic_capacity={status:'inconclusive',reason:'Traffic comparison is unavailable: Run setting request_timeout_seconds differs.'};
const unavailable=renderComparison(value);assert.match(unavailable,/Request timeout differs from baseline/);assert.doesNotMatch(unavailable,/Run setting|request_timeout_seconds/);
''')


def test_open_limits_applies_remediation_and_can_undo_after_route():
    run_render_checks(r'''
(async()=>{
state.config={...structuredClone(bootstrap.defaults),targets:[{name:'local',url:'http://local',model:'m'}],max_requests:80,max_duration_seconds:10};
state.detail={id:'r',state:'completed',report:report([],{manifest:{config:state.config}})};
const remediation={action:'set_limit',limit:'max_duration',clear:'max_requests',minimum:60};
$('#main').innerHTML=renderRemediation(check('load','inconclusive',{remediation}));
const button=$('[data-action="edit-result-limits"]');assert(button.dataset.remediation);
updateNav=schedulePoll=updateBounds=saveDraft=captureFormDraft=()=>{};readForm=()=>({config:state.config});
renderNew=()=>{$('#main').innerHTML='<form id="new-run-form">'+renderSharedRunSettings(state.config)+'</form>';};
rerun=async()=>{location.hash='#new';};
await listeners.click({target:button,preventDefault(){}});await listeners['window:hashchange']();
assert.equal(state.config.max_requests,null);assert.equal(state.config.max_duration_seconds,60);
assert.match($('#main').textContent,/Applied: Max requests off · Max duration 1 m/);
assert.equal(document.activeElement,$('#field-max_duration_seconds'));
await listeners.click({target:$('[data-action="undo-limit-remediation"]'),preventDefault(){}});
assert.equal(state.config.max_requests,80);assert.equal(state.config.max_duration_seconds,10);
assert.doesNotMatch($('#main').textContent,/Applied:/);
state.appliedLimits={before:{max_requests:80},after:{max_requests:null}};state.config.max_requests=null;renderNew();
assert.match($('#main').textContent,/Applied:/);state.config.max_requests=20;updateSharedSettingsSummaries(state.config);
assert.doesNotMatch($('#main').textContent,/Applied:/);
})()
''',dom=True)


def test_compatibility_probe_is_preparation_and_keeps_request_detail_access():
    run_render_checks(r'''
const probe={id:'probe',target:'local',scenario:'performance_evidence_probe',condition_id:'performance_evidence_probe',status:'completed',valid:true,score:null};
const value=report([check('generation','pass')],{requests:[probe]});state.detail={id:'r',state:'completed',report:value};
const strip=renderTests([probe],{report:value});assert.doesNotMatch(strip,/data-condition-id="performance_evidence_probe"|Response validity/);
const prep=renderPreparation(state.detail);assert.match(prep,/Preparation · Compatibility probe/);assert.match(prep,/data-action="open-condition"/);
const view=[...conditionViews.values()][0];assert.equal(view.name,'Compatibility probe');assert.equal(view.checkName,'Preparation');
assert.equal(conditionInfo(probe,value).label,'Compatibility probe');
const archived={...state.detail,live_conditions:[],report:{...value,requests:[],conditions:[],observations:{targets:{local:{conditions:[{condition:{id:probe.condition_id},started:1}]}}}}};
assert.match(renderPreparation(archived),/Preparation · Compatibility probe/);assert.match(renderPreparation(archived),/1 request/);
''')


def test_empty_preparation_and_completed_builtin_have_no_phantom_or_hedges():
    run_render_checks(r'''
assert.equal(renderPreparation({id:'r',state:'completed',report:{observations:{targets:{local:{preparation:{scenario:'runtime_metrics_discovery',status:'unavailable',elapsed_seconds:0}}}}}}),'');
const html=renderPreparation({id:'r',state:'completed',report:{observations:{targets:{local:{preparations:{load_preparation_short:{status:'completed',builtin:true,reason:"These are reference token counts, not the served model's counts"}}}}}}});
assert.match(html,/Preparation complete/);assert.doesNotMatch(html,/reference token counts|served model|this does not establish a workload failure/);
''')


def test_limits_keep_planned_count_when_cap_allows_the_plan():
    run_render_checks(r'''
const config={...bootstrap.defaults,max_requests:60,request_timeout_seconds:300,concurrency:4};
assert.equal(sharedLimitsSummary(config,{planned_total:16,count_kind:'exact'}),'16 requests (≤ 60) · 5 m default request timeout · 4 at once');
assert.equal(sharedLimitsSummary(config,{planned_total:16,count_kind:'adaptive'}),'16+ requests (≤ 60) · 5 m default request timeout · 4 at once');
''')


def test_request_titles_unknown_concurrency_and_mobile_wrong_answers():
    run_render_checks(r'''
(async()=>{
assert.equal(conditionDescription({id:'unknown'},{}),'');
assert.equal(conditionDescription({id:'unknown'},{concurrency_description:'Concurrency not recorded'}),'');
state.detail={id:'r',state:'completed',report:report([],{requests:[],requests_compact:true})};
let reject;api=()=>new Promise((resolve,no)=>{reject=no;});const loading=openRequest('missing');
assert.equal($('#request-title').textContent,'Request');reject(new Error('Not retained'));await loading;
assert.equal($('#request-title').textContent,'Request unavailable');
const html=renderRequestTable([{id:'bad',status:'completed',valid:true,score:false,evaluation:{expected:'42',expected_provided:true},output:'43'}],createRequestTableState());
assert.match(html,/class="request-mobile-expected"[^>]*>Expected: 42/);
assert.match(html,/class="request-mobile-actual"[^>]*>Received: 43/);
const chips=renderRequestTable([{id:'ok',status:'completed',score:true},{id:'unsaved',status:'completed',score:null}],createRequestTableState());
assert.match(chips,/class="request-score-chip request-match"/);assert.match(chips,/class="request-score-chip request-unsaved"/);
})()
''',dom=True)


def test_restored_draft_limits_are_visible_and_reset_only_limits():
    run_render_checks(r'''
(async()=>{
bootstrap.config={...structuredClone(bootstrap.defaults),targets:[{name:'local',url:'http://local',model:'m'}]};
const restored={...structuredClone(bootstrap.config),max_requests:80,max_duration_seconds:60};
const legacy=structuredClone(restored);delete legacy.config_version;
sessionStorage={getItem:()=>JSON.stringify({config:legacy}),removeItem(){}};
api=async path=>{assert.equal(path,'/api/config/draft');return {config:restored};};
await restoreDraft();assert.equal(state.limitSource,'restored');assert.equal(state.prefillBaseline,'');
saveDraft=captureFormDraft=()=>{};readForm=()=>({config:state.config});
renderNew=()=>{$('#main').innerHTML='<form id="new-run-form">'+renderSharedRunSettings(state.config)+'</form>';};
renderNew();assert.match($('#main').textContent,/Limits from your last run/);
await listeners.click({target:$('[data-action="reset-restored-limits"]'),preventDefault(){}});
assert.equal(state.config.max_requests,bootstrap.config.max_requests);assert.equal(state.config.max_duration_seconds,bootstrap.config.max_duration_seconds);
assert.equal(state.config.targets[0].model,'m');assert.doesNotMatch($('#main').textContent,/Limits from your last run/);
})()
''',dom=True)
