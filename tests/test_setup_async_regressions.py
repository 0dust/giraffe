"""Setup actions keep migrated configuration and the current form authoritative."""

import json

import httpx
import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.web import create_app
from tests.test_ux_results import run_render_checks


# Give the existing renderer DOM fixture normal form-control behavior. Application
# rendering, configuration serialization, profile selection and discovery stay real.
SETUP = r'''
const proto=Object.getPrototypeOf(document.body);
Object.defineProperties(proto,{
 name:{get(){return this.getAttribute('name')||'';}},
 type:{get(){return this.getAttribute('type')||'';}},
 checked:{get(){return this.hasAttribute('checked');},set(v){if(v)this.setAttribute('checked','');else this.removeAttribute('checked');}},
 value:{get(){if(this._value!==undefined)return this._value;if(this.tagName==='SELECT'){const option=this.querySelector('option[selected]')||this.querySelector('option');return option?.getAttribute('value')||'';}return this.getAttribute('value')??(this.tagName==='TEXTAREA'?this.textContent:'');},set(v){this._value=String(v);}},
 elements:{get(){return Object.fromEntries(this.querySelectorAll('[name]').map(node=>[node.name,node]));}},
 previousElementSibling:{get(){return this.parentNode?.childNodes.filter(n=>n.nodeType===1)[this.parentNode.childNodes.filter(n=>n.nodeType===1).indexOf(this)-1]||null;}}
});
proto.setCustomValidity=function(value){this.validationMessage=value;};
proto.checkValidity=function(){return !this.validationMessage;};proto.reportValidity=proto.checkValidity;
proto.focus=function(){document.activeElement=this;};
FormData=class {
 constructor(form){this.rows=[];for(const input of form.querySelectorAll('[name]'))if(!['checkbox','radio'].includes(input.type)||input.checked)this.rows.push([input.name,input.value||(input.type==='checkbox'?'on':'')]);}
 get(name){return this.rows.find(row=>row[0]===name)?.[1]??null;}
 has(name){return this.rows.some(row=>row[0]===name);}
 getAll(name){return this.rows.filter(row=>row[0]===name).map(row=>row[1]);}
 set(name,value){this.rows=this.rows.filter(row=>row[0]!==name);this.rows.push([name,value]);}
};
state.config=structuredClone(bootstrap.defaults);state.config.targets=[{name:'local',url:'http://current/v1',model:'current-model'}];
bootstrap.config=structuredClone(state.config);state.jsonDraft=state.jsonBase='synced';
state.baselines=[{id:'base',name:'Baseline'}];state.prefillBaseline='base';
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=watchPlanViewport=scheduleTokenizerSelection=renderTokenizerSelection=()=>{};
renderNew();
'''


async def test_matching_legacy_baseline_preserves_load_at_launch(tmp_path):
    archived = {"targets": [{"name": "old", "url": "http://old/v1", "model": "old-model"}],
                "checks": ["access", "capacity"], "request_timeout_seconds": 15}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                 base_url="http://localhost") as client:
        response = await client.post("/api/config/draft", json=archived)
    assert response.status_code == 200
    normalized = response.json()
    assert "load" in normalized["config"]["checks"]
    run_render_checks(SETUP + "\n(async()=>{\nconst archived=" + json.dumps(archived)
                      + ";const normalized=" + json.dumps(normalized) + r''';
state.baselineConfigs=new Map([['base',archived]]);
const target=structuredClone(readForm().config.targets[0]);let requests=0;
api=async(path,options)=>{assert.equal(path,'/api/config/draft');assert.equal(JSON.stringify(JSON.parse(options.body)),JSON.stringify(archived));requests++;return normalized;};
await matchBaselineSettings();
assert($('input[name="check"][value="load"]').checked,'migrated Load must be selected');
const launched=readForm();
assert.deepEqual(Array.from(launched.config.checks),['access','load']);
assert.equal(JSON.stringify(launched.config.targets[0]),JSON.stringify(target));
assert.equal(launched.baseline_id,'base');assert.equal(requests,1);
assert.equal(launched.config.request_timeout_seconds,15);
assert.equal(JSON.stringify(state.migrationNotes),JSON.stringify(normalized.migration_notes));
})()
''', dom=True)


@pytest.mark.parametrize("change", ["route", "config", "baseline", "json"])
def test_baseline_normalization_does_not_overwrite_newer_setup(change):
    run_render_checks(SETUP + "\n(async()=>{\nconst change=" + json.dumps(change) + r''';
state.baselineConfigs=new Map([['base',structuredClone(state.config)]]);
let resolve;api=()=>new Promise(done=>{resolve=done;});
const pending=matchBaselineSettings();
assert(state.configBusy);
if(change==='route')state.routeVersion++;
if(change==='config'){state.config.request_timeout_seconds=77;configChanged();}
if(change==='baseline')$('#new-run-form').elements.baseline_id.value='';
if(change==='json')state.jsonDraft='unapplied edit';
const expected=JSON.stringify(state.config);
resolve({config:{...state.config,request_timeout_seconds:999},migration_notes:[]});await pending;
assert.equal(JSON.stringify(state.config),expected);assert(!state.configBusy);
if(change==='json')assert.equal(state.jsonDraft,'unapplied edit');
})()
''', dom=True)


def test_shared_summary_uses_serialized_effective_concurrency_and_names_timeout_default():
    config = RunConfig(targets=[{"name": "local", "url": "http://current/v1", "model": "m"}],
                       checks=["correctness"], concurrency=None,
                       test_options={"correctness": {"samples": 16, "concurrency": 8,
                                                     "request_timeout_seconds": 600}})
    plan = request_plan(config)
    assert plan["required_concurrency"]["peak"] == 8
    assert config.effective_check("correctness")["request_timeout_seconds"] == 600
    run_render_checks("const config=" + config.model_dump_json() + ";const plan=" + json.dumps(plan) + r''';
const text=sharedLimitsSummary(config,plan);
assert.match(text,/16 requests/);assert.match(text,/8 at once/);
assert.match(text,/5 m default request timeout/);assert.doesNotMatch(text,/≤ 5 m per request/);
assert.match(sharedLimitsSummary({...config,concurrency:10},plan),/10 at once/);
assert.match(sharedLimitsSummary(config,null),/Concurrency updating/);
assert.doesNotMatch(sharedLimitsSummary(config,null),/4 at once/);
''')


def test_baseline_normalization_error_preserves_current_setup_and_allows_retry():
    run_render_checks(SETUP + r'''
(async()=>{
const current=JSON.stringify(readForm().config);
state.baselineConfigs=new Map([['base',structuredClone(state.config)]]);
api=async()=>{throw new Error('Saved baseline settings could not be normalized');};
await matchBaselineSettings();
assert.equal(JSON.stringify(state.config),current);assert(!state.configBusy);
assert.match($('#config-error').textContent,/could not be normalized/);
api=async()=>({config:{...state.config,request_timeout_seconds:18},migration_notes:[]});
await matchBaselineSettings();assert(!state.configBusy);
assert.equal(readForm().config.request_timeout_seconds,18);
})()
''', dom=True)


@pytest.mark.parametrize("result", [
    {"status": "connected", "model": "discovered-model", "latency_ms": 42},
    {"status": "needs_model", "models": [], "message": "Enter a model"},
])
def test_discovery_updates_visible_form_after_profile_rerender(result):
    run_render_checks(SETUP + "\n(async()=>{\nconst result=" + json.dumps(result) + r''';
state.config.targets[0].model='';state.jsonDraft=state.jsonBase='synced';renderNew();
const detached=$('#new-run-form');let resolve;
api=()=>new Promise(done=>{resolve=done;});
const pending=testConnection();
assert.equal(state.modelDiscovery.status,'pending');
chooseSetupProfile('latency');const current=$('#new-run-form');
assert.notEqual(current,detached);assert(!detached.isConnected);
resolve(result);await pending;
if(result.status==='connected'){
 assert.equal(current.elements.model.value,'discovered-model');
 assert.equal(state.config.targets[0].model,'discovered-model');
 assert.equal(readForm().config.targets[0].model,'discovered-model');
 assert.equal(detached.elements.model.value,'');
 assert.match($('#model-discovery-status').textContent,/Connected · discovered-model/);
}else assert.equal(document.activeElement,current.elements.model);
})()
''', dom=True)
