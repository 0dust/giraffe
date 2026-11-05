from types import SimpleNamespace

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import RunConfig, Target
from tests.test_client import setup_client, spec


async def test_readiness_deadline_at_zero_is_expired(monkeypatch):
    client, _, calls = setup_client(monkeypatch)
    monkeypatch.setattr("giraffe.client.time", SimpleNamespace(monotonic=lambda: 10.0))

    async with client:
        ready = await client.ready(deadline=10.0)

    assert ready is False
    assert calls == []


@pytest.mark.parametrize(("field", "message", "nested"), [
    ("logprobs", "Unsupported logprobs; echoed secret-request-token", True),
    ("top_logprobs", "Unknown field top_logprobs; echoed secret-request-token", False),
    ("stream_options", "Does not support stream_options; echoed secret-request-token", True),
])
async def test_free_text_rejection_retains_only_known_field(monkeypatch, field, message, nested):
    target = Target(name="local", url="http://local", model="test")
    original = httpx.AsyncClient
    body = {"error": {"message": message} if nested else message}
    monkeypatch.setattr("giraffe.client.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=body)), **kwargs,
    ))

    async with LLMClient(target, RunConfig(targets=[target])) as client:
        result = await client.execute(spec(workload={"performance_evidence_probe": True}))

    assert result.status == "failed" and result.http_status == 400
    assert result.error == "HTTP 400"
    assert result.workload == {
        "performance_evidence_probe": True, "rejected_evidence_field": field,
    }
    assert "secret-request-token" not in result.model_dump_json()
