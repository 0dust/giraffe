"""Structured conflicts keep corrections local and previews current."""

import json

import pytest

from giraffe.models import RunConfig
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_setup import run_browser_logic
from tests.test_web import browser


async def test_traffic_cap_correction_uses_real_error_actions_and_refreshes_preview(tmp_path):
    cfg=RunConfig(targets=[{'name':'local','url':'http://localhost/v1','model':'test'}],checks=['load'],load_test={'workloads':{'combinations':{'selected':['short_short']}},'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':True},'rates':[250]},'schedule':{'duration_seconds':400}})
    invalid=cfg.model_dump(mode='json')
    invalid['load_test']['schedule']['duration_seconds']=3600
    async with browser(tmp_path) as client:
        response=await client.post('/api/config/validate',json=invalid)
        assert response.status_code==422
        errors=response.json()['detail']
        valid=await client.post('/api/config/validate',json=cfg.model_dump(mode='json'))
        assert valid.status_code==200
    browser_logic('const errors='+json.dumps(errors)+r''';
state.jsonDraft=state.jsonBase='synced';state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors};
const markup=renderSharedRunSettings(config)+renderCheckSelection(config),focused=[];const elements={};
for(const path of errors.flatMap(error=>error.fields)){const d=settingDestination(path);assert(markup.includes('name="'+d.name+'"'));elements[d.name]={dataset:{},getAttribute(){return '';},setAttribute(){},removeAttribute(){},focus(){focused.push(d.name);}};nodes['#field-'+d.name]=elements[d.name];nodes['#field-'+d.name+'-error']={};}
nodes['#new-run-form']={elements};revealSetupStep=()=>{};revealCatalogueCheck=id=>assert.equal(id,'load');syncInlineErrors();
for(const path of errors.flatMap(error=>error.fields)){const d=settingDestination(path);assert.equal(nodes['#field-'+d.name+'-error'].hidden,false);focusSetting(path);assert.equal(focused.at(-1),d.name);}
assert.match(renderSettingIssues(errors),/data-setting-path="load_test.schedule.duration_seconds"/);
''',cfg)
    assert valid.json()['config']['max_requests']==invalid['max_requests']
    assert valid.json()['config']['max_duration_seconds']==invalid['max_duration_seconds']


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
const markup=renderPlanWarnings(plan);
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
