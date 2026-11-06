"""LiteLLM qualification requires the effective window and reserved RPM evidence."""
import pytest

from giraffe.gateway_policy import summarize
from giraffe.gateway_response import response_evidence
from tests.test_gateway_policy import LIMITED_KEY_HASH, MODEL_POOL, exercise


def observed(run):
    return summarize(run.config.gateway_policy, run.observations["proxy"]["gateway_policy"], run.records)


@pytest.mark.parametrize("window", [None, 0, 0.01])
async def test_unknown_or_crossed_effective_window_stays_unverified(monkeypatch, window):
    _, run, _ = await exercise(monkeypatch)
    for record in run.records:
        if record.http_status == 429:
            record.gateway_evidence.retry_after_seconds = window
    assert observed(run)["qualification"] == "unverified"


async def test_conflicting_effective_windows_stay_unverified(monkeypatch):
    _, run, _ = await exercise(monkeypatch)
    rejected = [record for record in run.records if record.http_status == 429]
    for record in rejected:
        record.gateway_evidence.retry_after_seconds = 60
    rejected[-1].gateway_evidence.retry_after_seconds = 30
    assert observed(run)["qualification"] == "unverified"


@pytest.mark.parametrize("completed_ms", [4500, 6150])
async def test_legitimate_admission_after_short_bucket_reset_is_not_policy_failure(monkeypatch, completed_ms):
    _, run, _ = await exercise(monkeypatch)
    for record in run.records:
        if record.http_status == 429:
            record.gateway_evidence.retry_after_seconds = 5
    last = [record for record in run.records if record.identity == "limited"][-1]
    last.status, last.http_status, last.valid = "completed", 200, True
    last.dispatch_ms, last.completed_ms = completed_ms - 50, completed_ms
    run.observations["proxy"]["gateway_policy"].update(start_ms=0, end_ms=completed_ms)
    assert observed(run)["qualification"] == "unverified"


@pytest.mark.parametrize("limit,version", [(None, "v3"), (1, "v3"), (3, None), (3, "v2")])
async def test_priority_pool_requires_matching_scoped_v3_limit(monkeypatch, limit, version):
    _, run, _ = await exercise(monkeypatch, "priority_rpm", behavior="overadmit", saturation_threshold=.5,
        users=[dict(identity="limited", requests=5, rpm=3, priority="low"),
               dict(identity="peer", requests=5, rpm=7, priority="high")])
    for record in run.records:
        if record.http_status == 200:
            headers = {"x-litellm-priority": "low" if record.identity == "limited" else "high",
                       "x-litellm-model-group": MODEL_POOL}
            if version:
                headers["x-litellm-rate-limiter-version"] = version
            if limit is not None:
                headers["x-ratelimit-priority_model-limit-requests"] = str(limit if record.identity == "limited" else 7)
            record.gateway_evidence = response_evidence(200, headers=headers,
                declared_priority="low" if record.identity == "limited" else "high")
    assert observed(run)["qualification"] == "unverified"


def test_allowlisted_priority_limit_has_version_provenance():
    evidence = response_evidence(200, headers={"x-litellm-rate-limiter-version": "v3",
        "x-ratelimit-priority_model-limit-requests": "3"})
    assert getattr(evidence, "limiter_version", None) == "v3"
    assert getattr(evidence, "priority_request_limit", None) == 3


@pytest.mark.parametrize("status,error_type,expected", [
    (401, "auth_error", "authentication"),
    (403, "key_model_access_denied", "model_access"),
    (429, "throttling_error", "rate_limit"),
])
def test_duplicate_error_types_do_not_attest_gateway_policy(status, error_type, expected):
    message = (f"Rate limit exceeded for api_key: {LIMITED_KEY_HASH}. Limit type: requests. "
               "Current limit: 2, Remaining: 0. Limit resets at: now")
    body = f'"type":"{error_type}","code":{status},"message":"{message}"'
    clear = ('{"error":{' + body + '}}').encode()
    duplicate = ('{"error":{"type":"auth_provider_unavailable",' + body + '}}').encode()
    assert response_evidence(status, clear, expected_key_hash=LIMITED_KEY_HASH).error_kind == expected
    evidence = response_evidence(status, duplicate, expected_key_hash=LIMITED_KEY_HASH)
    assert evidence.error_kind == "unknown"
    if status == 429:
        assert evidence.policy_rejection == "unknown"


@pytest.mark.parametrize("duplicate", [False, True])
async def test_declared_custom_auth_header_qualifies_only_distinct_keys(monkeypatch, duplicate):
    import httpx
    from giraffe import runner
    from giraffe.client import LLMClient
    from giraffe.gateway_policy import execute, result
    from giraffe.models import RunConfig
    from tests.test_gateway_policy import configuration

    raw = configuration().model_dump(mode="json")
    raw["gateway_policy"]["auth_header"] = "X-Litellm-Key"
    raw["targets"][0]["test_identities"] = {
        "limited": {"headers_env": {"x-litellm-key": "CUSTOM_LIMITED"}},
        "peer": {"headers_env": {"X-Litellm-Key": "CUSTOM_PEER"}}}
    cfg = RunConfig.model_validate(raw)
    monkeypatch.setenv("CUSTOM_LIMITED", "Bearer limited-secret")
    monkeypatch.setenv("CUSTOM_PEER", "bearer limited-secret" if duplicate else "Bearer peer-secret")
    counts = {"limited": 0, "peer": 0}

    async def handler(request):
        import asyncio
        assert "authorization" not in request.headers
        identity = {"bearer limited-secret": "limited", "bearer peer-secret": "peer"}[request.headers["x-litellm-key"].lower()]
        counts[identity] += 1
        await asyncio.sleep(.01)
        if identity == "limited" and counts[identity] > 2:
            return httpx.Response(429, headers={"retry-after": "60"}, json={"error": {
                "type": "throttling_error", "code": "429", "message":
                f"Rate limit exceeded for api_key: {LIMITED_KEY_HASH}. Limit type: requests. Current limit: 2, Remaining: 0. Limit resets at: now"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    run = runner._Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
        await execute(run, cfg.targets[0], client)
    check = result(run, cfg.targets[0])
    assert check.metrics["gateway_policy"]["qualification"] == ("unverified" if duplicate else "observed")
    assert sum(counts.values()) == (0 if duplicate else 13)
    assert "limited-secret" not in check.model_dump_json()


@pytest.mark.parametrize("peer_header", ["X-LiteLLM-API-Key", "api-key"])
async def test_default_policy_rejects_duplicate_effective_keys_before_dispatch(monkeypatch, peer_header):
    import httpx
    from giraffe import runner
    from giraffe.client import LLMClient
    from giraffe.gateway_policy import execute, result
    from giraffe.models import RunConfig
    from tests.test_gateway_policy import configuration

    raw = configuration().model_dump(mode="json")
    identities = raw["targets"][0]["test_identities"]
    identities["limited"]["headers_env"]["x-litellm-api-key"] = "SAME_KEY"
    if peer_header == "api-key":
        identities["peer"].pop("api_key_env")
    identities["peer"]["headers_env"][peer_header] = "SAME_KEY"
    config = RunConfig.model_validate(raw)
    monkeypatch.setenv("LIMITED", "front-limited")
    monkeypatch.setenv("PEER", "front-peer")
    monkeypatch.setenv("SAME_KEY", "same-gateway-key")

    def handle(request):
        pytest.fail(f"Duplicate effective keys must not dispatch {request.method} requests")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    state = runner._Run(config, None, None)
    target = config.targets[0]
    async with LLMClient(target, config, max_connections=2) as client:
        await execute(state, target, client)
    check = result(state, target)
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert "Distinct authenticated test credentials" in check.summary
    assert not state.records


@pytest.mark.usefixtures("local_tokenizer")
async def test_multiple_targets_cannot_assume_independent_key_windows(monkeypatch):
    from tests import test_gateway_policy as fixture

    original = fixture.configuration
    def multiple_targets(*args, **kwargs):
        config = original(*args, **kwargs)
        config.targets.append(config.targets[0].model_copy(update={"name": "other-model", "model": "other"}))
        return config
    monkeypatch.setattr(fixture, "configuration", multiple_targets)
    _, report, seen = await fixture.exercise(monkeypatch, full=True)
    checks = [check for check in report.checks if check.id == "gateway_policy"]
    assert len(checks) == 2 and len(seen) == 26
    assert all(check.metrics["gateway_policy"]["qualification"] == "unverified" for check in checks)
    assert len(report.requests) == 26


async def test_actual_priority_work_includes_reasoning_completion_tokens(monkeypatch):
    check, run, _ = await exercise(monkeypatch, "priority_rpm", behavior="reasoning_work")
    affected = [record for record in run.records if record.identity == "limited" and record.http_status == 200]
    assert all(record.output_tokens == 2 and record.completion_tokens == 128 for record in affected)
    assert check.metrics["gateway_policy"]["qualification"] == "unverified"
    assert not check.metrics["gateway_policy"]["comparable_actual_work"]


def test_custom_auth_field_round_trips_in_policy_ui():
    from tests.test_gateway_policy import configuration
    from tests.test_ux_catalogue import browser_logic

    browser_logic("""
const markup=renderGatewayPolicyEditor(config);
assert.match(markup,/Gateway authentication header/);
const values=new FormData();values.set('gateway_policy_rendered','1');
values.set('gateway_policy_auth_header','X-Litellm-Key');
assert.equal(configureGatewayPolicy(config,values).gateway_policy.auth_header,'X-Litellm-Key');
const rendered=renderGatewayPolicyResults({qualification:'unverified',interpretation:'fixture',users:[{
identity:'limited',priority:'low',declared_rpm:2,admitted:1,rejected:1,completed:1,planned:2,
input_tokens:[8],output_tokens:[2],completion_tokens:[128]}]});
assert.match(rendered,/including reasoning/);assert.match(rendered,/128/);
""", configuration())


async def test_peer_dispatched_before_rejection_is_not_continued_service(monkeypatch):
    _, run, _ = await exercise(monkeypatch)
    rejected = [record for record in run.records if record.http_status == 429]
    for record in rejected:
        record.dispatch_ms, record.completed_ms = 20, 30
    for record in run.records:
        if record.identity == "peer":
            record.dispatch_ms, record.completed_ms = 10, 40
        elif record.http_status == 200:
            record.dispatch_ms, record.completed_ms = 0, 5
    run.observations["proxy"]["gateway_policy"].update(start_ms=0, end_ms=50)
    summary = observed(run)
    assert summary["qualification"] == "unexercised"
    assert summary["peer_continuation"] is False
