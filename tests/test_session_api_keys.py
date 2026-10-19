"""Pasted keys authenticate requests without entering persisted configuration."""

import asyncio
import json
import os

import httpx
import pytest

from giraffe.client import _headers
from giraffe.models import RunConfig
from giraffe.web import ConnectionTest, StartRun
from tests.test_connection import answer, endpoint
from tests.test_web import browser, config, finished, report


KEY = "test-only-pasted-credential-7Q42"


def credential(values, key=KEY, index=0):
    target = values["targets"][index]
    return {"target": target["name"], "url": target["url"], "api_key": key}


async def test_paste_discovers_and_connects_without_environment_or_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("GIRAFFE_API_KEY", raising=False)

    def respond(request):
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(401, text=KEY)
        return httpx.Response(200, json={"data": [{"id": "only-model"}]}
                              if request.method == "GET" else answer())

    requests, clients = endpoint(monkeypatch, respond)
    async with browser(tmp_path) as ui:
        response = await ui.post("/api/targets/test-connection", json={
            "url": "http://server/v1", "api_key": f" {KEY} ",
            "api_key_env": "GIRAFFE_API_KEY",
        })
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "connected"
        assert [r.method for r in requests] == ["GET", "POST"]
        assert all(r.headers["Authorization"] == f"Bearer {KEY}" for r in requests)
        assert requests[1].headers["X-Giraffe-Preflight"] == "1"
        assert all(not c["follow_redirects"] for c in clients)
        # A later caller does not inherit credentials from the successful probe.
        unauthenticated = await ui.post("/api/targets/test-connection", json={
            "url": "http://server/v1", "model": "only-model"})
        assert unauthenticated.status_code == 422
        assert KEY not in response.text + unauthenticated.text
        assert (await ui.get("/api/runs")).json()["runs"] == []
    assert "GIRAFFE_API_KEY" not in os.environ
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_private_key_survives_execution_copies_but_not_config_or_repr():
    values = config()
    body = StartRun(config=values, api_keys=[credential(values)])
    body.attach_api_keys(body.config)
    copied = body.config.model_copy(deep=True)
    assert _headers(copied.targets[0])["Authorization"] == f"Bearer {KEY}"
    assert copied.targets[0].api_key_env == "GIRAFFE_API_KEY"
    assert KEY not in body.model_dump_json() + repr(body) + repr(copied)
    assert "api_key" not in copied.model_dump()["targets"][0]
    restored = RunConfig.model_validate_json(copied.model_dump_json())
    assert restored.targets[0]._api_key is None


def test_key_does_not_bleed_into_another_target_or_metrics(monkeypatch):
    from giraffe.telemetry import _metrics_headers

    monkeypatch.setenv("SECOND_KEY", "second-target-key")
    values = config()
    values["targets"].append({"name": "other", "url": "http://other/v1", "model": "test",
                              "api_key_env": "SECOND_KEY"})
    body = StartRun(config=values, api_keys=[credential(values)])
    body.attach_api_keys(body.config)
    first, second = body.config.targets
    assert _headers(first)["Authorization"] == f"Bearer {KEY}"
    assert _headers(second)["Authorization"] == "Bearer second-target-key"
    assert "Authorization" not in _metrics_headers(first)


@pytest.mark.parametrize("field,value", [("url", "http://other/v1"), ("target", "other")])
async def test_mismatched_key_binding_rejected_before_run(tmp_path, field, value):
    values = config()
    key = credential(values) | {field: value}
    async with browser(tmp_path) as ui:
        r = await ui.post("/api/runs", json={"config": values, "api_keys": [key]})
        assert r.status_code == 422 and KEY not in r.text
        assert (await ui.get("/api/runs")).json()["runs"] == []


async def test_duplicate_binding_and_conflicting_authorization_are_rejected(tmp_path):
    values = config()
    async with browser(tmp_path) as ui:
        r = await ui.post("/api/runs", json={"config": values,
                         "api_keys": [credential(values), credential(values)]})
        assert r.status_code == 422
        r = await ui.post("/api/targets/models", json={"url": "http://server",
                         "api_key": KEY, "headers_env": {"authorization": "OTHER"}})
        assert r.status_code == 422 and KEY not in r.text
        values["targets"][0]["headers_env"] = {"Authorization": "OTHER"}
        r = await ui.post("/api/runs", json={"config": values,
                         "api_keys": [credential(values)]})
        assert r.status_code == 422 and KEY not in r.text
        assert "custom Authorization header" in r.text


@pytest.mark.parametrize("bad", [KEY + "\r\nX: value", KEY + "\u00e9", KEY * 1000,
                                  {"bad": KEY}, 123])
async def test_bad_keys_never_echo_in_validation_errors(tmp_path, bad):
    async with browser(tmp_path) as ui:
        for route, payload in [
            ("/api/targets/test-connection", {"url": "http://server", "api_key": bad}),
            ("/api/runs", {"config": config(), "api_keys": [credential(config(), bad)]}),
        ]:
            r = await ui.post(route, json=payload)
            assert r.status_code == 422 and KEY not in r.text


async def test_real_suite_uses_pasted_key_and_saved_surfaces_exclude_it(tmp_path, monkeypatch):
    monkeypatch.delenv("GIRAFFE_API_KEY", raising=False)
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(
        200, json=answer()) if request.headers.get("Authorization") == f"Bearer {KEY}"
        else httpx.Response(401, text=KEY))
    values = config(checks=["access"], max_requests=1, concurrency=1,
                    max_duration_seconds=5, stream=False)
    async with browser(tmp_path) as ui:
        r = await ui.post("/api/runs", json={"config": values, "api_keys": [credential(values)]})
        assert r.status_code == 202, r.text
        identifier = r.json()["id"]
        detail = await finished(ui, identifier)
        assert detail["state"] == "completed", detail
        assert requests and all(r.headers.get("Authorization") == f"Bearer {KEY}" for r in requests)
        assert any(row["valid"] for row in detail["report"]["requests"])
        for route in [f"/api/runs/{identifier}", f"/api/runs/{identifier}/config.json",
                      f"/api/runs/{identifier}/report.json", f"/api/runs/{identifier}/report.html",
                      f"/api/runs/{identifier}/reproduction.json", "/api/bootstrap", "/api/runs"]:
            response = await ui.get(route)
            assert response.status_code == 200 and KEY not in response.text
        saved = (await ui.get(f"/api/runs/{identifier}/config.json")).json()
        assert saved["targets"][0]["api_key_env"] == "GIRAFFE_API_KEY"
        cli = await ui.post("/api/config/cli", json={"config": values, "api_keys": [credential(values)]})
        assert cli.status_code == 200 and KEY not in cli.text and "GIRAFFE_API_KEY" in cli.text
        baseline = await ui.post("/api/baselines", json={"run_id": identifier, "name": "key-test"})
        assert baseline.status_code == 201 and KEY not in baseline.text
    assert "GIRAFFE_API_KEY" not in os.environ
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(), path


async def test_try_example_keeps_key_only_for_selected_target(tmp_path, monkeypatch):
    observed = []

    async def runner(configuration, **kwargs):
        observed.append(_headers(configuration.targets[0]))
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    values = config(request_source="request_pack", request_pack={"version": 1, "cases": [{
        "fixture_id": "one", "messages": [{"role": "user", "content": "Say OK"}],
        "scorer": "exact", "expected": "OK"}]})
    async with browser(tmp_path) as ui:
        r = await ui.post("/api/runs/try-example", json={"config": values, "fixture_id": "one",
                         "target_name": "local", "api_keys": [credential(values)]})
        assert r.status_code == 202, r.text
        detail = await finished(ui, r.json()["id"])
        assert detail["state"] == "completed"
        assert observed[0]["Authorization"] == f"Bearer {KEY}"
        assert KEY not in json.dumps(detail)


async def test_active_run_key_is_not_overwritten_by_another_request(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def runner(configuration, **kwargs):
        started.set()
        await release.wait()
        observed.append(_headers(configuration.targets[0]))
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    values = config()
    async with browser(tmp_path) as ui:
        r = await ui.post("/api/runs", json={"config": values, "api_keys": [credential(values)]})
        await asyncio.wait_for(started.wait(), 2)
        second = await ui.post("/api/runs", json={"config": values,
                               "api_keys": [credential(values, "different-key")]})
        assert second.status_code == 409
        release.set()
        await finished(ui, r.json()["id"])
        assert observed[0]["Authorization"] == f"Bearer {KEY}"
    assert ConnectionTest(url="http://server").target()._api_key is None
