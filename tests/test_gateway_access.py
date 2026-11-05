"""Declared gateway policies require complete, qualified response evidence."""
import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import RunConfig, Target
from giraffe.runner import _Run


def config(**changes):
    return RunConfig(targets=[Target(name="proxy", url="http://proxy/v1", model="test",
        route="gateway", test_identities={"allowed": {"api_key_env": "ALLOW_KEY"},
                                          "denied": {"api_key_env": "DENY_KEY"}})],
        checks=["gateway_access"], stream=False, max_duration_seconds=10,
        gateway_access={"profile": "litellm", "cases": [
            {"name": "valid", "identity": "allowed", "expected": "allow"},
            {"name": "invalid", "identity": "denied", "expected": "deny"}]}, **changes)


@pytest.mark.parametrize("status,error_type,body,expected", [
    (401, "token_not_found_in_db", None, "pass"),
    (403, "key_model_access_denied", None, "pass"),
    (200, None, "answer", "fail"),
    (401, "upstream_error", None, "inconclusive"),
    (429, "rate_limit_error", None, "inconclusive"),
    (500, None, None, "inconclusive"),
    (302, None, None, "inconclusive"),
    (200, None, "", "inconclusive"),
    (None, None, None, "inconclusive"),
])
async def test_policy_matrix_uses_evidence_not_failed_requests(monkeypatch, status, error_type, body, expected):
    from giraffe.gateway_access import execute, result

    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    seen = []

    def handler(req):
        seen.append(req)
        if req.headers["authorization"] == "Bearer allow-secret":
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        if status is None:
            raise httpx.ConnectError("deny-secret")
        if status == 200:
            return httpx.Response(200, json={"choices": [{"message": {"content": body}, "finish_reason": "stop"}]})
        return httpx.Response(status, json={"error": {"type": error_type, "message": "deny-secret"}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config()
    state = _Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(state, cfg.targets[0], client)
    check = result(state, cfg.targets[0])
    assert check.status == expected
    assert len(seen) == 2
    assert state.errors == 0
    assert check.metrics["gateway_access"]["cases"][0]["observed"] == "allow"
    assert "deny-secret" not in json.dumps(check.model_dump(mode="json"))
    assert all(record.check_ids == ["gateway_access"] for record in state.records)


async def test_missing_identity_sends_nothing_and_cannot_pass(monkeypatch):
    from giraffe.gateway_access import execute, result
    monkeypatch.delenv("ALLOW_KEY", raising=False)
    monkeypatch.delenv("DENY_KEY", raising=False)
    cfg = config()
    state = _Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(state, cfg.targets[0], client)
    assert not state.records
    assert result(state, cfg.targets[0]).status == "inconclusive"


async def test_full_runner_keeps_policy_separate_and_preserves_exports(monkeypatch, tmp_path, local_tokenizer):
    from giraffe.runner import run_suite
    from giraffe.planning import request_plan
    from giraffe.reporting import reproduction_export, write_report

    monkeypatch.setenv("ALLOW_KEY", "sensitive-allow-value")
    monkeypatch.setenv("DENY_KEY", "sensitive-deny-value")
    calls = []
    def handler(req):
        if req.method != "POST":
            return httpx.Response(404)
        calls.append(req)
        if req.headers.get("authorization") == "Bearer sensitive-deny-value":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db", "message": "sensitive-deny-value"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "sensitive-allow-value"}, "finish_reason": "stop"}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config()
    plan = request_plan(cfg)
    assert next(row for row in plan["rows"] if row["id"] == "gateway_access")["requests_per_target"] == 2
    report = await run_suite(cfg)
    check = next(check for check in report.checks if check.id == "gateway_access")
    assert check.status == "pass"
    assert len(calls) == 2
    exported = reproduction_export(report)
    assert exported["config"]["gateway_access"]["cases"] == cfg.gateway_access.model_dump()["cases"]
    assert exported["config"]["targets"][0]["test_identities"]["allowed"]["api_key_env"] == "ALLOW_KEY"
    write_report(report, tmp_path)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "sensitive-allow-value" not in path.read_text()
            assert "sensitive-deny-value" not in path.read_text()


@pytest.mark.parametrize("change", [{"max_requests": 1}, {"gateway_access": None},
    {"gateway_access": {"profile": "unknown", "cases": []}},
    {"gateway_access": {"profile": "other-gateway", "cases": []}},
    {"gateway_access": {"profile": "litellm", "cases": [{"name": "only", "identity": "allowed", "expected": "allow"}]}}])
async def test_incomplete_matrix_never_passes(monkeypatch, change):
    from giraffe.gateway_access import execute, result
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    cfg = RunConfig.model_validate({**config().model_dump(), **change})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})), **kw))
    state = _Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(state, cfg.targets[0], client)
    assert result(state, cfg.targets[0]).status == "inconclusive"
