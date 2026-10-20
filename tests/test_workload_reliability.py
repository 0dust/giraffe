"""Evidence gates and real client/runner behavior over an in-process HTTP transport."""

import asyncio
import json
import re
import time

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import Consistency, Limits, Prefix, Sessions
from giraffe.planning import request_plan
from giraffe.runner import run_suite
from tests.test_workloads import check, config


class MemoryEndpoint:
    url = 'http://memory.test/v1'

    def __init__(self, monkeypatch, *, mode='healthy', cached=None, delay=.004):
        self.mode, self.cached, self.delay = mode, cached, delay
        self.active, self.peak = 0, 0
        self.requests, self.started, self.finished = [], [], []

        async def enter(client):
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
            return client
        monkeypatch.setattr(LLMClient, '__aenter__', enter)

    async def handle(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.started.append(time.monotonic())
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        prompt = '\n'.join(m['content'] for m in payload['messages'])
        answer = 'K7P4'
        for marker in ('Instead, copy this text exactly:\n',
                       'Copy the following text exactly, with no extra text:\n'):
            if marker in prompt:
                answer = prompt.split(marker)[-1]
        if 'Use this exact bakery name for this session:' in prompt:
            answer = re.search(r'Use this exact bakery name for this session: (\w+)', prompt)[1]
            if self.mode == 'session_mix' and len(payload['messages']) > 1:
                answer = 'Session_9999_live'
        loaded = self.active > 1
        if self.mode == 'text_drift' and loaded:
            answer += ' '
        if self.mode == 'wrong_copy' and 'Copy the following' in prompt:
            answer = 'A different answer appears here. ' * 20
        if self.mode == 'reasoning' and 'Use this exact bakery' in prompt:
            answer = '<think>Consider the session carefully.</think>' + answer
        count = len(answer.split())
        usage = {'prompt_tokens': 32 if self.mode == 'flat_input' else max(1, len(prompt) // 4),
                 'completion_tokens': count}
        if 'then list the numbers' in prompt:
            usage['completion_tokens'] = 1 if self.mode == 'early_eos' else payload['max_tokens']
        if self.mode == 'template_overhead':
            usage['prompt_tokens'] += 96
        if self.mode == 'compressed_input':
            usage['prompt_tokens'] = 96 + len(prompt) // 1000
        if self.cached is not None:
            cached = self.cached(payload, usage) if callable(self.cached) else self.cached
            usage['prompt_tokens_details'] = {'cached_tokens': cached}
        logprobs = None
        if payload.get('logprobs'):
            if self.mode == 'token_drift' and loaded:
                answer += '!'
            token = {'token': answer, 'bytes': list(answer.encode()),
                     'logprob': -.30 if loaded and self.mode == 'logprob_drift' else -.05,
                     'top_logprobs': []}
            logprobs = {'content': [token]}
        self.active -= 1
        self.finished.append(time.monotonic())
        if not payload['stream']:
            return httpx.Response(200, json={'model': payload['model'], 'choices': [{
                'message': {'content': answer}, 'finish_reason': 'stop', 'logprobs': logprobs}],
                'usage': usage})
        endpoint = self

        class Stream(httpx.AsyncByteStream):
            def __init__(self):
                self.counted = False

            async def aclose(self):
                if self.counted:
                    self.counted = False
                    endpoint.active -= 1

            async def __aiter__(self):
                endpoint.active += 1
                self.counted = True
                try:
                    for piece in re.findall(r'\S+\s*', answer):
                        await asyncio.sleep(.001)
                        data = {'model': payload['model'], 'choices': [{'delta': {'content': piece}}]}
                        yield ('data: ' + json.dumps(data) + '\n\n').encode()
                    # Leave a measurable output-to-finish window even for a one-word answer.
                    await asyncio.sleep(.01)
                    data = {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}
                    yield ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()
                finally:
                    await self.aclose()
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())


@pytest.mark.parametrize('mode', ['logprob_drift', 'token_drift'])
async def test_w1_sequential_probability_baseline_and_strict_token_divergence(monkeypatch, mode):
    endpoint = MemoryEndpoint(monkeypatch, mode=mode)
    report = await run_suite(config(endpoint, checks=['consistency'], consistency={
        'shapes': ['short'], 'levels': [1, 2], 'repetitions': 3,
        'logprobs': {'strict': True, 'tolerance': .01, 'top_logprobs': 0, 'compare_levels': False}}))
    result = check(report, 'consistency')
    assert result.status == 'inconclusive'
    cross = result.metrics['logprobs']['cross_conditions'][0]
    assert cross['levels'] == [1, 2] and cross['status'] == 'inconclusive'
    assert not result.metrics['logprobs']['conditions'][1]['qualified_samples']
    assert 'server-side concurrency needs streamed output timing' in result.summary.lower()


async def test_w2_cross_level_modal_answers_and_distinct_known_passages(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, mode='text_drift')
    settings = config(endpoint, checks=['consistency'], consistency={
        'shapes': ['short'], 'repetitions': 4, 'strict': True})
    report = await run_suite(settings)
    assert check(report, 'consistency').status == 'fail'
    assert any(c['agreement_rate'] < 1 for c in check(report, 'consistency').metrics['cross_level'])
    assert len(endpoint.requests) == request_plan(settings)['planned_per_target'] == 24
    assert len({json.dumps(p['messages']) for p in endpoint.requests}) == 3
    assert all(p['temperature'] == 0 for p in endpoint.requests)
    assert all('Instead' not in p['messages'][0]['content'] for p in endpoint.requests)


@pytest.mark.parametrize('cached,status', [(None, 'inconclusive'), (0, 'fail'), (16, 'fail'), (1024, 'inconclusive')])
async def test_w3_prefix_requires_substantial_cached_tokens_and_separate_blocks(monkeypatch, cached, status):
    endpoint = MemoryEndpoint(monkeypatch, cached=cached)
    settings = config(endpoint, checks=['prefix'], context_limit=16384, prefix={'repeats': 3, 'history_turns': 2})
    report = await run_suite(settings)
    assert check(report, 'prefix').status == status
    assert len(endpoint.requests) == request_plan(settings)['planned_per_target']
    prompts = [p['messages'][0]['content'] for p in endpoint.requests[:6]]
    assert all(p.split('\n', 1)[1].startswith(' The rock') for p in prompts[:3])
    assert all(p.split('\n', 1)[1].startswith('Control') for p in prompts[3:])
    assert len(prompts[0]) > 8192






def test_w5_defaults_and_sample_count_warnings():
    minimum = Limits().min_samples
    assert min(Consistency().repetitions,
               Prefix().repeats - 1, Sessions().sessions) >= minimum
    settings = config(MemoryEndpoint, checks=['consistency', 'prefix', 'sessions'],
        consistency={'repetitions': 2},
        prefix={'repeats': 2}, sessions={'sessions': 1}, limits={'min_samples': 5})
    warnings = request_plan(settings)['warnings']
    for field in ('repetitions', 'repeats', 'sessions.sessions'):
        assert any(field in w and 'cannot qualify' in w for w in warnings)


async def test_w6_pool_refills_immediately_and_flags_tail_samples():
    from giraffe.logprob_workload import refill
    from types import SimpleNamespace
    from giraffe.workloads import spec
    starts, ends = [], []

    async def request(target, client, request, on_start):
        index = len(starts)
        starts.append(time.monotonic())
        on_start()
        await asyncio.sleep(.2 if index == 0 else .005)
        ends.append(time.monotonic())
        return SimpleNamespace(target=target.name, timing_start_ms=starts[index] * 1000,
                               first_output_ms=.1, last_output_ms=(ends[-1] - starts[index]) * 1000,
                               elapsed_ms=(ends[-1] - starts[index]) * 1000,
                               stream_end_ms=(ends[-1] - starts[index]) * 1000,
                               workload={})
    run = SimpleNamespace(allowed=lambda _: True, request=request)
    rows = await refill(run, SimpleNamespace(name='local'), None, [spec('p', 's', 'buckets')], 6, 2, condition_plan=False)
    assert starts[2] < ends[-1] - .03
    assert len(rows) == 6
    assert sum(r.workload['load_qualified'] for r in rows) == 5
    assert 1 < rows[-1].workload['client_time_weighted_inflight'] < 1.5

    starts.clear()
    rows = await refill(run, SimpleNamespace(name='local'), None, [spec('p', 's', 'buckets')], 1, 2, condition_plan=False)
    assert rows[0].workload['load_qualified'] is False


async def test_w7_history_mixup_fails_each_sessions_seeded_value(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, mode='session_mix')
    report = await run_suite(config(endpoint, checks=['sessions'], sessions={
        'modes': ['live'], 'sessions': 2, 'concurrency': 2}))
    assert check(report, 'sessions').status == 'fail'
    turns = [p['messages'][0]['content'] for p in endpoint.requests if len(p['messages']) == 1]
    assert len(set(turns)) == 2
    assert all(r.score is False for r in report.requests if 'turn1' in r.scenario)




async def test_w9_pending_queue_does_not_cap_inflight(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, delay=.03)
    report = await run_suite(config(endpoint, checks=['arrivals'], stream=False, concurrency=4,
        arrivals={'mode': 'burst', 'requests': 8, 'burst_size': 8, 'max_pending': 1}))
    assert endpoint.peak == 4
    assert len(report.requests) == 5  # Four dispatched and one queued; the remaining offers drop.
    assert check(report, 'arrivals').metrics['schedule']['unissued']










async def test_w5_session_turns_do_not_substitute_for_independent_samples(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch)
    settings = config(endpoint, checks=['sessions'], sessions={
        'sessions': 1, 'concurrency': 1, 'turns': ['Choose a name.', 'Repeat it.', 'Repeat it again.']},
        limits={'min_samples': 5})
    report = await run_suite(settings)
    result = check(report, 'sessions')
    assert len(report.requests) == 6
    assert result.status == 'inconclusive'
    assert result.metrics['completed_sessions_per_mode'] == {'fixed': 1, 'live': 1}




@pytest.mark.parametrize('workload', ['consistency', 'logprobs', 'prefix', 'sessions'])
async def test_round2_workload_temperature_preserves_user_option(monkeypatch, workload):
    endpoint = MemoryEndpoint(monkeypatch)
    selected = 'consistency' if workload == 'logprobs' else workload
    extra = {'consistency': {'shapes': ['short'], 'repetitions': 2}}
    if workload == 'logprobs':
        extra['consistency']['logprobs'] = {'top_logprobs': 0}
    settings = config(endpoint, checks=[selected], context_limit=16384 if selected == 'prefix' else 2048, request_options={'temperature': .7},
        sessions={'sessions': 1}, prefix={'repeats': 3}, **extra)
    await run_suite(settings)
    assert endpoint.requests and all(p['temperature'] == .7 for p in endpoint.requests)


async def test_round2_consistency_copy_prompts_and_wrong_answers_are_independent(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, mode='wrong_copy')
    settings = config(endpoint, checks=['consistency'], stream=False,
        consistency={'shapes': ['short'], 'repetitions': 4, 'strict': True})
    result = check(await run_suite(settings), 'consistency')
    assert result.status == 'inconclusive'
    assert result.metrics['correctness_observed'] == 0
    for payload in endpoint.requests:
        prompt = payload['messages'][0]['content']
        assert 'Copy the following' in prompt and 'box label' not in prompt and 'Instead' not in prompt
        sentences = prompt.split('\n')[-1].split('. ')
        assert len(sentences) >= 3 and len(set(sentences)) == len(sentences)




@pytest.mark.parametrize('misses,status', [(0, 'pass'), (1, 'pass'), (2, 'fail')])
async def test_round2_prefix_majority_and_discriminative_controls(monkeypatch, misses, status):
    seen = 0
    def cached(payload, usage):
        nonlocal seen
        prompt = payload['messages'][0]['content']
        if not prompt.split('\n', 1)[-1].startswith(' The rock'):
            return 16
        seen += 1
        return 0 if 1 < seen <= 1 + misses else usage['prompt_tokens']
    endpoint = MemoryEndpoint(monkeypatch, cached=cached)
    result = check(await run_suite(config(endpoint, checks=['prefix'], context_limit=16384, prefix={'repeats': 4})), 'prefix')
    assert result.status == status
    assert result.metrics['cache_repeat_misses'] == {'misses': misses, 'observed': 3, 'planned': 3}
    assert f'{misses} of 3 repeat misses' in result.summary


async def test_round2_prefix_all_cached_is_not_discriminative(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, cached=lambda p, u: u['prompt_tokens'])
    result = check(await run_suite(config(endpoint, checks=['prefix'], context_limit=16384)), 'prefix')
    assert result.status == 'inconclusive'
    assert 'cache reporting not discriminative' in result.summary




async def test_round2_live_session_history_strips_reasoning_and_fixed_keeps_seed(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, mode='reasoning')
    report = await run_suite(config(endpoint, checks=['sessions'], sessions={
        'sessions': 2, 'turns': ['Choose a name.', 'Recall it.', 'Recall it again.']}))
    assert check(report, 'sessions').status == 'pass'
    assert check(report, 'sessions').metrics['evidenced_session_overlap'] >= 2
    for payload in endpoint.requests:
        value = re.search(r'Use this exact bakery name for this session: (\w+)', payload['messages'][0]['content'])[1]
        assistants = [m['content'] for m in payload['messages'] if m['role'] == 'assistant']
        assert all(answer == value for answer in assistants)


async def test_round2_session_pool_refills_without_waiting_for_slowest(monkeypatch):
    class Jitter(MemoryEndpoint):
        async def handle(self, request):
            payload = json.loads(request.content)
            if 'Session_0000' in payload['messages'][0]['content']:
                await asyncio.sleep(.06)
            return await super().handle(request)
    endpoint = Jitter(monkeypatch)
    await run_suite(config(endpoint, checks=['sessions'], sessions={
        'modes': ['fixed'], 'sessions': 3, 'concurrency': 2}))
    values = [re.search(r'Use this exact bakery name for this session: (\w+)', p['messages'][0]['content'])[1]
              for p in endpoint.requests]
    assert values.index('Session_0002_fixed') < values.index('Session_0000_fixed')


@pytest.mark.parametrize('mode', ['logprob_drift', 'token_drift'])
async def test_round2_nonstrict_logprob_summary_shows_cross_level_drift(monkeypatch, mode):
    endpoint = MemoryEndpoint(monkeypatch, mode=mode)
    result = check(await run_suite(config(endpoint, checks=['consistency'], stream=False, consistency={
        'shapes': ['short'], 'repetitions': 3, 'logprobs': {'top_logprobs': 0}})), 'consistency')
    assert 'maximum cross-level delta' in result.summary.lower()
    assert 'server-side concurrency needs streamed output timing' in result.summary.lower()
    assert result.metrics['logprobs']['max_cross_level_delta'] is None
    assert 'token divergences:' in result.summary


async def test_round2_nonstrict_variation_preserves_coverage_reason(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, mode='text_drift')
    result = check(await run_suite(config(endpoint, checks=['consistency'], consistency={
        'shapes': ['short'], 'repetitions': 2}, limits={'min_samples': 3})), 'consistency')
    assert result.status == 'inconclusive'
    assert 'coverage' in result.summary.lower() or 'Too few' in result.summary




@pytest.mark.parametrize('workload', ['arrivals', 'prefix', 'sessions', 'consistency', 'logprobs'])
async def test_round2_workload_plan_matches_healthy_execution(monkeypatch, workload):
    endpoint = MemoryEndpoint(monkeypatch)
    selected = 'consistency' if workload == 'logprobs' else workload
    settings = config(endpoint, checks=[selected], context_limit=16384 if selected == 'prefix' else 2048, stream=False, sessions={'modes': ['fixed'], 'sessions': 3},
        arrivals={'requests': 3, 'requests_per_second': 100},
        prefix={'repeats': 3}, consistency={'shapes': ['short'], 'repetitions': 2,
            **({'logprobs': {'top_logprobs': 0}} if workload == 'logprobs' else {})})
    report = await run_suite(settings)
    planned = request_plan(settings)
    assert planned['count_kind'] == 'exact'
    assert planned['planned_per_target'] == len(endpoint.requests) == len(report.requests)


async def test_round2_overlap_is_time_weighted_over_each_request_lifetime(monkeypatch):
    from types import SimpleNamespace
    import giraffe.logprob_workload as workload
    from giraffe.workloads import spec
    now = 0
    started = []
    gates = [asyncio.Event() for _ in range(3)]

    async def request(target, client, request, on_start):
        index = len(started)
        started.append(index)
        on_start()
        await gates[index].wait()
        return SimpleNamespace(id=str(index), target=target.name, timing_start_ms=0,
                               first_output_ms=.1, last_output_ms=now, elapsed_ms=now, stream_end_ms=now, workload={})

    monkeypatch.setattr(workload, 'time', SimpleNamespace(monotonic=lambda: now))
    run = SimpleNamespace(allowed=lambda _: True, request=request)
    task = asyncio.create_task(workload.refill(run, SimpleNamespace(name='local'), None,
        [spec('p', 's', 'buckets')], 3, 3, condition_plan=False))
    async with asyncio.timeout(2):
        while len(started) < 3:
            if task.done():
                await task  # Surface worker errors instead of spinning forever.
                pytest.fail("Refill returned before starting the expected requests")
            await asyncio.sleep(0)
    for index, finish in enumerate((2, 4, 8)):
        now = finish
        gates[index].set()
        await asyncio.sleep(0)
    rows = await task
    assert [r.workload['observed_overlap'] for r in rows] == [3, 3, 3]
    assert [r.workload['client_time_weighted_inflight'] for r in rows] == [3, 2.5, 1.75]
    assert [r.workload['load_qualified'] for r in rows] == [True, True, False]


@pytest.mark.parametrize('location', ['repeat', 'control', 'input_usage'])
async def test_round2_prefix_missing_evidence_is_inconclusive(monkeypatch, location):
    class Missing(MemoryEndpoint):
        async def handle(self, request):
            response = await super().handle(request)
            payload = json.loads(request.content)
            prompt = payload['messages'][0]['content']
            if ((location != 'control' and prompt.split('\n', 1)[-1].startswith(' The rock') and 'Trial 0002' in prompt)
                    or (location == 'control' and prompt.split('\n', 1)[-1].startswith('Control 0002'))):
                data = json.loads(response.content)
                if location == 'input_usage':
                    data['usage'].pop('prompt_tokens')
                else:
                    data['usage'].pop('prompt_tokens_details')
                return httpx.Response(200, json=data)
            return response
    endpoint = Missing(monkeypatch, cached=lambda p, u: 0 if p['messages'][0]['content'].split('\n', 1)[-1].startswith('Control') else u['prompt_tokens'])
    result = check(await run_suite(config(endpoint, checks=['prefix'], context_limit=16384, stream=False)), 'prefix')
    assert result.status == 'inconclusive'
    assert 'missing' in result.summary.lower()


@pytest.mark.parametrize('run_options,spec_options,expected', [
    ({}, {}, {'temperature': 0}),
    ({'temperature': .7}, {}, {}),
    ({'temperature': .7}, {'temperature': .2}, {'temperature': .2}),
])
def test_round2_deterministic_options_respects_both_option_sources(run_options, spec_options, expected):
    from types import SimpleNamespace
    from giraffe.workloads import deterministic_options
    assert deterministic_options(SimpleNamespace(request_options=run_options), spec_options) == expected


async def test_round2_explicit_consistency_temperature_is_preserved(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch)
    await run_suite(config(endpoint, checks=['consistency'], stream=False,
        request_options={'temperature': .7}, consistency={
            'shapes': ['short'], 'temperature': .2, 'repetitions': 2}))
    assert endpoint.requests and all(p['temperature'] == .2 for p in endpoint.requests)


async def test_round2_fixed_session_plan_counts_admitted_turns_without_saved_answers(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch)
    settings = config(endpoint, checks=['sessions'], stream=False, context_limit=512, sessions={
        'modes': ['fixed'], 'sessions': 2, 'saved_answers': [],
        'turns': ['Choose a name.', 'Recall it.', 'Recall it again.', 'Recall it once more.']})
    report = await run_suite(settings)
    assert request_plan(settings)['planned_per_target'] == len(report.requests) == 8
    assert check(report, 'sessions').status == 'inconclusive'
    assert 'conversations were not observed concurrently' in check(report, 'sessions').summary.lower()
    limited = settings.model_copy(update={'context_limit': 256})
    report = await run_suite(limited)
    assert request_plan(limited)['planned_per_target'] == len(report.requests) == 4
