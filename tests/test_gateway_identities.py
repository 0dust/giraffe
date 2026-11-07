"""Identity selection must be isolated on the actual pooled request boundary."""
import asyncio
import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from giraffe.client import LLMClient
from giraffe.deployment import safe_config
from giraffe.models import RequestSpec, RunConfig, Target


def request(identity=None):
    return RequestSpec(fixture_id="identity", scenario="gateway", check_ids=[],
                       messages=[{"role": "user", "content": "Say OK"}], identity=identity)


async def test_concurrent_identities_replace_defaults_and_ignore_cookies(monkeypatch):
    for name, value in {"SHARED": "shared-secret", "ALICE": "alice-secret",
                        "BOB": "bob-secret", "TENANT": "tenant-secret"}.items():
        monkeypatch.setenv(name, value)
    target = Target(name="proxy", url="http://proxy", model="test", api_key_env="SHARED",
                    headers_env={"X-Shared": "SHARED"}, test_identities={
                        "alice": {"api_key_env": "ALICE", "headers_env": {"X-Tenant": "TENANT"}},
                        "bob": {"api_key_env": "BOB"}, "anonymous": {}})
    seen = []

    async def handler(req):
        seen.append(dict(req.headers))
        await asyncio.sleep(0)
        value = req.headers.get("authorization", "anonymous").replace("Bearer ", "")
        return httpx.Response(200, headers={"set-cookie": f"auth={value}; Path=/"},
            json={"model": "test", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    async with LLMClient(target, RunConfig(targets=[target], stream=False), max_connections=2) as client:
        await client.execute(request())
        for _ in range(2):
            results = await asyncio.gather(*(client.execute(request(who)) for who in ("alice", "bob", "anonymous")))
            assert all(row.valid for row in results)
        await client.execute(request())
    assert seen[0]["authorization"] == "Bearer shared-secret"
    for row, alias in zip(seen[1:-1], ["alice", "bob", "anonymous"] * 2):
        assert "cookie" not in row and "x-shared" not in row
        assert row.get("authorization") == {"alice": "Bearer alice-secret", "bob": "Bearer bob-secret"}.get(alias)
        assert row.get("x-tenant") == ("tenant-secret" if alias == "alice" else None)
        assert row["content-type"] == "application/json"
    assert seen[-1]["authorization"] == "Bearer shared-secret"
    assert seen[-1]["cookie"] == "auth=shared-secret"


@pytest.mark.parametrize("identity", ["missing", "unknown"])
async def test_missing_identity_credentials_never_fall_back(monkeypatch, identity):
    monkeypatch.delenv("ABSENT_GATEWAY_KEY", raising=False)
    monkeypatch.setenv("SHARED", "shared-secret")
    target = Target(name="proxy", url="http://proxy", model="test", api_key_env="SHARED",
                    test_identities={"missing": {"api_key_env": "ABSENT_GATEWAY_KEY"}})
    seen = []
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(401)), **kw))
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        record = await client.execute(request(identity))
    assert not seen and record.status == "failed"
    assert record.error == "Test identity credentials are unavailable."
    assert record.identity == identity


async def test_identity_probe_never_retains_echoed_secret(monkeypatch):
    secret = "identity-secret-sentinel"
    monkeypatch.setenv("ALICE", secret)
    target = Target(name="proxy", url="http://proxy", model="test",
                    test_identities={"alice": {"api_key_env": "ALICE"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda req: httpx.Response(200,
            headers={"x-backend-id": secret}, json={"id": secret, "model": secret,
                "choices": [{"message": {"content": secret, "reasoning_content": secret}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5}})), **kw))
    async with LLMClient(target, RunConfig(targets=[target], stream=False)) as client:
        record = await client.execute(request("alice"))
    assert record.valid and record.completion_tokens == 5
    assert secret not in record.model_dump_json()
    assert record.output == "" and record.answer_hash is None
    saved = safe_config(RunConfig(targets=[target]).model_dump(mode="json"))
    assert saved["targets"][0]["test_identities"]["alice"]["api_key_env"] == "ALICE"
    assert secret not in json.dumps(saved)


@pytest.mark.parametrize("identity", [
    {"api_key_env": "literal secret"}, {"headers_env": {"Host": "KEY"}},
    {"headers_env": {"X-Bad\r\n": "KEY"}},
    {"headers_env": {"X-Key": "KEY", "x-key": "OTHER"}},
    {"api_key_env": "KEY", "headers_env": {"Authorization": "OTHER"}},
])
def test_identity_config_rejects_ambiguous_or_literal_credentials(identity):
    with pytest.raises(ValidationError):
        Target(name="proxy", url="http://proxy", model="test", test_identities={"alice": identity})


def test_unused_identity_fields_preserve_serialized_contract():
    assert "identity" not in request().model_dump()
    assert "test_identities" not in Target(name="x", url="http://x", model="x").model_dump()


@pytest.mark.parametrize("status,error_type,message,expected", [
    (401, "token_not_found_in_db", "credential echoed", "authentication"),
    (401, "expired_key", "expired", "authentication"),
    (403, "key_model_access_denied", "not allowed", "model_access"),
    (401, "auth_error", "OpenAIException: upstream denied", "unknown"),
    (403, "auth_error", "denied", "unknown"),
    (401, ["auth_error"], "denied", "unknown"),
    (429, "rate_limit_error", "unknown provenance", "unknown"),
])
def test_gateway_error_evidence_is_strict_and_does_not_retain_text(status, error_type, message, expected):
    from giraffe.gateway_response import response_evidence
    evidence = response_evidence(status, json.dumps({"error": {"type": error_type, "message": message}}).encode())
    assert evidence.error_kind == expected
    assert "message" not in evidence.model_dump_json()


async def test_finish_reason_cannot_retain_echoed_credential(monkeypatch):
    monkeypatch.setenv("KEY", "finish-secret")
    target = Target(name="proxy", url="http://proxy", model="test", test_identities={"alice": {"api_key_env": "KEY"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "finish-secret"}]})), **kw))
    async with LLMClient(target, RunConfig(targets=[target], stream=False)) as client:
        result = await client.execute(request("alice"))
    assert "finish-secret" not in result.model_dump_json()


async def test_identity_dependency_logging_is_scoped_through_body_cleanup(monkeypatch, caplog):
    monkeypatch.setenv("KEY", "transport-secret")
    target = Target(name="proxy", url="http://proxy", model="test", test_identities={"alice": {"api_key_env": "KEY"}})

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            logging.getLogger("httpcore.http11").debug("body transport-secret")
            yield b'{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}'

        async def aclose(self):
            logging.getLogger("httpcore.connection").debug("close transport-secret")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Body(), extensions={"reason_phrase": b"transport-secret"})), **kw))
    with caplog.at_level(logging.DEBUG):
        async with LLMClient(target, RunConfig(targets=[target], stream=False)) as client:
            await client.execute(request("alice"))
            assert "transport-secret" not in caplog.text
            await client.execute(request())
    assert "transport-secret" in caplog.text  # Ordinary shared-key logging remains enabled.
