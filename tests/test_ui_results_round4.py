"""Round-four evidence selection, applied limits and deferred navigation."""
import pytest

from tests.test_ui_report_tabs import PERFORMANCE
from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize('check_id', ['capacity', 'recovery'])
def test_capacity_recovery_ids_enable_failure_action_but_legacy_counts_do_not(check_id):
    run_render_checks(r"""
(async()=>{
 const value=report([check(CHECK,'fail',{evidence_ids:['slow'],metrics:{timing_violations:1,violations:['slow']}})],{overall:'fail',requests:[{id:'slow',target:'local',status:'completed',valid:true,score:true,elapsed_ms:12000}]});
 detail(value);assert($('[data-action="show-failing"]'));await listeners.click({target:$('[data-action="show-failing"]')});assert($('.evidence-table [data-request="slow"]'));
 delete value.checks[0].metrics.violations;detail(value);assert.equal($('[data-action="show-failing"]'),null);
})()
""".replace('CHECK', repr(check_id)), dom=True)


def test_timing_reasons_prefer_applied_manifest_limits_and_keep_legacy_fallback():
    run_render_checks(r"""
const value=report([check('first_output','fail',{evidence_ids:['slow'],metrics:{violations:['slow']}})],{overall:'fail',requests:[{id:'slow',target:'local',status:'completed',valid:true,score:true,first_output_ms:12300}]});
value.manifest.config.limits={};value.manifest.effective_checks={first_output:{limits:{first_output_ms:10000}}};
detail(value);state.selectedCheck='local:first_output';let html=renderChecks(value);assert.match(html,/Too slow: first output 12\.3 s &gt; 10 s/);
value.manifest.config.limits.first_output_ms=20000;html=renderChecks(value);assert.match(html,/12\.3 s &gt; 10 s/);
value.manifest.config.limits={};value.checks[0].id='generation';state.selectedCheck='local:generation';value.requests[0].max_stream_gap_ms=6000;value.manifest.effective_checks={generation:{limits:{stream_gap_ms:5000}}};
assert.match(renderChecks(value),/Too slow: stream pause 6 s &gt; 5 s/);
delete value.manifest.effective_checks;value.manifest.config.limits.stream_gap_ms=5000;assert.match(renderChecks(value),/stream pause 6 s &gt; 5 s/);
value.manifest.config.limits={stream_gap_ms:5000};value.manifest.default_check_limits={generation:['stream_gap_ms']};assert.match(renderChecks(value),/stream pause 6 s &gt; 5 s/);
value.requests[0].generation_tokens_per_second=2;value.manifest.effective_checks={generation:{limits:{min_output_tokens_per_second:4}}};assert.match(renderChecks(value),/generation 2 tok\/s &lt; 4 tok\/s/);
""")


@pytest.mark.parametrize('qualified', [['independent'], []])
def test_qualified_workload_violations_replace_raw_ids_even_when_empty(qualified):
    run_render_checks(r"""
const value=report([check('tools','fail',{evidence_ids:['independent','client'],metrics:{timing_violations:['independent','client'],qualified_timing_violations:QUALIFIED}})],{overall:'fail',requests:['independent','client'].map(id=>({id,target:'local',status:'completed',valid:true,score:true,elapsed_ms:12000}))});
detail(value);state.selectedCheck='local:tools';state.failingRequestsOnly=true;const html=renderChecks(value);
assert.doesNotMatch(html.slice(html.indexOf('class="evidence-table"')),/data-request="client"/);
assert.equal(checkHasFailingRequests(value.checks[0],value),QUALIFIED.length>0);
""".replace('QUALIFIED', repr(qualified)))


@pytest.mark.parametrize('check_id', ['first_output', 'generation', 'capacity'])
def test_check_tables_do_not_reintroduce_excluded_initial_or_nonstage_records(check_id):
    run_render_checks(r"""
const value=report([check(CHECK,'fail',{evidence_ids:['measured'],metrics:{violations:['measured']}})],{overall:'fail',requests:[
 {id:'cold',target:'local',check_ids:[CHECK],scenario:'initial',status:'timeout',valid:false,score:null},
 {id:'measured',target:'local',check_ids:[CHECK],scenario:CHECK==='capacity'?'traffic_short':'generation',status:'completed',valid:true,score:true,elapsed_ms:12000}
]});detail(value);state.selectedCheck='local:'+CHECK;
for(const filtered of [false,true]){state.failingRequestsOnly=filtered;const html=renderCheck(value.checks[0],value);assert.match(html,/data-request="measured"/);assert.doesNotMatch(html,/data-request="cold"/);assert.match(html,/1 recorded/);}
""".replace('CHECK', repr(check_id)))


@pytest.mark.parametrize('leave', [False, True])
def test_async_performance_baseline_reveal_runs_once_for_current_route_only(leave):
    run_render_checks(PERFORMANCE + r"""
(async()=>{
 fetch=()=>{};clearPoll=schedulePoll=updateNav=()=>{};let finish,focuses=0;
 const originalFocus=Object.getPrototypeOf($('#main')).focus;Object.getPrototypeOf($('#main')).focus=function(...args){if(this.closest('#performance-baseline'))focuses++;return originalFocus.apply(this,args);};
 const original=renderPerformance;renderPerformance=()=>reportView.performance?original():'<section id="report-performance" role="tabpanel" aria-labelledby="tab-performance">Loading</section>';
 api=async path=>path.includes('/performance')?new Promise(resolve=>finish=resolve):({id:'B',state:'completed',report:report([check('access','pass')])});
 location.hash='#runs/B/performance/baseline';await route();assert.equal(focuses,0);
 if(LEAVE){location.hash='#runs/B/results';await route();}
 finish(performanceFixture());for(let i=0;i<8;i++)await Promise.resolve();
 assert.equal(focuses,LEAVE?0:1);if(!LEAVE)assert($('#performance-baseline').contains(document.activeElement));
 renderDetail();assert.equal(focuses,LEAVE?0:1);
})()
""".replace('LEAVE', str(leave).lower()), dom=True)


def test_same_run_tab_hash_change_preserves_armed_stop_confirmation():
    run_render_checks(r"""
(async()=>{
 let now=1000;Date.now=()=>now;setTimeout=()=>1;clearTimeout=()=>{};let stops=0;api=async()=>{stops++;return {state:'stopping'};};schedulePoll=()=>{};
 location.hash='#runs/live/results';state.detail={id:'live',state:'running',config:{targets:[]},progress:{},events:[]};renderDetail();await confirmStop();const confirmation=state.stopConfirmation;
 chooseReportTab('performance');await listeners['window:hashchange']();assert.equal(state.stopConfirmation,confirmation);assert.match($('[data-action="stop"]').textContent,/Confirm stop/);
 now+=301;await confirmStop();assert.equal(stops,1);
})()
""", dom=True)


def test_none_retention_invalid_zero_character_response_is_empty():
    run_render_checks(r"""
const value=report([check('correctness','fail',{evidence_ids:['empty']})],{overall:'fail',requests:[{id:'empty',target:'local',status:'completed',valid:false,score:false,output:'',output_chars:0,evaluation:{expected:'answer',expected_provided:true}}]});value.manifest.config.retention='none';
const html=detail(value);assert.match(html,/Received:<\/strong>.*Empty response/);assert.doesNotMatch(html,/Response not saved/);
""")
