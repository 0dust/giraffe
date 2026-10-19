import json

import pytest

from giraffe.fixtures import score_response
from giraffe.models import RequestRecord, RequestSpec, ToolFixture


def fixture(mode='none', **choice):
    return ToolFixture.model_validate({
        'fixture_id': mode, 'messages': [{'role': 'user', 'content': 'Use get_weather for Delhi.'}],
        'tools': [
            {'type': 'function', 'function': {'name': 'get_time', 'parameters': {
                'type': 'object', 'properties': {}, 'additionalProperties': False}}},
            {'type': 'function', 'function': {'name': 'get_weather', 'parameters': {
                'type': 'object', 'properties': {'city': {'type': 'string'}},
                'required': ['city'], 'additionalProperties': False}}},
        ],
        'choice': {'mode': mode, **choice},
    })


def call(name='get_weather', arguments=None):
    return {'id': 'call_1', 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps({'city': 'Delhi'} if arguments is None else arguments)}}


def evaluate(configured, calls=(), **updates):
    request = RequestSpec(fixture_id=configured.fixture_id, scenario='tools', check_ids=['tools'],
                          messages=[m.model_dump() for m in configured.messages], scorer='tool',
                          tool_expectation=configured)
    values = dict(id='r', target='local', fixture_id=configured.fixture_id, scenario='tools',
                  check_ids=['tools'], started_at='2026-10-17', status='completed', valid=True,
                  output='I cannot access live weather.', finish_reason='stop', tool_calls=list(calls))
    return score_response(request, RequestRecord(**(values | updates)))


@pytest.mark.parametrize('calls,expected', [([], True), ([call()], False)])
def test_none_requires_zero_calls_without_requiring_fabricated_text(calls, expected):
    result = evaluate(fixture(), calls)
    assert result.score is expected
    assert result.tool_evaluation.choice.status == ('pass' if expected else 'fail')
    assert result.tool_evaluation.task.status == 'not_checked'


@pytest.mark.parametrize('mode,rule,calls,passes', [
    ('required', {}, [], False),
    ('required', {}, [call()], True),
    ('required', {}, [call('get_time', {})], True),
    ('named', {'name': 'get_time'}, [call()], False),
    ('named', {'name': 'get_time'}, [call('get_time', {})], True),
    ('named', {'name': 'get_time'}, [], False),
    ('auto', {}, [], True),
    ('auto', {}, [call()], True),
    ('required', {'max_calls': 1}, [call(), call()], False),
    ('required', {'parallel_tool_calls': False}, [call(), call()], False),
    ('required', {'parallel_tool_calls': True}, [call(), call()], True),
])
def test_choice_rules_are_independent_of_task_expectations(mode, rule, calls, passes):
    result = evaluate(fixture(mode, **rule), calls)
    assert result.score is passes
    assert result.tool_evaluation.choice.status == ('pass' if passes else 'fail')
    assert result.tool_evaluation.task.status == 'not_checked'


def test_schema_failure_is_distinct_from_named_choice_compliance():
    result = evaluate(fixture('named', name='get_time'), [call('get_time', {'extra': 1})])
    assert result.score is False
    assert result.tool_evaluation.choice.status == 'pass'
    assert result.tool_evaluation.arguments.status == 'fail'


def test_auto_task_correctness_is_separate_from_mode_compliance():
    item = fixture('auto').model_dump()
    item['expected_calls'] = [{'name': 'get_weather', 'arguments': {'city': 'Delhi'}}]
    result = evaluate(ToolFixture.model_validate(item), [])
    assert result.tool_evaluation.choice.status == 'pass'
    assert result.tool_evaluation.task.status == 'fail'
    assert result.score is False


@pytest.mark.parametrize('support,score,choice', [('required', False, 'fail'), ('discover', None, 'inconclusive')])
@pytest.mark.parametrize('status', [400, 422, 501])
def test_rejection_never_passes_and_does_not_claim_verified_unsupported(support, score, choice, status):
    result = evaluate(fixture('required', support=support), status='failed', valid=False, http_status=status)
    assert result.score is score
    assert result.tool_evaluation.choice.status == choice
    assert result.tool_evaluation.support == 'rejected'
    assert 'unverified' in result.score_message


@pytest.mark.parametrize('updates', [
    {'status': 'timeout', 'valid': False}, {'status': 'cancelled', 'valid': False},
    {'status': 'failed', 'valid': False, 'http_status': 500},
    {'status': 'failed', 'valid': False, 'http_status': 401},
    {'status': 'failed', 'valid': False, 'http_status': 404},
    {'status': 'failed', 'valid': False, 'http_status': 429},
    {'finish_reason': 'length'},
])
def test_missing_calls_from_incomplete_response_never_pass_none(updates):
    result = evaluate(fixture('none', support='discover'), **updates)
    assert result.score is not True
    assert result.tool_evaluation.choice.status == 'inconclusive'
    assert result.tool_evaluation.support != 'rejected'


def test_none_allows_tool_shaped_text_without_structured_calls():
    result = evaluate(fixture('none'), output='{"name":"get_weather","arguments":{"city":"Delhi"}}')
    assert result.score is True


@pytest.mark.parametrize('change', [
    {'mode': 'named'}, {'mode': 'auto', 'name': 'get_time'},
    {'mode': 'named', 'name': 'unknown'}, {'mode': 'required', 'max_calls': 0},
    {'mode': 'required', 'max_calls': True}, {'mode': 'required', 'parallel_tool_calls': 'false'},
])
def test_invalid_rules_reject_before_dispatch(change):
    from pydantic import ValidationError
    item = fixture().model_dump()
    item['choice'] = change
    with pytest.raises(ValidationError):
        ToolFixture.model_validate(item)


@pytest.mark.parametrize('mode,rule,expected', [
    ('none', {}, [{'name': 'get_time'}]),
    ('required', {}, []),
    ('named', {'name': 'get_time'}, [{'name': 'get_weather'}]),
    ('required', {'max_calls': 1}, [{'name': 'get_time'}, {'name': 'get_time'}]),
])
def test_contradictory_task_and_choice_rules_reject(mode, rule, expected):
    from pydantic import ValidationError
    item = fixture(mode, **rule).model_dump()
    item['expected_calls'] = expected
    with pytest.raises(ValidationError, match='contradict'):
        ToolFixture.model_validate(item)


def run_config(endpoint, fixtures, **updates):
    from giraffe.models import RunConfig
    return RunConfig.model_validate({'targets': [{'name': 'local', 'url': endpoint.url, 'model': 'fake'}],
        'checks': ['tools'], 'tool_checks': {'fixtures': fixtures}, 'max_requests': 16,
        'max_duration_seconds': 10, 'request_timeout_seconds': 2, 'concurrency': 2,
        'retention': 'all', **updates})


async def test_honored_choice_matrix_sends_and_retains_exact_rules_for_both_transports():
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    fixtures = [fixture('none'), fixture('required'), fixture('named', name='get_time'), fixture('auto')]
    with FakeEndpoint(mode='tool_choice_honored') as endpoint:
        configuration = run_config(endpoint, fixtures, forced_tool_diagnostic=True,
                                   request_options={'tool_choice': 'none', 'parallel_tool_calls': False})
        report = await run_suite(configuration)
        sent = list(endpoint.requests)
    assert len(sent) == len(report.requests) == 8
    assert report.overall == 'pass'
    result = next(c for c in report.checks if c.id == 'tools')
    assert result.status == 'pass' and result.metrics['missing_cases'] == []
    for record, payload in zip(report.requests, sent, strict=True):
        assert record.tool_evaluation.choice.status == 'pass'
        assert record.tool_request_options['tool_choice'] == payload['tool_choice']
        assert record.tool_request_options['tools'] == payload['tools']
        assert 'parallel_tool_calls' not in payload
    assert [p['stream'] for p in sent] == [False, True] * 4
    assert sent[4]['tool_choice'] == {'type': 'function', 'function': {'name': 'get_time'}}


@pytest.mark.parametrize('endpoint_mode,configured', [
    ('tool_choice_ignored', fixture('none')),
    ('tool_choice_text', fixture('required')),
    ('tool_choice_ignored', fixture('named', name='get_time')),
    ('tool_choice_bad_arguments', fixture('required')),
    ('tool_choice_parallel', fixture('required', parallel_tool_calls=False)),
    ('tool_choice_truncated', fixture('none')),
    ('tool_choice_eof', fixture('none')),
])
async def test_http_success_cannot_hide_contract_or_completeness_failures(endpoint_mode, configured):
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode=endpoint_mode) as endpoint:
        report = await run_suite(run_config(endpoint, [configured]))
    assert report.overall == ('inconclusive' if endpoint_mode == 'tool_choice_truncated' else 'fail')
    assert any(r.score is not True for r in report.requests)
    assert all(r.http_status == 200 for r in report.requests)


@pytest.mark.parametrize('support,status', [('required', 'fail'), ('discover', 'inconclusive')])
async def test_rejected_discovery_is_inconclusive_without_error_rate_overriding_it(support, status):
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode='tool_rejection') as endpoint:
        report = await run_suite(run_config(endpoint, [fixture('required', support=support)]))
    assert report.overall == status
    assert all(r.tool_evaluation.support == 'rejected' for r in report.requests)
    if support == 'discover':
        assert len(report.requests) == 2
        assert report.abort_reason is None
        assert all(r.score is None for r in report.requests)


async def test_unexercised_contract_mode_prevents_pass_with_shared_request_budget():
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode='tool_choice_honored') as endpoint:
        report = await run_suite(run_config(endpoint, [fixture('none'), fixture('required')], max_requests=3))
    result = next(c for c in report.checks if c.id == 'tools')
    assert result.status == 'inconclusive'
    assert len(report.requests) == 3
    assert result.metrics['missing_cases'] == [{'fixture_id': 'required', 'mode': 'stream'}]


@pytest.mark.parametrize('mode', ['none', 'auto'])
def test_tool_call_finish_without_calls_is_a_protocol_failure(mode):
    result = evaluate(fixture(mode), finish_reason='tool_calls')
    assert result.score is False
    assert result.tool_evaluation.protocol.status == 'fail'
    assert result.tool_evaluation.choice.status == 'inconclusive'
    assert result.evaluation_error is None


async def test_each_rule_has_independent_wire_expectations_and_plan_count():
    from giraffe.planning import request_plan
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    fixtures = [fixture('none'), fixture('required', parallel_tool_calls=True),
                fixture('named', name='get_time', parallel_tool_calls=False), fixture('auto')]
    expected = [('none', None), ('required', True),
                ({'type': 'function', 'function': {'name': 'get_time'}}, False), ('auto', None)]
    with FakeEndpoint(mode='tool_choice_honored') as endpoint:
        config = run_config(endpoint, fixtures, forced_tool_diagnostic=True)
        plan = request_plan(config)
        report = await run_suite(config)
        sent = endpoint.requests
    assert report.overall == 'pass'
    assert next(row for row in plan['rows'] if row['id'] == 'tools')['requests_per_target'] == 8
    assert len(sent) == 8
    for i, (choice, parallel) in enumerate(expected):
        for payload in sent[2*i:2*i+2]:
            assert payload['tool_choice'] == choice
            assert payload['tools'] == fixtures[i].tools
            assert payload.get('parallel_tool_calls') is parallel


@pytest.mark.parametrize('endpoint_mode,configured,component', [
    ('tool_choice_ignored', fixture('none'), 'choice'),
    ('tool_choice_text', fixture('required'), 'choice'),
    ('tool_choice_ignored', fixture('named', name='get_time'), 'choice'),
    ('tool_choice_bad_arguments', fixture('required'), 'arguments'),
    ('tool_choice_parallel', fixture('required', parallel_tool_calls=False), 'choice'),
    ('tool_choice_truncated', fixture('none'), 'protocol'),
    ('tool_choice_eof', fixture('none'), 'protocol'),
])
async def test_http_failure_identifies_the_specific_component(endpoint_mode, configured, component):
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode=endpoint_mode) as endpoint:
        report = await run_suite(run_config(endpoint, [configured]))
    assert len(report.requests) == 2
    for record in report.requests:
        if endpoint_mode == 'tool_choice_eof' and not record.stream:
            assert record.score is True
        else:
            assert getattr(record.tool_evaluation, component).status == (
                'inconclusive' if endpoint_mode == 'tool_choice_truncated' else 'fail')
            if component != 'protocol':
                assert record.tool_evaluation.protocol.status == 'pass'
                assert record.valid


async def test_parallel_calls_reassemble_and_validate_with_explicit_permission():
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode='tool_choice_parallel') as endpoint:
        report = await run_suite(run_config(endpoint, [fixture('required', parallel_tool_calls=True)]))
    assert report.overall == 'pass'
    assert len(report.requests) == 2
    for record in report.requests:
        assert len(record.tool_calls) == 2
        assert record.tool_evaluation.arguments.status == 'pass'


@pytest.mark.parametrize('policy', ['all', 'failures', 'none'])
@pytest.mark.parametrize('mode', ['none', 'required'])
async def test_contract_report_retains_rules_and_verdicts_under_every_policy(policy, mode, tmp_path):
    from giraffe.models import RunReport
    from giraffe.reporting import write_report
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode='tool_choice_ignored') as endpoint:
        report = await run_suite(run_config(endpoint, [fixture(mode)], retention=policy))
    json_path, html_path = write_report(report, tmp_path)
    saved = RunReport.model_validate_json(json_path.read_text())
    for record in saved.requests:
        assert record.tool_evaluation.choice.status == ('fail' if mode == 'none' else 'pass')
        assert record.evaluation.tool_expectation.expected_calls is None
        assert record.tool_request_options['tool_choice'] == mode
        assert bool(record.tool_calls) == (policy == 'all' or (policy == 'failures' and mode == 'none'))
    html = html_path.read_text()
    for label in ['Tool choice rule', 'Argument schemas', 'Task expectations', 'Sent tool options',
                  'No task-specific call expectation configured.']:
        assert label in html


def test_fixture_editor_round_trips_contract_only_and_empty_task_assertions():
    from tests.test_ux_catalogue import browser_logic
    browser_logic(r"""
const fixtures=[{fixture_id:'none',messages:[{role:'user',content:'Ask'}],tools:[],choice:{mode:'none',support:'discover'},expected_calls:[]},
 {fixture_id:'required',messages:[{role:'user',content:'Ask'}],tools:[],choice:{mode:'required',parallel_tool_calls:false}}];
const values=new Map([['profile_edits_tools','["fixtures"]'],['profile_tools_fixtures',JSON.stringify(fixtures)]]);
const result=configureProfiles(config,values,{});
assert.equal(JSON.stringify(result.tool_checks.fixtures),JSON.stringify(fixtures));
assert.equal(JSON.stringify(configureProfiles(result,new Map(),{}).tool_checks.fixtures),JSON.stringify(fixtures));
""")


async def test_api_normalizes_runs_and_exports_contract_matrix(tmp_path):
    from tests.fake_endpoint import FakeEndpoint
    from tests.test_web import browser, finished, start
    with FakeEndpoint(mode='tool_choice_honored') as endpoint:
        config = run_config(endpoint, [fixture('none'), fixture('required')]).model_dump(mode='json')
        async with browser(tmp_path) as client:
            preview = await client.post('/api/config/preview', json=config)
            assert preview.status_code == 200
            normalized = preview.json()['config']
            assert normalized['tool_checks']['fixtures'][0]['expected_calls'] is None
            assert normalized['tool_checks']['fixtures'][1]['choice']['mode'] == 'required'
            command = await client.post('/api/config/cli', json={'config': normalized})
            assert command.status_code == 200
            assert 'required' in command.json()['command']
            identifier = await start(client, normalized)
            result = await finished(client, identifier)
            assert result['report']['overall'] == 'pass'
            assert len(result['report']['requests']) == 4
            saved = (await client.get(f'/api/runs/{identifier}/config.json')).json()
            assert saved['tool_checks'] == normalized['tool_checks']
            exported = (await client.get(f'/api/runs/{identifier}/report.json')).json()
            assert exported['requests'][2]['tool_request_options']['tool_choice'] == 'required'


async def test_choice_policy_changes_baseline_identity():
    from giraffe.reporting import _settings_reasons
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode='tool_choice_honored') as endpoint:
        base = fixture('auto').model_dump()
        baseline = await run_suite(run_config(endpoint, [base]))
        base['choice']['mode'] = 'none'
        current = await run_suite(run_config(endpoint, [base]))
    assert baseline.manifest['fixture_pack'] != current.manifest['fixture_pack']
    assert 'Fixture pack identity differs.' in _settings_reasons(current, baseline)


def test_browser_renders_contract_verdicts_and_escapes_evidence():
    from tests.test_ux_catalogue import browser_logic
    record = evaluate(fixture('auto'), output='<script>no</script>').model_dump(mode='json')
    record['tool_request_options'] = {'tool_choice': 'auto', 'tools': [{'description': '<unsafe>'}]}
    browser_logic('const record=' + json.dumps(record) + r""";
const outcomes=renderToolEvaluation(record);
for(const label of ['Tool choice','Argument schemas','Task expectations','Support: observed'])assert(outcomes.includes(label));
assert(!outcomes.includes('Tool calls</strong>'));
const evidence=renderEvaluationEvidence(record,{});
assert(evidence.includes('No task-specific call expectation configured.'));
assert(evidence.includes('Sent tool options'));
assert(evidence.includes('&lt;unsafe&gt;'));
assert(!evidence.includes('<unsafe>'));
""")


async def test_discovery_rejections_still_respect_global_error_stop_bound():
    from giraffe.runner import run_suite
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(mode='tool_rejection') as endpoint:
        report = await run_suite(run_config(endpoint, [fixture('required', support='discover')],
                                            stop_after_errors=1))
    assert len(report.requests) == 1
    assert report.abort_reason is not None
    assert report.overall == 'inconclusive'
    assert report.requests[0].tool_evaluation.support == 'rejected'
    assert next(c for c in report.checks if c.id == 'tools').metrics['missing_cases']


def test_browser_summary_does_not_claim_unused_legacy_diagnostics():
    from tests.test_ux_catalogue import browser_logic
    browser_logic(r"""
config.tool_checks={fixtures:[{fixture_id:'required',choice:{mode:'required'}}],modes:['complete','stream']};
config.forced_tool_diagnostic=true;
const summary=effectiveCheckSummary(bootstrap.checks.find(check=>check.id==='tools'),config);
assert(summary.includes('Choice: required'));
assert(!summary.includes('Includes forced tool diagnostic'));
assert(!summary.includes('Normal tool choice'));
""")
