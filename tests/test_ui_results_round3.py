"""Confirmed round-three failures reproduced through the real UI renderer."""
import pytest

from tests.test_ui_report_tabs import PERFORMANCE
from tests.test_ux_results import run_render_checks


def test_stop_confirmation_timer_cannot_redraw_a_different_route():
    run_render_checks(r"""
(async()=>{
 let timer;Date.now=()=>1000;const cancelled=[];setTimeout=(fn,ms)=>{if(ms===4000)timer=fn;return 7;};clearTimeout=id=>cancelled.push(id);
 clearPoll=schedulePoll=updateNav=()=>{};refreshRuns=async()=>{};window.matchMedia=()=>({matches:true});
 location.hash='#runs/live/results';state.detail={id:'live',state:'running',config:{targets:[]},events:[],progress:{}};renderDetail();
 await confirmStop();const stale=timer;assert(stale);
 location.hash='#runs';await route();const destination=$('#main').innerHTML;assert(cancelled.includes(7));
 stale();assert.equal(location.hash,'#runs');assert.equal($('#main').innerHTML,destination);assert.equal($('#main h1').textContent,'Runs');
})()
""", dom=True)


def test_stop_confirmation_rerender_invalidates_old_callback():
    run_render_checks(r"""
(async()=>{
 const timers=[];let now=1000;Date.now=()=>now;setTimeout=(fn,ms)=>{if(ms>0)timers.push(fn);return timers.length;};clearTimeout=()=>{};
 state.detail={id:'live',state:'running',config:{targets:[]},events:[],progress:{}};renderDetail();await confirmStop();const stale=timers.at(-1);
 let calls=0;const original=renderDetail;renderDetail=()=>{calls++;return original();};now+=100;renderDetail();const count=calls;
 stale();assert.equal(calls,count);assert.match($('[data-action="stop"]').textContent,/Confirm stop/);
 now=5000;timers.at(-1)();assert.match($('[data-action="stop"]').textContent,/^Stop run$/);
})()
""", dom=True)


@pytest.mark.parametrize('source', ['check', 'scenario', 'condition', 'load_group'])
def test_timing_only_failures_have_action_filtered_rows_and_reasons(source):
    run_render_checks(r"""
(async()=>{
 const source=SOURCE,metrics=source==='check'?{violations:['slow']}:source==='scenario'?{scenarios:{warm:{violations:['slow']}}}:source==='load_group'?{points:[{groups:[{timing_violations:['slow']}]}]}:{};
 const value=report([check('generation','fail',{summary:'Response time exceeded the configured limit.',metrics})],{overall:'fail',requests:[
  {id:'slow',target:'local',status:'completed',valid:true,score:true,elapsed_ms:12300,output:'Correct'},
  {id:'good',target:'local',status:'completed',valid:true,score:true,elapsed_ms:200,output:'Correct',check_ids:['generation']}
 ,{id:'client',target:'local',status:'completed',valid:true,score:true,elapsed_ms:20000,output:'Correct',check_ids:['generation']}
 ]});metrics.unattributed_timing_violations=['client'];value.manifest.config.limits={latency_ms:10000};
 if(source==='condition')value.conditions=[{target:'local',check_id:'generation',condition_id:'warm',metrics:{violations:['slow']}}];
 detail(value);const action=$('[data-action="show-failing"]');assert(action,'timing failures need an action');await listeners.click({target:action});
 const table=$('.check-detail .evidence-table');assert(table.querySelector('[data-request="slow"]'));assert.equal(table.querySelector('[data-request="good"]'),null);assert.equal(table.querySelector('[data-request="client"]'),null);
 assert.match(table.textContent,/Too slow: response time 12\.3 s > 10 s/);assert.doesNotMatch(table.textContent,/Incorrect/);
})()
""".replace('SOURCE', repr(source)), dom=True)


def test_findings_distinguish_unsaved_truncated_and_genuinely_empty_answers():
    run_render_checks(r"""
const record={id:'answer',target:'local',status:'completed',score:false,valid:false,output:'',output_chars:12,evaluation:{expected:'correct',expected_provided:true}};
const value=report([check('correctness','fail',{evidence_ids:['answer']})],{overall:'fail',requests:[record]});value.manifest.config.retention='none';
detail(value);assert.match($('.finding-requests').textContent,/Response not saved \(retention: none\)/);assert($('.finding-requests .hint'));
record.evaluation={tool_expectation:{expected_calls:[{name:'weather',arguments:{}}]}};record.output_chars=0;record.valid=true;
detail(value);assert.match($('.answer-difference').textContent,/Response not saved \(retention: none\)/);assert.doesNotMatch($('.answer-difference').childNodes.at(-1).textContent,/tool_calls/);
value.manifest.config.retention='all';record.evaluation={expected:'correct',expected_provided:true};record.valid=false;
detail(value);assert.match($('.finding-requests').textContent,/Empty response/);assert.doesNotMatch($('.finding-requests').textContent,/not saved/);
record.output_chars=12;value.manifest.storage={response_bodies_omitted:1};detail(value);assert.match($('.finding-requests').textContent,/Response not saved \(storage limit\)/);
record.output='part';detail(value);assert.match($('.finding-requests').textContent,/part/);assert.match($('.finding-requests').textContent,/Response truncated/);
record.output='';record.output_chars=0;record.evaluation={scorer:'tool',tool_expectation:{expected_calls:[{name:'weather',arguments:{}}]}};detail(value);assert.match($('.finding-requests').textContent,/Response evidence unavailable \(storage limit\)/);assert.doesNotMatch($('.finding-requests').textContent,/Empty response/);
""", dom=True)


@pytest.mark.parametrize('before,after,verdict,delta', [(1, 4, 'better', '+300%'), (4, 1, 'worse', '-75%')])
def test_capacity_comparison_direction_and_delta(before, after, verdict, delta):
    run_render_checks(r"""
const before=BEFORE,after=AFTER,expected=VERDICT;
const capacity={status:after>before?'pass':'fail',baseline_highest_acceptable_rate_rps:before,current_highest_acceptable_rate_rps:after,delta_rps:after-before,change_percent:(after-before)/before*100,reason:'Matched complete stages.'};
const value=report([check('access','pass')],{baseline:{run_id:'old',targets:[{target:'local',traffic_capacity:capacity}]}});detail(value);
const html=renderComparison(value);assert.match(html,new RegExp('>'+expected+'<'));assert(html.includes(DELTA));assert.match(renderBaselineSummary(value),new RegExp('1 '+expected));
for(const metric of ['throughput','output_tokens_per_second','generation_tokens_per_second']){
 const row={metric,status:after>before?'pass':'fail',baseline:before,current:after,threshold:20,worsening:after>before?-300:75,worsening_interval_95:after>before?[-310,-290]:[70,80]};
 assert.equal(comparisonVerdict(row),expected);assert.equal(comparisonVerdict({...row,status:'inconclusive'}),'not comparable');const table=comparisonTable([row]);assert(table.includes('>'+expected+'<'));assert(table.includes(DELTA));
}
""".replace('BEFORE', str(before)).replace('AFTER', str(after)).replace('VERDICT', repr(verdict)).replace('DELTA', repr(delta)))


def test_performance_baseline_route_focuses_only_the_visible_panel():
    run_render_checks(PERFORMANCE + r"""
(async()=>{
 clearPoll=schedulePoll=updateNav=()=>{};
 const value=report([check('access','pass')],{performance:performanceFixture(),baseline:{run_id:'old',targets:[]}});
 api=async()=>({id:'B',state:'completed',report:value});location.hash='#runs/B/performance/baseline';await route();
 const focused=document.activeElement;assert.equal(reportView.tab,'performance');assert($('#report-performance').contains(focused));assert.equal(focused.closest('[hidden]'),null);assert.equal(focused.closest('details:not([open])'),null);
 assert($('#performance-baseline').contains(focused));
 location.hash='#runs/B/results/baseline';await route();assert($('#report-results').contains(document.activeElement));assert.equal(document.activeElement.closest('[hidden]'),null);
})()
""", dom=True)
