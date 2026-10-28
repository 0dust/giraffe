"""Round-five qualification and evidence wording regressions."""
import pytest

from tests.test_ux_results import run_render_checks


def test_client_delayed_workload_without_qualified_ids_does_not_mark_raw_requests():
    run_render_checks(r"""
const value=report([check('tools','fail',{evidence_ids:['client','wrong'],metrics:{timing_quality:{status:'client_delayed'},timing_violations:['client']}})],{overall:'fail',requests:[
 {id:'client',target:'local',status:'completed',valid:true,score:true,elapsed_ms:12000},
 {id:'wrong',target:'local',status:'completed',valid:true,score:false,output:'wrong'}
]});detail(value);assert.equal(checkViolationIds(value.checks[0],value).size,0);
state.selectedCheck='local:tools';state.failingRequestsOnly=true;const html=renderCheck(value.checks[0],value);
assert.doesNotMatch(html,/data-request="client"|Too slow/);assert.match(html,/data-request="wrong"/);
""")


@pytest.mark.parametrize('status', ['failed', 'timeout'])
def test_transport_failure_is_not_an_empty_answer(status):
    run_render_checks(r"""
const value=report([check('correctness','fail',{evidence_ids:['failed']})],{overall:'fail',requests:[{id:'failed',target:'local',status:STATUS,valid:false,score:false,output:'',output_chars:0,error:'Endpoint <unreachable>',evaluation:{expected:'42',expected_provided:true}}]});value.manifest.config.retention='none';
const html=detail(value);assert.match(html,/No response received/);assert.match(html,/Endpoint &lt;unreachable&gt;/);assert.doesNotMatch(html,/Empty response|Endpoint <unreachable>/);
""".replace('STATUS', repr(status)))


def test_passing_capacity_keeps_stage_order_and_a_neutral_over_limit_note():
    run_render_checks(r"""
const value=report([check('capacity','pass',{evidence_ids:['accepted','over'],metrics:{highest_acceptable_rate_rps:1,traffic_stages:[{rate_rps:4,violations:['over']}]}})],{requests:[
 {id:'accepted',target:'local',status:'completed',valid:true,score:true,elapsed_ms:20,traffic_rate_rps:1},
 {id:'over',target:'local',status:'completed',valid:true,score:true,elapsed_ms:12000,traffic_rate_rps:4}
]});value.manifest.config.limits={latency_ms:10000};detail(value);const html=renderCheck(value.checks[0],value);
assert(html.indexOf('data-request="accepted"')<html.indexOf('data-request="over"'));
assert.match(html,/Over limit \(stage above accepted rate\)/);assert.doesNotMatch(html,/Too slow|class="status fail"/);
""")


@pytest.mark.parametrize('legacy', [False, True])
def test_capacity_labels_use_arrival_timing_and_accept_old_aggregate_ids(legacy):
    run_render_checks(r"""
(async()=>{
 const metrics=LEGACY?{violations:['slow']}:{traffic_stages:[{violations:['slow']}]};
 const value=report([check('capacity','fail',{evidence_ids:['slow'],metrics})],{overall:'fail',requests:[{id:'slow',target:'local',status:'completed',valid:true,score:true,first_output_ms:500,elapsed_ms:600,arrival_first_output_ms:12300,arrival_elapsed_ms:12400}]});
 value.manifest.effective_checks={capacity:{limits:{first_output_ms:10000,latency_ms:11000}}};detail(value);await listeners.click({target:$('[data-action="show-failing"]')});
 assert.match($('.evidence-table').textContent,/first output 12\.3 s > 10 s/);assert.match($('.evidence-table').textContent,/response time 12\.4 s > 11 s/);
})()
""".replace('LEGACY', str(legacy).lower()), dom=True)
