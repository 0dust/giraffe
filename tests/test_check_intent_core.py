"""Check-intent regressions for access, serving, first_output and generation; no local sockets."""

import asyncio
import json
import uuid

import httpx
import pytest

from giraffe import runner
from giraffe.client import REASONING_BUDGET_ERROR, LLMClient
from giraffe.fixtures import builtin_fixtures
from giraffe.models import Limits, RequestRecord, RequestSpec, RunConfig, Target

_HTTPX_CLIENT = httpx.AsyncClient

pytestmark = pytest.mark.asyncio


class Chunks(httpx.AsyncByteStream):
    """Each bytes item is one network read; a float sleeps between reads."""

    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for item in self.parts:
            if isinstance(item, float):
                await asyncio.sleep(item)
            else:
                yield item

    async def aclose(self):
        pass


def ev(content=None, *, reasoning=None, finish=None, usage=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    data = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if usage is not None:
        data["usage"] = usage
    return ("data: " + json.dumps(data) + "\n\n").encode()


DONE = b"data: [DONE]\n\n"


def install(monkeypatch, body_for):
    original = _HTTPX_CLIENT

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        parts = body_for(json.loads(request.content))
        if isinstance(parts, Exception):
            raise parts
        return httpx.Response(200, stream=Chunks(parts), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def cfg(**kw):
    values = dict(targets=[Target(name="t", model="m", url="http://local")], checks=["serving"],
                  samples=6, concurrency=1, max_requests=100, max_duration_seconds=30,
                  request_timeout_seconds=1, limits=Limits(min_samples=5))
    return RunConfig(**(values | kw))


def check(report, check_id):
    return next(c for c in report.checks if c.id == check_id)


def words(n, start=0):
    return [f"w{i} " for i in range(start, start + n)]


def streamed(n=6, delay=.01, tokens=None):
    parts = []
    for word in words(n):
        parts += [ev(word), delay]
    return parts + [ev(finish="stop", usage={"completion_tokens": tokens or n}), DONE]


async def one(monkeypatch, parts, scorer="none"):
    install(monkeypatch, lambda payload: parts)
    target = Target(name="t", model="m", url="http://local")
    spec = RequestSpec(fixture_id="f", scenario="warm", check_ids=["serving"], scorer=scorer,
                       messages=[{"role": "user", "content": "hi"}])
    async with LLMClient(target, RunConfig(targets=[target], request_timeout_seconds=5)) as client:
        return await client.execute(spec)


# ------------------------------------------------------------------ client evidence

async def test_buffered_stream_in_one_read_has_no_gap_evidence(monkeypatch):
    body = b"".join(ev(w) for w in words(40)) + ev(finish="stop", usage={"completion_tokens": 40}) + DONE
    record = await one(monkeypatch, [.05, body])
    assert record.valid and record.answer_chunks == 40 and record.answer_reads == 1
    assert record.max_stream_gap_ms is None and record.stream_gap_applicable
    assert record.generation_tokens_per_second is None
    assert runner._missing_timing([record], Limits(stream_gap_ms=20)) == ["stream_gap_ms"]


async def test_separate_reads_measure_gap_and_pace(monkeypatch):
    record = await one(monkeypatch, streamed(10, .02))
    assert record.valid and record.answer_reads == 10
    assert record.max_stream_gap_ms >= 15
    assert 30 < record.generation_tokens_per_second < 70


async def test_two_bursts_do_not_inflate_generation_rate(monkeypatch):
    first = b"".join(ev(w) for w in words(100))
    second = b"".join(ev(w) for w in words(100, 100))
    tail = ev(finish="stop", usage={"completion_tokens": 200}) + DONE
    record = await one(monkeypatch, [.5, first, .01, second, tail])
    assert record.valid and record.answer_reads == 2
    rate = record.generation_tokens_per_second
    assert rate is None or rate <= 400


async def test_unreported_reasoning_tokens_make_rate_unknown(monkeypatch):
    parts = [p for _ in range(5) for p in (ev(reasoning="think "), .05)]
    parts += [ev("K7P4 "), .01, ev("done"), ev(finish="stop", usage={"completion_tokens": 300}), DONE]
    record = await one(monkeypatch, parts)
    assert record.valid and record.reasoning and record.reasoning_tokens is None
    assert record.generation_tokens_per_second is None
    reported = [p for _ in range(5) for p in (ev(reasoning="think "), .05)]
    reported += [ev("K7P4 "), .02, ev("done "), .02, ev("now"), ev(finish="stop", usage={
        "completion_tokens": 8, "completion_tokens_details": {"reasoning_tokens": 5}}), DONE]
    record = await one(monkeypatch, reported)
    assert record.output_tokens == 3 and record.generation_tokens_per_second is not None


async def test_inline_reasoning_does_not_start_first_output(monkeypatch):
    parts = [ev("<think>"), ev("Let me consider."), .5, ev("</think>"), ev("K7P4"), ev(finish="stop"), DONE]
    record = await one(monkeypatch, parts)
    assert record.valid and record.reasoning_stripped
    assert record.first_output_ms >= 450
    assert record.first_reasoning_ms < 100
    assert record.answer_reads == 1


@pytest.mark.parametrize("parts", [
    [ev("<think>"), ev("Let me consider the label"), ev(finish="length"), DONE],
    [ev(reasoning="Let me consider"), ev(reasoning=" the label"), ev(finish="length"), DONE],
])
async def test_reasoning_that_exhausts_budget_is_not_an_answer(monkeypatch, parts):
    record = await one(monkeypatch, parts, scorer="exact")
    assert record.status == "completed" and not record.valid
    assert record.error == REASONING_BUDGET_ERROR
    assert record.first_output_ms is None and record.answer_hash is None
    assert not runner._serving_failure(record, "warm")


@pytest.mark.parametrize("parts", [
    [ev(reasoning="Let me consider"), ev(finish="stop"), DONE],
    [ev("<think>"), ev("Let me consider"), ev(finish="stop"), DONE],
])
async def test_missing_answer_without_reasoning_budget_is_failure(monkeypatch, parts):
    record = await one(monkeypatch, parts, scorer="exact")
    assert record.status == "failed" and record.error == "response contained no useful answer output"
    assert runner._serving_failure(record, "warm")


# ------------------------------------------------------------------ verdicts

async def test_healthy_stream_passes_core_timing_checks(monkeypatch):
    install(monkeypatch, lambda payload: streamed(8, .005))
    report = await runner.run_suite(cfg(checks=["access", "serving", "first_output", "generation"],
        limits=Limits(min_samples=5, first_output_ms=500, latency_ms=900, stream_gap_ms=200,
                      min_output_tokens_per_second=1)))
    for check_id in ("access", "serving", "first_output", "generation"):
        assert check(report, check_id).status == "pass", (check_id, check(report, check_id).summary)
    assert report.abort_reason is None


@pytest.mark.parametrize("parts", [
    [ev("<think>"), ev("Let me think. "), .3, ev("still thinking"), ev(finish="length"), DONE],
    [ev(reasoning="Let me think. "), .3, ev(reasoning="still"), ev(finish="length"), DONE],
])
async def test_reasoning_budget_records_make_serving_and_first_output_inconclusive(monkeypatch, parts):
    install(monkeypatch, lambda payload: parts)
    report = await runner.run_suite(cfg(checks=["access", "serving", "first_output"],
        limits=Limits(min_samples=5, first_output_ms=200), stop_after_errors=2))
    serving, first = check(report, "serving"), check(report, "first_output")
    assert serving.status == "inconclusive" and "raise max_output_tokens" in serving.summary
    assert serving.metrics["reasoning_budget_exhausted"] == serving.metrics["attempted"] > 0
    assert serving.metrics["failed"] == 0
    assert first.status == "inconclusive"
    assert report.abort_reason is None and report.overall != "pass"


async def test_reasoning_without_answer_on_stop_fails_serving(monkeypatch):
    install(monkeypatch, lambda payload: [ev(reasoning="thinking"), ev(finish="stop"), DONE])
    report = await runner.run_suite(cfg(checks=["serving"]))
    assert check(report, "serving").status == "fail"


async def test_run_deadline_cancellation_leaves_core_checks_unfinished(monkeypatch):
    calls = {"n": 0}

    def body(payload):
        calls["n"] += 1
        if calls["n"] == 3:
            return [10.0]
        return [ev("K7P4"), ev(finish="stop"), DONE]

    install(monkeypatch, body)
    report = await runner.run_suite(cfg(checks=["access", "serving"], samples=2, max_duration_seconds=1.0,
                                        request_timeout_seconds=5, limits=Limits(min_samples=2)))
    assert any(r.status == "cancelled" for r in report.requests)
    assert check(report, "serving").status == "inconclusive"
    assert check(report, "access").status == "inconclusive"
    assert report.abort_reason == "maximum run duration reached" and report.overall != "pass"


@pytest.mark.parametrize("error", [httpx.ProxyError, httpx.UnsupportedProtocol])
async def test_proxy_and_protocol_transport_failures_block_access(monkeypatch, error):
    install(monkeypatch, lambda payload: error("unreachable"))
    report = await runner.run_suite(cfg(checks=["access"], samples=2, stop_after_errors=50))
    assert check(report, "access").status == "blocked"


class FakeClient:
    def __init__(self, first_output_ms=1, status="completed"):
        self.first_output_ms, self.status = first_output_ms, status

    async def execute(self, spec, *, first_output_event=None):
        await asyncio.sleep(.001)
        ok = self.status == "completed"
        return RequestRecord(id=uuid.uuid4().hex, target="t", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now", status=self.status,
            valid=ok, output="x" if ok else "", elapsed_ms=self.first_output_ms + 1,
            first_output_ms=self.first_output_ms if ok else None, last_output_ms=self.first_output_ms + 1,
            answer_chunks=2, http_status=200 if ok else None, finish_reason="stop" if ok else None)


async def test_token_stress_scenarios_are_not_core_traffic():
    from giraffe.concurrent_workloads import _spec as stress_spec
    from giraffe.models import TokenStressCase
    config = cfg(checks=["access", "serving", "first_output"], concurrency=4, stop_after_errors=2,
                 limits=Limits(min_samples=2, first_output_ms=500))
    run = runner._Run(config, None, None)
    target = config.targets[0]
    assert runner._stress("stress_long_input_summary_c4") and runner._stress("stress_calibration_x")
    case = TokenStressCase(name="long_input_summary", input_tokens=200, output_tokens=50)
    spec = stress_spec(case, "p", "stress_long_input_summary_c4", level=4)
    records = await run.group(target, FakeClient(status="timeout"), [spec],
                              "stress_long_input_summary_c4", count=2, concurrency=2)
    assert records and all(not set(r.check_ids) & {"access", "serving", "first_output", "generation"}
                           for r in records)
    assert run.errors == 0 and run.stop_reason() is None


async def test_first_output_counts_timed_out_request_without_output(monkeypatch):
    calls = {"n": 0}

    def body(payload):
        calls["n"] += 1
        return [5.0] if calls["n"] == 4 else [ev("K7P4"), ev(finish="stop"), DONE]

    install(monkeypatch, body)
    report = await runner.run_suite(cfg(checks=["serving", "first_output"],
        limits=Limits(min_samples=5, first_output_ms=500, max_error_rate=.2)))
    timed_out = [r for r in report.requests if r.status == "timeout"]
    assert timed_out and timed_out[0].first_output_ms is None
    first = check(report, "first_output")
    assert first.status == "fail" and timed_out[0].id in first.metrics["violations"]


async def test_generation_counts_timed_out_request_as_latency_violation(monkeypatch):
    calls = {"n": 0}

    def body(payload):
        calls["n"] += 1
        if calls["n"] == 3:
            return [ev("w "), 5.0]
        return [ev("a "), ev("b "), ev(finish="stop", usage={"completion_tokens": 2}), DONE]

    install(monkeypatch, body)
    report = await runner.run_suite(cfg(checks=["generation"], limits=Limits(min_samples=5, latency_ms=500)))
    generation = check(report, "generation")
    assert generation.status == "fail"
    assert any(r.status == "timeout" and r.id in generation.metrics["violations"] for r in report.requests)


@pytest.mark.parametrize("scenario, fixture", [
    ("correctness_c4", "correctness"), ("json_c4", "json"), ("pack_loaded", "correctness"),
])
async def test_loaded_correctness_traffic_does_not_enter_first_output(scenario, fixture):
    config = cfg(checks=["first_output", "correctness"], concurrency=4,
                 limits=Limits(min_samples=2, first_output_ms=500))
    run = runner._Run(config, None, None)
    target = config.targets[0]
    warm = await run.group(target, FakeClient(50), builtin_fixtures(config)["short"], "warm", count=4)
    loaded = await run.group(target, FakeClient(4000), builtin_fixtures(config)[fixture],
                             scenario, count=4, concurrency=4, checks=["correctness"])
    first = next(c for c in runner._checks(run, target) if c.id == "first_output")
    assert first.status == "pass"
    assert set(first.evidence_ids) == {r.id for r in warm} and not set(first.evidence_ids) & {r.id for r in loaded}


async def test_buffered_streams_make_generation_gap_inconclusive(monkeypatch):
    blob = b"".join(ev(w) for w in words(30)) + ev(finish="stop", usage={"completion_tokens": 30}) + DONE
    install(monkeypatch, lambda payload: [.05, blob])
    report = await runner.run_suite(cfg(checks=["generation"], limits=Limits(min_samples=5, stream_gap_ms=20)))
    generation = check(report, "generation")
    assert generation.status == "inconclusive"
    assert generation.metrics["missing_timing_metrics"] == ["stream_gap_ms"]


async def test_gap_limit_needs_measured_gaps(monkeypatch):
    install(monkeypatch, lambda payload: [.05, ev("No."), ev(finish="stop", usage={"completion_tokens": 2}), DONE])
    report = await runner.run_suite(cfg(checks=["generation"], limits=Limits(min_samples=5, stream_gap_ms=20)))
    generation = check(report, "generation")
    assert generation.status == "inconclusive" and "separate network reads" in generation.summary


async def test_generation_only_run_excludes_cold_first_request(monkeypatch):
    install(monkeypatch, lambda payload: streamed(4, .005))
    report = await runner.run_suite(cfg(checks=["generation"], limits=Limits(min_samples=5, latency_ms=900)))
    initial = [r for r in report.requests if r.scenario == "initial"]
    generation = check(report, "generation")
    assert len(initial) == 1 and initial[0].id not in generation.evidence_ids
    assert all(r.scenario == "generation" for r in report.requests if r.id in generation.evidence_ids)


async def test_run_cancellation_keeps_last_request_unfinished():
    config = cfg(checks=["access", "serving"], limits=Limits(min_samples=2))
    stop = asyncio.Event()
    run = runner._Run(config, None, stop)
    target = config.targets[0]
    await run.group(target, FakeClient(), builtin_fixtures(config)["short"], "warm", count=2)

    class Cancelled(FakeClient):
        async def execute(self, spec, **kw):
            stop.set()
            record = await super().execute(spec, **kw)
            record.error = "run cancelled"
            return record

    await run.group(target, Cancelled(status="cancelled"), builtin_fixtures(config)["short"], "warm", count=1)
    assert run.reason == "cancelled by user"
    assert {"access", "serving"} <= run.unfinished[target.name]
    assert all(c.status == "inconclusive" for c in runner._checks(run, target) if c.id in config.checks)


async def test_intentional_cancellation_does_not_mark_run_unfinished():
    config = cfg(checks=["serving"])
    run = runner._Run(config, None, None)
    spec = builtin_fixtures(config)["short"][0].model_copy(update={
        "scenario": "warm", "check_ids": ["serving"], "cancel_after_chunks": 1})
    await run.request(config.targets[0], FakeClient(status="cancelled"), spec)
    assert not run.unfinished["t"] and run.reason is None and run.errors == 0


@pytest.mark.parametrize("http_status", [None, 200, 503])
async def test_no_output_failure_counts_only_after_successful_headers(http_status):
    from giraffe.timing import exceeded_unfinished
    record = RequestRecord(id="failed", target="t", fixture_id="f", scenario="warm",
        check_ids=["first_output"], started_at="now", status="failed", http_status=http_status,
        elapsed_ms=900)
    assert exceeded_unfinished(record, Limits(first_output_ms=500)) == (
        ["first_output_ms"] if http_status == 200 else [])


async def test_timeout_violation_respects_overlapping_client_delay():
    from giraffe.timing import qualify, qualified_violations
    record = RequestRecord(id="timeout", target="t", fixture_id="f", scenario="generation",
        check_ids=["first_output", "generation"], started_at="now", status="timeout", http_status=200,
        elapsed_ms=900, timing_start_ms=0, completed_ms=900)
    limits = Limits(first_output_ms=500, latency_ms=500)
    health = {"schema_version": 1, "windows": [{"start_ms": 100, "end_ms": 600, "lag_ms": 450}]}
    quality = qualify([record], health, 100)
    assert not qualified_violations([record], limits, quality, unfinished=True)
    quality = qualify([record], {"schema_version": 1, "windows": []}, 100)
    assert qualified_violations([record], limits, quality, unfinished=True) == [record.id]


async def test_inline_reasoning_and_answer_in_same_read_keep_both_timestamps(monkeypatch):
    record = await one(monkeypatch, [ev("<think>x</think>K7P4"), .05, ev(" yes"),
        ev(finish="stop", usage={"completion_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 2}}), DONE])
    assert record.valid and record.first_reasoning_ms == record.first_output_ms
    assert record.answer_reads == 2 and record.first_read_answer_chars == 4
    assert record.output_tokens == 2 and record.generation_tokens_per_second is not None


async def test_inline_reasoning_without_token_breakdown_has_no_rate(monkeypatch):
    record = await one(monkeypatch, [ev("<think>x</think>K7P4"), .05, ev(" yes"),
        ev(finish="stop", usage={"completion_tokens": 20}), DONE])
    assert record.valid and record.output_tokens is None and record.generation_tokens_per_second is None


async def test_read_tracking_limit_does_not_fabricate_gap_or_rate(monkeypatch):
    monkeypatch.setattr("giraffe.client._MAX_CONTENT_READS", 2)
    record = await one(monkeypatch, streamed(5))
    assert record.valid and record.answer_reads == 0
    assert record.max_stream_gap_ms is None and record.generation_tokens_per_second is None


async def test_legacy_sse_counts_are_not_network_read_evidence():
    record = RequestRecord(id="legacy", target="t", fixture_id="f", scenario="generation",
        check_ids=["generation"], started_at="now", status="completed", valid=True,
        output="abcd", output_tokens=4, completion_tokens=4, answer_chunks=4,
        first_output_ms=500, last_output_ms=750, max_stream_gap_ms=0)
    assert record.answer_reads == 0 and record.generation_tokens_per_second is None
    assert runner._missing_timing([record], Limits(stream_gap_ms=20)) == ["stream_gap_ms"]


async def test_rate_subtracts_first_read_token_share():
    record = RequestRecord(id="rate", target="t", fixture_id="f", scenario="generation",
        check_ids=["generation"], started_at="now", status="completed", valid=True,
        answer_chars=10, output_tokens=10, answer_reads=2, first_read_answer_chars=8,
        first_output_ms=500, last_output_ms=1500)
    assert record.generation_tokens_per_second == 2


async def test_too_few_measured_gap_samples_is_inconclusive(monkeypatch):
    config = cfg(checks=["generation"], limits=Limits(min_samples=2, stream_gap_ms=100))
    run = runner._Run(config, None, None)
    for index in range(3):
        run.records.append(RequestRecord(id=str(index), target="t", fixture_id="f", scenario="generation",
            check_ids=["generation"], started_at="now", status="completed", valid=True,
            output="ok", completion_tokens=2, answer_reads=2 if index == 0 else 1,
            first_output_ms=1, last_output_ms=2, max_stream_gap_ms=1 if index == 0 else None))
    generation = next(c for c in runner._checks(run, config.targets[0]) if c.id == "generation")
    assert generation.status == "inconclusive" and "separate network reads" in generation.summary


async def test_generation_rate_survives_response_retention(monkeypatch):
    record = await one(monkeypatch, streamed(4, .01))
    original = record.generation_tokens_per_second
    assert original is not None
    record.output = record.reasoning = ""
    restored = RequestRecord.model_validate(record.model_dump())
    assert restored.answer_chars > 0 and restored.generation_tokens_per_second == original


async def test_reasoning_budget_does_not_hide_real_serving_failure():
    config = cfg(checks=["serving"], limits=Limits(min_samples=2, max_error_rate=.1))
    run = runner._Run(config, None, None)
    base = dict(target="t", fixture_id="f", scenario="warm", check_ids=["serving"], started_at="now")
    run.records = [RequestRecord(id="good", status="completed", valid=True, **base),
        RequestRecord(id="budget", status="completed", error=REASONING_BUDGET_ERROR,
                      finish_reason="length", reasoning="thinking", **base),
        RequestRecord(id="failure", status="failed", **base)]
    stats = runner._stats(run.records)
    assert stats["error_rate"] == .5 and stats["reasoning_budget_exhausted"] == 1
    assert stats["attempted"] == sum(stats[k] for k in (
        "completed", "failed", "timed_out", "cancelled", "reasoning_budget_exhausted"))
    serving = next(c for c in runner._checks(run, config.targets[0]) if c.id == "serving")
    assert serving.status == "fail"


@pytest.mark.parametrize("run_stopped", [False, True])
async def test_stress_phase_cancellation_only_aborts_when_run_stopped(run_stopped):
    config = cfg(checks=["recovery"])
    run = runner._Run(config, None, None)
    spec = builtin_fixtures(config)["short"][0].model_copy(update={
        "scenario": "sustained", "check_ids": ["recovery"]})
    class Cancelled(FakeClient):
        async def execute(self, spec, **kw):
            if run_stopped:
                run.stop.set()
            return await super().execute(spec, **kw)

    await run.request(config.targets[0], Cancelled(status="cancelled"), spec)
    assert (run.reason is not None) is run_stopped
    assert ("recovery" in run.unfinished["t"]) is run_stopped
