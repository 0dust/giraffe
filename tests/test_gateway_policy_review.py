"""Policy verdicts distinguish response success from gateway admission evidence."""
import asyncio
import time

import httpx
import pytest

from giraffe.gateway_policy import summarize
from giraffe.gateway_response import response_evidence
from giraffe.models import RunConfig
from giraffe.runner import run_suite
from tests.test_gateway_policy import CACHE_KEY, LIMITED_KEY_HASH, MODEL_POOL, configuration, exercise


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


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("shared,stall", [
    (False, "build"), (True, "build"), (True, "lease"), (True, "progress"), (True, None),
])
async def test_policy_admits_only_at_prepared_transport(monkeypatch, shared, stall):
    from giraffe.runner import _Run
    from giraffe.transport import HTTPClientPool

    config = configuration(duration_seconds=.15 if stall is not None else 2)
    if shared:
        config.checks = ["serving", "gateway_policy"]
    config.samples = 2
    config.stream = False
    config.targets[0].api_key_env = "SHARED"
    for name in ("SHARED", "LIMITED", "PEER"):
        monkeypatch.setenv(name, name.lower())
    wire, events, runs = [], [], []
    stalled = False

    def stall_once(kind):
        nonlocal stalled
        if stall == kind and not stalled:
            stalled = True
            time.sleep(.25)

    def progress(event):
        if event["scenario"] == "gateway_policy" and event["event"] in {"request_started", "request_completed"}:
            events.append((event["event"], event["request_slot"], time.monotonic()))
            if event["event"] == "request_started":
                stall_once("progress")

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        wire.append((request.headers["authorization"], time.monotonic()))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    class Transport(httpx.AsyncClient):
        def build_request(self, method, url, **kwargs):
            if "cache" in (kwargs.get("json") or {}):
                stall_once("build")
            return super().build_request(method, url, **kwargs)

    send = HTTPClientPool.send

    async def delayed_lease(self, request, **kwargs):
        if stall != "lease" or stalled or request.headers.get("authorization") == "Bearer shared":
            return await send(self, request, **kwargs)
        acquire = self._slots.acquire

        async def slow_acquire():
            await acquire()
            stall_once("lease")

        with monkeypatch.context() as lease_patch:
            lease_patch.setattr(self._slots, "acquire", slow_acquire)
            return await send(self, request, **kwargs)

    init = _Run.__init__

    def capture_run(self, *args, **kwargs):
        init(self, *args, **kwargs)
        runs.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: Transport(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(HTTPClientPool, "send", delayed_lease)
    monkeypatch.setattr(_Run, "__init__", capture_run)
    report = await run_suite(config, progress=progress)
    run, = runs
    observation = report.observations["targets"]["proxy"]["gateway_policy"]
    shared_wire = [credential for credential, _ in wire if credential == "Bearer shared"]
    policy_wire = [at for credential, at in wire if credential != "Bearer shared"]
    policy_records = [record for record in report.requests if record.identity is not None]
    assert len(shared_wire) == (3 if shared else 0)
    assert len(policy_wire) == len(policy_records) == sum(observation["offered"].values())
    assert run.attempted == report.observations["request_budget_used"] == len(wire)
    assert run.slots.qsize() == run.max_connections
    assert all((at - run.start)*1000 < observation["end_ms"] for at in policy_wire)
    starts = {slot: at for event, slot, at in events if event == "request_started"}
    completions = {slot: at for event, slot, at in events if event == "request_completed"}
    assert starts.keys() == completions.keys()
    assert len(starts) == len(policy_wire)
    assert all(starts[slot] <= completions[slot] for slot in starts)
    if stall == "progress":
        assert stalled and policy_wire and min(policy_wire) <= min(starts.values())
    elif stall is not None:
        assert stalled and not policy_wire
    else:
        assert observation["complete"] and len(policy_wire) == 13


@pytest.mark.parametrize("late", [True, False])
async def test_policy_admission_needs_completion_inside_observation(monkeypatch, late):
    from giraffe.client import LLMClient
    from giraffe.gateway_policy import execute, result
    from giraffe.runner import _Run

    cfg = configuration(duration_seconds=.15, users=[dict(identity="limited", requests=4, rpm=2),
                                                        dict(identity="peer", requests=4, rpm=8)])
    target = cfg.targets[0]
    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")
    limited_count = 0
    bucket_start = time.monotonic()

    async def handler(request):
        nonlocal limited_count
        if request.headers.get("authorization") == "Bearer limited-secret":
            limited_count += 1
            if limited_count == 3:
                message = (f"Rate limit exceeded for api_key: {LIMITED_KEY_HASH}. Limit type: requests. "
                           "Current limit: 2, Remaining: 0. Limit resets at: 2026-01-01 00:00:00 UTC")
                return httpx.Response(429, headers={"retry-after": "2"}, json={
                    "error": {"type": "throttling_error", "code": "429", "message": message}})
            if late and limited_count == 4:
                # A synchronous transport segment can postpone timeout delivery
                # until the gateway's next bucket has legitimately admitted it.
                time.sleep(2.1)
                assert time.monotonic() > bucket_start + 2
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    run = _Run(cfg, None, None)
    async with LLMClient(target, cfg, max_connections=2) as client:
        await execute(run, target, client)
    check = result(run, target)
    policy = check.metrics["gateway_policy"]
    last = [record for record in run.records if record.identity == "limited"][-1]
    assert last.http_status == 200 and policy["users"][0]["admitted"] == 3
    if late:
        assert last.dispatch_ms < policy["end_ms"] < last.completed_ms
        assert check.status == "inconclusive" and policy["qualification"] == "unverified"
        assert not policy["complete"]
        assert "within the observation window" in check.summary
    else:
        assert last.completed_ms <= policy["end_ms"]
        assert check.status == "fail" and policy["qualification"] == "observed_failure"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
async def test_restart_readiness_makes_policy_window_unverified(monkeypatch, mode):
    check, report, _ = await exercise(monkeypatch, mode, full=True, restart=True)
    readiness = [record for record in report.requests if record.scenario == "readiness"]
    assert len(readiness) == 1 and readiness[0].identity is None and readiness[0].valid
    policy = check.metrics["gateway_policy"]
    assert [user["admitted"] for user in policy["users"]] == [1, 8]
    assert policy["users"][0]["rejected"] == 4
    assert check.status == "inconclusive" and policy["qualification"] == "unverified"
    assert "no restart readiness probes" in check.summary


async def test_genuine_early_rejection_is_not_hidden_by_a_later_failure(monkeypatch):
    _, run, _ = await exercise(monkeypatch, behavior="early")
    last = [record for record in run.records if record.identity == "limited"][-1]
    last.status, last.http_status, last.valid = "failed", 500, False
    last.gateway_evidence = None
    result = summarize(run.config.gateway_policy, run.observations["proxy"]["gateway_policy"], run.records)
    assert result["qualification"] == "observed_failure"
    assert result["status"] == "fail"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("behavior", ["malformed_peer", "peer_tool_call", "peer_function_call", "peer_invalid_refusal",
    "peer_duplicate_error", "peer_duplicate_role", "peer_duplicate_usage"])
async def test_malformed_assistant_reply_does_not_show_peer_continuation(monkeypatch, behavior):
    check, report, _ = await exercise(monkeypatch, behavior=behavior, full=True)
    peer = [record for record in report.requests if record.identity == "peer"]
    assert len(peer) == 8 and all(record.http_status == 200 and not record.valid for record in peer)
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert not check.metrics["gateway_policy"]["peer_continuation"]


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("behavior,reasoning_tokens,expected", [
    ("peer_inconsistent_reasoning", 128, "inconclusive"),
    ("peer_zero_answer_tokens", 2, "inconclusive"),
    ("peer_consistent_reasoning", 1, "pass"),
])
async def test_policy_requires_reasoning_tokens_to_fit_completion_total(monkeypatch, behavior, reasoning_tokens, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", behavior=behavior, full=True)
    summary = check.metrics["gateway_policy"]
    assert summary["users"][1]["completion_tokens"] == [2] * 8
    assert summary["users"][1]["reasoning_tokens"] == [reasoning_tokens] * 8
    assert summary["comparable_actual_work"] == (expected == "pass")
    assert check.status == expected, check.summary


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
async def test_priority_borrowing_stops_at_the_gateway_ratio_boundary(monkeypatch):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=100, duration_seconds=3,
        saturation_threshold=.55, admitted_affected=56, peer_delay=.5,
        users=[dict(identity="limited", requests=57, rpm=2, priority="low"),
               dict(identity="peer", requests=4, rpm=4, priority="high")])
    assert [user["admitted"] for user in check.metrics["gateway_policy"]["users"]] == [56, 4]
    assert check.status == "fail", check.summary
    assert "exceeded the declared RPM allowance" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("header,threshold,expected", [
    ("61.54%", .6152, "pass"), ("61.54%", .61539, "inconclusive"),
    (None, .6152, "pass"), ("61.50%", .6152, "inconclusive"),
    ("62.00%", .6152, "inconclusive"), ("61.54%, 90.00%", .6152, "inconclusive"),
])
async def test_priority_checks_rounded_saturation_at_an_rpm_boundary(monkeypatch, header, threshold, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=13,
        saturation_threshold=threshold, rejection_saturation=61.5, saturation_header=header, early_peer_admissions=6,
        users=[dict(identity="limited", requests=3, rpm=2, priority="low"),
               dict(identity="peer", requests=8, rpm=8, priority="high")])
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        assert "saturation" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("reported,expected", [(90.0, "inconclusive"), (25.0, "inconclusive"), (20.0, "pass")])
async def test_priority_rejects_saturation_without_a_feasible_rpm_count(monkeypatch, reported, expected):
    check, report, _ = await exercise(monkeypatch, "priority_rpm", full=True, saturation_threshold=.2,
        rejection_saturation=reported, peer_delay=.2,
        users=[dict(identity="limited", requests=3, rpm=2, priority="low"),
               dict(identity="peer", requests=5, rpm=8, priority="high")])
    rejection = next(record for record in report.requests if record.http_status == 429)
    assert sum(record.http_status == 200 and record.completed_ms < rejection.dispatch_ms
               for record in report.requests) == 2
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        assert "cannot match any possible model RPM count" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("reported,expected", [(0.1, "pass"), (0.2, "inconclusive")])
async def test_priority_matches_gateway_formatting_at_rounding_ties(monkeypatch, reported, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=2000, saturation_threshold=.0015,
        rejection_saturation=reported, saturation_header="0.15%", early_peer_admissions=1, peer_delay=.075,
        users=[dict(identity="limited", requests=3, rpm=2, priority="low"),
               dict(identity="peer", requests=5, rpm=8, priority="high")])
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        assert "cannot match any possible model RPM count" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("reported,middle_charge,model_tpm,expected", [
    ((30.0, 20.0), False, "not configured", "inconclusive"),
    ((30.0, 30.0), False, "not configured", "pass"),
    ((20.0, 30.0), False, "not configured", "pass"),
    ((30.0, 30.0), True, "not configured", "inconclusive"),
    ((30.0, 40.0), True, "not configured", "pass"),
    ((30.0, 20.0), False, None, "inconclusive"),
    ((25.0, 25.0), False, None, "inconclusive"),
    ((37.5, 25.0), False, "80", "inconclusive"),
    ((25.0, 25.0), False, "80", "inconclusive"),
    ((30.0, 30.0), True, "81", "inconclusive"),
    ((30.3, 30.3), True, "66", "inconclusive"),
    ((30.0, 30.0), True, "081", "inconclusive"),
])
async def test_priority_rejections_share_one_counter_history(monkeypatch, reported, middle_charge, model_tpm, expected):
    from giraffe.runner import _Run

    raw = configuration("priority_rpm", saturation_threshold=.2).model_dump()
    raw["gateway_policy"]["users"] = [dict(identity="limited", requests=4, rpm=2, priority="low"),
        dict(identity="peer", requests=2, rpm=4, priority="high"),
        dict(identity="middle", requests=1, rpm=4, priority="middle")]
    raw["targets"][0]["test_identities"]["middle"] = {"api_key_env": "MIDDLE"}
    config = RunConfig.model_validate(raw)
    users = {user.identity: user for user in config.gateway_policy.users}
    for name in users:
        monkeypatch.setenv(name.upper(), f"{name}-secret")
    counts = dict.fromkeys(users, 0)
    peer_started = asyncio.Event()
    first_rejected = asyncio.Event()
    second_rejected = asyncio.Event()
    middle_completed = asyncio.Event()

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        who = request.headers["authorization"].removeprefix("Bearer ").removesuffix("-secret")
        user = users[who]
        counts[who] += 1
        sequence = counts[who]
        headers = {"x-litellm-priority": user.priority, "retry-after": "60", "x-litellm-rate-limiter-version": "v3",
                   "x-ratelimit-priority_model-limit-requests": str(user.rpm), "x-litellm-model-group": MODEL_POOL}
        if who == "peer" and sequence == 1:
            peer_started.set()
            await second_rejected.wait()
        if who == "limited" and sequence >= 3:
            saturation = reported[sequence - 3]
            headers["x-litellm-saturation"] = f"{saturation:.2f}%"
            tpm = f"Model TPM: {model_tpm}, " if model_tpm is not None else ""
            message = (f"Priority-based rate limit exceeded. Model: {MODEL_POOL}, Priority: low, Rate limit type: requests, "
                       f"{tpm}Model RPM: 10, Remaining: 0, Model saturation: {saturation:.1f}%")
            return httpx.Response(429, headers=headers, json={"error": {"type": "throttling_error", "code": "429", "message": message}})
        return httpx.Response(200, headers=headers, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}})

    request = _Run.request

    async def ordered_request(self, target, client, spec, **kwargs):
        sequence = spec.workload["request_sequence"]
        if spec.identity == "middle":
            await (first_rejected if middle_charge else second_rejected).wait()
        if spec.identity == "limited" and sequence == 2:
            await peer_started.wait()
        if middle_charge and spec.identity == "limited" and sequence == 4:
            await middle_completed.wait()
        record = await request(self, target, client, spec, **kwargs)
        if spec.identity == "limited" and sequence == 3:
            first_rejected.set()
        if spec.identity == "limited" and sequence == 4:
            second_rejected.set()
        if spec.identity == "middle":
            middle_completed.set()
        return record

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(_Run, "request", ordered_request)
    # The middle identity needs a slot while the first peer is still in flight.
    config.concurrency = 3
    report = await run_suite(config)
    first, second = [record for record in report.requests if record.http_status == 429]
    first_peer = next(record for record in report.requests if record.identity == "peer")
    assert first_peer.dispatch_ms < first.dispatch_ms < first.completed_ms < second.dispatch_ms
    assert second.completed_ms < first_peer.completed_ms
    if middle_charge:
        middle = next(record for record in report.requests if record.identity == "middle")
        assert first.completed_ms < middle.dispatch_ms < middle.completed_ms < second.dispatch_ms
    check = next(check for check in report.checks if check.id == "gateway_policy")
    assert check.metrics["gateway_policy"]["peer_continuation"] == (model_tpm not in (None, "081"))
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        if model_tpm in (None, "081"):
            reason = "does not identify"
        elif reported[0] in (25., 30.3, 37.5):
            reason = "cannot match any possible model RPM count"
        else:
            reason = "contradicts earlier limiter evidence"
        assert reason in check.summary


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
        if model_tpm == "not configured":
            reason = "Overlapping admissions leave saturation uncertain"
        elif model_tpm == "60":
            reason = "observed RPM admissions do not establish strict mode"
        else:
            reason = "does not identify"
        assert reason in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("behavior,confirmation,expected", [
    ("enforce", True, "pass"),
    ("enforce", False, "inconclusive"),
    ("contradictory_priority", True, "inconclusive"),
])
async def test_priority_requires_canonical_rejection_and_confirmed_named_pools(monkeypatch, behavior, confirmation, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, behavior=behavior,
                                 named_reservations_confirmed=confirmation)
    summary = check.metrics["gateway_policy"]
    assert check.status == expected, check.summary
    assert [user["admitted"] for user in summary["users"]] == [2, 8]
    if behavior == "contradictory_priority":
        assert not summary["peer_continuation"]
        assert "does not identify" in check.summary
    elif not confirmation:
        assert summary["peer_continuation"]
        assert "configured named reservations" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("model_tpm,reported,header,threshold,expected", [
    ("1000000", 90.0, "90.00%", .75, "inconclusive"),
    ("80", 25.0, "25.00%", .22, "inconclusive"),
    ("80", 23.8, "23.75%", .22, "inconclusive"),
    ("80", 25.0, "25.00%", .2, "inconclusive"),
    ("80", 30.0, "30.00%", .25, "inconclusive"),
    ("80", 30.0, "30.00%", .2, "pass"),
    ("80", 20.0, "20.00%", .2, "pass"),
])
async def test_priority_saturation_requires_independent_rpm_evidence(
        monkeypatch, model_tpm, reported, header, threshold, expected):
    check, report, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_tpm=model_tpm,
        saturation_threshold=threshold, rejection_saturation=reported, saturation_header=header, peer_delay=.2,
        users=[dict(identity="limited", requests=3, rpm=2, priority="low"),
               dict(identity="peer", requests=5, rpm=8, priority="high")])
    rejection = next(record for record in report.requests if record.http_status == 429)
    assert sum(record.http_status == 200 and record.completed_ms < rejection.dispatch_ms
               for record in report.requests) == 2
    assert check.metrics["gateway_policy"]["peer_continuation"]
    assert check.status == expected, check.summary


@pytest.mark.usefixtures("local_tokenizer")
async def test_priority_rejections_require_one_model_tpm_configuration(monkeypatch):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, behavior="conflicting_model_tpm")
    assert check.metrics["gateway_policy"]["peer_continuation"]
    assert check.status == "inconclusive"
    assert "Model TPM configuration differs" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("unit,expected", [(None, "pass"), ("requests", "pass"), ("tokens", "inconclusive")])
async def test_key_rejection_unit_header_agrees_with_body_when_present(monkeypatch, unit, expected):
    check, _, _ = await exercise(monkeypatch, full=True, rejection_unit=unit)
    assert check.status == expected, check.summary
    assert check.metrics["gateway_policy"]["peer_continuation"] == (expected == "pass")
    if unit == "tokens":
        assert "does not identify" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("mode,status,scope,value,model_tpm,expected", [
    ("priority_rpm", 200, "model_saturation_check-limit-requests", "10", "not configured", "pass"),
    ("priority_rpm", 200, "model_saturation_check-limit-requests", "100", "not configured", "inconclusive"),
    ("priority_rpm", 200, "model_saturation_check-limit-requests", "10, 10", "not configured", "inconclusive"),
    ("priority_rpm", 200, "model_saturation_check-limit-tokens", "100", "not configured", "inconclusive"),
    ("priority_rpm", 200, "model_saturation_check-limit-tokens", "", "not configured", "inconclusive"),
    ("priority_rpm", 200, "model_saturation_check-limit-tokens", "100", "100", "pass"),
    ("priority_rpm", 429, "model_saturation_check-limit-tokens", "200", "100", "inconclusive"),
    ("priority_rpm", 429, "priority_model-limit-requests", "2", "not configured", "pass"),
    ("priority_rpm", 429, "priority_model-limit-requests", "3", "not configured", "inconclusive"),
    ("key_rpm", 200, "api_key-limit-requests", "2", "not configured", "pass"),
    ("key_rpm", 200, "api_key-limit-requests", "3", "not configured", "inconclusive"),
    ("key_rpm", 429, "api_key-limit-requests", "garbage", "not configured", "inconclusive"),
])
async def test_gateway_policy_requires_consistent_scoped_limits(
        monkeypatch, mode, status, scope, value, model_tpm, expected):
    transport = httpx.MockTransport

    def add_scoped_limit(handler):
        async def response(request):
            result = await handler(request)
            if result.status_code == status and (mode == "priority_rpm"
                    or request.headers["authorization"] == "Bearer limited-secret"):
                result.headers[f"x-ratelimit-{scope}"] = value
            return result

        return transport(response)

    monkeypatch.setattr(httpx, "MockTransport", add_scoped_limit)
    check, _, _ = await exercise(monkeypatch, mode, full=True, model_tpm=model_tpm)
    assert check.metrics["gateway_policy"]["peer_continuation"]
    assert check.status == expected, check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("priority,expected", [(None, "pass"), ("low", "pass"), ("high" * 17, "inconclusive")])
async def test_present_priority_rejection_header_must_agree_with_body(monkeypatch, priority, expected):
    transport = httpx.MockTransport

    def replace_header(handler):
        async def response(request):
            result = await handler(request)
            if result.status_code == 429:
                if priority is None:
                    result.headers.pop("x-litellm-priority")
                else:
                    result.headers["x-litellm-priority"] = priority
            return result

        return transport(response)

    monkeypatch.setattr(httpx, "MockTransport", replace_header)
    check, report, _ = await exercise(monkeypatch, "priority_rpm", full=True)
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        assert priority not in report.model_dump_json()
        assert "Priority tier" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("mode,scope", [("key_rpm", "api_key"), ("priority_rpm", "priority_model")])
@pytest.mark.parametrize("version,remaining,expected", [
    (None, None, "pass"),
    ("v3", "0", "pass"),
    ("v2", None, "inconclusive"),
    ("v3, v2", None, "inconclusive"),
    ("", None, "inconclusive"),
    ("v3", "1", "inconclusive"),
    ("v3", "0, 0", "inconclusive"),
    ("v3", "-1", "key_only"),
])
async def test_policy_rejection_version_and_scoped_remaining_agree(
        monkeypatch, mode, scope, version, remaining, expected):
    transport = httpx.MockTransport

    def replace_headers(handler):
        async def response(request):
            result = await handler(request)
            if result.status_code == 429:
                if version is None:
                    result.headers.pop("x-litellm-rate-limiter-version")
                else:
                    result.headers["x-litellm-rate-limiter-version"] = version
                if remaining is not None:
                    result.headers[f"x-ratelimit-{scope}-remaining-requests"] = remaining
                result.headers[f"x-ratelimit-{scope}-remaining-tokens"] = "7"
                result.headers["x-ratelimit-model_saturation_check-remaining-requests"] = "7"
                result.headers["x-ratelimit-remaining-requests"] = "7"
            return result

        return transport(response)

    monkeypatch.setattr(httpx, "MockTransport", replace_headers)
    check, _, _ = await exercise(monkeypatch, mode, full=True)
    if expected == "key_only":
        expected = "pass" if mode == "key_rpm" else "inconclusive"
    assert check.status == expected, check.summary
    assert check.metrics["gateway_policy"]["peer_continuation"] == (expected == "pass")


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("body_reset,header,expected", [
    ("2026-01-01 00:00:00 UTC", None, "pass"),
    ("2026-01-01 00:00:00 UTC", "2026-01-01 00:00:00 UTC", "pass"),
    ("now", None, "inconclusive"),
    ("2026-02-31 00:00:00 UTC", None, "inconclusive"),
    ("2026-01-01 00:00:00 UTC", "2026-01-01 00:00:01 UTC", "inconclusive"),
    ("2026-02-31 00:00:00 UTC", "2026-02-31 00:00:00 UTC", "inconclusive"),
])
async def test_key_rejection_reset_header_matches_body(monkeypatch, body_reset, header, expected):
    transport = httpx.MockTransport

    def add_reset_header(handler):
        async def response(request):
            result = await handler(request)
            if result.status_code == 429 and header is not None:
                result.headers["reset_at"] = header
            return result

        return transport(response)

    monkeypatch.setattr(httpx, "MockTransport", add_reset_header)
    check, report, _ = await exercise(monkeypatch, full=True, key_reset=body_reset)
    assert check.status == expected, check.summary
    serialized = report.model_dump_json()
    assert f'"{body_reset}"' not in serialized
    assert "Limit resets at:" not in serialized


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("reset,expected", [
    ("2026-01-01 00:00:00 UTC", "fail"),
    ("now", "inconclusive"),
    ("2026-02-31 00:00:00 UTC", "inconclusive"),
])
async def test_key_policy_early_rejection_needs_valid_body_reset(monkeypatch, reset, expected):
    check, _, _ = await exercise(monkeypatch, behavior="early", full=True, key_reset=reset)
    summary = check.metrics["gateway_policy"]
    assert summary["users"][0]["admitted"] == 1
    assert summary["users"][0]["rejected"] == 4
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        assert summary["qualification"] == "unverified"
        assert "does not identify" in check.summary
