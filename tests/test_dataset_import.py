import json

import pytest
from pydantic import ValidationError

from giraffe.dataset_import import preview_dataset, try_example_config
from giraffe.models import ConfigDraft, RunConfig
from giraffe.request_pack import pack_specs
from tests.test_judge_interfaces import judged_config


def test_empty_dataset_is_an_editable_draft_but_not_runnable():
    values = judged_config() | {"request_pack": None}
    assert ConfigDraft.model_validate(values).request_pack is None
    with pytest.raises(ValidationError, match="request_pack is required"):
        RunConfig.model_validate(values)


def test_csv_requires_mapping_then_preserves_quoted_values():
    text = 'question,answer\n"Hello,\nworld",yes\n'
    result = preview_dataset(text, "csv")
    assert result["needs_mapping"] and result["request_pack"] is None
    assert result["suggested_mapping"] == {"input_field": "question", "expected_field": "answer"}
    result = preview_dataset(text, "csv", input_field="question", expected_field="answer")
    case = result["request_pack"]["cases"][0]
    assert case["messages"][0]["content"] == "Hello,\nworld"
    assert case["expected"] == "yes" and case["scorer"] == "judge"


@pytest.mark.parametrize("text,format,field,row", [
    ('question,question\nx,y', 'csv', 'columns', None),
    ('question,\nx,y', 'csv', 'columns', None),
    ('question,answer\nx,y\n\n', 'csv', 'row', 2),
    ('question,answer\nx', 'csv', 'row', 1),
    ('[{"question":"x","answer":null}]', 'json', 'answer', 1),
    ('[{"question":"x","answer":{"nested":true}}]', 'json', 'answer', 1),
])
def test_import_errors_are_structured_and_never_drop_rows(text, format, field, row):
    result = preview_dataset(text, format, input_field="question", expected_field="answer")
    assert result["request_pack"] is None
    assert any(error["field"] == field and error["row"] == row for error in result["errors"])


def test_canonical_import_preserves_mixed_cases():
    pack = judged_config()["request_pack"]
    pack["cases"].append({"fixture_id": "id", "messages": [{"role": "user", "content": "ID"}],
                          "scorer": "exact", "expected": "A1", "weight": 2,
                          "max_tokens": 20, "options": {"temperature": 0}})
    result = preview_dataset(json.dumps(pack), "json")
    assert not result["errors"] and not result["needs_mapping"]
    for before, after in zip(pack["cases"], result["request_pack"]["cases"], strict=True):
        assert all(after[key] == value for key, value in before.items())


def test_bounded_import_and_duplicate_json_keys():
    assert preview_dataset("x" * (1024 * 1024 + 1), "csv")["errors"]
    assert preview_dataset(json.dumps([{"q": "x"}] * 1001), "json")["errors"]
    assert preview_dataset('[{"q":"x","q":"y"}]', "json")["errors"]


def test_try_one_preserves_effective_payload_and_ignores_other_draft_models():
    values = judged_config()
    values["targets"].append({"name": "unfinished", "url": "http://localhost:8999", "model": ""})
    values["max_output_tokens"] = 24
    values["request_timeout_seconds"] = 2
    values["restart_target"] = "local"
    draft = ConfigDraft.model_validate(values)
    original = pack_specs(draft)[0]
    selected = try_example_config(draft, "refund", "local")
    assert selected.checks == ["correctness"] and selected.max_requests == 1
    assert selected.restart_target is None and len(selected.targets) == 1
    assert selected.judging == draft.judging
    assert pack_specs(selected)[0].max_tokens == original.max_tokens == 24
    assert pack_specs(selected)[0].request_timeout_seconds == original.request_timeout_seconds == 2
    assert draft.restart_target == "local" and len(draft.targets) == 2


def test_canonical_json_expected_presence_and_schema_alias_are_preserved():
    cases = [{"fixture_id": name, "messages": [{"role": "user", "content": "JSON"}],
              "scorer": "json", "schema": {"type": "null"}, **expected}
             for name, expected in (("absent", {}), ("null", {"expected": None}))]
    imported = preview_dataset(json.dumps({"version": 1, "cases": cases}), "json")["request_pack"]
    assert "expected" not in imported["cases"][0]
    assert imported["cases"][1]["expected"] is None
    assert imported["cases"][0]["schema"] == {"type": "null"}


def test_try_one_preserves_explicit_retention():
    config = try_example_config(ConfigDraft.model_validate(judged_config() | {"retention": "none"}),
                                "refund", "local")
    assert config.retention == "none"


@pytest.mark.parametrize("canonical", [False, True])
def test_dataset_preview_warns_when_exact_answer_is_in_messages(canonical):
    text = json.dumps({"version": 1, "cases": [{"fixture_id": "echo", "scorer": "contains", "expected": "4",
        "messages": [{"role": "user", "content": "Return 4."}]}]}) if canonical else 'question,answer\nReturn 4.,4\n'
    result = preview_dataset(text, "json" if canonical else "csv", scorer="exact",
                             **({} if canonical else {"input_field": "question", "expected_field": "answer"}))
    assert result["request_pack"] and not result["errors"]
    assert any("echo would pass" in warning for warning in result["warnings"])
