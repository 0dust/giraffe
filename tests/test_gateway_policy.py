"""Gateway policy conclusions require attributed evidence and bounded peer work."""
import asyncio
import hashlib
import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import RunConfig, Target
from giraffe import runner


LIMITED_KEY_HASH = hashlib.sha256(b"limited-secret").hexdigest()
PEER_KEY_HASH = hashlib.sha256(b"peer-secret").hexdigest()
MODEL_POOL = "routed-test-pool"
CACHE_KEY = "opaque-cache-value-must-not-be-retained"


def configuration(mode="key_rpm", **changes):
    policy = dict(capability=mode, affected_identity="limited", duration_seconds=2,
                  interval_seconds=.005, window_state="isolated_fresh_single_counter", gateway_version="1.109.0",
                  users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
                         dict(identity="peer", requests=8, rpm=8, priority="high")])
    if mode == "priority_rpm":
        policy.update(model_rpm=10, saturation_threshold=0, saturation_cache_seconds=0)
    policy.update(changes)
    return RunConfig(targets=[Target(name="proxy", url="http://proxy", model="test",
        test_identities={"limited": {"api_key_env": "LIMITED"}, "peer": {"api_key_env": "PEER"}})],
        checks=["gateway_policy"], gateway_policy=policy, concurrency=2,
        max_duration_seconds=5, request_timeout_seconds=1)


async def exercise(monkeypatch, mode="key_rpm", *, behavior="enforce", full=False,
                   admitted_affected=None, rejection_saturation=50.0, peer_delay=.01, **settings):
    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")
    if behavior == "duplicate_credentials":
        monkeypatch.setenv("PEER", "limited-secret")
    seen = []
    counts = {"limited": 0, "peer": 0}

    async def handle(request):
        if request.method != "POST":
            return httpx.Response(404)
        who = {"Bearer limited-secret": "limited", "Bearer peer-secret": "peer"}.get(request.headers.get("authorization"))
        if who is None:
            return httpx.Response(401, json={"error": {"type": "authentication_error"}})
        assert json.loads(request.content)["cache"] == {"no-cache": True, "no-store": True}
        counts[who] += 1
        seen.append(who)
        await asyncio.sleep(.01 if who == "limited" else peer_delay)
        user = next(user for user in cfg.gateway_policy.users if user.identity == who)
        priority = user.priority or "default"
        headers = {"x-ratelimit-limit-requests": "2" if who == "limited" else "8",
                   "x-litellm-priority": priority, "retry-after": "1" if behavior == "short_bucket_reset" else "60",
                   "x-litellm-rate-limiter-version": "v3",
                   "x-ratelimit-priority_model-limit-requests": str(user.rpm)}
        threshold = admitted_affected if admitted_affected is not None else {"overadmit": 3, "early": 1}.get(behavior, 2)
        bucket_reset = behavior == "short_bucket_reset" and counts[who] == 5
        if who == "limited" and counts[who] > threshold and behavior != "ignore" and not bucket_reset:
            subject = PEER_KEY_HASH if behavior == "wrong_subject" else LIMITED_KEY_HASH
            message = (f"Priority-based rate limit exceeded. Model: {MODEL_POOL}, Priority: {priority}, Rate limit type: requests, "
                       f"Model TPM: not configured, Model RPM: {cfg.gateway_policy.model_rpm}, Remaining: 0, "
                       f"Model saturation: {rejection_saturation}%"
                       if mode == "priority_rpm" else
                       f"Rate limit exceeded for api_key: {subject}. Limit type: requests. Current limit: 2, Remaining: 0. Limit resets at: 2026-01-01")
            if behavior == "unknown" or (behavior == "mixed_rejections" and counts[who] > threshold + 1):
                message = "OpenAIException: provider rate limit"
            if behavior == "contradictory_pool":
                headers["x-litellm-model-group"] = "other-pool"
            return httpx.Response(429, headers=headers, json={"error": {
                "type": "throttling_error", "code": "429", "message": message}})
        usage = {"prompt_tokens": 8, "completion_tokens": 2}
        if behavior == "missing_usage":
            usage = {}
        if behavior == "reasoning_work" and who == "limited":
            usage = {"prompt_tokens": 8, "completion_tokens": 128,
                     "completion_tokens_details": {"reasoning_tokens": 126}}
        group = "other-pool" if behavior == "different_pool" and who == "peer" else MODEL_POOL
        if behavior != "missing_pool":
            headers["x-litellm-model-group"] = group
        if behavior == "fallback_pool" and who == "peer":
            headers["x-litellm-attempted-fallbacks"] = "1"
        if behavior == "cached_response":
            headers["x-litellm-cache-key"] = CACHE_KEY
        role = "user" if behavior == "malformed_peer" and who == "peer" else "assistant"
        return httpx.Response(200, headers=headers, json={"model": "test", "choices": [{"message": {
            "role": role, "content": "OK"}, "finish_reason": "stop"}], "usage": usage})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    from giraffe.gateway_policy import execute, result
    cfg = configuration(mode, **settings)
    if behavior == "cached_response":
        cfg.request_options = {"cache": {"no-cache": False, "no-store": False, "use-cache": True}}
    if full:
        cfg.targets[0].api_key_env = "UNUSED_SHARED_CREDENTIAL"
        monkeypatch.delenv("UNUSED_SHARED_CREDENTIAL", raising=False)
        report = await runner.run_suite(cfg)
        return next(check for check in report.checks if check.id == "gateway_policy"), report, seen
    state = runner._Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
        await execute(state, cfg.targets[0], client)
    return result(state, cfg.targets[0]), state, seen


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
@pytest.mark.parametrize("behavior,expected", [("enforce", "pass"), ("overadmit", "fail"), ("unknown", "inconclusive")])
async def test_full_runner_keeps_policy_verdict_and_does_not_send_shared_setup(monkeypatch, mode, behavior, expected):
    check, report, seen = await exercise(monkeypatch, mode, behavior=behavior, full=True)
    assert check.status == expected, check.summary
    assert len(seen) == 13
    assert all(record.identity in {"limited", "peer"} for record in report.requests)
    assert len(report.requests) == 13
    assert "limited-secret" not in report.model_dump_json()
    assert LIMITED_KEY_HASH not in report.model_dump_json()
    assert MODEL_POOL not in report.model_dump_json()


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
    assert LIMITED_KEY_HASH not in check.model_dump_json()


async def test_wrong_key_subject_cannot_verify_quota(monkeypatch):
    check, state, _ = await exercise(monkeypatch, behavior="wrong_subject")
    summary = check.metrics["gateway_policy"]
    assert check.status == "inconclusive"
    assert summary["qualification"] == "unverified"
    assert summary["users"][0]["rejected"] == 3
    assert not summary["peer_continuation"]
    assert PEER_KEY_HASH not in check.model_dump_json()
    assert all(PEER_KEY_HASH not in record.model_dump_json() for record in state.records)


async def test_one_second_bucket_can_reset_during_subsecond_observation(monkeypatch):
    check, state, _ = await exercise(monkeypatch, behavior="short_bucket_reset")
    summary = check.metrics["gateway_policy"]
    assert [record.http_status for record in state.records if record.identity == "limited"] == [200, 200, 429, 429, 200]
    assert summary["peer_continuation"]
    assert check.status == "inconclusive"
    assert summary["qualification"] == "unverified"


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
async def test_unknown_429_is_not_policy_enforcement_or_failure(monkeypatch, mode):
    check, _, _ = await exercise(monkeypatch, mode, behavior="unknown")
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"


@pytest.mark.parametrize("mode,attributed", [("key_rpm", "key_rpm"), ("priority_rpm", "priority_pool")])
async def test_mixed_affected_rejections_leave_admissions_unverified(monkeypatch, mode, attributed):
    check, _, _ = await exercise(monkeypatch, mode, behavior="mixed_rejections")
    summary = check.metrics["gateway_policy"]
    affected = summary["users"][0]
    assert affected["admitted"] == 2 and affected["rejected"] == 3
    assert [row["policy"]["policy_rejection"] for row in affected["evidence"] if row["http_status"] == 429] == [
        attributed, "unknown", "unknown"]
    assert summary["peer_continuation"]
    assert check.status == "inconclusive"
    assert summary["qualification"] == "unverified"


async def test_unknown_window_cannot_certify_priority(monkeypatch):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", window_state="unknown")
    assert check.status == "inconclusive"


async def test_two_aliases_with_one_credential_are_unverified(monkeypatch):
    check, state, seen = await exercise(monkeypatch, behavior="duplicate_credentials")
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert not check.metrics["gateway_policy"]["identities_distinct"]
    assert not state.records and not seen


async def test_priority_cached_saturation_is_unverified(monkeypatch):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", saturation_cache_seconds=5)
    assert check.status == "inconclusive"


async def test_priority_without_actual_overlap_is_unverified(monkeypatch):
    from giraffe.gateway_policy import summarize
    check, state, _ = await exercise(monkeypatch, "priority_rpm")
    for index, record in enumerate(state.records):
        record.dispatch_ms, record.completed_ms = index*100, index*100+10
    summary = summarize(state.config.gateway_policy, state.observations["proxy"]["gateway_policy"], state.records)
    assert summary["status"] == "inconclusive"
    assert not summary["overlap_observed"]


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

    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")
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


@pytest.mark.usefixtures("local_tokenizer")
def test_policy_reports_exports_ui_and_baseline(tmp_path, capsys, monkeypatch):
    from giraffe.cli import _print_result
    from giraffe.reporting import write_report, reproduction_export, compare_baseline
    from tests.test_ux_catalogue import browser_logic
    check, report, _ = asyncio.run(exercise(monkeypatch, "priority_rpm", full=True))
    json_path, html_path = write_report(report, tmp_path, html=True)
    assert "Gateway policy · observed" in html_path.read_text()
    saved_policy = next(item for item in json.loads(json_path.read_text())["checks"]
                        if item["id"] == "gateway_policy")
    assert saved_policy["metrics"]["gateway_policy"]["users"][0]["rejected"] == 3
    assert saved_policy["metrics"]["gateway_policy"]["settings"]["window_state"] == "isolated_fresh_single_counter"
    exported = reproduction_export(report)
    assert exported["gateway_policy"]["proxy"]["settings"]["capability"] == "priority_rpm"
    assert exported["gateway_policy"]["proxy"]["settings"]["window_state"] == "isolated_fresh_single_counter"
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
    different = report.model_copy(deep=True)
    different.manifest["config"]["targets"][0]["test_identities"]["peer"]["api_key_env"] = "OTHER_PEER"
    assert compare_baseline(report, different).baseline["status"] == "inconclusive"
    browser_logic('const policy='+json.dumps(check.metrics["gateway_policy"])+r''';
const check=bootstrap.checks.find(c=>c.id==='gateway_policy');
const markup=renderTestCard(check,config);
assert.match(markup,/gateway_policy_users/);
assert.match(markup,/value="isolated_fresh_single_counter" selected/);
const values=new FormData();values.set('gateway_policy_rendered','1');
values.set('gateway_policy_users','limited, 6, 2, low\npeer, 8, 8, high');
values.set('gateway_policy_window_state','isolated_fresh_single_counter');
const changed=configureGatewayPolicy(config,values);
assert.equal(changed.gateway_policy.users[0].requests,6);
assert.equal(changed.gateway_policy.model_rpm,10);
assert.equal(changed.gateway_policy.window_state,'isolated_fresh_single_counter');
const result=renderGatewayPolicyResults(policy);
assert.match(result,/limited/);assert.match(result,/observed/);
assert.match(result,/Actual input tokens/);assert.doesNotMatch(result,/limited-secret/);
const comparison=comparisonTable([{metric:'policy_acceptance',aggregation:'observed',status:'pass',current:'observed',baseline:'observed',current_samples:5,baseline_samples:5}]);
assert.match(comparison,/Policy evidence/);assert.doesNotMatch(comparison,/Median|No material regression/);
''', RunConfig.model_validate(report.manifest["config"]))


def test_parser_never_persists_raw_rate_limit_identity():
    from giraffe.gateway_response import response_evidence
    body = json.dumps({"error": {"type": "throttling_error", "code": "429", "message":
        f"Rate limit exceeded for api_key: {LIMITED_KEY_HASH}. Limit type: requests. Current limit: 2, Remaining: 0. Limit resets at: now"}}).encode()
    assert response_evidence(429, body).policy_rejection == "unknown"
    evidence = response_evidence(429, body, expected_key_hash=LIMITED_KEY_HASH)
    assert evidence.policy_rejection == "key_rpm"
    assert evidence.request_limit == 2
    assert LIMITED_KEY_HASH not in evidence.model_dump_json()


@pytest.mark.parametrize("body", [b'[]', b'{"error":[]}', b'{"error":{"type":[],"code":[]}}', b'['*1200+b']'*1200])
def test_malformed_policy_evidence_is_unknown(body):
    from giraffe.gateway_response import response_evidence
    assert response_evidence(429, body).policy_rejection == "unknown"
