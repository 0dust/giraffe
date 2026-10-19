"""Model discovery reads metadata only and never exposes authentication values."""

import httpx
import pytest
from fastapi import HTTPException

from giraffe.web import ModelDiscovery, discover_models
from tests.test_web import browser


@pytest.mark.parametrize("suffix", ["", "/v1", "/v1/chat/completions"])
async def test_discovery_normalizes_url_and_preserves_auth(monkeypatch, suffix):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-a"},
                                                 {"id": "model-b"}, {"id": ""}, {}]})

    client = httpx.AsyncClient
    monkeypatch.setenv("GIRAFFE_TEST_KEY", "private-key")
    monkeypatch.setenv("GIRAFFE_TEST_HEADER", "private-gateway-key")
    monkeypatch.setattr("giraffe.preflight.httpx.AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    result = await discover_models(ModelDiscovery(url="http://server" + suffix,
                                  api_key_env="GIRAFFE_TEST_KEY",
                                  headers_env={"X-Gateway": "GIRAFFE_TEST_HEADER"}))
    assert result["models"] == ["model-a", "model-b"]
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "http://server/v1/models"
    assert requests[0].headers["Authorization"] == "Bearer private-key"
    assert requests[0].headers["X-Gateway"] == "private-gateway-key"
    assert "private" not in str(result)


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="private error"),
    httpx.Response(403, text="private error"),
    httpx.Response(302, headers={"Location": "https://elsewhere.invalid"}),
    httpx.Response(404, text="private error"),
    httpx.Response(200, text="private error"),
    httpx.Response(200, json={"private": "error"}),
    httpx.Response(200, content=b"x" * 1_048_577),
])
async def test_discovery_errors_are_bounded_and_do_not_echo_remote_body(monkeypatch, response):
    requests = []

    def respond(request):
        requests.append(request)
        return response

    client = httpx.AsyncClient
    monkeypatch.setattr("giraffe.preflight.httpx.AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(HTTPException) as error:
        await discover_models(ModelDiscovery(url="http://server"))
    assert error.value.status_code == 422
    assert "private error" not in error.value.detail
    assert len(requests) == 1


async def test_discovery_missing_auth_does_not_connect(monkeypatch):
    monkeypatch.delenv("GIRAFFE_MISSING_KEY", raising=False)
    monkeypatch.setattr("giraffe.preflight.httpx.AsyncClient", lambda **kw: pytest.fail("Connected"))
    with pytest.raises(HTTPException, match="authentication environment variable"):
        await discover_models(ModelDiscovery(url="http://server", api_key_env="GIRAFFE_MISSING_KEY"))


async def test_discovery_network_error_is_actionable(monkeypatch):
    def respond(request):
        raise httpx.ConnectError("private exception", request=request)

    client = httpx.AsyncClient
    monkeypatch.setattr("giraffe.preflight.httpx.AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(HTTPException) as error:
        await discover_models(ModelDiscovery(url="http://server"))
    assert "server is running" in error.value.detail
    assert "private exception" not in error.value.detail


async def test_discovery_api_validates_urls_and_rejects_cross_origin(tmp_path):
    async with browser(tmp_path) as client:
        for url in ["file:///private", "http://user:secret@server/v1", "http://server/v1?key=secret"]:
            response = await client.post("/api/targets/models", json={"url": url})
            assert response.status_code == 422
            assert "secret" not in response.text
        response = await client.post("/api/targets/models", json={"url": "http://server"},
                                     headers={"Origin": "https://elsewhere.invalid"})
        assert response.status_code == 403
        assert (await client.get("/api/runs")).json()["runs"] == []
