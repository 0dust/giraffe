import asyncio
import copy
import json
import math
import time
import uuid

import httpx
import pytest
from pydantic import ValidationError

from giraffe.load_test import (
    execute, interference_measurements, load_time_plan, minimum_reservation, result, assess_point, score_group,
)
from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_OPTION_FIELDS, LoadTest, RequestRecord, RunConfig, SHAPES
from giraffe.planning import request_plan
from giraffe.runner import _Run, run_suite
from giraffe.web import _configuration_errors, create_app


pytestmark = pytest.mark.usefixtures("local_tokenizer")


TARGET = {"name": "local", "url": "http://localhost:1/v1", "model": "test"}


def configuration(**values):
    # Compact fixture API; build the final load schema directly.
    settings = {"profile": {s: {"input_tokens": n, "output_tokens": o} for s, n, o in
                         [("short", 64, 8), ("medium", 96, 16), ("long", 128, 32)]},
        "workloads": {"combinations": {"selected": ["short_short"]}},
        "patterns": {"concurrent": {"levels": [1]}, "constant": {"enabled": False}},
        "schedule": {"duration_seconds": 1, "finish_seconds": .1},
        "pass_criteria": {"min_valid_responses": 2}}
    extra = values.pop("load", {})
    def merge(dst, src):
        for key, value in src.items():
            if isinstance(value, dict):
                merge(dst.setdefault(key, {}), value)
            else:
                dst[key] = value
    for key in ("combinations", "mixed", "stress", "interference"):
        if key in extra:
            entry = dict(extra.pop(key))
            if key == "mixed":
                if "seed" in entry:
                    settings["patterns"]["poisson"] = {"seed": entry.pop("seed")}
                if "shares" in entry:
                    entry.update(share_mode="custom", selected=[s for s, v in entry["shares"].items() if v > 0])
            if key == "stress":
                entry["cases"] = (["long_short"] if entry.pop("large_input", True) else []) + (["short_long"] if entry.pop("large_output", True) else [])
            if key == "interference":
                if entry.get("trigger") == "after_chunks":
                    entry["trigger"] = "after_five_chunks"
                entry.pop("trigger_chunks", None)
            settings["workloads"][key] = entry
    if "levels" in extra.get("schedule", {}):
        settings["patterns"]["concurrent"]["levels"] = extra["schedule"].pop("levels")
    old_criteria = {"response_p95_ms": "max_completion_ms", "first_output_p95_ms": "max_first_answer_ms"}
    criteria = extra.get("pass_criteria", {})
    for old, new in old_criteria.items():
        if old in criteria:
            criteria[new] = criteria.pop(old)
    merge(settings, extra)
    if "request_timeout_seconds" in values:
        settings["request_timeout_seconds"] = values["request_timeout_seconds"]
    if "checks" in values:
        values["checks"] = list(dict.fromkeys("load" if c in {"concurrency", "capacity", "arrivals"} else c for c in values["checks"]))
    return RunConfig.model_validate({"targets": [TARGET], "checks": ["load"], "max_requests": 500,
        "load_test": settings, **values})


def short_windows(config, seconds=.2):
    for settings in [config.load_test, *[v.settings for v in config.load_test.migrated_tests]]:
        settings.schedule.duration_seconds = seconds
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
            for _ in range(5):
                await asyncio.sleep(.004 if index % 2 else .007)
                if first_output_event is not None and hasattr(first_output_event, "on_answer_chunk"):
                    first_output_event.on_answer_chunk()
            record.elapsed_ms = (time.monotonic() - start) * 1000
            record.last_output_ms = record.elapsed_ms - 1
            record.stream_end_ms = record.elapsed_ms
            record.answer_chunks = 1 if self.buffered else 6
            record.answer_reads = record.answer_chunks
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
    return check.metrics["points"][0]


def test_defaults_and_planning_numbers():
    settings = LoadTest()
    assert settings.profile.long.output_tokens == 512
    assert settings.schedule.duration_seconds == 15
    assert not hasattr(settings.schedule, "finish_seconds")
    default = load_time_plan(settings)
    assert default['scheduled_tests'] == 12 and default['configured_load_seconds'] == 180
    assert default['time_with_allowances_seconds'] == 180 + 12 * settings.request_timeout_seconds
    assert CHECK_OPTION_FIELDS['load'] == set()
    assert CHECK_LIMIT_FIELDS['load'] == set()
    c = configuration(concurrency=8, max_duration_seconds=1000,
        load={'combinations': {'selected': list(SHAPES)}, 'schedule': {'levels': [1, 4, 8], 'duration_seconds': 30}})
    plan = request_plan(c)
    assert plan['configured_load_seconds'] == 810
    assert plan['load_test']['workloads']['combinations']['test_points'] == 27
    assert next(r for r in plan['rows'] if r['id'] == 'load_combinations')['requests_per_target'] is None
    assert plan['request_warnings'][0]['fields'] == ['max_requests']
    assert plan['request_warnings'][0]['kind'] == 'adaptive_risk'
    i = configuration(load={'combinations': {'enabled': False}, 'interference': {'enabled': True}, 'schedule': {'levels': [2], 'duration_seconds': 30}})
    assert request_plan(i)['configured_load_seconds'] == 60
    assert minimum_reservation(i.load_test) == 3


@pytest.mark.parametrize("path,value,fields", [
    ("patterns.concurrent.levels", [8], ["load_test.patterns.concurrent.levels", "concurrency"]),
    ("profile.long.output_tokens", 65537, ["load_test.profile.long.output_tokens"]),
    ("workloads.combinations.selected", [], ["load_test.workloads.combinations.selected"]),
    ("profile.medium.input_tokens", 1, ["load_test.profile.medium.input_tokens"]),
    ("profile.long.input_tokens", 131073, ["load_test.profile.long.input_tokens"]),
    ("profile.short.output_tokens", 0, ["load_test.profile.short.output_tokens"]),
    ("schedule.duration_seconds", 3601, ["load_test.schedule.duration_seconds"]),
    ("patterns.concurrent.levels", [2, 2], ["load_test.patterns.concurrent.levels"]),
    ("workloads.combinations.enabled", False, ["load_test.workloads.combinations.enabled"]),
])
def test_structured_errors(path, value, fields):
    values = configuration(concurrency=4).model_dump()
    node = values["load_test"]
    parts = path.split(".")
    for key in parts[:-1]:
        node = node[key]
    node[parts[-1]] = value
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    error = _configuration_errors(exc.value.errors(), values)[0]
    assert error["fields"] == fields


@pytest.mark.parametrize("updates,fields", [
    ({"mixed": {"enabled": True, "shares": {"short_short": 99}}}, ["load_test.mixed.shares.short_short"]),
    ({"stress": {"enabled": True, "large_input": False, "large_output": False}}, ["load_test.stress.large_input"]),
    ({"interference": {"enabled": True}}, ["load_test.patterns.concurrent.levels", "load_test.workloads.interference.enabled"]),
])
def test_other_structured_errors(updates, fields):
    values = configuration().model_dump()
    if 'mixed' in updates:
        values['load_test']['workloads']['mixed'].update(enabled=True, selected=['short_short','long_short'], share_mode='custom', shares={'short_short':99, 'long_short':1.1})
        fields = ['load_test.workloads.mixed.shares', 'load_test.workloads.mixed.share_mode']
    elif 'stress' in updates:
        values['load_test']['workloads']['stress'].update(enabled=True, cases=[])
        fields = ['load_test.workloads.stress.cases']
    else:
        values['load_test']['workloads']['interference']['enabled'] = True
        fields = ['load_test.patterns.concurrent.levels']
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    assert _configuration_errors(exc.value.errors(), values)[0]['fields'] == fields


@pytest.mark.parametrize("which", ["stream", "overlap", "context"])
def test_cross_field_errors(which):
    values = configuration().model_dump()
    expected = []
    if which == "stream":
        values["stream"] = False
        values["load_test"]["patterns"]["concurrent"]["levels"] = [2]
        values["load_test"]["workloads"]["interference"]["enabled"] = True
        expected = ["load_test.workloads.interference.enabled", "stream"]
    if which == "overlap":
        values.update(overlap_models=True, targets=[TARGET, {**TARGET, "name": "other"}], concurrency=3)
        values["load_test"]["patterns"]["concurrent"]["levels"] = [2]
        expected = ["load_test.patterns.concurrent.levels", "concurrency"]
    if which == "context":
        values["targets"][0]["deployment"]["serving"]["context_limit"] = 50
        expected = ["load_test.profile.short.input_tokens", "load_test.profile.short.output_tokens"]
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    assert _configuration_errors(exc.value.errors(), values)[0]["fields"] == expected


async def test_individual_refill_duration_drain_nonce_and_conditions():
    run, check, client, events = await perform(configuration(load={"schedule": {"levels": [2]}}))
    p = point(check)
    assert p["dispatched"] > 2 and p["delivered_window_seconds"] >= .199
    assert client.started[2] < client.finished[1]  # faster slot refills before slow slot completes
    assert p["drain_seconds"] > 0 and p["groups"][0]["completed"] == p["dispatched"]
    assert p["client_mean_inflight"] >= 1.8 and p["server_evidenced_concurrency"] == 2
    assert check.status == "pass" and p["groups"][0]["completed_requests_per_second"] > 0
    assert len(set(client.prompts)) == len(client.prompts)
    assert len({len(p) for p in client.prompts}) == 1
    assert all(r.load_point and r.requested_input_tokens and r.workload["input_preparation"]["method"] == "local_tokenizer" for r in run.records)
    assert {"condition_planned", "condition_started", "condition_finished"} <= {e["event"] for e in events}
    assert all(r.condition_id == p["id"] for r in run.records if r.load_point["type"] != "calibration")


@pytest.mark.parametrize("client,code", [(Client(serial=True), "load_not_delivered"), (Client(cached=True), "cached_input"),
    (Client(wrong=True), "correctness"), (Client(cap_violation=True), "output_cap"), (Client(reasoning=True), "insufficient_responses"),
    (Client(failure=True), "error_rate")])
async def test_evidence_and_failure_classification(client, code):
    load = {"schedule": {"levels": [2]}, "combinations": {"selected": ["long_short"]}}
    _, check, _, _ = await perform(configuration(load=load), client)
    assert point(check)["reason_code"] == code
    if code in {"correctness", "output_cap", "error_rate"}:
        assert check.status == "fail"
    else:
        assert check.status == "inconclusive"
    if code == "insufficient_responses":
        assert point(check)["reasoning_budget_exhausted"] == point(check)["dispatched"]
        assert point(check)["failed"] == 0


async def test_unknown_tokens_and_stress_never_report_zero():
    _, check, _, _ = await perform(configuration(load={"combinations": {"enabled": False}, "stress": {"enabled": True, "large_output": False}}), Client(unknown=True))
    p = point(check)
    assert p["status"] == "inconclusive" and p["reason_code"] == "input_not_verified"
    assert p["groups"][0]["lengths"]["input_tokens"] == {"median": None, "min": None, "max": None}
    assert p["groups"][0]["aggregate_output_tokens_per_second"] is not None
    assert check.metrics["calibrations"][0]["status"] == "calibrated"
    assert check.metrics["calibrations"][0]["method"] == "local_tokenizer"


async def test_mixed_seed_and_scenarios():
    c = configuration(load={"combinations": {"enabled": False}, "schedule": {"levels": [2]}, "mixed": {"enabled": True, "seed": 7,
        "shares": {"short_short": 60, "long_short": 40}}})
    r1, a, _, _ = await perform(c)
    r2, b, _, _ = await perform(c)
    # Seed controls dispatched request choices. Concurrent completions can reorder.
    def seq(run):
        rows = sorted((r for r in run.records if r.load_point["type"] == "mixed"), key=lambda r: r.dispatch_ms)
        return [r.load_point["shape"] for r in rows]
    assert len(seq(r1)) >= 8 and len(seq(r2)) >= 8
    assert seq(r1)[:8] == seq(r2)[:8]
    assert a.metrics["seed"] == 7
    assert set(point(a)["observed_mix"]) == {"short_short", "long_short"}
    assert all(r.scenario == point(a)["id"] and r.condition_id == point(a)["id"] for r in r1.records if r.load_point["type"] == "mixed")
    assert math.isclose(sum(x["observed_percent"] for x in point(a)["observed_mix"].values()), 100)


@pytest.mark.parametrize("cap,threshold", [(512, 410), (128, 103)])
async def test_stress_ceil_and_coverage_alone_does_not_pass(cap, threshold):
    c = configuration(max_output_tokens=cap, load={"profile": {s: {"input_tokens": n, "output_tokens": o} for s, n, o in
        [("short", 64, 8), ("medium", 96, 16), ("long", 128, cap)]}, "combinations": {"enabled": False},
        "stress": {"enabled": True, "large_input": False}, "pass_criteria": {"response_p95_ms": 1}})
    _, check, _, _ = await perform(c)
    assert point(check)["groups"][0]["coverage"]["threshold_tokens"] == threshold
    assert point(check)["groups"][0]["coverage"]["output_covered"] >= 2
    assert check.status == "fail" and point(check)["reason_code"] == "timing"


async def test_interference_alternates_and_triggers_after_chunks():
    # Issue 45 replaces A/B alternation with two independent full windows.
    c = configuration(load={'combinations': {'enabled': False}, 'interference': {'enabled': True, 'trigger': 'after_five_chunks'}, 'schedule': {'levels': [2]}})
    run, check, _, _ = await perform(c)
    p = point(check)
    assert [w['phase'] for w in p['measurement_windows']] == ['baseline', 'probe']
    assert all(w['delivered_window_seconds'] >= .199 for w in p['measurement_windows'])
    probes = [r for r in run.records if r.load_point.get('role') == 'probe']
    assert probes and all(r.load_point['phase'] == 'probe' and r.load_point['shape'] == 'long_short' for r in probes)
    assert p['interference']['baseline']['matched_count'] and p['interference']['probe']['matched_count']
    assert p['interference']['actual_overlap_request_ids']


@pytest.mark.parametrize("slowdown,limit,status", [(2, 1.5, "fail"), (1.1, 1.5, "pass"), (1.1, None, "inconclusive")])
def test_interference_slowdown_scoring(slowdown, limit, status, monkeypatch):
    from giraffe import runner
    from tests.test_review26_phase4_accuracy import run_virtual

    # Slowdown now compares matched completion times in independent windows.
    c = configuration(load={'combinations': {'enabled': False}, 'interference': {'enabled': True}, 'schedule': {'levels': [2]}})
    if limit is None:
        with pytest.raises(ValidationError):
            type(c.load_test.workloads.interference)(max_slowdown=None)
        return
    c.load_test.workloads.interference.max_slowdown = limit

    async def scenario():
        # Keep synthetic client timestamps and scheduling on the same clock.
        # Real wall-clock jitter must not change this scoring-only assertion.
        monkeypatch.setattr("tests.test_concurrent_load.time", runner.time)
        run, check, _, _ = await perform(c)
        p = point(check)
        rows = [r for r in run.records if r.load_point.get('point_id') == p['id']]
        # Change completion measurements only; retain actual overlap and occupancy.
        baseline = p['interference']['baseline']['completion_median_ms']
        for r in rows:
            if r.load_point.get('phase') == 'probe' and r.load_point.get('role') == 'short_stream':
                r.elapsed_ms = baseline * slowdown
        assess_point(run, c.targets[0], c.load_test, p, rows)
        assert p['interference']['slowdown'] == pytest.approx(slowdown)
        assert p['status'] == status, (p['reason'], p['interference'])

    run_virtual(monkeypatch, scenario())


async def test_budget_cut_short_failures_precede_evidence_and_later_reservation():
    c = configuration(max_requests=8, checks=['load','consistency'], consistency={'repetitions':2,'levels':[1],'shapes':['short'],'prompts_per_shape':2})
    run, check, _, _ = await perform(c)
    p = point(check)
    assert p['request_allowance'] == 4 and p['reserved_later_requests'] == 4
    assert run.remaining('local') == 4 and p['cut_short'] and check.status == 'inconclusive'
    for kind in ('error', 'wrong', 'timing'):
        g = copy.deepcopy(p['groups'][0])
        if kind == 'error':
            g['error_percent'] = 100
        elif kind == 'wrong':
            g.update(scored=1, correct=0, correct_percent=0)
        else:
            g['timing_violations'] = ['response']
        assert score_group(c.load_test, p, g, [])['status'] == 'fail'
    # Enough samples never erase a truncated sending window under issue 45.
    g = copy.deepcopy(p['groups'][0])
    g['valid'] = 100
    assert score_group(c.load_test, p, g, [])['status'] == 'inconclusive'


@pytest.mark.parametrize('reported', [False, True])
async def test_known_runtime_context_not_sent(reported, monkeypatch):
    from giraffe import deployment
    from giraffe.load_test import context_window
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
    rows = check.metrics["points"]
    assert rows[0]["status"] == "fail" and rows[0]["reason_code"] == "error_rate"
    assert rows[1]["not_run"] and rows[1]["status"] == "inconclusive"
    assert run.errors == 1 and check.status == "fail"


@pytest.mark.parametrize("endpoint", ["draft", "validate", "preview", "plan"])
async def test_migration_metadata_endpoints(endpoint, tmp_path):
    old = {"targets": [TARGET], "checks": ["buckets"], "buckets": {"levels": [1], "samples_per_bucket": 7}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787") as client:
        response = await client.post(f"/api/config/{endpoint}", json=old)
    assert response.status_code == 200, response.text
    data = response.json()
    assert any("samples_per_bucket=7" in n for n in data["migration_notes"])
    assert "concurrent_load" in data["config"] and "buckets" not in data["config"]
    assert not RunConfig.model_validate(data["config"])._migration_notes


async def test_suite_wires_new_check_and_manifest_notes(monkeypatch):
    from giraffe import runner
    class ContextClient(Client):
        def __init__(self, target, config, *, max_connections=None):
            super().__init__()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(runner, 'LLMClient', ContextClient)
    c = short_windows(configuration())
    c._migration_notes = ['saved migration note']
    report = await run_suite(c)
    check = next(x for x in report.checks if x.id == 'load')
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
    check = next(x for x in report.checks if x.id == 'load')
    point = check.metrics['points'][0]
    assert point['dispatched'] > 2
    assert len(point['request_ids']) == point['dispatched']
    assert point['not_sent'] == point['cancelled'] == 0
    assert all(not r.scenario.startswith('load_calibration_') for r in report.requests)


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
    assert p['cancelled'] == 1 and p['timed_out'] == 0 and p['groups'][0]['error_percent'] == 0
    assert p['status'] == 'inconclusive'


async def test_missing_completion_counts_leave_throughput_unknown():
    class NoOutputCount(Client):
        async def execute(self, spec, **kwargs):
            r = await super().execute(spec, **kwargs)
            r.completion_tokens = r.output_tokens = None
            return r
    _, check, _, _ = await perform(client=NoOutputCount())
    assert point(check)['groups'][0]['aggregate_output_tokens_per_second'] is None
    assert point(check)['groups'][0]['lengths']['output_tokens']['median'] is None


async def test_local_preparation_variants_do_not_send_inference_probes():
    class Miscalibrated(Client):
        async def execute(self, spec, **kwargs):
            r = await super().execute(spec, **kwargs)
            r.input_tokens = spec.requested_input_tokens // 2
            return r
    _, check, client, _ = await perform(configuration(load={'combinations': {'selected': ['short_short', 'short_long']}}), Miscalibrated())
    assert client.specs
    assert all(not s.scenario.startswith('load_calibration_') and s.max_tokens > 1 for s in client.specs)
    assert {c['variant'] for c in check.metrics['calibrations']} == {'short_answer', 'long_output'}
    assert all(c['status'] == 'calibrated' and not c['request_ids'] for c in check.metrics['calibrations'])


async def test_interference_never_triggered_is_reported():
    c = configuration(load={'combinations':{'enabled':False}, 'schedule':{'levels':[2]}, 'interference':{'enabled':True,'trigger':'after_five_chunks'}})
    class FourChunks(Client):
        async def execute(self, spec, first_output_event=None):
            # Buffer progress until return: the fifth answer chunk never arrives.
            return await super().execute(spec, None)
    _, check, _, _ = await perform(c, FourChunks())
    p = point(check)
    assert p['interference']['triggered'] == 0
    assert p['interference']['baseline']['matched_count'] == p['interference']['probe']['matched_count'] == 0
    assert p['status'] == 'inconclusive'


def test_matching_uses_short_decode_and_probe_prefill_intersection():
    from tests.test_load_executor_regressions import record
    baseline = record('baseline', start=0, first=10, last=30, load_point={'role':'short_stream','phase':'baseline'})
    short = record('short', start=100, first=10, last=30, load_point={'role':'short_stream','phase':'probe'})
    probe = record('probe', 'probe', start=115, first=10, last=40, load_point={'role':'probe','phase':'probe'})
    windows = [{'phase':'baseline','sending_start_ms':0}, {'phase':'probe','sending_start_ms':100,'triggered':1}]
    data = interference_measurements([baseline, short, probe], windows)
    assert data['baseline']['matched_count'] == data['probe']['matched_count'] == 1
    assert data['actual_overlap_request_ids'] == ['short']
    probe.timing_start_ms = 131
    assert interference_measurements([baseline,short,probe], windows)['probe']['matched_count'] == 0
    probe.timing_start_ms = 115
    short.answer_chunks = 1
    assert interference_measurements([baseline,short,probe], windows)['probe']['matched_count'] == 0


async def test_recovery_reserves_load_points_and_later_checks(monkeypatch):
    from giraffe import runner
    from tests.test_runner import FakeClient
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    c = short_windows(configuration(checks=['recovery', 'concurrency'], max_requests=25, samples=2, sustained_seconds=.01,
                                  limits={'min_samples': 2, 'recovery_max_ratio': 2}))
    report = await run_suite(c)
    reserved = report.observations['targets']['local']['sustained_budget']['reserved_later']
    assert reserved >= minimum_reservation(c.load_test) + c.samples
    assert any(r.load_point for r in report.requests)


async def test_real_stream_client_progress_and_cached_usage_without_sockets(monkeypatch):
    from giraffe.client import LLMClient
    payloads = []
    class Stream(httpx.AsyncByteStream):
        def __init__(self, payload):
            self.payload = payload
        async def __aiter__(self):
            payload = self.payload
            for text in ('K7P4', ' ', 'one', ' two', ' three', ' four', ' five'):
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
    check = next(x for x in report.checks if x.id == 'load')
    data = point(check)
    assert data['interference']['baseline']['matched_count'] > 0
    assert data['interference']['probe']['matched_count'] > 0
    assert all(p['temperature'] == .7 for p in payloads)
    assert all(r.cached_prompt_tokens == 0 for r in report.requests)
    assert all(r.answer_chunks >= 2 for r in report.requests)


async def test_nonstream_load_is_client_only_and_wrong_count_allowance_is_exact():
    run, check, _, _ = await perform(configuration(stream=False, load={'schedule':{'levels':[2]}}))
    p = point(check)
    assert p['status'] == 'inconclusive' and not p['load_qualified']
    assert p['server_evidenced_concurrency'] == 0
    settings = run.config.load_test.model_copy(deep=True)
    settings.pass_criteria.min_correct_percent = 80
    g = copy.deepcopy(p['groups'][0])
    g.update(scored=5,correct=4,correct_percent=80)
    assert score_group(settings,p,g,[])['reason_code'] == 'load_not_delivered'
    g.update(correct=3,correct_percent=60)
    assert score_group(settings,p,g,[])['status'] == 'fail'


async def test_low_output_length_is_coverage_not_failure():
    class EarlyEnd(Client):
        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            record.completion_tokens = record.output_tokens = 1
            return record
    _, check, _, _ = await perform(configuration(load={'combinations': {'enabled': False}, 'stress': {'enabled': True, 'large_input': False}}), EarlyEnd())
    assert point(check)['status'] == 'inconclusive'
    assert point(check)['reason_code'] == 'insufficient_coverage'
    assert point(check)["groups"][0]["coverage"]['output_covered'] == 0


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
    run.records.append(RequestRecord(id='quiet', target='local', fixture_id='pack:quiet',
        scenario='pack_control', check_ids=['correctness'], started_at='now',
        status='completed', valid=True, score=True, load_point=None))
    task = asyncio.create_task(execute(run, c.targets[0], client))
    await asyncio.wait_for(client.sent.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    rows = result(run, c.targets[0]).metrics['points']
    assert rows[0]['cancelled'] == rows[0]['dispatched'] > 0
    assert rows[0]['groups'][0]['error_percent'] == 0 and rows[0]['status'] == 'inconclusive'
    assert rows[1]['not_run'] and rows[1]['status'] == 'inconclusive'
    assert run.records[0].id == 'quiet' and run.records[0].load_point is None


async def test_partial_point_failure_is_scored_when_executor_is_interrupted():
    from giraffe.load_test import test_points as points
    c = configuration()
    run = _Run(c, None, None)
    p = points(c.load_test)[0]
    run.observations['local']['load_test'] = {'points': [p]}
    run.records.append(RequestRecord(id='error', target='local', fixture_id='x', scenario=p['id'], check_ids=['concurrency'],
        started_at='now', status='failed', error='HTTP 500', load_point={'point_id': p['id'], 'shape': 'short_short'}))
    check = result(run, c.targets[0])
    assert check.status == 'fail' and point(check)['failed'] == 1


async def test_endpoint_failure_does_not_mislabel_unreached_levels_as_calibration_failures():
    _, check, _, _ = await perform(configuration(stop_after_errors=1, load={'schedule': {'levels': [1, 2]}}), Client(failure=True))
    rows = check.metrics['points']
    assert len(rows) == 2
    assert rows[0]['status'] == 'fail' and rows[0]['reason_code'] == 'error_rate'
    assert rows[1]['not_run'] and rows[1]['status'] == 'inconclusive'


# Fake inference clients in this module use the matching local tokenizer.
