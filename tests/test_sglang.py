"""SGLang's discovery and Prometheus contracts; inference uses the shared client."""

import asyncio
import json
import time

import httpx
import pytest

from giraffe import telemetry
from giraffe.deployment import snapshot
from giraffe.models import RunConfig, Target


@pytest.mark.parametrize("wrapped", [False, True])
async def test_discovery_selects_safe_fields_and_preserves_provenance(monkeypatch, wrapped):
    args = {
        "context_length": 1024, "max_running_requests": 2, "max_total_tokens": 2048,
        "chunked_prefill_size": 64, "mem_fraction_static": .3, "schedule_policy": "fcfs",
        "disable_radix_cache": False, "enable_metrics": True, "mlx_enable_sampling": True,
        "grammar_backend": "xgrammar", "tool_call_parser": "llama3",
        "attention_backend": "torch_native", "tp_size": 1, "pp_size": 1,
        "api_key": "private-inference-key", "admin_api_key": "private-admin-key",
        "model_loader_extra_config": '{"password":"private-loader-key"}',
        "ssl_keyfile": "/private-key.pem", "unexpected": "private-unknown-field",
    }
    body = {"version": "test-sglang", "server_args": args} if wrapped else args | {
        "version": "test-sglang"}
    seen = []
    actual = httpx.AsyncClient

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    monkeypatch.setenv("INFERENCE_KEY", "private-inference-key")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(
        **kw, transport=httpx.MockTransport(respond)))
    target = Target(name="sglang", url="http://localhost:30000/v1", model="llama",
                    api_key_env="INFERENCE_KEY", deployment={
                        "discovery": "sglang", "serving": {"context_limit": 4096}})
    result = await snapshot(target, RunConfig(targets=[target]), time.monotonic()+5, asyncio.Event())
    assert str(seen[0].url) == "http://localhost:30000/get_server_info"
    assert seen[0].headers["Authorization"] == "Bearer private-inference-key"
    fields = result["fields"]
    assert fields["runtime.engine"]["reported"]["value"] == "sglang"
    assert fields["runtime.version"]["reported"]["value"] == "test-sglang"
    assert fields["serving.context_limit"]["status"] == "conflicting"
    assert fields["serving.context_limit"]["reported"]["value"] == 1024
    assert fields["serving.context_limit"]["reported"]["provenance"] == "runtime-reported"
    assert fields["serving.prefix_caching"]["reported"]["value"] is True
    assert fields["serving.instrumentation"]["reported"]["value"] == "enabled"
    assert fields["serving.resolved_settings"]["reported"]["value"]["max_total_tokens"] == 2048
    assert fields["serving.resolved_settings"]["reported"]["value"]["grammar_backend"] == "xgrammar"
    assert "hardware.gpu_type" not in fields
    assert "backend.kernel" not in fields  # MPS device selection does not prove MLX.
    saved = json.dumps(result)
    for secret in ("private-inference-key", "private-admin-key", "private-loader-key",
                   "private-key.pem", "private-unknown-field"):
        assert secret not in saved


@pytest.mark.parametrize("body", [[], {"server_args": []}, {"context_length": True}])
async def test_malformed_discovery_never_invents_context_or_blocks_inference(monkeypatch, body):
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(
        **kw, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))))
    target = Target(name="sglang", url="http://localhost:30000/v1", model="llama",
                    deployment={"discovery": "sglang"})
    result = await snapshot(target, RunConfig(targets=[target]), time.monotonic()+5, asyncio.Event())
    assert "serving.context_limit" not in result["fields"]
    assert result["discovery"][0]["status"] == ("available" if isinstance(body, dict)
                                              and "server_args" not in body else "unavailable")


def test_metrics_keep_retraction_gauge_distinct_from_counter_and_cache_ratio():
    target = Target(name="sglang", url="http://localhost:30000/v1", model="llama",
                    metrics_profile="sglang", metrics_runtime_version="test-sglang")
    config = RunConfig(targets=[target])
    snapshots = []
    for index, count in enumerate((4, 7)):
        parsed = telemetry.parse(
            f'sglang:num_retracted_requests_total{{tp_rank="0"}} {count}\n'
            'sglang:num_retracted_reqs{tp_rank="0"} 2\n'
            'sglang:token_usage{tp_rank="0"} 0.95\n'
            'sglang:num_running_reqs{tp_rank="0"} 2\n'
            'sglang:num_queue_reqs{tp_rank="0"} 1\n'
            'sglang:cache_hit_rate{tp_rank="0"} 0.5\n'
            'sglang:cached_tokens_total{cache_source="device"} 100\n'
            'sglang:prompt_tokens_total{is_streaming="true"} 200\n'
            'sglang:time_to_first_token_seconds_sum 0.1\n'
            'sglang:time_to_first_token_seconds_count 2\n', target, config)
        assert parsed["status"] == "available" and parsed["missing_families"] == []
        assert "SGLang" in parsed["mapping_reference"]
        snapshots.append(parsed | {"phase": "load", "collected_at": str(index)})
    summary = telemetry.summarize({"snapshots": snapshots}, .9)
    series = {s["family"]: s for s in summary["series"]}
    assert series["retractions"]["kind"] == "counter"
    assert series["retractions"]["delta"] == 3
    assert series["retractions_current"]["kind"] == "gauge"
    assert series["retractions_current"]["peak"] == 2
    assert series["prefix_hit_rate"]["unit"] == "fraction"
    assert series["cached_tokens"]["unit"] == "tokens"
    assert summary["cache_pressure"]["status"] == "observed"
    # The old gauge alone cannot establish a cumulative retraction count.
    missing = telemetry.parse('sglang:num_retracted_reqs 0\n', target, config)
    assert "retractions" in missing["missing_families"]


@pytest.mark.parametrize("profile", ["unknown", "vllm-v1", "sglang"])
def test_metrics_detect_unknown_profile_but_respect_explicit_runtime_profile(profile):
    target = Target(name="service", url="http://localhost:30000", model="llama",
                    metrics_profile=profile)
    parsed = telemetry.parse('sglang:num_running_reqs 2\n', target, RunConfig(targets=[target]))
    assert parsed["status"] == ("available" if profile in {"unknown", "sglang"} else "absent_families")
