"""Explain confirmed optional capability rejections in saved request evidence."""

import json

import pytest

from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize(
    ("overrides", "assertions"),
    [
        pytest.param(
            {},
            r"""
assert.match(html,/Unsupported optional capability: the endpoint rejected <code>logprobs<\/code> \(HTTP 400\)/);
assert.match(html,/per-token timing stays unavailable/);
assert.match(html,/class="notice[^"]*"[^>]*>HTTP 400<\/div>/);
assert.doesNotMatch(html,/class="notice error"/);
""",
            id="confirmed-logprobs-rejection",
        ),
        pytest.param(
            {
                "http_status": 422,
                "error": "HTTP 422",
                "workload": {
                    "performance_evidence_probe": True,
                    "rejected_evidence_field": "stream_options",
                },
            },
            r"""
assert.match(html,/Unsupported optional capability: the endpoint rejected <code>stream_options<\/code> \(HTTP 422\)/);
assert.doesNotMatch(html,/streamed token usage.*unavailable/);
assert.match(html,/This setup probe is not a serving failure\.<\/div>/);
assert.match(html,/class="notice[^"]*"[^>]*>HTTP 422<\/div>/);
assert.doesNotMatch(html,/class="notice error"/);
""",
            id="confirmed-stream-options-rejection",
        ),
        pytest.param(
            {"workload": {"performance_evidence_probe": True}},
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 400/);
""",
            id="unconfirmed-rejection",
        ),
        pytest.param(
            {"http_status": 500, "error": "HTTP 500"},
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 500/);
""",
            id="server-error",
        ),
        pytest.param(
            {"scenario": "initial"},
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 400/);
""",
            id="non-probe-scenario",
        ),
        pytest.param(
            {
                "workload": {
                    "performance_evidence_probe": True,
                    "rejected_evidence_field": "temperature",
                }
            },
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 400/);
""",
            id="unknown-field",
        ),
    ],
)
def test_optional_probe_rejection_notice(overrides, assertions):
    record = {
        "id": "probe",
        "target": "local",
        "scenario": "performance_evidence_probe",
        "status": "failed",
        "http_status": 400,
        "error": "HTTP 400",
        "workload": {
            "performance_evidence_probe": True,
            "rejected_evidence_field": "logprobs",
        },
        **overrides,
    }
    run_render_checks(
        "(async()=>{\nconst r=" + json.dumps(record) + r""";
state.detail={report:{manifest:{config:{retention:'all'}},requests:[r]}};
nodes['#request-dialog']={innerHTML:'',showModal(){this.shown=true;}};
await openRequest('probe');
const html=nodes['#request-dialog'].innerHTML;
assert(nodes['#request-dialog'].shown);
""" + assertions + "\n})();"
    )


@pytest.mark.parametrize("status", ["passed", "inconclusive"])
@pytest.mark.parametrize("source", ["saved", "live", "fallback"])
def test_probe_request_uses_condition_verdict_and_visible_reason(status, source):
    run_render_checks("const status=" + json.dumps(status) + ";const source=" + json.dumps(source) + r''';
(async()=>{
const r={id:'probe',target:'local',scenario:'performance_evidence_probe',condition_id:'performance_evidence_probe',status:'completed',valid:true,score:null,score_message:'No semantic scorer.',elapsed_ms:10,evaluation:{scorer:'none',messages:[]}};
const row={target:'local',condition_id:r.scenario,condition:{id:r.scenario,label:'Streamed token and usage support',parameters:{concurrency:1,same_prompt:true}},status,reason:'Diagnostic <details> & evidence',planned:1};
const other={...row,target:'other',status:'passed',reason:'Wrong target reason'};
const value=report([],{requests:[r],conditions:source==='saved'?[other,row]:[],observations:{targets:{local:{token_evidence:{reason:'Fallback <reason>'}}}}});
state.detail={id:'saved',state:'completed',report:value,live_conditions:source==='live'?[other,{...row,status:'finished',aggregate_status:status}]:[]};
if(source==='fallback')r.condition_summary={...row,reason:null};
nodes['#request-dialog']={innerHTML:'',showModal(){this.shown=true;}};
await openRequest('probe');
const html=nodes['#request-dialog'].innerHTML;
assert.match(html,/Request 1 of 1/);
const reason=source==='fallback'?'Fallback &lt;reason&gt;':'Diagnostic &lt;details&gt; &amp; evidence';
assert(html.includes(`</p><p class="${status==='passed'?'hint':'notice warning'}">${reason}</p><section class="request-sent">`));
assert.doesNotMatch(html,/Wrong target reason|No semantic scorer\./);
assert.match(html,/Answer unscored/);assert.match(html,/<h3>Answer check<\/h3><p>No answer check<\/p>/);
const expected=status==='passed'?'pass':'inconclusive';
assert.equal(requestStatus(r),expected);assert.equal(requestOutcome(r),expected);
assert(requestIdentityContent(r,[r]).includes(status==='passed'?'Passed':'Inconclusive'));
const mosaic=renderMosaic([r]);
assert(mosaic.includes(`class="request-spot ${expected}`));
assert.match(mosaic,/Passed/);assert.match(mosaic,/Inconclusive/);
assert.equal(requestOutcome({...r,scenario:'warm'}),'inconclusive');
})();
''')


def test_unavailable_optional_preparation_stays_visible_after_later_preparation():
    run_render_checks(r'''
state.detail={id:'done',state:'completed',report:{observations:{targets:{local:{preparations:{
runtime_metrics_discovery:{step:'Runtime telemetry capability',status:'unavailable',reason:'No metrics exporter',elapsed_seconds:1},
performance_evidence_probe:{step:'Streamed token and usage support',status:'unavailable',reason:'The stream did not provide token boundaries.',elapsed_seconds:1},
context_preparation:{step:'Ready',status:'completed',elapsed_seconds:2}
}}}}}};
const html=renderPreparation();
assert.match(html,/<section class="run-preparation"[^>]*data-status="unavailable"/);
assert.match(html,/Optional measurements unavailable/);
assert.match(html,/The stream did not provide token boundaries\./);
assert.doesNotMatch(html,/No metrics exporter|Runtime telemetry capability/);
assert.match(html,/Unsupported measurements stay unknown; this does not establish a workload failure\./);
assert.doesNotMatch(html,/Supported measurements are available/);
state.detail.report.observations.targets.local.preparations.performance_evidence_probe.status='completed';
assert.doesNotMatch(renderPreparation(),/Optional measurements unavailable/);
''')


def test_performance_measurement_selector_excludes_only_support_probe():
    run_render_checks(r'''
const scenarios=['performance_evidence_probe','initial','warm'];
const target={cohorts:scenarios.map(id=>({id,scenarios:[id],measurement_scope:id!=='performance_evidence_probe'})),windows:scenarios.map(id=>({id,cohort_id:id,label:id,request_count:1}))};
const html=performanceWindowOptions(target,target.windows[1]);
assert.doesNotMatch(html,/performance_evidence_probe/);
assert.match(html,/value="initial"/);assert.match(html,/value="warm"/);
''')


@pytest.mark.parametrize(("planned", "started", "single"), [
    (None, 1, True), (1, 1, True), (None, 2, False), (2, 1, False), (0, 1, False),
])
def test_probe_same_prompt_describes_single_observed_request(planned, started, single):
    run_render_checks("const planned=" + json.dumps(planned) + ";const started=" + json.dumps(started)
                      + ";const single=" + json.dumps(single) + r''';
const r={id:'probe',target:'local',scenario:'performance_evidence_probe',status:'completed'};
const row={target:'local',condition_id:r.scenario,planned,started,
condition:{id:r.scenario,parameters:{concurrency:1,same_prompt:true}}};
state.detail={state:'completed',report:{requests:[r],conditions:[row]}};
const html=renderRequestContext(r,[r]);
assert(html.includes(single?'Single request':'Yes · repeated across this test'));
assert(!html.includes(single?'Yes · repeated across this test':'Single request'));
''')


@pytest.mark.parametrize("selection", ["default_window", "selected_window", "argument", "legacy", "eligible", "mixed"])
def test_performance_dropdown_retains_selected_and_legacy_windows(selection):
    run_render_checks("const selection=" + json.dumps(selection) + r''';
const probe={id:'probe',scenarios:['performance_evidence_probe'],measurement_scope:false};
const target={cohorts:[probe,{id:'initial',measurement_scope:true}],windows:[
{id:'full:probe',cohort_id:'probe',label:'Probe',request_count:1},
{id:'full:initial',cohort_id:'initial',label:'Initial',request_count:1}]};
if(['default_window','selected_window'].includes(selection))target[selection]='full:probe';
if(selection==='legacy')delete probe.measurement_scope;
if(selection==='eligible'||selection==='mixed')probe.measurement_scope=true;
if(selection==='mixed')probe.scenarios.push('initial');
const selected=target.windows[selection==='argument'?0:1];
const html=performanceWindowOptions(target,selected);
assert.match(html,/value="full:probe"/);
assert(html.includes(`value="${selected.id}" selected`));
if(selection==='default_window'||selection==='selected_window'||selection==='argument'){
  target.windows=[target.windows[0]];
  assert.match(performanceWindowOptions(target,target.windows[0]),/value="full:probe" selected/);
}
''')


@pytest.mark.parametrize(("status", "expected_status", "expected_outcome"), [
    ("running", "running", "running"), ("queued", "queued", "inconclusive"),
    ("reserved", "reserved", "inconclusive"), ("stopping", "stopping", "inconclusive"),
    ("timeout", "timeout", "fail"), ("cancelled", "cancelled", "inconclusive"),
    ("skipped", "skipped", "inconclusive"), ("failed", "fail", "fail"),
])
@pytest.mark.parametrize("verdict", ["passed", "inconclusive"])
def test_probe_transport_status_precedes_capability_verdict(status, expected_status, expected_outcome, verdict):
    run_render_checks("const [status,expectedStatus,expectedOutcome,verdict]="
                      + json.dumps([status, expected_status, expected_outcome, verdict]) + r''';
const r={id:'probe',target:'local',scenario:'performance_evidence_probe',status,http_status:status==='failed'?500:null};
state.detail={state:'running',report:{conditions:[{target:'local',condition_id:r.scenario,status:verdict,reason:'Capability reason'}]}};
assert.equal(requestStatus(r),expectedStatus);
assert.equal(requestOutcome(r),expectedOutcome);
assert.match(renderProbeReason(r),/Capability reason/);
if(status==='running')assert.equal(requestTiming(r),'Timing pending');
if(status==='failed'){
  r.http_status=400;r.workload={performance_evidence_probe:true,rejected_evidence_field:'logprobs'};
  assert.equal(requestStatus(r),'fail');assert.equal(requestOutcome(r),'fail');
  assert.equal(optionalProbeRejection(r),'logprobs');
}
''')
