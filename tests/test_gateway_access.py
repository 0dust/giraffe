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
    (401, "auth_error", "Authentication Error, custom identity backend connection refused", "inconclusive"),
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
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        if status is None:
            raise httpx.ConnectError("deny-secret")
        if status == 200:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": body}, "finish_reason": "stop"}]})
        return httpx.Response(status, json={"error": {"type": error_type, "message": body or "deny-secret"}})

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


@pytest.mark.parametrize("param,expected", [
    ("model", "model_access"),
    (None, "model_access"),
    ("search_tool_name", "unknown"),
    ("team_id", "unknown"),
])
def test_403_denial_must_refer_to_model_access(param, expected):
    from giraffe.gateway_access import denial_kind

    body = json.dumps({"error": {"type": "key_model_access_denied", "code": "403", "param": param}})
    assert denial_kind(403, body) == expected


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


async def test_full_runner_keeps_policy_separate_and_preserves_exports(monkeypatch, tmp_path, local_tokenizer, capsys):
    from giraffe.runner import run_suite
    from giraffe.planning import request_plan
    from giraffe.reporting import reproduction_export, write_report, compare_baseline
    from giraffe.cli import _print_result
    from giraffe.performance import build_performance

    monkeypatch.setenv("ALLOW_KEY", "sensitive-allow-value")
    monkeypatch.setenv("DENY_KEY", "sensitive-deny-value")
    calls = []
    def handler(req):
        if req.method != "POST":
            return httpx.Response(404)
        calls.append(req)
        if req.headers.get("authorization") == "Bearer sensitive-deny-value":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db", "message": "sensitive-deny-value"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "sensitive-allow-value"}, "finish_reason": "stop"}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config(retention="all")
    cfg.targets[0].api_key_env = "UNUSED_SHARED_KEY"
    monkeypatch.delenv("UNUSED_SHARED_KEY", raising=False)
    cfg.stream = True  # Policy probes still use complete responses and skip shared-key setup.
    plan = request_plan(cfg)
    assert next(row for row in plan["rows"] if row["id"] == "gateway_access")["requests_per_target"] == 2
    report = await run_suite(cfg)
    check = next(check for check in report.checks if check.id == "gateway_access")
    assert check.status == "pass"
    assert len(calls) == 2
    assert all(row["condition"]["check"] == "gateway_access"
               for row in report.observations["targets"]["proxy"]["conditions"]
               if row["condition"]["id"] == "gateway_access")
    assert not build_performance(report)["targets"][0]["request_series"]
    exported = reproduction_export(report)
    assert exported["config"]["gateway_access"]["cases"] == cfg.gateway_access.model_dump()["cases"]
    assert exported["config"]["targets"][0]["test_identities"]["allowed"]["api_key_env"] == "ALLOW_KEY"
    assert exported["gateway_access"]["proxy"]["cases"][1]["observed"] == "deny"
    assert RunConfig.model_validate(exported["config"]).gateway_access == cfg.gateway_access
    json_path, html_path = write_report(report, tmp_path, html=True)
    _print_result(report, json_path, html_path, tmp_path, {})
    cli = capsys.readouterr().out
    assert "expected deny, observed deny" in cli
    assert "Declared access cases" in html_path.read_text()
    assert "sensitive-allow-value" not in cli and "sensitive-deny-value" not in cli
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "sensitive-allow-value" not in path.read_text()
            assert "sensitive-deny-value" not in path.read_text()
    altered = report.model_copy(deep=True)
    altered.manifest["config"]["gateway_access"]["cases"][0]["expected"] = "deny"
    comparison = compare_baseline(altered, report)
    assert comparison.baseline["status"] == "inconclusive"
    rows = comparison.baseline["targets"][0]["comparisons"]
    assert len(rows) == 2
    assert all("Gateway access settings differ" in row["reason"] for row in rows)
    assert next(c for c in comparison.checks if c.id == "gateway_access").metrics == check.metrics
    altered = report.model_copy(deep=True)
    altered.manifest["config"]["targets"][0]["test_identities"]["allowed"]["api_key_env"] = "DIFFERENT_KEY"
    comparison = compare_baseline(altered, report)
    target = comparison.baseline["targets"][0]
    assert target["status"] == "inconclusive"
    assert {row["scenario"]: row["status"] for row in target["comparisons"]} == {
        "valid": "inconclusive", "invalid": "pass"}
    assert "identity references" in next(row["reason"] for row in target["comparisons"] if row["scenario"] == "valid")


@pytest.mark.parametrize("mode,expected", [
    ("match", "pass"),
    ("unexpected_access", "fail"),
    ("unverified", "inconclusive"),
    ("unverified_baseline", "inconclusive"),
    ("old_baseline", "inconclusive"),
    ("previous_denial_rules", "inconclusive"),
    ("previous_usage_rules", "inconclusive"),
    ("legacy_source", "pass"),
    ("changed_source", "inconclusive"),
    ("changed_endpoint", "inconclusive"),
    ("changed_proxy", "inconclusive"),
    ("irrelevant_fixture_change", "pass"),
])
async def test_baseline_compares_qualified_access_cases(monkeypatch, mode, expected):
    from giraffe.reporting import compare_baseline
    from giraffe.runner import run_suite

    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    deny_status = 503 if mode == "unverified_baseline" else 401

    def handler(req):
        if req.method != "POST":
            return httpx.Response(404)
        if req.headers["authorization"] == "Bearer deny-secret" and deny_status != 200:
            return httpx.Response(deny_status, json={"error": {"type": "token_not_found_in_db"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config(retention="all")
    baseline = await run_suite(cfg)
    assert baseline.overall == ("inconclusive" if mode == "unverified_baseline" else "pass")
    if mode == "old_baseline":
        check = next(check for check in baseline.checks if check.id == "gateway_access")
        del check.metrics["gateway_access"]["evidence_version"]
    elif mode == "previous_denial_rules":
        check = next(check for check in baseline.checks if check.id == "gateway_access")
        check.metrics["gateway_access"]["evidence_version"] = 2
    elif mode == "previous_usage_rules":
        check = next(check for check in baseline.checks if check.id == "gateway_access")
        check.metrics["gateway_access"]["evidence_version"] = 3
    elif mode == "legacy_source":
        del baseline.manifest["gateway_access_source_sha256"]
    elif mode == "changed_source":
        baseline.manifest["gateway_access_source_sha256"] = "different-source"
    elif mode == "changed_endpoint":
        baseline.manifest["config"]["targets"][0]["url"] = "http://another-proxy/v1"
    elif mode == "changed_proxy":
        baseline.manifest["config"]["proxy"] = "http://forward-proxy"
    elif mode == "irrelevant_fixture_change":
        baseline.manifest["fixture_pack"]["builtin_sha256"] = "different-builtins"
    deny_status = {"unexpected_access": 200, "unverified": 503}.get(mode, 401)
    current = await run_suite(cfg)
    compared = compare_baseline(current, baseline)
    assert compared.baseline["status"] == expected
    assert compared.overall == expected
    target = compared.baseline["targets"][0]
    rows = {row["scenario"]: row for row in target["comparisons"]}
    assert set(rows) == {"valid", "invalid"}
    assert rows["valid"]["baseline"] == rows["valid"]["current"] == "allow"
    assert rows["invalid"]["status"] == expected
    assert rows["invalid"]["baseline"] == ("unverified" if mode == "unverified_baseline" else "deny")
    assert rows["invalid"]["current"] == {
        "unexpected_access": "allow", "unverified": "unverified",
    }.get(mode, "deny")
    assert "collect at least" not in target["summary"]
    if mode in {"old_baseline", "previous_denial_rules", "previous_usage_rules"}:
        assert all(row["status"] == "inconclusive" for row in rows.values())
        assert all("credential evidence is missing" in row["reason"] for row in rows.values())
    if mode == "changed_source":
        assert all(row["status"] == "inconclusive" and "source identity" in row["reason"] for row in rows.values())
    if mode == "unexpected_access":
        failure = next(check for check in compared.checks if check.id == "baseline")
        assert failure.evidence_ids == rows["invalid"]["evidence_ids"]


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
        lambda req: httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})), **kw))
    state = _Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(state, cfg.targets[0], client)
    assert result(state, cfg.targets[0]).status == "inconclusive"


@pytest.mark.parametrize("configured", [True, False])
async def test_policy_denials_and_missing_configuration_do_not_fail_shared_key_access(monkeypatch, local_tokenizer, configured):
    from giraffe.runner import run_suite
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    cfg = RunConfig.model_validate({**config().model_dump(), "checks": ["access", "gateway_access"],
        "stop_after_errors": 1, "gateway_access": config().gateway_access.model_dump() if configured else None})
    original = httpx.AsyncClient
    def handler(req):
        if req.method != "POST":
            return httpx.Response(404)
        if req.headers.get("authorization") == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    assert report.overall == "pass" and report.abort_reason is None
    assert next(c for c in report.checks if c.id == "access").status == "pass"
    assert next(c for c in report.checks if c.id == "gateway_access").status == ("pass" if configured else "inconclusive")
    assert all("access" not in r.check_ids for r in report.requests if r.identity)


async def test_expected_allow_but_recognized_denial_fails(monkeypatch):
    from giraffe.gateway_access import execute, result, observation
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    cfg = config()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(
        lambda req: httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})), **kw))
    state = _Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(state, cfg.targets[0], client)
    assert result(state, cfg.targets[0]).status == "fail"
    assert observation(state.records[0].model_copy(update={"status": "timeout"}))[0] == "unverified"


def test_limits_and_example_are_valid():
    from pathlib import Path
    import yaml
    from pydantic import ValidationError
    from giraffe.models import GatewayAccess
    for changes in ({"request_timeout_seconds": 61}, {"max_output_tokens": 129},
                    {"cases": [{"name": "same", "identity": "allowed", "expected": "allow"}] * 17}):
        with pytest.raises(ValidationError):
            GatewayAccess.model_validate(changes)
    values = yaml.safe_load((Path(__file__).parents[1] / "examples/gateway-access.yaml").read_text())
    assert RunConfig.model_validate(values).checks == ["gateway_access"]
