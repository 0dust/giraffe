import asyncio
import json

import httpx
import pytest

from giraffe.judging import judge_records
from giraffe.models import RequestRecord, RequestSpec, RunConfig


def configuration(judges=1, **settings):
    return RunConfig(
        targets=[{"name": "target", "url": "http://target", "model": "candidate"}],
        max_output_tokens=1, request_options={"temperature": 1, "stop": ["secret"]},
        judging={"judges": [{"name": f"j{i}", "url": f"http://judge{i}", "model": f"m{i}"}
                            for i in range(judges)], **settings},
    )


def fixture():
    return RequestSpec(
        fixture_id="pack:refund", scenario="request_pack", check_ids=["correctness"],
        messages=[{"role": "system", "content": "Customer support"},
                  {"role": "user", "content": "When can I return my order?"}],
        scorer="judge", expected="Within 30 days, with a receipt.",
        rubric="The receipt requirement is essential.",
    )


def record(number=0, **changes):
    return RequestRecord(
        id=str(number), target="target", fixture_id="pack:refund", scenario="pack_control",
        check_ids=["correctness"], started_at="2026-10-11T00:00:00Z",
        **({"status": "completed", "valid": True,
            "output": "Bring your receipt within thirty days."} | changes),
    )


def response(content, *, finish="stop"):
    return httpx.Response(200, json={"model": "observed-judge", "choices": [{
        "message": {"content": content}, "finish_reason": finish,
    }]})


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    clients = []

    def create(**kwargs):
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", create)
    return clients


@pytest.mark.asyncio
async def test_reference_protocol_isolated_from_target_and_every_output_judged(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return response(json.dumps({"verdict": True, "reason": "Same deadline and requirement."}))

    clients = transport(monkeypatch, handler)
    records = [record(0), record(1), record(2, valid=False, status="failed")]
    original = [r.model_dump() for r in records]
    events = []
    summary = await judge_records(configuration(), records, [fixture()], progress=events.append)
    assert len(seen) == 2
    for payload in seen:
        assert payload["model"] == "m0" and payload["max_tokens"] == 256
        assert payload["stream"] is False and payload["temperature"] == 0
        assert "stop" not in payload and "stream_options" not in payload
        data = json.loads(payload["messages"][1]["content"])
        assert data == {"messages": fixture().messages, "reference_answer": fixture().expected,
                        "generated_answer": records[0].output, "rubric": fixture().rubric}
        assert "untrusted" in payload["messages"][0]["content"].lower()
    assert all(r.score is True and r.judgement.status == "pass" for r in records[:2])
    assert records[2].judgement is None
    for before, after in zip(original, records):
        assert {k: v for k, v in after.model_dump().items() if k not in {"score", "score_message", "judgement"}} == {
            k: v for k, v in before.items() if k not in {"score", "score_message", "judgement"}}
    assert summary["judge_calls_total"] == summary["judge_calls_completed"] == 2
    assert [event["event"] for event in events] == ["judging_started", "judging_progress", "judging_progress", "judging_finished"]
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("votes,score,status", [
    ([True], True, "pass"), ([False], False, "fail"),
    ([True, False, True], True, "pass"), ([False, True, False], False, "fail"),
    ([True, False], None, "inconclusive"), ([True, True, "bad"], None, "inconclusive"),
])
async def test_complete_panel_majority(monkeypatch, votes, score, status):
    def handler(request):
        vote = votes[int(request.url.host.removeprefix("judge"))]
        return response(json.dumps({"verdict": vote, "reason": "A short reason."}))

    transport(monkeypatch, handler)
    item = record()
    summary = await judge_records(configuration(len(votes)), [item], [fixture()])
    assert item.score is score and item.judgement.status == status
    assert len(item.judgement.votes) == len(votes)
    assert summary["ties"] == int(votes == [True, False])
    assert all(vote.observed_model == "observed-judge" for vote in item.judgement.votes)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish,category", [
    ('{"verdict":true,"reason":"ok"}', "length", "truncated_response"),
    ('{"verdict":"true","reason":"ok"}', "stop", "invalid_verdict"),
    ('{"verdict":1,"reason":"ok"}', "stop", "invalid_verdict"),
    ('{"verdict":true,"reason":""}', "stop", "invalid_verdict"),
    ('{"verdict":true,"reason":"ok","extra":1}', "stop", "invalid_verdict"),
    ('{"verdict":true,"verdict":false,"reason":"ok"}', "stop", "invalid_verdict"),
    ('```json\n{"verdict":true,"reason":"ok"}\n``` trailing', "stop", "invalid_verdict"),
    (json.dumps({"verdict": True, "reason": "x" * 2001}), "stop", "invalid_verdict"),
])
async def test_strict_verdict_contract(monkeypatch, content, finish, category):
    transport(monkeypatch, lambda request: response(content, finish=finish))
    item = record()
    await judge_records(configuration(), [item], [fixture()])
    assert item.score is None and item.judgement.votes[0].error_category == category


@pytest.mark.asyncio
async def test_credentials_and_initialization_failures_are_sanitized(monkeypatch):
    settings = configuration(2)
    settings.judging.judges[0].api_key_env = "MISSING_JUDGE_KEY"
    settings.judging.judges[1].api_key_env = "GOOD_JUDGE_KEY"
    monkeypatch.delenv("MISSING_JUDGE_KEY", raising=False)
    monkeypatch.setenv("GOOD_JUDGE_KEY", "never-persist-this")
    headers = []

    def handler(request):
        headers.append(request.headers["Authorization"])
        return response('{"verdict":true,"reason":"Equivalent."}')

    clients = transport(monkeypatch, handler)
    item = record()
    await judge_records(settings, [item], [fixture()])
    assert item.score is None
    assert headers == ["Bearer never-persist-this"]
    assert item.judgement.votes[0].error_category == "client_initialization"
    assert "never-persist-this" not in item.model_dump_json()
    assert "MISSING_JUDGE_KEY" not in item.model_dump_json()
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_timeout_does_not_shrink_panel(monkeypatch):
    async def handler(request):
        if request.url.host == "judge2":
            await asyncio.sleep(.1)
        return response('{"verdict":true,"reason":"Equivalent."}')

    transport(monkeypatch, handler)
    item = record()
    await judge_records(configuration(3, request_timeout_seconds=.01), [item], [fixture()])
    assert item.score is None
    assert [v.verdict for v in item.judgement.votes] == [True, True, None]
    assert item.judgement.votes[2].error_category == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_phase_deadline_and_user_stop_bound_pending_work(monkeypatch, cancel):
    started = 0
    active = 0
    peak = 0
    stop = asyncio.Event()

    async def handler(request):
        nonlocal started, active, peak
        started += 1
        active += 1
        peak = max(active, peak)
        if cancel:
            stop.set()
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1
        return response('{"verdict":true,"reason":"Equivalent."}')

    clients = transport(monkeypatch, handler)
    records = [record(i) for i in range(30)]
    async with asyncio.timeout(.5):
        summary = await judge_records(configuration(3, concurrency=2, max_duration_seconds=.02),
                                      records, [fixture()], stop_event=stop)
    assert peak <= 2 and started <= 2 and active == 0
    assert summary["judge_calls_total"] == 90
    assert all(item.score is None for item in records)
    categories = {vote.error_category for item in records for vote in item.judgement.votes}
    assert categories == {"cancelled" if cancel else "phase_deadline"}
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_stopped_before_phase_does_not_open_clients(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Must not initialize clients after stop")

    monkeypatch.setattr("giraffe.judging.LLMClient", fail)
    stop = asyncio.Event()
    stop.set()
    item = record()
    await judge_records(configuration(), [item], [fixture()], stop_event=stop)
    assert item.score is None and item.judgement.votes[0].error_category == "cancelled"


@pytest.mark.asyncio
async def test_worker_pool_finishes_all_calls_without_deduplication(monkeypatch):
    active = 0
    peak = 0
    calls = 0

    async def handler(request):
        nonlocal active, peak, calls
        active += 1
        peak = max(peak, active)
        calls += 1
        await asyncio.sleep(.001)
        active -= 1
        return response('{"verdict":true,"reason":"Equivalent."}')

    clients = transport(monkeypatch, handler)
    records = [record(i) for i in range(20)]
    summary = await judge_records(configuration(3, concurrency=2), records, [fixture()])
    assert peak == 2 and calls == 60 and active == 0
    assert summary["judge_calls_completed"] == 60 and summary["scored_requests"] == 20
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_external_cancellation_closes_transport_and_preserves_partial_evidence(monkeypatch):
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            exited.set()

    clients = transport(monkeypatch, handler)
    item = record()
    task = asyncio.create_task(judge_records(configuration(), [item], [fixture()]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set() and all(client.is_closed for client in clients)
    assert item.score is None and item.judgement.votes[0].error_category == "cancelled"


@pytest.mark.asyncio
async def test_missing_ca_bundle_is_initialization_error(monkeypatch):
    transport(monkeypatch, lambda request: pytest.fail("No request after invalid TLS config"))
    item = record()
    await judge_records(configuration(ca_bundle="/nonexistent/giraffe-ca.pem"), [item], [fixture()])
    assert item.score is None
    assert item.judgement.votes[0].error_category == "client_initialization"
    assert "/nonexistent" not in item.model_dump_json()


@pytest.mark.asyncio
async def test_http_failure_does_not_persist_endpoint_error_body(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(503, text="credential=secret"))
    item = record()
    await judge_records(configuration(), [item], [fixture()])
    assert item.judgement.votes[0].error_category == "http_error"
    assert "secret" not in item.model_dump_json()


@pytest.mark.asyncio
async def test_malformed_unicode_judge_does_not_abort_other_votes(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "judge0":
            # A legal JSON escape can decode to a surrogate that cannot be encoded
            # when LLMClient hashes the completed answer in its finally block.
            body = {"choices": [{"message": {"content": "secret-\ud800"},
                                 "finish_reason": "stop"}]}
            return httpx.Response(200, content=json.dumps(body).encode())
        return response('{"verdict":true,"reason":"Equivalent."}')

    clients = transport(monkeypatch, handler)
    item = record()
    summary = await judge_records(configuration(3, concurrency=1), [item], [fixture()])
    assert seen == ["judge0", "judge1", "judge2"]
    assert item.valid and item.status == "completed" and item.score is None
    assert item.judgement.votes[0].error_category == "request_failed"
    assert [vote.verdict for vote in item.judgement.votes] == [None, True, True]
    assert summary["judge_calls_completed"] == 3 and summary["judge_calls_errors"] == 1
    assert "secret-" not in item.model_dump_json()
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_deterministic_and_failed_requests_do_not_trigger_judging(monkeypatch):
    monkeypatch.setattr("giraffe.judging.LLMClient", lambda *args: pytest.fail("Inactive judging"))
    item = record(valid=False, status="timeout")
    summary = await judge_records(configuration(), [item], [fixture()])
    assert summary["judge_calls_total"] == 0 and item.judgement is None
    deterministic = fixture().model_copy(update={"scorer": "exact"})
    item = record()
    summary = await judge_records(configuration(), [item], [deterministic])
    assert summary["judge_calls_total"] == 0 and item.judgement is None


@pytest.mark.asyncio
async def test_judges_receive_only_final_answer_and_skip_reasoning_only_and_truncation(monkeypatch):
    seen = []

    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.append(data["generated_answer"])
        return response('<think>Evaluate carefully.</think>{"verdict":true,"reason":"Correct."}')

    transport(monkeypatch, handler)
    records = [record(0, output=" \n<THINKING>Within 5 days</thinking>Within 30 days, with a receipt."),
               record(1, output="<reasoning>Within 30 days"),
               record(2, output="Within 30 days, with a receipt.", finish_reason="length"),
               record(3, output="<think>Within 30 days</think> ")]
    summary = await judge_records(configuration(), records, [fixture()])
    assert seen == ["Within 30 days, with a receipt."]
    assert records[0].score is True and records[0].reasoning_stripped
    assert records[0].output.startswith(" \n<THINKING>")
    for index in (1, 3):
        assert records[index].score is None and records[index].reasoning_stripped
        assert records[index].score_message == "reasoning without final answer"
    assert records[2].score is None and records[2].score_message == "truncated before a complete answer"
    assert all(item.judgement.status == "inconclusive" and item.judgement.votes == [] for item in records[1:])
    assert summary["judge_calls_total"] == summary["judge_calls_completed"] == 1
    assert summary["eligible_requests"] == 1 and summary["inconclusive_requests"] == 3


@pytest.mark.asyncio
async def test_only_unjudgeable_answers_make_no_judge_calls(monkeypatch):
    def handler(request):
        pytest.fail("A truncated answer must never be sent to a judge")

    clients = transport(monkeypatch, handler)
    item = record(finish_reason="length")
    summary = await judge_records(configuration(), [item], [fixture()])
    assert not clients and summary["judge_calls_total"] == 0
    assert summary["inconclusive_requests"] == 1
    assert item.score is None and item.score_message == "truncated before a complete answer"
