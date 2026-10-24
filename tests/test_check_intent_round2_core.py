"""Round-two evidence regressions; transport tests use no local sockets."""

import asyncio
import json
import random
import time

import httpx
import pytest

from giraffe import runner
from giraffe.client import REASONING_BUDGET_ERROR, _answer_spans
from giraffe.fixtures import final_answer, reasoning_budget
from giraffe.logprob_workload import summarize_logprobs
from giraffe.models import Consistency, Limits, RequestRecord, RequestSpec
from giraffe.overlap import evidenced_overlap, max_evidenced_overlap, output_window, peak_evidenced_concurrency
from giraffe.timing import exceeded_unfinished
from tests.test_check_intent_core import DONE, FakeClient, cfg, check, ev, install, one, words


def record(name="r", **extra):
    return RequestRecord(**(dict(id=name, target="t", fixture_id="f", scenario="warm",
        check_ids=["serving"], started_at="now", status="completed", valid=True, score=True,
        http_status=200, output="answer", first_output_ms=2, last_output_ms=10,
        stream_end_ms=12, elapsed_ms=30, timing_start_ms=0, finish_reason="stop",
        answer_reads=4, answer_chunks=4, output_tokens=4, completion_tokens=4,
        answer_chars=6, first_read_answer_chars=1) | extra))


async def test_v1_first_burst_has_no_pace_or_measured_gap(monkeypatch):
    first = b"".join(ev(w) for w in words(97))
    second = b"".join(ev(w) for w in words(3, 97))
    r = await one(monkeypatch, [first, .02, second, ev(finish="stop", usage={"completion_tokens": 100}), DONE])
    assert r.generation_tokens_per_second is None
    run = runner._Run(cfg(checks=["generation"], limits=Limits(min_samples=2, stream_gap_ms=500)), None, None)
    r.scenario, r.check_ids = "generation", ["generation"]
    run.records = [r]
    result = next(c for c in runner._checks(run, run.config.targets[0]) if c.id == "generation")
    assert result.status == "inconclusive"
    assert result.metrics["measured_gap_samples"] == 0


def test_v2_cleanup_is_not_an_overlap_window():
    a = record("a", first_output_ms=1, last_output_ms=2, stream_end_ms=3, elapsed_ms=100)
    b = record("b", timing_start_ms=4, first_output_ms=1, stream_end_ms=3)
    assert output_window(a) == (1, 3)
    assert evidenced_overlap(a, [a, b]) == peak_evidenced_concurrency([a, b]) == 1
    assert output_window(a.model_copy(update={"stream_end_ms": None})) is None


@pytest.mark.parametrize("duration, expected", [(.5, 1), (1, 2), (2, 2)])
def test_v2_overlap_needs_one_unstalled_millisecond(duration, expected):
    a = record("a", first_output_ms=0, stream_end_ms=10)
    b = record("b", first_output_ms=10-duration, stream_end_ms=20)
    assert evidenced_overlap(a, [a, b]) == peak_evidenced_concurrency([a, b]) == expected
    delays = [{"start_ms": 10-duration, "end_ms": 10-.5}]
    assert evidenced_overlap(a, [a, b], delays) == peak_evidenced_concurrency([a, b], delays) == 1


async def test_v2_stream_end_is_observed_before_slow_close(monkeypatch):
    from tests.test_check_intent_core import Chunks
    async def close(self):
        await asyncio.sleep(.02)
    monkeypatch.setattr(Chunks, "aclose", close)
    r = await one(monkeypatch, [ev("answer"), .002, ev(finish="stop"), DONE])
    # HTTP cleanup is now background work, outside the completed response timer.
    assert r.stream_end_ms is not None and 0 <= r.elapsed_ms-r.stream_end_ms < 15


@pytest.mark.parametrize("scenario", ["sustained", "recovery_baseline", "recovery"])
def test_v3_recovery_budget_is_inconclusive_not_error(scenario):
    run = runner._Run(cfg(checks=["recovery"], limits=Limits(min_samples=2, latency_ms=500)), None, None)
    run.records = [record(scenario=s, check_ids=["recovery"]) for s in ("sustained", "recovery_baseline", "recovery")]
    r = next(r for r in run.records if r.scenario == scenario)
    r.valid, r.error, r.finish_reason, r.output, r.reasoning = False, REASONING_BUDGET_ERROR, "length", "", "thinking"
    result = next(c for c in runner._checks(run, run.config.targets[0]) if c.id == "recovery")
    assert result.status == "inconclusive" and "reasoning exhausted the output budget" in result.summary.lower()
    assert result.metrics["sustained_error_rate"] == 0
    assert result.metrics["baseline_non_timeout_error_rate"] in (None, 0)




@pytest.mark.parametrize("check_id, scenario", [("recovery", "sustained")])
def test_v3_reasoning_budget_does_not_hide_a_real_failure(check_id, scenario):
    config = cfg().model_copy(update={"checks": [check_id]})
    run = runner._Run(config, None, None)
    run.records = [record("budget", scenario=scenario, check_ids=[check_id], valid=False, score=None,
        output="", reasoning="thinking", error=REASONING_BUDGET_ERROR, finish_reason="length"),
        record("failure", scenario=scenario, check_ids=[check_id], status="failed", valid=False, score=None)]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == check_id)
    assert result.status == "fail"


async def test_v4_prime_timeout_stays_outside_serving():
    config = cfg(checks=["serving", "first_output", "recovery"])
    run = runner._Run(config, None, None)
    spec = RequestSpec(fixture_id="p", scenario="recovery_prime", check_ids=["recovery"], messages=[])
    r = await run.request(config.targets[0], FakeClient(status="timeout"), runner._spec(spec, "recovery_prime"))
    assert run.errors == 0 and r.check_ids == ["recovery"]


@pytest.mark.parametrize("separate", [False, True])
async def test_v5_cancellation_counts_reasoning_generation(monkeypatch, separate):
    parts = [ev(reasoning="hm") if separate else ev("<think>hm"), .002,
             ev(reasoning=" more") if separate else ev(" more"), .5, ev("answer"), ev(finish="stop"), DONE]
    install(monkeypatch, lambda payload: parts)
    config = cfg(checks=["cancellation"], test_options={"cancellation": {"cancel_after_chunks": 2}})
    spec = RequestSpec(fixture_id="cancel", scenario="client_cancel", check_ids=["cancellation"],
        messages=[], cancel_after_chunks=2)
    from giraffe.client import LLMClient
    async with LLMClient(config.targets[0], config) as client:
        r = await client.execute(spec)
    assert r.status == "cancelled" and r.first_output_ms is None
    assert r.generation_chunks == 2 and r.first_reasoning_ms <= r.client_cancelled_ms
    run = runner._Run(config, None, None)
    run.records = cancellation_records(r)
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "cancellation")
    assert result.status == "pass", result.summary


def cancellation_records(cancel=None):
    return [cancel or record("cancel", scenario="client_cancel", check_ids=["cancellation"], status="cancelled",
                valid=False, finish_reason=None, client_cancelled_at_ns=1, generation_chunks=2,
                first_reasoning_ms=1, first_output_ms=None, client_cancelled_ms=3),
        record("control", scenario="stop_control", check_ids=["cancellation"], fixture_id="limits.stop_control.v1", output="blue [END] green"),
        record("stop", scenario="limits", check_ids=["cancellation"], fixture_id="limits.stop.v1", output="blue"),
        record("baseline", scenario="cancellation_baseline", check_ids=["cancellation"]),
        record("probe", scenario="cancellation_probe", check_ids=["cancellation"])]


def test_v5_generation_evidence_must_precede_cancellation():
    config = cfg(checks=["cancellation"], test_options={"cancellation": {"cancel_after_chunks": 2}})
    run = runner._Run(config, None, None)
    run.records = cancellation_records()
    run.records[0].first_reasoning_ms = 4
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "cancellation")
    assert result.status == "inconclusive"


@pytest.mark.parametrize("separate", [False, True])
async def test_v5_tags_or_whitespace_are_not_first_generation(monkeypatch, separate):
    from giraffe.client import LLMClient
    parts = [ev(reasoning=" ") if separate else ev("<think>"), .02,
             ev(reasoning="hm") if separate else ev("hm"), ev(finish="stop"), DONE]
    install(monkeypatch, lambda payload: parts)
    config = cfg()
    spec = RequestSpec(fixture_id="cancel", scenario="client_cancel", check_ids=[], messages=[], cancel_after_chunks=1)
    async with LLMClient(config.targets[0], config) as client:
        r = await client.execute(spec)
    assert r.status == "cancelled" and r.first_output_ms is None
    assert r.generation_chunks == 1 and 15 <= r.first_reasoning_ms <= r.client_cancelled_ms


@pytest.mark.parametrize("shape", ["completed", "failed"])
@pytest.mark.parametrize("evidence", ["none", "separate", "inline", "tokens"])
def test_v6_budget_classifier_requires_reasoning(shape, evidence):
    r = record(valid=False, status=shape, finish_reason="length", output=" \n",
        error=REASONING_BUDGET_ERROR if shape == "completed" else "response contained no useful answer output")
    if evidence == "separate":
        r.reasoning = "thinking"
    elif evidence == "inline":
        r.output = "<think>thinking"
    elif evidence == "tokens":
        r.reasoning_tokens = 1
    assert reasoning_budget(r) == (evidence != "none")
    stats = runner._stats([r])
    assert stats["reasoning_budget_exhausted"] == int(evidence != "none")
    assert stats["failed"] == int(evidence == "none")


@pytest.mark.parametrize("text", ["", " \n", "\t"])
async def test_v6_empty_length_is_a_real_failure(monkeypatch, text):
    r = await one(monkeypatch, [ev(text), ev(finish="length"), DONE])
    assert r.status == "failed" and r.error == "response contained no useful answer output"
    assert runner._serving_failure(r, "warm")


def test_v8_logprob_length_reason_is_explicit():
    settings = Consistency(levels=[1], shapes=["short"], repetitions=2, logprobs={"top_logprobs": 0})
    rows = [record(f"r{i}", finish_reason="length", requested_max_tokens=8, workload={
        "phase": "measured", "level": 1, "input_bucket": "short", "round": i,
        "corpus_index": 0, "load_qualified": True}) for i in range(2)]
    result = summarize_logprobs(rows, settings)
    reasons = " ".join(result["conditions"][0]["coverage_reasons"]).lower()
    assert "output budget" in reasons and "8" in reasons


@pytest.mark.parametrize("text", ["<think>a<think>b</think>c</think>ans",
    "<think>a<reasoning>b</think>c</reasoning></think>ans", "hidden</think>ans",
    "prefix<think>a</think>suffix", "<think>unclosed"])
def test_v10_answer_spans_follow_final_answer_depth(text):
    spans = _answer_spans(text)
    assert "".join(text[a:b] for a, b in spans or ()).lstrip() == final_answer(text)[0]


async def test_v10_nested_reasoning_does_not_start_answer_clock(monkeypatch):
    r = await one(monkeypatch, [ev("<think>a<think>b</think>c</think>"), .02, ev("ans"), ev(finish="stop"), DONE])
    assert r.first_output_ms >= 15 and r.answer_reads == 1


@pytest.mark.parametrize("separate", [False, True])
async def test_v11_zero_reasoning_tokens_are_unknown_with_reasoning(monkeypatch, separate):
    r = await one(monkeypatch, [ev(reasoning="thinking") if separate else ev("<think>thinking</think>"),
        ev("a "), .002, ev("b "), .002, ev("c"), ev(finish="stop", usage={
            "completion_tokens": 6, "completion_tokens_details": {"reasoning_tokens": 0}}), DONE])
    assert r.generation_tokens_per_second is None and r.output_tokens is None


@pytest.mark.parametrize("usage, expected", [(3, "fail"), (8, "pass"), (9, "fail")])
def test_v12_budget_caps_still_require_consistent_usage(usage, expected):
    config = cfg(checks=["cancellation"], test_options={"cancellation": {"cancel_after_chunks": 2}})
    run = runner._Run(config, None, None)
    cap = record("cap", scenario="limits", fixture_id="limits.output_cap.v1", check_ids=["cancellation"],
        valid=False, output="", reasoning="thinking", finish_reason="length", error=REASONING_BUDGET_ERROR,
        requested_max_tokens=8, completion_tokens=usage, output_tokens=0)
    run.records = [*cancellation_records(), cap]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "cancellation")
    assert result.status == expected, result.summary
    assert result.metrics["reasoning_budget_exhausted"] == 1
    assert result.metrics["reasoning_budget_request_ids"] == ["cap"]


@pytest.mark.parametrize("status", ["failed", "timeout"])
@pytest.mark.parametrize("http_status", [None, 200, 400])
def test_v13_only_accepted_unfinished_responses_violate_timing(status, http_status):
    r = record(status=status, valid=False, http_status=http_status, first_output_ms=None, elapsed_ms=100)
    assert bool(exceeded_unfinished(r, Limits(first_output_ms=50))) == (http_status == 200)


async def test_v14_finishing_past_deadline_does_not_abort(monkeypatch):
    async def target(run, target, fixtures):
        run.records = [record(check_ids=["serving"])]
        run.deadline = time.monotonic() - 1
    monkeypatch.setattr(runner, "_target", target)
    report = await runner.run_suite(cfg(checks=["serving"]))
    assert report.abort_reason is None and check(report, "serving").status == "pass"


async def test_v14_monitor_does_not_abort_work_finished_during_its_tick(monkeypatch):
    run = runner._Run(cfg(), None, None)
    pending = asyncio.get_running_loop().create_future()
    async def tick(_):
        pending.set_result(None)
        run.deadline = time.monotonic()-1
    monkeypatch.setattr(asyncio, "sleep", tick)
    await run.monitor([pending])
    assert run.reason is None


@pytest.mark.parametrize("scenario, unfinished", [("json_c2", {"json"}), ("warm", {"serving", "access", "first_output"})])
async def test_v15_deadline_only_marks_scenario_owners(scenario, unfinished):
    config = cfg(checks=["json", "serving", "access", "first_output"])
    run = runner._Run(config, None, None)
    spec = RequestSpec(fixture_id="f", scenario=scenario, check_ids=list(unfinished), messages=[])
    await run.request(config.targets[0], FakeClient(status="cancelled"), runner._spec(spec, scenario))
    assert run.unfinished["t"] == unfinished


async def test_v15_json_deadline_preserves_completed_core_verdicts(monkeypatch):
    class Client(FakeClient):
        def __init__(self, *args, max_connections=None):
            super().__init__()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, spec, **kwargs):
            if spec.scenario.startswith("json_c"):
                await asyncio.sleep(5)
            return await super().execute(spec, **kwargs)
    monkeypatch.setattr(runner, "LLMClient", Client)
    config = cfg(checks=["access", "serving", "first_output", "json"], samples=2,
        max_duration_seconds=1, request_timeout_seconds=5, limits=Limits(min_samples=2, first_output_ms=100))
    report = await runner.run_suite(config)
    assert check(report, "json").status == "inconclusive"
    assert all(check(report, c).status == "pass" for c in ("access", "serving", "first_output"))
    assert report.abort_reason == "maximum run duration reached"


@pytest.mark.parametrize("estimated, reported, expected", [(700, None, "fail"), (701, None, "inconclusive"),
    (850, None, "inconclusive"), (900, 700, "fail"), (500, 950, "inconclusive")])
def test_v16_context_estimates_need_density_margin(estimated, reported, expected):
    config = cfg(checks=["context"], context_limit=1000)
    run = runner._Run(config, None, None)
    run.records = [record(status="failed", valid=False, score=None, scenario="context", check_ids=["context"],
        http_status=400, error="context length rejection", input_tokens=reported,
        workload={"calibrated_input_tokens": estimated})]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "context")
    assert result.status == expected


def test_v9_gate_sweeps_large_serial_cohort_once(monkeypatch):
    from giraffe import overlap
    rows = [record(str(i), timing_start_ms=i*20) for i in range(6000)]
    original, calls = overlap.output_window, []
    def window(r):
        calls.append(r.id)
        return original(r)
    monkeypatch.setattr(overlap, "output_window", window)
    delays = [{"start_ms": k*500, "end_ms": k*500+120} for k in reversed(range(100))]
    assert max_evidenced_overlap(rows[:5000], rows, delays) == 1
    assert len(calls) == len(rows)


def test_v9_sweep_agrees_with_pairwise_gate_across_delays_and_targets():
    rng = random.Random(1)
    for _ in range(30):
        rows = [record(str(i), timing_start_ms=rng.randrange(30), target=rng.choice(["t", "u"]),
                       first_output_ms=0, stream_end_ms=rng.randrange(15)) for i in range(20)]
        delays = [{"start_ms": a, "end_ms": a+rng.randrange(5)} for a in rng.choices(range(40), k=10)]
        chosen = rows[::3]
        expected = min(2, max(evidenced_overlap(r, rows, delays) for r in chosen))
        assert max_evidenced_overlap(chosen, rows, delays) == expected


@pytest.mark.parametrize("level", [1, 2])
def test_v8_logprob_reasons_survive_summary_overrides(level):
    config = cfg(checks=["consistency"], concurrency=level, consistency={"levels": [level], "shapes": ["short"],
        "repetitions": 2, "logprobs": {"top_logprobs": 0}})
    run = runner._Run(config, None, None)
    run.records = [record(f"r{i}", scenario=f"consistency_c{level}_short", check_ids=["consistency"],
        stream=False, finish_reason="length", requested_max_tokens=8, workload={
        "phase": "measured", "level": level, "input_bucket": "short", "round": i,
        "corpus_index": 0, "load_qualified": False, "logprobs": True}) for i in range(2)]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "consistency")
    assert result.status == "inconclusive" and "output budget (8 tokens)" in result.summary.lower()
    if level > 1:
        assert "server-side concurrency needs streamed output timing" in result.summary.lower()


@pytest.mark.parametrize("serial", [True, False])
async def test_v2_socket_free_server_release_precedes_client_cleanup(monkeypatch, serial):
    from giraffe.client import LLMClient
    from tests.test_check_intent_core import _HTTPX_CLIENT
    slot = asyncio.Semaphore(1 if serial else 4)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ev("answer")
            await asyncio.sleep(0 if serial else .004)
            yield ev(finish="stop")
            slot.release()
            yield DONE
        async def aclose(self):
            await asyncio.sleep(.006)
    async def handler(request):
        await slot.acquire()
        return httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kw))
    config = cfg(concurrency=4)
    spec = RequestSpec(fixture_id="f", scenario="warm", check_ids=["serving"], messages=[])
    async with LLMClient(config.targets[0], config, max_connections=4) as client:
        rows = await asyncio.gather(*(client.execute(spec) for _ in range(4)))
    for r in rows:
        r.timing_start_ms = r._timing_started_monotonic * 1000
        assert r.valid and 0 <= r.elapsed_ms-r.stream_end_ms < 4
    assert peak_evidenced_concurrency(rows) <= 1 if serial else peak_evidenced_concurrency(rows) == 4
    assert max_evidenced_overlap(rows, rows) == (1 if serial else 2)


async def test_v2_complete_body_end_precedes_httpx_automatic_close(monkeypatch):
    from giraffe.client import LLMClient
    from tests.test_check_intent_core import Chunks, _HTTPX_CLIENT
    async def close(self):
        await asyncio.sleep(.02)
    monkeypatch.setattr(Chunks, "aclose", close)
    body = json.dumps({"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]}).encode()
    async def handler(request):
        return httpx.Response(200, stream=Chunks([body]))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kw))
    config = cfg(stream=False)
    async with LLMClient(config.targets[0], config) as client:
        r = await client.execute(RequestSpec(fixture_id="f", scenario="warm", check_ids=[], messages=[], stream=False))
    assert r.valid and r.elapsed_ms-r.stream_end_ms >= 15
    assert output_window(r) is None


@pytest.mark.parametrize("serial", [True, False])
async def test_v2_fast_serial_endpoint_never_qualifies(monkeypatch, serial):
    from tests.fake_endpoint import FakeEndpoint
    from tests.test_check_intent_overlap import configuration
    with FakeEndpoint(service_slots=1 if serial else None, first_token_delay=0 if serial else .03,
                      chunk_delay=0 if serial else .03) as endpoint:
        report = await runner.run_suite(configuration(targets=[{"name": "local", "url": endpoint.url,
            "model": "fake-model"}], concurrency=4, samples=12))
    for c in report.checks:
        if c.id in {"correctness", "json"}:
            assert c.status == ("inconclusive" if serial else "pass"), c
