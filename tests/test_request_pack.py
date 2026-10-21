import json
import math
from collections import Counter

import pytest
from pydantic import ValidationError

from giraffe.models import ConfigDraft, RequestPack, RunConfig, Target, TrafficConfig
from giraffe.request_pack import (active_pack, case_sequence, load_request_pack,
                                  pack_identity, pack_preview, pack_specs)


def case(fixture_id="one", **updates):
    return {"fixture_id": fixture_id, "messages": [{"role": "user", "content": "Return A"}],
            "scorer": "exact", "expected": "A", **updates}


def config(cases=None, **updates):
    traffic = TrafficConfig.model_validate(updates.get("traffic", {}))
    return RunConfig(targets=[Target(name="local", url="http://localhost", model="test")],
        checks=["load", "correctness"], request_source="request_pack",
        request_pack={"version": 1, "cases": cases or [case()]},
        load_test={"workloads": {"combinations": {"selected": ["short_short"]}},
            "patterns": {"concurrent": {"enabled": False},
                "constant": {"enabled": traffic.arrival == "steady"},
                "poisson": {"enabled": traffic.arrival == "poisson", "seed": traffic.seed},
                "rates": traffic.rates},
            "schedule": {"duration_seconds": math.ceil(traffic.duration_seconds)}}, **updates)


@pytest.mark.parametrize("updates", [
    {"weight": 0}, {"weight": -1}, {"weight": float("inf")}, {"weight": float("nan")},
    {"fixture_id": " "}, {"messages": []},
    {"messages": [{"role": "tool", "content": "private content"}]},
    {"messages": [{"role": "user", "content": [{"type": "image"}]}]},
    {"messages": [{"role": "user", "content": "ok", "other": True}]},
    {"scenario": "mine"}, {"stream": False}, {"scorer": "tool"},
    {"scorer": "contains", "expected": " "}, {"scorer": "json", "expected": None},
    {"options": {"n": 2}}, {"options": {"extra": {"api_key": "private content"}}},
    {"options": {"headers": {"Authorization": "private content"}}},
])
def test_invalid_case_contract(updates):
    with pytest.raises(ValidationError):
        config([case(**updates)])


@pytest.mark.parametrize("schema,expected", [
    ({"type": "nonsense"}, None),
    ({"type": "integer"}, "private content"),
    ({"$ref": "https://example.org/remote"}, None),
    ({"$defs": {"unused": {"$ref": "#/$defs/missing"}}}, None),
    ({"$ref": "#missing"}, None),
])
def test_invalid_schema_is_rejected_before_a_request(schema, expected):
    with pytest.raises(ValidationError):
        config([case(scorer="json", schema=schema, expected=expected)])


def test_portable_pack_roundtrip_and_local_reference(tmp_path):
    schema = {"$defs": {"answer": {"type": "integer"}}, "$ref": "#/$defs/answer"}
    path = tmp_path / "pack.json"
    path.write_text(json.dumps({"version": 1, "cases": [case(scorer="json", schema=schema, expected=4)]}))
    pack = load_request_pack(path)
    configured = config(pack.cases)
    path.unlink()
    assert RunConfig.model_validate(configured.model_dump()) == configured
    assert configured.model_dump()["request_pack"]["cases"][0]["schema"] == schema
    spec = pack_specs(configured)[0]
    assert spec.fixture_id == "pack:one" and spec.workload["case_id"] == "one"
    assert spec.schema_ == schema and spec.expected == 4


def test_bounded_import_and_safe_indexed_errors(tmp_path):
    path = tmp_path / "pack.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        load_request_pack(path)
    path.write_text(json.dumps({"version": 1, "cases": [case(expected={"secret": "PRIVATE_BODY"})]}))
    with pytest.raises(ValueError) as caught:
        load_request_pack(path)
    assert "cases.0" in str(caught.value)
    assert "PRIVATE_BODY" not in str(caught.value)
    with pytest.raises(ValidationError):
        RequestPack(version=1, cases=[case(str(n)) for n in range(1001)])
    with pytest.raises(ValidationError):
        config([case(), case()])
    with pytest.raises(ValidationError):
        config([case(messages=[{"role": "user", "content": "x" * (1024 * 1024)}])])


def test_source_selection_and_legacy_conflict():
    configured = config()
    builtin = RunConfig.model_validate({**configured.model_dump(), "request_source": "builtin"})
    assert not active_pack(builtin) and pack_identity(builtin) is None
    assert not pack_specs(builtin)
    with pytest.raises(ValidationError, match="request_pack"):
        RunConfig.model_validate({**configured.model_dump(), "request_pack": None})
    with pytest.raises(ValidationError, match="custom_fixtures"):
        config(custom_fixtures="does-not-exist.json")
    draft = ConfigDraft.model_validate({**configured.model_dump(), "checks": [],
                                      "targets": [{"name": "local", "url": "http://localhost", "model": ""}]})
    assert not active_pack(draft) and pack_identity(draft) is None
    assert pack_preview(draft)["quiet_requests"] == 0


def test_explicit_case_output_budget_overrides_suite_default_and_options_merge():
    configured = config([case(max_tokens=999, options={"temperature": .2})],
                        max_output_tokens=32, stream=False,
                        request_options={"temperature": .8, "stop": ["END"]})
    spec = pack_specs(configured)[0]
    assert spec.max_tokens == 999 and spec.stream is False
    assert spec.options == {"temperature": .2, "stop": ["END"]}
    preview = pack_preview(configured)
    assert preview["cases"][0]["effective"]["max_tokens"] == 999
    assert preview["cases"][0]["requested"]["max_tokens"] == 999
    assert preview["quiet_requests"] == 1


def test_effective_identity_ignores_order_weight_scale_and_inactive_settings():
    first = config([case("a", weight=1), case("b", weight=3)])
    second = config([case("b", weight=6), case("a", weight=2)],
                    traffic={"mix": {"short": 0, "long_input": 1, "long_output": 0},
                             "long_input_chars": 5000},
                    test_options={"json": {"samples": 8}})
    assert pack_identity(first) == pack_identity(second)
    for updates in ({"expected": "B"}, {"weight": 2}, {"options": {"temperature": .3}},
                    {"messages": [{"role": "user", "content": "Return B"}]}):
        assert pack_identity(first) != pack_identity(config([case("a", **updates), case("b", weight=3)]))


def test_weighted_sequence_preserves_mix_without_forcing_coverage():
    configured = config([case("rare", weight=1), case("common", weight=99)],
                        traffic={"rates": [1], "duration_seconds": 2})
    assert Counter(s.workload["case_id"] for s in case_sequence(configured, 2, 0)) == {"common": 2}
    sequence = case_sequence(configured, 100, 0)
    assert Counter(s.workload["case_id"] for s in sequence) == {"common": 99, "rare": 1}
    assert sequence == case_sequence(configured, 100, 0)
    preview = pack_preview(configured)
    assert preview["load_test"]["expected_scheduled_requests"] == 2
    assert preview["stages"] == []
    assert preview["warnings"]


def test_content_is_not_sanitized_and_unscored_is_explicit():
    content = "api_key=sample text " + "x" * 20000
    configured = config([case(messages=[{"role": "user", "content": content}],
                            options={"response_format": {"type": "json_schema", "json_schema": {
                                "name": "answer", "schema": {"type": "object", "properties": {
                                    "token": {"type": "string"}}}}}})])
    assert pack_specs(configured)[0].messages[0]["content"] == content
    assert config([case(scorer="none", expected=None)]).request_pack.cases[0].scorer == "none"


def test_explicit_null_expected_is_validated_against_schema():
    with pytest.raises(ValidationError, match="expected"):
        config([case(scorer="json", schema={"type": "string"}, expected=None)])


def test_expected_presence_survives_export_and_changes_scoring_and_identity():
    from giraffe.fixtures import score_response
    from giraffe.models import RequestRecord

    absent = case(scorer="json", schema={})
    del absent["expected"]
    omitted = config([absent])
    explicit = config([case(scorer="json", schema={}, expected=None)])
    assert "expected" not in omitted.model_dump()["request_pack"]["cases"][0]
    assert explicit.model_dump()["request_pack"]["cases"][0]["expected"] is None
    assert pack_identity(omitted) != pack_identity(explicit)
    for original, wrong_passes in ((omitted, True), (explicit, False)):
        for configured in (original, RunConfig.model_validate_json(original.model_dump_json())):
            spec = pack_specs(configured)[0]
            record = RequestRecord(id="test", target="local", fixture_id=spec.fixture_id,
                                   scenario=spec.scenario, check_ids=spec.check_ids,
                                   started_at="2026-10-11T00:00:00Z", status="completed",
                                   valid=True, output='"wrong"')
            assert score_response(spec, record).score is wrong_passes
            assert score_response(spec, record.model_copy(update={"output": "null"})).score is True


@pytest.mark.asyncio
async def test_global_response_schema_survives_validation_preview_export_and_wire(monkeypatch):
    import httpx
    from giraffe.client import LLMClient

    options = {"response_format": {"type": "json_schema", "json_schema": {
        "name": "answer", "schema": {"type": "object", "properties": {
            "password": {"type": "string"}, "token": {"type": "string"}}}}}}
    configured = config(request_options=options, stream=False)
    assert configured.request_options == options
    configured = RunConfig.model_validate_json(configured.model_dump_json())
    assert pack_preview(configured)["cases"][0]["effective"]["options"] == options
    received = []

    def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "A"}, "finish_reason": "stop"}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))
    async with LLMClient(configured.targets[0], configured) as client:
        record = await client.execute(pack_specs(configured)[0])
    assert record.valid
    assert received[0]["response_format"] == options["response_format"]


def test_saved_pack_config_preserves_global_schema_and_rejects_credentials():
    from giraffe.deployment import safe_config

    options = {"response_format": {"type": "json_schema", "json_schema": {
        "name": "answer", "schema": {"type": "object", "properties": {
            "password": {"type": "string"}, "token": {"type": "string"}}}}}}
    original = config(request_options=options)
    saved = safe_config(original.model_dump())
    restored = RunConfig.model_validate(json.loads(json.dumps(saved)))
    assert restored.request_options == options
    assert pack_specs(restored) == pack_specs(original)
    saved["request_options"] = {"api_key": "secret"}
    with pytest.raises(ValueError):
        safe_config(saved)


def judge_config(**updates):
    return {"judges": [{"name": "reviewer", "url": "http://localhost:8001/v1", "model": "judge"}], **updates}


def test_judge_case_contract_and_portable_draft():
    judged = case(scorer="judge", expected="Refund within thirty days", rubric="Require the time window")
    draft_data = {"targets": [{"name": "local", "url": "http://localhost", "model": ""}],
                  "checks": ["correctness"], "request_source": "request_pack",
                  "request_pack": {"version": 1, "cases": [judged]},
                  "judging": {"judges": [{"name": "reviewer", "url": "http://localhost", "model": ""}]}}
    draft = ConfigDraft.model_validate(draft_data)
    assert ConfigDraft.model_validate_json(draft.model_dump_json()) == draft
    assert pack_specs(draft)[0].rubric == judged["rubric"]
    with pytest.raises(ValidationError):
        RunConfig.model_validate({**draft_data, "targets": [{"name": "local", "url": "http://localhost", "model": "test"}]})
    with pytest.raises(ValidationError, match="judg"):
        config([judged])
    for invalid in (case(scorer="judge", expected=" "), case(rubric="Only judges"),
                    case(scorer="judge", rubric=" ")):
        with pytest.raises(ValidationError):
            config([invalid], judging=judge_config())


def test_judge_identity_is_effective_and_order_independent():
    judges = [{"name": name, "url": "http://localhost/v1", "model": name} for name in ("a", "b")]
    original = config([case(scorer="judge", rubric="Accept paraphrases")], judging=judge_config(judges=judges))
    reordered = config([case(scorer="judge", rubric="Accept paraphrases")],
                       judging=judge_config(judges=list(reversed(judges))))
    assert pack_identity(original) == pack_identity(reordered)
    for change in ({"judging": judge_config()},
                   {"request_pack": {"version": 1, "cases": [case(scorer="judge", rubric="Require units")]}}):
        changed = RunConfig.model_validate(original.model_dump() | change)
        assert pack_identity(original) != pack_identity(changed)
    deterministic = config()
    inactive = config(judging={"judges": [{"model": ""}]})
    assert pack_identity(deterministic) == pack_identity(inactive)
    assert pack_preview(inactive)["judging"]["planned_call_upper_bound"] == 0


def test_judge_preview_counts_only_eligible_cases_and_distinguishes_quota():
    configured = config([case("judge", scorer="judge"), case("exact")], judging=judge_config(),
                        traffic={"rates": [2], "duration_seconds": 2}, max_requests=3)
    preview = pack_preview(configured)["judging"]
    assert preview["planned_call_upper_bound"] == 5  # one judged quiet + up to four judged offers
    assert preview["quota_call_upper_bound"] == 3
    assert preview["judge_count"] == 1
    assert preview["active"] is True


def test_judge_preview_is_read_only_draft_readiness_and_panel_bounds(monkeypatch):
    from giraffe import request_pack

    judged = config([case(scorer="judge")], judging=judge_config(),
                    traffic={"rates": [2], "duration_seconds": 2}, max_requests=3)
    original_identity = pack_identity(judged)
    monkeypatch.setattr(request_pack, "JUDGE_PROMPT_VERSION", "changed-policy")
    assert pack_identity(judged) != original_identity
    draft = ConfigDraft.model_validate(judged.model_dump() | {"judging": {"judges": [{"name": "first"}]}})
    assert not pack_preview(draft)["judging"]["ready"]
    assert pack_preview(draft)["judging"]["readiness_errors"] == ["Judge 1 needs: url, model."]
    two_targets = RunConfig.model_validate(judged.model_dump() | {
        "targets": [{"name": name, "url": "http://localhost", "model": "target"} for name in ("a", "b")],
        "judging": judge_config(judges=[{"name": name, "url": "http://localhost", "model": "judge"}
                                        for name in ("a", "b")]),
    })
    preview = pack_preview(two_targets)["judging"]
    assert preview["planned_call_upper_bound"] == 20
    assert preview["quota_call_upper_bound"] == 6
    assert preview["ready"]


@pytest.mark.parametrize("arrival", ["steady", "poisson"])
@pytest.mark.parametrize("checks", [["load", "correctness"], ["correctness"], ["load"]])
@pytest.mark.parametrize("ready", [True, False])
def test_final_pack_summary_matches_preview_and_evaluator_bounds(arrival, checks, ready):
    from giraffe.request_pack import pack_summary
    configured = config([case("judged", scorer="judge", weight=3, max_tokens=128),
                         case("exact", weight=1, max_tokens=16)],
                        judging=judge_config(), traffic={"rates": [1.5, 3], "duration_seconds": 2,
                                                       "arrival": arrival},
                        max_requests=3, max_output_tokens=32, stream=False)
    configured = ConfigDraft.model_validate(configured.model_dump() | {
        "checks": checks,
        "targets": [{"name": name, "url": "http://localhost/v1", "model": ""} for name in ("a", "b")],
        "judging": {"judges": [{"name": name, "url": "http://localhost/v1" if ready else "",
                                 "model": "judge" if ready else ""} for name in ("one", "two")]},
    })
    preview, summary = pack_preview(configured), pack_summary(configured)
    expected = {key: preview[key] for key in summary}
    expected["judging"] = {key: value for key, value in preview["judging"].items() if key != "identity"}
    assert summary == expected
    assert summary["request_cap"] == 3 and summary["per_target_request_quota"] == {"a": 2, "b": 1}
    assert summary["quiet_requests"] == (2 if "correctness" in checks else 0)
    assert summary["stages"] == []
    assert summary["judging"]["ready"] is ready
    if "load" in checks:
        assert summary["load_test"]["expected_scheduled_requests"] == 9
        assert summary["judging"]["planned_requests_approximate"] == (arrival == "poisson")
        if arrival == "poisson":
            assert summary["judging"]["planned_call_upper_bound"] == summary["judging"]["quota_call_upper_bound"] == 6
        else:
            # Weighted random selection may choose the judged case on every request.
            assert summary["judging"]["planned_call_upper_bound"] == (40 if "correctness" in checks else 36)
            assert summary["judging"]["quota_call_upper_bound"] == 6
    else:
        assert summary["judging"]["planned_call_upper_bound"] == summary["judging"]["quota_call_upper_bound"] == 4


@pytest.mark.parametrize("scorer", ["exact", "contains"])
def test_pack_preview_warns_about_expected_text_in_own_messages_without_rejecting(scorer):
    configured = config([case("echo", scorer=scorer, expected="4", messages=[
        {"role": "system", "content": "Return 4."}, {"role": "user", "content": "Copy the number."}])])
    warnings = pack_preview(configured)["warnings"]
    assert any("echo" in warning and "echo would pass" in warning for warning in warnings)
    safe = config([case("safe", scorer=scorer, expected="4", messages=[{"role": "user", "content": "Two plus two?"}])])
    assert not any("echo would pass" in warning for warning in pack_preview(safe)["warnings"])


async def test_final_load_judge_upper_bound_covers_weighted_random_cases(monkeypatch):
    from giraffe import judging, runner
    from tests.test_runner import FakeClient, judge_summary
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)

    async def evaluate(config, records, specs, **kwargs):
        for record in records:
            record.score = True
        return judge_summary(records)

    monkeypatch.setattr(judging, 'judge_records', evaluate)
    configured = config([case('judged', scorer='judge', weight=99), case('exact', weight=1)],
        judging=judge_config(), max_requests=None,
        traffic={'rates': [20], 'duration_seconds': 1})
    preview = pack_preview(configured)['judging']
    report = await runner.run_suite(configured)
    actual_judged = sum(record.fixture_id == 'pack:judged' for record in report.requests)
    assert preview['planned_call_upper_bound'] >= actual_judged
    assert preview['quota_call_upper_bound'] >= actual_judged
