"""Gateway policy conclusions require attributed evidence and bounded peer work."""
import asyncio
import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import RunConfig, Target
from giraffe import runner


def configuration(mode="key_rpm", **changes):
    policy = dict(capability=mode, affected_identity="limited", duration_seconds=2,
                  interval_seconds=.005, window_state="isolated_fresh", gateway_version="1.109.0",
                  users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
                         dict(identity="peer", requests=8, rpm=8, priority="high")])
    if mode == "priority_rpm":
        policy.update(model_rpm=10, saturation_threshold=0, saturation_cache_seconds=0)
    policy.update(changes)
    return RunConfig(targets=[Target(name="proxy", url="http://proxy", model="test",
        test_identities={"limited": {"api_key_env": "LIMITED"}, "peer": {"api_key_env": "PEER"}})],
        checks=["gateway_policy"], gateway_policy=policy, concurrency=2,
        max_duration_seconds=5, request_timeout_seconds=1)


async def exercise(monkeypatch, mode="key_rpm", *, behavior="enforce", **settings):
    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")
    seen = []
    counts = {"limited": 0, "peer": 0}

    async def handle(request):
        who = "limited" if request.headers["authorization"] == "Bearer limited-secret" else "peer"
        counts[who] += 1
        seen.append(who)
        await asyncio.sleep(.01)
        priority = "low" if who == "limited" else "high"
        headers = {"x-ratelimit-limit-requests": "2" if who == "limited" else "8",
                   "x-litellm-priority": priority}
        threshold = 3 if behavior == "overadmit" else 1 if behavior == "early" else 2
        if who == "limited" and counts[who] > threshold and behavior != "ignore":
            message = ("Priority-based rate limit exceeded. Model: test, Priority: low, Rate limit type: requests, Model TPM: not configured, Model RPM: 10, Remaining: 0, Model saturation: 50%"
                       if mode == "priority_rpm" else
                       "Rate limit exceeded for api_key: private-hash. Limit type: requests. Current limit: 2, Remaining: 0. Limit resets at: 2026-01-01")
            if behavior == "unknown":
                message = "OpenAIException: provider rate limit"
            return httpx.Response(429, headers=headers, json={"error": {
                "type": "throttling_error", "code": "429", "message": message}})
        usage = {"prompt_tokens": 8, "completion_tokens": 2}
        if behavior == "missing_usage":
            usage = {}
        return httpx.Response(200, headers=headers, json={"choices": [{"message": {
            "content": "OK"}, "finish_reason": "stop"}], "usage": usage})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    from giraffe.gateway_policy import execute, result
    cfg = configuration(mode, **settings)
    state = runner._Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
        await execute(state, cfg.targets[0], client)
    return result(state, cfg.targets[0]), state, seen


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
async def test_policy_enforcement_preserves_rejections_and_peer_continuation(monkeypatch, mode):
    check, state, seen = await exercise(monkeypatch, mode)
    summary = check.metrics["gateway_policy"]
    assert check.status == "pass", summary
    assert summary["qualification"] == "observed"
    assert summary["users"][0]["rejected"] == 3
    assert summary["users"][1]["completed"] == 8
    assert summary["peer_continuation"]
    assert len(seen) == 13 == len(state.records)
    assert state.slots.qsize() == 2
    assert "limited-secret" not in check.model_dump_json()
    assert "private-hash" not in check.model_dump_json()


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
async def test_unknown_429_is_not_policy_enforcement_or_failure(monkeypatch, mode):
    check, _, _ = await exercise(monkeypatch, mode, behavior="unknown")
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"


async def test_unknown_window_cannot_certify_priority(monkeypatch):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", window_state="unknown")
    assert check.status == "inconclusive"


async def test_priority_missing_actual_work_is_unverified(monkeypatch):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", behavior="missing_usage")
    assert check.status == "inconclusive"


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
@pytest.mark.parametrize("behavior", ["overadmit", "early"])
async def test_attributed_policy_violation_is_not_missing_evidence(monkeypatch, mode, behavior):
    check, _, _ = await exercise(monkeypatch, mode, behavior=behavior)
    assert check.status == "fail"
    assert check.metrics["gateway_policy"]["qualification"] == "observed_failure"


async def test_no_rejection_is_unexercised_not_pass(monkeypatch):
    check, _, _ = await exercise(monkeypatch, behavior="ignore")
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unexercised"


async def test_duration_cancels_work_and_releases_every_slot(monkeypatch):
    check, state, seen = await exercise(monkeypatch, duration_seconds=.025)
    assert check.status == "inconclusive"
    assert len(seen) < 13
    assert len(seen) == len(state.records)
    assert state.slots.qsize() == 2


async def test_request_budget_bounds_all_identities(monkeypatch):
    from giraffe.gateway_policy import execute, result
    from tests.test_runner import FakeClient
    cfg = configuration().model_copy(update={"max_requests": 2})
    state = runner._Run(cfg, None, None)
    await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
    assert len(state.records) == 2
    assert result(state, cfg.targets[0]).status == "inconclusive"
    assert state.slots.qsize() == 2


async def test_unsupported_gateway_dispatches_nothing(monkeypatch):
    check, _, seen = await exercise(monkeypatch, gateway="unsupported")
    assert seen == []
    assert check.metrics["gateway_policy"]["qualification"] == "unavailable"


def test_policy_reports_exports_ui_and_baseline(tmp_path, capsys, monkeypatch):
    from giraffe.cli import _print_result
    from giraffe.reporting import write_report, reproduction_export, compare_baseline
    from tests.test_reporting import make_report
    from tests.test_ux_catalogue import browser_logic
    check, state, _ = asyncio.run(exercise(monkeypatch, "priority_rpm"))
    report = make_report()
    report.manifest["config"] = state.config.model_dump(mode="json")
    report.checks, report.requests = [check], state.records
    report.observations = {"targets": state.observations}
    json_path, html_path = write_report(report, tmp_path, html=True)
    assert "Gateway policy · observed" in html_path.read_text()
    assert json.loads(json_path.read_text())["checks"][0]["metrics"]["gateway_policy"]["users"][0]["rejected"] == 3
    exported = reproduction_export(report)
    assert exported["gateway_policy"]["proxy"]["settings"]["capability"] == "priority_rpm"
    assert "limited-secret" not in json.dumps(exported)
    _print_result(report, json_path, html_path, tmp_path, {})
    assert "limited: 2 admitted, 3 rejected" in capsys.readouterr().out
    matched = compare_baseline(report, report.model_copy(deep=True))
    assert matched.baseline["status"] == "pass", matched.baseline
    rows = matched.baseline["targets"][0]["comparisons"]
    assert len([row for row in rows if row["metric"] == "policy_acceptance"]) == 2
    different = report.model_copy(deep=True)
    different.manifest["config"]["gateway_policy"]["duration_seconds"] = 1
    assert compare_baseline(report, different).baseline["status"] == "inconclusive"
    browser_logic('const policy='+json.dumps(check.metrics["gateway_policy"])+r''';
const check=bootstrap.checks.find(c=>c.id==='gateway_policy');
const markup=renderTestCard(check,config);
assert.match(markup,/gateway_policy_users/);
const values=new FormData();values.set('gateway_policy_rendered','1');
values.set('gateway_policy_users','limited, 6, 2, low\npeer, 8, 8, high');
const changed=configureGatewayPolicy(config,values);
assert.equal(changed.gateway_policy.users[0].requests,6);
assert.equal(changed.gateway_policy.model_rpm,10);
const result=renderGatewayPolicyResults(policy);
assert.match(result,/limited/);assert.match(result,/observed/);
assert.match(result,/Actual input tokens/);assert.doesNotMatch(result,/limited-secret/);
''', state.config)


def test_parser_never_persists_raw_rate_limit_identity():
    from giraffe.gateway_response import response_evidence
    body = json.dumps({"error": {"type": "throttling_error", "code": "429", "message":
        "Rate limit exceeded for api_key: secret-hash. Limit type: requests. Current limit: 2, Remaining: 0. Limit resets at: now"}}).encode()
    evidence = response_evidence(429, body)
    assert evidence.policy_rejection == "key_rpm"
    assert evidence.request_limit == 2
    assert "secret-hash" not in evidence.model_dump_json()
