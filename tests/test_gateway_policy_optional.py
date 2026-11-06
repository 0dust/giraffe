"""Selecting optional policies without configuring them must not invent traffic."""
import json

import httpx
import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.runner import run_suite


def configuration():
    return RunConfig(targets=[{"name": "proxy", "url": "http://127.0.0.1:1", "model": "test"}],
                     checks=["gateway_policy"], max_memory_mb=0)


async def test_unconfigured_policy_is_unavailable_without_traffic(tmp_path, capsys):
    from giraffe.cli import _print_result
    from giraffe.reporting import write_report, reproduction_export, compare_baseline

    config = configuration()
    report = await run_suite(config)
    check = next(check for check in report.checks if check.id == "gateway_policy")
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unavailable"
    assert not report.requests
    row = next(row for row in request_plan(config)["rows"] if row["id"] == "gateway_policy")
    assert row["requests_per_target"] == 0 and "unavailable" in json.dumps(row)
    json_path, html_path = write_report(report, tmp_path, html=True)
    assert "unavailable" in html_path.read_text()
    assert reproduction_export(report)["gateway_policy"]["proxy"]["qualification"] == "unavailable"
    _print_result(report, json_path, html_path, tmp_path, {})
    assert "unavailable" in capsys.readouterr().out
    assert compare_baseline(report, report.model_copy(deep=True)).baseline["status"] == "inconclusive"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("capability", [None, "key_rpm", "priority_rpm"])
async def test_unavailable_policy_with_no_identities_keeps_shared_key_access(monkeypatch, capability):
    from tests.test_gateway_policy import configuration as configured_policy

    raw = configured_policy(gateway="unsupported").model_dump(mode="json")
    if capability is None:
        raw["gateway_policy"] = None
    else:
        raw["gateway_policy"]["capability"] = capability
        if capability == "priority_rpm":
            for user in raw["gateway_policy"]["users"]:
                user["priority"] = None
    raw["checks"] = ["access", "gateway_policy"]
    raw["stream"] = False
    raw["targets"][0].update(test_identities={}, api_key_env="SHARED")
    monkeypatch.setenv("SHARED", "shared-secret")
    config = RunConfig.model_validate(raw)

    sent = []

    def handle(request):
        if request.method != "POST":
            return httpx.Response(404)
        sent.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    report = await run_suite(config)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == "pass", checks["access"].summary
    assert checks["gateway_policy"].metrics["gateway_policy"]["qualification"] == "unavailable"
    assert not checks["gateway_policy"].required
    assert report.overall == "pass"
    assert sent == ["Bearer shared-secret"]
    assert all("gateway_policy" not in record.check_ids for record in report.requests)


@pytest.mark.usefixtures("local_tokenizer")
async def test_unavailable_policy_references_do_not_block_serving_baseline(monkeypatch):
    from giraffe.reporting import compare_baseline
    from tests.test_gateway_policy import configuration as configured_policy

    def handle(request):
        if request.method != "POST":
            return httpx.Response(404)
        assert request.headers["authorization"] == "Bearer shared-secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setenv("SHARED", "shared-secret")

    async def run(ref):
        raw = configured_policy(gateway="unsupported").model_dump(mode="json")
        raw.update(checks=["serving", "gateway_policy"], stream=False, samples=8)
        raw["targets"][0].update(api_key_env="SHARED", test_identities={"limited": {"api_key_env": ref}})
        report = await run_suite(RunConfig.model_validate(raw))
        assert report.overall == "pass"
        return report

    baseline = await run("PREVIOUS_UNUSED")
    current = await run("CURRENT_UNUSED")
    compared = compare_baseline(current, baseline).baseline["targets"][0]
    assert compared["status"] == "pass", compared["summary"]
    assert any(row["check"] == "serving" and row["status"] == "pass" for row in compared["comparisons"])


@pytest.mark.parametrize("credential", [None, " "])
async def test_unavailable_policy_credentials_do_not_consume_requests(monkeypatch, credential):
    from tests.test_gateway_policy import configuration as configured_policy

    config = configured_policy().model_copy(update={"max_requests": 3})
    monkeypatch.setenv("LIMITED", "limited-secret")
    if credential is None:
        monkeypatch.delenv("PEER", raising=False)
    else:
        monkeypatch.setenv("PEER", credential)
    sent = []

    def handle(request):
        if request.method == "POST":
            sent.append(request)
        return httpx.Response(404)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    report = await run_suite(config)
    check = next(check for check in report.checks if check.id == "gateway_policy")
    policy = check.metrics["gateway_policy"]
    assert report.overall == check.status == "inconclusive"
    assert policy["qualification"] == "unverified"
    assert "credentials are unavailable" in check.summary
    assert [user["dispatched"] for user in policy["users"]] == [0, 0]
    assert not sent
    assert not report.requests
    assert report.observations["request_budget_used"] == 0
    assert report.abort_reason is None


def test_configured_policy_still_requires_all_identity_references():
    from tests.test_gateway_policy import configuration as configured_policy

    raw = configured_policy().model_dump(mode="json")
    raw["targets"][0]["test_identities"].pop("peer")
    with pytest.raises(ValueError, match="Every policy identity"):
        RunConfig.model_validate(raw)


def test_selecting_optional_policy_does_not_save_example_identity_defaults():
    from tests.test_ux_catalogue import browser_logic

    browser_logic('''
const values=new FormData();values.set('gateway_policy_rendered','1');
const result=configureGatewayPolicy(config,values);
assert.equal(result.gateway_policy,undefined);
const check=bootstrap.checks.find(c=>c.id==='gateway_policy');
assert.match(effectiveCheckSummary(check,config),/Not configured/);
''', configuration())
