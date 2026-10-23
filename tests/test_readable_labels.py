"""One test name follows a request from dispatch to saved evidence."""

import ast
import itertools
import json
from pathlib import Path
from typing import get_args

import pytest

from giraffe.conditions import _SCENARIOS, condition_for
from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize("scenario,workload,request_parameters,expected", [
    ("bucket_c32_short_short", {}, {"input_chars": 128, "output_tokens": 24},
     {"concurrency": 32, "input_chars": 128, "input_tokens_approx": 32, "output_tokens": 24, "same_prompt": True}),
    ("consistency_c4_medium_p2", {}, {"repetitions_per_prompt": 8},
     {"concurrency": 4, "same_prompt": False, "repetitions_per_prompt": 8}),
    ("fairness_short", {}, {}, {"same_prompt": True}),
    ("stress_long_input_summary_c8", {"requested_input_tokens": 10000}, {"output_tokens": 50},
     {"concurrency": 8, "same_prompt": True, "input_tokens_approx": 10000, "output_tokens": 50}),
    ("tools_weather_result_stream_turn1", {}, {}, {"same_prompt": False}),
    ("correctness_c4", {}, {}, {"concurrency": 4, "same_prompt": False}),
    ("arrival", {}, {}, {"same_prompt": True}),
])
def test_condition_explains_purpose_and_actual_request_parameters(scenario, workload, request_parameters, expected):
    condition = condition_for(scenario, workload, request_parameters=request_parameters)
    assert condition["purpose"].endswith(".")
    assert {key: condition["parameters"][key] for key in expected} == expected


def test_capacity_rate_and_mixed_partner_metadata():
    assert condition_for("traffic_stage_1", {"rate_rps": 2})["label"] == "2 requests/s"
    for phase in ("before_output", "active_stream"):
        short = condition_for(f"mixed_short_long_input_{phase}")
        assert short == condition_for(f"mixed_heavy_long_input_{phase}")
        assert short["parameters"] == {"concurrency": 2, "same_prompt": False}
        assert "short" in short["purpose"].lower()


def builder_scenarios():
    from giraffe.models import Consistency, Sessions, ToolChoiceRule

    values = {
        "level": [1, 4, 32], "effective.concurrency": [1, 4], "check_id": ["correctness", "json"],
        "input_shape": ["short", "medium", "long"], "output_shape": ["short", "medium", "long"],
        "shape": get_args(get_args(Consistency.model_fields["shapes"].annotation)[0]),
        "kind": ["long_input", "long_output"], "phase": ["before_output", "active_stream"],
        "fixture.fixture_id": ["lookup", "weather_result"], "prefix": ["tools", "tools_lookup"],
        "choice_mode": [*get_args(ToolChoiceRule.model_fields["mode"].annotation), "forced"],
        "turn": [0, 1, 2], "prompt_index": [0, 2], "index": [0, 1], "stage_index": [0, 1],
    }
    scenarios = set(_SCENARIOS)
    for level in (1, 4, 32):
        scenarios.update(f"load_combo_{a}_{b}_c{level}" for a in ("short", "medium", "long") for b in ("short", "medium", "long"))
        scenarios.add(f"load_mixed_c{level}")
        scenarios.add(f"load_mixed_c{level}.short_long")
        scenarios.update(f"load_interference_{kind}_c{level}" for kind in ("control", "probe"))
        scenarios.update(f"load_interference_c{level}.{role}" for role in ("short_stream", "short_probe", "long_probe"))
        scenarios.update(f"load_stress_{kind}_c{level}" for kind in ("input", "output"))
    scenarios.update(f"load_calibration_{size}_{variant}" for size in ("short", "medium", "long") for variant in ("short_answer", "long_output"))
    sources = Path(__file__).parents[1] / "src/giraffe"
    for name in ("runner", "workloads", "logprob_workload", "traffic", "tool_conversations"):
        tree = ast.parse((sources / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            template = "".join(part.value if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}" for part in node.values)
            if not (template.startswith(("bucket_c", "mixed_", "session_", "stress_", "consistency_c", "traffic_stage_", "tools_"))
                    or template in {"{prefix}_{choice_mode}_{mode}", "{check_id}_c{effective.concurrency}"}):
                continue
            if template.startswith("tools_") and "{mode}" not in template:
                continue  # The intermediate fixture prefix is not a scenario.
            fields = list(dict.fromkeys(ast.unparse(part.value) for part in node.values if isinstance(part, ast.FormattedValue)))
            choices = dict(values)
            choices["mode"] = (get_args(get_args(Sessions.model_fields["modes"].annotation)[0])
                               if template.startswith("session_") else ["complete", "stream"])
            for combination in itertools.product(*(choices[field] for field in fields)):
                scenario = template
                for field, value in zip(fields, combination):
                    scenario = scenario.replace("{" + field + "}", str(value))
                scenarios.add(scenario)
    assert "tools_lookup_named_complete" in scenarios
    return sorted(scenarios)


def test_javascript_fallback_labels_match_python():
    pairs = [(scenario, condition_for(scenario)["label"]) for scenario in builder_scenarios()]
    run_render_checks("for(const [id,label] of " + json.dumps(pairs) + ")assert.equal(phaseName(id),label,id);"
        "assert.equal(phaseName('traffic_stage_1',{rate_rps:2}),'2 requests/s');")


def test_shared_label_in_spots_dialog_live_activity_and_saved_evidence():
    run_render_checks(r"""
const condition={id:'bucket_c32_short_short',label:'Short input / short output · 32 at once',purpose:'Checks short answers under concurrent load.',parameters:{concurrency:32,input_chars:128,input_tokens_approx:32,output_tokens:24,same_prompt:true}};
const records=Array.from({length:32},(_,i)=>({id:'r'+i,slot:i,target:'local',scenario:condition.id,fixture_id:'cryptic.fixture',condition_id:condition.id,status:'completed',valid:true,score:true,elapsed_ms:812}));
const summary={target:'local',condition,planned:32,started:32,completed:32};
const saved=report([check('concurrency','pass',{evidence_ids:records.map(r=>r.id)})],{requests:records,observations:{targets:{local:{conditions:[summary]}}}});
detail(saved);
const label=condition.label+'. Request 5 of 32 · Passed · 812 ms';
assert(renderMosaic(records).includes('aria-label="'+label+'"'));
const tip={hidden:true,style:{},getBoundingClientRect:()=>({width:200,height:60})};nodes['#spot-tip']=tip;window.innerWidth=1200;
const spot={dataset:{},getAttribute:()=>label,classList:{contains:s=>s==='pass'},getBoundingClientRect:()=>({left:10,top:100,width:30}),closest:()=>spot};
listeners.pointerover({target:spot,clientX:10,clientY:100});assert(tip.innerHTML.includes(condition.label));assert(tip.innerHTML.includes('Request 5 of 32 · Passed · 812 ms'));
tip.hidden=true;listeners.focusin({target:spot});assert.equal(tip.hidden,false);assert(tip.innerHTML.includes(condition.label));
listeners.focusout({target:spot});assert.equal(tip.hidden,true);
nodes['#request-dialog']={innerHTML:'',showModal(){}};openRequest('r4');let html=nodes['#request-dialog'].innerHTML;
for(const text of [condition.label,condition.purpose,'Request 5 of 32','What was sent','128','~32','24','Same prompt','Internal ID','cryptic.fixture'])assert(html.includes(text),text);
state.detail={id:'live',state:'running',config:{targets:[],max_requests:32},live_requests:records,live_conditions:[summary],progress:{scenario:condition.id,target:'local'},events:[{event:'scenario_started',scenario:condition.id,target:'local'}]};
renderLive();html=nodes['#main'].innerHTML;assert(html.includes('<h2>'+condition.label+'</h2>'));assert(html.includes('Scenario started · '+condition.label));
openLiveRequest(4);html=nodes['#request-dialog'].innerHTML;for(const text of [condition.label,condition.purpose,'Request 5 of 32','What was sent','Internal ID'])assert(html.includes(text),text);
detail(report([],{requests:records.map(({condition_id,...r})=>r)}));html=nodes['#main'].innerHTML;assert(html.includes(condition.label));assert(!html.includes('aria-label="Request 1: cryptic'));
""")


async def test_live_metadata_comes_from_actual_builders_and_survives_report_save(tmp_path):
    from giraffe.runner import _Run
    from giraffe.models import RunConfig
    from giraffe.load_test import build_spec, test_points
    from giraffe.reporting import write_report
    from tests.test_condition_tracking import Client
    from tests.test_web import report

    config = RunConfig(targets=[{"name":"local","url":"http://unused.test/v1","model":"test"}],checks=["load"],load_test={"workloads":{"combinations":{"selected":["short_long"]}},"patterns":{"concurrent":{"levels":[2]},"constant":{"enabled":True},"rates":[1000]}})
    events = []
    run = _Run(config, events.append, None)
    point = next(point for point in test_points(config.load_test) if point["workload"] == "short_long")
    request = build_spec(run, config.load_test, point, "short_long", {("short", "long_output"): {"filler_chars": 64}})
    await run.group(config.targets[0], Client(), [request], request.scenario, count=2, concurrency=2)
    arrival_point = next(item for item in test_points(config.load_test) if item["pattern"] == "constant")
    arrival_request = build_spec(run, config.load_test, arrival_point, "short_long", {("short", "long_output"): {"filler_chars": 48}})
    await run.group(config.targets[0], Client(), [arrival_request], arrival_request.scenario, count=2, concurrency=1)
    rows = run.observations["local"]["conditions"]
    matrix = rows[0]["condition"]
    assert matrix["parameters"] == {"concurrency": point["level"], "same_prompt": True,
        "input_chars": len(request.messages[0]["content"]),
        "input_tokens_target": request.requested_input_tokens,
        "output_tokens": request.max_tokens, "request_timeout_seconds": config.load_test.request_timeout_seconds}
    arrival = rows[1]["condition"]
    assert arrival["rate_rps"] == 1000
    assert arrival["parameters"]["same_prompt"] is True
    assert arrival["parameters"]["input_chars"] == len(arrival_request.messages[0]["content"])
    assert all(event["condition_summary"]["condition"]["purpose"] for event in events if event.get("condition_summary"))
    started = next(e for e in events if e["event"] == "request_started")
    assert started["input_chars"] == matrix["parameters"]["input_chars"]
    assert "messages" not in started and "evaluation" not in started
    saved = report(config)
    saved.observations = {"targets": run.observations}
    saved.requests = run.records
    write_report(saved, tmp_path)
    actual = json.loads((tmp_path / "report.json").read_text())
    assert actual["observations"]["targets"]["local"]["conditions"] == rows


def test_saved_request_number_uses_dispatch_order_and_metadata_wins_over_fallback():
    run_render_checks(r"""
const condition={id:'bucket_c2_short_short',label:'Canonical test <name>',purpose:'Explains the configured test.',parameters:{concurrency:2,same_prompt:true}};
const first={id:'first',target:'local',scenario:condition.id,condition_id:condition.id,dispatch_ms:10,status:'completed',valid:true,score:true},second={...first,id:'second',dispatch_ms:20};
const records=[second,first],saved=report([],{requests:records,observations:{targets:{local:{conditions:[{condition,planned:2}]}}}});
detail(saved);
assert.equal(requestIdentity(second,records),'Request 2 of 2');
assert(renderMosaic(records).includes('aria-label="Canonical test &lt;name&gt;. Request 2 of 2'));
assert(renderBeeswarm(records.map(r=>({...r,elapsed_ms:1}))).includes('aria-label="Canonical test &lt;name&gt;. Request 2 of 2'));
assert(renderPhases([{event:'scenario_started',scenario:condition.id,target:'local'}]).includes('Canonical test &lt;name&gt;'));
assert(comparisonTable([{scenario:condition.id,target:'local',metric:'latency_ms'}]).includes('Canonical test &lt;name&gt;'));
nodes['#request-dialog']={innerHTML:'',showModal(){}};openRequest('second');assert(nodes['#request-dialog'].innerHTML.includes('Canonical test &lt;name&gt;'));
""")


async def test_group_metadata_preserves_each_different_request():
    from giraffe.runner import _Run
    from giraffe.workloads import spec
    from tests.test_condition_tracking import Client, configuration

    config = configuration()
    run = _Run(config, None, None)
    requests = [spec("one", "correctness", "correctness", case_id="first", max_tokens=8),
                spec("different question", "correctness", "correctness", case_id="second", max_tokens=16)]
    records = await run.group(config.targets[0], Client(), requests, "correctness", count=2, concurrency=2)
    assert [r.workload["case_id"] for r in records] == ["first", "second"]
    parameters = run.observations["local"]["conditions"][0]["condition"]["parameters"]
    assert parameters["same_prompt"] is False
    assert parameters["input_chars"] == [3, 18]
    assert parameters["output_tokens"] == [8, 16]


@pytest.mark.parametrize("requested", [None, 9999])
async def test_condition_and_live_dialog_metadata_use_effective_output_cap(requested):
    from giraffe.runner import _Run
    from giraffe.workloads import spec
    from tests.test_condition_tracking import Client, configuration

    config = configuration()
    events = []
    run = _Run(config, events.append, None)
    await run.request(config.targets[0], Client(), spec("hi", "generation", "generation", max_tokens=requested))
    effective = config.max_output_tokens if requested is None else requested
    assert run.observations["local"]["conditions"][0]["condition"]["parameters"]["output_tokens"] == effective
    assert next(e for e in events if e["event"] == "request_started")["requested_max_tokens"] == effective


def test_partial_condition_card_and_global_spots_use_the_same_request_total():
    run_render_checks(r"""
const condition={id:'bucket_c32_short_short',label:'Short input / short output · 32 at once',parameters:{},purpose:'Checks concurrent requests.'};
const record={id:'partial',target:'local',scenario:condition.id,condition_id:condition.id,status:'completed'};
const summary={target:'local',condition,planned:32,started:1,completed:1,status:'finished'};
const saved=report([],{requests:[record],observations:{targets:{local:{conditions:[summary]}}}});
state.detail={id:'partial-run',report:saved,state:'completed'};
const html=renderTests([record],{report:saved}),view=[...conditionViews.values()][0];
assert.equal((html.match(/class="condition-spot request-spot /g)||[]).length,1);
assert.match(html,/1 of 32 requests finished · 0 active/);
assert.equal(view.summary.planned,32);assert.equal(view.summary.started,1);assert.equal(view.summary.completed,1);
const counts=conditionDialogCounts(view);assert.match(counts,/<dt>Planned<\/dt><dd>32<\/dd>/);assert.match(counts,/<dt>Started<\/dt><dd>1<\/dd>/);
assert(renderMosaic([record]).includes('aria-label="'+condition.label+'. Request 1 of 32'));
""")


def test_logprob_evidence_links_name_the_test_instead_of_only_the_request_id():
    run_render_checks(r"""
const record={id:'internal-request',target:'local',scenario:'consistency_c4_medium',status:'completed'};
state.detail={report:report([],{requests:[record]})};
const html=renderLogprobResults({conditions:[{shape:'medium',level:4,examples:[{kind:'token',position:1,request_ids:[record.id]}]}]});
assert(html.includes('data-request="internal-request">Medium input consistency · 4 at once · Request 1 of 1</button>'));
""")
