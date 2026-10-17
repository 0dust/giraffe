"""Review confirms scope plainly while preserving plan bounds and warnings."""

import json

import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_ux_catalogue import browser_logic


def configuration(**overrides):
    return RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}], **overrides)


@pytest.mark.parametrize("config,kind", [
    (configuration(checks=["access", "correctness"]), "exact"),
    (configuration(checks=["capacity"], traffic={"arrival": "poisson", "rates": [1, 2], "duration_seconds": 1}), "estimate"),
    (configuration(checks=["fairness"], concurrency=2), "bounded"),
])
def test_visible_traffic_keeps_count_certainty_and_limits(config, kind):
    plan = request_plan(config)
    assert plan["count_kind"] == kind
    browser_logic(r"""
const plan=PLAN;
nodes['#request-plan']={};nodes['#start-run']={};nodes['#run-bar-summary']={textContent:'unchanged'};
state.jsonDraft=state.jsonBase='synced';state.setupStep=3;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
renderRequestPlan();const markup=nodes['#request-plan'].innerHTML,visible=markup.split('<details class="request-planning">')[0];
assert.match(visible,/<h3>Traffic<\/h3>/);assert(visible.includes(setupPlanCount(plan)));
assert.match(visible,/<dt>Request limit<\/dt>/);assert.match(visible,/<dt>Traffic time limit<\/dt>/);
assert(visible.includes(`<dd>${number(config.max_requests)}</dd>`));assert(visible.includes(`<dd>${elapsed(config.max_duration_seconds)}</dd>`));
assert.doesNotMatch(visible,/evaluator calls|Endpoint request budgets|Pass criteria|requests per endpoint/);
assert.match(markup,/<details class="request-planning"><summary>Request breakdown<\/summary>/);
assert.equal(nodes['#run-bar-summary'].textContent,'unchanged'); // No second traffic count in the footer.
assert.equal(nodes['#start-run'].disabled,false);
if(plan.count_kind==='bounded')assert.match(visible,/known requests \+ adaptive checks/);
if(plan.count_kind==='estimate')assert.match(visible,/About /);
state.configRevision++;renderRequestPlan();assert.equal(nodes['#start-run'].disabled,true);
assert.match(nodes['#request-plan'].innerHTML,/stale/);assert.doesNotMatch(nodes['#request-plan'].innerHTML,/plan-total|Request breakdown/);
""".replace("PLAN", json.dumps(plan)), config)


def test_budget_warning_and_fix_actions_remain_outside_breakdown():
    config = configuration(checks=["access", "correctness"], max_requests=2)
    plan = request_plan(config)
    browser_logic(r"""
const plan=PLAN;nodes['#request-plan']={};state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
renderRequestPlan();const markup=nodes['#request-plan'].innerHTML,visible=markup.split('<details class="request-planning">')[0];
assert.match(visible,/budget is too small/);assert.match(visible,/data-action="use-request-budget"/);
assert(visible.includes('data-budget="'+plan.required_request_limit+'"'));assert.match(visible,/data-action="edit-load"/);
for(const warning of plan.warnings)assert(visible.includes(escape(warning)));
assert(markup.indexOf('Endpoint request budgets')>markup.indexOf('Request breakdown'));
assert.match(markup,/local: up to 2 requests/);
""".replace("PLAN", json.dumps(plan)), config)


def test_multi_endpoint_dataset_and_restart_scope_are_preserved_without_duplicates():
    browser_logic(r"""
config.targets=[{name:'local',url:'http://local/v1',model:'model-a'},
  {name:'staging <b>',url:'https://stage.example/v1',model:'model-b',restart_command:['restart','server']}];
config.request_source='request_pack';config.request_pack={version:1,cases:[{scorer:'exact'},{scorer:'exact'}]};
config.checks=['access','correctness'];const before=JSON.stringify(config);
let markup=renderReviewScope({config,mode:'cold-start',restart_target:'staging <b>'});
for(const value of ['http://local/v1','https://stage.example/v1','model-a','model-b','staging &lt;b&gt;'])assert(markup.includes(value));
assert.match(markup,/Your questions · 2 saved questions/);assert.match(markup,/Restart staging &lt;b&gt; before testing/);
assert.match(markup,/data-setup-step="1">Edit connection/);assert.match(markup,/data-setup-step="2">Edit checks/);
assert.doesNotMatch(markup,/Your run|setup-side-summary|Simultaneous limit|Whole-run time limit/);
assert.match(markup,/<dl class="review-identity">/);assert.match(markup,/<ul class="review-check-names"><li>Endpoint response<\/li><li>Answer correctness<\/li><\/ul>/);
assert.equal(JSON.stringify(config),before);
config.checks=['access'];markup=renderReviewScope({config,mode:'normal'});
assert.doesNotMatch(markup,/Your questions|saved questions|Restart /); // Inactive dataset is not advertised as traffic.
config.checks=['correctness'];config.request_pack.cases=[];config.targets[0].model='';
markup=renderReviewScope({config,mode:'cold-start',restart_target:'local'});
assert.match(markup,/Enter an endpoint URL and model/);assert.match(markup,/Add a question/);assert.match(markup,/no restart command/);
""")


def test_evaluator_destinations_and_scope_stay_visible_and_thresholds_stay_separate():
    config = configuration(checks=["correctness", "capacity"], request_source="request_pack",
                           request_pack={"version": 1, "cases": [{"fixture_id": "one", "messages": [{"role": "user", "content": "Question"}], "expected": "Answer", "scorer": "judge"}]},
                           judging={"judges": [{"name": "reviewer", "url": "https://judge.example/v1", "model": "judge-model"}]},
                           test_options={"correctness": {"limits": {"min_correctness": .95}},
                                         "capacity": {"limits": {"min_correctness": .8}}})
    plan = request_plan(config)
    assert plan["evaluator"]["active"]
    browser_logic(r"""
const plan=PLAN;nodes['#request-plan']={};state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
renderRequestPlan();const markup=nodes['#request-plan'].innerHTML,visible=markup.split('<details class="request-planning">')[0];
assert(visible.includes('Up to '+number(plan.evaluator.call_upper_bound)+' evaluator calls'));
assert.match(visible,/Questions, expected answers and model answers/);assert.match(visible,/separate limits/);
assert.match(visible,/judge-model/);assert.match(visible,/https:\/\/judge.example\/v1/);
const criteria=renderReviewPassCriteria(config);
assert(criteria.includes(catalogTitle('correctness')));assert(criteria.includes(catalogTitle('capacity')));
assert.match(criteria,/Correct answers 95%/);assert.match(criteria,/Correct answers 80%/);
assert(markup.indexOf('Pass criteria')>markup.indexOf('Request breakdown'));
plan.evaluator.readiness_errors=['Evaluator needs a URL'];plan.warnings.push('Evaluator needs a URL');renderRequestPlan();
assert.equal((nodes['#request-plan'].innerHTML.match(/Evaluator needs a URL/g)||[]).length,1);
assert(nodes['#request-plan'].innerHTML.indexOf('Evaluator needs a URL')<nodes['#request-plan'].innerHTML.indexOf('Request breakdown'));
""".replace("PLAN", json.dumps(plan)), config)


def test_breakdown_uses_catalogue_names_and_keeps_special_allocation_labels():
    browser_logic(r"""
nodes['#request-plan']={};state.jsonDraft=state.jsonBase='synced';
const plan={count_kind:'exact',planned_total:3,planned_per_target:3,request_limit:160,rows:[
  {id:'correctness',title:'Old backend title',requests_per_target:2,kind:'exact',summary:'Two examples',source:'generated'},
  {id:'initial',title:'Connection check',requests_per_target:1,kind:'exact',summary:'First request',source:'generated'},
  {id:'access',title:'Shared accounting',requests_per_target:0,kind:'exact'}],warnings:[]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
renderRequestPlan();const markup=nodes['#request-plan'].innerHTML;
assert(markup.includes(catalogTitle('correctness')));assert.match(markup,/Connection check/);
assert.doesNotMatch(markup,/Old backend title|Shared accounting/);
""")


def test_review_identifies_dataset_consumers_when_other_checks_generate_requests():
    browser_logic(r"""
config.request_source='request_pack';config.request_pack={version:1,cases:[{scorer:'exact'}]};
for(const additional of ['access','json','context','generation']){
  config.checks=['correctness','capacity',additional];
  const original=JSON.stringify(config),markup=renderReviewScope({config});
  assert(markup.includes('1 saved question for '+escape(catalogTitle('correctness')+' and '+catalogTitle('capacity'))));
  assert.match(markup,/Other checks use generated requests/);assert.equal(JSON.stringify(config),original);
}
config.checks=['correctness','gpu'];let markup=renderReviewScope({config});
assert.match(markup,/Your questions · 1 saved question/);assert.doesNotMatch(markup,/Other checks use generated/);
config.request_source='builtin';markup=renderReviewScope({config});
assert.match(markup,/Built-in examples/);assert.doesNotMatch(markup,/Your questions|Other checks use generated/);
""")


def test_review_capacity_stages_show_requested_rates_and_effective_concurrency():
    config = configuration(checks=["capacity"], concurrency=3,
                           traffic={"rates": [.125, 2, 8], "duration_seconds": 2.5,
                                    "max_in_flight": None, "arrival": "poisson"})
    plan = request_plan(config)
    browser_logic(r"""
const plan=PLAN;nodes['#request-plan']={};state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
const original=JSON.stringify(config);renderRequestPlan();
const visible=nodes['#request-plan'].innerHTML.split('<details class="request-planning">')[0];
assert.match(visible,/Capacity target rates: 0.125 → 2 → 8 requests\/s · 2.5s per rate · up to 3 at once · random arrivals/);
assert.equal(JSON.stringify(config),original); // The summary does not rewrite a saved load profile.
config.traffic.max_in_flight=2;config.traffic.arrival='steady';
assert.match(renderReviewLoad(config),/up to 2 at once/);assert.doesNotMatch(renderReviewLoad(config),/random arrivals/);
config.checks=['access','correctness'];assert.equal(renderReviewLoad(config),'');
""".replace("PLAN", json.dumps(plan)), config)


def test_review_explains_measurement_only_timing_with_explicit_null_overrides():
    config = configuration(checks=["first_output", "generation"],
                           limits={"first_output_ms": 400, "latency_ms": 2000},
                           test_options={"first_output": {"limits": {"first_output_ms": None}},
                                         "generation": {"limits": {"latency_ms": None}}})
    browser_logic(r"""
const original=JSON.stringify(config);let markup=renderReviewPassCriteria(config);
assert.equal((markup.match(/Measurement only — no timing target; result is inconclusive/g)||[]).length,2);
assert.match(markup,/Minimum usable answers/);assert.doesNotMatch(markup,/First output within 400|Completion limit 2,000/);
assert.equal(JSON.stringify(config),original);
config.test_options.generation.limits.stream_gap_ms=150;markup=renderReviewPassCriteria(config);
assert.equal((markup.match(/Measurement only — no timing target/g)||[]).length,1);
assert.match(markup,/Stream pause 150 ms/);
delete config.test_options.first_output;markup=renderReviewPassCriteria(config);
assert.doesNotMatch(markup,/Measurement only/);assert.match(markup,/First output within 400 ms/);
""", config)


def test_review_surfaces_only_an_active_baseline_and_escapes_its_name():
    browser_logic(r"""
state.baselines=[{id:'baseline-123',name:'Before <upgrade>'}];
let markup=renderReviewScope({config,baseline_id:'baseline-123'});
assert.match(markup,/Compare with saved run: Before &lt;upgrade&gt;/);
assert.doesNotMatch(markup,/baseline-123/);
markup=renderReviewScope({config,baseline_id:'missing-<id>'});
assert.match(markup,/Compare with saved run: missing-&lt;id&gt;/);
assert.doesNotMatch(renderReviewScope({config,baseline_id:null}),/review-baseline|Compare with saved run/);
""")


def test_reduce_checks_action_reveals_only_selected_workloads():
    browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';
const revealed=[];let synced=0,step=null,focused=false;
readForm=()=>({config});revealSetupStep=value=>step=value;revealCatalogueCheck=id=>revealed.push(id);
syncTestControls=()=>synced++;configError=error=>{throw error;};
nodes['#check-search']={value:'cache'};
nodes['[data-catalogue-filter="selected"]']={focus(){focused=true;}};
const click=()=>listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'edit-load'}}:null}});
state.catalogueFilter='all';state.catalogueCategory='server';state.catalogueSearch='cache';state.openTest='buckets';
await click();assert.equal(step,2);assert.equal(state.catalogueFilter,'selected');assert.equal(state.catalogueCategory,'all');
assert.equal(state.catalogueSearch,'');assert.equal(nodes['#check-search'].value,'');assert.equal(state.openTest,null);
assert.equal(revealed.length,0);assert.equal(synced,1);assert(focused);
config.checks=['buckets'];await click();assert.deepEqual(revealed,['buckets']);
config.checks=['buckets','capacity'];await click();assert.deepEqual(revealed,['buckets','capacity']);
""")


def test_edit_limits_action_opens_shared_settings_and_focuses_request_budget():
    browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';
let captured=false,step=null,updated=false,focused=false;
captureFormDraft=()=>captured=true;revealSetupStep=value=>step=value;updateBounds=()=>updated=true;
configError=error=>{throw error;};
nodes['#shared-settings']={open:false};nodes['#new-run-form']={elements:{max_requests:{focus(){focused=true;}}}};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'edit-run-limits'}}:null}});
assert(captured);assert.equal(step,2);assert(updated);assert(nodes['#shared-settings'].open);assert(focused);
""")
