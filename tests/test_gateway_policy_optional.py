"""Selecting optional policies without configuring them must not invent traffic."""
import asyncio
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
@pytest.mark.parametrize("policy", ["absent", "missing", "duplicate", "available"])
async def test_policy_reserves_probe_budget_only_when_credentials_can_dispatch(monkeypatch, policy):
    from tests.test_gateway_policy import configuration as configured_policy

    raw = configured_policy(users=[dict(identity="limited", requests=3, rpm=2),
                                   dict(identity="peer", requests=3, rpm=3)]).model_dump(mode="json")
    raw.update(checks=["access", "generation"], stream=True, samples=2, max_requests=4,
               max_memory_mb=0, retention="all", limits={"min_samples": 2, "min_output_tokens_per_second": 1})
    if policy == "absent":
        raw["gateway_policy"] = None
    else:
        raw["checks"].append("gateway_policy")
    raw["targets"][0]["api_key_env"] = "SHARED"
    monkeypatch.setenv("SHARED", "shared-secret")
    monkeypatch.delenv("LIMITED", raising=False)
    monkeypatch.delenv("PEER", raising=False)
    if policy in {"duplicate", "available"}:
        monkeypatch.setenv("LIMITED", "limited-secret")
        monkeypatch.setenv("PEER", "peer-secret" if policy == "available" else "limited-secret")
    config = RunConfig.model_validate(raw)
    sent = []

    class Answer(httpx.AsyncByteStream):
        def __init__(self, payload):
            self.payload = payload

        async def __aiter__(self):
            for part in ("Hello ", "there."):
                await asyncio.sleep(.015)
                choice = {"delta": {"content": part}}
                if self.payload.get("logprobs"):
                    choice["logprobs"] = {"content": [{"token": part}]}
                yield ("data: " + json.dumps({"choices": [choice]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            if self.payload.get("stream_options", {}).get("include_usage"):
                yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            yield b"data: [DONE]\n\n"

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        auth = request.headers["authorization"]
        sent.append(auth)
        if auth == "Bearer shared-secret":
            return httpx.Response(200, stream=Answer(json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config)
    checks = {check.id: check.status for check in report.checks}
    assert checks["access"] == "pass"
    assert report.observations["request_budget_used"] == len(sent) == 4
    if policy != "absent":
        planned = next(row for row in request_plan(config)["rows"] if row["id"] == "gateway_policy")
        assert planned["requests_per_target"] == 6
    if policy == "available":
        assert checks["generation"] == "inconclusive"
        assert sent[:3] == ["Bearer shared-secret"] * 3
        assert sent[3] in {"Bearer limited-secret", "Bearer peer-secret"}
        assert [record.scenario for record in report.requests] == ["initial", "generation", "generation", "gateway_policy"]
    else:
        assert checks["generation"] == "pass"
        assert sent == ["Bearer shared-secret"] * 4
        assert [record.scenario for record in report.requests] == ["initial", "performance_evidence_probe", "generation", "generation"]


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("policy", ["absent", "unconfigured", "missing", "available"])
async def test_recovery_reserves_only_usable_policy_work(monkeypatch, policy):
    from tests.test_gateway_policy import configuration as configured_policy

    raw = configured_policy(users=[dict(identity="limited", requests=4, rpm=2),
                                   dict(identity="peer", requests=3, rpm=3)]).model_dump(mode="json")
    raw.update(checks=["recovery"], stream=False, samples=2, concurrency=3, max_requests=10,
               sustained_seconds=2, max_memory_mb=0, retention="all", limits={"min_samples": 2})
    if policy != "absent":
        raw["checks"].append("gateway_policy")
    if policy in {"absent", "unconfigured"}:
        raw["gateway_policy"] = None
    raw["targets"][0]["api_key_env"] = "SHARED"
    monkeypatch.setenv("SHARED", "shared-secret")
    monkeypatch.delenv("LIMITED", raising=False)
    monkeypatch.delenv("PEER", raising=False)
    if policy == "available":
        monkeypatch.setenv("LIMITED", "limited-secret")
        monkeypatch.setenv("PEER", "peer-secret")
    config = RunConfig.model_validate(raw)
    sent = []

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        sent.append(request.headers["authorization"])
        prompt = json.loads(request.content)["messages"][0]["content"]
        prefix = "Copy exactly, with no extra text:\n"
        answer = prompt.removeprefix(prefix) if prompt.startswith(prefix) else "L8N2"
        await asyncio.sleep(.025)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": answer},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": max(1, len(answer.split()))}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(config)
    recovery = next(check for check in report.checks if check.id == "recovery")
    budget = recovery.metrics["sustained_budget"]
    assert report.observations["request_budget_used"] == len(sent) == 10
    if policy == "available":
        assert budget["allowance"] == 0 and budget["reserved_later"] == 9
        assert sent[:4] == ["Bearer shared-secret"] * 4
        assert len([record for record in report.requests if record.identity]) == 6
    else:
        assert recovery.status == "pass", recovery.summary
        assert budget["allowance"] == 6 and budget["reserved_later"] == 2
        assert sent == ["Bearer shared-secret"] * 10
        assert [record.scenario for record in report.requests] == ["recovery_baseline"] * 2 + ["sustained"] * 6 + ["recovery"] * 2


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("change", ["reference", "gateway"])
async def test_unavailable_policy_does_not_block_serving_baseline(monkeypatch, change):
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

    async def run(ref, gateway="unsupported"):
        raw = configured_policy(gateway=gateway).model_dump(mode="json")
        raw.update(checks=["serving", "gateway_policy"], stream=False, samples=8)
        raw["targets"][0].update(api_key_env="SHARED", test_identities={"limited": {"api_key_env": ref}})
        report = await run_suite(RunConfig.model_validate(raw))
        assert report.overall == "pass"
        # Isolate policy compatibility from the mock transport's wall-clock timing.
        for record in report.requests:
            if "serving" in record.check_ids:
                record.elapsed_ms = 10
                record.first_output_ms = 5
        return report

    baseline = await run("PREVIOUS_UNUSED")
    current = (await run("CURRENT_UNUSED") if change == "reference"
               else await run("PREVIOUS_UNUSED", "another-unsupported"))
    compared = compare_baseline(current, baseline).baseline["targets"][0]
    assert compared["status"] == "pass", compared["summary"]
    assert any(row["check"] == "serving" and row["status"] == "pass" for row in compared["comparisons"])


@pytest.mark.usefixtures("local_tokenizer")
async def test_required_policy_cannot_use_unavailable_baseline_for_coverage(monkeypatch):
    from giraffe.reporting import compare_baseline
    from tests.test_gateway_policy import configuration as configured_policy

    def handle(request):
        if request.method != "POST":
            return httpx.Response(404)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setenv("LIMITED", "limited-secret")
    monkeypatch.setenv("PEER", "peer-secret")

    async def run(gateway):
        raw = configured_policy(gateway=gateway).model_dump(mode="json")
        raw.update(checks=["serving", "gateway_policy"], stream=False, samples=8)
        return await run_suite(RunConfig.model_validate(raw))

    baseline, current = await run("unsupported"), await run("litellm")
    assert baseline.overall == "pass"
    assert next(check for check in current.checks if check.id == "gateway_policy").required
    compared = compare_baseline(current, baseline).baseline["targets"][0]
    assert compared["status"] == "inconclusive"
    assert any(row["check"] == "serving" and row["status"] == "pass" for row in compared["comparisons"])
    assert "No comparable verdict metric for selected check(s): gateway_policy" in compared["summary"]


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


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("case", [
    "unsupported", "missing_credentials", "supported_at_cap", "supported_partial",
    "shared_truncated", "standalone_truncated", "multi_target", "target_partial",
])
async def test_optional_policy_preserves_shared_traffic_at_run_limits(monkeypatch, case):
    from tests.test_gateway_policy import configuration as configured_policy

    raw = configured_policy(gateway="unsupported" if case == "unsupported" else "litellm",
                            duration_seconds=.8).model_dump(mode="json")
    raw.update(checks=["gateway_policy"] if case == "standalone_truncated" else ["serving", "gateway_policy"],
               stream=False, samples=2, max_memory_mb=0, max_duration_seconds=2, retention="all",
               max_requests={"supported_partial": 4, "shared_truncated": 2, "standalone_truncated": 1,
                             "multi_target": None, "target_partial": 8}.get(case, 3), limits={"min_samples": 2})
    raw["targets"][0]["api_key_env"] = "SHARED"
    monkeypatch.setenv("SHARED", "shared-secret")
    monkeypatch.delenv("LIMITED", raising=False)
    monkeypatch.delenv("PEER", raising=False)
    if case in {"multi_target", "target_partial"}:
        second = dict(raw["targets"][0], name="second", url="http://second")
        if case == "target_partial":
            monkeypatch.delenv("UNAVAILABLE_PEER", raising=False)
            second["test_identities"] = {**second["test_identities"], "peer": {"api_key_env": "UNAVAILABLE_PEER"}}
        raw["targets"].append(second)
    if case not in {"unsupported", "missing_credentials"}:
        monkeypatch.setenv("LIMITED", "limited-secret")
        monkeypatch.setenv("PEER", "peer-secret")
    config = RunConfig.model_validate(raw)
    sent = []
    stop = asyncio.Event()

    async def handle(request):
        if request.method != "POST":
            return httpx.Response(404)
        auth = request.headers["authorization"]
        sent.append((request.url.host, auth))
        if auth != "Bearer shared-secret" and case == "multi_target":
            stop.set()
            await asyncio.sleep(.8)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    report = await run_suite(config, stop_event=stop)
    serving = [check for check in report.checks if check.id == "serving"]
    policy = next(check for check in report.checks if check.id == "gateway_policy")
    if case == "standalone_truncated":
        assert len(sent) == 1 and sent[0][1] != "Bearer shared-secret"
        assert report.abort_reason == "maximum request budget reached"
        assert policy.status == "inconclusive"
        return
    if case == "shared_truncated":
        assert sent == [("proxy", "Bearer shared-secret")] * 2
        assert report.abort_reason == "maximum request budget reached"
        assert serving[0].status == "inconclusive"
        return
    assert all(check.status == "pass" for check in serving)
    if case in {"multi_target", "target_partial"}:
        assert sent[:6] == [("proxy", "Bearer shared-secret")] * 3 + [("second", "Bearer shared-secret")] * 3
        assert len(sent) > 6 and sent[6][1] != "Bearer shared-secret"
        timeline = request_plan(config)["time_timeline"]
        assert [(row["target"], row["id"]) for row in timeline] == [
            ("proxy", "initial"), ("proxy", "warm"), ("second", "initial"), ("second", "warm"),
            ("proxy", "gateway_policy"), ("second", "gateway_policy"),
        ]
        if case == "target_partial":
            assert report.abort_reason is None
            assert report.limits_reached == ["max_requests"]
            assert report.observations["request_budget_used"] == len(sent) == 7
            summary = policy.metrics["gateway_policy"]
            assert sum(user["dispatched"] for user in summary["users"]) == 1
            assert summary["qualification"] == "unverified"
            assert summary["stop_reason"] == "The target request limit interrupted the configured gateway policy probes."
            assert summary["stop_reason"] in policy.summary
    else:
        assert sent[:3] == [("proxy", "Bearer shared-secret")] * 3
        assert report.abort_reason is None
        assert policy.metrics["gateway_policy"]["qualification"] == (
            "unavailable" if case == "unsupported" else "unverified")
        assert len(sent) == (4 if case == "supported_partial" else 3)
        if case == "supported_partial":
            assert sent[3][1] in {"Bearer limited-secret", "Bearer peer-secret"}
            assert policy.metrics["gateway_policy"]["stop_reason"] == "maximum request budget reached"
        if case in {"unsupported", "supported_at_cap", "supported_partial"}:
            from giraffe.reporting import compare_baseline

            compared = compare_baseline(report, report.model_copy(deep=True)).baseline
            target = compared["targets"][0]
            assert any(row["check"] == "serving" and row["status"] == "pass" for row in target["comparisons"])
            if case == "unsupported":
                assert compared["status"] == "pass"
            else:
                if case == "supported_at_cap":
                    assert "no slots" in policy.summary
                assert report.limits_reached == ["max_requests"]
                assert all(row["status"] == "inconclusive" for row in target["comparisons"]
                           if row["check"] == "gateway_policy")
