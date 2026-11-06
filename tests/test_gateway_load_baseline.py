"""Optional gateway dispatch preserves Load identity only at attested sources."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from giraffe import concurrent_load, load_test, runner, traffic, workloads
from giraffe.deployment import fixture_hash
from giraffe.reporting import _settings_reasons, compare_baseline
from tests.test_load_test_phase_a import config


CORE_GENERATOR = "d9a9e921584262148e06b636f0e8f195b7e530fd2a69c5e98f013359cf947ede"
GATEWAY_GENERATOR = "60eac0571375a103c1ebe7b23c3107b9814ceb0c002797736c55a6248a168f86"
CURRENT_LOAD_SOURCE = "788f4c8f0b14421d5fc00f8b32c0ffcef79c04c647730767cb44b16584ed4aa4"


@pytest.mark.asyncio
async def test_completed_load_survives_only_known_gateway_dispatch_changes(monkeypatch, local_tokenizer):
    cfg = config().model_copy(update={"stream": False})
    payloads = []

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        await asyncio.sleep(.005)
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "K7P4"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 128, "completion_tokens": 4}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    current = await runner.run_suite(cfg)
    assert current.abort_reason is None and not current.limits_reached
    assert payloads and all(row.status == "completed" for row in current.requests)
    assert _settings_reasons(current, current) == []
    source = hashlib.sha256(b"".join(Path(module.__file__).read_bytes()
                                    for module in (load_test, concurrent_load, traffic))).hexdigest()
    assert source == CURRENT_LOAD_SOURCE
    assert workloads.FIXTURE_SOURCE_SHA256 == GATEWAY_GENERATOR
    identity = {"generator_source_sha256": GATEWAY_GENERATOR,
                "load_test_source_sha256": source,
                "settings": {"load_test": cfg.load_test.model_dump(mode="json")}}
    assert fixture_hash(identity) == current.manifest["fixture_pack"]["workload_fixture_sha256"]
    baseline = current.model_copy(deep=True)
    baseline.run_id = "core-load"
    identity["generator_source_sha256"] = CORE_GENERATOR
    baseline.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    assert _settings_reasons(current, baseline) == []
    assert _settings_reasons(baseline, current) == []
    compared = compare_baseline(current, baseline)
    assert compared.baseline["targets"][0]["load_test"]["points"]

    for change in ("old_traffic", "pre_final_admission", "unknown_traffic", "unknown_generator", "settings", "missing_settings"):
        changed = baseline.model_copy(deep=True)
        altered = json.loads(json.dumps(identity))
        if change == "old_traffic":
            altered["load_test_source_sha256"] = "df3b7bbf30cab87d2e8c1c6dab8819f9cfe592b181407caa7c32af4c5f720cd4"
        elif change == "pre_final_admission":
            altered["load_test_source_sha256"] = "b7b9faf687f2e5f96f3642b65e347960860211969d1be2a6e9d9a9144ec4e628"
        elif change == "unknown_traffic":
            altered["load_test_source_sha256"] = "unknown"
        elif change == "unknown_generator":
            altered["generator_source_sha256"] = "unknown"
        elif change == "settings":
            changed.manifest["config"]["load_test"]["profile"]["short"]["input_tokens"] += 1
            altered["settings"]["load_test"] = changed.manifest["config"]["load_test"]
        else:
            del changed.manifest["config"]["load_test"]
        changed.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(altered)
        assert "Fixture pack identity differs." in _settings_reasons(current, changed), change
        assert "Fixture pack identity differs." in _settings_reasons(changed, current), change
