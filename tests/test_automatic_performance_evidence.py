"""Automatic capability negotiation is setup traffic, never a timed retry."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from giraffe import telemetry
from giraffe.client import LLMClient
from giraffe.models import RequestSpec, RunConfig, Target


def completion(*, tokens=True, usage=True):
    choice = {"delta": {"content": "1"}, "finish_reason": "length"}
    if tokens:
        choice["logprobs"] = {"content": [{"token": "1", "logprob": -0.1}]}
    body = {"choices": [choice]}
    if usage:
        body["usage"] = {"completion_tokens": 1, "prompt_tokens": 8}
    return httpx.Response(200, text=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n",
                          headers={"content-type": "text/event-stream"})


def mock_http(monkeypatch, handler):
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(**kw, transport=httpx.MockTransport(handler)))


def workload():
    return RequestSpec(fixture_id="answer", scenario="load", check_ids=["load"],
                       messages=[{"role": "user", "content": "Reply with 1."}])


async def test_proven_streamed_fields_are_requested_on_measured_requests(monkeypatch):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return completion()
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        probe = client.token_evidence_probe()
        assert probe.max_tokens == 1 and probe.request_timeout_seconds == client.config.request_timeout_seconds
        assert probe.check_ids == [] and probe.workload["role"] == "setup"
        evidence = client.observe_token_evidence_probe(await client.execute(probe))
        assert evidence["logprobs"] == evidence["usage"] == "supported"
        assert client.token_evidence_probe() is None
        result = await client.execute(workload())
    assert result.valid and len(calls) == 2
    assert calls[1]["logprobs"] is True and calls[1]["top_logprobs"] == 0
    assert calls[1]["stream_options"] == {"include_usage": True}
    assert result.workload["token_evidence"]["logprobs"] == "supported"


async def test_rejected_logprobs_are_removed_in_setup_and_never_retried_in_workload(monkeypatch):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body.get("logprobs"):
            return httpx.Response(400, json={"error": {"param": "logprobs", "message": "SECRET"}})
        return completion(tokens=False)
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        while (probe := client.token_evidence_probe()) is not None:
            result = await client.execute(probe)
            evidence = client.observe_token_evidence_probe(result)
            assert "SECRET" not in json.dumps(result.model_dump())
        assert evidence["logprobs"] == "rejected" and evidence["usage"] == "supported"
        assert (await client.execute(workload())).valid
    assert len(calls) == 3
    assert all("logprobs" not in body for body in calls[1:])
    assert all(body["stream_options"]["include_usage"] for body in calls)


async def test_rejected_usage_option_is_not_sent_to_measured_requests(monkeypatch):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if "stream_options" in body:
            return httpx.Response(422, json={"detail": [{"loc": ["body", "stream_options"], "type": "extra_forbidden"}]})
        return completion(usage=False)
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        while (probe := client.token_evidence_probe()) is not None:
            evidence = client.observe_token_evidence_probe(await client.execute(probe))
        assert evidence["usage"] == "rejected" and evidence["logprobs"] == "supported"
        assert (await client.execute(workload())).valid
    assert len(calls) == 3 and "stream_options" not in calls[-1]


async def test_unclassified_probe_failure_does_not_retry_or_enable_optional_fields(monkeypatch):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(500, json={"error": {"message": "SECRET"}})
        return completion(tokens=False, usage=False)
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        evidence = client.observe_token_evidence_probe(await client.execute(client.token_evidence_probe()))
        assert evidence["status"] == "unavailable" and client.token_evidence_probe() is None
        assert (await client.execute(workload())).valid
    assert "logprobs" not in calls[-1] and "stream_options" not in calls[-1]


@pytest.mark.parametrize("options,state", [({"response_format": {"type": "json_object"}}, "shape_unverified"),
                                           ({"logprobs": False}, "disabled_by_request")])
async def test_auto_logprobs_respect_explicit_options_and_unprobed_request_shapes(monkeypatch, options, state):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return completion()
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        client.observe_token_evidence_probe(await client.execute(client.token_evidence_probe()))
        result = await client.execute(workload().model_copy(update={"options": options}))
    assert not calls[-1].get("logprobs") and result.workload["token_evidence"]["logprobs"] == state


async def test_runtime_discovery_uses_one_route_and_caches_unavailable_result(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(404)
    mock_http(monkeypatch, handler)
    monkeypatch.setenv("INFERENCE_TOKEN", "secret")
    target = Target(name="test", url="http://runtime/v1", model="model", api_key_env="INFERENCE_TOKEN")
    config = RunConfig(targets=[target], checks=["serving"], metrics=False)
    run = SimpleNamespace(config=config, start=time.monotonic(), deadline=time.monotonic()+5,
                          stop=asyncio.Event(), observations={target.name: {}})
    first = await telemetry.scrape(run, target, "start")
    last = await telemetry.scrape(run, target, "end")
    assert len(calls) == 1 and str(calls[0].url) == "http://runtime/metrics"
    assert "authorization" not in calls[0].headers
    assert first["status"] == last["status"] == "endpoint_absent"
    assert last["attempted"] is False
    row = telemetry.derive_server_metrics({"snapshots": [first, last]})["server_ttft_mean_ms"]
    assert row["value"] is None and "HTTP 404" in row["reason"]
    assert row["details"]["endpoint"] == "http://runtime/metrics"


def test_unknown_runtime_is_detected_only_from_unambiguous_exact_metric_names():
    target = Target(name="test", url="http://runtime", model="model")
    config = RunConfig(targets=[target])
    result = telemetry.parse("vllm:num_requests_running 2\n", target, config)
    assert result["profile"] == "vllm-v1" and result["profile_source"] == "observed exact metric names"
    assert result["samples"][0]["family"] == "running"
    for body in ("my_vllm_num_requests_running 2\n", "vllm:num_requests_running 2\nsglang:num_running_reqs 3\n"):
        result = telemetry.parse(body, target, config)
        assert result["profile"] == "unknown" and result["samples"] == []


async def test_runner_negotiation_is_budgeted_setup_and_optional_failure_does_not_fail_serving(monkeypatch):
    from giraffe.runner import run_suite

    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        if body.get("logprobs"):
            return httpx.Response(400, json={"error": {"param": "logprobs"}})
        return completion(tokens=False)
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    config = RunConfig(targets=[target], checks=["serving"], samples=1, max_requests=6,
                       stop_after_errors=1, max_duration_seconds=5, metrics=False)
    report = await run_suite(config)
    probes = [r for r in report.requests if r.scenario == "performance_evidence_probe"]
    measured = [r for r in report.requests if r.scenario != "performance_evidence_probe"]
    assert len(probes) == 2 and all(not r.check_ids for r in probes)
    assert measured and all(r.valid for r in measured)
    assert len(report.requests) <= config.max_requests
    assert next(check for check in report.checks if check.id == "serving").status != "fail"
    assert report.observations["targets"][target.name]["token_evidence"]["logprobs"] == "rejected"
    assert len([r for r in calls if r.method == "GET" and r.url.path == "/metrics"]) == 1


@pytest.mark.parametrize("timeout", [.04, 8])
def test_probe_uses_configured_request_timeout(timeout):
    target = Target(name="test", url="http://runtime/v1", model="model")
    client = LLMClient(target, RunConfig(targets=[target], request_timeout_seconds=timeout))
    assert client.token_evidence_probe().request_timeout_seconds == timeout


async def test_runner_accepts_probe_content_after_five_seconds(monkeypatch):
    from giraffe.runner import run_suite

    class SlowContent(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(5.2)
            yield completion().content

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        if json.loads(request.content).get("max_tokens") == 1:
            return httpx.Response(200, stream=SlowContent(), headers={"content-type": "text/event-stream"})
        return completion()

    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    config = RunConfig(targets=[target], checks=["serving"], samples=1, max_requests=6,
                       request_timeout_seconds=8, max_duration_seconds=12, metrics=False)
    report = await run_suite(config)
    probes = [r for r in report.requests if r.scenario == "performance_evidence_probe"]
    assert len(probes) == 1
    assert probes[0].status == "completed" and probes[0].first_token_ms >= 5000
    assert probes[0].check_ids == [] and probes[0].workload["role"] == "setup"
    evidence = report.observations["targets"][target.name]["token_evidence"]
    assert evidence["status"] == "observed"
    assert evidence["logprobs"] == evidence["usage"] == "supported"
    assert all(r.valid for r in report.requests if r not in probes)


async def test_probe_rejection_retries_share_one_timeout_budget(monkeypatch):
    from giraffe.runner import run_suite

    probes = []

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        if body.get("max_tokens") == 1:
            probes.append(body)
            if body.get("logprobs"):
                await asyncio.sleep(.1)
                return httpx.Response(400, json={"error": {"param": "logprobs"}})
            await asyncio.sleep(.3)
        return completion()

    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    config = RunConfig(targets=[target], checks=["serving"], samples=1, max_requests=6,
                       request_timeout_seconds=.3, max_duration_seconds=5, metrics=False)
    report = await run_suite(config)
    records = [r for r in report.requests if r.scenario == "performance_evidence_probe"]
    assert len(probes) == len(records) == 2
    assert records[1].request_timeout_seconds < .25
    assert records[1].status == "timeout"
    evidence = report.observations["targets"][target.name]["token_evidence"]
    assert evidence["status"] == "unavailable" and evidence["usage"] == "unverified"
    assert all(r.valid for r in report.requests if r not in records)


async def test_probe_cannot_exceed_overall_run_deadline(monkeypatch):
    from giraffe.runner import run_suite

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        if json.loads(request.content).get("max_tokens") == 1:
            await asyncio.sleep(2)
        return completion()

    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime/v1", model="model")
    config = RunConfig(targets=[target], checks=["serving"], samples=1, max_requests=6,
                       request_timeout_seconds=8, max_duration_seconds=.5, metrics=False)
    started = time.monotonic()
    report = await run_suite(config)
    assert time.monotonic() - started < 2
    probes = [r for r in report.requests if r.scenario == "performance_evidence_probe"]
    assert len(probes) == 1 and not probes[0].valid
    assert probes[0].request_timeout_seconds <= .5
    assert report.observations["targets"][target.name]["token_evidence"]["status"] == "unavailable"


async def test_effective_request_deadline_is_recorded_and_cancel_payload_is_not_persisted(monkeypatch):
    async def handler(request):
        await asyncio.sleep(.2)
        return completion()
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime", model="model")
    async with LLMClient(target, RunConfig(targets=[target], request_timeout_seconds=1)) as client:
        result = await client.execute(workload().model_copy(update={"request_timeout_seconds": .02}))
        assert result.termination_reason == "request_deadline"
        assert result.request_timeout_seconds == result.termination_limit_seconds == .02
        task = asyncio.create_task(client.execute(workload()))
        await asyncio.sleep(.01)
        task.cancel("SECRET")
        cancelled = await task
        assert cancelled.termination_reason == "external_cancellation"
        assert "SECRET" not in json.dumps(cancelled.model_dump())


async def test_runner_outer_deadline_preserves_effective_request_timeout(monkeypatch):
    from giraffe.conditions import request_result_text
    from giraffe.runner import _Run

    async def handler(request):
        await asyncio.sleep(.2)
        return completion()
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime", model="model")
    config = RunConfig(targets=[target], checks=["serving"], request_timeout_seconds=.02,
                       max_duration_seconds=2)
    run = _Run(config, None, None)
    request = workload().model_copy(update={"scenario": "warm", "check_ids": ["serving"]})
    async with LLMClient(target, config) as client:
        record = await run.request(target, client, request)
    assert record.status == "timeout"
    assert record.termination_reason in {"request_timeout", "request_deadline"}
    assert record.termination_limit_seconds == record.request_timeout_seconds == .02
    assert request_result_text(record.model_dump()) == "Request timeout (0.02 s)"


@pytest.mark.parametrize("timer", ["readiness", "transport_read"])
async def test_longer_phase_deadline_does_not_replace_actual_timeout_reason(monkeypatch, timer):
    from giraffe.conditions import request_result_text
    from giraffe.runner import _Run

    async def handler(request):
        if timer == "transport_read":
            raise httpx.ReadTimeout("private transport details")
        await asyncio.sleep(.2)
        return completion()
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime", model="model")
    config = RunConfig(targets=[target], checks=["serving"], request_timeout_seconds=.02,
                       max_duration_seconds=2)
    run = _Run(config, None, None)
    request = workload().model_copy(update={"scenario": "readiness", "check_ids": ["serving"]})
    async with LLMClient(target, config) as client:
        record = await run.request(target, client, request, deadline_override=time.monotonic() + .5)
    assert record.status == "timeout"
    if timer == "transport_read":
        assert record.termination_reason == "read_timeout"
        assert request_result_text(record.model_dump()) == "Response read timeout"
    else:
        assert record.termination_reason in {"request_timeout", "request_deadline"}
        assert request_result_text(record.model_dump()) == "Request timeout (0.02 s)"


async def test_optional_probes_preserve_exact_planned_workload_budget(monkeypatch):
    from giraffe.planning import request_plan
    from giraffe.runner import run_suite

    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(404) if request.method == "GET" else completion()
    mock_http(monkeypatch, handler)
    target = Target(name="test", url="http://runtime", model="model")
    config = RunConfig(targets=[target], checks=["first_output"], samples=6, max_requests=7)
    assert request_plan(config)["required_request_limit"] == 7
    report = await run_suite(config)
    assert len(report.requests) == report.observations["request_budget_used"] == 7
    assert [r.scenario for r in report.requests] == ["initial", *(["warm"] * 6)]
    assert all(r.valid for r in report.requests)
    assert len([r for r in calls if r.method == "POST"]) == 7


def test_connection_timeout_retains_transport_failure_accounting_and_exact_label():
    from giraffe.conditions import request_result_text, request_status

    record = {"status": "failed", "termination_reason": "connect_timeout", "termination_limit_seconds": 10}
    assert request_status(record) == "failed"
    assert request_result_text(record) == "Connection timeout (10 s)"
