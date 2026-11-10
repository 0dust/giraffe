"""Policy deadlines cancel blocked requests; fixture credentials are exact."""
import asyncio

import httpx

from giraffe import identities, runner
from giraffe.client import LLMClient
from giraffe.gateway_policy import execute, result
from tests.test_gateway_policy import configuration, exercise


async def test_policy_deadline_cancels_blocked_transport_and_cleans_up(monkeypatch):
    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")
    entered, cleaned = set(), set()
    blocked = asyncio.Event()

    async def handle(request):
        credential = request.headers["authorization"]
        entered.add(credential)
        try:
            await blocked.wait()
        finally:
            cleaned.add(credential)
        raise AssertionError("Blocked requests must be cancelled")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    cfg = configuration(duration_seconds=.2)
    state = runner._Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
        await asyncio.wait_for(execute(state, cfg.targets[0], client), timeout=1)
    summary = result(state, cfg.targets[0]).metrics["gateway_policy"]
    assert entered == cleaned == {"Bearer limited-secret", "Bearer peer-secret"}
    assert len(state.records) == 2
    assert all(record.status == "cancelled" for record in state.records)
    assert [user["cancelled"] for user in summary["users"]] == [1, 1]
    assert summary["stop_reason"] == "Policy traffic duration reached."
    assert summary["complete"] is False
    assert state.slots.qsize() == 2
    assert not any(state.active.values())
    assert all(row["running"] == 0 and row["status"] == "finished" for row in state.conditions.values())
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]


async def test_policy_fixture_rejects_unrelated_peer_credential(monkeypatch):
    original = identities.identity_headers

    def wrong_peer(target, identity):
        headers = original(target, identity)
        if identity == "peer":
            headers["Authorization"] = "Bearer unrelated-secret"
        return headers

    monkeypatch.setattr(identities, "identity_headers", wrong_peer)
    check, state, _ = await exercise(monkeypatch)
    peer = [record for record in state.records if record.identity == "peer"]
    assert peer and all(record.http_status == 401 for record in peer)
    assert check.status == "inconclusive"
