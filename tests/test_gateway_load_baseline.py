"""Current saved gateway companion runs preserve workload identity."""

import asyncio
import json
import re

import httpx
import pytest

from giraffe import runner
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import _settings_reasons, compare_baseline, write_report
from tests.test_load_test_phase_a import config


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
    baseline = current.model_copy(deep=True)
    baseline.run_id = "saved-current-workload"
    saved, _ = write_report(baseline, tmp_path)
    baseline = RunReport.model_validate_json(saved.read_bytes())
    for report, previous in ((current, baseline), (baseline, current)):
        assert _settings_reasons(report, previous) == []
        result = compare_baseline(report, previous).baseline["targets"][0]
        assert result["status"] == "pass"
        assert {row["check"] for row in result["comparisons"]} >= set(checks) - {"gateway_attribution"}

    unverified = baseline.model_copy(deep=True)
    unverified.manifest["fixture_pack"]["workload_fixture_sha256"] = "unknown"
    unverified.manifest["fixture_pack"]["shared_workload_fixture_sha256"] = "unknown"
    assert "Fixture pack identity differs." in _settings_reasons(current, unverified)


@pytest.mark.parametrize("bad_digest", ["unknown", "a" * 63, "A" * 64, "g" * 64, None])
async def test_load_fixture_identity_requires_well_formed_full_digests(monkeypatch, local_tokenizer, bad_digest):
    def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 128, "completion_tokens": 4}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = RunConfig.model_validate({**config().model_dump(), "stream": False})
    current = await runner.run_suite(cfg)
    baseline = await runner.run_suite(cfg)
    assert _settings_reasons(current, baseline) == []
    assert compare_baseline(current, baseline).baseline["status"] == "pass"

    for report in (current, baseline):
        report.manifest["fixture_pack"]["shared_workload_fixture_sha256"] = "a" * 64
    # Matching placeholders must not attest Load's exemption from builtin identity.
    current.manifest["fixture_pack"]["workload_fixture_sha256"] = bad_digest
    baseline.manifest["fixture_pack"]["workload_fixture_sha256"] = bad_digest
    baseline.manifest["fixture_pack"]["builtin_sha256"] = "different-builtins"
    assert "Fixture pack identity differs." in _settings_reasons(current, baseline)
    assert "Fixture pack identity differs." in _settings_reasons(baseline, current)

    # Either side being malformed is enough, even with valid matching shared digests.
    for report in (current, baseline):
        report.manifest["fixture_pack"]["workload_fixture_sha256"] = "a" * 64
        assert "Fixture pack identity differs." in _settings_reasons(current, baseline)
        report.manifest["fixture_pack"]["workload_fixture_sha256"] = bad_digest
