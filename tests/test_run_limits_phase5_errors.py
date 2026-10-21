"""Structured conflicts share destinations, safe evidence and CLI/API copy."""

import json

import pytest
from pydantic import ValidationError

from giraffe.cli import main
from giraffe.configuration_errors import configuration_errors
from giraffe.models import RunConfig
from tests.test_web import browser


TARGET = {"name": "local", "url": "http://endpoint.invalid/v1", "model": "model"}
SINGLE = ("250 requests/second × 3,600 seconds = 900,000 planned arrivals. "
          "This traffic plan allows at most 100,000 per endpoint. "
          "At this rate, the longest stage is 400 seconds (6 minutes 40 seconds).")


def config(**updates):
    return {"config_version": 2, "targets": [TARGET], "checks": ["capacity"], **updates}


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate", "plan", "start", "cli"])
async def test_traffic_cap_is_identical_at_every_config_boundary_and_action_fixes_only_duration(tmp_path, endpoint):
    values = config(traffic={"rates": [250], "duration_seconds": 3600}, max_requests=12, max_duration_seconds=10)
    route = "/api/runs" if endpoint == "start" else f"/api/config/{endpoint}"
    body = {"config": values} if endpoint in {"start", "cli"} else values
    async with browser(tmp_path) as client:
        response = await client.post(route, json=body)
        assert response.status_code == 422, response.text
        issue, = response.json()["detail"]
        assert issue["msg"] == SINGLE
        assert issue["field"] == "traffic.rates"
        assert issue["fields"] == ["traffic.rates", "traffic.duration_seconds"]
        assert issue["values"] == {"traffic.rates": [250], "traffic.duration_seconds": 3600,
            "planned_arrivals": 900000, "maximum_planned_arrivals": 100000, "longest_stage_seconds": 400}
        assert issue["actions"] == [{"label": "Use 400 seconds", "field": "traffic.duration_seconds", "value": 400},
                                    {"label": "Edit sending rate", "field": "traffic.rates"}]
        assert "scheduled traffic only" in issue["note"]
        action = issue["actions"][0]
        parent, field = action["field"].split(".")
        values[parent][field] = action["value"]
        fixed = await client.post("/api/config/preview", json=values)
        assert fixed.status_code == 200, fixed.text
    resolved = fixed.json()["config"]
    assert resolved["traffic"]["rates"] == [250]
    assert resolved["traffic"]["duration_seconds"] == 400
    assert resolved["max_requests"] == 12
    assert resolved["max_duration_seconds"] == 10
    assert fixed.json()["request_plan"]["planned_total"] == 100000
    assert fixed.json()["request_plan"]["time_warnings"]
    assert fixed.json()["request_plan"]["request_warnings"]


CONFLICTS = [
    config(checks=["tools"], stream=False, tool_checks={"modes": ["stream"]}),
    config(checks=["sessions"], test_options={"sessions": {"max_output_tokens": 256}}),
    config(traffic={"rates": [250], "duration_seconds": 3600}),
    config(traffic={"rates": [50, 100, 250], "duration_seconds": 300}),
    config(checks=["correctness"], concurrency=40, test_options={"correctness": {"concurrency": 64}}),
    config(checks=["arrivals"], concurrency=40, arrivals={"max_in_flight": 64}),
    config(concurrency=40, traffic={"max_in_flight": 64}),
    config(checks=["sessions"], concurrency=16, sessions={"sessions": 64, "concurrency": 32}),
    config(checks=["consistency"], concurrency=40, consistency={"levels": [64]}),
    config(checks=["concurrency"], concurrency=40, concurrent_load={"schedule": {"levels": [64]}}),
    config(checks=["concurrency"], concurrent_load={"profile": {"medium": {"input_tokens": 1}}}),
    config(checks=["concurrency"], stream=False, concurrent_load={"interference": {"enabled": True}}),
    config(checks=["correctness"], request_source="request_pack", request_pack={"version": 1, "cases": [{
        "fixture_id": "one", "messages": [{"role": "user", "content": "Question"}], "scorer": "none"}]},
        test_options={"correctness": {"max_output_tokens": 256}}),
    config(checks=["concurrency"], concurrent_load={"interference": {"trigger": "bad"}}),
    config(checks=["access"], targets=[{"url": "http://endpoint.invalid/v1", "model": "model"}]),
]


@pytest.mark.parametrize("values", CONFLICTS)
@pytest.mark.parametrize("command", ["validate", "preview", "run"])
async def test_api_and_cli_use_same_conflict_messages(tmp_path, capsys, values, command):
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422, response.text
    api_issues = response.json()["detail"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    assert main([command, "--config", str(path), "--output-format", "json"]) == 4
    captured = capsys.readouterr()
    assert captured.err == ""
    cli_issues = json.loads(captured.out)["errors"]
    assert [issue["message"] for issue in cli_issues] == [issue["msg"] for issue in api_issues]
    for issue in api_issues:
        assert issue["field"] == issue["fields"][0]
        assert issue["actions"]
        assert all(action.get("field") in issue["fields"] or action.get("fields") for action in issue["actions"])
    for cli, api in zip(cli_issues, api_issues):
        if "fields" in cli:
            assert cli["fields"] == api["fields"]
            assert cli["values"] == api["values"]
            assert cli["actions"] == api["actions"]


async def test_cli_text_reports_same_exact_arithmetic_as_api(tmp_path, capsys):
    values = config(traffic={"rates": [250], "duration_seconds": 3600})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    assert main(["validate", "--config", str(path), "--output-format", "text"]) == 4
    captured = capsys.readouterr()
    assert captured.err.strip() == "giraffe: traffic: " + SINGLE
    assert captured.out == ""


@pytest.mark.parametrize("location,fields,expected", [
    (("body", "config", "traffic"), ["rates", "duration_seconds"], ["traffic.rates", "traffic.duration_seconds"]),
    (("body", "config", "traffic"), ["traffic.rates", "traffic.duration_seconds"], ["traffic.rates", "traffic.duration_seconds"]),
    (("body", "concurrent_load", "profile", "medium", "input_tokens"), ["medium.input_tokens", "short.input_tokens"],
     ["concurrent_load.profile.medium.input_tokens", "concurrent_load.profile.short.input_tokens"]),
    (("body", "concurrent_load", "profile", "medium", "input_tokens"), ["short.input_tokens", "medium.input_tokens"],
     ["concurrent_load.profile.short.input_tokens", "concurrent_load.profile.medium.input_tokens"]),
])
def test_relative_fields_values_and_actions_get_one_consistent_prefix(location, fields, expected):
    issues = configuration_errors([{"loc": location, "type": "test_conflict", "msg": "Conflict",
        "ctx": {"fields": fields, "values": {fields[0]: 250, fields[1]: 3600, "planned_arrivals": 900000},
                "actions": [{"label": "Use 400 seconds", "field": fields[1], "value": 400},
                            {"label": "Edit both", "fields": fields}]}}], {})
    issue, = issues
    assert issue["fields"] == expected
    assert issue["values"][expected[0]] == 250
    assert issue["values"][expected[1]] == 3600
    assert issue["values"]["planned_arrivals"] == 900000
    assert issue["actions"] == [{"label": "Use 400 seconds", "field": expected[1], "value": 400},
                                {"label": "Edit both", "fields": expected}]


def test_diagnostics_keep_computed_numbers_but_never_echo_sensitive_or_nonfinite_values():
    issue, = configuration_errors([{"loc": ("body", "traffic"), "type": "traffic_plan_cap", "msg": SINGLE,
        "ctx": {"fields": ["traffic.rates", "traffic.duration_seconds"],
                "values": {"traffic.rates": [250], "traffic.duration_seconds": 3600, "planned_arrivals": 900000,
                           "api_key": 1234, "secret": "PRIVATE", "headers": {"Authorization": "PRIVATE"},
                           "nan": float("nan"), "inf": float("inf"), "nested": {"private": 1},
                           "invalid": [1, "PRIVATE"]},
                "actions": [{"label": "Wrong field", "field": "api_key", "value": 1234},
                            {"label": "Wrong value", "field": "traffic.rates", "value": float("inf")}],
                "input": "PRIVATE"}}], {"traffic": {"rates": [250], "duration_seconds": 3600}, "api_key": "PRIVATE"})
    assert issue["values"] == {"traffic.rates": [250], "traffic.duration_seconds": 3600, "planned_arrivals": 900000}
    assert all(action["field"] in issue["fields"] for action in issue["actions"])
    assert "PRIVATE" not in json.dumps(issue, allow_nan=False)
    assert "input" not in issue and "ctx" not in issue


def test_direct_pydantic_traffic_error_has_structured_context_without_web_dependency():
    with pytest.raises(ValidationError) as captured:
        RunConfig.model_validate(config(traffic={"rates": [250], "duration_seconds": 3600}))
    error, = captured.value.errors()
    assert error["type"] == "traffic_plan_cap"
    assert error["ctx"]["fields"] == ["traffic.rates", "traffic.duration_seconds"]
    assert error["ctx"]["values"]["planned_arrivals"] == 900000


@pytest.mark.parametrize("legacy_metadata", [False, True])
async def test_capacity_estimate_is_advisory_for_actual_requests_and_baseline_matching(monkeypatch, legacy_metadata):
    from giraffe import runner
    from giraffe.reporting import _settings_reasons
    from tests.test_runner import FakeClient

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    values = config(traffic={"rates": [40], "duration_seconds": .03})
    original = RunConfig.model_validate(values)
    estimated = RunConfig.model_validate({**values, "traffic": {**values["traffic"], "expected_response_seconds": 2}})
    reference = await runner.run_suite(original)
    current = await runner.run_suite(estimated)
    assert [(record.fixture_id, record.scenario, record.input_chars, record.requested_max_tokens)
            for record in current.requests] == [
           (record.fixture_id, record.scenario, record.input_chars, record.requested_max_tokens)
            for record in reference.requests]
    if legacy_metadata:
        reference.manifest["config"]["traffic"].pop("expected_response_seconds")
        reference.manifest["effective_traffic"].pop("expected_response_seconds")
    before = reference.model_dump_json()
    assert _settings_reasons(current, reference) == []
    assert reference.model_dump_json() == before


@pytest.mark.parametrize("rates", [[0], [-1], [10001], [50, 50], [100, 50]])
async def test_other_traffic_schedule_conflicts_name_sending_rate_inline(tmp_path, capsys, rates):
    values = config(traffic={"rates": rates})
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422
    issue, = response.json()["detail"]
    assert issue["fields"] == ["traffic.rates"]
    assert issue["values"] == {"traffic.rates": rates}
    assert issue["actions"][0]["field"] == "traffic.rates"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    assert main(["validate", "--config", str(path), "--output-format", "json"]) == 4
    assert json.loads(capsys.readouterr().out)["errors"][0]["message"] == issue["msg"]


async def test_tool_stream_conflict_has_both_setting_destinations_and_safe_bool_evidence(tmp_path):
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=config(checks=["tools"], stream=False,
                                                                      tool_checks={"modes": ["stream"]}))
    assert response.status_code == 422
    issue, = response.json()["detail"]
    assert issue["fields"] == ["tool_checks.modes", "stream"]
    assert issue["values"] == {"stream": False}
    assert [action["field"] for action in issue["actions"]] == issue["fields"]
    assert issue["msg"] == "Enable streaming on Connect or choose complete tool requests."


async def test_plan_warning_classes_all_supply_inline_fields_safe_values_and_edit_actions(tmp_path):
    values = config(checks=["capacity", "arrivals", "concurrency"], concurrency=2,
        max_requests=1, max_duration_seconds=.5,
        traffic={"rates": [25], "duration_seconds": 1},
        arrivals={"mode": "burst", "requests": 8, "burst_size": 8, "max_pending": 1},
        concurrent_load={"schedule": {"duration_seconds": 1}})
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 200, response.text
    plan = response.json()["request_plan"]
    for group in ("concurrency_warnings", "load_warnings", "time_warnings", "request_warnings"):
        assert plan[group]
        for issue in plan[group]:
            assert issue["fields"]
            assert "values" in issue
            assert issue["actions"]
            assert issue["msg"] in plan["warnings"]


@pytest.mark.parametrize("time_budget,request_budget", [(None, None), (300, None), (None, 100), (300, 100)])
async def test_a13_valid_long_traffic_has_separate_nonblocking_budget_guidance(tmp_path, capsys, time_budget, request_budget):
    values = config(traffic={"rates": [25], "duration_seconds": 3600},
                    max_duration_seconds=time_budget, max_requests=request_budget)
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/validate", json=values)
    assert response.status_code == 200, response.text
    plan = response.json()["request_plan"]
    assert plan["planned_total"] == 90000
    assert plan["errors"] == []
    assert bool(plan["time_warnings"]) == (time_budget is not None)
    assert bool(plan["request_warnings"]) == (request_budget is not None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    assert main(["validate", "--config", str(path), "--output-format", "json"]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["config"]["traffic"]["rates"] == [25]
    assert cli["config"]["traffic"]["duration_seconds"] == 3600
    for warning in [*plan["time_warnings"], *plan["request_warnings"]]:
        assert warning["msg"] in cli["warnings"]


async def test_a14_aggregate_correction_applies_to_duration_only(tmp_path):
    values = config(traffic={"rates": [50, 100, 250], "duration_seconds": 300})
    async with browser(tmp_path) as client:
        rejected = await client.post("/api/config/preview", json=values)
        assert rejected.status_code == 422
        issue, = rejected.json()["detail"]
        assert issue["msg"] == ("(50 + 100 + 250) requests/second × 300 seconds = 120,000 planned arrivals. "
            "This traffic plan allows at most 100,000 per endpoint. At these rates, the longest stage that fits is 250 seconds.")
        action = issue["actions"][0]
        assert action == {"label": "Use 250 seconds", "field": "traffic.duration_seconds", "value": 250}
        values["traffic"][action["field"].split(".")[1]] = action["value"]
        accepted = await client.post("/api/config/preview", json=values)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["config"]["traffic"]["rates"] == [50, 100, 250]
    assert accepted.json()["request_plan"]["planned_total"] == 100000
