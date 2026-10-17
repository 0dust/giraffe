"""Runtime discovery contracts; these fixtures do not qualify live engines."""

import asyncio
import json
import time

import httpx
import pytest

from giraffe import telemetry
from giraffe.deployment import snapshot
from giraffe.models import RunConfig, Target


DISCOVERY = [
    ("llamacpp", "/props", {
        "build_info": "b11696-test", "total_slots": 2,
        "default_generation_settings": {"n_ctx": 4096},
        "chat_template": "test template", "model_path": "/private/model.gguf",
        "api_key": "private-key",
    }, {"runtime.version": "b11696-test", "serving.context_limit": 4096,
        "serving.max_num_seqs": 2}),
    ("tensorrt-llm", "/version", {"version": "test-trtllm", "api_key": "private-key"},
     {"runtime.version": "test-trtllm"}),
    ("lmdeploy", "/v1/models", {"data": [
        {"id": "another-model", "owned_by": "lmdeploy"},
        {"id": "test-model", "owned_by": "lmdeploy", "root": "/private/model"},
    ]}, {"model.served": "test-model"}),
    ("tgi", "/info", {"router": "text-generation-router", "version": "test-tgi",
        "model_id": "test-model", "model_sha": "model-revision", "sha": "runtime-commit",
        "max_total_tokens": 4096, "max_input_tokens": 3072, "max_concurrent_requests": 128,
        "api_key": "private-key"},
     {"runtime.version": "test-tgi", "model.revision": "model-revision",
      "runtime.commit": "runtime-commit", "serving.context_limit": 4096}),
]


@pytest.mark.parametrize("engine,path,body,expected", DISCOVERY)
async def test_engine_discovery_selects_safe_fields(monkeypatch, engine, path, body, expected):
    actual = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    monkeypatch.setenv("ENGINE_KEY", "private-key")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(
        **kw, transport=httpx.MockTransport(respond)))
    target = Target(name=engine, url="http://localhost:8000/v1", model="test-model",
                    api_key_env="ENGINE_KEY", deployment={"discovery": engine})
    result = await snapshot(target, RunConfig(targets=[target]), time.monotonic()+5, asyncio.Event())
    assert len(seen) == 1 and seen[0].url.path == path
    assert seen[0].headers["Authorization"] == "Bearer private-key"
    fields = result["fields"]
    assert fields["runtime.engine"]["reported"]["value"] == engine
    for key, value in expected.items():
        assert fields[key]["reported"]["value"] == value
        assert fields[key]["reported"]["provenance"] == "runtime-reported"
    assert "hardware.gpu_type" not in fields
    assert "backend.kernel" not in fields
    saved = json.dumps(result)
    assert "private-key" not in saved and "/private/" not in saved
    if engine == "lmdeploy":
        assert "runtime.version" not in fields
    if engine == "tgi":
        # Admission limit is not a count of active decoding slots.
        assert "serving.max_num_seqs" not in fields


@pytest.mark.parametrize("engine,path,body,expected", DISCOVERY)
@pytest.mark.parametrize("bad_body", [[], {"unrelated": "value"}])
async def test_malformed_engine_discovery_stays_unavailable(
    monkeypatch, engine, path, body, expected, bad_body,
):
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(
        **kw, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad_body))))
    target = Target(name=engine, url="http://localhost:8000/v1", model="test-model",
                    deployment={"discovery": engine})
    result = await snapshot(target, RunConfig(targets=[target]), time.monotonic()+5, asyncio.Event())
    assert result["discovery"][0]["status"] == "unavailable"
    assert "runtime.engine" not in result["fields"]


@pytest.mark.parametrize("engine,body,family,value,missing", [
    ("llamacpp", "llamacpp:requests_processing 2\nllamacpp:requests_deferred 1\n",
     "running", 2, "cache_occupancy"),
    ("lmdeploy", "lmdeploy:gpu_cache_usage_perc 0.75\nlmdeploy:num_requests_running 2\n",
     "cache_occupancy", .75, "waiting"),
    ("tgi", "tgi_request_queue_duration_sum 0.5\ntgi_request_queue_duration_count 2\n",
     "server_queue_sum", .5, "cache_occupancy"),
])
def test_engine_metrics_preserve_exact_units_and_missing_evidence(engine, body, family, value, missing):
    target = Target(name=engine, url="http://localhost:8000/v1", model="test-model",
                    metrics_profile=engine)
    result = telemetry.parse(body, target, RunConfig(targets=[target]))
    assert result["status"] == "available"
    assert next(s for s in result["samples"] if s["family"] == family)["value"] == value
    assert missing in result["missing_families"]
    # Unrelated runtime metrics must not be used by this profile.
    wrong = telemetry.parse("vllm:num_requests_running 99\n", target, RunConfig(targets=[target]))
    assert wrong["samples"] == []


def test_tensorrt_json_statistics_are_not_prometheus_or_healthy_zeroes():
    target = Target(name="trt", url="http://localhost:8000/v1", model="test-model",
                    metrics_profile="tensorrt-llm")
    result = telemetry.parse('[{"numActiveRequests": 0}]', target, RunConfig(targets=[target]))
    assert result["status"] == "unsupported_format" and result["samples"] == []
    assert "running" in result["missing_families"]


@pytest.mark.parametrize("engine", [item[0] for item in DISCOVERY])
async def test_optional_discovery_http_failure_keeps_configured_evidence(monkeypatch, engine):
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(
        **kw, transport=httpx.MockTransport(lambda _: httpx.Response(404))))
    target = Target(name=engine, url="http://localhost:8000/v1", model="test-model",
                    deployment={"discovery": engine, "runtime": {"engine": engine}})
    result = await snapshot(target, RunConfig(targets=[target]), time.monotonic()+5, asyncio.Event())
    assert result["discovery"][0]["status"] == "unavailable"
    assert result["fields"]["runtime.engine"]["configured"]["value"] == engine
    assert "reported" not in result["fields"]["runtime.engine"]


@pytest.mark.parametrize("engine", [item[0] for item in DISCOVERY])
async def test_native_cancellation_without_an_adapter_remains_inconclusive(engine):
    from giraffe.backend_cancellation import exercise, server_stop_result
    from giraffe.runner import _Run

    target = Target(name=engine, url="http://localhost:8000/v1", model="test-model",
                    deployment={"discovery": engine})
    config = RunConfig(targets=[target], checks=["cancellation"],
                       test_options={"cancellation": {"backend_verification": "required"}})
    run = _Run(config, asyncio.Event(), None)
    await exercise(run, target, None, {})
    evidence = run.observations[engine]["backend_cancellation"]
    assert evidence["status"] == "inconclusive" and evidence["attempted"] == 0
    assert "unavailable" in evidence["summary"]
    assert server_stop_result(evidence)["status"] == "inconclusive"
