import copy
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from giraffe.fixtures import score_response
from giraffe.models import RequestRecord, RequestSpec, RunConfig, ToolFixture
from giraffe.tool_checks import resolved_tool_fixtures, resolved_tool_modes


def fixture(**updates):
    value = {
        "fixture_id": "weather-text", "messages": [{"role": "user", "content": "Check weather."}],
        "tools": [{"type": "function", "function": {"name": "weather", "parameters": {
            "type": "object", "properties": {"city": {"type": "string"}},
            "required": ["city"], "additionalProperties": False,
        }}}],
        "expected_calls": [{"name": "weather", "arguments": {"city": "Delhi"}}],
        "expected_content": {"mode": "exact", "value": "Checking now. Final sentence."},
    }
    return value | updates


def config(**updates):
    return RunConfig(targets=[{"name": "test", "url": "http://localhost", "model": "test"}],
                     checks=["tools"], **updates)


def scored(output="Checking now. Final sentence.", arguments='{"city":"Delhi"}', **updates):
    configured = ToolFixture.model_validate(fixture())
    request = RequestSpec(fixture_id="weather-text", scenario="tools", check_ids=["tools"],
                          messages=[], scorer="tool", tool_expectation=configured)
    values = {"id": "r", "target": "test", "fixture_id": request.fixture_id, "scenario": "tools",
              "check_ids": ["tools"], "started_at": "2026-01-01T00:00:00Z", "status": "completed",
              "valid": True, "output": output, "finish_reason": "tool_calls",
              "tool_calls": [{"type": "function", "function": {"name": "weather", "arguments": arguments}}]}
    return score_response(request, RequestRecord(**(values | updates)))


@pytest.mark.parametrize("output,passes", [
    ("Checking now. Final sentence.", True), ("Checking now.", False),
    ("Checking now. Final sentence. Final sentence.", False), ("", False),
    ("Checking now.  Final sentence.", False),
])
def test_mixed_expectations_require_complete_exact_text(output, passes):
    result = scored(output)
    assert result.score is passes
    assert result.tool_evaluation.content.status == ("pass" if passes else "fail")
    assert result.tool_evaluation.calls.status == "pass"
    assert result.tool_evaluation.protocol.status == "pass"
    assert result.evaluation.tool_expectation.expected_content.value == "Checking now. Final sentence."


def test_failed_call_does_not_hide_successful_content():
    result = scored(arguments='{"city":"Mumbai"}')
    assert result.score is False
    assert result.tool_evaluation.calls.status == "fail"
    assert result.tool_evaluation.content.status == "pass"


@pytest.mark.parametrize("arguments", ['{"city":"Delhi","city":"Delhi"}',
                                       '{"city":NaN}', '```json\n{"city":"Delhi"}\n```',
                                       '[]', '{"city":"Delhi","extra":1}'])
def test_call_arguments_must_be_strict_complete_schema_valid_json(arguments):
    assert scored(arguments=arguments).tool_evaluation.calls.status == "fail"


def test_incomplete_transport_does_not_score_partial_content_or_calls():
    result = scored(valid=False, status="failed")
    assert result.score is None
    assert result.tool_evaluation.protocol.status == "fail"
    assert result.tool_evaluation.content.status == "inconclusive"
    assert result.tool_evaluation.calls.status == "inconclusive"


def test_tool_only_default_and_mode_compatibility():
    configured = config()
    default = resolved_tool_fixtures(configured)[0]
    assert default.expected_content is None
    assert default.expected_calls[0].name == "get_weather"
    assert resolved_tool_modes(configured) == ["complete", "stream"]
    assert resolved_tool_modes(config(stream=False)) == ["complete"]
    assert resolved_tool_modes(config(tool_checks={"modes": ["stream"]})) == ["stream"]
    with pytest.raises(ValidationError, match="stream"):
        config(stream=False, tool_checks={"modes": ["stream"]})


@pytest.mark.parametrize("update", [
    {"fixture_id": " "}, {"expected_calls": []},
    {"expected_calls": [{"name": "missing", "arguments": {}}]},
    {"expected_calls": [{"name": "weather", "arguments": {"city": 4}}]},
    {"expected_content": {"mode": "contains", "value": ""}},
    {"options": {"tools": []}}, {"options": {"tool_choice": "none"}},
    {"options": {"stream": False}}, {"options": {"temperature": float("nan")}},
])
def test_invalid_tool_contracts_are_rejected(update):
    with pytest.raises(ValidationError):
        ToolFixture.model_validate(fixture(**update))


def test_local_schemas_only_and_duplicate_function_names():
    value = fixture()
    value["tools"][0]["function"]["parameters"] = {"$ref": "https://example.com/private"}
    with pytest.raises(ValidationError, match="no downloads"):
        ToolFixture.model_validate(value)
    value = fixture()
    value["tools"].append(copy.deepcopy(value["tools"][0]))
    with pytest.raises(ValidationError, match="unique"):
        ToolFixture.model_validate(value)


def test_config_requires_unique_bounded_cases_and_modes():
    with pytest.raises(ValidationError, match="unique"):
        config(tool_checks={"fixtures": [fixture(), fixture()]})
    for modes in ([], ["stream", "stream"]):
        with pytest.raises(ValidationError):
            config(tool_checks={"modes": modes})


def test_model_errors_do_not_echo_private_content():
    value = fixture(options={"tool_choice": "SECRET_PROMPT"})
    with pytest.raises(ValidationError) as caught:
        ToolFixture.model_validate(value)
    assert "SECRET_PROMPT" not in str(caught.value)


def test_explicit_empty_exact_and_schema_only_are_distinct_from_omission():
    value = ToolFixture.model_validate(fixture(
        expected_content={"mode": "exact", "value": ""},
        expected_calls=[{"name": "weather", "arguments": None}],
    ))
    assert value.expected_content.value == ""
    assert value.expected_calls[0].arguments is None
    assert json.loads(value.model_dump_json())["expected_content"]["value"] == ""


def evaluate_fixture(value, output, calls):
    configured = ToolFixture.model_validate(value)
    request = RequestSpec(fixture_id=configured.fixture_id, scenario="tools", check_ids=["tools"],
                          messages=[], scorer="tool", tool_expectation=configured)
    response = RequestRecord(id="r", target="test", fixture_id=request.fixture_id, scenario="tools",
                             check_ids=["tools"], started_at="2026-01-01", status="completed",
                             valid=True, output=output, tool_calls=calls, finish_reason="tool_calls")
    return score_response(request, response)


def call(name="weather", arguments=None):
    return {"type": "function", "function": {
        "name": name, "arguments": json.dumps({"city": "Delhi"} if arguments is None else arguments),
    }}


def test_content_normalization_and_contains_are_explicit_and_tool_only_is_unchecked():
    value = fixture(expected_content={"mode": "exact", "value": "Checking now. Final sentence.",
                                      "normalization": "whitespace"})
    assert evaluate_fixture(value, " Checking  now.\n Final sentence. ", [call()]).score is True
    value = fixture(expected_content={"mode": "contains", "value": "Final sentence."})
    result = evaluate_fixture(value, "Final sentence. Final sentence.", [call()])
    assert result.score is True
    assert "does not validate full content or duplication" in result.tool_evaluation.content.message
    value = fixture(expected_content=None)
    result = evaluate_fixture(value, "", [call()])
    assert result.score is True
    assert result.tool_evaluation.content.status == "not_checked"


def test_multiple_calls_are_compared_in_order_and_extra_calls_fail():
    value = fixture(expected_calls=[{"name": "weather", "arguments": {"city": "Delhi"}},
                                    {"name": "weather", "arguments": {"city": "Mumbai"}}])
    calls = [call(), call(arguments={"city": "Mumbai"})]
    assert evaluate_fixture(value, "Checking now. Final sentence.", calls).score is True
    assert evaluate_fixture(value, "Checking now. Final sentence.", calls[::-1]).score is False
    assert evaluate_fixture(value, "Checking now. Final sentence.", calls + [call()]).score is False


def test_schema_only_still_validates_shape_and_rejects_nonfinite_json():
    value = fixture(expected_calls=[{"name": "weather"}], expected_content=None)
    assert evaluate_fixture(value, "", [call(arguments={"city": "Mumbai"})]).score is True
    assert evaluate_fixture(value, "", [call(arguments={"city": 1})]).score is False
    value["tools"][0]["function"]["parameters"] = {"type": "object"}
    overflow = call()
    overflow["function"]["arguments"] = '{"number":1e999}'
    assert evaluate_fixture(value, "", [overflow]).score is False


def test_booleans_do_not_equal_expected_numbers():
    value = fixture(expected_calls=[{"name": "weather", "arguments": {"count": 1}}], expected_content=None)
    value["tools"][0]["function"]["parameters"] = {"type": "object"}
    assert evaluate_fixture(value, "", [call(arguments={"count": True})]).score is False


@pytest.mark.parametrize("expected,actual,matches", [
    (1, 1.0, True), (0, -0.0, True), (True, 1, False), (False, 0.0, False),
    ({"nested": [1, {"value": 0}]}, {"nested": [1.0, {"value": -0.0}]}, True),
    ({"nested": [1, {"value": False}]}, {"nested": [1.0, {"value": 0}]}, False),
])
def test_exact_arguments_compare_json_values_not_serializations(expected, actual, matches):
    value = fixture(expected_calls=[{"name": "weather", "arguments": {"value": expected}}], expected_content=None)
    value["tools"][0]["function"]["parameters"] = {"type": "object"}
    assert evaluate_fixture(value, "", [call(arguments={"value": actual})]).score is matches


def test_truncation_cannot_pass_even_when_observed_assertions_match():
    result = scored(finish_reason="length")
    assert result.score is False
    assert result.tool_evaluation.protocol.status == "fail"
    assert result.tool_evaluation.content.status == "pass"
    assert "Truncated tool-call completion" in result.score_message


def test_scoring_messages_never_embed_generated_private_text_or_arguments():
    result = scored(output="PRIVATE_OUTPUT", arguments='{"city":"PRIVATE_ARGUMENT"}')
    assert result.score is False
    assert result.tool_evaluation.content.status == result.tool_evaluation.calls.status == "fail"
    assert "PRIVATE" not in result.score_message
    assert "PRIVATE" not in result.tool_evaluation.model_dump_json()


def test_config_is_bounded_and_inactive_settings_do_not_override_global_stream():
    with pytest.raises(ValidationError, match="64 KiB"):
        config(tool_checks={"fixtures": [fixture(messages=[{"role": "user", "content": "x" * 65536}])]})
    with pytest.raises(ValidationError):
        config(tool_checks={"fixtures": [fixture(fixture_id=f"fixture-{i}") for i in range(17)]})
    inactive = RunConfig(targets=[{"name": "test", "url": "http://localhost", "model": "test"}],
                         checks=["correctness"], stream=False, tool_checks={"modes": ["stream"]})
    assert not inactive.tool_calling


def test_request_spec_rejects_ignored_tool_expectations():
    with pytest.raises(ValidationError, match="requires the tool scorer"):
        RequestSpec(fixture_id="t", scenario="t", check_ids=["tools"], messages=[],
                    tool_expectation=ToolFixture.model_validate(fixture()))
    with pytest.raises(ValidationError, match="tool_expectation instead"):
        RequestSpec(fixture_id="t", scenario="t", check_ids=["tools"], messages=[],
                    scorer="tool", expected="text that would be ignored")


def test_schema_recursion_is_inconclusive_not_an_incorrect_answer(monkeypatch):
    from giraffe import fixtures

    class RecursiveValidator:
        def validate(self, value):
            raise RecursionError

    monkeypatch.setattr(fixtures, "bounded_tool_schema", lambda schema, **kwargs: RecursiveValidator())
    result = scored()
    assert result.score is None
    assert result.tool_evaluation.calls.status == "inconclusive"
    assert result.tool_evaluation.content.status == "pass"
    assert result.evaluation_error == "schema_recursion"


@pytest.mark.parametrize("kind", ["regex", "branching", "error_tree"])
def test_pathological_schemas_reject_before_evaluation_in_bounded_subprocess(kind):
    # A regression must fail by process deadline instead of hanging pytest.
    script = r'''
import json, sys
from giraffe.models import ToolFixture
from pydantic import ValidationError
kind = sys.argv[1]
if kind == "regex":
    schema = {"type":"object", "properties":{"text":{"type":"string", "pattern":"^(a+)+$"}}}
    arguments = {"text":"a" * 30 + "!"}
elif kind == "error_tree":
    schema = {"type":"object", "properties":{"values":{"anyOf":[{"items":{"type":"string"}} for _ in range(125)]}}}
    arguments = {"values":[0] * 1000}
else:
    definitions = {"end":{"type":"null"}}
    prior = "end"
    for index in range(23):
        name = str(index)
        definitions[name] = {"anyOf":[{"$ref":"#/$defs/" + prior}, {"$ref":"#/$defs/" + prior}]}
        prior = name
    schema = {"$defs":definitions, "type":"object", "properties":{"text":{"$ref":"#/$defs/" + prior}}}
    arguments = {"text":"wrong"}
try:
    ToolFixture(fixture_id="t", messages=[{"role":"user", "content":"test"}],
        tools=[{"type":"function", "function":{"name":"f", "parameters":schema}}],
        expected_calls=[{"name":"f", "arguments":arguments}])
except ValidationError:
    print("rejected")
else:
    raise AssertionError("expensive schema was accepted")
'''
    result = subprocess.run([sys.executable, "-c", script, kind], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rejected"


@pytest.mark.parametrize("keyword,value", [
    ("pattern", "x"), ("patternProperties", {"x": {}}), ("format", "email"),
    ("uniqueItems", True), ("propertyNames", {}), ("contains", {}),
    ("if", {}), ("multipleOf", 1e-308), ("unknownAssertion", True),
])
def test_unsupported_tool_schema_keywords_reject_instead_of_weakening_assertions(keyword, value):
    item = fixture()
    item["tools"][0]["function"]["parameters"][keyword] = value
    with pytest.raises(ValidationError, match="unsupported tool schema keyword"):
        ToolFixture.model_validate(item)


@pytest.mark.parametrize("field_schema", [
    {"anyOf": [{"type": "string"}, {"type": "null"}]},
    {"type": ["string", "null"]},
    {"oneOf": [{"type": "string"}, {"type": "null"}]},
    {"allOf": [{"type": ["string", "null"]}]},
])
def test_bounded_nullable_combinations_and_schema_only_validation(field_schema):
    item = fixture(expected_calls=[{"name": "weather", "arguments": {"city": None}}], expected_content=None)
    item["tools"][0]["function"]["parameters"]["properties"]["city"] = field_schema
    assert evaluate_fixture(item, "", [call(arguments={"city": None})]).score is True
    item["expected_calls"][0]["arguments"] = None
    assert evaluate_fixture(item, "", [call(arguments={"city": "Delhi"})]).score is True
    assert evaluate_fixture(item, "", [call(arguments={"city": 3})]).score is False


def test_acyclic_local_refs_work_and_recursive_refs_are_rejected():
    item = fixture()
    schema = item["tools"][0]["function"]["parameters"]
    schema["$defs"] = {"city": {"type": "string"}}
    schema["properties"]["city"] = {"$ref": "#/$defs/city"}
    assert evaluate_fixture(item, "Checking now. Final sentence.", [call()]).score is True
    schema["$defs"]["city"] = {"$ref": "#/$defs/city"}
    with pytest.raises(ValidationError, match="recursive"):
        ToolFixture.model_validate(item)


@pytest.mark.parametrize("keyword", ["const", "enum"])
def test_schema_keyword_names_inside_literal_data_are_not_validation_keywords(keyword):
    literal = {"pattern": "text", "format": "text"}
    item = fixture(expected_calls=[{"name": "weather", "arguments": literal}], expected_content=None)
    item["tools"][0]["function"]["parameters"] = {keyword: [literal] if keyword == "enum" else literal}
    assert evaluate_fixture(item, "", [call(arguments=literal)]).score is True


@pytest.mark.parametrize("arguments", [
    {"text": "x" * 65536}, {"values": list(range(1025))},
])
def test_argument_budget_breaches_are_inconclusive(arguments):
    item = fixture(expected_calls=[{"name": "weather"}], expected_content=None)
    item["tools"][0]["function"]["parameters"] = {"type": "object"}
    result = evaluate_fixture(item, "", [call(arguments=arguments)])
    assert result.score is None
    assert result.tool_evaluation.calls.status == "inconclusive"
    assert result.evaluation_error == "tool_evaluation_budget"


def test_argument_depth_budget_breach_is_inconclusive():
    arguments = {}
    for _ in range(34):
        arguments = {"next": arguments}
    item = fixture(expected_calls=[{"name": "weather"}], expected_content=None)
    item["tools"][0]["function"]["parameters"] = {"type": "object"}
    assert evaluate_fixture(item, "", [call(arguments=arguments)]).tool_evaluation.calls.status == "inconclusive"


def test_combinator_error_tree_work_is_bounded_for_actual_and_expected_arguments():
    item = fixture(expected_calls=[{"name": "weather"}], expected_content=None)
    item["tools"][0]["function"]["parameters"] = {
        "type": "object", "properties": {"values": {"anyOf": [{"items": {"type": "string"}} for _ in range(125)]}},
    }
    arguments = {"values": [0] * 1000}
    result = evaluate_fixture(item, "", [call(arguments=arguments)])
    assert result.tool_evaluation.calls.status == "inconclusive"
    assert result.evaluation_error == "tool_evaluation_budget"
    item["expected_calls"][0]["arguments"] = arguments
    with pytest.raises(ValidationError, match="combined evaluation work limit"):
        ToolFixture.model_validate(item)


def test_schema_depth_rejected_before_generic_meta_validation(monkeypatch):
    from giraffe import models
    from giraffe.tool_schema import bounded_tool_schema

    def must_not_run(*args, **kwargs):
        raise AssertionError("generic validator ran before the resource guard")

    monkeypatch.setattr(models, "validated_json_schema", must_not_run)
    schema = {"type": "string"}
    for _ in range(34):
        schema = {"properties": {"child": schema}}
    with pytest.raises(ValueError, match="node/depth limit"):
        bounded_tool_schema(schema)
