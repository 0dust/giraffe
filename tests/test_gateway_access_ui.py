"""Policy setup and results survive browser form and report round trips."""
from giraffe.models import RunConfig
from tests.test_gateway_access import config
from tests.test_ux_catalogue import browser_logic


def test_gateway_policy_form_roundtrip_and_validation():
    value = browser_logic(r'''
const markup=renderTestCard(bootstrap.checks.find(c=>c.id==='gateway_access'),config);
assert.match(markup,/Declared cases/);assert.match(markup,/Environment references/);
const values=new FormData();values.set('gateway_access_rendered','1');
values.set('gateway_access_profile','litellm');values.set('gateway_access_request_timeout_seconds','3');
values.set('gateway_access_max_output_tokens','8');
values.set('gateway_access_cases','valid allowed allow\ninvalid denied deny');
values.set('gateway_access_identities_0',JSON.stringify(config.targets[0].test_identities));
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


def test_gateway_results_show_expected_observed_and_unverified():
    browser_logic(r'''
const html=renderGatewayAccessResults({qualification:'Limited declared cases only.',cases:[
{name:'valid',identity:'allowed',expected:'allow',observed:'allow',status:'pass',http_status:200,reason:'valid_completion'},
{name:'invalid',identity:'denied',expected:'deny',observed:'unverified',status:'inconclusive',http_status:503,reason:'incomplete_or_unsupported_response'}]});
assert.match(html,/Expected/);assert.match(html,/Observed/);assert.match(html,/unverified/);
assert.match(html,/Limited declared cases only/);assert.match(html,/503/);
''', config())
