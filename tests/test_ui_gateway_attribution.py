"""Gateway attribution is selectable and retains its safe configuration in the UI."""
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


def test_gateway_catalogue_and_configuration_editor():
    browser_logic(r'''
const check=bootstrap.checks.find(row=>row.id==='gateway_attribution');
const markup=renderCheckSelection(config);
assert.equal((markup.match(/data-test-card="gateway_attribution"/g)||[]).length,1);
assert(checkGroups.find(group=>group.id==='server').checks.includes(check.id));
assert.match(renderTestCard(check,config),/Not configured/);
assert.match(renderProfileEditor(check,config),/data-setting-path="gateway_attribution"/);
config.gateway_attribution={identities:['alice','bob'],samples:2,settle_seconds:1};
config.targets[0].test_identities={alice:{api_key_env:'ALICE'},bob:{headers_env:{'X-Key':'BOB'}}};
config.targets[0].gateway={profile:'litellm',url:'http://localhost/spend/logs/v2',api_key_env:'OBSERVER'};
const before=JSON.stringify(config),editor=renderProfileEditor(check,config);
assert.match(editor,/alice, bob/);assert.match(editor,/2 samples per identity/);
assert.match(editor,/Evidence endpoint configured/);assert.match(editor,/environment-variable references/);
assert.equal(JSON.stringify(config),before);
state.jsonDraft=state.jsonBase='same';
let switched=false,focused=false;
nodes['#config-editor']={focus(){focused=true},scrollIntoView(){}};
switchConfigView=view=>{assert.equal(view,'json');switched=true;};
assertAppliedConfig=()=>{};
focusSetting('gateway_attribution');assert(switched&&focused);
''')


def test_gateway_results_have_identity_counts_and_no_timing_verdict():
    run_render_checks(r'''
const c=check('gateway_attribution','inconclusive',{metrics:{state:'unverified',
 matched_requests:1,observed_requests:2,prompt_tokens:3,completion_tokens:5,
 identities:[{identity:'alice',observed_requests:1,matched_requests:1,mismatched_requests:0,unverified_requests:0,prompt_tokens:3,completion_tokens:5},
 {identity:'bob<script>',observed_requests:1,matched_requests:0,mismatched_requests:0,unverified_requests:1,prompt_tokens:0,completion_tokens:0}]}});
const html=renderCheck(c,report([c]));
assert.match(html,/Attribution evidence/);assert.match(html,/Unverified/);
assert.match(html,/Per-identity attribution/);assert.match(html,/bob&lt;script&gt;/);
assert.match(html,/Matched input \/ output tokens/);assert.match(html,/3 \/ 5/);
assert.doesNotMatch(html,/<script>|Response time · p95|Longest stream pause|More measurements/);
assert.match(keyMetric(c),/1 \/ 2/);
for(const state of ['not_selected','unavailable','unsupported','unverified','mismatch','verified']){
 c.metrics.state=state;assert.match(renderCheck(c,report([c])),new RegExp(escape(human(state))));
}
''')


def test_attribution_baseline_uses_categorical_outcomes():
    run_render_checks(r'''
const rows=[{check:'gateway_attribution',metric:'attribution_outcome',scenario:'alice',
 scenario_label:'alice',aggregation:'observed',baseline:'verified',current:'mismatch',
 baseline_samples:1,current_samples:1,status:'fail'}];
const result={...report([]),baseline:{run_id:'reference',targets:[{target:'local',comparisons:rows}]}};
const html=renderComparison(result);
assert.match(html,/Gateway attribution outcomes/);
assert.match(html,/alice/);
assert.match(html,/Observed outcome/);
assert.match(html,/Outcome changed/);
assert.doesNotMatch(html,/Median|NaN/);
''')


def test_gateway_plan_shows_named_target_counts():
    browser_logic(r'''
const row={id:'gateway_attribution',title:'Gateway attribution',kind:'exact',requests_per_target:null,
  target_request_counts:{'proxy<script>':2,other:0}};
assert.equal(planRowCount(row),'proxy&lt;script&gt;: 2 · other: 0');
assert.match(renderFlightPlan({rows:[row]}),/proxy&lt;script&gt;: 2 · other: 0/);
assert.doesNotMatch(renderFlightPlan({rows:[row]}),/Adaptive|<script>/);
''')
