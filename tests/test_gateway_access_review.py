import asyncio
import json

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.gateway_access import execute, result
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import compare_baseline, reproduction_export, write_report
from giraffe.runner import _Run, run_suite
from tests.test_gateway_access import config


async def test_cancelled_probe_retains_case_evidence_in_saved_reports(monkeypatch, tmp_path):
    cfg = config(retention="all")
    cfg.targets[0].test_identities["allowed"].api_key_env = None
    monkeypatch.setenv("DENY_KEY", "deny-secret")
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
@pytest.mark.parametrize("value", ["secret\nvalue", "secret ", " secret"])
async def test_malformed_credentials_leave_budget_for_valid_case(monkeypatch, credential, value):
    raw = config(max_requests=1).model_dump()
    identity = ({"api_key_env": "MALFORMED_KEY"} if credential == "api_key_env" else
                {"headers_env": {"X-Test-Key": "MALFORMED_KEY"}})
    raw["targets"][0]["test_identities"] = {"invalid": identity, "anonymous": {}}
    raw["gateway_access"]["cases"] = [
        {"name": "invalid", "identity": "invalid", "expected": "deny"},
        {"name": "valid", "identity": "anonymous", "expected": "allow"}]
    cfg = RunConfig.model_validate(raw)
    monkeypatch.setenv("MALFORMED_KEY", value)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

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


@pytest.mark.parametrize("credential", ["alias", "bearer", "litellm_header", "api_key", "custom_header", "anonymous"])
async def test_opposite_cases_with_identical_credentials_stay_unverified(monkeypatch, credential):
    secret = "same-virtual-key"
    monkeypatch.setenv("ALLOW_KEY", secret)
    monkeypatch.setenv("DENY_KEY", secret)
    monkeypatch.setenv("AUTH_HEADER", "bearer " + secret)
    cfg = config(retention="all")
    cases = cfg.gateway_access.cases
    identities = cfg.targets[0].test_identities
    if credential == "alias":
        cases[1].identity = cases[0].identity
    elif credential in {"bearer", "litellm_header", "api_key"}:
        header = {"bearer": "authorization", "litellm_header": "X-LiteLLM-API-Key", "api_key": "api-key"}[credential]
        monkeypatch.setenv("AUTH_HEADER", secret if credential == "api_key" else "bearer " + secret)
        identities["denied"] = identities["denied"].model_copy(
            update={"api_key_env": None, "headers_env": {header: "AUTH_HEADER"}})
    elif credential == "custom_header":
        cfg.gateway_access.auth_header = "X-Proxy-Key"
        for alias, header in (("allowed", "X-Proxy-Key"), ("denied", "x-proxy-key")):
            identities[alias] = identities[alias].model_copy(
                update={"api_key_env": None, "headers_env": {header: "AUTH_HEADER"}})
    elif credential == "anonymous":
        for alias in identities:
            identities[alias] = identities[alias].model_copy(update={"api_key_env": None})

    calls = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [{
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(401, json={"error": {"type": "token_not_found_in_db", "code": "401"}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    check = next(check for check in report.checks if check.id == "gateway_access")
    assert report.overall == check.status == "inconclusive"
    assert [row["reason"] for row in check.metrics["gateway_access"]["cases"]] == ["identity_conflict"] * 2
    assert not calls and not report.requests
    assert secret not in report.model_dump_json()


@pytest.mark.parametrize("role,finish,error_code", [
    ("user", "stop", "401"),
    (None, "stop", "401"),
    ("assistant", "nonsense", "401"),
    ("assistant", "stop", 503),
    ("assistant", "stop", "503"),
])
async def test_malformed_evidence_cannot_verify_access(monkeypatch, role, finish, error_code):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        if request.headers["authorization"] == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db", "code": error_code}})
        message = {"content": "OK"}
        if role is not None:
            message["role"] = role
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config(retention="all"))
    check = next(check for check in report.checks if check.id == "gateway_access")
    assert report.overall == check.status == "inconclusive"
    assert [row["observed"] for row in check.metrics["gateway_access"]["cases"]] == (
        ["allow", "unverified"] if error_code in (503, "503") else ["unverified", "deny"])


@pytest.mark.parametrize("message,finish,error,observed", [
    ({"refusal": "deny-secret"}, "stop", None, "allow"),
    ({"refusal": " "}, "stop", None, "unverified"),
    ({"refusal": "deny-secret"}, "length", None, "unverified"),
    ({"refusal": "deny-secret"}, "stop", {"type": "upstream_error"}, "unverified"),
    ({"content": "OK", "refusal": []}, "stop", None, "unverified"),
    ({"content": "OK", "refusal": "\ud800"}, "stop", None, "unverified"),
    ({"content": "OK", "reasoning_content": False}, "stop", None, "unverified"),
    ({"content": "OK", "reasoning_content": "thinking", "reasoning": "\ud800"}, "stop", None, "unverified"),
    ({"refusal": "deny-secret", "reasoning": {}}, "stop", None, "unverified"),
])
async def test_access_requires_valid_assistant_text_fields(monkeypatch, message, finish, error, observed):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        if request.headers["authorization"] == "Bearer allow-secret":
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(200, content=json.dumps({"choices": [{"message": {
            "role": "assistant", **message}, "finish_reason": finish}], "error": error}).encode())

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config(retention="all"))
    check = next(check for check in report.checks if check.id == "gateway_access")
    denied = check.metrics["gateway_access"]["cases"][1]
    assert denied["observed"] == observed
    assert report.overall == check.status == ("fail" if observed == "allow" else "inconclusive")
    assert "deny-secret" not in report.model_dump_json()


@pytest.mark.parametrize("message,finish,usage,observed", [
    ({"content": "OK", "tool_calls": None, "function_call": None}, "stop", {}, "allow"),
    ({"content": "OK", "tool_calls": []}, "stop", {}, "allow"),
    ({"content": "OK", "tool_calls": [{}]}, "stop", {}, "unverified"),
    ({"refusal": "deny-secret", "tool_calls": [{}]}, "stop", {}, "unverified"),
    ({"content": None, "tool_calls": [{}]}, "length", {"completion_tokens": 16,
        "completion_tokens_details": {"reasoning_tokens": 16}}, "unverified"),
    ({"content": "OK", "tool_calls": {}}, "stop", {}, "unverified"),
    ({"content": "OK", "function_call": {}}, "stop", {}, "unverified"),
    ({"content": "OK", "function_call": {"name": "unexpected", "arguments": "{}"}}, "stop", {}, "unverified"),
])
async def test_access_requires_tool_free_completions(monkeypatch, message, finish, usage, observed):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        if request.headers["authorization"] == "Bearer allow-secret":
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", **message}, "finish_reason": finish}], "usage": usage})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config(retention="all"))
    check = next(check for check in report.checks if check.id == "gateway_access")
    assert check.metrics["gateway_access"]["cases"][1]["observed"] == observed
    assert report.overall == check.status == ("fail" if observed == "allow" else "inconclusive")
    assert "deny-secret" not in report.model_dump_json()


@pytest.mark.parametrize("other_check,setup,uses_tool_options", [
    (None, None, False),
    ("gateway_attribution", None, False),
    ("gateway_attribution", "no_reader", False),
    ("gateway_attribution", "unsupported_reader", False),
    ("gateway_attribution", "potentially_active", True),
    ("gateway_policy", None, False),
    ("gateway_policy", "unsupported", False),
    ("serving", None, True),
])
async def test_access_baseline_matches_transmitted_request_options(monkeypatch, other_check, setup, uses_tool_options):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    inherited = {"tools": [{"type": "function", "function": {"name": "unexpected"}}],
                 "tool_choice": "required", "parallel_tool_calls": True,
                 "functions": [{"name": "legacy"}], "function_call": "auto"}
    payloads = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        authorization = request.headers.get("authorization")
        payloads.append((authorization, request.content))
        if authorization == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))

    async def run_with(options):
        payloads.clear()
        raw = config(request_options=options).model_dump()
        if other_check:
            raw["checks"].append(other_check)
        if other_check == "gateway_attribution" and setup is not None:
            raw["gateway_attribution"] = {"identities": ["allowed"], "samples": 1}
            if setup != "no_reader":
                raw["targets"][0]["gateway"] = {"url": "http://reader", "api_key_env": "ALLOW_KEY",
                    "profile": "litellm" if setup == "potentially_active" else "unsupported"}
                raw["max_requests"] = 8  # The mocked reader's 404 preflight sends no inference.
        if other_check == "gateway_policy" and setup is not None:
            raw["gateway_policy"] = {"capability": "unsupported", "affected_identity": "allowed", "users": [
                {"identity": alias, "requests": 1, "rpm": 1} for alias in ("allowed", "denied")]}
        report = await run_suite(RunConfig.model_validate(raw))
        assert report.overall == "pass"
        probes = {auth: body for auth, body in payloads if auth is not None}
        assert len(probes) == 2
        return report, probes

    baseline, original_probes = await run_with({**inherited, "temperature": 0.4})
    assert all(not inherited.keys() & json.loads(body).keys() for body in original_probes.values())
    if other_check == "serving":
        shared = [json.loads(body) for auth, body in payloads if auth is None]
        assert shared and all(inherited.keys() <= payload.keys() for payload in shared)
    else:
        assert len(payloads) == 2

    current, probes = await run_with({"temperature": 0.4})
    assert probes == original_probes
    comparison = compare_baseline(current, baseline).baseline["targets"][0]
    if uses_tool_options:
        assert comparison["status"] == "inconclusive"
        assert "Run setting request_options differs." in comparison["reasons"]
    else:
        assert comparison["status"] == "pass"
        assert [row["status"] for row in comparison["comparisons"] if row["check"] == "gateway_access"] == ["pass", "pass"]

    changed, probes = await run_with({"temperature": 0.7})
    assert probes != original_probes
    assert all(json.loads(body)["temperature"] == 0.7 for body in probes.values())
    comparison = compare_baseline(changed, baseline).baseline["targets"][0]
    assert comparison["status"] == "inconclusive"
    assert "Run setting request_options differs." in comparison["reasons"]


async def test_rotated_credential_does_not_observe_another_access_identity(monkeypatch):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    sent = []
    events = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        authorization = request.headers.get("authorization")
        sent.append(authorization)
        if authorization == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    def progress(event):
        events.append(event)
        if event["event"] == "request_started" and event.get("fixture_id") == "gateway-access-invalid":
            monkeypatch.setenv("DENY_KEY", "allow-secret")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config(retention="all"), progress=progress)
    check = next(check for check in report.checks if check.id == "gateway_access")
    allowed, denied = check.metrics["gateway_access"]["cases"]
    assert report.overall == check.status == "inconclusive"
    assert allowed["observed"] == "allow"
    assert denied["observed"] == "unverified" and denied["reason"] == "identity_changed"
    assert sent == ["Bearer allow-secret"]
    serialized = report.model_dump_json() + json.dumps(events)
    assert "allow-secret" not in serialized and "deny-secret" not in serialized


async def test_access_denial_rules_do_not_change_policy_evidence(monkeypatch, local_tokenizer):
    from tests.test_gateway_policy import exercise

    check, report, _ = await exercise(monkeypatch, full=True, window_state="isolated_fresh_single_counter")
    assert check.status == "pass"
    baseline = report.model_copy(deep=True)
    # A passing quota run saved on the immediate policy predecessor, before gateway access.
    baseline.manifest["gateway_policy_source_sha256"] = "f28f95e9274d75ca50244dffadaed6d9e83992fb6ef15c7f6d5577c9c21bec14"
    assert compare_baseline(report, baseline).baseline["status"] == "pass"


@pytest.mark.parametrize("completion,reasoning,reasoning_text,extra_usage,extra_details,observed", [
    (16, 16, "deny-secret", {}, {}, "allow"),
    (16, 16, None, {}, {}, "allow"),
    (16, 16, None, {"total_tokens": 24}, {"accepted_prediction_tokens": 0, "audio_tokens": None}, "allow"),
    (16, 16, None, {"total_tokens": 8}, {}, "unverified"),
    (16, 16, None, {"total_tokens": 24}, {"accepted_prediction_tokens": 2}, "unverified"),
    (16, None, "deny-secret", {}, {}, "unverified"),
    (16, 17, None, {}, {}, "unverified"),
    (15, 15, None, {}, {}, "unverified"),
    (17, 17, None, {}, {}, "unverified"),
])
async def test_reasoning_only_access_requires_metered_cap_exhaustion(
        monkeypatch, completion, reasoning, reasoning_text, extra_usage, extra_details, observed):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        if request.headers["authorization"] == "Bearer allow-secret":
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 8}})
        assert json.loads(request.content)["max_tokens"] == 16
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": None, "reasoning_content": reasoning_text},
            "finish_reason": "length"}], "usage": {"prompt_tokens": 8,
            "completion_tokens": completion, **extra_usage,
            "completion_tokens_details": {"reasoning_tokens": reasoning, **extra_details}}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config(retention="all"))
    check = next(check for check in report.checks if check.id == "gateway_access")
    allowed_case, denied_case = check.metrics["gateway_access"]["cases"]
    record = next(record for record in report.requests if record.id == denied_case["request_id"])
    assert allowed_case["observed"] == "allow" and allowed_case["reason"] == "valid_completion"
    assert report.overall == check.status == denied_case["status"] == ("fail" if observed == "allow" else "inconclusive")
    assert denied_case["observed"] == observed
    assert not record.valid and record.answer_chars == 0
    assert "deny-secret" not in report.model_dump_json()


@pytest.mark.parametrize("anonymous", [False, True])
async def test_distinct_custom_credentials_qualify_with_anonymous_or_custom_denial(monkeypatch, anonymous):
    monkeypatch.setenv("ALLOW_KEY", "Bearer allow-secret")
    monkeypatch.setenv("DENY_KEY", "Bearer deny-secret")
    values = config().model_dump()
    values["gateway_access"]["auth_header"] = "X-Proxy-Key"
    values["targets"][0]["test_identities"] = {
        "allowed": {"headers_env": {"X-Proxy-Key": "ALLOW_KEY"}},
        "denied": {} if anonymous else {"headers_env": {"x-proxy-key": "DENY_KEY"}},
    }
    sent = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        key = request.headers.get("x-proxy-key")
        sent.append(key)
        if key == "Bearer allow-secret":
            return httpx.Response(200, json={"choices": [{
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "length"}]})
        return httpx.Response(403, json={"error": {"type": "key_model_access_denied", "code": 403}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(RunConfig.model_validate(values))
    assert report.overall == "pass"
    assert sent == ["Bearer allow-secret", None if anonymous else "Bearer deny-secret"]


@pytest.mark.parametrize("auth_header,shared_gateway_key,expected_reason", [
    ("X-Proxy-Key", False, None),
    ("X-Proxy-Key", True, "identity_conflict"),
    (None, True, "auth_header_unverified"),
])
async def test_access_distinguishes_gateway_key_from_front_proxy_auth(
        monkeypatch, auth_header, shared_gateway_key, expected_reason):
    monkeypatch.setenv("ALLOW_KEY", "shared-front-secret" if not shared_gateway_key else "front-allow-secret")
    monkeypatch.setenv("DENY_KEY", "shared-front-secret" if not shared_gateway_key else "front-deny-secret")
    monkeypatch.setenv("ALLOW_GATEWAY", "Bearer gateway-allow-secret")
    monkeypatch.setenv("DENY_GATEWAY", "Bearer gateway-allow-secret" if shared_gateway_key else
                       "Bearer gateway-deny-secret")
    value = config().model_dump()
    value["gateway_access"]["auth_header"] = auth_header
    for alias, env in (("allowed", "ALLOW_GATEWAY"), ("denied", "DENY_GATEWAY")):
        value["targets"][0]["test_identities"][alias]["headers_env"] = {"X-Proxy-Key": env}
    sent = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        sent.append(request.headers["x-proxy-key"])
        if len(sent) == 1:
            return httpx.Response(200, json={"choices": [{
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(403, json={"error": {"type": "key_model_access_denied", "code": 403}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(RunConfig.model_validate(value))
    check = next(check for check in report.checks if check.id == "gateway_access")
    if expected_reason:
        assert not sent and not report.requests
        assert check.status == "inconclusive"
        assert [row["reason"] for row in check.metrics["gateway_access"]["cases"]] == [expected_reason] * 2
    else:
        assert sent == ["Bearer gateway-allow-secret", "Bearer gateway-deny-secret"]
        assert check.status == "pass"
        assert reproduction_export(report)["config"]["gateway_access"]["auth_header"] == auth_header


async def test_unsupported_access_settings_do_not_veto_serving_baseline(monkeypatch):
    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"choices": [{
            "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))

    async def run_with(profile, ref):
        raw = config(retention="all").model_dump()
        raw.update(checks=["serving", "gateway_access"], samples=8)
        raw["gateway_access"]["profile"] = profile
        raw["targets"][0]["test_identities"]["allowed"]["api_key_env"] = ref
        return await run_suite(RunConfig.model_validate(raw))

    current = await run_with("other-gateway", "CURRENT_UNUSED")
    baseline = await run_with("unknown-gateway", "BASELINE_UNUSED")
    assert current.overall == baseline.overall == "pass"
    assert all(record.identity is None for report in (current, baseline) for record in report.requests)
    assert reproduction_export(current)["config"]["gateway_access"]["profile"] == "other-gateway"
    comparison = compare_baseline(current, baseline).baseline["targets"][0]
    assert comparison["status"] == "pass"
    assert any(row["check"] == "serving" and row["status"] == "pass" for row in comparison["comparisons"])


async def test_access_baseline_scopes_settings_and_identity_references_to_its_cases(monkeypatch):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        if request.headers.get("authorization") == "Bearer deny-secret":
            return httpx.Response(403, json={"error": {"type": "key_model_access_denied"}})
        return httpx.Response(200, json={"choices": [{
            "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))

    async def run_with(unused_ref, max_output_tokens, profile="litellm"):
        raw = config(retention="all").model_dump()
        raw.update(checks=["serving", "gateway_access"], samples=8)
        raw["gateway_access"]["max_output_tokens"] = max_output_tokens
        raw["gateway_access"]["profile"] = profile
        raw["targets"][0]["test_identities"]["unused"] = {"api_key_env": unused_ref}
        report = await run_suite(RunConfig.model_validate(raw))
        assert report.overall == "pass"
        # Compare access settings independently of the mock transport's Serving speed.
        for record in report.requests:
            if "serving" in record.check_ids:
                record.elapsed_ms = 10
                record.first_output_ms = 5
        return report

    baseline = await run_with("BASELINE_UNUSED", 16)
    current = await run_with("CURRENT_UNUSED", 16)
    target = compare_baseline(current, baseline).baseline["targets"][0]
    assert target["status"] == "pass"
    assert [row["status"] for row in target["comparisons"] if row["check"] == "gateway_access"] == ["pass", "pass"]

    current = await run_with("CURRENT_UNUSED", 32)
    target = compare_baseline(current, baseline).baseline["targets"][0]
    assert target["status"] == "inconclusive"
    assert any(row["check"] == "serving" and row["status"] == "pass" for row in target["comparisons"])
    rows = [row for row in target["comparisons"] if row["check"] == "gateway_access"]
    assert len(rows) == 2
    assert all(row["status"] == "inconclusive" and "settings differ" in row["reason"] for row in rows)

    unsupported = await run_with("BASELINE_UNUSED", 32, profile="other-gateway")
    target = compare_baseline(current, unsupported).baseline["targets"][0]
    assert target["status"] == "inconclusive"
    assert "No comparable verdict metric for selected check(s): gateway_access" in target["summary"]
    assert any(row["check"] == "serving" and row["status"] == "pass" for row in target["comparisons"])
