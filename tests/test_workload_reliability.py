"""Evidence gates and real client/runner behavior over an in-process HTTP transport."""

import asyncio
import json
import re
import time

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import Buckets, Consistency, Limits, Prefix, Sessions
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
        count = len(answer.split())
        usage = {'prompt_tokens': 32 if self.mode == 'flat_input' else max(1, len(prompt) // 4),
                 'completion_tokens': count}
        if 'then list the numbers' in prompt:
            usage['completion_tokens'] = 1 if self.mode == 'early_eos' else payload['max_tokens']
        if self.cached is not None:
            usage['prompt_tokens_details'] = {'cached_tokens': self.cached}
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
            async def __aiter__(self):
                endpoint.active += 1
                try:
                    for piece in re.findall(r'\S+\s*', answer):
                        await asyncio.sleep(.001)
                        data = {'model': payload['model'], 'choices': [{'delta': {'content': piece}}]}
                        yield ('data: ' + json.dumps(data) + '\n\n').encode()
                    data = {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}
                    yield ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()
                finally:
                    endpoint.active -= 1
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())


@pytest.mark.parametrize('mode', ['logprob_drift', 'token_drift'])
async def test_w1_sequential_probability_baseline_and_strict_token_divergence(monkeypatch, mode):
    endpoint = MemoryEndpoint(monkeypatch, mode=mode)
    report = await run_suite(config(endpoint, checks=['consistency'], consistency={
        'shapes': ['short'], 'levels': [1, 2], 'repetitions': 3,
        'logprobs': {'strict': True, 'tolerance': .01, 'top_logprobs': 0, 'compare_levels': False}}))
    result = check(report, 'consistency')
    assert result.status == 'fail'
    cross = result.metrics['logprobs']['cross_conditions'][0]
    assert cross['levels'] == [1, 2] and cross['status'] == 'fail'
    assert (cross['max_absolute_delta'] == .25 if mode == 'logprob_drift'
            else cross['token_divergences'] > 0)


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
    assert any('item 5 is ready.' in p['messages'][0]['content'] for p in endpoint.requests)


@pytest.mark.parametrize('cached,status', [(None, 'inconclusive'), (0, 'fail'), (16, 'fail'), (1024, 'pass')])
async def test_w3_prefix_requires_substantial_cached_tokens_and_separate_blocks(monkeypatch, cached, status):
    endpoint = MemoryEndpoint(monkeypatch, cached=cached)
    settings = config(endpoint, checks=['prefix'], prefix={'repeats': 3, 'history_turns': 2})
    report = await run_suite(settings)
    assert check(report, 'prefix').status == status
    assert len(endpoint.requests) == request_plan(settings)['planned_per_target']
    prompts = [p['messages'][0]['content'] for p in endpoint.requests[:6]]
    assert all(p.startswith(' The rock') for p in prompts[:3])
    assert all(p.startswith('Control') for p in prompts[3:])
    assert len(prompts[0]) > 8192


async def test_w4_cell_window_stops_dispatch_without_cancelling_healthy_requests(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, delay=.025)
    settings = config(endpoint, checks=['buckets'], stream=False, buckets={
        'levels': [2], 'samples_per_bucket': 4, 'window_seconds': .01})
    report = await run_suite(settings)
    result = check(report, 'buckets')
    assert len(endpoint.requests) == 18
    assert all(r.status == 'completed' for r in report.requests)
    assert result.metrics['error_rate'] == 0 and result.status == 'inconclusive'


async def test_w4_genuine_server_timeout_still_fails(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, delay=.05)
    report = await run_suite(config(endpoint, checks=['buckets'], stream=False,
        request_timeout_seconds=.01, buckets={'levels': [1], 'window_seconds': .02}))
    assert check(report, 'buckets').status == 'fail'
    assert any(r.status == 'timeout' for r in report.requests)


def test_w5_defaults_and_sample_count_warnings():
    minimum = Limits().min_samples
    assert min(Buckets().samples_per_bucket, Buckets().mixed_pairs, Consistency().repetitions,
               Prefix().repeats - 1, Sessions().sessions) >= minimum
    settings = config(MemoryEndpoint, checks=['buckets', 'mixed', 'consistency', 'prefix', 'sessions'],
        buckets={'samples_per_bucket': 2, 'mixed_pairs': 1}, consistency={'repetitions': 2},
        prefix={'repeats': 2}, sessions={'sessions': 1}, limits={'min_samples': 5})
    warnings = request_plan(settings)['warnings']
    for field in ('samples_per_bucket', 'mixed_pairs', 'repetitions', 'repeats', 'sessions.sessions'):
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
        await asyncio.sleep(.06 if index == 0 else .005)
        ends.append(time.monotonic())
        return SimpleNamespace(workload={})
    run = SimpleNamespace(allowed=lambda _: True, request=request)
    rows = await refill(run, SimpleNamespace(name='local'), None, [spec('p', 's', 'buckets')], 6, 2)
    assert starts[2] < ends[-1] - .03
    assert len(rows) == 6 and all(r.workload['load_qualified'] for r in rows)
    assert 1 < rows[0].workload['time_weighted_inflight'] < 2

    starts.clear()
    rows = await refill(run, SimpleNamespace(name='local'), None, [spec('p', 's', 'buckets')], 1, 2)
    assert rows[0].workload['load_qualified'] is False


async def test_w7_history_mixup_fails_each_sessions_seeded_value(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, mode='session_mix')
    report = await run_suite(config(endpoint, checks=['sessions'], sessions={
        'modes': ['live'], 'sessions': 2, 'concurrency': 2}))
    assert check(report, 'sessions').status == 'fail'
    turns = [p['messages'][0]['content'] for p in endpoint.requests if len(p['messages']) == 1]
    assert len(set(turns)) == 2
    assert all(r.score is False for r in report.requests if 'turn1' in r.scenario)


@pytest.mark.parametrize('mode,status', [('healthy', 'pass'), ('flat_input', 'inconclusive')])
async def test_w8_input_lengths_must_increase_and_match_requested_shape(monkeypatch, mode, status):
    endpoint = MemoryEndpoint(monkeypatch, mode=mode)
    settings = config(endpoint, checks=['buckets'], stream=False,
                      buckets={'levels': [1], 'samples_per_bucket': 2})
    report = await run_suite(settings)
    assert check(report, 'buckets').status == status
    assert len(report.requests) == request_plan(settings)['planned_per_target'] == 18
    cells = check(report, 'buckets').metrics['buckets']
    if mode == 'flat_input':
        assert all(any('input length not exercised' in g for g in c['coverage_gaps']) for c in cells.values())
    assert Buckets().input_chars['long'] >= 8192


async def test_w9_pending_queue_does_not_cap_inflight(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, delay=.03)
    report = await run_suite(config(endpoint, checks=['arrivals'], stream=False, concurrency=4,
        arrivals={'mode': 'burst', 'requests': 8, 'burst_size': 8, 'max_pending': 1}))
    assert endpoint.peak == 4
    assert len(report.requests) == 5  # Four dispatched and one queued; the remaining offers drop.
    assert check(report, 'arrivals').metrics['schedule']['unissued']


@pytest.mark.parametrize('ratio,mode,status', [(None, 'healthy', 'inconclusive'),
    (100, 'early_eos', 'inconclusive'), (100, 'healthy', 'pass')])
async def test_l3_mixed_needs_limit_long_light_and_exercised_heavy(monkeypatch, ratio, mode, status):
    endpoint = MemoryEndpoint(monkeypatch, mode=mode)
    report = await run_suite(config(endpoint, checks=['mixed'],
        buckets={'mixed_pairs': 2}, limits={'min_samples': 2, 'fairness_max_ratio': ratio}))
    result = check(report, 'mixed')
    assert result.status == status
    if ratio is None:
        assert result.summary == 'effect observed; no acceptance ratio configured'
    lights = [r for r in report.requests if r.scenario.startswith('mixed_short')]
    assert all(r.completion_tokens >= 64 for r in lights)
    assert len(report.requests) == 18
    if mode == 'early_eos':
        assert any(c['shape_qualified_samples'] == 0 for c in result.metrics['comparisons'])


async def test_l3_buffered_answer_with_delayed_trailers_is_not_active_decoding(monkeypatch):
    class Buffered(MemoryEndpoint):
        async def handle(self, request):
            response = await super().handle(request)
            if 'Copy the following' not in request.content.decode():
                return response
            source = response.stream

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    chunks = [chunk async for chunk in source]
                    yield b''.join(chunks[:-1])
                    await asyncio.sleep(.025)
                    yield chunks[-1]
            return httpx.Response(200, headers=response.headers, stream=Stream())
    endpoint = Buffered(monkeypatch)
    report = await run_suite(config(endpoint, checks=['mixed'], buckets={'mixed_pairs': 2},
        limits={'min_samples': 2, 'fairness_max_ratio': 100}))
    result = check(report, 'mixed')
    assert result.status == 'inconclusive'
    assert all(c['shape_qualified_samples'] == 0 for c in result.metrics['comparisons']
               if c['scenario'].endswith('active_stream'))


async def test_l3_weighted_pairs_cannot_borrow_another_blocks_shape_evidence(monkeypatch):
    class Weighted(MemoryEndpoint):
        heavy_outputs = 0

        async def handle(self, request):
            heavy = 'then list the numbers' in request.content.decode()
            if heavy:
                self.heavy_outputs += 1
            response = await super().handle(request)
            if not heavy or self.heavy_outputs <= 4:
                return response
            source = response.stream

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    async for chunk in source:
                        if b'"usage"' in chunk:
                            data, done = chunk.decode().split('\n\ndata: [DONE]')
                            payload = json.loads(data.removeprefix('data: '))
                            payload['usage']['completion_tokens'] = 1
                            chunk = ('data: ' + json.dumps(payload) + '\n\ndata: [DONE]' + done).encode()
                        yield chunk
            return httpx.Response(200, headers=response.headers, stream=Stream())
    endpoint = Weighted(monkeypatch)
    report = await run_suite(config(endpoint, checks=['mixed'], buckets={
        'mixed_pairs': 2, 'heavy_weights': {'long_input': 1, 'long_output': 2}},
        limits={'min_samples': 2, 'fairness_max_ratio': 100}))
    result = check(report, 'mixed')
    assert result.status == 'inconclusive'
    assert len(report.requests) == 26
    assert all(c['samples_and_overlap_qualified'] is False for c in result.metrics['comparisons']
               if 'long_output' in c['scenario'])


async def test_l3_small_output_budget_sends_no_unexercisable_light_passage(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch)
    settings = config(endpoint, checks=['mixed'], max_output_tokens=32,
                      buckets={'output_tokens': {'short': 8, 'medium': 16, 'long': 32}})
    report = await run_suite(settings)
    assert check(report, 'mixed').status == 'inconclusive'
    assert request_plan(settings)['planned_per_target'] == len(report.requests) == 0


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


@pytest.mark.parametrize('kind', ['buckets', 'logprobs'])
async def test_w6_bucket_and_logprob_dispatch_refills_a_slow_round(monkeypatch, kind):
    width = 6 if kind == 'buckets' else 3

    class Jitter(MemoryEndpoint):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.begins, self.ends = [], {}

        async def handle(self, request):
            index = len(self.begins)
            self.begins.append(time.monotonic())
            if index % width == 0:
                await asyncio.sleep(.04)
            response = await super().handle(request)
            self.ends[index] = time.monotonic()
            return response
    endpoint = Jitter(monkeypatch)
    if kind == 'buckets':
        settings = config(endpoint, checks=['buckets'], stream=False,
            buckets={'levels': [2], 'samples_per_bucket': 6})
        slow, refill_index = 0, 2
    else:
        settings = config(endpoint, checks=['consistency'], concurrency=3, consistency={
            'shapes': ['short'], 'levels': [1, 2, 3], 'repetitions': 2,
            'logprobs': {'top_logprobs': 0}})
        slow, refill_index = 6, 8
    report = await run_suite(settings)
    assert endpoint.begins[refill_index] < endpoint.ends[slow]
    assert len(report.requests) == request_plan(settings)['planned_per_target']
    if kind == 'buckets':
        cells = check(report, 'buckets').metrics['buckets']
        assert all(c['qualified_samples'] == 6 and 1 < c['time_weighted_inflight'] < 2
                   for c in cells.values())
    else:
        conditions = check(report, 'consistency').metrics['logprobs']['conditions']
        loaded = next(c for c in conditions if c['level'] == 2)
        assert loaded['coverage_complete']
        assert all(r['qualified_samples'] == 3 and r['time_weighted_inflight'] > 1
                   for r in loaded['rounds'])
