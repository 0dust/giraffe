"""Optional gateway dispatch preserves shared identities only at attested sources."""

import asyncio
import hashlib
import json
from pathlib import Path
import re

import httpx
import pytest

from giraffe import concurrent_load, load_test, runner, traffic, workloads
from giraffe.deployment import fixture_hash
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import _settings_reasons, compare_baseline, write_report
from tests.test_load_test_phase_a import config


CORE_GENERATOR = "d9a9e921584262148e06b636f0e8f195b7e530fd2a69c5e98f013359cf947ede"
GATEWAY_GENERATORS = (
    "7a16b503242045f808ecfdb41df6bc8c507ca68181aaf019f003dc2d4d2ffb72",
    "8037d490881621f37f1aef446474bc52a1ba276bcfcd174f39a14e37032dc9f0",
)
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
    assert workloads.FIXTURE_SOURCE_SHA256 in GATEWAY_GENERATORS
    identity = {"generator_source_sha256": workloads.FIXTURE_SOURCE_SHA256,
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


async def test_saved_load_and_fixed_sessions_baseline_survives_gateway_dispatch(monkeypatch, local_tokenizer, tmp_path):
    cfg = RunConfig.model_validate({**config().model_dump(), "checks": ["load", "sessions"], "stream": True,
        "sessions": {"sessions": 2, "modes": ["fixed"], "concurrency": 2}, "limits": {"min_samples": 2}})
    usage = {"prompt_tokens": 128, "completion_tokens": 4}

    class Answer(httpx.AsyncByteStream):
        def __init__(self, text):
            self.text = text

        async def __aiter__(self):
            for part in (self.text[:2], self.text[2:]):
                await asyncio.sleep(.01)
                yield ("data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) + "\n\n").encode()
            yield ("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage})
                   + "\n\ndata: [DONE]\n\n").encode()

    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        payload = json.loads(request.content)
        marker = re.search(r"Session_\d+_fixed", payload["messages"][0]["content"])
        answer = marker[0] if marker else "K7P4"
        if payload.get("stream"):
            return httpx.Response(200, stream=Answer(answer))
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
                                        "usage": usage})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    current = await runner.run_suite(cfg)
    assert current.overall == "pass"
    identity = {"generator_source_sha256": workloads.FIXTURE_SOURCE_SHA256, "load_test_source_sha256": CURRENT_LOAD_SOURCE,
                "settings": {"load_test": cfg.load_test.model_dump(mode="json"), "sessions": cfg.sessions.model_dump(mode="json")}}
    assert fixture_hash(identity) == current.manifest["fixture_pack"]["workload_fixture_sha256"]
    identity["generator_source_sha256"] = CORE_GENERATOR
    baseline = current.model_copy(deep=True)
    baseline.run_id = "core-load-and-sessions"
    baseline.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    saved, _ = write_report(baseline, tmp_path)
    baseline = RunReport.model_validate_json(saved.read_bytes())
    for report, previous in ((current, baseline), (baseline, current)):
        assert _settings_reasons(report, previous) == []
        result = compare_baseline(report, previous).baseline["targets"][0]
        assert result["status"] == "pass"
        assert {row["check"] for row in result["comparisons"]} >= {"load", "sessions"}

    for change in (
        {"load_test_source_sha256": "df3b7bbf30cab87d2e8c1c6dab8819f9cfe592b181407caa7c32af4c5f720cd4"},
        {"load_test_source_sha256": "unknown"},
        {"generator_source_sha256": "unknown"},
        {"session_sampling_source_sha256": "unknown"},
    ):
        unverified = baseline.model_copy(deep=True)
        unverified.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({**identity, **change})
        assert "Fixture pack identity differs." in _settings_reasons(current, unverified)
