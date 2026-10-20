import asyncio
import copy
import json
import math
import random
import time
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from giraffe.concurrent_load import (
    aggregate, calibration_keys, execute, interference_measurements, load_time_plan, measurements,
    minimum_reservation, point_shapes, prompt, result, score_point, test_points as load_points,
)
from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_OPTION_FIELDS, ConcurrentLoad, RequestRecord, RunConfig, SHAPES, migrate_concurrent_load
from giraffe.planning import request_plan
from giraffe.runner import _Run, run_suite
from giraffe.web import _configuration_errors, create_app


TARGET = {"name": "local", "url": "http://localhost:1/v1", "model": "test"}


def configuration(**values):
    load = {"profile": {s: {"input_tokens": n, "output_tokens": o} for s, n, o in
                         [("short", 64, 8), ("medium", 96, 16), ("long", 128, 32)]},
            "combinations": {"selected": ["short_short"]}, "mixed": {"enabled": False},
            "schedule": {"levels": [1], "duration_seconds": 1}, "pass_criteria": {"min_valid_responses": 2}}
    extra = values.pop("load", {})
    for k, v in extra.items():
        load[k] = {**load.get(k, {}), **v} if isinstance(v, dict) else v
    return RunConfig.model_validate({"targets": [TARGET], "checks": ["concurrency"], "max_requests": 500,
                                     "concurrent_load": load, **values})


def short_windows(config, seconds=.2):
    config.concurrent_load.schedule.duration_seconds = seconds
    return config


class Client:
    def __init__(self, *, serial=False, unknown=False, cached=False, failure=False, buffered=False, wrong=False, cap_violation=False, reasoning=False):
        self.serial, self.unknown, self.cached = serial, unknown, cached
        self.failure, self.buffered, self.wrong = failure, buffered, wrong
        self.cap_violation, self.reasoning = cap_violation, reasoning
        self.lock = asyncio.Lock()
        self.started, self.finished, self.prompts, self.specs = [], [], [], []
        self.clock = time.monotonic()

    async def execute(self, spec, first_output_event=None):
        self.specs.append(spec)
        calibration = spec.scenario.startswith("load_calibration_")
        record = RequestRecord(id=uuid.uuid4().hex, target="local", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now", status="completed", valid=True,
            input_chars=spec.input_chars, requested_max_tokens=spec.max_tokens, stream=spec.stream,
            input_tokens=None if self.unknown else round(spec.input_chars / 4), output="K7P4", completion_tokens=spec.max_tokens,
            output_tokens=spec.max_tokens, finish_reason="stop", response_body_bytes=10)
        if self.failure:
            record.status, record.valid, record.error, record.http_status = "failed", False, "HTTP 500", 500
            return record
        if calibration:
            return record
        self.prompts.append(spec.messages[0]["content"])
        index = len(self.started)
        self.started.append(time.monotonic())
        async def response():
            start = time.monotonic()
            record._timing_started_monotonic = start
            await asyncio.sleep(.002)
            if first_output_event is not None:
                first_output_event.set()
                if hasattr(first_output_event, "on_answer_chunk"):
                    first_output_event.on_answer_chunk()
            record.first_output_ms = 2
            for _ in range(3):
                await asyncio.sleep(.004 if index % 2 else .007)
                if first_output_event is not None and hasattr(first_output_event, "on_answer_chunk"):
                    first_output_event.on_answer_chunk()
            record.elapsed_ms = (time.monotonic() - start) * 1000
            record.last_output_ms = record.elapsed_ms - 1
            record.stream_end_ms = record.elapsed_ms
            record.answer_chunks = 1 if self.buffered else 4
            record.max_stream_gap_ms = 7
        if self.serial:
            async with self.lock:
                await response()
        else:
            await response()
        if self.wrong:
            record.output = "wrong"
        if self.cached:
            record.cached_prompt_tokens = record.input_tokens
        if self.cap_violation:
            record.completion_tokens += 1
        if self.reasoning:
            record.output, record.reasoning, record.reasoning_tokens = "", "thinking", spec.max_tokens
            record.finish_reason, record.valid = "length", False
            record.error = "reasoning exhausted the output budget before an answer"
        self.finished.append(time.monotonic())
        return record


async def perform(config=None, client=None):
    config, client = short_windows(config or configuration()), client or Client()
    events = []
    run = _Run(config, events.append, None)
    run.run_id = "fixed-seed"
    await execute(run, config.targets[0], client)
    return run, result(run, config.targets[0]), client, events


def point(check):
    return next(iter(check.metrics["load_types"].values()))["points"][0]


def test_defaults_and_planning_numbers():
    load = ConcurrentLoad()
    assert load.profile.long.output_tokens == 128
    assert load.schedule.duration_seconds == 15
    assert sum(v["configured_load_seconds"] for v in load_time_plan(load).values()) == 120
    assert "concurrency" not in CHECK_OPTION_FIELDS and "concurrency" not in CHECK_LIMIT_FIELDS
    c = configuration(concurrency=8, max_duration_seconds=1000,
        load={"combinations": {"selected": list(SHAPES)}, "schedule": {"levels": [8, 1, 4], "duration_seconds": 30}})
    assert c.concurrent_load.schedule.levels == [1, 4, 8]
    plan = request_plan(c)
    assert plan["configured_load_seconds"] == plan["minimum_seconds"] == 810
    assert plan["concurrent_load"]["combinations"]["test_points"] == 27
    assert next(r for r in plan["rows"] if r["id"] == "load_combinations")["requests_per_target"] is None
    assert plan["load_warnings"][0]["fields"] == ["max_requests"]
    i = configuration(load={"combinations": {"enabled": False}, "interference": {"enabled": True}, "schedule": {"levels": [2], "duration_seconds": 30}})
    assert request_plan(i)["configured_load_seconds"] == 60
    assert minimum_reservation(i.concurrent_load) == 3


@pytest.mark.parametrize("path,value,fields", [
    ("schedule.levels", [8], ["concurrent_load.schedule.levels", "concurrency"]),
    ("profile.long.output_tokens", 256, ["concurrent_load.profile.long.output_tokens", "max_output_tokens"]),
    ("combinations.selected", [], ["concurrent_load.combinations.selected"]),
    ("profile.medium.input_tokens", 1, ["concurrent_load.profile.medium.input_tokens"]),
    ("profile.long.input_tokens", 131073, ["concurrent_load.profile.long.input_tokens"]),
    ("profile.short.output_tokens", 0, ["concurrent_load.profile.short.output_tokens"]),
    ("schedule.duration_seconds", 3601, ["concurrent_load.schedule.duration_seconds"]),
    ("schedule.levels", [2, 2], ["concurrent_load.schedule.levels"]),
    ("combinations.enabled", False, ["concurrent_load.combinations.enabled"]),
])
def test_structured_errors(path, value, fields):
    values = configuration().model_dump()
    node = values["concurrent_load"]
    parts = path.split(".")
    for key in parts[:-1]:
        node = node[key]
    node[parts[-1]] = value
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    error = _configuration_errors(exc.value.errors(), values)[0]
    assert error["fields"] == fields


@pytest.mark.parametrize("updates,fields", [
    ({"mixed": {"enabled": True, "shares": {"short_short": 99}}}, ["concurrent_load.mixed.shares.short_short"]),
    ({"stress": {"enabled": True, "large_input": False, "large_output": False}}, ["concurrent_load.stress.large_input"]),
    ({"interference": {"enabled": True}}, ["concurrent_load.schedule.levels", "concurrent_load.interference.enabled"]),
])
def test_other_structured_errors(updates, fields):
    values = configuration().model_dump()
    for k, v in updates.items():
        values["concurrent_load"][k].update(v)
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    assert _configuration_errors(exc.value.errors(), values)[0]["fields"] == fields


@pytest.mark.parametrize("which", ["stream", "overlap", "time", "context"])
def test_cross_field_errors(which):
    values = configuration().model_dump()
    expected = []
    if which == "stream":
        values["stream"] = False
        values["concurrent_load"]["schedule"]["levels"] = [2]
        values["concurrent_load"]["interference"]["enabled"] = True
        expected = ["concurrent_load.interference.enabled", "stream"]
    if which == "overlap":
        values.update(overlap_models=True, targets=[TARGET, {**TARGET, "name": "other"}], concurrency=3)
        values["concurrent_load"]["schedule"]["levels"] = [2]
        expected = ["concurrent_load.schedule.levels", "concurrency"]
    if which == "time":
        values["max_duration_seconds"] = .5
        expected = ["concurrent_load.schedule.duration_seconds", "max_duration_seconds"]
    if which == "context":
        values["targets"][0]["deployment"]["serving"]["context_limit"] = 50
        expected = ["concurrent_load.profile.short.input_tokens"]
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    assert _configuration_errors(exc.value.errors(), values)[0]["fields"] == expected


async def test_individual_refill_duration_drain_nonce_and_conditions():
    run, check, client, events = await perform(configuration(load={"schedule": {"levels": [2]}}))
    p = point(check)
    assert p["sent"] > 2 and p["delivered_window_seconds"] >= .199
    assert client.started[2] < client.finished[1]  # faster slot refills before slow slot completes
    assert p["drain_seconds"] > 0 and p["completed"] == p["sent"]
    assert p["client_mean_inflight"] >= 1.8 and p["server_evidenced_concurrency"] == 2
    assert check.status == "pass" and p["requests_per_second"] > 0
    assert len(set(client.prompts)) == len(client.prompts)
    assert len({len(p) for p in client.prompts}) == 1
    assert all(r.load_point and r.requested_input_tokens and not r.workload for r in run.records)
    assert {"condition_planned", "condition_started", "condition_finished"} <= {e["event"] for e in events}
    assert all(r.condition_id == p["id"] for r in run.records if r.load_point["type"] != "calibration")


@pytest.mark.parametrize("client,code", [(Client(serial=True), "load_not_delivered"), (Client(cached=True), "cached_input"),
    (Client(wrong=True), "correctness"), (Client(cap_violation=True), "output_cap"), (Client(reasoning=True), "insufficient_responses"),
    (Client(failure=True), "calibration_failed")])
async def test_evidence_and_failure_classification(client, code):
    load = {"schedule": {"levels": [2]}, "combinations": {"selected": ["long_short"]}}
    _, check, _, _ = await perform(configuration(load=load), client)
    assert point(check)["reason_code"] == code
    if code in {"correctness", "output_cap", "calibration_failed"}:
        assert check.status == "fail"
    else:
        assert check.status == "inconclusive"
    if code == "insufficient_responses":
        assert point(check)["reasoning_budget_exhausted"] == point(check)["sent"]
        assert point(check)["failed"] == 0


async def test_unknown_tokens_and_stress_never_report_zero():
    _, check, _, _ = await perform(configuration(load={"combinations": {"enabled": False}, "stress": {"enabled": True, "large_output": False}}), Client(unknown=True))
    p = point(check)
    assert p["status"] == "inconclusive" and p["reason_code"] == "input_not_verified"
    assert p["token_lengths"]["long_short"]["input_tokens"] == {"median": None, "min": None, "max": None}
    assert p["output_tokens_per_second"] is not None
    assert check.metrics["calibrations"][0]["reason"].startswith("estimated")


async def test_mixed_seed_and_scenarios():
    c = configuration(load={"combinations": {"enabled": False}, "mixed": {"enabled": True, "seed": 7,
        "shares": {"short_short": 60, "long_short": 40}}})
    r1, a, _, _ = await perform(c)
    r2, b, _, _ = await perform(c)
    seq = lambda run: [r.load_point["shape"] for r in run.records if r.load_point["type"] == "mixed"]
    assert seq(r1)[:8] == seq(r2)[:8]
    assert a.metrics["mixed_seed"] == 7
    assert set(point(a)["observed_mix"]) == {"short_short", "long_short"}
    assert all(r.scenario == f"load_mixed_c1.{r.load_point['shape']}" and r.condition_id == "load_mixed_c1" for r in r1.records if r.load_point["type"] == "mixed")
    assert math.isclose(sum(x["inflight_percent"] for x in point(a)["observed_mix"].values()), 100)


@pytest.mark.parametrize("cap,threshold", [(512, 410), (128, 103)])
async def test_stress_ceil_and_coverage_alone_does_not_pass(cap, threshold):
    c = configuration(max_output_tokens=cap, load={"profile": {s: {"input_tokens": n, "output_tokens": o} for s, n, o in
        [("short", 64, 8), ("medium", 96, 16), ("long", 128, cap)]}, "combinations": {"enabled": False},
        "stress": {"enabled": True, "large_input": False}, "pass_criteria": {"response_p95_ms": 1}})
    _, check, _, _ = await perform(c)
    assert point(check)["coverage"]["threshold_tokens"] == threshold
    assert point(check)["coverage"]["covered_valid_responses"] >= 2
    assert check.status == "fail" and point(check)["reason_code"] == "timing"


async def test_interference_alternates_and_triggers_after_chunks():
    c = configuration(load={"combinations": {"enabled": False}, "interference": {"enabled": True, "trigger": "after_chunks", "trigger_chunks": 2},
                            "schedule": {"levels": [2]}})
    run, check, _, _ = await perform(c)
    probes = [r for r in run.records if r.load_point.get("role") == "probe"]
    assert {r.load_point["sub_window"] for r in probes} == {0, 1, 2, 3}
    assert all(r.load_point["shape"] == ("long_short" if r.load_point["sub_window"] % 2 else "short_short") for r in probes)
    data = point(check)["interference"]
    assert data["control"]["matched_count"] and data["probe"]["matched_count"]
    assert data["trigger_latency_ms"]["median"] > 0


@pytest.mark.parametrize("slowdown,limit,status", [(2, 1.5, "fail"), (1.1, 1.5, "pass"), (1.1, None, "inconclusive")])
async def test_interference_slowdown_scoring(slowdown, limit, status):
    run, check, _, _ = await perform()
    p = copy.deepcopy(point(check))
    settings = run.config.concurrent_load.model_copy(deep=True)
    settings.interference.max_slowdown = limit
    p["type"] = "interference"
    p["interference"] = {"control": {"matched_count": 2}, "probe": {"matched_count": 2}, "slowdown": slowdown, "buffered_stream": False}
    score_point(settings, p, [])
    assert p["status"] == status
    p["interference"]["buffered_stream"] = True
    p["interference"]["slowdown"] = 1
    assert score_point(settings, p, [])["reason_code"] == "buffered_stream"


async def test_budget_cut_short_failures_precede_evidence_and_later_reservation():
    c = configuration(max_requests=8, checks=["concurrency", "consistency"], consistency={"repetitions": 2, "levels": [1], "shapes": ["short"], "prompts_per_shape": 2})
    run, check, _, _ = await perform(c)
    p = point(check)
    assert p["request_allowance"] == 3 and p["reserved_later_requests"] == 4
    assert p["cut_short"] and check.status == "inconclusive"
    for kind in ("error", "wrong", "timing"):
        q = copy.deepcopy(p)
        settings = c.concurrent_load.model_copy(deep=True)
        if kind == "error":
            q["error_percent"] = 100
        elif kind == "wrong":
            q.update(scored=1, correct=0, correct_percent=0)
        else:
            settings.pass_criteria.response_p95_ms = 1
            q.update(response_p95_ms=5, timing_quality={"affected_metrics": {}})
        records = [RequestRecord(id="r", target="local", fixture_id="x", scenario="x", check_ids=[], started_at="now", status="completed", valid=True, elapsed_ms=5)]
        assert score_point(settings, q, records)["status"] == "fail"
    q = copy.deepcopy(p)
    q.update(delivered_window_seconds=.1, configured_window_seconds=.2, valid=5, load_qualified=True, response_p50_ms=1)
    assert score_point(c.concurrent_load, q, [])["status"] == "pass"
    q["delivered_window_seconds"] = .01
    assert score_point(c.concurrent_load, q, [])["status"] == "inconclusive"


@pytest.mark.parametrize('reported', [False, True])
async def test_known_runtime_context_not_sent(reported, monkeypatch):
    from giraffe import deployment
    from giraffe.concurrent_load import context_window
    c = short_windows(configuration(load={"combinations": {"selected": ["long_short"]}}))
    target = c.targets[0]
    target.deployment.serving['context_limit'] = 1000 if reported else 100
    target.deployment.discovery = 'none'
    if reported:
        target.deployment.collector_url = 'http://collector/metadata'
        monkeypatch.setattr(deployment, '_discovery_client', lambda *args: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'serving': {'context_limit': 100}}))))
    snapshot = await deployment.snapshot(target, c, time.monotonic() + 1, asyncio.Event())
    run = _Run(c, None, None)
    run.observations["local"]["deployment"] = snapshot
    # The snapshot takes precedence over subsequent target metadata.
    target.deployment.serving['context_limit'] = 2048
    limit, source = context_window(target, run.observations['local'])
    assert limit == 100 and ('reported' if reported else 'configured') in source
    assert ('configured-collector' if reported else 'user-supplied') in source
    client = Client()
    await execute(run, target, client)
    assert not client.specs
    p = point(result(run, target))
    assert p["reason_code"] == "context_exceeded" and source in p['reason']


async def test_error_threshold_stop_still_fails_and_remaining_points_not_run():
    class Errors(Client):
        async def execute(self, spec, **kw):
            self.failure = not spec.scenario.startswith("load_calibration_")
            return await super().execute(spec, **kw)
    run, check, _, _ = await perform(configuration(stop_after_errors=1, load={"combinations": {"selected": ["short_short", "long_short"]}}), Errors())
    rows = check.metrics["load_types"]["combinations"]["points"]
    assert rows[0]["status"] == "fail" and rows[0]["reason_code"] == "error_rate"
    assert rows[1]["not_run"] and rows[1]["status"] == "inconclusive"
    assert run.errors == 1 and check.status == "fail"


@pytest.mark.parametrize("endpoint", ["draft", "validate", "preview", "plan"])
async def test_migration_metadata_endpoints(endpoint, tmp_path):
    old = {"targets": [TARGET], "checks": ["buckets"], "buckets": {"levels": [1], "samples_per_bucket": 7}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://test") as client:
        response = await client.post(f"/api/config/{endpoint}", json=old)
    assert response.status_code == 200, response.text
    data = response.json()
    assert any("samples_per_bucket=7" in n for n in data["migration_notes"])
    assert "concurrent_load" in data["config"] and "buckets" not in data["config"]
    assert not RunConfig.model_validate(data["config"])._migration_notes


async def test_suite_wires_new_check_and_manifest_notes(monkeypatch):
    from giraffe import runner
    class ContextClient(Client):
        def __init__(self, target, config):
            super().__init__()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(runner, 'LLMClient', ContextClient)
    c = short_windows(configuration())
    c._migration_notes = ['saved migration note']
    report = await run_suite(c)
    check = next(x for x in report.checks if x.id == 'concurrency')
    assert check.status == 'pass'
    assert 'scenarios' not in check.metrics
    assert report.manifest['config_migration_notes'] == ['saved migration note']
    assert check.metrics['migration_notes'] == ['saved migration note']
    assert not {'fairness', 'buckets', 'mixed', 'token_stress'} & set(report.manifest['config']['checks'])


async def test_socket_endpoint_load_integration():
    from tests.fake_endpoint import FakeEndpoint
    with FakeEndpoint(first_token_delay=.005, chunk_delay=.005) as endpoint:
        c = short_windows(configuration(targets=[{**TARGET, 'url': endpoint.url}]))
        report = await run_suite(c)
    check = next(x for x in report.checks if x.id == 'concurrency')
    assert check.metrics['load_types']['combinations']['points'][0]['sent'] > 2
    assert report.requests[0].scenario.startswith('load_calibration_')


async def test_request_timeout_drain_and_deadline_cancellation():
    class Slow(Client):
        async def execute(self, spec, **kwargs):
            if not spec.scenario.startswith('load_calibration_'):
                await asyncio.sleep(.3)
            return await super().execute(spec, **kwargs)
    _, check, _, _ = await perform(configuration(request_timeout_seconds=.03), Slow())
    assert point(check)['timed_out'] > 0 and check.status == 'fail'
    c = short_windows(configuration(), .2)
    run = _Run(c, None, None)
    run.deadline = time.monotonic() + .04
    await execute(run, c.targets[0], Slow())
    p = point(result(run, c.targets[0]))
    assert p['cancelled'] == 1 and p['timed_out'] == 0 and p['error_percent'] == 0
    assert p['status'] == 'inconclusive'


async def test_missing_completion_counts_leave_throughput_unknown():
    class NoOutputCount(Client):
        async def execute(self, spec, **kwargs):
            r = await super().execute(spec, **kwargs)
            r.completion_tokens = r.output_tokens = None
            return r
    _, check, _, _ = await perform(client=NoOutputCount())
    assert point(check)['output_tokens_per_second'] is None
    assert point(check)['token_lengths']['short_short']['output_tokens']['median'] is None


async def test_calibration_variants_and_attempt_limit():
    class Miscalibrated(Client):
        async def execute(self, spec, **kwargs):
            r = await super().execute(spec, **kwargs)
            r.input_tokens = spec.requested_input_tokens // 2
            return r
    _, check, client, _ = await perform(configuration(load={'combinations': {'selected': ['short_short', 'short_long']}}), Miscalibrated())
    assert len(client.specs) == 6
    assert all(s.scenario.startswith('load_calibration_') and s.max_tokens == 1 for s in client.specs)
    assert {c['variant'] for c in check.metrics['calibrations']} == {'short_answer', 'long_output'}
    assert all(p['status'] == 'inconclusive' and 'not calibrated' in p['reason'] for p in check.metrics['load_types']['combinations']['points'])


async def test_interference_never_triggered_is_reported():
    c = configuration(request_timeout_seconds=.03, load={'combinations': {'enabled': False},
        'schedule': {'levels': [2]}, 'interference': {'enabled': True, 'trigger': 'after_chunks', 'trigger_chunks': 1000}})
    _, check, _, _ = await perform(c)
    p = point(check)
    assert p['interference']['probes_never_triggered'] > 0
    assert p['interference']['control']['matched_count'] == p['interference']['probe']['matched_count'] == 0
    assert p['status'] == 'inconclusive'


def test_matching_uses_short_decode_and_probe_prefill_intersection():
    def r(name, role, sub, start, first, last, chunks=2):
        return RequestRecord(id=name, target='local', fixture_id='x', scenario='x', check_ids=[], started_at='now',
            status='completed', valid=True, first_output_ms=first, last_output_ms=last, elapsed_ms=last+5,
            answer_chunks=chunks, timing_start_ms=start, max_stream_gap_ms=2,
            load_point={'role': role, 'sub_window': sub})
    short = r('short', 'short_stream', 0, 0, 10, 30)
    control = r('control', 'probe', 0, 15, 10, 40)
    probe = r('probe', 'probe', 1, 20, 20, 60)
    data = interference_measurements([short, control, probe], 2, 2)
    assert data['control']['matched_count'] == data['probe']['matched_count'] == 0
    assert data['ambiguous_short_streams'] == 1
    probe.timing_start_ms = 31
    assert interference_measurements([short, control, probe], 2, 2)['probe']['matched_count'] == 0
    short.answer_chunks = 1
    assert interference_measurements([short, control, probe], 2, 2)['buffered_stream']


async def test_recovery_reserves_load_points_and_later_checks(monkeypatch):
    from giraffe import runner
    from tests.test_runner import FakeClient
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    c = short_windows(configuration(checks=['recovery', 'concurrency'], max_requests=25, samples=2, sustained_seconds=.01,
                                  limits={'min_samples': 2, 'recovery_max_ratio': 2}))
    report = await run_suite(c)
    reserved = report.observations['targets']['local']['sustained_budget']['reserved_later']
    assert reserved >= minimum_reservation(c.concurrent_load) + c.samples
    assert any(r.load_point for r in report.requests)


async def test_real_stream_client_progress_and_cached_usage_without_sockets(monkeypatch):
    from giraffe.client import LLMClient
    payloads = []
    class Stream(httpx.AsyncByteStream):
        def __init__(self, payload):
            self.payload = payload
        async def __aiter__(self):
            payload = self.payload
            for text in ('K7P4', ' ', 'one', ' two'):
                await asyncio.sleep(.002)
                event = {'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]}
                yield ('data: ' + json.dumps(event) + '\n\n').encode()
            yield ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': len(payload['messages'][0]['content']) // 4,
                          'completion_tokens': payload['max_tokens'], 'prompt_tokens_details': {'cached_tokens': 0}}}) + '\n\n').encode()
            yield b'data: [DONE]\n\n'
    async def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream(payload))
    monkeypatch.setattr(LLMClient, '_create_client', lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    c = short_windows(configuration(request_options={'temperature': .7}, load={'combinations': {'enabled': False},
        'interference': {'enabled': True, 'trigger': 'after_chunks', 'trigger_chunks': 2}, 'schedule': {'levels': [2]}}))
    report = await run_suite(c)
    check = next(x for x in report.checks if x.id == 'concurrency')
    data = point(check)
    assert data['interference']['control']['matched_count'] > 0
    assert data['interference']['probe']['matched_count'] > 0
    assert all(p['temperature'] == .7 for p in payloads)
    assert all(r.cached_prompt_tokens == 0 for r in report.requests)
    assert all(r.answer_chunks >= 2 for r in report.requests)


async def test_nonstream_load_is_client_only_and_wrong_count_allowance_is_exact():
    run, check, _, _ = await perform(configuration(stream=False, load={'schedule': {'levels': [2]}}))
    p = point(check)
    assert p['status'] == 'inconclusive' and not p['load_qualified']
    assert p['concurrency_reason'] == 'client-delivered only; server overlap not evidenced'
    settings = run.config.concurrent_load.model_copy(deep=True)
    settings.pass_criteria.min_valid_responses = 5
    settings.pass_criteria.min_correct_percent = 80
    p.update(scored=1, correct=0, correct_percent=0)
    assert score_point(settings, p, [])['status'] == 'inconclusive'
    p.update(scored=2, correct=0, correct_percent=0)
    assert score_point(settings, p, [])['status'] == 'fail'


async def test_low_output_length_is_coverage_not_failure():
    class EarlyEnd(Client):
        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            record.completion_tokens = record.output_tokens = 1
            return record
    _, check, _, _ = await perform(configuration(load={'combinations': {'enabled': False}, 'stress': {'enabled': True, 'large_input': False}}), EarlyEnd())
    assert point(check)['status'] == 'inconclusive'
    assert point(check)['reason_code'] == 'insufficient_coverage'
    assert point(check)['coverage']['covered_valid_responses'] == 0


async def test_cancelling_executor_finalizes_inflight_and_unreached_points():
    class Paused(Client):
        def __init__(self):
            super().__init__()
            self.sent = asyncio.Event()
        async def execute(self, spec, **kwargs):
            if not spec.scenario.startswith('load_calibration_'):
                self.sent.set()
                await asyncio.Event().wait()
            return await super().execute(spec, **kwargs)
    c = short_windows(configuration(load={'schedule': {'levels': [2]}, 'combinations': {'selected': ['short_short', 'long_short']}}))
    run, client = _Run(c, None, None), Paused()
    task = asyncio.create_task(execute(run, c.targets[0], client))
    await asyncio.wait_for(client.sent.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    rows = result(run, c.targets[0]).metrics['load_types']['combinations']['points']
    assert rows[0]['cancelled'] == rows[0]['sent'] > 0
    assert rows[0]['error_percent'] == 0 and rows[0]['status'] == 'inconclusive'
    assert rows[1]['not_run'] and rows[1]['status'] == 'inconclusive'


async def test_partial_point_failure_is_scored_when_executor_is_interrupted():
    from giraffe.concurrent_load import test_points as points
    c = configuration()
    run = _Run(c, None, None)
    p = points(c.concurrent_load)[0]
    run.observations['local']['concurrent_load'] = {'load_types': {'combinations': {'title': 'Combinations', 'points': [p]}}}
    run.records.append(RequestRecord(id='error', target='local', fixture_id='x', scenario=p['id'], check_ids=['concurrency'],
        started_at='now', status='failed', error='HTTP 500', load_point={'point_id': p['id'], 'shape': 'short_short'}))
    check = result(run, c.targets[0])
    assert check.status == 'fail' and point(check)['failed'] == 1


async def test_calibration_failure_remains_failure_for_unreached_dependent_levels():
    _, check, _, _ = await perform(configuration(stop_after_errors=1, load={'schedule': {'levels': [1, 2]}}), Client(failure=True))
    rows = check.metrics['load_types']['combinations']['points']
    assert len(rows) == 2 and all(p['status'] == 'fail' and p['reason_code'] == 'calibration_failed' for p in rows)
