"""Scoring acceptance contracts; HTTP integration uses an in-process transport."""

import copy
import json

import httpx
import pytest


from giraffe import runner
from giraffe.client import LLMClient
from giraffe.fixtures import final_answer, score_response
from giraffe.judging import _vote
from giraffe.models import JudgeEndpoint, RequestRecord, RequestSpec, RunConfig, ToolExpectation
from tests.test_tool_contract import call, evaluate_fixture, fixture as tool_fixture
from tests.test_tool_conversations import fixture as conversation_fixture
from tests.test_workload_reliability import MemoryEndpoint
from tests.test_workloads import check, config


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def record(spec, output="", **changes):
    values = dict(id="r", target="local", fixture_id=spec.fixture_id, scenario=spec.scenario,
                  check_ids=spec.check_ids, started_at="now", status="completed", valid=True,
                  output=output, finish_reason="stop", input_chars=spec.input_chars)
    return RequestRecord(**(values | changes))


def answer_spec(scorer="exact", expected="K7P4", **changes):
    return RequestSpec(fixture_id="answer", scenario="answer", check_ids=["correctness"],
                       messages=[{"role": "user", "content": "Answer only."}],
                       scorer=scorer, expected=expected, **changes)


def test_sc1_truncated_contains_is_unscored():
    spec = answer_spec("contains", "25")
    assert score_response(spec, record(spec, "17 plus 8 is 25, but let me", finish_reason="length")).score is None


@pytest.mark.parametrize("output", ["-42", "+42", "answer: -42"])
def test_sc2_numeric_sign_is_part_of_number(output):
    spec = answer_spec("contains", "42")
    assert score_response(spec, record(spec, output)).score is False


def test_sc3_nested_reasoning_is_stripped_by_depth():
    assert final_answer("<think>a<think>b</think>c</think>K7P4") == ("K7P4", True, False)
    assert final_answer("<think>a<reasoning>b</reasoning>c</think>K7P4")[0] == "K7P4"
    assert final_answer("<think>a<think>b</think>K7P4")[2]


@pytest.mark.parametrize("output", ["K7P4.", "**K7P4**", "`K7P4`", '"K7P4"', "**K7P4.**"])
def test_q2_lenient_builtin_exact_keeps_user_sources_strict(output):
    spec = answer_spec()
    assert score_response(spec, record(spec, output)).score is True
    for source in ("request_pack", "custom_fixtures"):
        strict = spec.model_copy(update={"workload": {"request_source": source}})
        assert score_response(strict, record(strict, output)).score is False


@pytest.mark.parametrize("output,finish,expected", [
    ('```json\n{"count"', "length", None), ('```json {"count":', "length", None),
    ('```json {"count":3}', "length", None), ('```json {"count":3}```', "stop", True),
    ('```{"count":3}```', "stop", True), ('```json {"count":99}```', "length", False),
    ('```json {broken', "length", False),
])
def test_j1_j2_json_fences(output, finish, expected):
    spec = answer_spec("json", {"count": 3}, schema={"type": "object"})
    assert score_response(spec, record(spec, output, finish_reason=finish)).score is expected


def cache_simulator():
    seen = []
    def cached(payload, usage):
        text = "\n".join(m["content"] for m in payload["messages"])
        best = 0
        for old in seen:
            length = 0
            for a, b in zip(old, text):
                if a != b:
                    break
                length += 1
            best = max(best, length)
        seen.append(text)
        return best // 64 * 16
    return cached


@pytest.mark.asyncio
async def test_p1_p2_warm_server_reruns_with_period_answers(monkeypatch):
    class Period(MemoryEndpoint):
        async def handle(self, request):
            response = await super().handle(request)
            data = json.loads(response.content)
            data["choices"][0]["message"]["content"] += "."
            return httpx.Response(200, json=data)
    endpoint = Period(monkeypatch, cached=cache_simulator())
    settings = config(endpoint, checks=["prefix"], context_limit=16384, stream=False)
    for _ in range(2):
        result = check(await runner.run_suite(settings), "prefix")
        assert result.status == "pass", result.summary
        assert result.metrics["cache_controls_discriminative"]


@pytest.mark.asyncio
async def test_p3_oversized_prefix_is_not_sent(monkeypatch):
    endpoint = MemoryEndpoint(monkeypatch)
    result = check(await runner.run_suite(config(endpoint, checks=["prefix"],
        prefix={"prefix_chars": 65536}, stream=False)), "prefix")
    assert result.status == "inconclusive"
    assert not any("Trial" in p["messages"][0]["content"] for p in endpoint.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("size,status", [(128, "inconclusive"), (8192, "fail")])
async def test_p4_small_prefix_misses_are_unverified(monkeypatch, size, status):
    endpoint = MemoryEndpoint(monkeypatch, cached=0)
    result = check(await runner.run_suite(config(endpoint, checks=["prefix"], context_limit=16384,
        prefix={"prefix_chars": size}, stream=False)), "prefix")
    assert result.status == status, result.summary


@pytest.mark.asyncio
async def test_se2_sessions_accept_period_answers(monkeypatch):
    class Period(MemoryEndpoint):
        async def handle(self, request):
            response = await super().handle(request)
            data = json.loads(response.content)
            data["choices"][0]["message"]["content"] += "."
            return httpx.Response(200, json=data)
    endpoint = Period(monkeypatch)
    result = check(await runner.run_suite(config(endpoint, checks=["sessions"], stream=False)), "sessions")
    assert result.metrics["correct"] == result.metrics["scored"] > 0
    assert result.metrics["failed"] == 0
    assert result.status == "inconclusive", result.summary
    assert "server-side concurrency needs streamed output timing" in result.summary


def mock_transport(monkeypatch, handler):
    async def enter(client):
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client
    monkeypatch.setattr(LLMClient, "__aenter__", enter)


def tool_config(**changes):
    return RunConfig.model_validate(dict(targets=[{"name": "local", "url": "http://memory.test/v1", "model": "fake"}],
        checks=["tools"], stream=False, max_requests=100, max_duration_seconds=10,
        context_limit=16384, max_output_tokens=128) | changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("check_id", ["prefix", "sessions", "tools"])
async def test_r1_t2_reasoning_budget_is_not_request_error(monkeypatch, check_id):
    cache = cache_simulator()
    def handler(request):
        payload = json.loads(request.content)
        usage = {"prompt_tokens": sum(len(m["content"]) for m in payload["messages"]) // 4,
                 "completion_tokens": payload["max_tokens"]}
        usage["prompt_tokens_details"] = {"cached_tokens": cache(payload, usage)}
        return httpx.Response(200, json={"choices": [{"message": {"content": None,
            "reasoning_content": "Thinking about the answer"}, "finish_reason": "length"}], "usage": usage})
    mock_transport(monkeypatch, handler)
    report = await runner.run_suite(tool_config(checks=[check_id], stop_after_errors=1, limits={"min_samples": 2}))
    result = check(report, check_id)
    assert result.status == ("pass" if check_id == "prefix" else "inconclusive"), result.summary
    assert all(r.score is None for r in report.requests)


@pytest.mark.parametrize("choice", [False, True])
@pytest.mark.parametrize("arguments,expected", [('{"city":"Delhi"}', None), ('{"city":"Paris"}', False),
    ('{"city":', None), ('{broken}', False)])
def test_t2_complete_wrong_calls_still_fail_when_truncated(choice, arguments, expected):
    value = tool_fixture(expected_content=None)
    expectation = ToolExpectation.model_validate({k: v for k, v in value.items()
        if k in {"tools", "expected_calls", "expected_content"}} | ({"choice": {"mode": "required"}} if choice else {}))
    spec = answer_spec("tool", None, tool_expectation=expectation)
    calls = [{"id": "c", "type": "function", "function": {"name": "weather", "arguments": arguments}}]
    assert score_response(spec, record(spec, finish_reason="length", tool_calls=calls)).score is expected


def test_t3_empty_arguments_are_empty_object():
    value = tool_fixture(expected_content=None, expected_calls=[{"name": "weather", "arguments": {}}])
    value["tools"][0]["function"]["parameters"] = {"type": "object", "properties": {}, "additionalProperties": False}
    item = call()
    item["function"]["arguments"] = "  "
    assert evaluate_fixture(value, "", [item]).score is True


def test_t4_parallel_calls_are_multiset_and_extras_fail():
    value = tool_fixture(expected_content=None, expected_calls=[{"name": "weather", "arguments": {"city": "Delhi"}},
        {"name": "weather", "arguments": {"city": "Paris"}}])
    calls = [call(arguments={"city": "Paris"}), call(arguments={"city": "Delhi"})]
    assert evaluate_fixture(value, "", calls).score is True
    assert evaluate_fixture(value, "", calls + calls[:1]).score is False
    assert evaluate_fixture(value, "", [calls[0], calls[0]]).score is False


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_kind,status", [("reasoning", "fail"), ("unclosed", "inconclusive"), ("answer", "pass")])
async def test_t1_conversation_matches_only_final_answer(monkeypatch, answer_kind, status):
    def handler(request):
        payload = json.loads(request.content)
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        if not results:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Warsaw"}'}}]}
        else:
            text = results[0]["content"]
            output = "<think>" + text + ("</think>Cannot help." if answer_kind == "reasoning" else "")
            message = {"role": "assistant", "content": text if answer_kind == "answer" else output}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})
    mock_transport(monkeypatch, handler)
    report = await runner.run_suite(tool_config(tool_checks={"conversations": [conversation_fixture()], "modes": ["complete"]}))
    assert check(report, "tools").status == status


@pytest.mark.asyncio
async def test_t4_conversation_reordered_calls_keep_result_ids(monkeypatch):
    value = conversation_fixture()
    second = copy.deepcopy(value["steps"][0]["expected_calls"][0])
    second["arguments"]["city"] = "Paris"
    second["result"]["temperature_c"] = 20
    value["steps"][0]["expected_calls"].append(second)
    def handler(request):
        payload = json.loads(request.content)
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        if not results:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": city, "type": "function", "function": {"name": "get_weather", "arguments": json.dumps({"city": city})}}
                for city in ("Paris", "Warsaw")]}
        else:
            assert {m["tool_call_id"]: json.loads(m["content"])["temperature_c"] for m in results} == {"Paris": 20, "Warsaw": 15}
            message = {"role": "assistant", "content": next(m["content"] for m in results if m["tool_call_id"] == "Warsaw")}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls" if not results else "stop"}]})
    mock_transport(monkeypatch, handler)
    report = await runner.run_suite(tool_config(tool_checks={"conversations": [value], "modes": ["complete"]}))
    assert check(report, "tools").status == "pass"


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```json {} ```"])
def test_jd1_judge_accepts_one_complete_fence(fence):
    spec = answer_spec()
    vote = _vote(JudgeEndpoint(name="judge", url="http://judge", model="m"),
                 record(spec, fence.format('{"verdict":true,"reason":"Correct."}')))
    assert vote.verdict is True and vote.error_category is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,wording", [(400, "maximum context"), (422, "context length"),
    (400, "context size"), (413, "too many tokens"), (400, "context_length"),
    (400, "context window"), (400, "max_model_len")])
async def test_x1_calibrated_near_limit_rejections_are_inconclusive(monkeypatch, status, wording):
    import re
    def handler(request):
        payload = json.loads(request.content)
        prompt = payload["messages"][-1]["content"]
        tokens = len(prompt) // 4
        if tokens > 2048:
            return httpx.Response(status, text=wording + " PRIVATE_REQUEST_TEXT")
        answer = re.search(r"The box label is (\w+)\.", prompt)[1]
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens, "completion_tokens": 2}})
    mock_transport(monkeypatch, handler)
    result = check(await runner.run_suite(tool_config(checks=["context"], context_limit=4096, concurrency=1)), "context")
    assert result.status == "inconclusive", result.summary
    assert "server rejected" in result.summary and "70% estimated" in result.summary
    assert "PRIVATE" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_x1_locally_sized_context_rejection_without_usage_is_failure(monkeypatch):
    mock_transport(monkeypatch, lambda request: httpx.Response(422, text="context size too small"))
    result = check(await runner.run_suite(tool_config(checks=["context"], context_limit=4096)), "context")
    # Even the medium case is locally known to be inside the safe context budget.
    assert result.status == "fail", result.summary
    assert "server rejected" in result.summary.lower()


def context_run():
    settings = tool_config(checks=["context"], context_limit=4096, limits={"min_correctness": .8})
    return runner._Run(settings, None, None)


def context_record(position, *, size="long", chars=12000, tokens=3400, score=True, scenario="context", **changes):
    spec = answer_spec().model_copy(update={"fixture_id": f"context.{size}.{position}.v2", "scenario": scenario,
        "check_ids": ["context"], "input_chars": chars})
    return record(spec, "K7P4", input_tokens=tokens, score=score,
        workload={"position_coverage": {"positions": ["beginning", "middle", "end"], "reduced": False}}, **changes)


def test_x2_near_limit_beginning_miss_cannot_hide_in_pooled_accuracy():
    run = context_run()
    run.records = [context_record(p, chars=4000, tokens=1000) for p in ("beginning", "middle", "end")] * 4
    run.records += [context_record(p, score=p != "beginning") for p in ("beginning", "middle", "end")]
    result = next(r for r in runner._checks(run, run.config.targets[0]) if r.id == "context")
    assert result.metrics["accuracy"] > .9
    assert result.status == "fail" and "beginning" in result.summary
    assert result.metrics["near_limit_positions"]


@pytest.mark.parametrize("probe_only", [False, True])
def test_x2_each_near_limit_size_needs_every_position(probe_only):
    run = context_run()
    run.records = [context_record(p, chars=4000, tokens=1000) for p in ("beginning", "middle", "end")]
    run.records += [context_record("middle", scenario="context_probe" if probe_only else "context")]
    if not probe_only:
        run.records += [context_record("beginning", chars=13000), context_record("end", chars=13000)]
    result = next(r for r in runner._checks(run, run.config.targets[0]) if r.id == "context")
    assert result.status == "inconclusive", result.summary


def test_x2_healthy_near_limit_recall_passes():
    run = context_run()
    run.records = [context_record(p) for p in ("beginning", "middle", "end")]
    assert next(r for r in runner._checks(run, run.config.targets[0]) if r.id == "context").status == "pass"


@pytest.mark.parametrize("check_id", ["prefix", "sessions"])
@pytest.mark.parametrize("real_failure", [False, True])
@pytest.mark.parametrize("status,error", [("failed", "response contained no useful answer output"),
    ("completed", "reasoning exhausted the output budget before an answer")])
def test_r1_both_reasoning_budget_record_contracts(check_id, real_failure, status, error):
    from giraffe.fixtures import reasoning_budget
    settings = tool_config(checks=[check_id], prefix={"repeats": 3}, limits={"min_samples": 2})
    run = runner._Run(settings, None, None)
    spec = answer_spec().model_copy(update={"check_ids": [check_id]})
    for kind in ("shared", "independent"):
        for index in range(3):
            item = record(spec, status=status, valid=False, error=error, finish_reason="length",
                reasoning="thinking",
                input_tokens=2100, cached_prompt_tokens=2048 if kind == "shared" else 0,
                scenario=f"prefix_{kind}", input_chars=8400, workload={"shared_prefix_chars": 8192})
            assert reasoning_budget(item)
            run.records.append(item)
    if real_failure:
        run.records.append(record(spec, status="failed", valid=False, error="HTTP 503"))
    result = next(r for r in runner._checks(run, settings.targets[0]) if r.id == check_id)
    assert result.status == ("fail" if real_failure else "pass" if check_id == "prefix" else "inconclusive"), result.summary
    assert result.metrics["reasoning_budget_unscored"] == 6
    assert result.metrics["failed"] == int(real_failure)
    assert result.metrics["error_rate"] in ({1} if real_failure else {None, 0})


def test_t2_complete_wrong_call_is_not_hidden_by_later_partial_call():
    value = tool_fixture(expected_content=None, expected_calls=[{"name": "weather", "arguments": {"city": "Delhi"}},
        {"name": "weather", "arguments": {"city": "Paris"}}])
    expectation = ToolExpectation.model_validate({k: value[k] for k in ("tools", "expected_calls")})
    spec = answer_spec("tool", None, tool_expectation=expectation)
    calls = [call(arguments={"city": "Mumbai"}), call(arguments={"city": "Paris"})]
    calls[-1]["function"]["arguments"] = '{"city":'
    assert score_response(spec, record(spec, tool_calls=calls, finish_reason="length")).score is False


@pytest.mark.asyncio
async def test_t2_conversation_complete_wrong_call_fails_despite_truncation(monkeypatch):
    def handler(request):
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "length"}]})
    mock_transport(monkeypatch, handler)
    report = await runner.run_suite(tool_config(tool_checks={"conversations": [conversation_fixture()], "modes": ["complete"]}))
    assert check(report, "tools").status == "fail"


@pytest.mark.parametrize("prefix_chars,context_limit,history_turns,expected", [
    (8192, 4096, 3, 15), (65536, 4096, 3, 0),
    (32, 128, 8, 0), (163, 128, 3, 0), (164, 128, 3, 0),
])
async def test_p3_prefix_plan_matches_token_admission_and_nonce_boundary(
    monkeypatch, prefix_chars, context_limit, history_turns, expected,
):
    from giraffe.planning import request_plan
    endpoint = MemoryEndpoint(monkeypatch)
    settings = RunConfig(targets=[{"name": "local", "url": endpoint.url, "model": "fake-model"}],
        checks=["prefix"], max_requests=100, max_duration_seconds=30, stream=prefix_chars == 8192,
        context_limit=context_limit, prefix={"prefix_chars": prefix_chars, "history_turns": history_turns})
    planned = request_plan(settings)
    report = await runner.run_suite(settings)
    assert planned["count_kind"] == "exact"
    probes = [r for r in report.requests if r.scenario == "performance_evidence_probe"]
    assert len(endpoint.requests) == len(report.requests) == expected + len(probes)
    assert planned["planned_total"] == expected
    if not expected:
        assert check(report, "prefix").status == "inconclusive"
        assert any("prefix" in warning.lower() for warning in planned["warnings"])
    for item in report.requests:
        assert item.input_tokens + item.requested_max_tokens + 32 <= context_limit
