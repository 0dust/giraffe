import asyncio
import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.gateway_access import execute, result
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import reproduction_export, write_report
from giraffe.runner import _Run, run_suite
from tests.test_gateway_access import config


async def test_cancelled_probe_retains_case_evidence_in_saved_reports(monkeypatch, tmp_path):
    cfg = config(retention="all")
    cfg.targets[0].test_identities = {name: identity.model_copy(update={"api_key_env": None})
                                    for name, identity in cfg.targets[0].test_identities.items()}
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    run = _Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as client:
        task = asyncio.create_task(execute(run, cfg.targets[0], client))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(run.records) == 1
    record = run.records[0]
    assert record.status == "cancelled"
    check = result(run, cfg.targets[0])
    assert check.status == "inconclusive"
    attempted, untouched = check.metrics["gateway_access"]["cases"]
    assert attempted["request_id"] == record.id
    assert attempted["observed"] == "unverified"
    assert attempted["reason"] == "request_cancelled"
    assert untouched["request_id"] is None and untouched["reason"] == "not_run"
    assert check.evidence_ids == [record.id]
    report = RunReport(run_id="cancelled-policy", started_at=record.started_at,
        finished_at=record.started_at, overall="inconclusive",
        manifest={"config": cfg.model_dump(mode="json")}, requests=run.records, checks=[check])
    saved, html = write_report(report, tmp_path, html=True)
    retained = json.loads(saved.read_text())
    assert retained["checks"][0]["evidence_ids"] == [record.id]
    assert "request_cancelled" in html.read_text()
    exported = reproduction_export(report)["gateway_access"]["proxy"]["cases"]
    assert exported[0]["request_id"] == record.id
    assert exported[1]["request_id"] is None


@pytest.mark.parametrize("configured", [True, False])
async def test_missing_credentials_keep_explicit_policy_required(monkeypatch, local_tokenizer, configured):
    monkeypatch.delenv("ALLOW_KEY", raising=False)
    monkeypatch.delenv("DENY_KEY", raising=False)
    value = config().model_dump()
    value.update(checks=["access", "gateway_access"], samples=1,
                 gateway_access=value["gateway_access"] if configured else None)
    cfg = RunConfig.model_validate(value)
    calls = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    assert next(check for check in report.checks if check.id == "access").status == "pass"
    policy = next(check for check in report.checks if check.id == "gateway_access")
    assert policy.status == "inconclusive"
    assert policy.required is configured
    assert report.overall == ("inconclusive" if configured else "pass")
    assert len(calls) == 1
    assert all(record.identity is None for record in report.requests)
    assert all(row["reason"] == "identity_unavailable" for row in policy.metrics["gateway_access"]["cases"])


@pytest.mark.parametrize("credential", ["api_key_env", "headers_env"])
async def test_malformed_credentials_leave_budget_for_valid_case(monkeypatch, credential):
    value = config(max_requests=1).model_dump()
    identity = ({"api_key_env": "MALFORMED_KEY"} if credential == "api_key_env" else
                {"headers_env": {"X-Test-Key": "MALFORMED_KEY"}})
    value["targets"][0]["test_identities"] = {"invalid": identity, "anonymous": {}}
    value["gateway_access"]["cases"] = [
        {"name": "invalid", "identity": "invalid", "expected": "deny"},
        {"name": "valid", "identity": "anonymous", "expected": "allow"}]
    cfg = RunConfig.model_validate(value)
    monkeypatch.setenv("MALFORMED_KEY", "secret\nvalue")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    run = _Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(run, cfg.targets[0], client)
    check = result(run, cfg.targets[0])
    invalid, valid = check.metrics["gateway_access"]["cases"]
    assert len(calls) == run.attempted == len(run.records) == 1
    assert run.records[0].identity == "anonymous"
    assert invalid["reason"] == "identity_unavailable" and invalid["request_id"] is None
    assert valid["observed"] == "allow" and valid["request_id"] == run.records[0].id
    assert check.status == "inconclusive" and check.required
    assert "secret" not in check.model_dump_json()


def test_finite_access_matrix_has_exact_request_plan():
    from giraffe.planning import request_plan

    plan = request_plan(config(max_requests=2))
    row = next(row for row in plan["rows"] if row["id"] == "gateway_access")
    assert row["kind"] == "exact" and row["requests_per_target"] == 2
    assert not any("adaptive traffic" in warning for warning in plan["warnings"])
