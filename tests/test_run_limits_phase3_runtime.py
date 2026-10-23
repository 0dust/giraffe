"""Per-check request settings reach the runner and the real HTTPX client, sans sockets."""

import asyncio
import json

import httpx
import pytest

from giraffe import runner
from giraffe.client import LLMClient
from giraffe.fixtures import builtin_fixtures
from giraffe.models import RequestSpec, RunConfig

TARGET = {'name': 'local', 'url': 'http://local/v1', 'model': 'm'}


async def test_a21_runner_and_client_apply_check_timeout_and_output_above_suite_defaults(monkeypatch):
    original = httpx.AsyncClient
    sent = []

    async def answer(request):
        payload = json.loads(request.content)
        sent.append((payload, request.extensions['timeout']))
        await asyncio.sleep(.03)  # Exceeds the suite's default, inside this check's timeout.
        prompt = payload['messages'][-1]['content']
        content = '25' if '17 cards' in prompt else 'B' if 'dax means' in prompt else 'T8R2'
        return httpx.Response(200, json={'model': 'm', 'choices': [
            {'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 30, 'completion_tokens': 1}})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(answer), **kwargs))
    config = RunConfig(targets=[TARGET], checks=['correctness'], stream=False,
                       max_output_tokens=2, request_timeout_seconds=.01,
                       test_options={'correctness': {'concurrency': 1, 'max_output_tokens': 80,
                                                     'request_timeout_seconds': .2}})
    report = await runner.run_suite(config)
    assert len(report.requests) >= 3
    assert all(record.valid and record.status == 'completed' for record in report.requests)
    assert all(record.requested_max_tokens == 80 for record in report.requests)
    assert all(record.elapsed_ms >= 25 for record in report.requests)
    assert all(payload['max_tokens'] == 80 for payload, _ in sent)
    assert all(timeout['connect'] == pytest.approx(.18) for _, timeout in sent)
    assert next(check for check in report.checks if check.id == 'correctness').status == 'pass'
    assert config.max_output_tokens == 2 and config.request_timeout_seconds == .01


async def test_client_respects_spec_settings_without_a_runner(monkeypatch):
    original = httpx.AsyncClient
    sent = []

    async def answer(request):
        sent.append(json.loads(request.content))
        await asyncio.sleep(.02)
        return httpx.Response(200, json={'model': 'm', 'choices': [
            {'message': {'role': 'assistant', 'content': 'hi'}, 'finish_reason': 'stop'}]})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(answer), **kwargs))
    config = RunConfig(targets=[TARGET], checks=['serving'], stream=False,
                       max_output_tokens=2, request_timeout_seconds=.005)
    spec = RequestSpec(fixture_id='own', scenario='warm', check_ids=['serving'], stream=False,
                       messages=[{'role': 'user', 'content': 'hi'}], max_tokens=100,
                       request_timeout_seconds=.2)
    async with LLMClient(config.targets[0], config, max_connections=1) as client:
        record = await client.execute(spec)
    assert record.valid and record.status == 'completed'
    assert record.requested_max_tokens == sent[0]['max_tokens'] == 100


def test_context_fixture_uses_its_check_output_and_context_window():
    config = RunConfig(targets=[TARGET], checks=['context'], max_output_tokens=8, context_limit=4096,
                       test_options={'context': {'max_output_tokens': 96, 'context_limit': 512,
                                                'request_timeout_seconds': 45}})
    effective = runner._check_config(config, 'context')
    specs = builtin_fixtures(effective, check_id='context')['context']
    assert specs and all(spec.max_tokens == 96 for spec in specs)
    assert all(spec.request_timeout_seconds == 45 for spec in specs)
    assert max(spec.input_chars for spec in specs) <= 512 - 96 - 32


@pytest.mark.parametrize('check', ['serving', 'first_output', 'generation', 'context',
                                  'correctness', 'json', 'cancellation', 'recovery'])
async def test_core_builders_place_their_own_request_settings_on_every_spec(check, monkeypatch, local_tokenizer):
    from tests.test_runner import FakeClient

    specs = []

    class Capture(FakeClient):
        async def execute(self, spec, **kwargs):
            specs.append(spec.model_copy(deep=True))
            return await super().execute(spec, **kwargs)

    monkeypatch.setattr(runner, 'LLMClient', Capture)
    options = {'max_output_tokens': 96, 'request_timeout_seconds': .5}
    if check in {'serving', 'generation', 'context', 'correctness', 'json', 'cancellation', 'recovery'}:
        options['samples'] = 2
    if check == 'context':
        options['context_limit'] = 1024
    if check == 'recovery':
        options['sustained_seconds'] = .01
    if check in {'correctness', 'json', 'recovery'}:
        options['concurrency'] = 1
    config = RunConfig(targets=[TARGET], checks=[check], samples=2, max_requests=40,
                       request_timeout_seconds=.02, max_output_tokens=4,
                       traffic={'rates': [1], 'duration_seconds': .01},
                       test_options={check: options})
    await runner.run_suite(config)
    owned = [spec for spec in specs if spec.scenario not in {'initial', 'readiness'}
             or check in {'serving', 'first_output'}]
    assert owned
    assert all(spec.request_timeout_seconds == .5 for spec in owned)
    assert all(spec.max_tokens == (8 if spec.fixture_id.startswith('limits.output_cap') else 96)
               for spec in owned)
    if check in {'serving', 'first_output'}:
        assert len([spec for spec in specs if spec.scenario == 'initial']) == 1
        assert len([spec for spec in specs if spec.scenario == 'warm']) == 2
    if check == 'generation':
        initial = [spec for spec in specs if spec.scenario == 'initial']
        assert len(initial) == 1 and initial[0].max_tokens == 4
        assert initial[0].request_timeout_seconds == .02


async def test_shared_initial_and_warm_use_serving_precedence_without_extra_requests(monkeypatch):
    from tests.test_runner import FakeClient

    specs = []

    class Capture(FakeClient):
        async def execute(self, spec, **kwargs):
            specs.append(spec)
            return await super().execute(spec, **kwargs)

    monkeypatch.setattr(runner, 'LLMClient', Capture)
    config = RunConfig(targets=[TARGET], checks=['serving', 'first_output'],
                       test_options={'serving': {'samples': 3, 'request_timeout_seconds': .5,
                                                'max_output_tokens': 96},
                                     'first_output': {'request_timeout_seconds': 1,
                                                      'max_output_tokens': 256}})
    await runner.run_suite(config)
    assert [spec.scenario for spec in specs] == ['initial', 'warm', 'warm', 'warm']
    assert all(spec.max_tokens == 96 and spec.request_timeout_seconds == .5 for spec in specs)


async def test_custom_request_explicit_values_and_missing_defaults_are_resolved_before_dispatch(tmp_path, monkeypatch):
    from tests.test_runner import FakeClient

    specs = []

    class Capture(FakeClient):
        async def execute(self, spec, **kwargs):
            specs.append(spec.model_copy(deep=True))
            return await super().execute(spec, **kwargs)

    monkeypatch.setattr(runner, 'LLMClient', Capture)
    path = tmp_path / 'custom.json'
    path.write_text(json.dumps([
        {'fixture_id': 'own', 'scorer': 'exact', 'expected': 'hi', 'max_tokens': 256,
         'request_timeout_seconds': 1, 'messages': [{'role': 'user', 'content': 'hi'}]},
        {'fixture_id': 'inherited', 'scorer': 'exact', 'expected': 'hi',
         'messages': [{'role': 'user', 'content': 'hi'}]},
    ]))
    config = RunConfig(targets=[TARGET], checks=['correctness'], max_output_tokens=4,
                       request_timeout_seconds=.02, custom_fixtures=str(path),
                       test_options={'correctness': {'concurrency': 1, 'max_output_tokens': 96,
                                                     'request_timeout_seconds': .5}})
    await runner.run_suite(config)
    own = [spec for spec in specs if spec.fixture_id == 'own']
    inherited = [spec for spec in specs if spec.fixture_id == 'inherited']
    assert own and all(spec.max_tokens == 256 and spec.request_timeout_seconds == 1 for spec in own)
    assert inherited and all(spec.max_tokens == 96 and spec.request_timeout_seconds == .5 for spec in inherited)


def test_disabled_access_timeout_does_not_change_generation_initial_request_settings():
    config = RunConfig(targets=[TARGET], checks=['generation'], request_timeout_seconds=2,
                       test_options={'access': {'request_timeout_seconds': 90}})
    assert config.effective_response()['request_timeout_seconds'] == 2
    assert config.test_options['access'].request_timeout_seconds == 90
