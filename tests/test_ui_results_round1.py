"""Round-one regression coverage for actionable findings and safe navigation."""
from tests.test_ux_results import run_render_checks


def test_hashchange_after_tab_navigation_does_not_steal_focus():
    run_render_checks(r"""
(async()=>{
 detail(report([check('access','pass')]));chooseReportTab('performance',{focus:true});
 assert.equal(document.activeElement,$('#tab-performance'));await listeners['window:hashchange']();
 assert.equal(document.activeElement,$('#tab-performance'));
})()
""", dom=True)


def test_user_stop_uses_exact_reason_and_authoritative_finished_count():
    run_render_checks(r"""
const value=report([check('generation','inconclusive')],{overall:'inconclusive',abort_reason:'cancelled by user',requests:[{status:'completed'}]});
const html=detail(value,{progress:{completed:16},request_plan:{planned_total:20}});
assert.match(html,/16 of 20 planned requests finished/);
for(const reason of ['User queue exhausted','Not cancelled by user','cancelled by user: memory limit'])assert.equal(userStopped({...value,abort_reason:reason}),false);
assert.equal(userStopped({...value,abort_reason:'Cancelled by user'}),true);
state.detail={id:'old',state:'cancelled'};assert.equal(userStopped({...value,abort_reason:null}),false);
""")


def test_guidance_is_informative_and_keeps_abort_reason_and_sentence_case():
    run_render_checks(r"""
const html=detail(report([check('generation','inconclusive',{summary:'Timing observed; no acceptance limit configured. Set limits.latency_ms to grade responses.'})],{overall:'inconclusive',observations:{summary:'Only 2 requests completed.'},abort_reason:'Maximum duration reached.'}));
assert.doesNotMatch(html,/Inspect the findings and request evidence below/);
assert.match(html.split('report-toolbar')[0],/Maximum duration reached/);
assert.match(html,/\. Set limits.latency_ms/);assert.doesNotMatch(html,/\. set limits/);
""")


def test_live_progress_falls_back_without_unknown_and_uses_bootstrap_plan():
    run_render_checks(r"""
state.detail={id:'live',state:'running',config:{targets:[],max_requests:50},progress:{completed:3,elapsed_seconds:2},events:[]};
renderLive();assert.match($('#main').textContent,/3 requests finished · 2s · limit 50/);assert.doesNotMatch($('#main').textContent,/of unknown/);
state.bootstrap.config=state.detail.config;state.bootstrap.request_plan={planned_total:12};renderLive();assert.match($('#main').textContent,/3 of ~12 planned requests/);
""", dom=True)


def test_stop_double_click_and_finish_transition_cannot_trigger_other_actions():
    run_render_checks(r"""
(async()=>{
 let now=1000;Date.now=()=>now;let timer;setTimeout=(fn,ms)=>{if(ms===4000)timer=fn;};let cancelled=0,reruns=0;api=async()=>{cancelled++;return {state:'stopping'};};rerun=async()=>reruns++;schedulePoll=()=>{};
 state.detail={id:'live',state:'running',config:{targets:[]},progress:{},events:[]};renderDetail();await confirmStop();now+=100;await confirmStop();assert.equal(cancelled,0);
 timer();assert($('[data-action="stop"] svg'));await confirmStop();now+=301;await confirmStop();assert.equal(cancelled,1);
 state.detail={id:'live',state:'completed',report:report([check('access','pass')])};renderDetail();
 await listeners.click({target:$('[data-action="rerun"]')});assert.equal(reruns,0);
 now+=601;await listeners.click({target:$('[data-action="rerun"]')});assert.equal(reruns,1);
})()
""", dom=True)


def test_passed_disclosure_survives_check_selection_and_compact_table_is_honest():
    run_render_checks(r"""
(async()=>{
 const value=report([check('access','pass',{evidence_ids:['one']}),check('correctness','fail',{evidence_ids:['bad']})],{overall:'fail',requests_compact:true,requests:[{id:'one',target:'local',status:'completed',score:false},{id:'bad',target:'local',status:'completed',score:false}]});detail(value);
 $('.passed-checks').open=true;await listeners.click({target:$('[data-check="local:access"]')});
 assert($('.passed-checks').open);assert.doesNotMatch($('.check-detail').textContent,/Expected: Not retained|Received: Not retained/);assert.match($('.check-detail').textContent,/Open request|Loading expected/);
})()
""", dom=True)


def test_failure_action_filters_request_table_and_saved_baseline_compares_new_run():
    run_render_checks(r"""
(async()=>{
 const requests=[{id:'bad',target:'local',status:'completed',score:false,evaluation:{messages:[{role:'user',content:'What is the capital of France?'}],expected:'Paris',expected_provided:true},output:'Lyon'},{id:'good',target:'local',status:'completed',score:true,output:'Paris'}];
 const value=report([check('correctness','fail',{evidence_ids:['bad','good']})],{overall:'fail',requests});detail(value);
 await listeners.click({target:$('[data-action="show-failing"]')});const table=$('.evidence-table');assert(table);assert(table.querySelector('[data-request="bad"]'));assert.equal(table.querySelector('[data-request="good"]'),null);
 assert.match(table.textContent,/What is the capital of France\?/);
 await listeners['window:hashchange']();assert.equal(document.activeElement,table);assert.equal(table.querySelector('[data-request="good"]'),null);
 state.baselines=[{id:'base',run_id:'saved-run',name:'Before'}];detail(report([check('access','pass')]));assert.match($('.result-heading').textContent,/Compare a new run/);
 detail(report([check('generation','inconclusive')]));assert.equal($('[data-action="show-failing"]'),null);assert.match($('.result-heading').textContent,/Show inconclusive checks/);
})()
""", dom=True)


def test_schema_rubric_and_tool_only_findings_have_meaningful_evidence():
    run_render_checks(r"""
for(const evaluation of [{scorer:'json',schema:{type:'object'},expected_provided:false},{scorer:'judge',rubric:'Explain the calculation',expected_provided:false}]){
 const html=detail(report([check('correctness','fail',{evidence_ids:['bad']})],{overall:'fail',requests:[{id:'bad',target:'local',status:'completed',score:false,evaluation,score_message:'Answer did not satisfy the requirement.',output:'wrong'}]}));
 assert.doesNotMatch(html,/Expected:<\/strong> Not retained/);assert.match(html,/Answer did not satisfy the requirement/);assert.match(html,/Schema:|Rubric:/);
}
""")


def test_baseline_groups_condition_rows_discloses_noise_and_links_to_section():
    run_render_checks(r"""
(async()=>{
 const row=(metric,status,extra={})=>({check:'generation',scenario:'warm',metric,status,baseline:10,current:10,reason:'No material worsening detected in these samples.',...extra});
 const baseline={run_id:'old',status:'pass',targets:[{target:'local',comparisons:[row('latency_ms','pass'),row('first_output_ms','inconclusive',{reason:'Too few samples.'})],load_test:{status:'pass',reason:'Load qualification improved.',points:[{status:'pass',reason:'Current load passes.',baseline:{type:'combinations',shape:'short_short',pattern:'concurrent',level:2,status:'fail'},current:{type:'combinations',shape:'short_short',pattern:'concurrent',level:2,status:'pass'}}]}}]};
 detail(report([check('access','pass')],{baseline}));assert.match($('.baseline-delta').textContent,/1 better/);
 const html=renderComparison(state.detail.report);assert.match(html,/no material change/);assert.match(html,/data-label="Verdict">not comparable/);assert.match(html,/Too few samples/);assert.doesNotMatch(html,/No material worsening detected in these samples/);assert.match(html,/Load qualification improved/);assert.match(html,/data-comparison-condition/);
 const link=$('.baseline-delta a');assert.equal(link.getAttribute('href'),'#runs/saved-run/results/baseline');await listeners.click({target:link,preventDefault(){}});await route();assert($('#report-baseline-reference').open);assert.equal(document.activeElement,$('#report-baseline-reference summary'));
})()
""", dom=True)


def test_live_plan_fetch_retries_after_returning_to_same_run():
    run_render_checks(r"""
(async()=>{
 fetch=()=>{};clearPoll=schedulePoll=updateNav=loadPerformance=()=>{};const pending=[];
 const live={id:'A',state:'running',config:{checks:['access'],targets:[],max_requests:100},progress:{completed:3},events:[]};
 api=async(path,options)=>{
  if(path==='/api/config/plan'){assert.equal(options.method,'POST');assert.equal(JSON.parse(options.body).max_requests,100);return new Promise(resolve=>pending.push(resolve));}
  return path==='/api/runs/A'?structuredClone(live):{id:'B',state:'completed',report:report([check('access','pass')])};
 };
 location.hash='#runs/A/results';await route();assert.equal(pending.length,1);
 location.hash='#runs/B/results';await route();location.hash='#runs/A/results';await route();assert.equal(pending.length,2);
 pending[0]({request_plan:{planned_total:999}});await Promise.resolve();await Promise.resolve();assert.equal(plannedRequests(),null);
 pending[1]({request_plan:{planned_total:42}});await Promise.resolve();await Promise.resolve();await Promise.resolve();
 assert.equal(plannedRequests(),42);assert.match($('#main').textContent,/3 of ~42 planned requests/);assert.doesNotMatch($('#main').textContent,/999/);
})()
""", dom=True)


def test_baseline_94_rows_stay_grouped_and_summary_counts_all_evidence():
    run_render_checks(r"""
const comparisons=Array.from({length:94},(_,index)=>({check:'generation',scenario:'warm',metric:'metric_'+index,status:index<64?'pass':'inconclusive',baseline:10,current:10,reason:index<64?'No material worsening detected in these samples.':'No eligible samples.'}));
const value=report([check('access','pass')],{baseline:{run_id:'before-upgrade',targets:[{target:'local',comparisons}]}});
const hero=detail(value);assert.match(hero,/vs baseline before-upgrade: no regressions \(64 unchanged, 30 not comparable\)/);
const html=renderComparison(value);assert.equal((html.match(/data-comparison-condition=/g)||[]).length,1);assert.equal((html.match(/class="comparison-metric"/g)||[]).length,94);for(let index=0;index<94;index++)assert(html.includes('Metric '+index));
assert.equal(comparisonConditionGroups(value.baseline.targets[0],value)[0].rows.length,94);assert.equal((html.match(/data-label="Verdict">not comparable/g)||[]).length,30);assert.match(html,/No eligible samples/);
assert.match(html,/class="comparison-reason"/);assert.doesNotMatch(html,/No material worsening detected in these samples\.|Entry \d/);
""")
