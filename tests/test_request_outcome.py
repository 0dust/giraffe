"""One derived request outcome and one error denominator across workloads."""

import json

import httpx
import pytest

from giraffe import runner, states
from giraffe.client import LLMClient
from giraffe.models import RequestRecord, RequestSpec, RunConfig, RunReport, Target
from giraffe.request_outcome import count_outcomes, request_outcome


def record(**updates):
    return RequestRecord(**{
        "id": "r", "target": "service", "fixture_id": "answer", "scenario": "initial",
        "check_ids": ["serving"], "started_at": "2026-11-27T00:00:00Z",
        "status": "completed", "valid": True, **updates,
    })


BUDGET = {"valid": False, "finish_reason": "length", "reasoning": "thinking",
          "error": "reasoning exhausted the output budget before an answer"}


@pytest.mark.parametrize("updates,expected", [
    ({"not_dispatched": True, "status": "failed"}, "not_dispatched"),
    ({"not_dispatched": True, "intentional": True, "status": "cancelled"}, "not_dispatched"),
    ({"intentional": True, "status": "timeout"}, "intentional_stop"),
    ({"intentional": True, "status": "cancelled"}, "intentional_stop"),
    ({"status": "cancelled"}, "cancelled"),
    ({"status": "timeout"}, "timeout"),
    (BUDGET, "budget_exhausted"),
    ({**BUDGET, "status": "failed", "error": "response contained no useful answer output"}, "budget_exhausted"),
    ({"status": "failed", "valid": False, "error": "request failed (ConnectError)"}, "failed"),
    ({"valid": False}, "invalid"),
    ({}, "ok"),
    ({"score": False}, "ok"),
])
def test_derivation_and_precedence(updates, expected):
    assert request_outcome(record(**updates)) == expected


@pytest.mark.parametrize("reason", [
    "user_stop", "run_duration_limit", "memory_limit", "error_threshold", "endpoint_unresponsive",
    "external_cancellation", "workload_cancelled", "generation_chunk_limit", "intentional_cancellation",
    "intentional_cancellation_deadline", "intentional_deadline", None,
])
def test_cancellation_outcome_uses_intentional_flag_not_reason(reason):
    cancelled = record(status="cancelled", termination_reason=reason)
    assert request_outcome(cancelled) == "cancelled"
    cancelled.intentional = True
    assert request_outcome(cancelled) == "intentional_stop"


def test_count_outcomes_partitions_records_and_error_denominator():
    records = [record(not_dispatched=True), record(intentional=True), record(status="cancelled"),
               record(status="timeout"), record(**BUDGET), record(status="failed"),
               record(valid=False), record()]
    counts = count_outcomes(iter(records))
    assert {name: counts[name] for name in states.REQUEST_OUTCOMES} == dict.fromkeys(states.REQUEST_OUTCOMES, 1)
    assert counts["errors"] == 3
    assert counts["evaluated"] == 4
    assert counts["error_rate"] == .75
    assert sum(counts[name] for name in states.REQUEST_OUTCOMES) == len(records)
    assert count_outcomes([])["error_rate"] is None
    assert count_outcomes(records[:3])["evaluated"] == 0


def test_computed_outcome_is_serialized_and_ignores_saved_input():
    value = record()
    assert "outcome" not in RequestRecord.model_fields
    assert "outcome" not in RequestRecord.model_json_schema()["properties"]
    assert value.model_dump()["outcome"] == "ok"
    assert RequestRecord.model_validate({**value.model_dump(), "outcome": "failed"}).outcome == "ok"
    old_record = value.model_dump(exclude={"outcome", "not_dispatched"})
    assert RequestRecord.model_validate(old_record).outcome == "ok"
    report = RunReport(run_id="run", started_at=value.started_at, finished_at=value.started_at,
                       overall="pass", manifest={}, requests=[value])
    from giraffe.reporting import _report_json_chunks

    saved = json.loads("".join(_report_json_chunks(report)))
    assert saved["requests"][0]["outcome"] == "ok"
    assert RunReport.model_validate(saved).requests[0].outcome == "ok"


@pytest.mark.parametrize("fault,expected", [("prepare", "not_dispatched"), ("connect", "failed")])
async def test_real_client_preparation_failure_vs_connect_error(monkeypatch, fault, expected):
    target = Target(name="service", url="http://service", model="test")
    config = RunConfig(targets=[target], checks=["serving"])
    original = httpx.AsyncClient

    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(refuse), **kw))
    async with LLMClient(target, config) as client:
        if fault == "prepare":
            monkeypatch.setattr(client._client, "build_request", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad payload")))
        value = await client.execute(RequestSpec(fixture_id="answer", scenario="initial", check_ids=["serving"],
                                                messages=[{"role": "user", "content": "answer"}]))
    assert value.outcome == expected
    assert value.not_dispatched is (fault == "prepare")
    # A missing runner timestamp is also normal for standalone client errors.
    assert value.dispatch_ms is None


async def test_request_completed_carries_computed_outcome():
    target = Target(name="service", url="http://service", model="test")
    config = RunConfig(targets=[target], checks=["serving"])
    events = []
    run = runner._Run(config, events.append, None)

    class Client:
        async def execute(self, spec, **kwargs):
            return record(output="answer")

    value = await run.request(target, Client(), RequestSpec(
        fixture_id="answer", scenario="initial", check_ids=["serving"],
        messages=[{"role": "user", "content": "answer"}]))
    completed = next(event for event in events if event["event"] == "request_completed")
    assert completed["request"]["outcome"] == value.outcome == "ok"


@pytest.mark.parametrize("status,error", [
    ("completed", "reasoning exhausted the output budget before an answer"),
    ("failed", "response contained no useful answer output"),
])
async def test_live_request_reconstruction_preserves_budget_outcome_without_content(status, error):
    from collections import OrderedDict

    from giraffe.web import _track_live_request

    target = Target(name="service", url="http://service", model="test")
    events = []
    run = runner._Run(RunConfig(targets=[target], checks=["serving"]), events.append, None)

    class Client:
        async def execute(self, spec, **kwargs):
            return record(status=status, valid=False, finish_reason="length", error=error,
                          output="<think>private output reasoning</think>", reasoning="private reasoning")

    value = await run.request(target, Client(), RequestSpec(
        fixture_id="answer", scenario="initial", check_ids=["serving"],
        messages=[{"role": "user", "content": "answer"}]))
    completed = next(event for event in events if event["event"] == "request_completed")
    assert completed["request"]["outcome"] == value.outcome == "budget_exhausted"
    assert not {"output", "reasoning"} & completed["request"].keys()
    assert "private" not in json.dumps(completed["request"])
    job = {"requests": OrderedDict(), "live_request_count": 0, "evicted_running": set(),
           "live_request_summary": {"evicted": 0, "through_slot": None, "statuses": {}}}
    _track_live_request(job, completed, None)
    # Match the live performance report's field filter: computed outcomes are not inputs.
    rows = [{key: value for key, value in row.items()
             if key in RequestRecord.model_fields and value is not None}
            for row in job["requests"].values()]
    live = RunReport.model_validate({
        "run_id": "live", "started_at": value.started_at, "finished_at": value.started_at,
        "overall": "inconclusive", "manifest": {}, "requests": rows,
    }, context={"metadata_only": True})
    assert live.requests[0].outcome == "budget_exhausted"
    assert runner._stats(live.requests)["evaluated"] == 0
    assert runner._stats(live.requests)["error_rate"] is None


def test_runner_counting_and_deliberate_live_failure_rules():
    values = [record(), record(valid=False), record(status="cancelled"), record(**BUDGET),
              record(status="timeout", intentional=True), record(status="failed", not_dispatched=True)]
    stats = runner._stats(values)
    assert stats["error_rate"] == .5
    assert stats["failed"] == 1
    assert stats["completed"] == 1
    assert stats["intentional_stop"] == stats["not_dispatched"] == 1
    assert not runner._serving_failure(record(status="timeout"), "load_concurrent")
    assert not runner._serving_failure(record(status="failed", http_status=400, error="maximum context length"), "context")
    assert runner._request_failed(record(score=False))
    assert not runner._request_failed(record(score=False, scenario="deadline_probe"))
    assert not runner._request_failed(record(**BUDGET))


async def test_runner_preparation_failure_marks_non_dispatch():
    target = Target(name="service", url="http://service", model="test")
    events = []
    run = runner._Run(RunConfig(targets=[target], checks=["serving"]), events.append, None)

    class Client:
        _supports_dispatch_guard = True

        async def execute(self, spec, **kwargs):
            raise ValueError("request cannot be prepared")

    value = await run.request(target, Client(), RequestSpec(
        fixture_id="answer", scenario="traffic_stage_0", check_ids=["serving"],
        messages=[{"role": "user", "content": "answer"}]),
        prepared_admission=True, admission_deadline=run.deadline)
    assert value.outcome == "not_dispatched"
    assert value.not_dispatched and value.dispatch_ms is None
    assert runner._stats([value])["error_rate"] is None
    completed = next(event for event in events if event["event"] == "request_completed")
    assert completed["request"]["outcome"] == "not_dispatched"
    assert completed["request"]["not_dispatched"] is True


@pytest.mark.parametrize("override,early_timeout,expected", [
    ("long_timeout", True, "timeout"),
    ("long_deadline", True, "timeout"),
    ("long_timeout", False, "timeout"),
    ("long_deadline", False, "timeout"),
    ("short_timeout", True, "timeout"),
    ("short_timeout", False, "intentional_stop"),
    ("short_deadline", False, "intentional_stop"),
])
async def test_override_timeout_is_intentional_only_when_applied_deadline_expires(override, early_timeout, expected):
    import asyncio
    import time

    target = Target(name="service", url="http://service", model="test")
    events = []
    run = runner._Run(RunConfig(targets=[target], checks=["serving"], request_timeout_seconds=1),
                      events.append, None)

    class Client:
        async def execute(self, spec, **kwargs):
            if not early_timeout:
                await asyncio.sleep(10)
            return record(status="timeout", valid=False, error="request timeout (ReadTimeout)")

    seconds = .01 if override.startswith("short") else 2
    arguments = ({"timeout_override": seconds} if override.endswith("timeout")
                 else {"deadline_override": time.monotonic() + seconds})
    value = await run.request(target, Client(), RequestSpec(
        fixture_id="answer", scenario="initial", check_ids=["serving"],
        messages=[{"role": "user", "content": "answer"}]), **arguments)
    assert value.outcome == expected
    assert value.termination_reason == ("intentional_deadline" if expected == "intentional_stop" else "request_timeout")
    assert runner._stats([value])["error_rate"] == (None if expected == "intentional_stop" else 1)


@pytest.mark.parametrize("limit", ["chunks", "deadline"])
async def test_client_designed_stops_declare_intentional_outcome(monkeypatch, limit):
    from tests.test_client import event, setup_client, spec

    client, response, _ = setup_client(monkeypatch, [event("answer"), .2])
    options = {"cancel_after_chunks": 1} if limit == "chunks" else {"cancel_after_ms": 10}
    async with client:
        value = await client.execute(spec(**options))
    assert value.status == "cancelled" and response.closed
    assert value.intentional and not value.not_dispatched
    assert value.outcome == "intentional_stop"


@pytest.mark.parametrize("scenario", ["initial", "warm"])
async def test_surrogate_preparation_failures_prevent_any_check_pass(monkeypatch, scenario):
    from giraffe.request_outcome import preparation_failure_reason

    target = Target(name="service", url="http://service", model="test")
    run = runner._Run(RunConfig(targets=[target], checks=["serving", "first_output", "access"],
                               stop_after_errors=10), None, None)
    original = httpx.AsyncClient
    sent = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200)), **kw))
    async with LLMClient(target, run.config) as client:
        for _ in range(3):
            await run.request(target, client, RequestSpec(
                fixture_id="answer", scenario=scenario, check_ids=["serving", "first_output", "access"],
                messages=[{"role": "user", "content": "bad \ud800"}]))
    assert not sent
    assert run.errors == 3
    assert all(value.outcome == "not_dispatched" for value in run.records)
    checks = runner._checks(run, target)
    assert not any(check.status == "pass" for check in checks)
    for check in checks:
        if check.id in run.config.checks:
            assert check.status == "inconclusive"
            if check.id == "serving":
                assert check.summary == preparation_failure_reason(run.records)
    serving = next(check for check in checks if check.id == "serving")
    assert serving.metrics["error_rate"] is None


def test_real_server_failure_wins_over_preparation_inconclusive():
    target = Target(name="service", url="http://service", model="test")
    run = runner._Run(RunConfig(targets=[target], checks=["serving"]), None, None)
    run.records = [record(id="local", status="failed", valid=False, not_dispatched=True),
                   record(id="server", status="failed", valid=False, http_status=500)]
    serving = next(check for check in runner._checks(run, target) if check.id == "serving")
    assert serving.status == "fail"
    assert serving.metrics["error_rate"] == 1


@pytest.mark.parametrize("scenario", ["client_deadline", "deadline_probe", "client_cancel"])
@pytest.mark.parametrize("probe_seconds", [.01, .02])
async def test_designed_deadline_probe_at_or_above_request_timeout_stays_intentional(scenario, probe_seconds):
    import asyncio

    target = Target(name="service", url="http://service", model="test")
    run = runner._Run(RunConfig(targets=[target], checks=["cancellation"], request_timeout_seconds=.01),
                      None, None)

    class Client:
        async def execute(self, spec, **kwargs):
            await asyncio.sleep(1)

    value = await run.request(target, Client(), RequestSpec(
        fixture_id="deadline", scenario=scenario, check_ids=["cancellation"], messages=[]),
        timeout_override=probe_seconds)
    assert value.status == "timeout"
    assert value.outcome == "intentional_stop"
    assert run.errors == 0
    assert runner._stats([value])["error_rate"] is None


@pytest.mark.parametrize("values,count", [
    ([], 0),
    ([record(status="failed", error="request failed (ConnectError)")], 0),
    ([record(not_dispatched=True, intentional=True)], 1),
    ([record(not_dispatched=True), record(), record(not_dispatched=True)], 2),
])
def test_preparation_failure_reason_is_shared_and_accepts_iterators(values, count):
    from giraffe.request_outcome import preparation_failure_reason

    expected = (f"{count} request(s) could not be prepared locally and were not sent; "
                "fix the request inputs and rerun.") if count else None
    assert preparation_failure_reason(iter(values)) == expected


@pytest.mark.parametrize("scenario", ["initial", "deadline_probe"])
def test_live_failed_count_keeps_local_preparation_failures(scenario):
    value = record(not_dispatched=True, status="failed", scenario=scenario)
    assert runner._request_failed(value)
    assert not runner._serving_failure(value, scenario)


def test_pack_quiet_preparation_failure_is_inconclusive_even_with_scored_coverage():
    from tests.test_runner import _pack_config
    from giraffe.request_outcome import preparation_failure_reason

    config = _pack_config(checks=["correctness"])
    run = runner._Run(config, None, None)
    target = config.targets[0]
    run.records = [record(id=spec.fixture_id, target=target.name, fixture_id=spec.fixture_id,
                          scenario="pack_control", check_ids=["correctness"], score=True)
                   for spec in run.pack_specs]
    local = record(id="local", target=target.name, fixture_id=run.pack_specs[0].fixture_id,
                   scenario="pack_control", check_ids=["correctness"], valid=False,
                   status="failed", not_dispatched=True)
    run.records.append(local)
    pack = runner._pack_results(run, target)
    assert pack["coverage_complete"]
    assert pack["control_status"] == "inconclusive"
    assert pack["cases"][0]["quiet"]["status"] == "inconclusive"
    assert pack["cases"][0]["quiet"]["reason"] == preparation_failure_reason([local])
    check = next(check for check in runner._checks(run, target) if check.id == "correctness")
    assert check.status == "inconclusive"
    assert check.summary.startswith("Some fixtures have no complete, locally scored answer.")


@pytest.mark.parametrize("scenario", ["initial", "client_deadline", "deadline_probe", "client_cancel"])
@pytest.mark.parametrize("exception", [
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.TimeoutException, TimeoutError,
    httpx.ConnectTimeout,
])
async def test_real_transport_timeout_keeps_designed_probe_intent(monkeypatch, scenario, exception):
    target = Target(name="service", url="http://service", model="test")
    run = runner._Run(RunConfig(targets=[target], checks=["cancellation"], request_timeout_seconds=.01),
                      None, None)
    original = httpx.AsyncClient

    def timeout(request):
        raise exception("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(timeout), **kw))
    async with LLMClient(target, run.config) as client:
        value = await run.request(target, client, RequestSpec(
            fixture_id="deadline", scenario=scenario, check_ids=["cancellation"], messages=[]),
            timeout_override=.02)
    expected = ("failed" if exception is httpx.ConnectTimeout else
                "timeout" if scenario == "initial" else "intentional_stop")
    assert value.outcome == expected
    assert not value.not_dispatched


async def test_preparation_callback_preserves_diagnostic_and_retains_local_failure(monkeypatch):
    target = Target(name="service", url="http://service", model="test")
    events, diagnostics = [], []
    run = runner._Run(RunConfig(targets=[target], checks=["serving"], stop_after_errors=1),
                      events.append, None)
    original = httpx.AsyncClient
    sent = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200)), **kw))
    async with LLMClient(target, run.config) as client:
        monkeypatch.setattr(client._client, "build_request", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad input")))
        value = await run.request(target, client, RequestSpec(
            fixture_id="prepare", scenario="warm", check_ids=["serving"], messages=[]),
            prepared_admission=True, admission_deadline=run.deadline,
            on_preparation_failure=lambda record: diagnostics.append(record.error))
    assert diagnostics == ["request failed (ValueError)"]
    assert value is not None and value.outcome == "not_dispatched"
    assert run.records == [value] and run.errors == 1
    assert run.stop_reason() == "error threshold reached"
    assert not sent and run.attempted == run.budget_used == 0
    assert value.dispatch_ms is None
    assert not any(event["event"] == "request_started" for event in events)
    assert next(event for event in events if event["event"] == "request_completed")["request"]["outcome"] == "not_dispatched"


@pytest.mark.parametrize("status", ["pass", "fail", "blocked", "skipped", "inconclusive"])
def test_preparation_guard_preserves_nonpassing_check_status_and_prose(monkeypatch, status):
    from giraffe import load_test
    from giraffe.models import CheckResult

    target = Target(name="service", url="http://service", model="test")
    run = runner._Run(RunConfig(targets=[target], checks=["load"]), None, None)
    run.records = [record(status="failed", valid=False, not_dispatched=True, check_ids=["load"])]
    monkeypatch.setattr(load_test, "result", lambda *args: CheckResult(
        id="load", target=target.name, title="Load", status=status,
        summary="Original result explanation.", finding_summary="Original finding.",
        metrics={"reason_code": "original_reason"}))
    check = next(check for check in runner._checks(run, target) if check.id == "load")
    assert check.status == ("inconclusive" if status == "pass" else status)
    if status == "pass":
        assert "could not be prepared locally" in check.summary
    else:
        assert check.summary == "Original result explanation."
        assert check.finding_summary == "Original finding."
        assert check.metrics["reason_code"] == "original_reason"
