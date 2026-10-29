from tests.evidence_helpers import workload_records, workload_payloads
import copy
import json
import time

import httpx
import pytest
from pydantic import ValidationError

from giraffe.models import RunConfig, ToolConversationFixture, ToolFixture
from giraffe.runner import run_suite
from giraffe.tool_schema import tool_call_fingerprint
from tests.fake_endpoint import FakeEndpoint


def fixture(**updates):
    value = {'fixture_id': 'weather-result',
        'messages': [{'role': 'user', 'content': 'Check Warsaw weather then report its observation.'}],
        'tools': [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {
            'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city'],
            'additionalProperties': False}}}],
        'steps': [{'expected_calls': [{'name': 'get_weather', 'arguments': {'city': 'Warsaw'},
            'result': {'temperature_c': 15, 'conditions': 'sunny', 'observation_id': '{{observation_id}}'}}]}],
        'final_content': [{'mode': 'contains', 'value': '"temperature_c": 15'},
                          {'mode': 'contains', 'value': '"conditions": "sunny"'},
                          {'mode': 'contains', 'value': '{{observation_id}}'}]}
    return value | updates


def config(endpoint, conversations=None, **updates):
    return RunConfig.model_validate({'targets': [{'name': 'test', 'url': endpoint.url, 'model': 'fake'}],
        'checks': ['tools'], 'tool_checks': {'conversations': conversations or [fixture()]},
        'max_requests': 16, 'max_duration_seconds': 10, 'request_timeout_seconds': 2,
        'max_output_tokens': 128, 'context_limit': 8192, 'retention': 'all', **updates})


def timeout_tool_followups(monkeypatch):
    """Inject a transport timeout only after a real tool call has been answered."""
    original = httpx.AsyncHTTPTransport.handle_async_request
    timed_out = []

    async def handle(transport, request):
        if request.method == 'POST' and request.url.path.endswith('/chat/completions'):
            payload = json.loads(request.content)
            if any(message.get('role') == 'tool' for message in payload['messages']):
                timed_out.append(payload)
                raise httpx.ReadTimeout('controlled tool-result timeout', request=request)
        return await original(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', handle)
    return timed_out


@pytest.fixture
def final_turn_timeout(monkeypatch):
    from giraffe.runner import _Run

    # Exercise the final conversation timeout without imposing a 50 ms socket
    # deadline on unrelated successful single-response requests.
    original_request = _Run.request

    async def final_turn_deadline(self, target, client, spec, *args, **kwargs):
        if spec.workload.get('selection') == 'conversation' and spec.workload.get('stage') == 'final':
            spec = spec.model_copy(update={'request_timeout_seconds': .05})
        return await original_request(self, target, client, spec, *args, **kwargs)

    monkeypatch.setattr(_Run, 'request', final_turn_deadline)


async def test_complete_and_stream_preserve_actual_history_and_consume_stub():
    with FakeEndpoint(mode='conversation_success') as endpoint:
        report = await run_suite(config(endpoint))
        sent = workload_payloads(endpoint.requests)
    assert report.overall == 'pass'
    assert len(workload_records(report.requests)) == len(sent) == 4
    for initial, final, first_payload, final_payload in zip(workload_records(report.requests)[::2], workload_records(report.requests)[1::2], sent[::2], sent[1::2]):
        assert initial.tool_conversation['status'] == final.tool_conversation['status'] == 'pass'
        assert final_payload['messages'][1] == initial.assistant_message
        assert final_payload['messages'][1]['content'] == 'Checking.'
        assert final_payload['messages'][1]['tool_calls'] == initial.tool_calls
        assert final_payload['messages'][2]['tool_call_id'] == initial.tool_calls[0]['id']
        assert final_payload['tool_choice'] == first_payload['tool_choice'] == 'auto'
        assert 'parallel_tool_calls' not in final_payload
        stub = json.loads(final_payload['messages'][2]['content'])
        assert json.loads(final.output) == stub
        assert stub['observation_id'] not in first_payload['messages'][0]['content']
        assert initial.tool_conversation['calls'][0]['supplied_in_request'] == final.id
    assert workload_records(report.requests)[0].tool_conversation['session_id'] != workload_records(report.requests)[2].tool_conversation['session_id']
    assert sent[1]['messages'][2]['content'] != sent[3]['messages'][2]['content']


@pytest.mark.parametrize('mode,reason', [
    ('conversation_repeat', 'repeated_satisfied_call'),
    ('conversation_repeat_truncated', 'repeated_satisfied_call'),
    ('conversation_wrong_answer', 'final_answer_mismatch'),
    ('conversation_wrong_city', 'unexpected_call'),
    ('conversation_malformed', 'invalid_call'),
    ('conversation_missing_id', 'protocol_or_api_failure'),
    ('conversation_duplicate_id', 'protocol_or_api_failure'),
])
async def test_failures_are_classified_without_executing_unexpected_calls(mode, reason):
    with FakeEndpoint(mode=mode) as endpoint:
        report = await run_suite(config(endpoint))
    assert report.overall == 'fail'
    outcomes = next(check for check in report.checks if check.id == 'tools').metrics['conversations']
    assert len(outcomes) == 2
    assert {item['reason'] for item in outcomes} == {reason}
    if reason == 'repeated_satisfied_call':
        for first, final in zip(workload_records(report.requests)[::2], workload_records(report.requests)[1::2]):
            assert first.tool_calls[0]['id'] != final.tool_calls[0]['id']
            assert final.tool_conversation['repetition_count'] == 1
            assert final.score is False
    if reason in {'unexpected_call', 'invalid_call', 'protocol_or_api_failure'}:
        assert len(workload_records(report.requests)) == 2


@pytest.mark.parametrize('limit', [1, 2, 3])
async def test_budget_exhaustion_never_promotes_first_stage_to_conversation_pass(limit):
    with FakeEndpoint(mode='conversation_success') as endpoint:
        report = await run_suite(config(endpoint, max_requests=limit))
    assert len(report.requests) == len(workload_records(report.requests)) == limit
    assert report.overall == 'inconclusive'
    assert next(check for check in report.checks if check.id == 'tools').metrics['missing_cases']
    if limit == 1:
        assert workload_records(report.requests)[0].score is True
        assert workload_records(report.requests)[0].tool_conversation['calls'][0]['supplied_in_request'] is None


@pytest.mark.parametrize('mode', ['conversation_truncated', 'conversation_timeout'])
async def test_incomplete_followup_is_not_a_wrong_answer_or_pass(mode, monkeypatch):
    if mode == 'conversation_timeout':
        timed_out = timeout_tool_followups(monkeypatch)
        endpoint_mode, reason = 'conversation_success', 'timeout'
    else:
        endpoint_mode, reason = mode, 'truncated'
    with FakeEndpoint(mode=endpoint_mode) as endpoint:
        report = await run_suite(config(endpoint))
    assert report.overall == 'inconclusive'
    outcomes = next(check for check in report.checks if check.id == 'tools').metrics['conversations']
    assert len(outcomes) == 2
    assert all(item['status'] == 'inconclusive' and item['reason'] == reason for item in outcomes)
    records = workload_records(report.requests)
    assert len(records) == 4
    assert all(record.status == 'completed' and record.score is True for record in records[::2])
    if mode == 'conversation_timeout':
        assert [payload['stream'] for payload in timed_out] == [False, True]
        assert all(record.status == 'timeout' and record.termination_reason == 'read_timeout'
                   for record in records[1::2])


async def test_early_eof_keeps_complete_and_stream_coverage_distinct():
    with FakeEndpoint(mode='conversation_eof') as endpoint:
        report = await run_suite(config(endpoint))
    outcomes = next(check for check in report.checks if check.id == 'tools').metrics['conversations']
    assert [item['status'] for item in outcomes] == ['pass', 'fail']
    assert report.overall == 'fail'


@pytest.mark.parametrize('change', [
    {'steps': []}, {'final_content': []},
    {'final_content': [{'mode': 'contains', 'value': 'sunny'}]},
    {'options': {'tool_choice': 'none'}}, {'options': {'parallel_tool_calls': False}},
])
def test_invalid_conversation_contracts_rejected(change):
    with pytest.raises(ValidationError):
        ToolConversationFixture.model_validate(fixture(**change))


def test_repeating_success_requires_a_new_question_but_error_retry_is_allowed():
    value = fixture()
    value['steps'].append(copy.deepcopy(value['steps'][0]))
    with pytest.raises(ValidationError, match='successful operation'):
        ToolConversationFixture.model_validate(value)
    value['steps'][1]['user_message'] = 'Check again for my new question.'
    ToolConversationFixture.model_validate(value)
    value['steps'][1].pop('user_message')
    value['steps'][0]['expected_calls'][0]['outcome'] = 'error'
    ToolConversationFixture.model_validate(value)


def test_semantic_fingerprint_ignores_key_order_and_numeric_spelling_but_not_types():
    assert tool_call_fingerprint('f', {'a': 1, 'b': [2.0]}) == tool_call_fingerprint('f', {'b': [2], 'a': 1.0})
    assert tool_call_fingerprint('f', {'a': True}) != tool_call_fingerprint('f', {'a': 1})
    assert tool_call_fingerprint('f', {'a': [1, 2]}) != tool_call_fingerprint('f', {'a': [2, 1]})
    assert tool_call_fingerprint('f', {'city': 'Warsaw'}) != tool_call_fingerprint('f', {'city': 'Paris'})


@pytest.mark.parametrize('reuse', ['retry', 'later', 'changed-city'])
async def test_declared_followup_operations_do_not_trigger_blanket_function_loop_rule(reuse):
    value = fixture()
    value['steps'].append(copy.deepcopy(value['steps'][0]))
    if reuse == 'retry':
        value['steps'][0]['expected_calls'][0].update(outcome='error', result={'error': 'try again'})
    else:
        value['steps'][1]['user_message'] = 'Check Warsaw again.' if reuse == 'later' else 'Now check Paris.'
        if reuse == 'changed-city':
            value['steps'][1]['expected_calls'][0]['arguments']['city'] = 'Paris'
    with FakeEndpoint(mode='conversation_success') as endpoint:
        report = await run_suite(config(endpoint, [value]))
    assert report.overall == 'pass'
    assert len(workload_records(report.requests)) == 6
    assert all(item['repetition_count'] == 0 for item in next(check for check in report.checks if check.id == 'tools').metrics['conversations'])


@pytest.mark.parametrize('policy', ['all', 'failures', 'none'])
@pytest.mark.parametrize('mode', ['conversation_success', 'conversation_repeat'])
async def test_retention_covers_dynamic_history_results_and_assertions(policy, mode, tmp_path):
    from giraffe.models import RunReport
    from giraffe.reporting import write_report
    with FakeEndpoint(mode=mode) as endpoint:
        report = await run_suite(config(endpoint, retention=policy))
    marker = workload_records(report.requests)[0].tool_conversation['calls'][0]['result']['observation_id']
    json_path, html_path = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text())
    keep = policy == 'all' or (policy == 'failures' and mode == 'conversation_repeat')
    assert (marker in json_path.read_text()) is keep
    assert (marker in html_path.read_text()) is keep
    assert marker in report.model_dump_json()  # Export must not mutate the working report.
    for record in workload_records(saved.requests):
        assert bool(record.evaluation.messages) is keep
        assert bool(record.assistant_message) is keep
        assert record.tool_conversation['session_id']
    assert workload_records(saved.requests)[0].tool_conversation['calls'][0]['fingerprint']
    assert 'Tool-result conversation' in html_path.read_text()


async def test_parallel_calls_link_results_by_actual_ids_after_stream_reconstruction():
    value = fixture()
    value['messages'][0]['content'] += ' for two cities'
    second = copy.deepcopy(value['steps'][0]['expected_calls'][0])
    second['arguments']['city'] = 'Paris'
    value['steps'][0]['expected_calls'].append(second)
    with FakeEndpoint(mode='conversation_success') as endpoint:
        report = await run_suite(config(endpoint, [value]))
        sent = workload_payloads(endpoint.requests)
    assert report.overall == 'pass'
    for first, payload in zip(workload_records(report.requests)[::2], sent[1::2]):
        assert len(first.tool_calls) == 2
        assert [m['tool_call_id'] for m in payload['messages'] if m['role'] == 'tool'] == [
            call['id'] for call in first.tool_calls]
        assert [json.loads(c['function']['arguments'])['city'] for c in first.tool_calls] == ['Warsaw', 'Paris']


async def test_two_fixtures_have_isolated_histories_and_operation_sets():
    values = [fixture(), fixture(fixture_id='second')]
    with FakeEndpoint(mode='conversation_success') as endpoint:
        report = await run_suite(config(endpoint, values))
        sent = workload_payloads(endpoint.requests)
    assert report.overall == 'pass'
    assert len({r.tool_conversation['session_id'] for r in workload_records(report.requests)}) == 4
    assert [len(p['messages']) for p in sent] == [1, 3] * 4
    assert len({json.loads(p['messages'][-1]['content'])['observation_id'] for p in sent[1::2]}) == 4


async def test_context_admission_stops_before_sending_oversized_history():
    value = fixture()
    value['steps'][0]['expected_calls'][0]['result']['padding'] = 'x' * 1024
    with FakeEndpoint(mode='conversation_success') as endpoint:
        report = await run_suite(config(endpoint, [value], context_limit=512))
        assert len(workload_payloads(endpoint.requests)) == 2
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert {o['reason'] for o in outcomes} == {'history_limit'}
    assert report.overall == 'inconclusive'


async def test_timeout_remains_inconclusive_alongside_passing_single_response_fixture(monkeypatch, final_turn_timeout):
    value = fixture()
    single = {k: value[k] for k in ['fixture_id', 'messages', 'tools']}
    single.update(fixture_id='single', messages=[{'role': 'user', 'content': 'Single response: check Warsaw weather.'}],
                  expected_calls=[{'name': 'get_weather', 'arguments': {'city': 'Warsaw'}}])
    delayed_modes = []
    with FakeEndpoint(mode='conversation_timeout') as endpoint:
        handler = endpoint._server.RequestHandlerClass
        original = handler._conversation

        def delayed_single_response(self, payload, mode):
            if payload['messages'][0]['content'].startswith('Single response:'):
                delayed_modes.append(payload['stream'])
                time.sleep(.075)  # Longer than the conversation-only final-turn deadline.
            return original(self, payload, mode)

        monkeypatch.setattr(handler, '_conversation', delayed_single_response)
        cfg = config(endpoint)
        cfg.tool_checks.fixtures = [ToolFixture.model_validate(single)]
        report = await run_suite(cfg)
    assert report.overall == 'inconclusive', [r.model_dump(include={
        'scenario', 'status', 'valid', 'score', 'error', 'score_message', 'http_status',
        'elapsed_ms', 'request_timeout_seconds', 'termination_reason', 'termination_limit_seconds',
        'tool_evaluation', 'tool_conversation', 'tool_calls'}) for r in workload_records(report.requests)]
    assert delayed_modes == [False, True]
    singles = [r for r in workload_records(report.requests) if r.workload.get('tool_fixture_id') == 'single']
    assert len(singles) == 2
    assert all(r.status == 'completed' and r.score is True for r in singles)
    assert all(r.request_timeout_seconds == cfg.request_timeout_seconds for r in singles)
    final_turns = [r for r in workload_records(report.requests) if r.workload.get('stage') == 'final']
    assert len(final_turns) == 2
    assert all(r.status == 'timeout' and r.termination_reason == 'request_timeout'
               and r.request_timeout_seconds == .05 for r in final_turns)
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert len(outcomes) == 2
    assert all(o['reason'] == 'timeout' and o['status'] == 'inconclusive' for o in outcomes)


@pytest.mark.parametrize('invalid_fixture', ['single', 'conversation'])
async def test_conversation_timeout_does_not_mask_independent_fixture_failure(monkeypatch, invalid_fixture):
    timed_out = timeout_tool_followups(monkeypatch)
    value = fixture()
    single = {k: value[k] for k in ['fixture_id', 'messages', 'tools']}
    single.update(fixture_id='single', expected_calls=[{'name': 'get_weather', 'arguments': {
        'city': 'Paris' if invalid_fixture == 'single' else 'Warsaw'}}])
    conversations = [value]
    if invalid_fixture == 'conversation':
        invalid = fixture(fixture_id='wrong-city')
        invalid['steps'][0]['expected_calls'][0]['arguments']['city'] = 'Paris'
        conversations.append(invalid)
    with FakeEndpoint(mode='conversation_success') as endpoint:
        cfg = config(endpoint, conversations)
        cfg.tool_checks.fixtures = [ToolFixture.model_validate(single)]
        report = await run_suite(cfg)
    singles = [r for r in workload_records(report.requests) if r.workload.get('tool_fixture_id') == 'single']
    assert len(singles) == 2
    assert all(r.status == 'completed' and r.score is (invalid_fixture != 'single') for r in singles)
    assert [payload['stream'] for payload in timed_out] == [False, True]
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert [(o['status'], o['reason']) for o in outcomes[:2]] == [('inconclusive', 'timeout')] * 2
    if invalid_fixture == 'conversation':
        assert [(o['status'], o['reason']) for o in outcomes[2:]] == [('fail', 'unexpected_call')] * 2
    else:
        assert len(outcomes) == 2
    assert report.overall == 'fail'


async def test_stop_after_errors_leaves_unstarted_sessions_inconclusive(monkeypatch):
    timed_out = timeout_tool_followups(monkeypatch)
    with FakeEndpoint(mode='conversation_success') as endpoint:
        report = await run_suite(config(endpoint, stop_after_errors=1))
    records = workload_records(report.requests)
    assert len(records) == 2
    assert records[0].status == 'completed' and records[0].score is True
    assert records[1].status == 'timeout' and records[1].termination_reason == 'read_timeout'
    assert [payload['stream'] for payload in timed_out] == [False]
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert [o['reason'] for o in outcomes] == ['timeout', 'budget_exhausted']
    assert report.overall == 'inconclusive'


def test_conversation_editor_preserves_fixtures_and_validates_saved_drafts():
    from tests.test_ux_catalogue import browser_logic
    browser_logic(r"""
const conversations=[{fixture_id:'result',steps:[{expected_calls:[{name:'f',arguments:{},result:'{{observation_id}}'}]}]}];
config.tool_checks={fixtures:[{fixture_id:'single'}],modes:['complete']};
const values=new Map([['profile_edits_tools','["conversations"]'],['profile_tools_conversations',JSON.stringify(conversations)]]);
const result=configureProfiles(config,values,{});
assert.equal(JSON.stringify(result.tool_checks.conversations),JSON.stringify(conversations));
assert.equal(JSON.stringify(result.tool_checks.fixtures),JSON.stringify(config.tool_checks.fixtures));
assert.equal(JSON.stringify(configureProfiles(result,new Map(),{})),JSON.stringify(result));
const check=bootstrap.checks.find(c=>c.id==='tools');
assert.match(renderProfileEditor(check,result),/name="profile_tools_conversations"/);
result.tool_checks.fixtures=[];
assert.match(effectiveCheckSummary(check,result),/1 tool-result conversations/);
assert.doesNotMatch(effectiveCheckSummary(check,result),/Built-in weather/);
values.set('profile_tools_conversations','[');
assert.throws(()=>configureProfiles(config,values,{}),/JSON array/);
const draft=configureProfiles(config,values,{}, {allowInvalid:true});
assert.equal(draft.tool_checks.conversations,'[');
assert.throws(()=>configureProfiles(draft,new Map(),{}),/JSON array/);
const html=renderToolEvaluation({tool_evaluation:{},tool_conversation:{reason:'<script>unsafe</script>'}});
assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
""")


async def test_api_preview_run_export_and_bounded_plan(tmp_path):
    from tests.test_web import browser, finished, start
    with FakeEndpoint(mode='conversation_success') as endpoint:
        async with browser(tmp_path) as client:
            preview = await client.post('/api/config/preview', json=config(endpoint).model_dump(mode='json'))
            assert preview.status_code == 200
            normalized = preview.json()['config']
            assert normalized['tool_checks']['conversations'][0]['steps'][0]['expected_calls'][0]['outcome'] == 'success'
            command = await client.post('/api/config/cli', json={'config': normalized})
            assert command.status_code == 200
            assert 'observation_id' in command.json()['command']
            identifier = await start(client, normalized)
            result = await finished(client, identifier)
            assert result['report']['overall'] == 'pass'
            assert len(workload_records(result['report']['requests'])) == 4
            saved = (await client.get(f'/api/runs/{identifier}/config.json')).json()
            assert saved['tool_checks'] == normalized['tool_checks']
            exported = (await client.get(f'/api/runs/{identifier}/report.json')).json()
            assert exported['requests'][-1]['tool_conversation']['reason'] == 'completed'
    from giraffe.planning import request_plan
    plan = request_plan(RunConfig.model_validate(normalized))
    row = next(row for row in plan['rows'] if row['id'] == 'tools')
    assert row['kind'] == 'bounded'
    assert row['requests_per_target'] == 4


async def test_result_and_final_assertions_participate_in_baseline_identity():
    from giraffe.reporting import _settings_reasons
    with FakeEndpoint(mode='conversation_success') as endpoint:
        base = fixture()
        baseline = await run_suite(config(endpoint, [base]))
        base['steps'][0]['expected_calls'][0]['result']['conditions'] = 'rain'
        current = await run_suite(config(endpoint, [base]))
    assert baseline.manifest['fixture_pack'] != current.manifest['fixture_pack']
    assert 'Fixture pack identity differs.' in _settings_reasons(current, baseline)


@pytest.mark.parametrize('mode,reason', [
    ('conversation_wrong_marker', 'final_answer_mismatch'),
    ('conversation_initial_text', 'unexpected_call'),
    ('conversation_unknown_field', 'protocol_or_api_failure'),
])
async def test_unusable_history_and_invented_observations_cannot_pass(mode, reason):
    with FakeEndpoint(mode=mode) as endpoint:
        report = await run_suite(config(endpoint))
    assert report.overall == 'fail'
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert {o['reason'] for o in outcomes} == {reason}


async def test_repetition_uses_semantics_not_argument_bytes_or_call_ids():
    value = fixture()
    value['tools'][0]['function']['parameters']['properties']['days'] = {'type': 'number'}
    value['steps'][0]['expected_calls'][0]['arguments']['days'] = 1
    with FakeEndpoint(mode='conversation_repeat_semantic') as endpoint:
        report = await run_suite(config(endpoint, [value]))
    assert report.overall == 'fail'
    for initial, repeated in zip(workload_records(report.requests)[::2], workload_records(report.requests)[1::2]):
        assert initial.tool_calls[0]['function']['arguments'] != repeated.tool_calls[0]['function']['arguments']
        assert initial.tool_calls[0]['id'] != repeated.tool_calls[0]['id']
        assert initial.tool_conversation['calls'][0]['fingerprint'] == repeated.tool_conversation['calls'][0]['fingerprint']
        assert repeated.tool_conversation['reason'] == 'repeated_satisfied_call'


async def test_live_completion_events_publish_final_conversation_verdicts():
    events = []
    with FakeEndpoint(mode='conversation_wrong_answer') as endpoint:
        report = await run_suite(config(endpoint), progress=events.append)
    all_completions = [e['request'] for e in events if e['event'] == 'request_completed']
    assert len(all_completions) == len(report.requests)
    completions = workload_records(all_completions)
    assert [e['score'] for e in completions] == [True, False, True, False]
    assert [r.score for r in workload_records(report.requests)] == [e['score'] for e in completions]
    assert all('output' not in e and 'assistant_message' not in e for e in all_completions)


async def test_assistant_null_content_and_reasoning_survive_both_transports():
    with FakeEndpoint(mode='conversation_reasoning') as endpoint:
        report = await run_suite(config(endpoint))
        payloads = workload_payloads(endpoint.requests)
    assert report.overall == 'pass'
    for payload in payloads[1::2]:
        assistant = payload['messages'][1]
        assert assistant['content'] is None
        assert assistant['reasoning_content'] == 'Need weather.'
        assert assistant['tool_calls'][0]['function']['name'] == 'get_weather'


async def test_run_duration_limit_leaves_no_false_conversation_pass():
    with FakeEndpoint(mode='conversation_timeout') as endpoint:
        report = await run_suite(config(endpoint, max_duration_seconds=.05))
    assert report.overall == 'inconclusive'
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert all(o['status'] == 'inconclusive' for o in outcomes)
    assert len(workload_records(report.requests)) <= 2


async def test_mixed_fixture_timeout_keeps_independent_single_response_evidence(final_turn_timeout):
    value = fixture()
    single = ToolFixture.model_validate({k: value[k] for k in ['fixture_id', 'messages', 'tools']} | {
        'fixture_id': 'single', 'expected_calls': [{'name': 'get_weather', 'arguments': {'city': 'Warsaw'}}]})
    with FakeEndpoint(mode='conversation_timeout') as endpoint:
        cfg = config(endpoint)
        cfg.tool_checks.fixtures = [single]
        report = await run_suite(cfg)
    singles = [r for r in workload_records(report.requests) if r.workload.get('tool_fixture_id') == 'single']
    assert len(singles) == 2
    assert all(r.score is True and r.status == 'completed' for r in singles), [r.model_dump(include={
        'scenario', 'status', 'valid', 'score', 'error', 'score_message', 'http_status',
        'elapsed_ms', 'request_timeout_seconds', 'termination_reason', 'termination_limit_seconds',
        'tool_evaluation', 'tool_calls'}) for r in singles]
    assert all(r.request_timeout_seconds == cfg.request_timeout_seconds for r in singles)
    final_turns = [r for r in workload_records(report.requests) if r.workload.get('stage') == 'final']
    assert len(final_turns) == 2
    assert all(r.status == 'timeout' and r.request_timeout_seconds == .05
               and r.termination_limit_seconds == .05 for r in final_turns)
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert len(outcomes) == 2
    assert all(o['reason'] == 'timeout' and o['status'] == 'inconclusive' for o in outcomes)
    assert report.overall == 'inconclusive'


def test_duplicate_successful_operation_in_one_step_is_not_a_valid_fixture():
    value = fixture()
    value['steps'][0]['expected_calls'] *= 2
    with pytest.raises(ValidationError, match='successful operation'):
        ToolConversationFixture.model_validate(value)


@pytest.mark.parametrize('invalid', ['marker-in-prompt', 'missing-result-marker', 'too-many-steps',
                                      'nonfinite-result', 'invalid-arguments', 'oversized-result'])
def test_conversation_configuration_rejects_unbounded_or_unprovable_inputs(invalid):
    value = fixture()
    call = value['steps'][0]['expected_calls'][0]
    if invalid == 'marker-in-prompt':
        value['messages'][0]['content'] += ' {{observation_id}}'
    elif invalid == 'missing-result-marker':
        call['result'].pop('observation_id')
    elif invalid == 'too-many-steps':
        value['steps'] *= 5
    elif invalid == 'nonfinite-result':
        call['result']['temperature_c'] = float('nan')
    elif invalid == 'invalid-arguments':
        call['arguments'] = {'city': 15}
    else:
        call['result']['padding'] = 'x' * 65537
    with pytest.raises(ValidationError):
        ToolConversationFixture.model_validate(value)


def test_fixture_ids_are_unique_across_single_response_and_conversations():
    from giraffe.models import ToolChecks
    value = fixture()
    single = {k: value[k] for k in ['fixture_id', 'messages', 'tools']}
    single['expected_calls'] = [{'name': 'get_weather', 'arguments': {'city': 'Warsaw'}}]
    with pytest.raises(ValidationError, match='unique'):
        ToolChecks.model_validate({'fixtures': [single], 'conversations': [value]})


async def test_conflicting_stream_call_ids_fail_before_supplying_any_result():
    with FakeEndpoint(mode='conversation_conflicting_id') as endpoint:
        report = await run_suite(config(endpoint))
        sent = workload_payloads(endpoint.requests)
    outcomes = next(c for c in report.checks if c.id == 'tools').metrics['conversations']
    assert [o['status'] for o in outcomes] == ['pass', 'fail']
    assert outcomes[1]['reason'] == 'protocol_or_api_failure'
    stream_requests = [p for p in sent if p['stream']]
    assert len(stream_requests) == 1
    assert all(m['role'] != 'tool' for m in stream_requests[0]['messages'])


async def test_verified_repeat_survives_later_stream_timeout():
    with FakeEndpoint(mode='conversation_repeat_timeout') as endpoint:
        report = await run_suite(config(endpoint, request_timeout_seconds=.05))
    assert report.overall == 'fail'
    stream_final = workload_records(report.requests)[-1]
    assert stream_final.stream and stream_final.status == 'timeout'
    assert stream_final.tool_conversation['reason'] == 'repeated_satisfied_call'
    assert stream_final.score is False
