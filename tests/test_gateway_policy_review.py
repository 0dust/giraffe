"""Policy verdicts distinguish response success from gateway admission evidence."""
import asyncio

import httpx
import pytest

from giraffe.gateway_policy import summarize
from giraffe.gateway_response import response_evidence
from giraffe.models import RunConfig
from giraffe.runner import run_suite
from tests.test_gateway_policy import CACHE_KEY, MODEL_POOL, configuration, exercise


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
@pytest.mark.parametrize("status,http_status", [("failed", 500), ("timeout", None), ("cancelled", None), ("failed", 429)])
async def test_unknown_prior_admission_cannot_prove_early_exhaustion(monkeypatch, mode, status, http_status):
    _, run, _ = await exercise(monkeypatch, mode)
    first = next(record for record in run.records if record.identity == "limited")
    first.status, first.http_status, first.valid = status, http_status, False
    first.gateway_evidence = None
    result = summarize(run.config.gateway_policy, run.observations["proxy"]["gateway_policy"], run.records)
    assert result["qualification"] == "unverified", result
    assert result["status"] == "inconclusive"


async def test_strict_priority_rejection_precludes_later_borrowing(monkeypatch):
    _, run, _ = await exercise(monkeypatch, "priority_rpm", saturation_threshold=.29,
        users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
               dict(identity="peer", requests=5, rpm=8, priority="high")])
    last = [record for record in run.records if record.identity == "limited"][-1]
    last.status, last.http_status, last.valid = "completed", 200, True
    last.input_tokens, last.output_tokens, last.completion_tokens = 8, 2, 2
    last.gateway_evidence = response_evidence(200, headers={"x-litellm-priority": "low",
        "x-litellm-rate-limiter-version": "v3", "x-ratelimit-priority_model-limit-requests": "2",
        "x-litellm-model-group": MODEL_POOL}, declared_priority="low")
    result = summarize(run.config.gateway_policy, run.observations["proxy"]["gateway_policy"], run.records)
    assert result["qualification"] == "observed_failure", result
    assert result["status"] == "fail"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("peer_requests,expected", [(7, "pass"), (8, "fail")])
async def test_priority_borrowing_cannot_exceed_aggregate_model_rpm(monkeypatch, peer_requests, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", behavior="overadmit", saturation_threshold=.39, full=True,
        peer_delay=.075, early_peer_admissions=1,
        users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
               dict(identity="peer", requests=peer_requests, rpm=8, priority="high")])
    policy = check.metrics["gateway_policy"]
    assert [user["admitted"] for user in policy["users"]] == [3, peer_requests]
    assert check.status == expected
    if expected == "fail":
        assert "exceeded the declared model RPM" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
@pytest.mark.parametrize("behavior", ["enforce", "overadmit"])
async def test_policy_requires_operator_confirmed_counter_scope(monkeypatch, mode, behavior):
    check, _, seen = await exercise(monkeypatch, mode, behavior=behavior, saturation_threshold=.5,
        window_state="isolated_fresh", full=True)
    assert len(seen) == 13
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert "operator-confirmed authoritative limiter counter" in check.summary


async def test_genuine_early_rejection_is_not_hidden_by_a_later_failure(monkeypatch):
    _, run, _ = await exercise(monkeypatch, behavior="early")
    last = [record for record in run.records if record.identity == "limited"][-1]
    last.status, last.http_status, last.valid = "failed", 500, False
    last.gateway_evidence = None
    result = summarize(run.config.gateway_policy, run.observations["proxy"]["gateway_policy"], run.records)
    assert result["qualification"] == "observed_failure"
    assert result["status"] == "fail"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("behavior", ["malformed_peer", "peer_tool_call", "peer_function_call", "peer_invalid_refusal"])
async def test_malformed_assistant_reply_does_not_show_peer_continuation(monkeypatch, behavior):
    check, report, _ = await exercise(monkeypatch, behavior=behavior, full=True)
    peer = [record for record in report.requests if record.identity == "peer"]
    assert len(peer) == 8 and all(record.http_status == 200 and not record.valid for record in peer)
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert not check.metrics["gateway_policy"]["peer_continuation"]


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("behavior", ["different_pool", "missing_pool", "fallback_pool", "contradictory_pool"])
async def test_priority_requires_one_attested_model_pool(monkeypatch, behavior):
    check, report, seen = await exercise(monkeypatch, "priority_rpm", behavior=behavior, full=True)
    assert len(seen) == 13
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert "common resolved model pool" in check.summary
    assert MODEL_POOL not in report.model_dump_json()
    assert "other-pool" not in report.model_dump_json()


@pytest.mark.usefixtures("local_tokenizer")
async def test_malformed_secondary_header_is_rejected_before_admission(monkeypatch):
    raw = configuration().model_dump(mode="json")
    raw["targets"][0]["test_identities"]["limited"]["headers_env"]["x-front-gateway"] = "FRONT"
    config = RunConfig.model_validate({**raw, "max_requests": 1})
    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")
    monkeypatch.setenv("FRONT", "malformed-secondary-secret ")

    def handler(request):
        assert request.method != "POST", "Malformed identity must fail before probe dispatch"
        return httpx.Response(404)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config)
    check = next(check for check in report.checks if check.id == "gateway_policy")
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert all(user["dispatched"] == 0 for user in check.metrics["gateway_policy"]["users"])
    assert not report.requests
    assert "malformed-secondary-secret" not in report.model_dump_json()


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
async def test_cached_completions_leave_policy_unverified(monkeypatch, mode):
    check, report, sent = await exercise(monkeypatch, mode, behavior="cached_response", full=True)
    assert len(sent) == 13
    assert sum(record.valid for record in report.requests) == 10
    policy = check.metrics["gateway_policy"]
    assert check.status == "inconclusive" and policy["qualification"] == "unverified"
    assert policy["response_cache_hit_reported"] is True
    assert not policy["comparable_actual_work"] and not policy["peer_continuation"]
    assert "reported a cached completion" in check.summary
    assert CACHE_KEY not in report.model_dump_json()


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("mode,expected", [("key_rpm", "pass"), ("priority_rpm", "inconclusive")])
async def test_default_pool_label_cannot_establish_distinct_priority_reservations(monkeypatch, mode, expected):
    check, _, _ = await exercise(monkeypatch, mode, full=True,
        users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
               dict(identity="peer", requests=5, rpm=8, priority="default_pool")])
    assert [user["admitted"] for user in check.metrics["gateway_policy"]["users"]] == [2, 5]
    assert check.status == expected
    if mode == "priority_rpm":
        assert check.metrics["gateway_policy"]["qualification"] == "unverified"
        assert "reserves default_pool" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("early_peers,reported,expected,reason", [
    (9, 55.0, "pass", "continued peer service"),
    (9, 54.9, "inconclusive", "Strict-mode evidence"),
    (10, 57.0, "inconclusive", "Reported saturation contradicts completed admissions"),
])
async def test_priority_compares_reported_saturation_to_completed_admissions(monkeypatch, early_peers, reported, expected, reason):
    check, report, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=20,
        saturation_threshold=.55, rejection_saturation=reported, early_peer_admissions=early_peers,
        users=[dict(identity="limited", requests=3, rpm=2, priority="low"),
               dict(identity="peer", requests=16, rpm=18, priority="high")])
    rejection = next(record for record in report.requests if record.http_status == 429)
    assert sum(record.http_status == 200 and record.completed_ms < rejection.dispatch_ms
               for record in report.requests) == early_peers + 2
    assert check.status == expected, check.summary
    assert reason in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("admitted,expected", [(51, "pass"), (56, "fail")])
async def test_priority_borrowing_stops_at_the_gateway_ratio_boundary(monkeypatch, admitted, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=100, duration_seconds=3,
        saturation_threshold=.55, admitted_affected=admitted, peer_delay=.25, early_peer_admissions=4,
        users=[dict(identity="limited", requests=57, rpm=2, priority="low"),
               dict(identity="peer", requests=6, rpm=6, priority="high")])
    policy = check.metrics["gateway_policy"]
    assert [user["admitted"] for user in policy["users"]] == [admitted, 6]
    assert check.status == expected, check.summary
    if expected == "fail":
        assert "exceeded the declared RPM allowance" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("header,threshold,expected", [
    ("61.54%", .6152, "pass"), ("61.54%", .61539, "inconclusive"),
    (None, .6152, "inconclusive"), ("61.50%", .6152, "inconclusive"),
    ("62.00%", .6152, "inconclusive"), ("61.54%, 90.00%", .6152, "inconclusive"),
])
async def test_priority_uses_consistent_finer_saturation_evidence(monkeypatch, header, threshold, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=13,
        saturation_threshold=threshold, rejection_saturation=61.5, saturation_header=header,
        users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
               dict(identity="peer", requests=8, rpm=8, priority="high")])
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        assert "Strict-mode evidence" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("early_peers,model_tpm,expected", [
    (1, "not configured", "pass"), (2, "not configured", "inconclusive"),
    (3, "not configured", "fail"), (1, "60", "inconclusive"),
    (1, None, "inconclusive"), (1, "not configured, Model TPM: 60", "inconclusive"),
    (3, "60", "fail"),
])
async def test_priority_borrowing_accounts_for_peer_admission_order(monkeypatch, early_peers, model_tpm, expected):
    config = configuration("priority_rpm", saturation_threshold=.49,
        users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
               dict(identity="peer", requests=early_peers + 3, rpm=8, priority="high")])
    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")
    counts = {"limited": 0, "peer": 0}
    admitted = 0
    initial_peers_completed = asyncio.Event()
    next_peer_dispatched = asyncio.Event()
    first_rejection_dispatched = asyncio.Event()
    saturation_reached = asyncio.Event()
    rejection_observed = asyncio.Event()

    async def handler(request):
        nonlocal admitted
        if request.method != "POST":
            return httpx.Response(404)
        who = "limited" if request.headers["authorization"] == "Bearer limited-secret" else "peer"
        counts[who] += 1
        sequence = counts[who]
        user = next(user for user in config.gateway_policy.users if user.identity == who)
        headers = {"x-litellm-priority": user.priority, "retry-after": "60", "x-litellm-rate-limiter-version": "v3",
                   "x-ratelimit-priority_model-limit-requests": str(user.rpm), "x-litellm-model-group": MODEL_POOL}
        if who == "peer":
            if sequence == early_peers:
                initial_peers_completed.set()
            if sequence == early_peers + 1:
                next_peer_dispatched.set()
                await first_rejection_dispatched.wait()
                saturation_reached.set()
            if sequence == early_peers + 2:
                await rejection_observed.wait()
        elif sequence == 1:
            await initial_peers_completed.wait()
        elif sequence == 3:
            await next_peer_dispatched.wait()
        elif sequence > 3:
            first_rejection_dispatched.set()
            await saturation_reached.wait()
            if sequence == 5:
                rejection_observed.set()
            saturation = admitted / 10
            headers["x-litellm-saturation"] = f"{saturation:.2%}"
            model_tpm_field = f"Model TPM: {model_tpm}, " if model_tpm is not None else ""
            message = (f"Priority-based rate limit exceeded. Model: {MODEL_POOL}, Priority: low, Rate limit type: requests, "
                       f"{model_tpm_field}Model RPM: 10, Remaining: 0, Model saturation: {saturation:.1%}")
            return httpx.Response(429, headers=headers, json={"error": {"type": "throttling_error", "code": "429", "message": message}})
        admitted += 1
        return httpx.Response(200, headers=headers, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config)
    check = next(check for check in report.checks if check.id == "gateway_policy")
    assert [user["admitted"] for user in check.metrics["gateway_policy"]["users"]] == [3, early_peers + 3]
    assert check.status == expected, check.summary
    if expected == "fail":
        assert "completed admissions reached model saturation" in check.summary
    if expected == "inconclusive":
        reason = "Overlapping admissions leave saturation uncertain" if model_tpm == "not configured" else "Model TPM is configured or unknown"
        assert reason in check.summary
