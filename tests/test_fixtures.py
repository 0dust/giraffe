import json

import pytest

from giraffe.fixtures import FIXTURE_VERSION, builtin_fixtures, load_custom_fixtures, score_response
from giraffe.models import RequestRecord, RequestSpec, RunConfig, Target


def config(**overrides):
    return RunConfig(targets=[Target(name="local", url="http://localhost:8000", model="test")], **overrides)


def record(spec, output, **overrides):
    values = dict(
        id="request-1", target="local", fixture_id=spec.fixture_id, scenario=spec.scenario,
        check_ids=spec.check_ids, started_at="2026-10-04T00:00:00Z", status="completed",
        valid=True, output=output,
    )
    return RequestRecord(**(values | overrides))


def custom_file(tmp_path, value):
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(value))
    return str(path)


def custom_entry(**overrides):
    return {
        "fixture_id": "invoice-code", "messages": [{"role": "user", "content": "Return X7."}],
        "scorer": "exact", "expected": "X7", **overrides,
    }


def test_builtins_repeatable_versioned_and_json_opt_in():
    first, second = builtin_fixtures(config()), builtin_fixtures(config())
    assert first == second
    assert set(first) == {"short", "long", "context", "correctness", "json", "limits", "sustained", "generation"}
    assert first["json"] == []
    specs = [spec for group in first.values() for spec in group]
    assert len({spec.fixture_id for spec in specs}) == len(specs)
    assert all(spec.fixture_id.endswith((".v1", ".v2")) and spec.check_ids for spec in specs)
    assert all(spec.expected is not None for spec in specs)
    assert FIXTURE_VERSION == "0.2.0"
    assert builtin_fixtures(config(structured_json=True))["json"]


def test_traffic_mix_has_scored_and_bounded_short_input_long_input_and_long_output():
    from giraffe.fixtures import traffic_fixtures

    configured = config(max_output_tokens=40,
                        traffic={"long_input_chars": 2000, "long_output_words": 24})
    fixtures = traffic_fixtures(configured)
    assert set(fixtures) == {"short", "long_input", "long_output"}
    assert fixtures["long_input"][0].input_chars == 2000
    assert len(fixtures["long_output"][0].expected.split()) == 24
    assert all(spec.scorer == "exact" and spec.expected and spec.max_tokens <= 40
               for group in fixtures.values() for spec in group)
    assert traffic_fixtures(configured) == fixtures


@pytest.mark.parametrize("context_limit", [128, 512, 4096, 32768])
def test_context_lengths_are_bounded_and_fact_positions_vary(context_limit):
    configured = config(context_limit=context_limit)
    fixtures = builtin_fixtures(configured)
    context = fixtures["context"]
    assert len(context) == 6
    assert {spec.context_position for spec in context} == {"beginning", "middle", "end"}
    assert len({spec.input_chars for spec in context}) == 2
    for spec in context + fixtures["long"]:
        text = spec.messages[0]["content"]
        assert spec.input_chars == len(text)
        assert len(text) + spec.max_tokens + 32 <= context_limit
        assert text.count(f"The box label is {spec.expected}.") == 1
        body = text.removeprefix("Notes:\n").removesuffix("\nReply with the box label only.")
        fact = f"The box label is {spec.expected}."
        if spec.context_position == "beginning":
            assert body.startswith(fact)
        elif spec.context_position == "end":
            assert body.endswith(fact)
        else:
            assert abs(body.index(fact) - (len(body) - len(fact)) // 2) <= 1


def test_output_limits_and_nonstreaming_config_apply_to_all_builtins():
    fixtures = builtin_fixtures(config(max_output_tokens=2, stream=False, structured_json=True))
    specs = [spec for group in fixtures.values() for spec in group]
    assert all(spec.max_tokens <= 2 and not spec.stream for spec in specs)
    assert any(spec.cancel_after_ms for spec in fixtures["limits"])
    assert any(spec.options.get("stop") for spec in fixtures["limits"])


@pytest.mark.parametrize("output,expected", [
    ("25", True), (" 25\n", True), ("125", False), ("250", False),
    ("The answer is 25", False), ("25\n26", False), ("26", False),
])
def test_arithmetic_uses_the_whole_answer(output, expected):
    spec = builtin_fixtures(config())["correctness"][1]
    assert score_response(spec, record(spec, output)).score is expected


def test_extraction_and_classification_do_not_accept_reasoning_or_wrong_values():
    extraction, _, classification = builtin_fixtures(config())["correctness"]
    assert score_response(extraction, record(extraction, "T8R2")).score is True
    assert score_response(extraction, record(extraction, "T8R3", reasoning="T8R2")).score is False
    assert score_response(classification, record(classification, "A")).score is False
    assert score_response(classification, record(classification, "B")).score is True


def test_reworded_fixtures_still_reject_refusals_and_extra_text():
    fixtures = builtin_fixtures(config())
    specs = (fixtures["short"] + fixtures["long"] + fixtures["context"]
             + [fixtures["correctness"][0]] + fixtures["sustained"])
    for spec in specs:
        assert spec.fixture_id.endswith(".v2")
        assert score_response(spec, record(spec, spec.expected)).score is True
        for output in ("I can't fulfill this request.", f"The answer is {spec.expected}"):
            assert score_response(spec, record(spec, output)).score is False


def test_scoring_preserves_original_record_and_does_not_pass_incomplete_requests():
    spec = builtin_fixtures(config())["short"][0]
    original = record(spec, spec.expected)
    scored = score_response(spec, original)
    assert scored.score is True and original.score is None
    for overrides in [{"status": "timeout"}, {"status": "failed"}, {"valid": False}]:
        assert score_response(spec, record(spec, spec.expected, **overrides)).score is None
    cap = builtin_fixtures(config())["limits"][0]
    assert score_response(cap, record(cap, "1")).score is None


@pytest.mark.parametrize("output,expected", [
    ('{"label":"J6Q2","count":3,"ready":true}', True),
    ('```json\n{"label":"J6Q2","count":3,"ready":true}\n```', True),
    ('{"label":"J6Q2","count":"3","ready":true}', False),
    ('{"label":"J6Q2","count":3}', False),
    ('{"label":"other","count":3,"ready":true}', False),
    ('{"label":"J6Q2","count":3,"ready":false}', False),
    ('{"label":"J6Q2","count":3,"ready":true,"extra":1}', False),
    ('Result: {"label":"J6Q2","count":3,"ready":true}', False),
    ('{"label":"J6Q2","count":3,"ready":true} trailing', False),
    ('{"label":"bad","label":"J6Q2","count":3,"ready":true}', False),
    ('{"label":"J6Q2","count":NaN,"ready":true}', False),
    ('{"label":"J6Q2","count":true,"ready":true}', False),
])
def test_json_parsing_schema_and_expected_values(output, expected):
    spec = builtin_fixtures(config(structured_json=True))["json"][0]
    assert score_response(spec, record(spec, output)).score is expected


def test_custom_defaults_and_input_character_count(tmp_path):
    fixtures = load_custom_fixtures(custom_file(tmp_path, [custom_entry(input_chars=999)]))
    spec = fixtures[0]
    assert spec.scenario == "custom" and spec.check_ids == ["correctness"]
    assert spec.input_chars == len("Return X7.")
    assert score_response(spec, record(spec, "X7")).score is True


def test_custom_schema_only_and_local_references(tmp_path):
    schema = {
        "type": "object", "properties": {"code": {"$ref": "#/$defs/code"}},
        "required": ["code"], "$defs": {"code": {"type": "string", "enum": ["A", "B"]}},
    }
    entry = custom_entry(scorer="json", expected=None, schema=schema)
    spec = load_custom_fixtures(custom_file(tmp_path, [entry]))[0]
    assert score_response(spec, record(spec, '{"code":"A"}')).score is True
    assert score_response(spec, record(spec, '{"code":"C"}')).score is False


@pytest.mark.parametrize("key", ["model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"])
def test_custom_options_cannot_override_run_controls(tmp_path, key):
    with pytest.raises(ValueError, match="cannot override"):
        load_custom_fixtures(custom_file(tmp_path, [custom_entry(options={key: 100})]))


@pytest.mark.parametrize("change,match", [
    ({"fixture_id": " "}, "fixture_id"),
    ({"messages": []}, "messages"),
    ({"messages": [{"role": "tool", "content": "text"}]}, "roles"),
    ({"messages": [{"role": "user", "content": [{"type": "image_url"}]}]}, "text"),
    ({"scorer": "exact", "expected": None}, "expected answer"),
    ({"scorer": "contains", "expected": ""}, "expected answer"),
    ({"scorer": "json", "schema": None}, "requires a schema"),
    ({"scorer": "json", "schema": {"type": "nonsense"}}, "Custom fixture 1"),
    ({"scorer": "json", "schema": {"$ref": "https://example.org/schema.json"}}, "no downloads"),
    ({"check_ids": ["invented"]}, "supported check names"),
    ({"unexpected": True}, "Extra inputs"),
])
def test_invalid_custom_fixtures_have_actionable_errors(tmp_path, change, match):
    with pytest.raises(ValueError, match=match):
        load_custom_fixtures(custom_file(tmp_path, [custom_entry(**change)]))


@pytest.mark.parametrize("value", [{}, [], ["bad"], [custom_entry(), custom_entry()]])
def test_custom_file_structure_and_duplicates_are_validated(tmp_path, value):
    with pytest.raises(ValueError):
        load_custom_fixtures(custom_file(tmp_path, value))


def test_malformed_or_missing_custom_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    with pytest.raises(ValueError, match="Cannot read custom fixtures"):
        load_custom_fixtures(str(path))
    with pytest.raises(ValueError, match="Cannot read custom fixtures"):
        load_custom_fixtures(str(tmp_path / "missing.json"))


def test_custom_stop_option_is_allowed(tmp_path):
    entry = custom_entry(options={"stop": ["END"]}, max_tokens=4)
    assert load_custom_fixtures(custom_file(tmp_path, [entry]))[0].options == {"stop": ["END"]}


def test_unresolvable_local_schema_reference_is_a_failed_score():
    spec = RequestSpec(
        fixture_id="bad-reference", scenario="custom", check_ids=["json"],
        messages=[{"role": "user", "content": "Return JSON."}], scorer="json",
        schema={"$ref": "#/$defs/missing"},
    )
    result = score_response(spec, record(spec, "{}"))
    assert result.score is False and "schema" in result.score_message
