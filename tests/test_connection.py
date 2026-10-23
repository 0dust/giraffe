"""Endpoint setup sends only the explicitly requested, bounded connection probe."""

import asyncio
import json
import ssl

import httpx
import pytest
from fastapi import HTTPException

from giraffe.web import ConnectionTest, check_connection
from tests.test_web import browser, config, report, successful_runner


def answer(**updates):
    return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], **updates}


def endpoint(monkeypatch, handler):
    original = httpx.AsyncClient
    requests, clients = [], []

    async def respond(request):
        requests.append(request)
        response = handler(request)
        return await response if hasattr(response, "__await__") else response

    def client(**kwargs):
        if "transport" not in kwargs:
            clients.append(dict(kwargs))
            # Record proxy selection without bypassing the fake endpoint transport.
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(respond)
        return original(**kwargs)

    monkeypatch.setattr("giraffe.preflight.httpx.AsyncClient", client)
    return requests, clients


@pytest.mark.parametrize("suffix", ["", "/v1", "/v1/chat/completions", "/gateway/v1/"])
async def test_explicit_model_sends_one_bounded_request_and_preserves_auth(monkeypatch, suffix):
    requests, clients = endpoint(monkeypatch, lambda request: httpx.Response(200, json=answer()))
    monkeypatch.setenv("CONNECTION_KEY", "private-key")
    monkeypatch.setenv("CONNECTION_HEADER", "private-header")
    result = await check_connection(ConnectionTest(
        url="http://server" + suffix, model="  manual-model  ", api_key_env=" CONNECTION_KEY ",
        headers_env={"X-Gateway": "CONNECTION_HEADER"}, request_options={"temperature": 0},
    ))
    assert set(result) == {"status", "model", "latency_ms", "message"}
    assert result["status"] == "connected" and result["model"] == "manual-model"
    assert result["latency_ms"] >= 0
    assert len(requests) == 1 and requests[0].method == "POST"
    base = "/gateway/v1" if suffix.startswith("/gateway") else "/v1"
    assert str(requests[0].url) == "http://server" + base + "/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer private-key"
    assert requests[0].headers["X-Gateway"] == "private-header"
    assert requests[0].headers["X-Giraffe-Preflight"] == "1"
    payload = json.loads(requests[0].content)
    assert payload == {"model": "manual-model", "messages": [{"role": "user", "content": "Reply with OK."}],
                       "stream": False, "max_tokens": 32, "n": 1, "temperature": 0}
    assert clients[0]["follow_redirects"] is False and clients[0]["trust_env"] is True
    assert "private" not in str(result)


async def test_single_model_is_discovered_once_and_then_probed(monkeypatch):
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(
        200, json={"data": [{"id": "only-model"}, {"id": "only-model"}]}
        if request.method == "GET" else answer(),
    ))
    result = await check_connection(ConnectionTest(url="http://server", model=" "))
    assert result["status"] == "connected" and result["model"] == "only-model"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert str(requests[0].url) == "http://server/v1/models"
    assert json.loads(requests[1].content)["model"] == "only-model"


@pytest.mark.parametrize("response,models", [
    (httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]}), ["a", "b"]),
    (httpx.Response(200, json={"data": []}), []),
    (httpx.Response(404, text="private body"), []),
    (httpx.Response(405, text="private body"), []),
    (httpx.Response(302, headers={"Location": "https://elsewhere.invalid"}), []),
    (httpx.Response(200, text="private malformed body"), []),
    (httpx.Response(200, content=b"x" * 1_048_577), []),
])
async def test_model_choice_or_manual_entry_never_sends_inference(monkeypatch, response, models):
    requests, _ = endpoint(monkeypatch, lambda request: response)
    result = await check_connection(ConnectionTest(url="http://server"))
    assert result["status"] == "needs_model" and result["models"] == models
    assert len(requests) == 1 and requests[0].method == "GET"
    assert "private" not in str(result)


@pytest.mark.parametrize("model", [None, "manual-model"])
@pytest.mark.parametrize("status", [401, 403])
async def test_denied_auth_is_actionable_and_does_not_retry(monkeypatch, model, status):
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(status, text="private denial"))
    with pytest.raises(HTTPException) as error:
        await check_connection(ConnectionTest(url="http://server", model=model))
    assert error.value.status_code == 422
    assert "authentication" in error.value.detail
    assert "private" not in error.value.detail
    assert len(requests) == 1


@pytest.mark.parametrize("model", [None, "manual-model"])
async def test_missing_credentials_send_no_traffic(monkeypatch, model):
    monkeypatch.delenv("CONNECTION_MISSING", raising=False)
    requests, _ = endpoint(monkeypatch, lambda request: pytest.fail("Unexpected endpoint traffic"))
    with pytest.raises(HTTPException, match="environment variable"):
        await check_connection(ConnectionTest(url="http://server", model=model,
                                             api_key_env="CONNECTION_MISSING"))
    assert not requests


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": "private"}}]}),
    httpx.Response(200, json={"choices": [{"message": {"reasoning": "private"}, "finish_reason": "length"}]}),
    httpx.Response(200, text="private response"),
    httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)),
    httpx.Response(302, headers={"Location": "https://elsewhere.invalid"}),
    httpx.Response(404, text="private model error"),
])
async def test_http_status_alone_does_not_prove_usable_inference(monkeypatch, response):
    requests, _ = endpoint(monkeypatch, lambda request: response)
    with pytest.raises(HTTPException) as error:
        await check_connection(ConnectionTest(url="http://server", model="model"))
    assert error.value.status_code == 422
    assert "private" not in error.value.detail
    assert len(requests) == 1


async def test_same_ca_proxy_and_environment_transport_apply_to_discovery_and_probe(monkeypatch):
    trust = ssl.create_default_context()
    ca_files = []
    monkeypatch.setattr("giraffe.preflight.ssl.create_default_context",
                        lambda *, cafile: ca_files.append(cafile) or trust)
    requests, clients = endpoint(monkeypatch, lambda request: httpx.Response(
        200, json={"data": [{"id": "model"}]} if request.method == "GET" else answer(),
    ))
    result = await check_connection(ConnectionTest(url="https://server", ca_bundle="private-ca.pem",
                                                 proxy="http://proxy:8080"))
    assert result["status"] == "connected" and len(requests) == 2
    assert ca_files == ["private-ca.pem", "private-ca.pem"]
    assert all(client["verify"] is trust and client["proxy"] == "http://proxy:8080"
               and client["trust_env"] and not client["follow_redirects"] for client in clients)


@pytest.mark.parametrize("model", [None, "model"])
async def test_bad_ca_file_fails_without_connecting(monkeypatch, model):
    requests, _ = endpoint(monkeypatch, lambda request: pytest.fail("Unexpected endpoint traffic"))
    with pytest.raises(HTTPException, match="TLS trust file") as error:
        await check_connection(ConnectionTest(url="https://server", model=model,
                                             ca_bundle="/missing/private-ca.pem"))
    assert "private-ca" not in error.value.detail and not requests


@pytest.mark.parametrize("model", [None, "model"])
async def test_network_errors_are_sanitized(monkeypatch, model):
    def disconnected(request):
        raise httpx.ConnectError("private transport details", request=request)

    requests, _ = endpoint(monkeypatch, disconnected)
    with pytest.raises(HTTPException) as error:
        await check_connection(ConnectionTest(url="https://server", model=model))
    assert "private" not in error.value.detail
    assert "TLS" in error.value.detail and len(requests) == 1


async def test_connection_deadline_does_not_retry(monkeypatch):
    timeout = asyncio.timeout
    monkeypatch.setattr("giraffe.preflight.asyncio.timeout", lambda seconds: timeout(.01))

    async def slow(request):
        await asyncio.sleep(.1)
        return httpx.Response(200, json=answer())

    requests, _ = endpoint(monkeypatch, slow)
    with pytest.raises(HTTPException, match="timed out after 15 seconds"):
        await check_connection(ConnectionTest(url="http://server", model="model"))
    assert len(requests) == 1


async def test_api_validates_sensitive_inputs_and_creates_no_run_artifacts(tmp_path, monkeypatch):
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(200, json=answer()))
    async with browser(tmp_path / "runs") as client:
        for invalid in [{"url": "http://user:PRIVATE@server"},
                        {"url": "http://server?key=PRIVATE"},
                        {"url": "file:///PRIVATE"},
                        {"url": "http://server", "proxy": "http://user:PRIVATE@proxy"},
                        {"url": "http://server", "request_options": {"max_tokens": 2000}}]:
            response = await client.post("/api/targets/test-connection", json=invalid)
            assert response.status_code == 422
            assert "PRIVATE" not in response.text
        assert not requests
        response = await client.post("/api/targets/test-connection", json={"url": "http://server"},
                                     headers={"Origin": "https://elsewhere.invalid"})
        assert response.status_code == 403 and not requests
        assert (await client.get("/api/bootstrap")).json()["config"]["targets"][0]["model"] == ""
        response = await client.post("/api/targets/test-connection",
                                     json={"url": "http://server", "model": "model"})
        assert response.status_code == 200 and response.json()["status"] == "connected"
        assert (await client.get("/api/runs")).json()["runs"] == []
    assert not list(tmp_path.iterdir())


async def test_active_run_blocks_extra_connection_traffic(tmp_path, monkeypatch, local_tokenizer):
    async def running(configuration, *, progress, stop_event):
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", running)
    requests, _ = endpoint(monkeypatch, lambda request: pytest.fail("Unexpected endpoint traffic"))
    async with browser(tmp_path) as client:
        assert (await client.post("/api/runs", json={"config": config()})).status_code == 202
        response = await client.post("/api/targets/test-connection",
                                     json={"url": "http://server", "model": "model"})
        assert response.status_code == 409 and not requests


@pytest.mark.parametrize("status", [200, 503])
async def test_pending_probe_blocks_runs_and_other_probes_then_releases(tmp_path, monkeypatch, status):
    from tests.test_cli import operator_pack

    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(request):
        entered.set()
        await release.wait()
        return httpx.Response(status, json=answer())

    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)
    requests, _ = endpoint(monkeypatch, pending)
    values = config(request_source="request_pack", request_pack=operator_pack(), checks=["correctness"])
    async with browser(tmp_path) as client:
        payload = {"url": "http://server", "model": "model"}
        probe = asyncio.create_task(client.post("/api/targets/test-connection", json=payload))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            assert (await client.post("/api/targets/test-connection", json=payload)).status_code == 409
            assert (await client.post("/api/runs", json={"config": values})).status_code == 409
            response = await client.post("/api/runs/try-example", json={
                "config": values, "target_name": "local",
                "fixture_id": values["request_pack"]["cases"][0]["fixture_id"],
            })
            assert response.status_code == 409
            assert len(requests) == 1
            assert (await client.get("/api/runs")).json()["runs"] == []
        finally:
            release.set()
            response = await probe
        assert response.status_code == (200 if status == 200 else 422)
        # Both success and endpoint failure release the reservation.
        assert (await client.post("/api/runs", json={"config": values})).status_code == 202


@pytest.mark.parametrize("model", [None, "model"])
async def test_invalid_proxy_is_connection_error_not_manual_model_prompt(model):
    with pytest.raises(HTTPException) as error:
        await check_connection(ConnectionTest(url="http://server", model=model,
                                             proxy="invalid://proxy"))
    assert error.value.status_code == 422 and "proxy" in error.value.detail


@pytest.mark.parametrize("status", [429, 500, 503, "timeout"])
async def test_ui_keeps_rejecting_advisory_cli_probe_failures(monkeypatch, status):
    def respond(request):
        if status == "timeout":
            raise httpx.ReadTimeout("private timeout", request=request)
        return httpx.Response(status, text="private server body")

    requests, _ = endpoint(monkeypatch, respond)
    with pytest.raises(HTTPException) as error:
        await check_connection(ConnectionTest(url="http://server", model="model"))
    assert error.value.status_code == 422
    assert "private" not in error.value.detail
    assert len(requests) == 1
