import asyncio
import hashlib
import json
import time

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


@pytest.mark.parametrize("cap", [4, 6])
async def test_access_plan_and_request_budget_follow_execution_order(monkeypatch, cap):
    from giraffe.planning import request_plan

    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    raw = config(max_requests=cap, retention="all").model_dump()
    raw.update(checks=["prefix", "gateway_access"], prefix={"prefix_chars": 96, "repeats": 2, "history_turns": 2})
    cfg = RunConfig.model_validate(raw)
    plan = request_plan(cfg)
    assert [(row["id"], row["requests_per_target"]) for row in plan["rows"]] == [("prefix", 6), ("gateway_access", 2)]
    warning, = plan["request_warnings"]
    assert warning["cut_checks"] == (["gateway_access"] if cap == 6 else ["prefix", "gateway_access"])
    assert warning["actions"][0]["value"] == 8
    shared, access = plan["time_timeline"]
    assert shared["id"] == "prefix" and access["id"] == "gateway_access"
    assert access["field"] == "gateway_access.request_timeout_seconds"
    assert plan["budget_high_seconds"] >= 2 * cfg.gateway_access.request_timeout_seconds
    assert any(action["field"] == "gateway_access.cases" for action in warning["actions"])

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        if request.headers.get("authorization") == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "K7P4"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    scenarios = ["prefix_shared"] * 2 + ["prefix_independent"] * 2 + ["prefix_history"] * 2
    assert [record.scenario for record in report.requests] == scenarios[:cap]
    assert report.abort_reason == (None if cap == 6 else "maximum request budget reached")
    access = next(check for check in report.checks if check.id == "gateway_access")
    assert access.status == "inconclusive"
    assert all(case["reason"] == "request_budget_exhausted" for case in access.metrics["gateway_access"]["cases"])


@pytest.mark.parametrize("overlap", [False, True])
async def test_slow_unsupported_gateway_access_waits_for_all_shared_targets(monkeypatch, overlap):
    from giraffe.planning import request_plan

    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    cfg = config(retention="all", max_memory_mb=0)
    cfg.max_duration_seconds = .7
    cfg.checks = ["prefix", "gateway_access"]
    cfg.overlap_models = overlap
    raw = cfg.model_dump()
    raw["prefix"] = {"prefix_chars": 96, "repeats": 2, "history_turns": 2}
    cfg = RunConfig.model_validate(raw)
    cfg.targets.append(cfg.targets[0].model_copy(update={"name": "second", "url": "http://second/v1"}))
    cfg.gateway_access.request_timeout_seconds = 2
    calls = []

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        key = request.headers.get("authorization")
        calls.append((request.url.host, key))
        if key == "Bearer deny-secret":
            await asyncio.sleep(2)
        if key:
            return httpx.Response(501)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "K7P4"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    assert calls[:12].count(("proxy", None)) == calls[:12].count(("second", None)) == 6
    assert calls[12:14] and all(key is not None for _, key in calls[12:])
    access = next(check for check in report.checks if check.id == "gateway_access" and check.target == "proxy")
    allowed, denied = access.metrics["gateway_access"]["cases"]
    assert access.status == "inconclusive" and allowed["http_status"] == 501 and allowed["observed"] == "unverified"
    assert denied["observed"] == "unverified" and "max_duration_seconds" in report.limits_reached
    timeline = request_plan(cfg)["time_timeline"]
    assert [(item["target"], item["id"]) for item in timeline] == [
        ("proxy", "prefix"), ("second", "prefix"), ("proxy", "gateway_access"), ("second", "gateway_access")]


def test_access_plan_counts_only_configured_aliases_by_target(monkeypatch):
    from giraffe.planning import request_plan

    cfg = config()
    proxy, = cfg.targets
    cfg.targets += [proxy.model_copy(update={"name": "missing", "test_identities": {}}),
                    proxy.model_copy(update={"name": "partial", "test_identities": {"allowed": proxy.test_identities["allowed"]}})]
    monkeypatch.delenv("ALLOW_KEY", raising=False)
    monkeypatch.delenv("DENY_KEY", raising=False)
    plan = request_plan(cfg)
    counts = {"proxy": 2, "missing": 0, "partial": 1}
    assert plan["planned_total"] == 3 and plan["planned_target_counts"] == counts
    row, = plan["rows"]
    assert row["kind"] == "exact" and row["target_request_counts"] == counts
    assert {item["target"]: item["requests"] for item in plan["unknown_time_components"]} == {"proxy": 2, "partial": 1}


def _mixed_access_attribution_config(check):
    raw = config(retention="all", samples=2, request_timeout_seconds=2, max_memory_mb=0).model_dump()
    raw.update(stream=True, checks=["access", check, "gateway_access", "gateway_attribution"],
               gateway_attribution={"identities": ["allowed"], "samples": 1, "settle_seconds": .1})
    raw["targets"][0]["gateway"] = {"profile": "litellm", "url": "http://reader", "api_key_env": "READER_KEY"}
    raw["targets"].append({"name": "shared", "url": "http://shared/v1", "model": "test"})
    return raw


def test_access_plan_timeline_preserves_compatibility_for_target_with_spare_shared_quota():
    from giraffe.planning import request_plan

    raw = _mixed_access_attribution_config("generation")
    raw.update(max_requests=7, max_duration_seconds=5)
    plan = request_plan(RunConfig.model_validate(raw))
    assert plan["target_quotas"] == {"proxy": 4, "shared": 3}
    assert [item["target"] for item in plan["time_timeline"]
            if item["id"] == "performance_evidence_probe"] == ["proxy"]
    assert all(component["target"] == "proxy" for warning in plan["time_warnings"]
               for component in warning["components"] if component["id"] == "performance_evidence_probe")


@pytest.mark.parametrize("cap,targets,minimum", [(18, 2, 19), (19, 2, 19), (None, 1, 10)])
async def test_access_plan_attribution_limit_covers_target_scoped_compatibility(
        monkeypatch, local_tokenizer, cap, targets, minimum):
    from giraffe.planning import request_plan
    from giraffe.tool_checks import default_tool_fixture
    from tests.test_gateway_attribution import immediate_spend_polls, spend_page
    from tests.test_tool_conversations import fixture

    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    monkeypatch.setenv("READER_KEY", "reader-secret")
    raw = _mixed_access_attribution_config("tools")
    raw["targets"] = raw["targets"][:targets]
    raw.update(max_requests=cap, tool_checks={"fixtures": [default_tool_fixture()], "conversations": [fixture()],
                                            "modes": ["complete"]})
    cfg = RunConfig.model_validate(raw)
    immediate_spend_polls(monkeypatch, cfg)
    rows = []
    reader_preflights = []
    compatibility = {target.name: 0 for target in cfg.targets}

    def handler(request):
        if request.method != "POST":
            if request.url.host != "reader":
                return httpx.Response(404)
            if not rows:
                reader_preflights.append(request)
            return spend_page(request, rows)
        payload = json.loads(request.content)
        usage = {"prompt_tokens": 3, "completion_tokens": 5}
        if marker := request.headers.get("x-litellm-trace-id"):
            rows.append({"request_id": str(len(rows)), "session_id": marker, "status": "success",
                "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                "api_key": hashlib.sha256(b"allow-secret").hexdigest(), "total_tokens": 8, **usage})
        elif payload["max_tokens"] == 1:
            compatibility[request.url.host] += 1
            if compatibility[request.url.host] <= 2:
                return httpx.Response(400, json={"error": {"param": "logprobs"}})
        elif request.headers.get("authorization") == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        if payload["stream"]:
            return httpx.Response(200, text=(
                'data: {"choices":[{"delta":{"content":"OK"},"logprobs":{"content":[{"token":"OK"}]}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
                'data: [DONE]\n\n'))
        messages = payload["messages"]
        message, finish = {"role": "assistant", "content": "OK"}, "stop"
        if messages[-1]["role"] == "tool":
            message["content"] = messages[-1]["content"]
        elif payload.get("tools"):
            city = "Warsaw" if messages[0]["content"].startswith("Check Warsaw") else "Delhi"
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {
                "name": payload["tools"][0]["function"]["name"], "arguments": json.dumps({"city": city})}}]
            finish = "tool_calls"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}], "usage": usage})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    plan = request_plan(cfg)
    assert plan["attribution_request_limit"] == minimum
    warnings = [warning for warning in plan["request_warnings"] if warning["kind"] == "incomplete_coverage"]
    if cap == 18:
        warning, = warnings
        assert warning["cut_checks"] == ["gateway_attribution"] and warning["actions"][0]["value"] == minimum
    else:
        assert not warnings
    report = await run_suite(cfg)
    assert compatibility["proxy"] == 3
    assert bool(reader_preflights) is (targets > 1)
    checks = {check.id: check for check in report.checks if check.target == "proxy"}
    assert checks["gateway_access"].status == checks["tools"].status == "pass"
    attribution = checks["gateway_attribution"]
    if cap == 18:
        assert attribution.status == "inconclusive" and attribution.metrics["reason_code"] == "budget_limited"
        assert attribution.remediation["minimum"] == minimum
    else:
        assert attribution.status == "pass" and attribution.metrics["matched_requests"] == 1
        scenarios = [record.scenario for record in report.requests if record.target == "proxy"]
        assert scenarios[-3:] == ["gateway_access", "gateway_access", "gateway_attribution"]


def _mock_recovery_gateway(monkeypatch):
    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        auth = request.headers.get("authorization")
        if auth and int(auth[-1]) % 2:
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        prompt = json.loads(request.content)["messages"][0]["content"]
        if auth:
            answer = "OK"
        elif prompt.startswith("Notes:"):
            answer = "L8N2"
        else:
            answer = prompt.partition("\n")[2] or "OK"
        await asyncio.sleep(.025)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": answer}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


async def test_recovery_reserves_access_cases_on_the_target_that_can_send_them(monkeypatch, local_tokenizer):
    from giraffe.planning import request_plan

    raw = config(max_requests=16, retention="all").model_dump()
    raw.update(checks=["recovery", "gateway_access"], test_options={"recovery": {
        "samples": 1, "concurrency": 1, "sustained_seconds": .5}}, limits={"min_samples": 2})
    ready = raw["targets"][0]
    ready.update(name="ready", test_identities={f"key{i}": {"api_key_env": f"RECOVERY_KEY_{i}"} for i in range(6)})
    raw["targets"].append({"name": "empty", "url": "http://empty/v1", "model": "test"})
    raw["gateway_access"]["cases"] = [
        {"name": f"case{i}", "identity": f"key{i}", "expected": "deny" if i % 2 else "allow"} for i in range(6)]
    for i in range(6):
        monkeypatch.setenv(f"RECOVERY_KEY_{i}", f"testkey-{i}")
    cfg = RunConfig.model_validate(raw)
    _mock_recovery_gateway(monkeypatch)
    report = await run_suite(cfg)
    checks = {(check.target, check.id): check for check in report.checks}
    assert checks["ready", "gateway_access"].status == "pass"
    assert checks["empty", "gateway_access"].status == "inconclusive"
    assert [record.scenario for record in report.requests if record.target == "ready"] == (
        ["recovery_baseline", "recovery"] + ["gateway_access"] * 6)
    assert [record.scenario for record in report.requests if record.target == "empty"] == (
        ["recovery_prime", "recovery_baseline"] + ["sustained"] * 5 + ["recovery"])
    targets = report.observations["targets"]
    assert targets["ready"]["sustained_budget"]["reserved_later"] == 7
    assert targets["ready"]["sustained_budget"]["allowance"] == 0
    assert targets["empty"]["sustained_budget"]["allowance"] == 5
    plan = request_plan(cfg)
    prime = next(row for row in plan["rows"] if row["id"] == "recovery_prime")
    assert prime["target_request_counts"] == {"ready": 0, "empty": 1}
    assert not any(warning["kind"] == "incomplete_coverage" for warning in plan["request_warnings"])


@pytest.mark.parametrize("configured", [False, True])
async def test_unavailable_access_does_not_halve_recovery_budget(monkeypatch, local_tokenizer, configured):
    cfg = config(max_requests=6, retention="all")
    cfg.checks = ["recovery", "gateway_access"]
    cfg = RunConfig.model_validate({**cfg.model_dump(), "test_options": {"recovery": {
        "samples": 1, "concurrency": 1, "sustained_seconds": .5}}, "limits": {"min_samples": 3}})
    if not configured:
        cfg.gateway_access = None
    monkeypatch.delenv("ALLOW_KEY", raising=False)
    monkeypatch.delenv("DENY_KEY", raising=False)
    _mock_recovery_gateway(monkeypatch)
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["recovery"].status == "pass"
    assert checks["gateway_access"].status == "inconclusive"
    assert not any(record.identity for record in report.requests)
    budget = report.observations["targets"]["proxy"]["sustained_budget"]
    assert budget["reserved_later"] == 1 and budget["allowance"] == 3


async def test_unavailable_access_preserves_the_cap_for_other_adaptive_work(monkeypatch, local_tokenizer):
    from giraffe.planning import request_plan

    raw = config(max_requests=6, retention="all").model_dump()
    raw.update(checks=["recovery", "gateway_access", "sessions"], gateway_access=None,
               sessions={"profile": "performance", "modes": ["live"], "workloads": [
                   {"name": "chat", "turns": ["First", "Second"]}]},
               test_options={"recovery": {"samples": 1, "concurrency": 1, "sustained_seconds": .5}},
               limits={"min_samples": 3})
    cfg = RunConfig.model_validate(raw)
    session_row = next(row for row in request_plan(cfg)["rows"] if row["id"] == "sessions")
    assert session_row["requests_per_target"] is None
    _mock_recovery_gateway(monkeypatch)
    report = await run_suite(cfg)
    budget = report.observations["targets"]["proxy"]["sustained_budget"]
    assert budget["reserved_later"] == 1 and budget["allowance"] == 2
    assert any(record.scenario.startswith("session_") for record in report.requests)


@pytest.mark.parametrize("availability", [
    "missing_aliases", "missing_env", "conflicting_keys", "partial_env", "available", "unsupported_response",
])
async def test_access_probes_leave_shared_token_evidence_budget(monkeypatch, local_tokenizer, availability):
    from giraffe.planning import request_plan

    both_keys = availability in {"available", "unsupported_response"}
    sends_access = both_keys or availability == "partial_env"
    cfg = config(max_requests=5 if sends_access else 4, retention="all")
    cfg.checks = ["access", "generation", "gateway_access"]
    cfg.stream = True
    cfg.samples = cfg.limits.min_samples = 2
    cfg.limits.min_output_tokens_per_second = 1
    monkeypatch.delenv("ALLOW_KEY", raising=False)
    monkeypatch.delenv("DENY_KEY", raising=False)
    if availability == "missing_aliases":
        cfg.targets[0].test_identities = {}
    elif availability == "conflicting_keys":
        monkeypatch.setenv("ALLOW_KEY", "same-secret")
        monkeypatch.setenv("DENY_KEY", "same-secret")
    elif sends_access:
        monkeypatch.setenv("ALLOW_KEY", "allow-secret")
        if both_keys:
            monkeypatch.setenv("DENY_KEY", "deny-secret")

    class Answer(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            for part in ("Hello ", "there."):
                await asyncio.sleep(.015)
                choice = {"delta": {"content": part}}
                if self.body.get("logprobs"):
                    choice["logprobs"] = {"content": [{"token": part}]}
                yield ("data: " + json.dumps({"choices": [choice]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            if self.body.get("stream_options", {}).get("include_usage"):
                yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            yield b'data: [DONE]\n\n'

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        key = request.headers.get("authorization")
        if key == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        if key == "Bearer allow-secret":
            if availability == "unsupported_response":
                return httpx.Response(501)
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(200, stream=Answer(json.loads(request.content)))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    shared = ["initial", "performance_evidence_probe", "generation", "generation"]
    if both_keys:
        baseline = await run_suite(cfg.model_copy(update={"checks": ["access", "generation"]}))
        assert baseline.overall == "pass" and [record.scenario for record in baseline.requests] == shared
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == checks["generation"].status == "pass"
    assert [record.scenario for record in report.requests] == shared + (["gateway_access"] if sends_access else [])
    rows = checks["gateway_access"].metrics["gateway_access"]["cases"]
    observed = "allow" if availability in {"available", "partial_env"} else "unverified"
    assert [row["observed"] for row in rows] == [observed, "unverified"]
    if both_keys:
        assert rows[0]["http_status"] == (501 if availability == "unsupported_response" else 200)
        assert rows[1]["reason"] == "request_budget_exhausted" and rows[1]["request_id"] is None
        assert report.abort_reason is None and report.observations["request_limit_scopes"] == ["gateway"]
        plan = request_plan(cfg)
        warning, = [item for item in plan["request_warnings"] if item["kind"] == "incomplete_coverage"]
        assert warning["cut_checks"] == ["gateway_access"] and warning["actions"][0]["value"] == 8
        assert any(item["id"] == "performance_evidence_probe" for item in plan["time_timeline"])
    else:
        assert rows[1]["reason"] == ("identity_conflict" if availability == "conflicting_keys" else "identity_unavailable")


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


@pytest.mark.parametrize("ambiguous,status,body", [
    ("allowed", 200, b'{"choices":[{"message":{"role":"user","role":"assistant","content":"OK"},"finish_reason":"stop"}]}'),
    ("denied", 401, b'{"error":{"type":"upstream_error","type":"token_not_found_in_db"}}'),
    ("denied", 403, b'{"error":{"type":"upstream_error"},"error":{"type":"key_model_access_denied"}}'),
])
async def test_duplicate_response_keys_do_not_qualify_gateway_access(monkeypatch, ambiguous, status, body):
    monkeypatch.setenv("ALLOW_KEY", "allowed")
    monkeypatch.setenv("DENY_KEY", "denied")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        auth = request.headers.get("authorization")
        if auth == "Bearer " + ambiguous:
            return httpx.Response(status, content=body)
        if auth == "Bearer denied":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        # Ordinary completion handling remains permissive about repeated keys.
        if not auth:
            return httpx.Response(200, content=b'{"choices":[{"message":{"role":"assistant","content":null,'
                                              b'"content":"OK"},"finish_reason":"stop"}]}')
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config()
    cfg.checks = ["access", "gateway_access"]
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == "pass"
    assert checks["gateway_access"].status == "inconclusive"
    rows = checks["gateway_access"].metrics["gateway_access"]["cases"]
    assert [row["observed"] for row in rows] == (["unverified", "deny"] if ambiguous == "allowed" else ["allow", "unverified"])


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


async def test_access_probes_only_the_declared_model(monkeypatch):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    requests = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        payload = json.loads(request.content)
        authorization = request.headers["authorization"]
        requests.append((authorization, payload))
        # Both keys can use the declared model. Only the first key can use the fallback.
        grants = {"test", "backup"} if authorization == "Bearer allow-secret" else {"test"}
        if not {payload["model"], *payload.get("fallbacks", [])} <= grants:
            return httpx.Response(403, json={"error": {"type": "key_model_access_denied", "param": "model"}})
        return httpx.Response(200, json={"model": "upstream-model", "choices": [{
            "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config(request_options={"fallbacks": ["backup"], "disable_fallbacks": False, "temperature": 0.3})
    cfg.gateway_access.max_output_tokens = 16
    report = await run_suite(cfg)
    cases = next(check for check in report.checks if check.id == "gateway_access").metrics["gateway_access"]["cases"]
    assert report.overall == "fail"
    assert [case["observed"] for case in cases] == ["allow", "allow"]
    payload = {"model": "test", "messages": [{"role": "user", "content": "Reply with OK."}],
               "stream": False, "max_tokens": 16, "n": 1, "disable_fallbacks": True}
    assert requests == [("Bearer allow-secret", payload), ("Bearer deny-secret", payload)]


@pytest.mark.parametrize("status,count,group,reason", [
    (200, "0", None, "valid_completion"),
    (200, "0", "test", "valid_completion"),
    (200, "1", None, "fallback_reported"),
    (200, "0, 1", None, "fallback_reported"),
    (403, "1", None, "fallback_reported"),
    (200, "0", "internal-backup", "model_group_mismatch"),
    (403, None, "internal-backup", "model_group_mismatch"),
])
async def test_access_requires_evidence_for_the_declared_model_group(monkeypatch, status, count, group, reason):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        if request.headers["authorization"] == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        if status == 403:
            body = {"error": {"type": "key_model_access_denied"}}
        else:
            body = {"model": "upstream-model", "choices": [{
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        headers = {key: value for key, value in {
            "x-litellm-attempted-fallbacks": count, "x-litellm-model-group": group}.items() if value is not None}
        return httpx.Response(status, headers=headers, json=body)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config(retention="all"))
    cases = next(check for check in report.checks if check.id == "gateway_access").metrics["gateway_access"]["cases"]
    observed = "allow" if reason == "valid_completion" else "unverified"
    assert cases[0]["observed"] == observed
    assert cases[0]["reason"] == reason
    assert cases[1]["observed"] == "deny"
    assert report.overall == ("pass" if observed == "allow" else "inconclusive")
    assert "internal-backup" not in report.model_dump_json()


@pytest.mark.parametrize("other_check,setup,uses_shared_options", [
    (None, None, False),
    ("gateway_attribution", None, False),
    ("gateway_attribution", "no_reader", False),
    ("gateway_attribution", "unsupported_reader", False),
    ("gateway_attribution", "potentially_active", True),
    ("gateway_policy", None, False),
    ("gateway_policy", "unsupported", False),
    ("serving", None, True),
    ("serving", "unexpected_access", True),
])
async def test_access_baseline_matches_transmitted_request_options(monkeypatch, other_check, setup, uses_shared_options):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    inherited = {"tools": [{"type": "function", "function": {"name": "unexpected"}}],
                 "tool_choice": "required", "parallel_tool_calls": True,
                 "functions": [{"name": "legacy"}], "function_call": "auto",
                 "fallbacks": ["backup"], "reasoning_effort": "medium", "temperature": 0.4}
    payloads = []
    unexpected_access = False

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        authorization = request.headers.get("authorization")
        payloads.append((authorization, request.content))
        if authorization == "Bearer deny-secret" and not unexpected_access:
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))

    async def run_with(options, max_output_tokens):
        payloads.clear()
        raw = config(request_options=options, max_output_tokens=max_output_tokens).model_dump()
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
        assert report.overall == ("fail" if unexpected_access else "pass")
        probes = {auth: body for auth, body in payloads if auth is not None}
        assert len(probes) == 2
        return report, probes

    baseline, original_probes = await run_with(inherited, 64)
    assert all(not inherited.keys() & json.loads(body).keys() for body in original_probes.values())
    if other_check == "serving":
        shared = [json.loads(body) for auth, body in payloads if auth is None]
        assert shared and all(all(payload.get(key) == value for key, value in inherited.items()) for payload in shared)
    else:
        assert len(payloads) == 2

    unexpected_access = setup == "unexpected_access"
    current, probes = await run_with({"temperature": 0.7}, 128)
    assert probes == original_probes
    comparison = compare_baseline(current, baseline).baseline["targets"][0]
    if uses_shared_options:
        assert comparison["status"] == ("fail" if unexpected_access else "inconclusive")
        assert not any(row["check"] == "serving" for row in comparison["comparisons"])
        assert "Run setting request_options differs." in comparison["reasons"]
    else:
        assert comparison["status"] == "pass"
        assert not comparison["reasons"]
    access_statuses = [row["status"] for row in comparison["comparisons"] if row["check"] == "gateway_access"]
    assert access_statuses == ["pass", "fail" if unexpected_access else "pass"]


@pytest.mark.parametrize("limit,case_reason,abort_reason", [
    ("stop", "run_stopped", "cancelled by user"),
    ("deadline", "run_deadline", "maximum run duration reached"),
    ("budget", "request_budget_exhausted", "maximum request budget reached"),
])
async def test_access_only_counts_requests_admitted_to_transport(monkeypatch, limit, case_reason, abort_reason):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    cfg = config(retention="all", max_requests=1 if limit == "budget" else 2)
    if limit == "deadline":
        cfg.max_duration_seconds = .5
    stop = asyncio.Event()
    sent, events = [], []
    interrupted = False
    original, build = httpx.AsyncClient, httpx.AsyncClient.build_request

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        sent.append(request.headers["authorization"])
        if sent[-1] == "Bearer deny-secret":
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    def build_request(self, method, *args, **kwargs):
        nonlocal interrupted
        request = build(self, method, *args, **kwargs)
        if method == "POST" and len(sent) == 1:
            interrupted = True
            if limit == "stop":
                stop.set()
            elif limit == "deadline":
                # Let the run's wall-clock deadline pass without yielding to its timer.
                time.sleep(.55)
        return request

    monkeypatch.setattr(original, "build_request", build_request)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg, stop_event=stop, progress=events.append)
    check = next(check for check in report.checks if check.id == "gateway_access")
    allowed, denied = check.metrics["gateway_access"]["cases"]
    assert interrupted is (limit != "budget")
    assert sent == ["Bearer allow-secret"]
    assert report.overall == check.status == "inconclusive" and report.abort_reason == abort_reason
    assert allowed["observed"] == "allow" and denied["observed"] == "unverified"
    assert denied["reason"] == case_reason and denied["request_id"] is None
    assert check.evidence_ids == [allowed["request_id"]] == [record.id for record in report.requests]
    assert report.observations["request_budget_used"] == events[-1]["attempted"] == 1
    assert [event["fixture_id"] for event in events if event["event"] == "request_started"] == ["gateway-access-valid"]


@pytest.mark.parametrize("cap", [3, 4])
@pytest.mark.parametrize("baseline_capped", [False, True])
async def test_access_quota_cutoff_preserves_completed_serving_comparison(monkeypatch, cap, baseline_capped):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    calls = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        calls.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config(retention="all", max_requests=cap, samples=2, limits={"min_samples": 2})
    cfg.checks = ["serving", "gateway_access"]
    baseline = await run_suite(cfg.model_copy(update={
        "checks": ["serving"], "max_requests": cap if baseline_capped else None}))
    current = await run_suite(cfg)
    assert baseline.overall == "pass" and current.overall == "inconclusive"
    assert calls == [None] * 6 + ["Bearer allow-secret"] * (cap - 3)
    assert current.observations["request_budget_used"] == cap
    assert current.abort_reason is None and "max_requests" in current.limits_reached
    checks = {check.id: check for check in current.checks}
    assert checks["serving"].status == "pass"
    assert checks["gateway_access"].required and checks["gateway_access"].status == "inconclusive"
    allowed, denied = checks["gateway_access"].metrics["gateway_access"]["cases"]
    budget_exhausted = ("unverified", "request_budget_exhausted", None)
    assert (denied["observed"], denied["reason"], denied["request_id"]) == budget_exhausted
    if cap == 3:
        assert (allowed["observed"], allowed["reason"], allowed["request_id"]) == budget_exhausted
    else:
        assert allowed["observed"] == "allow" and allowed["request_id"]
    for report in (baseline, current):
        for record in report.requests:
            record.elapsed_ms = 10
            record.first_output_ms = 5
    current = RunReport.model_validate_json(current.model_dump_json())
    baseline = RunReport.model_validate_json(baseline.model_dump_json())
    comparison = compare_baseline(current, baseline).baseline["targets"][0]
    assert not comparison["reasons"]
    assert any(row["check"] == "serving" and row["status"] == "pass" for row in comparison["comparisons"])


@pytest.mark.parametrize("replacement,reason", [("allow-secret", "identity_changed"), (None, "identity_unavailable")])
async def test_rotated_credential_does_not_observe_another_access_identity(monkeypatch, replacement, reason):
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
        if event["event"] == "request_completed" and event["request"]["fixture_id"] == "gateway-access-valid":
            if replacement is None:
                monkeypatch.delenv("DENY_KEY")
            else:
                monkeypatch.setenv("DENY_KEY", replacement)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config(retention="all"), progress=progress)
    check = next(check for check in report.checks if check.id == "gateway_access")
    allowed, denied = check.metrics["gateway_access"]["cases"]
    assert report.overall == check.status == "inconclusive"
    assert allowed["observed"] == "allow"
    assert denied["observed"] == "unverified" and denied["reason"] == reason and denied["request_id"] is None
    assert sent == ["Bearer allow-secret"]
    assert check.evidence_ids == [allowed["request_id"]] == [record.id for record in report.requests]
    assert report.observations["request_budget_used"] == events[-1]["attempted"] == 1
    assert [event["fixture_id"] for event in events if event["event"] == "request_started"] == ["gateway-access-valid"]
    serialized = report.model_dump_json() + json.dumps(events)
    assert "allow-secret" not in serialized and "deny-secret" not in serialized


async def test_access_denial_rules_do_not_change_policy_evidence(monkeypatch, local_tokenizer):
    from tests.test_gateway_policy import exercise

    check, report, _ = await exercise(monkeypatch, full=True, window_state="isolated_fresh_single_counter")
    assert check.status == "pass"
    baseline = report.model_copy(deep=True)
    # A passing quota run saved on the immediate policy predecessor, before gateway access.
    baseline.manifest["gateway_policy_source_sha256"] = "0c4e52aacf84d195615ccce1ed1d4cb2080f3bc62c375b8ea92c5fb6aedb0a2a"
    assert compare_baseline(report, baseline).baseline["status"] == "pass"


@pytest.mark.parametrize("usage,message,observed", [
    pytest.param({}, {"reasoning_content": "deny-secret"}, "allow", id="reasoning-text"),
    pytest.param({}, {}, "allow", id="sparse-usage"),
    pytest.param({"prompt_tokens": 8, "total_tokens": 24, "prompt_tokens_details": {
        "cached_tokens": 8, "text_tokens": 4, "audio_tokens": 4}, "completion_tokens_details": {
        "reasoning_tokens": 16, "accepted_prediction_tokens": 0, "audio_tokens": None,
        "image_tokens": 0, "video_tokens": None}},
        {"audio": None, "images": [], "refusal": ""}, "allow", id="coherent-complete-usage"),
    pytest.param({"prompt_tokens_details": None}, {}, "allow", id="null-optional-prompt-details"),
    pytest.param({"total_tokens": 24, "prompt_tokens_details": {"cached_tokens": 8}},
        {}, "allow", id="implied-prompt-count"),
    pytest.param({"prompt_tokens": 8, "total_tokens": 8}, {}, "unverified", id="inconsistent-total"),
    pytest.param({"prompt_tokens": 8, "total_tokens": 24, "completion_tokens_details": {
        "reasoning_tokens": 16, "accepted_prediction_tokens": 2}}, {}, "unverified", id="competing-completion-count"),
    pytest.param({"completion_tokens_details": {"reasoning_tokens": None}},
        {"reasoning_content": "deny-secret"}, "unverified", id="no-reasoning-count"),
    pytest.param({"completion_tokens_details": {"reasoning_tokens": 17}}, {}, "unverified", id="reasoning-over-cap"),
    pytest.param({"completion_tokens": 15, "completion_tokens_details": {"reasoning_tokens": 15}},
        {}, "unverified", id="output-below-cap"),
    pytest.param({"completion_tokens": 17, "completion_tokens_details": {"reasoning_tokens": 17}},
        {}, "unverified", id="output-over-cap"),
    pytest.param({"prompt_tokens": "invalid"}, {}, "unverified", id="malformed-prompt-count"),
    pytest.param({"prompt_tokens": None}, {}, "unverified", id="null-prompt-count"),
    pytest.param({"prompt_tokens": 8, "total_tokens": 24, "prompt_tokens_details": {"cached_tokens": 1000}},
        {}, "unverified", id="cached-over-prompt-count"),
    pytest.param({"total_tokens": 24, "prompt_tokens_details": {"cached_tokens": 9}},
        {}, "unverified", id="cached-over-implied-prompt-count"),
    pytest.param({"prompt_tokens": 8, "total_tokens": 24, "prompt_tokens_details": {"text_tokens": 8, "audio_tokens": 8}},
        {}, "unverified", id="prompt-modalities-exceed-total"),
    pytest.param({"total_tokens": 24, "prompt_tokens_details": {"image_tokens": 7, "video_tokens": 2}},
        {}, "unverified", id="prompt-modalities-exceed-implied-total"),
    pytest.param({"prompt_tokens_details": {"text_tokens": False}}, {}, "unverified", id="malformed-prompt-detail"),
    pytest.param({"prompt_tokens_details": []}, {}, "unverified", id="malformed-prompt-breakdown"),
    pytest.param({}, {"refusal": "deny-secret"}, "unverified", id="unfinished-refusal"),
    pytest.param({}, {"audio": {"id": "audio", "data": "AA==", "transcript": "deny-secret", "expires_at": 42}},
        "unverified", id="audio-output"),
    pytest.param({"completion_tokens_details": {"reasoning_tokens": 16, "image_tokens": 1}},
        {}, "unverified", id="metered-image-output"),
    pytest.param({"completion_tokens_details": {"reasoning_tokens": 16, "video_tokens": 1}},
        {}, "unverified", id="metered-video-output"),
    pytest.param({}, {"images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
        "unverified", id="image-output"),
])
async def test_reasoning_only_access_requires_metered_cap_exhaustion(monkeypatch, usage, message, observed):
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
            "role": "assistant", "content": None, **message}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 16, "completion_tokens_details": {"reasoning_tokens": 16}, **usage}})

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


@pytest.mark.parametrize("anonymous,message,param,observed", [
    (True, "Authentication Error, No api key passed in.", None, "deny"),
    (False, "Authentication Error, No api key passed in.", None, "unverified"),
    (True, "Authentication Error, No api key passed in.", "authorization", "unverified"),
    (True, "Authentication Error, authentication backend connection refused", None, "unverified"),
])
async def test_only_explicit_anonymous_access_qualifies_litellm_missing_key(monkeypatch, anonymous, message, param, observed):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")
    monkeypatch.setenv("SHARED_KEY", "shared-secret")
    raw = config().model_dump()
    raw["targets"][0]["api_key_env"] = "SHARED_KEY"
    if anonymous:
        raw["targets"][0]["test_identities"]["denied"] = {}
    credentials = []

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        key = request.headers.get("authorization")
        credentials.append(key)
        if key == "Bearer allow-secret":
            return httpx.Response(200, json={"choices": [{
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(401, json={"error": {
            "type": "auth_error", "message": message, "param": param, "code": "401"}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(RunConfig.model_validate(raw))
    check = next(check for check in report.checks if check.id == "gateway_access")
    denied = check.metrics["gateway_access"]["cases"][1]
    assert credentials == ["Bearer allow-secret", None if anonymous else "Bearer deny-secret"]
    assert denied["observed"] == observed
    assert report.overall == ("pass" if observed == "deny" else "inconclusive")


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


async def test_oversized_access_usage_preserves_shared_report(monkeypatch):
    monkeypatch.setenv("ALLOW_KEY", "allow-secret")
    monkeypatch.setenv("DENY_KEY", "deny-secret")

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        authorization = request.headers.get("authorization")
        if authorization == "Bearer deny-secret":
            return httpx.Response(403, json={"error": {"type": "key_model_access_denied", "param": "model"}})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 10 ** 400 if authorization else 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config(retention="all", samples=2)
    cfg.checks = ["serving", "gateway_access"]
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["serving"].status == "pass"
    access = checks["gateway_access"]
    assert access.status == "inconclusive"
    assert [case["observed"] for case in access.metrics["gateway_access"]["cases"]] == ["unverified", "deny"]
    rejected = next(record for record in report.requests if record.identity == "allowed")
    assert rejected.status == "failed" and rejected.output_tokens is None
