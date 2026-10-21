"""Arrival pressure and upgrade decisions through real loopback HTTP sockets."""

import asyncio

from giraffe.models import Limits, RunConfig, Target, TrafficConfig, required_concurrency
from giraffe.reporting import compare_baseline
from giraffe.runner import run_suite
from tests.fake_endpoint import FakeEndpoint


def traffic_config(endpoint, **overrides):
    from tests.test_concurrent_load import configuration, short_windows
    traffic=overrides.pop('traffic',TrafficConfig(rates=[10,50],duration_seconds=.6,drain_timeout_seconds=3,max_in_flight=32,mix={'short':1,'long_input':0,'long_output':0}))
    limits=overrides.pop('limits',Limits(latency_ms=250,min_samples=5))
    checks=overrides.pop('checks',['load'])
    shapes={'short':'short_short','long_input':'long_short','long_output':'short_long'}
    weights={shapes[k]:v*100 for k,v in traffic.mix.model_dump().items() if v>0}
    workload=({'combinations':{'selected':list(weights)}} if len(weights)==1 else
        {'combinations':{'enabled':False},'mixed':{'enabled':True,'selected':list(weights),'share_mode':'custom','shares':weights}})
    if overrides.get('request_source') == 'request_pack':
        workload={'combinations':{'selected':['short_short']}}
    values=dict(targets=[{'name':'service','url':endpoint.url,'model':'fake-model'}],checks=checks,concurrency=32,max_requests=100,max_duration_seconds=10,stop_after_errors=100,
        load={'workloads':workload,'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':traffic.arrival=='steady'},'poisson':{'enabled':traffic.arrival=='poisson','seed':traffic.seed},'rates':traffic.rates},
              'schedule':{'finish_seconds':traffic.drain_timeout_seconds},'request_timeout_seconds':3,
              'pass_criteria':{'max_first_answer_ms':limits.first_output_ms,'max_completion_ms':limits.latency_ms,'min_valid_responses':limits.min_samples}})
    cfg=configuration(**(values | overrides))
    return short_windows(cfg,traffic.duration_seconds)


def capacity(report):
    return next(check for check in report.checks if check.id == 'load')


def traffic_records(report):
    return [r for r in report.requests if r.load_point.get('type') not in {None,'calibration'}]


async def test_arrivals_reach_http_server_before_prior_requests_complete():
    with FakeEndpoint(first_token_delay=.25,chunk_delay=0) as endpoint:
        cfg=traffic_config(endpoint,traffic=TrafficConfig(rates=[20],duration_seconds=.3,max_in_flight=32,mix={'short':1,'long_input':0,'long_output':0}),limits=Limits(latency_ms=1000,min_samples=5))
        assert required_concurrency(cfg)['per_target_peak'] == 32
        report=await run_suite(cfg)
        setup=sum(r.load_point.get('type') == 'calibration' for r in report.requests)
        assert endpoint.request_started_at[setup+2] < min(endpoint.request_finished_at[setup:])
        assert endpoint.peak_active >= 3
        assert len(endpoint.requests) == 6 + setup
    p=capacity(report).metrics['points'][0]; g=p['groups'][0]
    assert p['scheduled'] == p['dispatched'] == g['correct_work_meeting_limits'] == 6
    assert p['status']=='pass' and p['local_drops'] == 0
    assert all(r.score for r in traffic_records(report))


async def test_mixed_http_workload_scores_short_long_input_and_long_output():
    with FakeEndpoint(first_token_delay=.001,chunk_delay=0) as endpoint:
        cfg=traffic_config(endpoint,traffic=TrafficConfig(rates=[30],duration_seconds=1,seed=42,max_in_flight=32,long_input_chars=512,long_output_words=16),limits=Limits(latency_ms=1000,min_samples=2))
        report=await run_suite(cfg)
    rows=traffic_records(report); p=capacity(report).metrics['points'][0]
    assert {r.load_point['shape'] for r in rows} == {'short_short','long_short','short_long'}
    assert len(rows) == p['scheduled'] == p['dispatched'] == 30
    assert all(r.score is True for r in rows)
    assert max(r.input_chars for r in rows) >= 512 and max(r.output_tokens for r in rows) >= 16
    assert sum(g['correct_work_meeting_limits'] for g in p['groups']) == 30
    assert {g['shape'] for g in p['groups']} == {'short_short','long_short','short_long'}


async def test_slower_upgrade_reduces_accepted_rate_as_http_queue_grows():
    with FakeEndpoint(first_token_delay=.002,chunk_delay=0,service_slots=1) as endpoint:
        cfg=traffic_config(endpoint)
        baseline=await run_suite(cfg)
        endpoint.first_token_delay=.05
        candidate=await run_suite(cfg)
    old,current=capacity(baseline).metrics,capacity(candidate).metrics
    assert old['highest_accepted'][0]['value'] == 50 and current['highest_accepted'][0]['value'] == 10
    low,high=current['points']; lg,hg=low['groups'][0],high['groups'][0]
    assert low['status']=='pass' and high['status']=='fail'
    assert high['dispatched']==high['scheduled'] and high['local_drops']==high['late_arrivals']==0
    assert hg['completion_p95_ms'] > lg['completion_p95_ms']*3
    assert hg['correct_work_per_second'] < hg['completed_requests_per_second']
    assert all(r.valid and r.score is True for r in traffic_records(candidate))
    compared=compare_baseline(candidate,baseline)
    assert compared.baseline['status']=='fail' and compared.overall=='fail'


async def test_stop_during_http_arrivals_preserves_partial_accounting():
    stop=asyncio.Event()
    def progress(event):
        if event['event']=='request_started' and event['scenario'].startswith('load_primary_') and event['attempted']>=4: stop.set()
    with FakeEndpoint(first_token_delay=.3,chunk_delay=0) as endpoint:
        cfg=traffic_config(endpoint,traffic=TrafficConfig(rates=[20],duration_seconds=2,max_in_flight=32,mix={'short':1,'long_input':0,'long_output':0}))
        report=await asyncio.wait_for(run_suite(cfg,progress=progress,stop_event=stop),3)
    p=capacity(report).metrics['points'][0]
    assert report.abort_reason=='cancelled by user'
    assert p['scheduled']==p['dispatched']+p['not_sent']
    assert p['not_sent']>0 and p['status']=='inconclusive'
    assert len(traffic_records(report))==p['dispatched']<=3


async def test_request_pack_quiet_passes_but_loaded_wrong_answer_is_case_failure():
    # The actual HTTP handler varies only the answer of one request while another
    # is active. No runner scenario metadata is sent to the service.
    with FakeEndpoint(first_token_delay=.07, chunk_delay=0) as endpoint:
        normal_answer = endpoint._answer

        def answer_under_load(prompt):
            if "PACK_ALPHA" in prompt and endpoint.active > 1:
                return "INCORRECT"
            return normal_answer(prompt)

        endpoint._answer = answer_under_load
        config = traffic_config(
            endpoint, checks=["capacity", "correctness"], max_output_tokens=9,
            request_source="request_pack", request_options={"temperature": .2},
            request_pack={"version": 1, "cases": [
                {"fixture_id": "alpha", "messages": [{"role": "user", "content": "Copy exactly: PACK_ALPHA"}],
                 "scorer": "exact", "expected": "PACK_ALPHA", "max_tokens": 4,
                 "options": {"temperature": .1}},
                {"fixture_id": "beta", "messages": [{"role": "user", "content": "Copy exactly: PACK_BETA"}],
                 "scorer": "contains", "expected": "PACK_BETA"},
            ]},
            traffic=TrafficConfig(rates=[5, 40], duration_seconds=.4, max_in_flight=32),
            limits=Limits(latency_ms=1000, min_samples=2),
        )
        report = await run_suite(config)
        payloads = endpoint.requests

    result = capacity(report)
    low, high = result.metrics['points']
    lg, hg = low['groups'][0], high['groups'][0]
    assert low["status"] == "pass" and high["status"] == "fail"
    assert result.metrics["highest_accepted"][0]["value"] == 5
    assert next(c for c in report.checks if c.id == "correctness").metrics["request_pack"]["control_status"] == "pass"
    assert sum(r.score is True for r in traffic_records(report) if r.load_point["point_id"] == high["id"] and r.workload.get("case_id") == "alpha") < sum(r.score is not None for r in traffic_records(report) if r.load_point["point_id"] == high["id"] and r.workload.get("case_id") == "alpha")
    assert all(r.score is True for r in traffic_records(report) if r.load_point["point_id"] == high["id"] and r.workload.get("case_id") == "beta")
    assert hg["correct_work_per_second"] < hg["completed_requests_per_second"]
    assert all(record.valid for record in report.requests)
    assert len(report.requests) == len(payloads) == 20
    assert next(check for check in report.checks if check.id == "correctness").status == "fail"
    for first in payloads[:2]:
        repeated = [payload for payload in payloads if payload["messages"] == first["messages"]]
        assert all(payload == first for payload in repeated)
    alpha = next(payload for payload in payloads if "ALPHA" in payload["messages"][0]["content"])
    assert alpha["max_tokens"] == 4 and alpha["temperature"] == .1


async def test_reference_judging_runs_after_http_traffic_without_extending_measurements():
    import json

    with FakeEndpoint(first_token_delay=.005, chunk_delay=0) as endpoint, FakeEndpoint(
        first_token_delay=.09, chunk_delay=0,
    ) as judge:
        endpoint._answer = lambda _: "Within thirty days." if len(endpoint.requests) == 1 else "Within 60 days."
        seen_inputs = []

        def evaluate(prompt):
            data = json.loads(prompt.splitlines()[-1])
            seen_inputs.append(data)
            return json.dumps({"verdict": "thirty" in data["generated_answer"],
                               "reason": "Matches the reference duration." if "thirty" in data["generated_answer"]
                               else "The refund duration differs."})

        judge._answer = evaluate
        config = traffic_config(
            endpoint, checks=["capacity", "correctness"], stream=False,
            max_duration_seconds=.3,
            request_source="request_pack", request_pack={"version": 1, "cases": [
                {"fixture_id": "refund", "messages": [{"role": "user", "content": "What is the refund window?"}],
                 "scorer": "judge", "expected": "Within 30 days.", "rubric": "The number of days must agree."},
            ]},
            judging={"judges": [{"name": "reviewer", "url": judge.url, "model": "judge-model"}],
                     "concurrency": 1, "max_duration_seconds": 3},
            traffic=TrafficConfig(rates=[30], duration_seconds=.1, max_in_flight=32),
            limits=Limits(latency_ms=1000, min_samples=2),
        )
        report = await run_suite(config)
        assert len(endpoint.requests) == len(judge.requests) == len(report.requests) == 4
        assert min(judge.request_started_at) >= max(endpoint.request_finished_at)
        assert all(payload["model"] == "judge-model" for payload in judge.requests)
        assert all(data["messages"] == [{"role": "user", "content": "What is the refund window?"}]
                   and data["reference_answer"] == "Within 30 days."
                   and data["rubric"] == "The number of days must agree." for data in seen_inputs)

    result = capacity(report)
    stage = result.metrics["points"][0]
    evaluation = report.observations["judging"]
    assert evaluation["judge_calls_completed"] == evaluation["judge_calls_total"] == 4
    assert evaluation["passed_requests"] == 1 and evaluation["failed_requests"] == 3
    assert evaluation["duration_seconds"] >= .3
    assert report.observations["inference_duration_seconds"] < .3
    assert stage["observation_seconds"] < .2
    assert stage["dispatched"] == stage["groups"][0]["completed"] == 3
    assert stage["groups"][0]["correct_work_meeting_limits"] == 0
    assert stage["groups"][0]["case_coverage"]["refund"] == 3
    assert stage["groups"][0]["scored"] == 3
    assert stage["groups"][0]["correct"] == 0
    assert stage["status"] == result.status == "fail"
    assert next(c for c in report.checks if c.id == "correctness").metrics["request_pack"]["control_status"] == "pass"
    assert next(check for check in report.checks if check.id == "correctness").status == "fail"
    assert report.abort_reason is None
    assert report.observations["request_budget_used"] == 4
    assert all(record.valid and record.judgement for record in report.requests)
