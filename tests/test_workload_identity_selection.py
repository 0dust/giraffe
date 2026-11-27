"""Version selection follows the builders used by selected executor paths."""

import pytest

from giraffe.deployment import fixture_hash
from giraffe.models import RunConfig
from giraffe.runner import build_workload_identity
from giraffe.workload_versions import WORKLOAD_VERSIONS


def config(checks, **settings):
    return RunConfig(targets=[{"name": "service", "url": "http://localhost:8000/v1", "model": "test"}],
                     checks=checks, **settings)


def fingerprint(settings):
    identity, _ = build_workload_identity(settings)
    return fixture_hash(identity)


@pytest.mark.parametrize(("key", "affected", "unaffected"), [
    ("prefix", "prefix", "consistency"),
    ("consistency", "consistency", "prefix"),
    ("tools", "tools", "prefix"),
    ("tools", "tools", "consistency"),
])
def test_independent_builder_bumps_only_change_consuming_identity(monkeypatch, key, affected, unaffected):
    consuming = config([affected])
    independent = config([unaffected])
    before_consuming, before_independent = fingerprint(consuming), fingerprint(independent)
    monkeypatch.setitem(WORKLOAD_VERSIONS, key, WORKLOAD_VERSIONS[key] + 1)
    assert fingerprint(consuming) != before_consuming
    assert fingerprint(independent) == before_independent


@pytest.mark.parametrize(("checks", "settings", "uses_planner"), [
    (["sessions"], {"sessions": {"profile": "performance", "modes": ["live"],
                                  "workloads": [{"name": "chat", "turns": ["First", "Second"]}]}}, True),
    (["multi_user"], {"multi_user": {"conversations": {}}}, True),
    (["sessions"], {"sessions": {"modes": ["fixed"]}}, False),
])
def test_session_plan_tracks_calls_without_sampling(monkeypatch, checks, settings, uses_planner):
    configured = config(checks, **settings)
    before = fingerprint(configured)
    identity, _ = build_workload_identity(configured)
    assert ("session_plan" in identity["workload_versions"]) is uses_planner
    monkeypatch.setitem(WORKLOAD_VERSIONS, "session_plan", WORKLOAD_VERSIONS["session_plan"] + 1)
    assert (fingerprint(configured) != before) is uses_planner


@pytest.mark.parametrize("selected", ["prefix", "consistency", "tools", "sessions", "multi_user", "load"])
def test_shared_identity_keeps_only_selected_shared_builder_versions(selected):
    base = config([selected])
    with_gateway = config([selected, "gateway_access"], gateway_access={})
    _, shared = build_workload_identity(with_gateway)
    assert shared["shared_workload_fixture_sha256"] == fingerprint(base)


@pytest.mark.parametrize(("key", "unaffected"), [
    ("prefix", "consistency"), ("consistency", "prefix"),
    ("tools", "prefix"), ("tools", "consistency"),
])
async def test_builder_bumps_isolate_report_fixture_digests(monkeypatch, key, unaffected):
    import httpx
    from giraffe import runner

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "K7P4"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 32, "completion_tokens": 4},
        })

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))

    async def digest(check):
        configured = config([check], stream=False, prefix={"prefix_chars": 128, "repeats": 2, "history_turns": 2},
                            consistency={"shapes": ["short"], "levels": [1], "prompts_per_shape": 2,
                                         "repetitions": 2})
        report = await runner.run_suite(configured)
        assert any(check in request.check_ids for request in report.requests)
        return report.manifest["fixture_pack"]["workload_fixture_sha256"]

    before = await digest(key), await digest(unaffected)
    monkeypatch.setitem(WORKLOAD_VERSIONS, key, WORKLOAD_VERSIONS[key] + 1)
    assert await digest(key) != before[0]
    assert await digest(unaffected) == before[1]


def test_conversation_users_include_population_and_schedule_version(monkeypatch):
    configured = config(["multi_user"], multi_user={"conversations": {}})
    identity, _ = build_workload_identity(configured)
    assert set(identity["workload_versions"]) == {"multi_user", "fixed_sessions", "session_plan"}
    before = fixture_hash(identity)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "multi_user", WORKLOAD_VERSIONS["multi_user"] + 1)
    assert fingerprint(configured) != before
