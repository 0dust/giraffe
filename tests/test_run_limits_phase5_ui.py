"""Structured conflicts keep corrections local and previews current."""

import json

import pytest

from giraffe.models import RunConfig
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_setup import run_browser_logic
from tests.test_web import browser


async def test_traffic_cap_correction_uses_real_error_actions_and_refreshes_preview(tmp_path):
    cfg = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                    checks=["capacity"], traffic={"rates": [250], "duration_seconds": 400})
    invalid = cfg.model_dump(mode="json")
    invalid["traffic"]["duration_seconds"] = 3600
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/validate", json=invalid)
        assert response.status_code == 422, response.text
        errors = response.json()["detail"]
        applied = browser_logic(r"""
const errors=ERRORS;
function element(extra={}){return {dataset:{},attributes:{},setAttribute(k,v){this.attributes[k]=v;},
 removeAttribute(k){delete this.attributes[k];},getAttribute(k){return this.attributes[k];},focus(){this.focused=true;},...extra};}
state.config.traffic.duration_seconds=3600;
state.jsonDraft=state.jsonBase='synced';
const elements={traffic_rates:element({value:'250'}),traffic_duration_seconds:element({value:'3600'})};
nodes['#new-run-form']={elements};nodes['#config-error']={hidden:true};nodes['#request-plan']={};
for(const [name,input] of Object.entries(elements)){
 nodes['#field-'+name]=input;nodes['#field-'+name+'-error']={};
}
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors,error:errors[0].msg};
readForm=()=>({config:{...structuredClone(state.config),traffic:{...state.config.traffic,duration_seconds:Number(elements.traffic_duration_seconds.value)}}});
syncInlineErrors();renderRequestPlan();
for(const name of Object.keys(elements)){
 assert.equal(elements[name].attributes['aria-invalid'],'true');
 assert.match(nodes['#field-'+name+'-error'].textContent,/900,000/);
}
assert.match(nodes['#request-plan'].innerHTML,/data-action="use-setting-correction"/);
assert.match(nodes['#request-plan'].innerHTML,/Use 400 seconds/);
assert.match(nodes['#request-plan'].innerHTML,/scheduled traffic/i);
assert.doesNotMatch(nodes['#request-plan'].innerHTML,/use-request-budget|use-time-budget/);
const details={tagName:'DETAILS',open:false};elements.traffic_rates.parentElement=details;
let opened=null;revealSetupStep=step=>state.setupStep=step;revealCatalogueCheck=check=>opened=check;
await listeners.click({target:{closest(selector){
 if(selector==='[data-action]')return {dataset:{action:'edit-setting'}};
 if(selector==='[data-setting-path]')return {dataset:{settingPath:'traffic.rates'}};
 return null;
}}});
assert.equal(opened,'capacity');assert.equal(state.setupStep,2);assert(details.open);assert(elements.traffic_rates.focused);
assert.equal(elements.traffic_duration_seconds.value,'3600');
let refreshed=0;renderRequestPreview=()=>{syncInlineErrors();assert.equal(state.requestPreview.status,'stale');};
updateBounds=()=>scheduleRequestPlan();scheduleRequestPlan=()=>refreshed++;
const revision=state.configRevision;
await listeners.click({target:{closest(selector){
 if(selector==='[data-action]')return {dataset:{action:'use-setting-correction'}};
 if(selector==='[data-setting-value]')return {dataset:{settingPath:'traffic.duration_seconds',settingValue:'400'}};
 return null;
}}});
assert.equal(elements.traffic_duration_seconds.value,400);assert.equal(elements.traffic_rates.value,'250');
assert.equal(state.configRevision,revision+1);assert.equal(refreshed,1);assert.equal(currentSettingErrors().length,0);
assert.equal(elements.traffic_rates.attributes['aria-invalid'],undefined);
assert.equal(nodes['#config-error'].hidden,true);
console.log(JSON.stringify(readForm().config));
""".replace("ERRORS", json.dumps(errors)), cfg)
        valid = await client.post("/api/config/validate", json=applied)
    assert valid.status_code == 200, valid.text
    assert valid.json()["config"]["traffic"]["rates"] == [250]
    assert valid.json()["config"]["traffic"]["duration_seconds"] == 400
    assert valid.json()["request_plan"]["errors"] == []
    for field in ("max_requests", "max_duration_seconds"):
        assert applied[field] == invalid[field]


@pytest.mark.parametrize("change", [
    "state.configRevision++;",
    "state.routeVersion++;",
    "state.jsonDraft='unapplied';",
    "elements.traffic_duration_seconds.value='3000';",
    "elements.traffic_rates.value='300';",
    "value='401';",
    "field='max_requests';",
])
def test_traffic_correction_rejects_stale_or_unoffered_action(change):
    run_browser_logic(r"""
state.config={checks:['capacity'],targets:[{}],traffic:{rates:[250],duration_seconds:3600}};
state.jsonDraft=state.jsonBase='synced';
const elements={traffic_duration_seconds:element({value:'3600'}),traffic_rates:element({value:'250'}),max_requests:element({value:'10'})};
readForm=()=>({config:{traffic:{rates:[Number(elements.traffic_rates.value)]}}});
nodes['#new-run-form']={elements};nodes['#config-error']=element();
let changed=false;configChanged=updateBounds=()=>{changed=true;};
const errors=[{fields:['traffic.rates','traffic.duration_seconds'],values:{'traffic.duration_seconds':3600,'traffic.rates':[250]},
 actions:[{field:'traffic.duration_seconds',value:400,label:'Use 400 seconds'}]}];
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors};
let value='400',field='traffic.duration_seconds';
CHANGE
const before=elements.traffic_duration_seconds.value;
await listeners.click({target:{closest(selector){
 if(selector==='[data-action]')return {dataset:{action:'use-setting-correction'}};
 if(selector==='[data-setting-value]')return {dataset:{settingPath:field,settingValue:value}};
 return null;
}}});
assert.equal(elements.traffic_duration_seconds.value,before);assert.equal(elements.max_requests.value,'10');
assert.equal(changed,false);assert.equal(nodes['#config-error'].hidden,false);
assert.match(nodes['#config-error'].textContent,/plan changed|JSON changes/);
""".replace("CHANGE", change))


def test_current_json_view_stays_synced_after_duration_correction():
    run_browser_logic(r"""
state.config={checks:['capacity'],targets:[{}],traffic:{rates:[250],duration_seconds:3600},max_requests:10,max_duration_seconds:20};
state.configView='json';state.jsonDraft=state.jsonBase='synced';
const input=element({value:'3600'});nodes['#new-run-form']={elements:{traffic_duration_seconds:input}};
nodes['#config-editor']=element();nodes['#config-error']=element();
readForm=()=>({config:{...state.config,traffic:{rates:[250],duration_seconds:Number(input.value)}}});
configChanged=()=>{state.configRevision++;};updateBounds=()=>{};
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors:[{
 values:{'traffic.duration_seconds':3600},actions:[{field:'traffic.duration_seconds',value:400}]}]};
await listeners.click({target:{closest(selector){
 if(selector==='[data-action]')return {dataset:{action:'use-setting-correction'}};
 if(selector==='[data-setting-value]')return {dataset:{settingPath:'traffic.duration_seconds',settingValue:'400'}};
 return null;
}}});
assert.equal(hasJsonChanges(),false);assert.equal(state.jsonDraft,state.jsonBase);
const saved=JSON.parse(nodes['#config-editor'].value);
assert.equal(saved.traffic.duration_seconds,400);assert.equal(saved.traffic.rates.join(','),'250');
assert.equal(saved.max_requests,10);assert.equal(saved.max_duration_seconds,20);
""")


def test_concurrency_resource_and_capacity_issues_have_inline_and_plan_presence():
    run_browser_logic(r"""
state.config={checks:['capacity'],targets:[{}]};state.jsonDraft=state.jsonBase='synced';
const plan={estimated_seconds:1,estimated_low_seconds:0,estimated_high_seconds:2,
 concurrency_warnings:[{msg:'Capacity waits for shared slots.',fields:['traffic.max_in_flight','concurrency']}],
 capacity_warnings:[{msg:'Estimate: 500 unfinished requests needed; the rest are dropped locally.',fields:['traffic.rates','traffic.expected_response_seconds','traffic.max_in_flight']}],
 resource_issues:[{msg:'The process needs more open files.',fields:['concurrency'],blocking:true}]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan};
const names=['traffic_rates','traffic_max_in_flight','traffic_expected_response_seconds','concurrency'];
for(const name of names){
 const input=element();input.getAttribute=key=>input.attributes[key];nodes['#field-'+name]=input;
 nodes['#field-'+name+'-error']=element();nodes['#field-'+name+'-warning']=element();
}
syncInlineErrors();
for(const name of ['traffic_rates','traffic_expected_response_seconds','traffic_max_in_flight']){
 assert.match(nodes['#field-'+name+'-warning'].innerHTML,/Estimate: 500/);
 assert.equal(nodes['#field-'+name].attributes['aria-invalid'],undefined);
}
assert.equal(nodes['#field-concurrency'].attributes['aria-invalid'],'true');
assert.match(nodes['#field-concurrency-error'].textContent,/needs more open files/);
assert.match(nodes['#field-concurrency-warning'].innerHTML,/Capacity waits/);
const markup=renderPlanTime(plan);
for(const message of ['Capacity waits','Estimate: 500','needs more open files'])assert(markup.includes(message));
assert.match(markup,/data-setting-path="traffic.expected_response_seconds"/);
assert.equal((markup.match(/role="alert"/g)||[]).length,1);
assert.equal(canStartRun(),false);
""")


async def test_tool_stream_conflict_reaches_both_inline_fields_and_plan(tmp_path):
    values = {"config_version": 2, "targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}],
              "checks": ["tools"], "stream": False, "tool_checks": {"modes": ["stream"]}}
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422
    errors = response.json()["detail"]
    run_browser_logic(r"""
state.config={checks:['tools'],targets:[{}],stream:false,tool_checks:{modes:['stream']}};
state.jsonDraft=state.jsonBase='synced';
const stream=element(),modes=element();
nodes['#new-run-form']={elements:{stream,profile_tools_modes:modes}};
for(const [name,input] of [['stream',stream],['profile_tools_modes',modes]]){
  nodes['#field-'+name]=input;nodes['#field-'+name+'-error']=element();
}
const errors=ERRORS;
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors};
syncInlineErrors();
assert.equal(stream.attributes['aria-invalid'],'true');assert.equal(modes.attributes['aria-invalid'],'true');
assert.match(renderRunBlueprint(state.config),/Enable streaming on Connect or choose complete tool requests/);
assert.equal(settingDestination('tool_checks.modes').name,'profile_tools_modes');
assert.equal(settingDestination('tool_checks.modes').check,'tools');
""".replace('ERRORS', json.dumps(errors)))
