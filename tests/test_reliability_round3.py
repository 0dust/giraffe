"""Final empirical audit regressions; all transports run without sockets."""

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest
import yaml

from giraffe import runner, telemetry
from giraffe.fixtures import builtin_fixtures, final_answer, score_response, traffic_fixtures
from giraffe.models import Arrival, Limits, RunConfig
from tests.test_fixtures import record
from tests.test_gpu_reliability import snapshot
from tests.test_runner import FakeClient, check, config
from tests.test_scoring_reliability import fixture
from tests.test_workload_reliability import MemoryEndpoint
from tests.test_workloads import config as workload_config


@pytest.mark.parametrize('status,valid,maximum,expected', [
    ('timeout', False, 0, 'fail'), ('failed', False, 0, 'fail'),
    ('timeout', False, 1, 'inconclusive'), ('completed', True, 0, 'inconclusive'),
])
async def test_sustained_errors_and_successful_sample_minimum(monkeypatch, status, valid, maximum, expected):
    class Client(FakeClient):
        async def execute(self, spec, **kwargs):
            result = await super().execute(spec, **kwargs)
            if spec.scenario == 'sustained':
                result.status, result.valid = status, valid
            return result

    monkeypatch.setattr(runner, 'LLMClient', Client)
    report = await runner.run_suite(config(checks=['serving', 'first_output', 'recovery'], samples=5,
        sustained_seconds=.001, limits=Limits(min_samples=5, first_output_ms=1000,
                                            latency_ms=1000, max_error_rate=maximum)))
    outcome = check(report, 'recovery')
    assert outcome.status == expected, (outcome.summary, outcome.metrics)
    assert outcome.metrics['sustained']['completed'] < 5
    assert outcome.metrics['sustained_error_rate'] == (0 if valid else 1)
    assert check(report, 'serving').status == 'pass'
    assert check(report, 'first_output').status == 'pass'


@pytest.mark.parametrize('ratio_limit,expected', [(None, 'pass'), (2, 'fail')])
async def test_recovery_absolute_only_pass_discloses_slowdown(monkeypatch, ratio_limit, expected):
    clock = [time.monotonic()]
    completed = asyncio.Barrier(2)

    class Client(FakeClient):
        async def execute(self, spec, **kwargs):
            result = await super().execute(spec, **kwargs)
            result.elapsed_ms = 85 if spec.scenario == 'recovery' else 10
            result.first_output_ms = 8.5 if spec.scenario == 'recovery' else 1
            if spec.scenario == 'sustained':
                if await completed.wait() == 0:
                    clock[0] += .01
                await completed.wait()
            return result

    # Keep asyncio's clock real while the paired sustained slots advance runner time.
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runner, 'LLMClient', Client)
    outcome = check(await runner.run_suite(config(checks=['recovery'],
        limits=Limits(min_samples=2, latency_ms=1000, recovery_max_ratio=ratio_limit))), 'recovery')
    assert outcome.status == expected, (outcome.summary, outcome.metrics['sustained_completion'])
    completion = outcome.metrics['sustained_completion']
    assert completion['reason'] == 'duration elapsed'
    assert completion['requests'] == 4
    assert completion['time_weighted_in_flight'] == pytest.approx(2)
    assert outcome.metrics['latency_ratio'] == 8.5
    if ratio_limit is None:
        assert '8.5' in outcome.summary
        assert 'no ratio limit configured' in outcome.summary.lower()


@pytest.mark.parametrize('name', ['local', 'vllm', 'workloads'])
def test_shipped_examples_enforce_recovery_ratio(name):
    example = yaml.safe_load((Path(__file__).parents[1] / 'examples' / f'{name}.yaml').read_text())
    assert example['limits']['recovery_max_ratio'] == 2


@pytest.mark.parametrize('access_timeout', [None, .05])
async def test_blackholed_connect_fails_access_before_request_deadline(monkeypatch, access_timeout):
    original = httpx.AsyncClient
    observed = []

    async def never_connect(request):
        if request.method == "GET":
            return httpx.Response(404)
        payload = json.loads(request.content)
        if payload.get("max_tokens") == 1 and payload["messages"] == [{"role": "user", "content": "Reply with 1."}]:
            field = "logprobs" if "logprobs" in payload else "stream_options"
            return httpx.Response(400, json={"error": {"param": field}})
        timeout = request.extensions['timeout']['connect']
        observed.append(timeout)
        # A network backend consumes this extension before opening a socket.
        await asyncio.sleep(timeout if timeout is not None else 1)
        raise httpx.ConnectTimeout('simulated blackhole', request=request)

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(never_connect))

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    outcome = check(await runner.run_suite(config(checks=['access'], samples=2,
        targets=[{'name': 'service', 'url': 'http://192.0.2.1/v1', 'model': 'test'}],
        request_timeout_seconds=.05 if access_timeout is None else 2,
        test_options={} if access_timeout is None else {'access': {'request_timeout_seconds': access_timeout}})), 'access')
    assert outcome.status == 'blocked'
    assert 'transport continuity failed' in outcome.summary
    assert observed and all(0 < value < .05 for value in observed)


async def test_sustained_allowance_needs_successes_not_only_attempts(monkeypatch):
    class Client(FakeClient):
        sustained = 0

        async def execute(self, spec, **kwargs):
            result = await super().execute(spec, **kwargs)
            if spec.scenario == 'sustained':
                type(self).sustained += 1
                if self.sustained % 2:
                    result.status, result.valid = 'timeout', False
            return result

    monkeypatch.setattr(runner, 'LLMClient', Client)
    outcome = check(await runner.run_suite(config(checks=['recovery'], max_requests=11,
        sustained_seconds=.5, limits=Limits(min_samples=5, max_error_rate=1, latency_ms=1000))), 'recovery')
    assert outcome.metrics['sustained']['attempted'] >= 5
    assert outcome.metrics['sustained']['completed'] < 5
    assert outcome.status == 'inconclusive'


@pytest.mark.parametrize('example', ['local', 'vllm'])
@pytest.mark.parametrize('mode', ['hang', 'phase_cancel', 'healthy'])
async def test_overlapping_heavy_hangs_survive_sustained_window_in_recovery_evidence(
    monkeypatch, example, mode,
):
    values = yaml.safe_load((Path(__file__).parents[1] / 'examples' / f'{example}.yaml').read_text())
    # Preserve concurrency, sample counts and all acceptance limits. Scale only
    # time: local fault window/deadline/hang = .05/.3/.6; vllm = .3/1.2/.6.
    values.update(checks=['access', 'serving', 'first_output', 'recovery'],
                  request_timeout_seconds=values['request_timeout_seconds'] / 100,
                  sustained_seconds=values['sustained_seconds'] / 100,
                  max_duration_seconds=5)
    if mode == 'healthy':
        # Give the healthy control enough time to establish sustained occupancy
        # despite scheduler jitter; retain the short fault-injection windows.
        values['sustained_seconds'] = max(.3, values['sustained_seconds'])
    values['targets'][0]['url'] = 'http://memory.test/v1'
    configured = RunConfig.model_validate(values)
    fixtures = builtin_fixtures(configured)
    heavy = traffic_fixtures(configured)
    answers = {tuple(m['content'] for m in spec.messages): str(spec.expected)
               for specs in [fixtures['short'], fixtures['sustained'], heavy['long_input'], heavy['long_output']]
               for spec in specs}
    heavy_prompts = {tuple(m['content'] for m in spec.messages)
                     for specs in [heavy['long_input'], heavy['long_output']] for spec in specs}
    original = httpx.AsyncClient
    heavy_open, heavy_started, hangs, interrupted = 0, 0, 0, 0
    phase_end = None

    async def handle(request):
        nonlocal heavy_open, heavy_started, hangs, interrupted, phase_end
        if request.method == 'GET':
            return httpx.Response(404)
        payload = json.loads(request.content)
        if payload.get('max_tokens') == 1 and payload['messages'] == [{'role': 'user', 'content': 'Reply with 1.'}]:
            field = 'logprobs' if 'logprobs' in payload else 'stream_options'
            return httpx.Response(400, json={'error': {'param': field}})
        prompt = tuple(m['content'] for m in payload['messages'])
        is_heavy = prompt in heavy_prompts
        timer = None
        if is_heavy:
            heavy_open += 1
            heavy_started += 1
            if phase_end is None:
                phase_end = asyncio.get_running_loop().time() + configured.sustained_seconds
        try:
            if mode != 'healthy' and is_heavy and heavy_open >= 2:
                hangs += 1
                if mode == 'phase_cancel':
                    # Exercise the real client's cancellation path, without
                    # inventing a cancelled record or discarding the request.
                    timer = asyncio.get_running_loop().call_at(phase_end + .005,
                                                               asyncio.current_task().cancel)
                try:
                    await asyncio.sleep(.6)
                except asyncio.CancelledError:
                    interrupted += 1
                    raise
                # sim2.py closes the connection without headers after its hang.
                raise httpx.RemoteProtocolError('peer closed without response headers')
            await asyncio.sleep(.003)
            answer = answers[prompt]
            split = max(1, len(answer) // (2 if is_heavy else 8))
            events = [dict(choices=[dict(delta={'content': answer[start:start + split]})])
                      for start in range(0, len(answer), split)]
            events.append(dict(choices=[dict(delta={}, finish_reason='stop')],
                               usage={'prompt_tokens': 32, 'completion_tokens': len(answer.split())}))
            class EventStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    for event in events:
                        await asyncio.sleep(.001)
                        yield ('data: ' + json.dumps(event) + '\n\n').encode()
                    yield b'data: [DONE]\n\n'

            return httpx.Response(200, stream=EventStream(),
                                  headers={'content-type': 'text/event-stream'})
        finally:
            if timer is not None:
                timer.cancel()
            if is_heavy:
                heavy_open -= 1

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handle)))
    report = await runner.run_suite(configured)
    outcome = next(c for c in report.checks if c.id == 'recovery')
    sustained = [r for r in report.requests if r.scenario == 'sustained']
    completion = outcome.metrics['sustained_completion']
    assert heavy_started == len(sustained) and heavy_open == 0
    assert outcome.metrics['recovery']['completed'] == configured.samples
    if mode == 'healthy':
        assert outcome.status == report.overall == 'pass', (
            outcome.model_dump(), report.abort_reason, completion)
        assert len(sustained) >= 5 and all(r.status == 'completed' and r.valid for r in sustained)
        assert outcome.metrics['sustained']['error_rate'] == 0
        return
    assert outcome.status == report.overall == ('inconclusive' if mode == 'phase_cancel' else 'fail'), (
        outcome.model_dump(), report.abort_reason,
        [(record.scenario, record.status, record.error) for record in report.requests])
    assert heavy_started == 3 and hangs == 2
    assert sum(r.status == 'completed' and r.valid for r in sustained) == 1
    assert completion['requests'] < completion['required_requests'] == 5
    assert completion['reason'] == 'insufficient load evidence'
    assert completion['successful_requests'] < completion['required_successful_requests'] == 5
    assert outcome.metrics['sustained_error_rate'] == pytest.approx(0 if mode == 'phase_cancel' else 2 / 3)
    if mode == 'phase_cancel':
        assert interrupted == 2
        assert sum(r.status == 'cancelled' for r in sustained) == 2
    elif example == 'local':
        assert interrupted == 2
        assert sum(r.status == 'timeout' for r in sustained) == 2
    else:
        assert interrupted == 0
        assert sum(r.status == 'failed' for r in sustained) == 2
    assert all(c.status == 'pass' for c in report.checks
               if c.id in {'access', 'serving', 'first_output'})


async def test_declared_gpu_count_reaches_assessment(monkeypatch):
    from giraffe import telemetry

    async def scrape(run, target, phase, **kwargs):
        return snapshot(timestamp=time.time() * 1000) | {'phase': phase}

    monkeypatch.setattr(telemetry, 'scrape', scrape)
    configured = config(checks=['gpu'], gpu_min_observation_seconds=0,
        targets=[{'name': 'service', 'url': 'http://unused.test', 'model': 'test',
                  'deployment': {'hardware': {'gpu_count': 8}}}])
    outcome = check(await runner.run_suite(configured), 'gpu')
    assert outcome.status == 'inconclusive'
    assert outcome.metrics['declared_gpu_count'] == 8
    assert outcome.metrics['observed_gpu_count'] == 1


@pytest.mark.parametrize('temperatures,limit,expected', [
    ([99, 99], 85, 'fail'), ([99, 80], 85, 'pass'),
    ([85, 85], 85, 'pass'), ([99, 99], None, 'pass'),
])
def test_gpu_temperature_limit_requires_repeated_observation(temperatures, limit, expected):
    snapshots = [snapshot({'0': [100, 30, temp, 0, 0]}, timestamp=time.time() * 1000)
                 for temp in temperatures]
    metrics, status, _ = telemetry.assess_gpu(snapshots, 60, gpu_max_temperature_c=limit)
    assert status == expected
    assert metrics['max_temperature_c'] == max(temperatures)


@pytest.mark.parametrize('mode,strict,expected', [
    ('text_drift', False, 'inconclusive'), ('text_drift', True, 'fail'),
    ('logprob_drift', False, 'inconclusive'), ('healthy', False, 'pass'),
])
async def test_nonstrict_consistency_does_not_certify_variation(monkeypatch, mode, strict, expected):
    endpoint = MemoryEndpoint(monkeypatch, mode=mode)
    settings = {'shapes': ['short'], 'repetitions': 3, 'strict': strict}
    if mode == 'logprob_drift':
        settings['logprobs'] = {'top_logprobs': 0, 'tolerance': .01}
    outcome = next(c for c in (await runner.run_suite(workload_config(endpoint,
        checks=['consistency'], stream=True, consistency=settings))).checks if c.id == 'consistency')
    assert outcome.status == expected
    if expected == 'inconclusive':
        assert ('server-side concurrency needs streamed output timing' if mode == 'logprob_drift'
                else 'variation observed; enable strict to enforce') in outcome.summary.lower()


async def test_consistency_summary_discloses_incorrect_answers(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch, mode='wrong_copy')
    outcome = next(c for c in (await runner.run_suite(workload_config(endpoint,
        checks=['consistency'], stream=False,
        consistency={'shapes': ['short'], 'repetitions': 3}))).checks if c.id == 'consistency')
    assert outcome.metrics['correctness_observed'] == 0
    assert 'correctness_observed: 0' in outcome.summary


@pytest.mark.parametrize('threshold,wait,achieved', [(None, 17, True), (10, 17, False), (50, 60, False)])
async def test_arrivals_dispatch_wait_tolerance(threshold,wait,achieved):
    from giraffe import load_test
    from tests.test_concurrent_load import configuration
    from tests.test_load_executor_regressions import record as load_record
    c=configuration(load={'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':True},'rates':[16]},'pass_criteria':{'max_first_answer_ms':None,'max_completion_ms':threshold if threshold is not None else 1000}})
    run=runner._Run(c,None,None)
    p=load_test.test_points(c.load_test)[0]
    p.update(sending_start_ms=0,sending_end_ms=1000,delivered_window_seconds=1,observation_seconds=1,cut_short=False,not_run=False)
    rows=[load_record(str(i),arrival_elapsed_ms=wait+2,arrival_first_output_ms=wait+1,dispatch_lag_ms=wait,load_point={'shape':'short_short'}) for i in range(16)]
    load_test.assess_point(run,c.targets[0],c.load_test,p,rows)
    assert p['dispatched'] == 16
    assert (p['status'] == 'pass') is achieved
    assert p['groups'][0]['timing_clock'] == 'planned arrival (includes dispatch delay)'
    assert p['groups'][0]['dispatch_delay_ms']['median'] == wait


@pytest.mark.parametrize('tokens,stream,missing', [(1, True, False), (2, True, False),
    (3, True, True), (None, True, True), (2, False, True)])
def test_single_chunk_gap_is_inapplicable_only_for_known_short_stream(tokens, stream, missing):
    response = record(fixture(), '4', stream=stream, answer_chunks=1,
                      completion_tokens=tokens, max_stream_gap_ms=None)
    assert ('stream_gap_ms' in runner._missing_timing([response], Limits(stream_gap_ms=50))) is missing


@pytest.mark.parametrize('tokens,expected', [(2, 'pass'), (3, 'inconclusive')])
async def test_capacity_single_chunk_gap_qualification(monkeypatch,tokens,expected,local_tokenizer):
    from giraffe import load_test
    from tests.test_concurrent_load import Client as TokenClient,configuration,short_windows
    class Client(TokenClient):
        async def execute(self,spec,**kwargs):
            r=await super().execute(spec,**kwargs)
            r.completion_tokens,r.output_tokens,r.answer_chunks,r.max_stream_gap_ms=tokens,tokens,1,None
            r.answer_reads=1
            return r
    c=short_windows(configuration(load={'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':True},'rates':[20]},'pass_criteria':{'max_first_answer_ms':1000,'max_stream_pause_ms':50}}),.2)
    run=runner._Run(c,None,None)
    await load_test.execute(run,c.targets[0],Client())
    outcome=load_test.result(run,c.targets[0])
    assert outcome.status == expected
    g=outcome.metrics['points'][0]['groups'][0]
    assert ('stream_gap_ms' in g['missing_timing']) is (tokens > 2)


@pytest.mark.parametrize('tag', ['think', 'thinking', 'reasoning', 'ThInK'])
def test_embedded_closed_reasoning_cannot_supply_contains_answer(tag):
    spec = fixture('contains', '4')
    output = f'14 <{tag}>4</{tag}>'
    assert score_response(spec, record(spec, output)).score is False
    assert final_answer(f'blue <{tag}>secret</{tag}> green') == ('blue  green', True, False)


def test_new_optional_config_defaults_and_validation():
    assert Arrival().max_client_wait_ms == 50
    assert Limits().gpu_max_temperature_c is None
    with pytest.raises(ValueError):
        Arrival(max_client_wait_ms=-1)
    with pytest.raises(ValueError):
        Limits(gpu_max_temperature_c=-1)
