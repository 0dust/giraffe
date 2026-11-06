"""Policy verdicts distinguish response success from gateway admission evidence."""
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
    _, run, _ = await exercise(monkeypatch, "priority_rpm", saturation_threshold=.5,
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
    check, _, _ = await exercise(monkeypatch, "priority_rpm", behavior="overadmit", saturation_threshold=.5, full=True,
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
async def test_malformed_assistant_reply_does_not_show_peer_continuation(monkeypatch):
    check, report, _ = await exercise(monkeypatch, behavior="malformed_peer", full=True)
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
@pytest.mark.parametrize("reported,expected", [(55.0, "pass"), (54.9, "inconclusive")])
async def test_priority_compares_reported_saturation_at_the_exact_threshold(monkeypatch, reported, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=20,
        saturation_threshold=.55, rejection_saturation=reported,
        users=[dict(identity="limited", requests=5, rpm=2, priority="low"),
               dict(identity="peer", requests=10, rpm=18, priority="high")])
    assert check.status == expected, check.summary
    if expected == "inconclusive":
        assert "strict-mode evidence" in check.summary


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("admitted,expected", [(55, "pass"), (56, "fail")])
async def test_priority_borrowing_stops_at_the_gateway_ratio_boundary(monkeypatch, admitted, expected):
    check, _, _ = await exercise(monkeypatch, "priority_rpm", full=True, model_rpm=100, duration_seconds=3,
        saturation_threshold=.55, rejection_saturation=56.0, admitted_affected=admitted, peer_delay=.04,
        users=[dict(identity="limited", requests=57, rpm=2, priority="low"),
               dict(identity="peer", requests=28, rpm=28, priority="high")])
    policy = check.metrics["gateway_policy"]
    assert [user["admitted"] for user in policy["users"]] == [admitted, 28]
    assert check.status == expected, check.summary
    if expected == "fail":
        assert "exceeded the declared RPM allowance" in check.summary
