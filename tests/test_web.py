"""Exercise browser job boundaries and the real suite through its local HTTP API."""

import asyncio
import json
import os
from contextlib import asynccontextmanager

import httpx
import pytest

from tests.fake_endpoint import FakeEndpoint
from giraffe.models import (
    CHECK_LIMIT_FIELDS, CHECK_OPTION_FIELDS, CheckResult, RequestRecord, RunConfig, RunReport,
)
from giraffe.reporting import write_report
from giraffe.web import create_app


@pytest.mark.parametrize("endpoint", ["preview", "draft", "validate", "plan"])
async def test_config_conflicts_include_fields_and_safe_values(tmp_path, endpoint):
    values = config(checks=["load"], concurrency=40, load_test={"patterns": {"concurrent": {"levels": [1, 2]}}})
    values["load_test"]["patterns"]["concurrent"]["levels"] = [1, 64]
    async with browser(tmp_path) as client:
        response = await client.post(f"/api/config/{endpoint}", json=values)
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["field"] == "load_test.patterns.concurrent.levels"
    assert error["fields"] == ["load_test.patterns.concurrent.levels", "concurrency"]
    assert error["values"] == {"load_test.patterns.concurrent.levels": [1, 64], "concurrency": 40}
    assert error["msg"] == "Concurrency levels exceed the run ceiling: 64 > 40. Update Run limits or lower the levels."


@pytest.mark.parametrize("endpoint", ["preview", "validate"])
async def test_config_time_policy_before_start(tmp_path, endpoint):
    values = config(checks=["capacity"], max_duration_seconds=9,
                    traffic={"rates": [1, 2], "duration_seconds": 5})
    async with browser(tmp_path) as client:
        response = await client.post(f"/api/config/{endpoint}", json=values)
        assert response.status_code == 200, response.text
        plan = response.json()["request_plan"]
        assert plan["errors"] == []
        assert plan["time_warnings"][0]["fields"][0] == "max_duration_seconds"
        assert plan["time_warnings"][0]["cut_checks"]
        values["max_duration_seconds"] = 12
        response = await client.post(f"/api/config/{endpoint}", json=values)
        assert response.status_code == 200
        assert response.json()["request_plan"]["time_warnings"]


async def test_time_coverage_warnings_remain_editable_exportable_and_allow_dataset_changes(tmp_path, fake_runner):
    values = config(checks=["capacity"], max_duration_seconds=120,
                    traffic={"rates": [1, 2, 3], "duration_seconds": 60})
    async with browser(tmp_path) as client:
        for edit in (False, True):
            if edit:
                values["request_pack"] = {"version": 1, "cases": [{"fixture_id": "one",
                    "messages": [{"role": "user", "content": "Question"}], "scorer": "none"}]}
                values["request_source"] = "request_pack"
            response = await client.post("/api/config/draft", json=values)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["request_plan"]["errors"] == []
            assert result["request_plan"]["time_warnings"][0]["fields"][0] == "max_duration_seconds"
            values = result["config"]  # Applying/importing and saving the normalized draft stays possible.
            assert values["max_duration_seconds"] == 120
            assert (await client.post("/api/config/validate", json=values)).status_code == 200
            assert (await client.post("/api/config/cli", json={"config": values})).status_code == 200
            started = await client.post("/api/runs", json={"config": values})
            assert started.status_code == 202, started.text
            assert (await finished(client, started.json()["id"]))["state"] == "completed"


@pytest.mark.parametrize("updates,field,expected", [
    ({"checks": ["load"], "concurrency": 4, "load_test": {"patterns": {"concurrent": {"levels": [1, 16]}}}}, "concurrency", 4),
    ({"checks": ["correctness"], "concurrency": 4, "test_options": {"correctness": {"concurrency": 8}}}, "concurrency", 4),
])
async def test_conflicts_resolve_explicit_ceiling_and_omitted_request_defaults(tmp_path, updates, field, expected):
    values = {"targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}], **updates}
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert "None" not in error["msg"]
    assert error["values"][field] == expected


async def test_profile_caps_can_exceed_suite_default_without_clamping(tmp_path):
    values = {"targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}],
              "checks": ["load"], "max_output_tokens": "64", "load_test": {
                  "profile": {"short": {"input_tokens": 128, "output_tokens": 16},
                              "medium": {"input_tokens": 512, "output_tokens": 48},
                              "long": {"input_tokens": 2048, "output_tokens": 96}}}}
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 200, response.text
    resolved = response.json()["config"]
    assert resolved["max_output_tokens"] == 64
    assert resolved["load_test"]["profile"] == {
        "short": {"input_tokens": 128, "output_tokens": 16},
        "medium": {"input_tokens": 512, "output_tokens": 48},
        "long": {"input_tokens": 2048, "output_tokens": 96},
    }


@pytest.mark.parametrize("raw_limit,expected", [("64", 200), ("not-a-number", 422)])
async def test_concurrent_defaults_do_not_compare_raw_numeric_strings(tmp_path, raw_limit, expected):
    values = {"targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}],
              "checks": ["load"], "concurrency": "4", "max_output_tokens": raw_limit,
              "load_test": {"profile":{"short":{"input_tokens":128,"output_tokens":16},"medium":{"input_tokens":512,"output_tokens":32},"long":{"input_tokens":2048,"output_tokens":64}}}}
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["config"]["max_output_tokens"] == 64
        assert response.json()["config"]["load_test"]["profile"]["long"]["output_tokens"] == 64


async def test_performance_workload_cap_can_exceed_suite_default(tmp_path):
    values = config(checks=["sessions"], max_output_tokens=64, sessions={"profile": "performance",
        "modes": ["live"], "workloads": [{"name": "coding", "turns": ["First", "Next"], "max_tokens": 32}]})
    values["sessions"]["workloads"][0]["max_tokens"] = "96"
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 200, response.text
    assert response.json()["config"]["sessions"]["workloads"][0]["max_tokens"] == 96
    assert response.json()["config"]["max_output_tokens"] == 64
    assert response.json()["request_plan"]["errors"] == []


async def test_workload_question_validation_does_not_echo_numeric_input(tmp_path):
    values = config(checks=["sessions"], sessions={"profile": "performance", "modes": ["live"],
        "workloads": [{"name": "chat", "turns": ["First", "Second"]}]})
    values["sessions"]["workloads"][0]["turns"][0] = 123456789
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422
    assert "123456789" not in response.text


async def test_field_validation_has_destination_without_echoing_secret_input(tmp_path):
    values = config(checks=["access"])
    values["request_timeout_seconds"] = "pasted-secret"
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["field"] == "request_timeout_seconds"
    assert error["loc"] == ["body", "request_timeout_seconds"]
    assert error["values"] == {}
    assert "pasted-secret" not in response.text


async def test_check_timeout_can_exceed_suite_default(tmp_path):
    values = config(checks=["access"], request_timeout_seconds=30)
    values["test_options"] = {"access": {"request_timeout_seconds": 60}}
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 200, response.text
    plan = response.json()["request_plan"]
    assert plan["request_settings"]["access"]["request_timeout_seconds"] == 60
    assert response.json()["config"]["request_timeout_seconds"] == 30
    assert plan["errors"] == []


async def test_nonfinite_validation_values_remain_serializable(tmp_path):
    values = config(checks=["access"])
    values["request_timeout_seconds"] = float("nan")
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", content=json.dumps(values),
                                     headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["values"] == {}


async def test_short_time_budget_starts_partial_fixed_window_work(tmp_path, fake_runner):
    values = config(checks=["capacity"], max_duration_seconds=1,
                    traffic={"rates": [1], "duration_seconds": 2})
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == 202, response.text
        assert (await finished(client, response.json()["id"]))["state"] == "completed"
        assert len((await client.get("/api/runs")).json()["runs"]) == 1


def config(**updates):
    return RunConfig(targets=[{"name": "local", "url": "http://localhost:11434/v1",
                               "model": "test-model"}], **updates).model_dump(mode="json")


def report(configuration, *, cancelled=False):
    return RunReport(run_id="suite-run-id", started_at="2026-10-04T10:00:00+00:00",
                     finished_at="2026-10-04T10:00:01+00:00",
                     overall="inconclusive" if cancelled else "pass",
                     manifest={"config": configuration.model_dump(mode="json")},
                     abort_reason="cancelled by user" if cancelled else None,
                     checks=[CheckResult(id="serving", target="local", title="Serving success",
                                         status="inconclusive" if cancelled else "pass",
                                         summary="Interrupted" if cancelled else "Completed")],
                     requests=[RequestRecord(id="request-one", target="local", fixture_id="short",
                                             scenario="warm", check_ids=["serving"],
                                             started_at="2026-10-04T10:00:00+00:00",
                                             status="completed", valid=True, score=True,
                                             output="private successful answer", output_chars=25)])


async def successful_runner(configuration, *, progress, stop_event):
    progress({"event": "request_completed", "target": "local", "scenario": "warm",
              "completed": 1, "attempted": 1})
    await asyncio.sleep(0.01)
    return report(configuration)


@pytest.mark.parametrize("checks", [[], ["capacity"], ["correctness"]])
async def test_pack_preview_matches_cli_and_preserves_blank_model_draft(tmp_path, monkeypatch, capsys, checks):
    import io
    from giraffe.cli import main
    from tests.test_cli import operator_pack

    values = config(checks=["capacity"], request_source="request_pack", request_pack=operator_pack())
    values["targets"][0]["model"] = ""
    values["checks"] = checks
    monkeypatch.setattr("giraffe.web.run_suite", lambda *a, **k: pytest.fail("Preview sent traffic"))
    monkeypatch.setattr("giraffe.web.load_custom_fixtures", lambda *a: pytest.fail("Preview read fixtures"))
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
        assert response.status_code == 200, response.text
        normalized = response.json()["config"]
        assert normalized["request_pack"] == values["request_pack"]
        assert normalized["targets"][0]["model"] == ""
        assert (await client.post("/api/config/draft", json=normalized)).json()["config"] == normalized
        for endpoint in ("/api/config/cli", "/api/runs"):
            assert (await client.post(endpoint, json={"config": normalized})).status_code == 422
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(values)))
        assert main(["preview", "--config", "-"]) == 0
        assert json.loads(capsys.readouterr().out) == response.json()
        assert (await client.get("/api/runs")).json()["runs"] == []


@pytest.mark.parametrize("pack", ["/private/pack.json", "https://example.com/pack.json", {
    "version": 1, "cases": [{"fixture_id": "secret", "messages": [{"role": "user", "content": "PRIVATE_SECRET"}],
                            "scorer": "PRIVATE_SECRET"}]}])
async def test_pack_api_rejects_paths_and_bad_cases_without_input_echo(tmp_path, monkeypatch, pack):
    monkeypatch.setattr("giraffe.web.run_suite", lambda *a, **k: pytest.fail("Invalid pack sent traffic"))
    monkeypatch.setattr("giraffe.web.load_custom_fixtures", lambda *a: pytest.fail("Invalid pack read fixtures"))
    values = config() | {"request_source": "request_pack", "request_pack": pack}
    async with browser(tmp_path) as client:
        for endpoint in ("/api/config/preview", "/api/config/draft", "/api/config/validate", "/api/config/cli", "/api/runs"):
            body = {"config": values} if endpoint in {"/api/config/cli", "/api/runs"} else values
            response = await client.post(endpoint, json=body)
            assert response.status_code == 422, response.text
            assert "PRIVATE_SECRET" not in response.text
            assert "request_pack" in str(response.json()["detail"][0]["loc"])
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_pack_conflicts_with_legacy_before_files_or_run_creation(tmp_path, monkeypatch):
    from tests.test_cli import operator_pack

    monkeypatch.setattr("giraffe.web.load_custom_fixtures", lambda *a: pytest.fail("Read conflicting fixtures"))
    values = config() | {"request_source": "request_pack", "request_pack": operator_pack(),
                         "custom_fixtures": "/missing/fixtures.json"}
    async with browser(tmp_path) as client:
        for endpoint in ("/api/config/preview", "/api/config/cli", "/api/runs"):
            response = await client.post(endpoint, json=values if endpoint.endswith("preview") else {"config": values})
            assert response.status_code == 422
            assert "cannot be combined" in response.text
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_pack_saved_config_download_preserves_inputs_under_response_retention(tmp_path, fake_runner):
    from tests.test_cli import operator_pack

    pack = operator_pack()
    pack["cases"][0]["messages"][0]["content"] = "api_key=literal-case-content " * 1000
    values = config(checks=["correctness"], request_source="request_pack", request_pack=pack,
                    retention="none")
    async with browser(tmp_path) as client:
        identifier = await start(client, values)
        result = await finished(client, identifier)
        saved = (await client.get(f"/api/runs/{identifier}/config.json")).json()
        assert saved["request_pack"] == values["request_pack"]
        assert result["report"]["requests_compact"]
        evidence = await client.get(f"/api/runs/{identifier}/requests/{result['report']['requests'][0]['id']}")
        assert evidence.json()["output"] == ""
        command = await client.post("/api/config/cli", json={"config": saved})
        assert command.status_code == 200
        assert "api_key=literal-case-content" in command.json()["command"]


@asynccontextmanager
async def browser(tmp_path, **kwargs):
    app = create_app(tmp_path, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost:8787") as client:
            yield client


async def finished(client, identifier):
    for _ in range(200):
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        if detail["state"] not in {"running", "stopping"}:
            return detail
        await asyncio.sleep(0.01)
    pytest.fail("Run did not finish")


async def start(client, configuration=None, **values):
    response = await client.post("/api/runs", json={"config": configuration or config(), **values})
    assert response.status_code == 202, response.text
    return response.json()["id"]


@pytest.fixture
def fake_runner(monkeypatch):
    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)


async def test_bootstrap_has_blank_model_and_no_automatic_traffic(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Bootstrap must not send inference requests")
    monkeypatch.setattr("giraffe.web.run_suite", forbidden)
    async with browser(tmp_path) as client:
        result = (await client.get("/api/bootstrap")).json()
        assert result["config"]["targets"][0]["model"] == ""
        assert result["config"]["restart_target"] is None
        assert len(result["checks"]) == 15
        assert {c["id"] for c in result["checks"] if c["optional"]} == {
            "json", "gpu", "prefix", "sessions", "consistency", "tools",
        }
        assert result["active_run_id"] is None
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_bootstrap_exposes_authoritative_configuration_fields(tmp_path):
    async with browser(tmp_path) as client:
        result = (await client.get("/api/bootstrap")).json()
        for check in result["checks"]:
            assert set(check["option_fields"]) == CHECK_OPTION_FIELDS.get(check["id"], set())
            assert set(check["limit_fields"]) == CHECK_LIMIT_FIELDS.get(check["id"], set())
            assert set(check["option_fields"]) <= result["option_schema"]["properties"].keys()
            assert set(check["limit_fields"]) <= result["limits_schema"]["properties"].keys()
        assert result["traffic_schema"]["properties"]["arrival"]["enum"] == ["steady", "poisson"]
        assert result["config"]["traffic"]["rates"] == [1, 2, 4]
        assert result["config"]["checks"] == ["access", "correctness"]


async def test_request_plan_is_additive_on_every_setup_endpoint(tmp_path, monkeypatch):
    from giraffe.request_pack import pack_preview

    monkeypatch.setattr("giraffe.web.run_suite", lambda *a, **k: pytest.fail("Setup sent traffic"))
    async with browser(tmp_path) as client:
        bootstrap = (await client.get("/api/bootstrap")).json()
        assert bootstrap["request_plan"]["planned_per_target"] == 7
        values = config(checks=["access", "correctness"])
        for endpoint in ("draft", "validate", "preview"):
            response = await client.post(f"/api/config/{endpoint}", json=values)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["config"] == values
            assert result["request_plan"] == bootstrap["request_plan"]
            if endpoint == "preview":
                assert result["request_pack_preview"] == pack_preview(RunConfig.model_validate(values))
        values["targets"][0]["model"] = ""
        values["checks"] = []
        draft = (await client.post("/api/config/draft", json=values)).json()
        assert draft["config"]["checks"] == []
        assert draft["request_plan"]["planned_total"] == 0


async def test_launcher_defaults_preserve_explicit_checks(tmp_path):
    values = config(checks=["access", "generation", "capacity"])
    async with browser(tmp_path, defaults=values) as client:
        result = (await client.get("/api/bootstrap")).json()
        assert result["config"]["checks"] == values["checks"]
        from giraffe.planning import request_plan
        assert result["request_plan"]["planned_per_target"] == request_plan(RunConfig.model_validate(values))["planned_per_target"]


async def test_capabilities_share_the_cli_catalog_and_do_not_start_traffic(tmp_path, monkeypatch):
    from giraffe.capabilities import capabilities

    async def forbidden(*args, **kwargs):
        pytest.fail("Capability discovery must not send traffic")

    monkeypatch.setattr("giraffe.web.run_suite", forbidden)
    async with browser(tmp_path) as client:
        discovery = (await client.get("/api/capabilities")).json()
        bootstrap = (await client.get("/api/bootstrap")).json()
        assert discovery == capabilities()
        assert discovery["checks"] == bootstrap["checks"]
        assert discovery["config_schema"] == bootstrap["config_schema"]
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_copy_cli_preserves_intent_without_starting_a_run(tmp_path, monkeypatch):
    from giraffe.handoff import cli_command

    async def forbidden(*args, **kwargs):
        pytest.fail("Copy CLI must not start traffic")

    monkeypatch.setattr("giraffe.web.run_suite", forbidden)
    values = config(checks=["correctness"], test_options={"correctness": {"samples": 9}})
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/cli", json={"config": values})
        assert response.status_code == 200
        assert response.json() == {"command": cli_command(RunConfig.model_validate(values))}
        assert (await client.get("/api/runs")).json()["runs"] == []
        assert not any(path.is_dir() and not path.name.startswith(".") for path in tmp_path.iterdir())


@pytest.mark.parametrize("mode,restart_target,config_restart,hook", [
    ("normal", "local", None, ["true"]),
    ("normal", None, "local", ["true"]),
    ("cold-start", None, None, ["true"]),
    ("cold-start", "unknown", None, ["true"]),
    ("cold-start", "local\x00", None, ["true"]),
    ("cold-start", "local", None, None),
])
async def test_copy_cli_and_start_reject_the_same_invalid_restart_intent(
    tmp_path, mode, restart_target, config_restart, hook,
):
    values = config(checks=["access"])
    values["targets"][0]["restart_command"] = hook
    values["restart_target"] = config_restart
    body = {"config": values, "mode": mode, "restart_target": restart_target}
    async with browser(tmp_path) as client:
        copy = await client.post("/api/config/cli", json=body)
        start = await client.post("/api/runs", json=body)
        assert copy.status_code == start.status_code == 422
        assert copy.json() == start.json()


async def test_copy_cli_checks_selected_fixture_and_baseline_availability(tmp_path):
    async with browser(tmp_path) as client:
        values = config(checks=["correctness"], custom_fixtures="/missing-fixtures.json")
        assert (await client.post("/api/config/cli", json={"config": values})).status_code == 422
        values["checks"] = ["capacity"]
        assert (await client.post("/api/config/cli", json={"config": values})).status_code == 200
        for endpoint in ("/api/config/cli", "/api/runs"):
            assert (await client.post(endpoint, json={"config": values,
                                                      "baseline_id": "missing"})).status_code == 404


async def test_download_config_returns_exact_saved_manifest_and_no_resolved_credentials(tmp_path, fake_runner):
    values = config(checks=["correctness"], test_options={"correctness": {"samples": 11}})
    values["targets"][0]["api_key_env"] = "LOCAL_MODEL_KEY"
    async with browser(tmp_path) as client:
        identifier = await start(client, values)
        detail = await finished(client, identifier)
        response = await client.get(f"/api/runs/{identifier}/config.json")
        assert response.status_code == 200
        assert response.json() == detail["report"]["manifest"]["config"]
        assert response.json()["targets"][0]["api_key_env"] == "LOCAL_MODEL_KEY"
        assert "attachment" in response.headers["content-disposition"]
        assert (await client.get("/api/runs/missing/config.json")).status_code == 404


async def test_config_validation_never_starts_traffic_and_preserves_sparse_overrides(
    tmp_path, monkeypatch,
):
    async def forbidden(*args, **kwargs):
        pytest.fail("Validating configuration must not start a runner")
    monkeypatch.setattr("giraffe.web.run_suite", forbidden)
    values = config(checks=["load"], limits={"latency_ms": 800}, test_options={

        "correctness": {"samples": 12},
    }, load_test={"patterns":{"rates":[1,3],"poisson":{"enabled":True}},"pass_criteria":{"max_completion_ms":None,"max_first_answer_ms":400}})
    values["targets"].append({"name": "other", "url": "http://localhost:8000/v1", "model": "b"})
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/validate", json=values)
        assert response.status_code == 200, response.text
        saved = response.json()["config"]
        assert saved["checks"] == ["load"]
        assert saved["test_options"] == values["test_options"]
        assert saved["targets"][1]["name"] == "other"
        assert saved["load_test"]["patterns"]["poisson"]["enabled"]
        assert saved["load_test"]["pass_criteria"]["max_completion_ms"] is None
        assert saved["load_test"]["pass_criteria"]["max_first_answer_ms"] == 400
        assert (await client.get("/api/runs")).json()["runs"] == []
        for changes in ({"load_test": {"patterns":{"rates": [3, 1]}}},
                        {"test_options": {"correctness": {"sustained_seconds": 5}}}):
            rejected = await client.post("/api/config/validate", json=values | changes)
            assert rejected.status_code == 422
        empty = await client.post("/api/config/validate", json=values | {"checks": []})
        assert empty.status_code == 200 and empty.json()["config"]["checks"] == []
        assert (await client.get("/api/runs")).json()["runs"] == []


@pytest.mark.parametrize("checks", [[], ["load"]])
async def test_blank_model_draft_sync_and_export_keep_execution_strict(tmp_path, checks):
    async with browser(tmp_path) as client:
        values = (await client.get("/api/bootstrap")).json()["config"]
        values["load_test"]["patterns"]["rates"] = [1, 3]
        values["checks"] = checks
        values["load_test"]["pass_criteria"]["max_completion_ms"] = None
        values["test_options"] = {"correctness":{"limits":{"min_correctness":1}}}
        values["targets"].append({"name": "second", "url": "http://localhost:8000/v1", "model": ""})
        response = await client.post("/api/config/draft", json=values)
        assert response.status_code == 200, response.text
        saved = response.json()["config"]
        assert [t["model"] for t in saved["targets"]] == ["", ""]
        assert saved["load_test"]["patterns"]["rates"] == [1, 3]
        assert saved["checks"] == checks
        assert saved["test_options"] == values["test_options"]
        assert (await client.post("/api/config/draft", json=saved)).json()["config"] == saved
        for endpoint, body in (("/api/config/validate", saved), ("/api/config/cli", {"config": saved}),
                               ("/api/runs", {"config": saved})):
            assert (await client.post(endpoint, json=body)).status_code == 422
        for changes in ({"load_test": {"patterns":{"rates": [3, 1]}}}, {"checks": ["unknown"]},
                        {"test_options": {"correctness": {"sustained_seconds": 5}}}):
            assert (await client.post("/api/config/draft", json=saved | changes)).status_code == 422
        for target in saved["targets"]:
            target["model"] = "test-model"
        assert (await client.post("/api/config/validate", json=saved)).status_code == 200
        saved["checks"] = ["load"]
        assert (await client.post("/api/config/validate", json=saved)).status_code == 200
        assert (await client.post("/api/config/cli", json={"config": saved})).status_code == 200
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_workload_and_deployment_drafts_survive_sync_and_remain_opt_in(tmp_path, monkeypatch):
    seen = []

    async def capture(configuration, **kwargs):
        seen.append(configuration)
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", capture)
    async with browser(tmp_path) as client:
        values = (await client.get("/api/bootstrap")).json()["config"]
        values.update(checks=["prefix"], prefix={"repeats": 7},
                      arrivals={"requests_per_second": 3}, consistency={"repetitions": 8})
        values["targets"][0].update(
            deployment={"runtime": {"version": "custom-runtime"}, "discovery": "vllm"},
            metrics_profile="vllm-v1", metrics_api_key_env="METRICS_TOKEN")
        draft = await client.post("/api/config/draft", json=values)
        assert draft.status_code == 200, draft.text
        saved = draft.json()["config"]
        assert saved["checks"] == ["prefix"]
        assert saved["targets"][0]["model"] == ""
        assert saved["prefix"]["repeats"] == 7
        assert saved["arrivals"]["requests_per_second"] == 3
        assert saved["targets"][0]["deployment"]["runtime"] == {"version": "custom-runtime"}
        assert saved["targets"][0]["metrics_api_key_env"] == "METRICS_TOKEN"
        assert (await client.post("/api/config/draft", json=saved)).json()["config"] == saved
        saved["checks"] = ["correctness"]
        saved["targets"][0]["model"] = "test-model"
        command = await client.post("/api/config/cli", json={"config": saved})
        assert command.status_code == 200, command.text
        assert '"repeats": 7' in command.json()["command"]
        identifier = await start(client, saved)
        await finished(client, identifier)
        assert seen[0].checks == ["correctness"]
        assert seen[0].prefix.repeats == 7
        assert seen[0].targets[0].deployment.runtime == {"version": "custom-runtime"}


async def test_selected_checks_and_custom_settings_reach_runner_exactly(tmp_path, monkeypatch):
    seen = []

    async def capture(configuration, **kwargs):
        seen.append(configuration)
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", capture)
    values = config(checks=["correctness"], test_options={"correctness": {"samples": 9},
                                                        "recovery": {"sustained_seconds": 2}})
    # Old flags must not silently re-enable explicitly deselected checks.
    values.update(structured_json=True, metrics=True)
    async with browser(tmp_path) as client:
        identifier = await start(client, values)
        detail = await finished(client, identifier)
        assert seen[0].checks == ["correctness"]
        assert seen[0].effective_check("correctness")["samples"] == 9
        assert seen[0].test_options["recovery"].sustained_seconds == 2
        assert detail["config"]["checks"] == ["correctness"]
        assert not detail["config"]["structured_json"]
        assert not detail["config"]["metrics"]


async def test_job_lifecycle_reads_retained_artifacts(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert detail["state"] == "completed"
        assert detail["report"]["requests_compact"]
        evidence = await client.get(f"/api/runs/{identifier}/requests/{detail['report']['requests'][0]['id']}")
        assert evidence.json()["output"] == ""
        assert detail["report"]["run_id"] == "suite-run-id"
        listing = (await client.get("/api/runs")).json()
        assert listing["active_run_id"] is None
        assert listing["runs"][0]["id"] == identifier
        assert listing["runs"][0]["overall"] == "pass"
        assert listing["runs"][0]["request_count"] == 1
        assert listing["runs"][0]["check_counts"]["pass"] == 1
        saved = await client.get(f"/api/runs/{identifier}/report.json")
        assert saved.status_code == 200
        assert "attachment" in saved.headers["content-disposition"]
        assert saved.json()["requests"][0] == evidence.json()
        assert {k: v for k, v in saved.json().items() if k != "requests"} == {
            k: v for k, v in detail["report"].items() if k not in {"requests", "request_count", "requests_compact"}}
        html = await client.get(f"/api/runs/{identifier}/report.html")
        assert html.status_code == 200
        assert "private successful answer" not in html.text
        assert (await client.get(f"/api/runs/{identifier}/report.exe")).status_code == 404
    async with browser(tmp_path) as client:
        assert (await client.get(f"/api/runs/{identifier}")).json()["state"] == "completed"


async def test_one_active_run_progress_and_cancel_preserves_partial_report(tmp_path, monkeypatch):
    async def waiting(configuration, *, progress, stop_event):
        for i in range(150):
            progress({"event": "request_completed", "completed": i, "scenario": "warm"})
        await stop_event.wait()
        return report(configuration, cancelled=True)
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await asyncio.sleep(0)
        second = await client.post("/api/runs", json={"config": config()})
        assert second.status_code == 409
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        assert detail["progress"]["completed"] == 149
        assert len(detail["events"]) == 100
        assert detail["report"] is None
        assert (await client.get("/api/bootstrap")).json()["active_run_id"] == identifier
        assert (await client.post(f"/api/runs/{identifier}/cancel", json={})).status_code == 200
        detail = await finished(client, identifier)
        assert detail["report"]["abort_reason"] == "cancelled by user"
        assert detail["report"]["overall"] == "inconclusive"
        # Repeated cancellation cannot mutate the completed report.
        assert (await client.post(f"/api/runs/{identifier}/cancel", json={})).json()["state"] == "completed"


async def test_cancel_still_stops_traffic_when_status_write_fails(tmp_path, monkeypatch):
    from giraffe.web import _write

    stopped = asyncio.Event()

    async def waiting(configuration, *, progress, stop_event):
        await stop_event.wait()
        stopped.set()
        return report(configuration, cancelled=True)

    def fail_stopping_write(path, value):
        if value.get("state") == "stopping":
            raise OSError("disk full")
        return _write(path, value)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        monkeypatch.setattr("giraffe.web._write", fail_stopping_write)
        response = await client.post(f"/api/runs/{identifier}/cancel", json={})
        assert response.status_code == 200
        assert response.json()["state"] == "stopping"
        await asyncio.wait_for(stopped.wait(), timeout=1)
        detail = await finished(client, identifier)
        assert detail["report"]["abort_reason"] == "cancelled by user"


async def test_final_state_remains_visible_when_status_write_fails(tmp_path, monkeypatch):
    release = asyncio.Event()

    async def waiting(configuration, *, progress, stop_event):
        await release.wait()
        return report(configuration)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        monkeypatch.setattr("giraffe.web._write", fail_write)
        release.set()
        detail = await finished(client, identifier)
        assert detail["state"] == "completed"
        assert detail["report"]["overall"] == "pass"
        assert "status could not be saved" in detail["error"]
        assert (await client.get("/api/runs")).json()["active_run_id"] is None


async def test_shutdown_cooperatively_saves_report(tmp_path, monkeypatch):
    async def waiting(configuration, *, progress, stop_event):
        await stop_event.wait()
        return report(configuration, cancelled=True)
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
    saved = json.loads((tmp_path / identifier / "report.json").read_text())
    assert saved["abort_reason"] == "cancelled by user"
    assert json.loads((tmp_path / identifier / ".web.json").read_text())["state"] == "completed"


async def test_unexpected_failure_is_visible_without_exception_contents(tmp_path, monkeypatch):
    async def failing(*args, **kwargs):
        raise RuntimeError("secret-key-hidden")
    monkeypatch.setattr("giraffe.web.run_suite", failing)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert detail["state"] == "error"
        assert "RuntimeError" in detail["error"]
        assert "secret-key" not in json.dumps(detail)
        assert (await client.get("/api/runs")).json()["active_run_id"] is None
        assert detail["report"] is None


async def test_shutdown_timeout_marks_interrupted(tmp_path, monkeypatch):
    async def waiting(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    monkeypatch.setattr("giraffe.web._SHUTDOWN_SECONDS", 0.01)
    async with browser(tmp_path) as client:
        identifier = await start(client)
    assert json.loads((tmp_path / identifier / ".web.json").read_text())["state"] == "interrupted"


async def test_existing_cli_runs_and_interrupted_jobs_survive_restart(tmp_path):
    write_report(report(RunConfig.model_validate(config())), tmp_path / "from-cli")
    interrupted = tmp_path / "old-web"
    interrupted.mkdir()
    (interrupted / ".web.json").write_text(json.dumps({"id": "old-web", "state": "running", "config": config(),
                                                      "started_at": "2026-10-04T12:00:00Z"}))
    os.utime(interrupted / ".web.json", (1, 1))
    # A standalone baseline JSON is not a run directory.
    (tmp_path / "baseline.json").write_text("{}")
    async with browser(tmp_path) as client:
        rows = (await client.get("/api/runs")).json()["runs"]
        assert {row["id"] for row in rows} == {"from-cli", "old-web"}
        assert rows[0]["state"] == "interrupted"
        assert rows[1]["state"] == "completed"


async def test_baselines_are_explicit_named_copies_and_never_replaced_by_run(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        first = await start(client)
        await finished(client, first)
        assert (await client.get("/api/baselines")).json()["baselines"] == []
        response = await client.post("/api/baselines", json={"run_id": first, "name": "Before upgrade"})
        assert response.status_code == 201
        baseline = response.json()
        saved_path = tmp_path / ".baselines" / baseline["id"] / "report.json"
        original = saved_path.read_bytes()
        second = await start(client, baseline_id=baseline["id"])
        detail = await finished(client, second)
        assert detail["baseline_id"] == baseline["id"]
        assert detail["report"]["baseline"] is not None
        assert saved_path.read_bytes() == original
        assert (await client.post("/api/baselines", json={"run_id": second,
               "name": "Before upgrade"})).status_code == 409
        replaced = await client.post("/api/baselines", json={"run_id": second,
                                    "name": "Before upgrade", "replace": True})
        assert replaced.status_code == 201
        assert replaced.json()["id"] == baseline["id"]
        assert (await client.get("/api/baselines")).json()["baselines"][0]["run_id"] == second


async def test_unknown_baseline_rejected_before_any_run_starts(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        result = await client.post("/api/runs", json={"config": config(), "baseline_id": "unknown"})
        assert result.status_code == 404
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_invalid_configuration_and_custom_fixtures_preflight(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        invalid = config()
        invalid["targets"][0]["url"] = "https://secret-key@example.com"
        response = await client.post("/api/runs", json={"config": invalid})
        assert response.status_code == 422
        assert "secret-key" not in response.text
        missing = await client.post("/api/runs", json={"config": config(custom_fixtures="/missing")})
        assert missing.status_code == 422
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_disabled_correctness_does_not_load_custom_fixture_file(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        identifier = await start(client, config(checks=["capacity"], custom_fixtures="/missing"))
        detail = await finished(client, identifier)
        assert detail["state"] == "completed"
        assert detail["config"]["custom_fixtures"] == "/missing"


async def test_normal_run_never_runs_restart_config(tmp_path, fake_runner):
    values = config(restart_target="local")
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    async with browser(tmp_path, defaults=values) as client:
        assert (await client.get("/api/bootstrap")).json()["config"]["restart_target"] is None
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == 422
        response = await client.post("/api/runs", json={"config": values, "mode": "cold-start"})
        assert response.status_code == 422
        identifier = await start(client, values, mode="cold-start", restart_target="local")
        assert (await finished(client, identifier))["config"]["restart_target"] == "local"


async def test_cold_start_requires_matching_target_with_hook(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", json={"config": config(), "mode": "cold-start",
                                                       "restart_target": "local"})
        assert response.status_code == 422
        values = config(restart_target="local")
        values["targets"].append({"name": "other", "url": "http://localhost:1234/v1",
                                  "model": "test", "restart_command": ["echo", "restart"]})
        response = await client.post("/api/runs", json={"config": values, "mode": "cold-start",
                                                       "restart_target": "other"})
        assert response.status_code == 422


async def test_mutations_require_json_and_same_origin(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", content=json.dumps({"config": config()}))
        assert response.status_code == 415
        response = await client.post("/api/runs", json={"config": config()},
                                     headers={"Origin": "https://elsewhere.example"})
        assert response.status_code == 403
        malformed = await client.post("/api/runs", json={"config": config()},
                                      headers={"Origin": "http://["})
        assert malformed.status_code == 403
        response = await client.post("/api/runs", json={"config": config()},
                                     headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403
        response = await client.post("/api/runs", json={"config": config()},
                                     headers={"Origin": "http://localhost:8787"})
        assert response.status_code == 202
        await finished(client, response.json()["id"])


async def test_paths_and_symlinks_do_not_expose_unrelated_files(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write_report(report(RunConfig.model_validate(config())), outside)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "linked").symlink_to(outside, target_is_directory=True)
    (runs / "bad-artifact").mkdir()
    (runs / "bad-artifact" / "report.json").symlink_to(outside / "report.json")
    async with browser(runs) as client:
        assert (await client.get("/api/runs/linked")).status_code == 404
        assert (await client.get("/api/runs/linked/report.json")).status_code == 404
        assert (await client.get("/api/runs/bad-artifact/report.json")).status_code == 404
        assert (await client.get("/api/runs/%2E%2E%2Foutside")).status_code == 404
        assert all(row["id"] != "linked" for row in (await client.get("/api/runs")).json()["runs"])


async def test_real_api_to_runner_to_http_endpoint(tmp_path):
    with FakeEndpoint(first_token_delay=0.03, chunk_delay=0.03) as endpoint:
        values = config(checks=["access", "serving", "correctness"], concurrency=2,
                        samples=2, max_requests=30, max_duration_seconds=15)
        values["targets"][0]["url"] = endpoint.url
        async with browser(tmp_path) as client:
            identifier = await start(client, values)
            detail = await finished(client, identifier)
            assert detail["state"] == "completed"
            assert detail["report"]["overall"] == "pass"
            assert len(endpoint.requests) > 0
            assert len(detail["report"]["requests"]) == len(endpoint.requests)
            assert {check["id"] for check in detail["report"]["checks"]
                    if check["status"] != "skipped"} == {
                "access", "serving", "correctness"}


async def test_live_requests_and_phases_survive_activity_feed_truncation(tmp_path, monkeypatch):
    async def waiting(configuration, *, progress, stop_event):
        progress({"event": "scenario_started", "target": "local", "scenario": "warm",
                  "elapsed_seconds": 0})
        for slot in range(120):
            progress({"event": "request_started", "request_slot": slot, "target": "local",
                      "scenario": "warm", "fixture_id": f"fixture-{slot}"})
        for slot in reversed(range(120)):
            progress({"event": "request_completed", "request_slot": slot,
                      "request": {"id": f"request-{slot}", "status": "completed", "score": None}})
        progress({"event": "scenario_finished", "target": "local", "scenario": "warm",
                  "elapsed_seconds": 12})
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await asyncio.sleep(0)
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        assert len(detail["events"]) == 100
        assert len(detail["live_requests"]) == 120
        assert [r["slot"] for r in detail["live_requests"]] == list(range(120))
        assert detail["live_requests"][0]["id"] == "request-0"
        assert [p["event"] for p in detail["phases"]] == ["scenario_started", "scenario_finished"]
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)


async def test_startup_and_read_only_requests_leave_runs_directory_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost:8787") as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/api/bootstrap")).status_code == 200
            assert (await client.get("/api/runs")).json()["runs"] == []
            assert (await client.get("/api/baselines")).json()["baselines"] == []
            assert (await client.get("/api/runs/missing")).status_code == 404
            assert (await client.post("/api/runs", json={})).status_code == 422
    assert not (tmp_path / "runs").exists()


async def test_first_run_and_baseline_create_only_their_required_directories(tmp_path, fake_runner):
    runs = tmp_path / "nested" / "custom-runs"
    async with browser(runs) as client:
        assert not runs.exists()
        identifier = await start(client)
        await finished(client, identifier)
        assert (runs / identifier / "report.json").is_file()
        assert not (runs / ".baselines").exists()
        assert (await client.get("/api/baselines")).json()["baselines"] == []
        assert not (runs / ".baselines").exists()
        response = await client.post("/api/baselines", json={"run_id": identifier, "name": "First"})
        assert response.status_code == 201
        baseline = response.json()
        assert (runs / ".baselines" / baseline["id"] / "report.json").is_file()
        assert (await client.get("/api/baselines")).json()["baselines"] == [baseline]


async def test_dropped_offer_count_in_live_judging_and_reopened_payload(tmp_path, monkeypatch):
    ready, release = asyncio.Event(), asyncio.Event()

    async def waiting(configuration, *, progress, stop_event):
        progress({"event": "offer_dropped", "budget_used": 4, "attempted": 2,
                  "completed": 2, "dropped_offers": 2})
        progress({"event": "judging_started", "phase": "judging", "budget_used": 4,
                  "attempted": 2, "completed": 2, "dropped_offers": 2})
        ready.set()
        await release.wait()
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await asyncio.wait_for(ready.wait(), timeout=1)
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        assert detail["state"] == "running"
        assert detail["progress"]["phase"] == "judging"
        assert detail["progress"]["dropped_offers"] == 2
        assert detail["progress"]["budget_used"] > detail["progress"]["attempted"]
        assert detail["events"][0]["dropped_offers"] == 2
        release.set()
        await finished(client, identifier)
    async with browser(tmp_path) as client:
        saved = (await client.get(f"/api/runs/{identifier}")).json()
        assert saved["state"] == "completed"
        assert saved["progress"]["dropped_offers"] == 2
