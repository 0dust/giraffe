"""Concurrency claims require output evidence, even when client requests overlap."""

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest

from giraffe.fixtures import builtin_fixtures
from giraffe.logprob_workload import logprob_fixtures, refill, summarize_logprobs
from giraffe.logprobs import collect_logprobs
from giraffe.models import Consistency, RequestRecord, RunConfig
from giraffe.overlap import evidenced_overlap, output_window, peak_evidenced_concurrency
from giraffe.runner import _Run, _checks, _missing_timing, _pack_results, _stats, _timing_violations, run_suite
from giraffe.workloads import results, spec
from tests.fake_endpoint import FakeEndpoint


def configuration(**extra):
    return RunConfig.model_validate({
        'targets': [{'name': 'local', 'url': 'http://localhost:1/v1', 'model': 'fake-model'}],
        'checks': ['correctness', 'json'], 'concurrency': 2, 'default_concurrency': 2, 'samples': 2,
        'max_requests': 300, 'max_duration_seconds': 60, 'request_timeout_seconds': 5,
        'context_limit': 8192, 'max_output_tokens': 128,
        'limits': {'min_samples': 2, 'fairness_max_ratio': 100}, **extra})


def record(name, start=0, first=10, last=20, **extra):
    return RequestRecord(**{
        'id': name, 'target': 'local', 'fixture_id': name, 'scenario': 'correctness_c2',
        'check_ids': ['correctness'], 'started_at': 'now', 'status': 'completed',
        'valid': True, 'score': True, 'finish_reason': 'stop', 'timing_start_ms': start,
        'first_output_ms': first, 'last_output_ms': last, 'dispatch_ms': start - 100,
        'elapsed_ms': last or 20, 'stream_end_ms': extra.get('elapsed_ms', last or 20),
        'answer_chunks': 2, 'stream': True, 'overlap': 8,
        'completion_tokens': 80, 'output_tokens': 80, 'input_tokens': 100,
        'requested_max_tokens': 128, **extra})


def workload_result(run, check):
    return next(c for c in results(run, run.config.targets[0], _stats, _timing_violations, _missing_timing) if c.id == check)


def test_output_windows_use_stream_completion_and_strict_intersections():
    a, b = record('a', 100), record('b', 110)
    assert output_window(a) == (110, 120)
    assert evidenced_overlap(a, [a, b]) == 1  # Touching boundaries are serial.
    point = record('point', 105, 10, 10)
    assert evidenced_overlap(a, [a, point]) == 1  # Zero intersection cannot qualify.
    point.stream_end_ms = 18
    assert output_window(point) == (115, 123)
    assert evidenced_overlap(a, [a, point]) == evidenced_overlap(point, [a, point]) == 2
    assert peak_evidenced_concurrency([a, point]) == 2
    assert output_window(record('missing', first=None)) is None
    assert output_window(record('complete', stream=False)) is None
    assert output_window(record('missing', last=None)) == (10, 20)


def test_single_chunk_streams_require_positive_intersection_outside_delays():
    a, b = record('a', first=10, last=10, elapsed_ms=20), record('b', first=15, last=15, elapsed_ms=25)
    assert evidenced_overlap(a, [a, b]) == peak_evidenced_concurrency([a, b]) == 2
    delays = [{'start_ms': 15, 'end_ms': 20, 'lag_ms': 150}]
    assert evidenced_overlap(a, [a, b], delays) == peak_evidenced_concurrency([a, b], delays) == 1
    # Some overlap outside a delay still proves concurrency.
    delays[0]['end_ms'] = 19
    assert evidenced_overlap(a, [a, b], delays) == peak_evidenced_concurrency([a, b], delays) == 2
    # Adjacent and overlapping delay windows jointly cover the intersection.
    delays += [{'start_ms': 18, 'end_ms': 20, 'lag_ms': 150}]
    assert evidenced_overlap(a, [a, b], delays) == peak_evidenced_concurrency([a, b], delays) == 1


def test_peak_excludes_simultaneity_hidden_only_inside_delay():
    rows = [record('a', first=1, elapsed_ms=10), record('b', first=5, elapsed_ms=15),
            record('c', first=8, elapsed_ms=20)]
    delays = [{'start_ms': 8, 'end_ms': 10, 'lag_ms': 150}]
    assert peak_evidenced_concurrency(rows) == 3
    assert peak_evidenced_concurrency(rows, delays) == 2


def test_peak_is_simultaneous_not_number_of_pairwise_neighbors():
    wide, left, right = record('wide', 0, 0, 100), record('left', 0, 10, 20), record('right', 0, 30, 40)
    assert evidenced_overlap(wide, [wide, left, right]) == 3
    assert peak_evidenced_concurrency([wide, left, right]) == 2
    assert peak_evidenced_concurrency([record('end', 0, 0, 10), record('point', 0, 10, 10), record('start', 0, 10, 20)]) == 1


def test_output_evidence_is_target_local_and_rejects_invalid_windows():
    a, b = record('a'), record('b', target='other')
    assert evidenced_overlap(a, [a, b]) == 1
    assert peak_evidenced_concurrency([a, b]) == 1
    assert output_window(record('reversed', first=20, last=10, elapsed_ms=10)) is None
    assert output_window(record('nan').model_copy(update={'timing_start_ms': float('nan')})) is None


@pytest.mark.parametrize('check', ['correctness', 'json'])
@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('delayed', [False, True])
def test_known_answers_require_server_output_overlap(check, concurrent, delayed):
    config = configuration(checks=[check])
    run = _Run(config, None, None)
    run.records = [record('a', scenario=f'{check}_c2', check_ids=[check]),
                   record('b', 5 if concurrent else 20, scenario=f'{check}_c2', check_ids=[check])]
    if delayed:
        run.timing_health.windows = [{'start_ms': 0, 'end_ms': 1000, 'lag_ms': 150}]
    result = next(c for c in _checks(run, config.targets[0]) if c.id == check)
    assert result.status == ('pass' if concurrent and not delayed else 'inconclusive')
    assert result.metrics['evidenced_overlap'] == (2 if concurrent and not delayed else 1)
    assert result.metrics['configured_concurrency'] == 2


@pytest.mark.parametrize('check', ['correctness', 'json'])
def test_wrong_serial_answer_still_fails(check):
    config = configuration(checks=[check])
    run = _Run(config, None, None)
    run.records = [record('wrong', score=False, check_ids=[check])]
    assert next(c for c in _checks(run, config.targets[0]) if c.id == check).status == 'fail'


@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('delayed', [False, True])
def test_loaded_pack_requires_server_evidence(concurrent, delayed):
    config = configuration(checks=['correctness'], request_source='request_pack', request_pack={'version': 1, 'cases': [
        {'fixture_id': 'case', 'messages': [{'role': 'user', 'content': 'Copy exactly: two words'}], 'scorer': 'exact', 'expected': 'two words'}]})
    run = _Run(config, None, None)
    run.records = [record('control', 0, fixture_id='pack:case', scenario='pack_control'),
                   record('loaded', 100, fixture_id='pack:case', scenario='pack_loaded'),
                   record('other', 105 if concurrent else 120, check_ids=['serving'])]
    if delayed:
        run.timing_health.overflow = {'start_ms': 0, 'end_ms': 1000, 'lag_ms': 150}
    loaded = _pack_results(run, config.targets[0])
    assert loaded['loaded_status'] == ('pass' if concurrent and not delayed else 'inconclusive')
    assert loaded['cases'][0]['loaded']['evidenced_overlap'] == (2 if concurrent and not delayed else 1)








@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('delayed', [False, True])
async def test_refill_keeps_client_load_separate_from_server_evidence(concurrent, delayed):
    issued = 0
    async def request(target, client, request, *, on_start):
        nonlocal issued
        index = issued
        issued += 1
        on_start()
        await asyncio.sleep(.001)
        return record(str(index), index * (5 if concurrent else 20))
    from giraffe.timing import TimingHealth
    health = TimingHealth(0)
    if delayed:
        health.windows = [{'start_ms': 0, 'end_ms': 1000, 'lag_ms': 150}]
    run = SimpleNamespace(allowed=lambda _: True, request=request, unfinished=defaultdict(set), timing_health=health)
    rows = await refill(run, SimpleNamespace(name='local'), None, [spec('Copy exactly: two words', 'bucket', 'buckets')], 4, 2, condition_plan=False)
    assert max(r.workload['client_peak_inflight'] for r in rows) == 2
    assert all(r.workload['cell_evidenced_concurrency'] == (0 if delayed else 2 if concurrent else 1) for r in rows)
    assert any(r.workload['load_qualified'] for r in rows) is (concurrent and not delayed)
    assert all('client_time_weighted_inflight' in r.workload and 'time_weighted_inflight' not in r.workload for r in rows)


@pytest.mark.parametrize('concurrency,concurrent', [(1, False), (2, False), (2, True)])
@pytest.mark.parametrize('delayed', [False, True])
def test_sessions_require_two_conversations_with_overlapping_outputs(concurrency, concurrent, delayed):
    config = configuration(checks=['sessions'], concurrency=concurrency,
                           sessions={'sessions': 2, 'concurrency': concurrency, 'modes': ['live']})
    run = _Run(config, None, None)
    run.observations['local']['sessions'] = {'sessions': [
        {'id': str(i), 'mode': 'live', 'turns': [0, 1], 'stop_reason': None, 'completed_turns': 2, 'planned_turns': 2} for i in range(2)]}
    run.records = [record(str(i), 5 * i if concurrent else 20 * i, scenario='session_live_turn0', check_ids=['sessions'],
                          workload={'session_id': str(i), 'mode': 'live'}) for i in range(2)]
    if delayed:
        run.timing_health.windows = [{'start_ms': 0, 'end_ms': 1000, 'lag_ms': 150}]
    result = workload_result(run, 'sessions')
    assert result.status == ('pass' if concurrent and not delayed else 'inconclusive')
    if not concurrent or delayed:
        assert 'conversations were not observed concurrently' in result.summary.lower()


def probability_rows(positions):
    rows = []
    for repeat in range(3):
        tokens = [{'token': f'{i} ', 'bytes': list(f'{i} '.encode()), 'logprob': -.1, 'top_logprobs': []} for i in range(positions)]
        evidence = collect_logprobs({'content': tokens}, ''.join(t['token'] for t in tokens), 0)
        rows.append(record(str(repeat), logprob_evidence=evidence, request_hash='same',
                           workload={'input_bucket': 'short', 'level': 1, 'round': repeat, 'phase': 'measured',
                                     'corpus_index': 0, 'load_qualified': True}))
    return rows


@pytest.mark.parametrize('positions,expected', [(1, 'inconclusive'), (31, 'inconclusive'), (32, 'pass')])
def test_logprob_consistency_requires_meaningful_compared_positions(positions, expected):
    settings = Consistency(levels=[1], shapes=['short'], repetitions=3,
                           logprobs={'strict': True, 'tolerance': 0, 'top_logprobs': 0})
    result = summarize_logprobs(probability_rows(positions), settings)
    assert result['status'] == expected
    assert result['conditions'][0]['coverage_complete'] is (positions >= 32)


def test_logprob_short_sample_drift_still_fails():
    rows = probability_rows(1)
    rows[0].logprob_evidence.tokens[0].logprob = -.9
    settings = Consistency(levels=[1], shapes=['short'], repetitions=3,
                           logprobs={'strict': True, 'tolerance': 0, 'top_logprobs': 0})
    assert summarize_logprobs(rows, settings)['status'] == 'fail'


def test_logprob_fixtures_copy_a_substantial_nonrepetitive_passage():
    config = configuration(checks=['consistency'])
    fixtures = logprob_fixtures(config, builtin_fixtures(config))
    assert all(len(f.expected.split()) >= 64 for f in fixtures.values())
    assert all('Copy the following text exactly' in f.messages[0]['content'] for f in fixtures.values())
    assert fixtures['long'].input_chars > fixtures['short'].input_chars




def test_performance_sessions_without_limits_do_not_claim_limits_met():
    config = configuration(checks=['sessions'], sessions={
        'profile': 'performance', 'modes': ['live'], 'sessions': 2, 'concurrency': 2,
        'workloads': [{'name': 'chat', 'turns': ['Say hello', 'Say goodbye']} ]})
    run = _Run(config, None, None)
    run.observations['local']['sessions'] = {'sessions': [
        {'id': str(i), 'mode': 'live', 'turns': [0, 1], 'stop_reason': None, 'completed_turns': 2, 'planned_turns': 2} for i in range(2)]}
    run.records = [record(str(i), 5 * i, scenario='session_live_turn0', check_ids=['sessions'], score=None,
                          workload={'session_id': str(i), 'mode': 'live'}) for i in range(2)]
    result = workload_result(run, 'sessions')
    assert result.status == 'pass'
    assert 'no timing limits are configured' in result.summary
    assert 'met configured limits' not in result.summary


def test_sessions_cannot_use_two_turns_of_the_same_conversation_as_evidence():
    config = configuration(checks=['sessions'], sessions={'modes': ['live'], 'sessions': 2})
    run = _Run(config, None, None)
    run.records = [record('a', check_ids=['sessions'], workload={'session_id': 'one'}),
                   record('b', check_ids=['sessions'], workload={'session_id': 'one'})]
    assert workload_result(run, 'sessions').metrics['evidenced_session_overlap'] == 1




@pytest.mark.parametrize('serial', [False, True])
async def test_http_known_answers_require_concurrent_output(serial):
    with FakeEndpoint(service_slots=1 if serial else None, first_token_delay=.02, chunk_delay=.02) as endpoint:
        report = await run_suite(configuration(samples=6, targets=[{'name': 'local', 'url': endpoint.url, 'model': 'fake-model'}]))
    for result in report.checks:
        if result.id in {'correctness', 'json'}:
            assert result.status == ('inconclusive' if serial else 'pass'), result


@pytest.mark.parametrize('concurrency,serial', [(1, False), (2, True), (2, False)])
async def test_http_sessions_require_concurrent_conversations(concurrency, serial):
    with FakeEndpoint(service_slots=1 if serial else None, first_token_delay=.02, chunk_delay=.02) as endpoint:
        report = await run_suite(configuration(checks=['sessions'], concurrency=concurrency,
            targets=[{'name': 'local', 'url': endpoint.url, 'model': 'fake-model'}],
            sessions={'sessions': 2, 'concurrency': concurrency, 'modes': ['live']}))
    result = next(c for c in report.checks if c.id == 'sessions')
    assert result.status == ('pass' if concurrency == 2 and not serial else 'inconclusive'), result


async def test_http_complete_responses_do_not_establish_concurrency():
    with FakeEndpoint() as endpoint:
        report = await run_suite(configuration(stream=False, targets=[{'name': 'local', 'url': endpoint.url, 'model': 'fake-model'}]))
    for result in report.checks:
        if result.id in {'correctness', 'json'}:
            assert result.status == 'inconclusive'
            assert 'server-side concurrency needs streamed output timing' in result.summary


class StreamEndpoint:
    """In-process output service, optionally serialized through the last chunk."""
    url = 'http://stream.test/v1'

    def __init__(self, monkeypatch, serial):
        import httpx
        from giraffe.client import LLMClient
        self.lock = asyncio.Lock() if serial else None
        async def enter(client):
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
            return client
        monkeypatch.setattr(LLMClient, '__aenter__', enter)

    async def handle(self, request):
        import httpx
        import json
        import re
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'fake-model'}]})
        payload = json.loads(request.content)
        prompt = '\n'.join(m['content'] for m in payload['messages'])
        answer = FakeEndpoint._answer(prompt)
        if 'Use this exact bakery name for this session:' in prompt:
            answer = re.search(r'Use this exact bakery name for this session: (\w+)', prompt)[1]
        pieces = re.findall(r'\S+\s*', answer)
        finish = 'length' if len(pieces) > payload['max_tokens'] else 'stop'
        pieces = pieces[:payload['max_tokens']]
        usage = {'prompt_tokens': len(prompt.split()) + 8, 'completion_tokens': len(pieces)}
        if self.lock:
            await self.lock.acquire()
        await asyncio.sleep(.003)
        endpoint = self
        class Stream(httpx.AsyncByteStream):
            released = False
            async def aclose(self):
                if not self.released:
                    self.released = True
                    if endpoint.lock:
                        endpoint.lock.release()
            async def __aiter__(self):
                try:
                    for piece in pieces:
                        yield ('data: ' + json.dumps({'model': 'fake-model', 'choices': [{'delta': {'content': piece}}]}) + '\n\n').encode()
                        await asyncio.sleep(.001)
                    await asyncio.sleep(.01)
                    yield ('data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}], 'usage': usage}) + '\n\ndata: [DONE]\n\n').encode()
                finally:
                    await self.aclose()
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())




@pytest.mark.parametrize('serial', [False, True])
async def test_single_chunk_packs_and_sessions_have_repeatable_evidence(monkeypatch, serial):
    endpoint = StreamEndpoint(monkeypatch, serial)
    settings = configuration(targets=[{'name': 'local', 'url': endpoint.url, 'model': 'fake-model'}],
        checks=['correctness', 'sessions'], request_source='request_pack',
        request_pack={'version': 1, 'cases': [
            {'fixture_id': f'question-{index}', 'messages': [{'role': 'user', 'content': f'Return 42 for question {index}'}],
             'scorer': 'exact', 'expected': '42'} for index in range(6)]},
        sessions={'sessions': 2, 'concurrency': 2, 'modes': ['live']})
    for _ in range(5):
        report = await run_suite(settings)
        expected = 'inconclusive' if serial else 'pass'
        for result in report.checks:
            if result.id in settings.checks:
                assert result.status == expected, (result.id, result.summary, result.metrics)
            if result.id == 'correctness':
                pack = result.metrics['request_pack']
                assert pack['control_status'] == 'pass'
                assert pack['loaded_status'] == expected
                assert all((case['loaded']['evidenced_overlap'] == 1 if serial else
                            case['loaded']['evidenced_overlap'] >= 2) for case in pack['cases'])
            if result.id == 'sessions':
                assert (result.metrics['evidenced_session_overlap'] == 1 if serial else
                        result.metrics['evidenced_session_overlap'] >= 2)
        assert all(r.answer_chunks == 1 for r in report.requests)


@pytest.mark.parametrize('levels,expected', [([1], 'pass'), ([1, 2], 'inconclusive')])
async def test_complete_logprob_pass_needs_depth_and_serial_scope(monkeypatch, levels, expected):
    import httpx
    import re
    from tests.test_workload_reliability import MemoryEndpoint
    class TokenEndpoint(MemoryEndpoint):
        async def handle(self, request):
            response = await super().handle(request)
            data = response.json()
            choice = data['choices'][0]
            output = choice['message']['content']
            choice['logprobs'] = {'content': [
                {'token': piece, 'bytes': list(piece.encode()), 'logprob': -.1, 'top_logprobs': []}
                for piece in re.findall(r'\S+\s*', output)]}
            return httpx.Response(200, json=data)
    endpoint = TokenEndpoint(monkeypatch)
    report = await run_suite(configuration(checks=['consistency'], targets=[{'name': 'local', 'url': endpoint.url, 'model': 'fake-model'}],
        consistency={'levels': levels, 'shapes': ['short'], 'repetitions': 3,
                     'logprobs': {'response_mode': 'complete', 'top_logprobs': 0, 'strict': True, 'tolerance': 0}}))
    result = next(c for c in report.checks if c.id == 'consistency')
    assert result.status == expected, result
    assert result.metrics['logprobs']['conditions'][0]['compared_positions'] >= 64
    assert result.metrics['logprobs']['conditions'][0]['coverage_complete']
    if 2 in levels:
        loaded = result.metrics['logprobs']['conditions'][1]
        assert not loaded['qualified_samples'] and not loaded['coverage_complete']
        assert 'server-side concurrency needs streamed output timing' in result.summary.lower()




@pytest.mark.parametrize('serial', [False, True])
async def test_http_single_chunk_answers_have_honest_concurrency(serial):
    with FakeEndpoint(service_slots=1 if serial else None, first_token_delay=.02, chunk_delay=.02) as endpoint:
        report = await run_suite(configuration(samples=6, targets=[{'name': 'local', 'url': endpoint.url, 'model': 'fake-model'}]))
    for check in report.checks:
        if check.id in {'correctness', 'json'}:
            assert check.status == ('inconclusive' if serial else 'pass'), check
    assert all(r.answer_chunks == 1 for r in report.requests)
