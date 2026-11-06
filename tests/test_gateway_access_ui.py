"""Policy setup and results survive browser form and report round trips."""
import json

import pytest

from giraffe.models import RunConfig
from tests.test_gateway_access import config
from tests.test_ux_catalogue import browser_logic
from tests.test_web import browser, finished, successful_runner


def test_gateway_policy_form_roundtrip_and_validation():
    value = browser_logic(r'''
const markup=renderTestCard(bootstrap.checks.find(c=>c.id==='gateway_access'),config);
assert.match(markup,/Declared cases/);assert.match(markup,/Environment references/);
assert.match(markup,/LiteLLM custom key header/);
config.gateway_access.auth_header='X-Saved-Key';
assert.match(renderTestCard(bootstrap.checks.find(c=>c.id==='gateway_access'),config),
  /name="gateway_access_auth_header"[^>]*value="X-Saved-Key"/);
const values=new FormData();values.set('gateway_access_rendered','1');
values.set('gateway_access_profile','litellm');values.set('gateway_access_request_timeout_seconds','3');
values.set('gateway_access_max_output_tokens','8');
values.set('gateway_access_cases','valid allowed allow\ninvalid denied deny');
values.set('gateway_access_auth_header','X-Proxy-Key');
values.set('gateway_access_identities_0',JSON.stringify({...config.targets[0].test_identities,
  proxy:{headers_env:{'X-Proxy-Key':'PROXY_KEY'}},anonymous:{}}));
const edited=configureGatewayAccess(config,values);
assert.equal(edited.gateway_access.cases[1].expected,'deny');
assert.equal(edited.targets[0].test_identities.allowed.api_key_env,'ALLOW_KEY');
values.set('gateway_access_cases','invalid secret missing decision');
assert.throws(()=>configureGatewayAccess(config,values),/case name, identity alias and allow or deny/);
assert.equal(settingDestination('gateway_access.cases.0.expected').check,'gateway_access');
console.log(JSON.stringify(edited));
''', config())
    result = RunConfig.model_validate(value)
    assert result.gateway_access.request_timeout_seconds == 3
    assert result.gateway_access.auth_header == "X-Proxy-Key"
    assert result.targets[0].test_identities["proxy"].headers_env == {"X-Proxy-Key": "PROXY_KEY"}
    assert result.targets[0].test_identities["anonymous"].api_key_env is None


@pytest.mark.parametrize("malformed,invalid,companions,server_only", [
    ("cases", "bad draft", [], False),
    ("identities_0", '{"allowed":', ["gateway_policy", "gateway_attribution"], False),
    ("identities_0", '{"allowed":[]}', ["gateway_policy", "gateway_attribution"], False),
    ("identities_0", '{"allowed":{"api_key_env":["BAD"]}}', [], False),
    ("identities_0", '{"allowed":{"headers_env":{"Host":"BAD"}}}', ["gateway_policy", "gateway_attribution"], False),
    ("request_timeout_seconds", "0", [], False),
    ("cases", "valid allowed allow\nvalid denied deny", [], True),
    ("cases", "1bad allowed allow\ninvalid denied deny", [], True),
    ("cases", "\n".join(f"case{i} allowed allow" for i in range(17)), [], True),
    ("auth_header", "X bad", ["gateway_policy", "gateway_attribution"], True),
])
async def test_unchecking_gateway_access_keeps_invalid_draft_out_of_live_setup(
        malformed, invalid, companions, server_only, tmp_path, monkeypatch, local_tokenizer):
    raw = config().model_dump(mode="json")
    raw.update(checks=["access", "gateway_access", *companions], gateway_attribution={"identities": ["allowed"]},
               gateway_policy={"affected_identity": "allowed", "users": [
                   {"identity": alias, "requests": 1, "rpm": 1} for alias in ("allowed", "denied")]})
    args = json.dumps([malformed, invalid, companions, server_only])
    value = browser_logic(f"const [malformed,invalid,companions,serverOnly]={args};" + r'''
const controls=Object.entries({gateway_access_rendered:'1',gateway_access_profile:'litellm',
  gateway_access_auth_header:'',gateway_access_request_timeout_seconds:'10',gateway_access_max_output_tokens:'16',
  gateway_access_cases:'valid allowed allow\ninvalid denied deny',
  gateway_access_identities_0:JSON.stringify(config.targets[0].test_identities)})
  .map(([name,value])=>({name,value,type:'text'}));
const check={name:'check',value:'gateway_access',checked:true,id:'',matches:()=>false,
  closest:selector=>selector==='#new-run-form'?nodes[selector]:null,focus(){}};
const initialIdentities=JSON.stringify(config.targets[0].test_identities),sent=[];
const NativeFormData=FormData;
FormData=class extends NativeFormData {
  constructor(){super();for(const name of ['url','model','name'])this.set(name,config.targets[0][name]);
    for(const id of ['access',...(check.checked?['gateway_access']:[]),...companions])this.append('check',id);}
};
nodes['#new-run-form']={elements:{mode:{value:'normal'},baseline_id:{value:''},restart_target:{value:''}},
  querySelectorAll:()=>controls};
nodes['#form-error']={};nodes['#start-run']={};
const edited=controls.find(input=>input.name==='gateway_access_'+malformed);
edited.value=invalid;
tokenizerChoice=()=>config.targets[0].tokenizer;
tokenizerSetupReady=()=>true;
renderRequestPreview=()=>{};
renderNew=()=>{};
enterRoute=()=>{};setActive=()=>{};validateRunForm=()=>true;
api=async(path,request)=>{const body=JSON.parse(request.body);sent.push({path,body});
  return {id:'mock-run',config:body,migration_notes:[],request_plan:{errors:[]}};};
const assertActiveError=()=>{
  if(malformed==='request_timeout_seconds')assert(localSettingErrors().some(issue=>issue.fields.includes('gateway_access.request_timeout_seconds')));
  else assert.throws(()=>readForm(false),malformed==='cases'?/case name, identity alias/:/Environment references/);
};
check.checked=false;
await listeners.change({target:check});
const ordinary=readForm(false).config;
assert.equal(ordinary.checks.join(','),['access',...companions].join(','));
assert.equal(ordinary.gateway_access,null);
assert.equal(localSettingErrors().length,0);
assert.equal(JSON.stringify(ordinary.targets[0].test_identities),initialIdentities);
await refreshRequestPreview(undefined,{draft:true});
assert.equal(sent.at(-1).body.gateway_access,null);
assert.equal(JSON.stringify(sent.at(-1).body.targets[0].test_identities),initialIdentities);
const exported=portableConfig(await validatedSnapshot({draft:true}));
assert.equal(JSON.stringify(exported.targets[0].test_identities),initialIdentities);
assert(canStartRun());
await startNewRun();
await refreshRequestPreview();
assert(canStartRun());
await startRun({preventDefault(){},target:nodes['#new-run-form']});
assert.equal(sent.at(-1).path,'/api/runs');
saveDraft();await restoreDraft();
const markup=renderGatewayAccessEditor(state.config);
if(malformed==='request_timeout_seconds')assert.match(markup,/name="gateway_access_request_timeout_seconds"[^>]*value="0"/);
else assert(markup.includes(escape(edited.value)));
check.checked=true;
await listeners.change({target:check});
const active=serverOnly?readForm(false).config:null;
if(!serverOnly)assertActiveError();
if(serverOnly){
  const previous={...active,checks:ordinary.checks};
  sessionStorage.setItem('giraffe-draft-v1',JSON.stringify({bootstrap:state.bootstrap.config,config:previous}));
  await restoreDraft();
  assert.equal(state.config.gateway_access,null);
  assert(renderGatewayAccessEditor(state.config).includes(escape(edited.value)));
  // A New Run opened from another page also has no form to recapture.
  delete nodes['#new-run-form'];state.config=previous;state.gatewayAccessDraft={};
  await startNewRun();
  assert.equal(sent.at(-1).body.gateway_access,null);
  assert(renderGatewayAccessEditor(state.config).includes(escape(edited.value)));
}
console.log(JSON.stringify({ordinary,exported,active,sent}));
''', RunConfig.model_validate(raw))
    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)
    async with browser(tmp_path) as client:
        for request in value["sent"]:
            response = await client.post(request["path"], json=request["body"])
            assert response.status_code == (202 if request["path"] == "/api/runs" else 200), response.text
            if request["path"] == "/api/runs":
                assert (await finished(client, response.json()["id"]))["state"] == "completed"
        if server_only:
            response = await client.post("/api/config/validate", json=value["active"])
            assert response.status_code == 422, response.text
            assert "gateway_access" in response.text
    ordinary, exported = (RunConfig.model_validate(value[key]) for key in ("ordinary", "exported"))
    assert ordinary.checks == ["access", *companions]
    assert exported.gateway_access is None
    assert ordinary.gateway_attribution == exported.gateway_attribution
    assert ordinary.gateway_policy == exported.gateway_policy


def test_gateway_results_show_expected_observed_and_unverified():
    browser_logic(r'''
const html=renderGatewayAccessResults({qualification:'Limited declared cases only.',cases:[
{name:'valid',identity:'allowed',expected:'allow',observed:'allow',status:'pass',http_status:200,reason:'valid_completion'},
{name:'invalid',identity:'denied',expected:'deny',observed:'unverified',status:'inconclusive',http_status:503,reason:'incomplete_or_unsupported_response'}]});
assert.match(html,/Expected/);assert.match(html,/Observed/);assert.match(html,/unverified/);
assert.match(html,/Limited declared cases only/);assert.match(html,/503/);
''', config())


def test_access_report_uses_case_counts_instead_of_completion_metrics():
    browser_logic(r'''
const check={id:'gateway_access',target:'proxy',status:'pass',required:true,summary:'Cases matched.',metrics:{gateway_access:{cases:[
{name:'valid',identity:'allowed',expected:'allow',observed:'allow',status:'pass'},
{name:'invalid',identity:'denied',expected:'deny',observed:'deny',status:'pass'}]}}};
const html=renderCheck(check,{requests:[],checks:[check],manifest:{config}});
assert.match(html,/Cases matched/);assert.match(html,/2 \/ 2/);
assert.doesNotMatch(html,/Requests completed/);assert.doesNotMatch(html,/>0 \/ 0</);
''', config())


def test_expected_denial_is_not_appended_as_a_policy_failure():
    from giraffe.models import CheckResult, RequestRecord
    from giraffe.runner import _verdict_summary

    check = CheckResult(id='gateway_access', title='Gateway access policy', target='proxy',
        status='fail', summary='Unexpected access in declared cases.')
    record = RequestRecord(id='denied', target='proxy', fixture_id='expected-denial',
        scenario='gateway_access', check_ids=['gateway_access'], started_at='2026-11-06T00:00:00Z',
        status='failed', http_status=401, error='HTTP 401')
    summary = _verdict_summary(check, config(), [record], '')
    assert summary == check.summary


def test_gateway_access_phase_fallback_matches_canonical_label():
    from giraffe.conditions import _SCENARIOS

    browser_logic("assert.equal(phaseName('gateway_access'), " +
                  json.dumps(_SCENARIOS['gateway_access'][0]) + ");", config())


def test_access_baseline_shows_decisions_as_policy_evidence():
    browser_logic(r'''
const html=renderComparison({baseline:{run_id:'reference',targets:[{target:'proxy',comparisons:[{
  check:'gateway_access',scenario:'invalid',scenario_label:'invalid (denied) · expected deny',
  metric:'policy_acceptance',aggregation:'observed',status:'fail',
  baseline:'deny',current:'allow',baseline_samples:1,current_samples:1,
}]}]}});
assert.match(html,/Gateway access decisions/);
assert.match(html,/invalid \(denied\) · expected deny/);
assert.match(html,/Observed failure/);
assert.doesNotMatch(html,/Response timing/);
''', config())
