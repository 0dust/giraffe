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
    "60eac0571375a103c1ebe7b23c3107b9814ceb0c002797736c55a6248a168f86",
    "d72acf540fbf8fbd8c45d3ab76d6041054209bb45913c681df171525f8ddc158",
    "ffda4bac34b3ab331e55c050a91707b1a0bfa16322347e7109646540e5fc4891",
    "16c362d1f79e5dd0b3f0601e0100ec9b19d7a512f5cee081af72594a683bf49f",
    "2d8a82427a1c241c95e2719c8ab307513d970ba7f14c80db77201d85c35a0897",
    "13a3e111a4f8985fa4dd8f763a093ade7ec118a6272e875b5f8abe0e1f104d94",
    "67da297a94f8a07f9ab21008c9c6ca87090a7fac3d30e4ff9a543e7491427667",
    "49dcdfb6b173a93720e4e9e5a9bc54ffcea7580346d2ea0210e600e0779a03e6",
    "e4ddc7a7e8563f9ab3bba2fc42ad0232ee924c65f67d92dafd89c21025d202d0",
)
CURRENT_LOAD_SOURCE = "fa40b2a909d2b067529ab02bd686c61adb8a175063e481d802ad33d308ef49fa"


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

    for change in ("interim_traffic", "pre_final_admission", "unknown_traffic", "unknown_generator", "settings", "missing_settings"):
        changed = baseline.model_copy(deep=True)
        altered = json.loads(json.dumps(identity))
        if change == "interim_traffic":
            altered["load_test_source_sha256"] = "113325dd5cffa04626cc7be7ee93bc6e9be67335531288bc329b3542a155c92a"
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


@pytest.mark.usefixtures("local_tokenizer")
async def test_policy_does_not_spend_load_reserve_for_sessions_and_tools(monkeypatch):
    raw = config().model_dump()
    raw.update(checks=["load", "sessions", "tools", "gateway_policy"], stream=False, retention="all", max_requests=6,
               sessions={"sessions": 1, "modes": ["fixed"], "turns": ["Name", "Recall"]}, limits={"min_samples": 2})
    raw["gateway_policy"] = {"affected_identity": "limited", "users": [
        {"identity": "limited", "requests": 3, "rpm": 2}, {"identity": "peer", "requests": 3, "rpm": 3}]}
    raw["targets"][0].update(api_key_env="SHARED", test_identities={
        "limited": {"api_key_env": "LIMITED"}, "peer": {"api_key_env": "PEER"}})
    cfg = RunConfig.model_validate(raw)
    for name in ("SHARED", "LIMITED", "PEER"):
        monkeypatch.setenv(name, name.lower() + "-key")
    wire = []

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        auth = request.headers["authorization"]
        wire.append(auth)
        if auth != "Bearer shared-key":
            return httpx.Response(404)
        await asyncio.sleep(.005)
        payload = json.loads(request.content)
        marker = re.search(r"Session_\d+_fixed", payload["messages"][0]["content"])
        message = {"role": "assistant", "content": marker[0] if marker else "K7P4"}
        finish = "stop"
        if payload.get("tools"):
            message.update(content="", tool_calls=[{"type": "function", "id": "call_1", "function": {
                "name": payload["tools"][0]["function"]["name"], "arguments": json.dumps({"city": "Delhi"})}}])
            finish = "tool_calls"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}],
                                        "usage": {"prompt_tokens": 128, "completion_tokens": 4}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await runner.run_suite(cfg)
    assert wire == ["Bearer shared-key"] * 6
    assert [record.scenario for record in report.requests][-3:] == ["session_fixed_turn0", "session_fixed_turn1", "tools_auto_complete"]
    assert next(check for check in report.checks if check.id == "tools").status == "pass"


@pytest.mark.parametrize("checks", [
    ["load", "sessions"],
    ["serving", "load", "sessions"],
    ["load", "sessions", "gateway_attribution"],
    ["serving", "sessions"],
    ["sessions", "gateway_attribution"],
])
async def test_saved_fixed_sessions_baseline_survives_gateway_dispatch(monkeypatch, local_tokenizer, tmp_path, checks):
    cfg = RunConfig.model_validate({**config().model_dump(), "checks": checks, "stream": True,
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
    identity = {"generator_source_sha256": workloads.FIXTURE_SOURCE_SHA256,
                "settings": {"sessions": cfg.sessions.model_dump(mode="json")}}
    if "load" in checks:
        identity["load_test_source_sha256"] = CURRENT_LOAD_SOURCE
        identity["settings"]["load_test"] = cfg.load_test.model_dump(mode="json")
    shared = "gateway_attribution" in checks
    fingerprint_key = "shared_workload_fixture_sha256" if shared else "workload_fixture_sha256"
    assert fixture_hash(identity) == current.manifest["fixture_pack"][fingerprint_key]
    identity["generator_source_sha256"] = GATEWAY_GENERATORS[0] if shared else CORE_GENERATOR
    baseline = current.model_copy(deep=True)
    baseline.run_id = "older-ordinary-workload"
    baseline.manifest["fixture_pack"][fingerprint_key] = fixture_hash(identity)
    if shared:
        baseline.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
            **identity, "settings": {**identity["settings"], "gateway_attribution": None}})
    saved, _ = write_report(baseline, tmp_path)
    baseline = RunReport.model_validate_json(saved.read_bytes())
    for report, previous in ((current, baseline), (baseline, current)):
        assert _settings_reasons(report, previous) == []
        result = compare_baseline(report, previous).baseline["targets"][0]
        assert result["status"] == "pass"
        assert {row["check"] for row in result["comparisons"]} >= set(checks) - {"gateway_attribution"}

    for change in (
        {"load_test_source_sha256": "113325dd5cffa04626cc7be7ee93bc6e9be67335531288bc329b3542a155c92a"},
        {"load_test_source_sha256": "unknown"},
        {"generator_source_sha256": "unknown"},
        {"session_sampling_source_sha256": "unknown"},
    ):
        unverified = baseline.model_copy(deep=True)
        unverified.manifest["fixture_pack"][fingerprint_key] = fixture_hash({**identity, **change})
        assert "Fixture pack identity differs." in _settings_reasons(current, unverified)
