"""Regression cases for the second core reliability review; no local sockets."""

import hashlib
import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.fixtures import final_answer, score_response
from tests.test_fixtures import config, record
from tests.test_scoring_reliability import fixture


@pytest.mark.parametrize('tag', ['think', 'thinking', 'reasoning'])
def test_prompt_opened_reasoning_uses_last_closing_tag(tag):
    output = f'Consider 14.</{tag}>More reasoning.</{tag}> 4'
    assert final_answer(output) == ('4', True, False)
    assert score_response(fixture(), record(fixture(), output)).score is True


@pytest.mark.parametrize('scorer,expected,output,schema', [
    ('exact', '42', '14', None),
    ('exact', '42', '42 extra', None),
    ('json', {'count': 42}, '{"count":4}', {}),
    ('json', None, '{"count":"wrong"}', {'type': 'object', 'properties': {'count': {'type': 'integer'}}}),
    ('json', None, 'not JSON', {}),
])
def test_length_finish_cannot_excuse_already_wrong_answers(scorer, expected, output, schema):
    spec = fixture(scorer, expected, **({'schema': schema} if schema is not None else {}))
    assert score_response(spec, record(spec, output, finish_reason='length')).score is False


@pytest.mark.parametrize('expected,output', [('.5', '1.5'), ('5', '1.5'), ('5', '5,000'), ('1,000', '21,000')])
def test_contains_does_not_match_part_of_number(expected, output):
    spec = fixture('contains', expected)
    assert score_response(spec, record(spec, output)).score is False


@pytest.mark.asyncio
async def test_payload_omits_none_and_hash_uses_final_answer():
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'reasoning</think>4'}, 'finish_reason': 'stop'}]})
    configured = config(stream=False)
    client = LLMClient(configured.targets[0], configured)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        result = await client.execute(fixture(options={'stop': None}))
    assert 'stop' not in seen[0]
    assert result.answer_hash == hashlib.sha256(b'4').hexdigest()


@pytest.mark.parametrize('exception', [httpx.ConnectTimeout, httpx.ConnectError])
@pytest.mark.asyncio
async def test_connect_failure_is_transport_failure(exception):
    def handle(request):
        raise exception('offline')
    configured = config(stream=False)
    client = LLMClient(configured.targets[0], configured)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        result = await client.execute(fixture())
    assert result.status == 'failed' and result.http_status is None
    assert exception.__name__ in result.error


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_known_answer_runner_preserves_user_and_case_temperature(monkeypatch, stream):
    from giraffe import runner
    from giraffe.fixtures import deterministic_options
    from tests.test_load_reliability import LoadClient
    from tests.test_runner import config as run_config
    class Endpoint(LoadClient):
        calls = []
    monkeypatch.setattr(runner, 'LLMClient', Endpoint)
    configured = run_config(checks=['correctness', 'json', 'cancellation', 'fairness'],
                            structured_json=True, stream=stream, request_options={'temperature': .7})
    await runner.run_suite(configured)
    assert Endpoint.calls
    assert all('temperature' not in item[0].options for item in Endpoint.calls)
    assert deterministic_options(configured, {'temperature': .3}) == {'temperature': .3}


@pytest.mark.parametrize('scenario', ['context', 'fairness_long', 'client_cancel', 'cancellation_probe'])
@pytest.mark.parametrize('failure', ['http', 'protocol', 'timeout'])
@pytest.mark.asyncio
async def test_real_stress_failures_reach_serving_and_error_stop(scenario, failure):
    from giraffe import runner
    from giraffe.fixtures import builtin_fixtures
    from tests.test_execution_reliability import Client, config as run_config
    configured = run_config(checks=['access', 'serving', 'context'], stop_after_errors=1)
    run = runner._Run(configured, None, None)
    class Broken(Client):
        async def execute(self, spec, **kwargs):
            result = await super().execute(spec, **kwargs)
            result.status, result.valid = ('timeout' if failure == 'timeout' else 'failed'), False
            result.http_status = 503 if failure == 'http' else None
            result.error = 'request timeout' if failure == 'timeout' else 'invalid SSE'
            return result
    spec = runner._spec(builtin_fixtures(configured)['context'][0], scenario, ['context'])
    result = await run.request(configured.targets[0], Broken(), spec)
    serving = next(c for c in runner._checks(run, configured.targets[0]) if c.id == 'serving')
    assert run.errors == (0 if failure == 'timeout' else 1)
    assert ('serving' in result.check_ids) is (failure != 'timeout')
    if failure != 'timeout':
        assert serving.status == 'fail'
        assert run.stop_reason() == 'error threshold reached'
        if scenario == 'context':
            assert next(c for c in runner._checks(run, configured.targets[0]) if c.id == 'context').status == 'fail'


def test_context_reduced_position_coverage_cannot_pass():
    from giraffe import runner
    from giraffe.fixtures import builtin_fixtures
    from tests.test_execution_reliability import config as run_config
    configured = run_config(checks=['context'], context_limit=128)
    run = runner._Run(configured, None, None)
    for spec in builtin_fixtures(configured)['context']:
        result = record(spec, spec.expected, target='t', input_tokens=120, workload=spec.workload)
        run.records.append(score_response(spec, result))
    result = next(c for c in runner._checks(run, configured.targets[0]) if c.id == 'context')
    assert result.status == 'inconclusive'
    assert result.summary == 'reduced fact-position coverage (context_limit too small)'
    assert result.metrics['reduced_position_coverage']


@pytest.mark.asyncio
async def test_ready_uses_models_and_health_without_inference():
    seen = []
    def handle(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(404 if request.url.path.endswith('models') else 200)
    import time
    configured = config()
    client = LLMClient(configured.targets[0], configured)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        assert await client.ready(time.monotonic()+1)
    assert seen == [('GET', '/v1/models'), ('GET', '/health')]


@pytest.mark.asyncio
async def test_nonstreaming_cancellation_has_explicit_reason(monkeypatch):
    from giraffe import runner
    from tests.test_runner import FakeClient, check, config as run_config
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    report = await runner.run_suite(run_config(checks=['cancellation'], stream=False))
    result = check(report, 'cancellation')
    assert result.status == 'inconclusive'
    assert 'client cancellation requires streaming' in result.summary


@pytest.mark.asyncio
async def test_gpu_only_waits_within_run_duration(monkeypatch):
    import time
    from giraffe import runner, telemetry
    from tests.test_gpu_reliability import snapshot
    from tests.test_runner import config as run_config, check
    calls = []
    async def scrape(run, target, phase, **kwargs):
        calls.append(time.monotonic())
        return snapshot(timestamp=time.time()*1000, elapsed=calls[-1]-run.start) | {'status': 'available', 'phase': phase}
    monkeypatch.setattr(telemetry, 'scrape', scrape)
    configured = run_config(checks=['gpu'], max_duration_seconds=.08)
    started = time.monotonic()
    report = await runner.run_suite(configured)
    elapsed = time.monotonic()-started
    assert .05 <= calls[1]-calls[0] < .1
    assert elapsed < .2
    result = check(report, 'gpu')
    assert result.status == 'inconclusive'
    assert 'observation window shorter than exporter refresh' in result.summary
    assert result.metrics['observation_window']['required_seconds'] >= 30


@pytest.mark.asyncio
async def test_documented_context_length_rejection_stays_inconclusive():
    from giraffe import runner
    from giraffe.fixtures import builtin_fixtures
    from tests.test_execution_reliability import config as run_config
    configured = run_config(checks=['serving', 'context'])
    client = LLMClient(configured.targets[0], configured)
    def handle(request):
        return httpx.Response(400, json={'error': {'message': 'Maximum context length exceeded. Private echoed prompt.'}})
    run = runner._Run(configured, None, None)
    spec = runner._spec(builtin_fixtures(configured)['context'][0], 'context', ['context'])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        result = await run.request(configured.targets[0], client, spec)
    assert result.error == 'HTTP 400: context length rejection'
    assert run.errors == 0 and 'serving' not in result.check_ids
    assert next(c for c in runner._checks(run, configured.targets[0]) if c.id == 'context').status == 'inconclusive'


@pytest.mark.parametrize('sample_limit', [2, 3])
@pytest.mark.asyncio
async def test_gpu_final_scrape_reuses_bounded_metrics_end_phase(monkeypatch, sample_limit):
    import time
    from giraffe import runner, telemetry
    from tests.test_gpu_reliability import snapshot
    from tests.test_runner import FakeClient, config as run_config
    phases = []
    async def scrape(run, target, phase, **kwargs):
        phases.append(phase)
        return snapshot(timestamp=time.time()*1000, elapsed=time.monotonic()-run.start) | {
            'status': 'available', 'phase': phase}
    monkeypatch.setattr(telemetry, 'scrape', scrape)
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    report = await runner.run_suite(run_config(checks=['serving', 'gpu'], metrics=True,
        metrics_max_samples=sample_limit, gpu_min_observation_seconds=0, max_duration_seconds=.08))
    snapshots = report.observations['targets']['service']['serving_telemetry']['snapshots']
    assert len(phases) == len(snapshots) <= sample_limit
    assert phases[0] == 'start' and phases[-1] == 'end'
    assert 'after' not in phases


@pytest.mark.parametrize('minimum', [0, .025])
@pytest.mark.asyncio
async def test_gpu_observation_window_is_configurable(monkeypatch, minimum):
    import time
    from giraffe import runner, telemetry
    from tests.test_gpu_reliability import snapshot
    from tests.test_runner import config as run_config, check
    async def scrape(run, target, phase, **kwargs):
        return snapshot(timestamp=time.time()*1000, elapsed=time.monotonic()-run.start) | {
            'status': 'available', 'phase': phase}
    monkeypatch.setattr(telemetry, 'scrape', scrape)
    configured = run_config(checks=['gpu'], gpu_min_observation_seconds=minimum,
                            max_duration_seconds=.5, metrics_interval_seconds=1)
    started = time.monotonic()
    report = await runner.run_suite(configured)
    assert time.monotonic()-started < .2
    result = check(report, 'gpu')
    assert result.status == 'pass'
    assert result.metrics['observation_window']['required_seconds'] == minimum
    assert result.metrics['observation_window']['elapsed_seconds'] >= minimum


@pytest.mark.asyncio
async def test_gpu_observation_waits_only_for_remaining_window(monkeypatch):
    from giraffe import runner, telemetry
    from tests.test_gpu_reliability import snapshot
    from tests.test_runner import config as run_config
    run = runner._Run(run_config(checks=['gpu'], gpu_min_observation_seconds=.1), None, None)
    data = run.observations['service']['serving_telemetry']
    data['snapshots'].append(snapshot(elapsed=.02) | {'phase': 'start', 'status': 'available'})
    clock = {'now': run.start + .1}
    waits = []
    async def wait_for(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
        clock['now'] += timeout
        raise TimeoutError
    async def scrape(state, target, phase, **kwargs):
        return snapshot(elapsed=clock['now']-run.start) | {'phase': phase, 'status': 'available'}
    from types import SimpleNamespace
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    monkeypatch.setattr(runner.asyncio, 'wait_for', wait_for)
    monkeypatch.setattr(telemetry, 'scrape', scrape)
    await runner._finish_gpu_observation(run, run.config.targets[0])
    assert waits == [pytest.approx(.02)]
    waits.clear()
    clock['now'] += .1
    await runner._finish_gpu_observation(run, run.config.targets[0])
    assert not waits


def test_gpu_observation_window_default_and_validation():
    from pydantic import ValidationError
    assert config().gpu_min_observation_seconds == 30
    with pytest.raises(ValidationError):
        config(gpu_min_observation_seconds=-1)
