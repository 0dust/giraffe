import asyncio
import json
import time

import httpx
import pytest

from giraffe import concurrent_load, deployment
from giraffe.models import RunConfig, Target
from giraffe.planning import request_plan


def configuration(discovery="none", **kwargs):
    return RunConfig(targets=[Target(name="local", url="http://server/proxy/v1", model="test-model",
                                    deployment={"discovery": discovery})], checks=["load"], **kwargs)


@pytest.mark.parametrize("owner", ["vllm", "sglang"])
async def test_probe_models_context_is_path_relative_and_private(monkeypatch, owner):
    config = configuration()
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": [
            {"id": "other", "max_model_len": 10},
            {"id": "test-model", "max_model_len": 4096, "owned_by": owner, "root": "/private/model"}]})

    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    value = await deployment.probe_context_window(config.targets[0], config)
    assert value["value"] == 4096 and value["source"] == "/v1/models max_model_len"
    assert value["collected_at"]
    assert seen == ["/proxy/v1/models"]
    assert "/private" not in json.dumps(value)


def test_plan_uses_reported_context_and_warns_per_target():
    config = configuration()
    unknown = request_plan(config)
    warning, = [w for w in unknown["load_warnings"] if w.get("kind") == "context_window_unknown"]
    assert warning["target"] == "local"
    assert warning["msg"] == "Context window unknown. Set the server's context window, or connect to a server that reports it."
    plan = request_plan(config, context_windows={"local": {"value": 16, "source": "test"}})
    assert plan["context_windows"] == {"local": {"value": 16, "source": "test"}}
    assert any(w.get("kind") == "context_window_exceeded" for w in plan["load_warnings"])
    assert not any(w.get("kind") == "context_window_unknown" for w in plan["load_warnings"])


async def test_snapshot_carries_preflight_without_discovery():
    config = configuration()
    config._preflight_context_windows = {"local": {"value": 4096, "source": "test", "collected_at": "2026-11-25T00:00:00Z"}}
    result = await deployment.snapshot(config.targets[0], config, time.monotonic()+5, asyncio.Event())
    assert concurrent_load.context_window(config.targets[0], {"deployment": result}) == (
        4096, "deployment.fields.serving.context_limit.reported (preflight test)")
    assert {"status": "not_enabled"} in result["discovery"]
    assert "serving.context_limit" not in result["unknown_fields"]


@pytest.mark.parametrize("engine,path,body", [
    ("llamacpp", "/props", {"total_slots": 1, "default_generation_settings": {"n_ctx": 4096}}),
    ("tgi", "/info", {"router": "text-generation-router", "model_id": "test-model", "max_total_tokens": 4096}),
    ("sglang", "/get_server_info", {"server_args": {"context_length": 4096}}),
    ("ollama", "/api/ps", {"models": [None, "invalid", {"model": "other", "context_length": 1},
                                        {"name": "test-model", "context_length": 4096}]}),
])
async def test_probe_prefers_configured_engine_route(monkeypatch, engine, path, body):
    config = configuration(engine)
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    value = await deployment.probe_context_window(config.targets[0], config)
    assert value["value"] == 4096 and value["source"] == engine + " " + path
    assert seen == [path]


@pytest.mark.parametrize("engine,body", [
    ("ollama", {"models": []}), ("lmdeploy", {"data": [{"id": "test-model", "owned_by": "lmdeploy"}]}),
    ("tensorrt-llm", {"data": [{"id": "test-model"}]}),
    *(('none', {"data": [{"id": "test-model", "max_model_len": value}]})
      for value in [True, 0, "4096", -1, 4096.0, None]),
    ("none", {"data": [{"id": "other", "max_model_len": 4096}]}),
])
async def test_probe_missing_or_invalid_context_returns_none(monkeypatch, engine, body):
    config = configuration(engine)
    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    assert await deployment.probe_context_window(config.targets[0], config) is None


@pytest.mark.parametrize("failure", ["404", "garbage", "timeout", "deadline"])
async def test_probe_failures_never_raise(monkeypatch, failure):
    config = configuration()

    async def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private server text")
        if failure == "deadline":
            await asyncio.sleep(1)
        return httpx.Response(404 if failure == "404" else 200, text="garbage")

    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    assert await deployment.probe_context_window(config.targets[0], config, timeout=.05) is None


async def test_engine_route_failure_falls_back_to_models(monkeypatch):
    config = configuration("sglang")
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return (httpx.Response(404) if request.url.path == "/get_server_info" else
                httpx.Response(200, json={"data": [{"id": "test-model", "max_model_len": 4096}]}))

    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    assert (await deployment.probe_context_window(config.targets[0], config))["value"] == 4096
    assert seen == ["/get_server_info", "/proxy/v1/models"]


async def test_probe_targets_concurrently_and_omits_unknown(monkeypatch):
    config = configuration()
    config.targets.append(Target(name="other", url="http://server", model="other"))
    started = set()

    async def probe(target, config):
        started.add(target.name)
        for _ in range(10):
            if len(started) == 2:
                break
            await asyncio.sleep(0)
        assert len(started) == 2
        return {"value": 4096, "source": "test", "collected_at": "now"} if target.name == "local" else None

    monkeypatch.setattr(deployment, "probe_context_window", probe)
    assert await deployment.probe_context_windows(config) == {
        "local": {"value": 4096, "source": "test", "collected_at": "now"}}


@pytest.mark.parametrize("discovery", ["none", "vllm"])
async def test_snapshot_preserves_conflicts_and_fresh_readings(monkeypatch, discovery):
    config = configuration(discovery)
    config.targets[0].deployment.serving["context_limit"] = 8192
    config._preflight_context_windows = {"local": {"value": 2048, "source": "test", "collected_at": "2026-11-25T00:00:00Z"}}
    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=(
            {"version": "test"} if request.url.path == "/version" else
            {"data": [{"id": "test-model", "max_model_len": 4096}]})))))
    result = await deployment.snapshot(config.targets[0], config, time.monotonic()+5, asyncio.Event())
    fact = result["fields"]["serving.context_limit"]
    assert fact["status"] == "conflicting"
    assert fact["reported"]["value"] == (2048 if discovery == "none" else 4096)
    assert fact["reported"]["provenance"] == ("preflight test" if discovery == "none" else "runtime-reported")
    if discovery == "none":
        assert fact["reported"]["collected_at"] == "2026-11-25T00:00:00Z"


def test_plan_known_windows_conflict_and_target_specific_unknowns():
    config = configuration()
    config.targets.append(Target(name="other", url="http://other", model="other"))
    plan = request_plan(config)
    unknown = [w for w in plan["load_warnings"] if w.get("kind") == "context_window_unknown"]
    assert [w["target"] for w in unknown] == ["local", "other"]
    assert unknown[1]["fields"][-1] == "targets.1.deployment.serving.context_limit"
    config.targets.pop()
    config.targets[0].deployment.serving["context_limit"] = 100000
    windows = {"local": {"value": 200000, "source": "test"}}
    plan = request_plan(config, context_windows=windows)
    warning, = [w for w in plan["load_warnings"] if w.get("kind", "").startswith("context_window_")]
    assert warning["kind"] == "context_window_conflict" and not warning.get("blocking")
    assert warning["msg"] == "The local server reports a 200,000-token context window; the configuration says 100,000. Giraffe uses the reported value."
    assert plan["context_windows"]["local"] == {"value": 200000, "source": "test"}
    assert request_plan(config)["context_windows"]["local"] == {"value": 100000, "source": "configuration"}
    config.targets[0].deployment.serving.clear()
    assert not any(w.get("kind", "").startswith("context_window_")
                   for w in request_plan(config, context_windows=windows)["load_warnings"])
    plan = request_plan(config, context_windows={"local": {"value": 16, "source": "test"}})
    warning = next(w for w in plan["load_warnings"] if w.get("kind") == "context_window_exceeded")
    assert warning["fields"] == ["load_test.profile.short.input_tokens", "load_test.profile.short.output_tokens"]
    assert not warning.get("blocking") and warning not in plan["errors"]


async def test_connection_caches_context_for_plan_and_clears_on_reconnect(tmp_path, monkeypatch):
    from tests.test_connection import answer, endpoint
    from tests.test_web import browser, config

    context = 100000

    def respond(request):
        return httpx.Response(200, json=({"data": [{"id": "test-model", "max_model_len": context,
                                                  "root": "/private/model"}]}
                                        if request.method == "GET" else answer()))

    endpoint(monkeypatch, respond)
    values = config(checks=["load"])
    async with browser(tmp_path) as client:
        result = await client.post("/api/targets/test-connection", json={
            "url": values["targets"][0]["url"], "model": "test-model"})
        assert result.status_code == 200 and result.json()["context_window"]["value"] == context
        plan = (await client.post("/api/config/plan", json=values)).json()["request_plan"]
        assert plan["context_windows"]["local"]["value"] == context
        assert not any(w.get("kind") == "context_window_unknown" for w in plan["load_warnings"])
        context = None
        assert (await client.post("/api/targets/test-connection", json={
            "url": values["targets"][0]["url"], "model": "test-model"})).json()["context_window"] is None
        plan = (await client.post("/api/config/plan", json=values)).json()["request_plan"]
        assert plan["context_windows"] == {}
        assert "/private/model" not in json.dumps(plan)


async def test_web_run_receives_cached_window(tmp_path, monkeypatch, local_tokenizer):
    from tests.test_connection import answer, endpoint
    from tests.test_web import browser, config, finished, successful_runner

    received = []

    async def run(configuration, **kwargs):
        received.append(configuration._preflight_context_windows)
        return await successful_runner(configuration, **kwargs)

    monkeypatch.setattr("giraffe.web.run_suite", run)
    endpoint(monkeypatch, lambda request: httpx.Response(200, json=(
        {"data": [{"id": "test-model", "max_model_len": 100000}]}
        if request.method == "GET" else answer())))
    values = config(checks=["load"])
    async with browser(tmp_path) as client:
        assert (await client.post("/api/targets/test-connection", json={
            "url": values["targets"][0]["url"], "model": "test-model"})).status_code == 200
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == 202
        await finished(client, response.json()["id"])
    assert received[0]["local"]["value"] == 100000


async def test_web_context_cache_drops_oldest_after_64(tmp_path, monkeypatch):
    from tests.test_connection import answer, endpoint
    from tests.test_web import browser, config

    endpoint(monkeypatch, lambda request: httpx.Response(200, json=(
        {"data": [{"id": request.url.host, "max_model_len": 100000}]}
        if request.method == "GET" else answer())))
    async with browser(tmp_path) as client:
        for index in range(65):
            response = await client.post("/api/targets/test-connection", json={
                "url": f"http://host{index}/v1", "model": f"host{index}"})
            assert response.status_code == 200
        for index, present in [(0, False), (1, True), (64, True)]:
            values = config(checks=["load"])
            values["targets"][0].update(url=f"http://host{index}/v1", model=f"host{index}")
            plan = (await client.post("/api/config/plan", json=values)).json()["request_plan"]
            assert ("local" in plan["context_windows"]) == present


async def test_connection_probe_deadline_preserves_success(monkeypatch):
    from giraffe import preflight

    original_timeout = asyncio.timeout

    async def connected(target, config):
        return {"status": "connected", "model": target.model}

    async def slow_probe(target, config, timeout, *, detail=False):
        assert 0 < timeout <= .05
        await asyncio.sleep(1)

    monkeypatch.setattr(preflight, "check_target", connected)
    monkeypatch.setattr(deployment, "probe_context_window", slow_probe)
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: original_timeout(.05))
    result = await preflight.check_connection(preflight.ConnectionTest(url="http://server", model="test-model"))
    assert result == {"status": "connected", "model": "test-model", "context_window": None,
                      "context_window_reason": "unavailable"}


async def test_web_cache_handles_invalid_models_url(tmp_path, monkeypatch):
    from tests.test_web import browser, config

    async def connected(body):
        return {"status": "connected", "model": body.model,
                "context_window": {"value": 4096, "source": "test", "collected_at": "now"}}

    def invalid(url):
        raise httpx.InvalidURL("invalid URL")

    monkeypatch.setattr("giraffe.web.check_connection", connected)
    monkeypatch.setattr("giraffe.client.models_url", invalid)
    async with browser(tmp_path) as client:
        response = await client.post("/api/targets/test-connection", json={"url": "http://server", "model": "test-model"})
        assert response.status_code == 200
        response = await client.post("/api/config/plan", json=config(checks=["load"]))
        assert response.status_code == 200 and response.json()["request_plan"]["context_windows"] == {}


async def test_probe_uses_first_valid_duplicate_and_skips_ollama_nondicts(monkeypatch):
    config = configuration("ollama")

    def respond(request):
        return httpx.Response(200, json=(
            {"models": [None, "test-model", {"name": "other", "context_length": 4096}]}
            if request.url.path == "/api/ps" else
            {"data": [{"id": "test-model", "max_model_len": True},
                      {"id": "test-model", "max_model_len": 0},
                      {"id": "test-model", "max_model_len": 4096},
                      {"id": "test-model", "max_model_len": 8192}]}))

    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    assert (await deployment.probe_context_window(config.targets[0], config))["value"] == 4096


async def test_probe_batch_survives_target_exception(monkeypatch):
    config = configuration()
    config.targets.append(Target(name="broken", url="http://broken", model="test"))

    async def probe(target, config):
        if target.name == "broken":
            raise httpx.InvalidURL("private query credentials")
        return {"value": 4096, "source": "test", "collected_at": "now"}

    monkeypatch.setattr(deployment, "probe_context_window", probe)
    assert await deployment.probe_context_windows(config) == {
        "local": {"value": 4096, "source": "test", "collected_at": "now"}}


def test_plan_defaults_to_preflight_windows():
    config = configuration()
    config._preflight_context_windows = {"local": {"value": 100000, "source": "test", "collected_at": "now"}}
    assert request_plan(config)["context_windows"] == {"local": {"value": 100000, "source": "test"}}
    assert request_plan(config, context_windows={})["context_windows"] == {}


async def test_stopped_snapshot_still_uses_preflight_window():
    config = configuration("vllm")
    config._preflight_context_windows = {"local": {"value": 4096, "source": "test", "collected_at": "now"}}
    stop = asyncio.Event()
    stop.set()
    result = await deployment.snapshot(config.targets[0], config, time.monotonic()+5, stop)
    assert {"status": "run_stopped"} in result["discovery"]
    assert result["fields"]["serving.context_limit"]["reported"]["value"] == 4096


@pytest.mark.parametrize("age,live,discovery,expected", [
    (0, None, "none", 4096), (601, None, "none", None),
    (601, 8192, "none", 8192), (0, 8192, "none", 8192),
    (0, None, "vllm", 4096), (0, "expires", "none", None),
])
async def test_web_start_probes_live_with_fresh_matching_cache_fallback(
    tmp_path, monkeypatch, local_tokenizer, age, live, discovery, expected,
):
    from datetime import datetime, timezone
    from tests.test_web import browser, config, finished, successful_runner

    cached = {"value": 4096, "source": "test", "collected_at": datetime.fromtimestamp(
        time.time()-age, timezone.utc).isoformat()}
    received, probes = [], []

    async def connected(body):
        return {"status": "connected", "model": body.model, "context_window": cached}

    async def probe(config):
        probes.append(config.targets[0].deployment.discovery)
        if live == "expires":
            cached["collected_at"] = datetime.fromtimestamp(time.time()-601, timezone.utc).isoformat()
            return {}
        return {"local": {**cached, "value": live}} if live is not None else {}

    async def run(configuration, **kwargs):
        received.append(configuration._preflight_context_windows)
        return await successful_runner(configuration, **kwargs)

    monkeypatch.setattr("giraffe.web.check_connection", connected)
    monkeypatch.setattr("giraffe.web.probe_context_windows", probe)
    monkeypatch.setattr("giraffe.web.run_suite", run)
    values = config(checks=["load"])
    values["targets"][0]["deployment"]["discovery"] = discovery
    async with browser(tmp_path) as client:
        assert (await client.post("/api/targets/test-connection", json={
            "url": values["targets"][0]["url"], "model": "test-model"})).status_code == 200
        preview = (await client.post("/api/config/plan", json=values)).json()["request_plan"]
        assert ("local" in preview["context_windows"]) == (age < 600)
        assert not probes
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == 202
        await finished(client, response.json()["id"])
    assert probes == [discovery]
    assert received[0].get("local", {}).get("value") == expected


@pytest.mark.parametrize("failure", ["404", "garbage", "timeout"])
async def test_failed_reconnect_keeps_cached_window(tmp_path, monkeypatch, failure):
    from tests.test_connection import answer, endpoint
    from tests.test_web import browser, config

    fail = False

    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json=answer())
        if fail:
            if failure == "timeout":
                raise httpx.ReadTimeout("private query credentials")
            return httpx.Response(404 if failure == "404" else 200, text="garbage")
        return httpx.Response(200, json={"data": [{"id": "test-model", "max_model_len": 4096}]})

    endpoint(monkeypatch, respond)
    values = config(checks=["load"])
    body = {"url": values["targets"][0]["url"], "model": "test-model"}
    async with browser(tmp_path) as client:
        assert (await client.post("/api/targets/test-connection", json=body)).json()["context_window"]["value"] == 4096
        fail = True
        response = await client.post("/api/targets/test-connection", json=body)
        assert response.json()["context_window_reason"] == "unavailable"
        preview = (await client.post("/api/config/plan", json=values)).json()["request_plan"]
        assert preview["context_windows"]["local"]["value"] == 4096


@pytest.mark.parametrize("error", [httpx.InvalidURL, httpx.ReadTimeout])
async def test_probe_unavailable_reason_and_logs_do_not_leak(monkeypatch, caplog, error):
    config = configuration()

    def fail(*args):
        raise error("https://server?api_key=private-query")

    monkeypatch.setattr(deployment, "_discovery_client", fail)
    with caplog.at_level("DEBUG", logger="giraffe.deployment"):
        assert await deployment.probe_context_window(config.targets[0], config) is None
        assert await deployment.probe_context_window(config.targets[0], config, detail=True) == (None, "unavailable")
    assert "private-query" not in caplog.text and error.__name__ in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_probe_successful_models_without_window_reports_no_window(monkeypatch):
    config = configuration()
    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": "test-model"}]}))))
    assert await deployment.probe_context_window(config.targets[0], config, detail=True) == (None, "no_window")


@pytest.mark.parametrize("slow", [False, True])
async def test_vllm_snapshot_path_relative_models_and_timeout_keep_version(monkeypatch, slow):
    config = configuration("vllm", metrics_timeout_seconds=.2)
    seen = []

    async def respond(request):
        seen.append(request.url.path)
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test-version"})
        assert request.url.path == "/proxy/v1/models"
        if slow:
            await asyncio.sleep(1)
        return httpx.Response(200, json={"data": [{"id": "test-model", "max_model_len": 4096}]})

    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    result = await deployment.snapshot(config.targets[0], config, time.monotonic()+5, asyncio.Event())
    assert seen == ["/version", "/proxy/v1/models"]
    assert result["fields"]["runtime.version"]["reported"]["value"] == "test-version"
    assert result["discovery"] == [
        {"status": "available", "source": "vllm /version"},
        {"status": "unavailable" if slow else "available", "source": "vllm /v1/models"},
    ]
    assert ("serving.context_limit" in result["fields"]) == (not slow)


def test_exceeded_warnings_only_cover_scheduled_shapes_once_per_source():
    from giraffe.models import LoadTestSettings, MigratedLoadTest

    config = configuration(load_test={
        "workloads": {"combinations": {"selected": ["short_short"]},
                      "mixed": {"enabled": True, "selected": ["short_short", "long_short"]},
                      "interference": {"enabled": True}},
        "patterns": {"concurrent": {"levels": [1, 2]}}})
    config.load_test.migrated_tests.append(MigratedLoadTest(
        source="capacity", settings=LoadTestSettings.model_validate(config.load_test.model_dump(exclude={"migrated_tests"}))))
    windows = {"local": {"value": 1, "source": "test"}}
    plan = request_plan(config, context_windows=windows)
    warnings = [w for w in plan["load_warnings"] if w.get("kind") == "context_window_exceeded"]
    assert len(warnings) == 4
    assert len({tuple(w["fields"]) for w in warnings}) == 4
    assert all(w["msg"].endswith("Giraffe will block those Load test points instead of sending them.") for w in warnings)
    assert any(w["fields"][0].startswith("load_test.migrated_tests.0.settings.") for w in warnings)
    config.load_test.migrated_tests.clear()
    config.load_test.workloads.mixed.enabled = False
    config.load_test.patterns.concurrent.levels = [1]
    # The draft's interference shapes have no scheduled point at concurrency one.
    plan = request_plan(config, context_windows=windows)
    warnings = [w for w in plan["load_warnings"] if w.get("kind") == "context_window_exceeded"]
    assert len(warnings) == 1
    assert warnings[0]["fields"] == ["load_test.profile.short.input_tokens", "load_test.profile.short.output_tokens"]


def test_exceeded_threshold_uses_input_plus_output_without_overhead():
    config = configuration(load_test={"workloads": {"combinations": {"selected": ["short_short"]}}})
    need = config.load_test.profile.short.input_tokens + config.load_test.profile.short.output_tokens
    windows = {"local": {"value": need, "source": "test"}}
    assert not any(w.get("kind") == "context_window_exceeded" for w in
                   request_plan(config, context_windows=windows)["load_warnings"])
    windows["local"]["value"] -= 1
    warning, = [w for w in request_plan(config, context_windows=windows)["load_warnings"]
                if w.get("kind") == "context_window_exceeded"]
    assert warning["msg"] == (f"Short input → Short output needs {need:,} tokens; the local server reports a {need-1:,}-token context window. "
                              "Giraffe will block those Load test points instead of sending them.")


async def test_connect_reuses_discovered_context_without_second_get(monkeypatch):
    from giraffe.preflight import ConnectionTest, check_connection
    from tests.test_connection import answer, endpoint

    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(200, json=(
        {"data": [{"id": "test-model", "max_model_len": False},
                  {"id": "test-model", "max_model_len": 4096, "root": "/private/model"}]}
        if request.method == "GET" else answer())))
    result = await check_connection(ConnectionTest(url="http://server/v1"))
    assert result["context_window"]["value"] == 4096
    assert result["context_window"]["source"] == "/v1/models max_model_len"
    assert result["context_window_reason"] is None
    assert [request.method for request in requests] == ["GET", "POST"]
    assert "/private/model" not in json.dumps(result)


async def test_snapshot_config_namespace_without_private_attribute(monkeypatch):
    from types import SimpleNamespace

    config = configuration("vllm")
    namespace = SimpleNamespace(**config.__dict__)
    monkeypatch.setattr(deployment, "_discovery_client", lambda *a: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=(
            {"version": "test"} if request.url.path == "/version" else {"data": []})))))
    result = await deployment.snapshot(config.targets[0], namespace, time.monotonic()+5, asyncio.Event())
    assert result["fields"]["runtime.version"]["reported"]["value"] == "test"


@pytest.mark.parametrize('value,article', [
    (8, 'an'), (80, 'an'), (800, 'an'), (8192, 'an'), (11, 'an'), (18, 'an'),
    (11000, 'an'), (18432, 'an'), (11000000, 'an'), (18000000, 'an'),
    (700, 'a'), (200000, 'a'), (1800, 'a'), (111, 'a'), (180, 'a'),
    (110000, 'a'), (180000, 'a'), (1000000, 'a'), (16, 'a'),
])
def test_context_window_article(value, article):
    from giraffe.planning import _context_window_article

    assert _context_window_article(value) == article


@pytest.mark.parametrize('source,description', [
    ('/v1/models max_model_len', 'the local server reports an 8,192-token context window'),
    ('configuration', "the local server's configured context window is 8,192 tokens"),
])
def test_context_window_exceeded_wording_uses_source_and_formatted_counts(source, description):
    config = configuration(load_test={
        'profile': {'long': {'input_tokens': 16000}, 'short': {'output_tokens': 8}},
        'workloads': {'combinations': {'selected': ['long_short']}}})
    plan = request_plan(config, context_windows={'local': {'value': 8192, 'source': source}})
    warning, = [w for w in plan['load_warnings'] if w.get('kind') == 'context_window_exceeded']
    assert warning['msg'] == (f'Long input → Short output needs 16,008 tokens; {description}. '
                              'Giraffe will block those Load test points instead of sending them.')
