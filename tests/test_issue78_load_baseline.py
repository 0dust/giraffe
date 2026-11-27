"""Generated Load only ignores unused built-in fixtures for exact workloads."""

import asyncio
import json

import httpx
import pytest

from giraffe import runner
from giraffe.models import RunConfig, RunReport
from giraffe.deployment import fixture_hash
from giraffe.reporting import _settings_reasons, compare_baseline
from tests.virtual_clock import run_virtual


async def generate_load_report(monkeypatch, *, restart=False):
    config = RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://unused.test/v1", "model": "test"}],
        "checks": ["load"], "stream": False, "max_memory_mb": 0,
        "restart_target": "local" if restart else None,
        "load_test": {
            "profile": {"short": {"input_tokens": 64, "output_tokens": 8}},
            "workloads": {"combinations": {"selected": ["short_short"]}},
            "patterns": {"concurrent": {"enabled": False}, "constant": {"enabled": True},
                         "rates": [40]},
            "schedule": {"duration_seconds": 1, "finish_seconds": .1},
            "pass_criteria": {"min_valid_responses": 1, "max_completion_ms": 1000},
        },
    })
    payloads = []

    if restart:
        async def completed_hook(run, target):
            run.observations[target.name]["restart"] = {
                "status": "hook_completed", "started_monotonic": asyncio.get_running_loop().time()}
            return True

        monkeypatch.setattr(runner, "_restart", completed_hook)

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(200 if restart else 404)
        payload = json.loads(request.content)
        payloads.append(payload)
        await asyncio.sleep(.002)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "K7P4"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 64, "completion_tokens": 4}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    report = await runner.run_suite(config)
    assert report.abort_reason is None and not report.limits_reached
    assert len(payloads) == len(report.requests) == 40 + int(restart)
    assert {payload["max_tokens"] for payload in payloads} == ({8, config.max_output_tokens} if restart else {8})
    assert sum(record.scenario.startswith("load_") for record in report.requests) == 40
    assert sum(record.scenario == "readiness" for record in report.requests) == int(restart)
    # Both sides use the same in-memory endpoint and tokenizer; the machine
    # running this regression is not an experimental difference.
    report.manifest["run_location"] = {"hostname": "fixture", "system": "fixture",
                                       "machine": "fixture", "python": "3.12.1"}
    return report


@pytest.mark.usefixtures("local_tokenizer")
def test_generated_load_ignores_unused_builtin_fixtures(monkeypatch, tmp_path):
    current = run_virtual(monkeypatch, generate_load_report(monkeypatch))
    saved = tmp_path / "report.json"
    saved.write_text(current.model_dump_json())
    baseline = RunReport.model_validate_json(saved.read_text())
    baseline.manifest["fixture_pack"]["builtin_sha256"] = "different-builtins"
    assert _settings_reasons(current, baseline) == []
    assert _settings_reasons(baseline, current) == []
    assert compare_baseline(current, baseline).baseline["targets"][0]["load_test"]["points"]

    for change in ("profile", "unknown_fingerprint", "missing_identity", "mixed_builtin"):
        changed = baseline.model_copy(deep=True)
        if change == "profile":
            changed.manifest["config"]["load_test"]["profile"]["short"]["output_tokens"] += 1
            identity, _ = runner.build_workload_identity(RunConfig.model_validate(changed.manifest["config"]))
            changed.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
        elif change == "unknown_fingerprint":
            changed.manifest["fixture_pack"]["workload_fixture_sha256"] = "unknown"
        elif change == "missing_identity":
            del changed.manifest["fixture_pack"]["workload_fixture_sha256"]
        else:
            changed.manifest["config"]["checks"].append("serving")
        assert "Fixture pack identity differs." in _settings_reasons(current, changed), change
        assert "Fixture pack identity differs." in _settings_reasons(changed, current), change


@pytest.mark.usefixtures("local_tokenizer")
def test_load_restart_still_requires_matching_readiness_fixture(monkeypatch):
    current = run_virtual(monkeypatch, generate_load_report(monkeypatch, restart=True))
    baseline = current.model_copy(deep=True)
    baseline.manifest["fixture_pack"]["builtin_sha256"] = "different-readiness-fixture"
    assert "Fixture pack identity differs." in _settings_reasons(current, baseline)
