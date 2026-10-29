"""Round-five qualification and evidence wording regressions."""
import json
from types import SimpleNamespace

import pytest

from giraffe.load_test import measurements, score_group, test_points as load_points
from giraffe.timing import TimingHealth
from tests.test_load_test_phase_a import config, record
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


@pytest.mark.parametrize("source", ["point", "condition"])
@pytest.mark.parametrize("wrong_answer", [False, True])
def test_qualified_load_failures_survive_partial_client_delay(source, wrong_answer):
    settings = config()
    settings.load_test.pass_criteria.max_completion_ms = 100
    records = [
        record().model_copy(update={
            "id": "slow", "elapsed_ms": 200, "dispatch_ms": 0, "completed_ms": 200,
        }),
        record().model_copy(update={
            "id": "delayed", "elapsed_ms": 200, "dispatch_ms": 1000, "completed_ms": 1200,
        }),
        record().model_copy(update={
            "id": "other", "score": not wrong_answer,
            "dispatch_ms": 2000, "completed_ms": 2020,
        }),
    ]
    health = TimingHealth(0)
    health.windows = [{"start_ms": 1000, "end_ms": 1200, "lag_ms": 150}]
    run = SimpleNamespace(records=records, max_lag_ms=150, timing_health=health)
    point = load_points(settings.load_test)[0]
    point.update(
        sending_start_ms=0, sending_end_ms=2100, delivered_window_seconds=2.1,
        observation_seconds=2.1, cut_short=False, load_qualified=True,
    )
    group = score_group(settings.load_test, point, measurements(
        run, settings.targets[0], settings.load_test, point, records, "short_short",
    ), records)
    assert group["status"] == "fail"
    assert group["timing_violations"] == ["slow"]
    assert group["unattributed_timing_violations"] == ["delayed"]
    assert group["timing_quality"]["status"] == "client_delayed"

    fixture = json.dumps({
        "source": source, "wrongAnswer": wrong_answer,
        "point": {**point, "groups": [group]},
        "records": [row.model_dump(mode="json") for row in records],
    })
    run_render_checks("const fixture=" + fixture + ";" + r"""
(async()=>{
 const group=fixture.point.groups[0],metrics={timing_quality:group.timing_quality};
 if(fixture.source==='point')metrics.points=[fixture.point];
 const value=report([check('load','fail',{metrics,evidence_ids:fixture.records.map(row=>row.id)})],{overall:'fail',requests:fixture.records});
 if(fixture.source==='condition')value.conditions=[{target:'local',check_id:'load',metrics:group}];
 detail(value);
 assert.deepEqual([...checkViolationIds(value.checks[0],value)],['slow']);
 const action=$('[data-action="show-failing"]');assert(action,'qualified timing failures need an action');
 await listeners.click({target:action});
 const table=$('.check-detail .evidence-table');
 assert(table.querySelector('[data-request="slow"]'),'independently slow response stays visible');
 assert.equal(table.querySelector('[data-request="delayed"]'),null);
 assert.equal(Boolean(table.querySelector('[data-request="other"]')),fixture.wrongAnswer);
})()
""", dom=True)


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
