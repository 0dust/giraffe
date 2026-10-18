"""Answer scoring must use final answers and preserve missing evidence."""

import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.fixtures import builtin_fixtures, load_custom_fixtures, score_response, traffic_fixtures
from giraffe.models import RequestSpec
from tests.test_fixtures import config, custom_entry, custom_file, record


def fixture(scorer="exact", expected="4", **updates):
    return RequestSpec(fixture_id="answer", scenario="custom", check_ids=["correctness"],
                       messages=[{"role": "user", "content": "Compute the answer."}],
                       scorer=scorer, expected=expected, **updates)


@pytest.mark.parametrize("tag", ["think", "thinking", "reasoning", "ThInK"])
@pytest.mark.parametrize("scorer,expected,answer", [
    ("exact", "4", "4"), ("contains", "4", "Answer: 4."),
    ("json", {"count": 4}, '{"count":4}'),
])
def test_inline_reasoning_is_removed_before_all_text_scoring(tag, scorer, expected, answer):
    spec = fixture(scorer, expected, **({"schema": {}} if scorer == "json" else {}))
    original = record(spec, f" \n<{tag}>Possibly 14.</{tag}>\n{answer}", reasoning="Separate reasoning")
    scored = score_response(spec, original)
    assert scored.score is True
    assert scored.reasoning_stripped is True
    assert scored.output == original.output and scored.reasoning == original.reasoning
    assert original.score is None and original.reasoning_stripped is False


@pytest.mark.parametrize("scorer,expected,answer", [
    ("contains", "4", "Wrong"), ("exact", "4", "14"),
    ("json", {"count": 4}, '{"count":14}'),
])
def test_inline_reasoning_cannot_supply_expected_answer(scorer, expected, answer):
    spec = fixture(scorer, expected, **({"schema": {}} if scorer == "json" else {}))
    scored = score_response(spec, record(spec, f"<think>{json.dumps(expected)}</think>{answer}"))
    assert scored.score is False and scored.reasoning_stripped


@pytest.mark.parametrize("output", ["<think>4", " \n<THINKING>4</reasoning>4", "<reasoning>4</reasoning>  "])
@pytest.mark.parametrize("scorer", ["exact", "contains", "json", "judge"])
def test_reasoning_without_final_answer_is_unscored(output, scorer):
    spec = fixture(scorer, **({"schema": {}} if scorer == "json" else {}))
    scored = score_response(spec, record(spec, output))
    assert scored.score is None
    assert scored.score_message == "reasoning without final answer"
    assert scored.reasoning_stripped


def test_reasoning_anywhere_is_removed_and_consecutive_blocks_are_stripped():
    spec = fixture()
    assert score_response(spec, record(spec, "4<think>extra</think>")).score is True
    assert score_response(spec, record(spec, "<think>one</think> <reasoning>two</reasoning>4")).score is True


@pytest.mark.parametrize("expected,output,score", [
    ("4", "14", False), ("4", "40", False), ("4", "x4", False), ("4", "4x", False),
    ("4", "(4).", True), ("4", "14 then 4", True), ("A1", "A10", False),
    ("A1", "A1", True), ("A1", "a1", False), ("cat", "cat_", True),
    ("cat", "écat", False), ("4", "4٢", False), ("a b", "a\n b", True),
    ("#4", "x#4", True), ("4!", "4!x", True), ("+", "1+2", True), ("a-a", "xa-a-a", True),
])
def test_contains_requires_alphanumeric_boundaries(expected, output, score):
    spec = fixture("contains", expected)
    assert score_response(spec, record(spec, output)).score is score


@pytest.mark.parametrize("scorer,expected,output", [
    ("exact", "42", "4"), ("contains", "42", "4"),
    ("json", {"count": 42}, '{"count":'),
])
def test_truncated_incomplete_answer_is_unscored(scorer, expected, output):
    spec = fixture(scorer, expected, **({"schema": {}} if scorer == "json" else {}))
    scored = score_response(spec, record(spec, output, finish_reason="length"))
    assert scored.score is None and scored.score_message == "truncated before a complete answer"
    assert score_response(spec, record(spec, output, finish_reason="stop")).score is False


@pytest.mark.parametrize("scorer,expected,output", [
    ("exact", "42", "42"), ("contains", "42", "Answer: 42"),
    ("json", {"count": 42}, '{"count":42}'),
])
def test_truncated_completion_with_complete_matching_value_is_scorable(scorer, expected, output):
    spec = fixture(scorer, expected, **({"schema": {}} if scorer == "json" else {}))
    assert score_response(spec, record(spec, output, finish_reason="length")).score is True


@pytest.mark.parametrize("expected,output,score", [
    (3, "3.0", True), (3.0, "3", True),
    ({"x": [3, {"y": 2.0}]}, '{"x":[3.0,{"y":2}]}', True),
    ({"x": [True]}, '{"x":[1]}', False), ({"x": [1]}, '{"x":[true]}', False),
    (False, "0", False), (0, "false", False), (3, '"3"', False),
    ([3], "[3.0,3]", False), ({"x": 3}, '{"y":3}', False),
])
def test_json_structural_comparison_normalizes_numbers_but_not_booleans(expected, output, score):
    spec = fixture("json", expected, schema={})
    assert score_response(spec, record(spec, output)).score is score


@pytest.mark.parametrize("context_limit", [128, 160, 200, 250, 512, 4096])
def test_context_positions_have_meaningful_separation_or_explicit_reduced_coverage(context_limit):
    specs = builtin_fixtures(config(context_limit=context_limit))["context"]
    for size in ("short", "long"):
        group = [spec for spec in specs if spec.fixture_id.startswith(f"context.{size}.")]
        offsets = sorted(spec.messages[0]["content"].index("The box label is") for spec in group)
        assert all(right - left >= 32 for left, right in zip(offsets, offsets[1:]))
        for spec in group:
            coverage = spec.workload["position_coverage"]
            assert coverage["positions"] == [item.context_position for item in group]
            assert coverage["reduced"] is (len(group) < 3)
            assert coverage["minimum_separation_chars"] == 32
            assert spec.input_chars + spec.max_tokens + 32 <= context_limit
    assert len(specs) < 6 if context_limit == 128 else len(specs) == 6 if context_limit >= 200 else True


@pytest.mark.parametrize("scorer", ["tool", "judge", "unsupported"])
def test_legacy_fixtures_reject_unsupported_scorers_with_allowed_list(tmp_path, scorer):
    with pytest.raises(ValueError, match="Allowed scorers: exact, contains, json, none"):
        load_custom_fixtures(custom_file(tmp_path, [custom_entry(scorer=scorer)]))


@pytest.mark.parametrize("temperature", [None, 0.7])
@pytest.mark.asyncio
async def test_known_answer_fixture_temperature_reaches_endpoint_and_respects_user(monkeypatch, temperature):
    configured = config(structured_json=True, stream=False,
                        request_options={} if temperature is None else {"temperature": temperature})
    groups = builtin_fixtures(configured) | {f"traffic_{key}": value for key, value in traffic_fixtures(configured).items()}
    specs = [spec for group in groups.values() for spec in group if spec.scorer in {"exact", "contains", "json"}]
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "4"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    async with LLMClient(configured.targets[0], configured) as client:
        for spec in specs:
            await client.execute(spec)
    assert seen and all(payload["temperature"] == (0 if temperature is None else temperature) for payload in seen)
    assert all(spec.options.get("temperature") == 0 for spec in specs) if temperature is None else all(
        "temperature" not in spec.options for spec in specs)


@pytest.mark.parametrize("output,score", [
    ("", True), ("<think>Need the weather.</think>", True),
    ("<think>Need the weather.", None),
])
def test_parsed_tool_call_is_an_answer_after_closed_reasoning(output, score):
    spec = fixture("tool", None)
    result = score_response(spec, record(spec, output, finish_reason="tool_calls", tool_calls=[{
        "id": "call1", "type": "function", "function": {
            "name": "get_weather", "arguments": '{"city":"Delhi"}'}}]))
    assert result.score is score
    if score is None:
        assert result.score_message == "reasoning without final answer"


@pytest.mark.parametrize('unclosed,wrong_call,expected', [
    (False, False, True), (True, False, None), (True, True, False),
])
def test_tool_content_scores_final_text_without_hiding_call_failures(unclosed, wrong_call, expected):
    from giraffe.models import ToolExpectation
    from giraffe.tool_checks import default_tool_fixture

    fixture_config = default_tool_fixture()
    expectation = ToolExpectation(tools=fixture_config.tools,
        expected_calls=fixture_config.expected_calls,
        expected_content={'mode': 'exact', 'value': 'Checking Delhi.'})
    spec = fixture('tool', None, tool_expectation=expectation)
    output = '<think>Need weather.' if unclosed else '<think>Need weather.</think>Checking Delhi.'
    scored = score_response(spec, record(spec, output, finish_reason='tool_calls', tool_calls=[{
        'id': 'call1', 'type': 'function', 'function': {'name': 'get_weather',
        'arguments': json.dumps({'city': 'Paris' if wrong_call else 'Delhi'})}}]))
    assert scored.score is expected
    assert scored.output == output
    if wrong_call:
        assert scored.tool_evaluation.calls.status == 'fail'
    elif unclosed:
        assert scored.tool_evaluation.content.status == 'inconclusive'
    else:
        assert scored.tool_evaluation.content.status == 'pass'
