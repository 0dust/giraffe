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
    import json
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
