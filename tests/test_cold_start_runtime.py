"""Socket-free acceptance for restart execution and cold-start measurements."""

import asyncio
import json
import os
import signal
import sys
import time

import httpx
import pytest

from giraffe import cold_start, runner, telemetry
from giraffe.client import LLMClient
from giraffe.models import Limits, RunConfig, Target
from giraffe.restart_script import preview_script
from tests.fake_endpoint import FakeEndpoint


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def settings(tmp_path, script=None, **updates):
    values = dict(targets=[Target(name="service", model="local", url="http://localhost:1234",
                                 restart_script=str(script) if script else None)],
                  checks=["serving"], samples=2, stream=False, concurrency=1,
                  max_requests=50, max_duration_seconds=5, request_timeout_seconds=1,
                  context_limit=512, limits=Limits(min_samples=2))
    if script:
        values.update(restart_target="service",
                      restart_script_sha256=preview_script(str(script))["sha256"])
    config = RunConfig(**(values | updates))
    config._restart_log_dir = tmp_path / "run"
    return config


def script_file(tmp_path, body):
    script = tmp_path / "restart.py"
    script.write_text(f"#!{sys.executable} -S\n{body}\n")
    script.chmod(0o700)
    return script


def observation(report):
    return report.observations["targets"]["service"]["restart"]


def mocked_http(monkeypatch, *, native=True, resident=False, duration=903_000_000,
                first_delay=0, models_status=200, reasoning_only=False,
                cold_failure=None, warm_failure=False):
    calls = []
    native_count = 0
    measurement_count = 0
    actual_client = httpx.AsyncClient

    async def handler(request):
        nonlocal native_count, measurement_count
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, path, payload))
        measured = payload and ": Reply with " in payload.get("messages", [{}])[-1].get("content", "")
        if measured:
            measurement_count += 1
            if measurement_count == 1 and cold_failure is not None:
                if cold_failure == "connect":
                    raise httpx.ConnectError("not up yet")
                if cold_failure == "timeout":
                    raise httpx.ReadTimeout("not up yet")
                if cold_failure == "missing_model":
                    return httpx.Response(404, json={"error": {"code": "model_not_found", "message": "model not found"}})
                if cold_failure == "unavailable_model":
                    return httpx.Response(404, json={"error": {"code": "model_not_available",
                        "message": "Requested model is not available yet."}})
                return httpx.Response(cold_failure, json={"error": "unavailable"})
            if warm_failure and payload["messages"][-1]["content"][0] in "23":
                return httpx.Response(503, json={"error": "warm failure"})
        if path == "/api/ps":
            if not native:
                return httpx.Response(404)
            return httpx.Response(200, json={"models": [{"model": "local:latest"}]
                if resident or native_count else []})
        if path == "/api/version":
            return httpx.Response(200 if native else 404, json={"version": "0.31"})
        if path.endswith("/models"):
            return httpx.Response(models_status, json={"data": []})
        if path == "/api/chat":
            native_count += 1
            if native_count == 1:
                await asyncio.sleep(first_delay)
            answer = payload["messages"][-1]["content"].split(":", 1)[0]
            chunks = [{"message": {"content": answer}, "done": False},
                      {"message": {"content": ""}, "done": True, "done_reason": "stop",
                       "load_duration": duration, "prompt_eval_count": 7, "eval_count": 1}]
            if reasoning_only:
                chunks = [{"model": "qwen3:0.6b", "message": {"role": "assistant", "content": "",
                    "thinking": "Let me think through the requested number."}, "done": True,
                    "done_reason": "length", "load_duration": duration,
                    "prompt_eval_count": 7, "eval_count": 128}]
            return httpx.Response(200, content="".join(json.dumps(c)+"\n" for c in chunks),
                                  headers={"content-type": "application/x-ndjson"})
        if path.endswith("/chat/completions"):
            prompt = "\n".join(m.get("content") or "" for m in payload["messages"])
            answer = (prompt.split(":", 1)[0] if ": Reply with " in prompt
                      else FakeEndpoint._answer(prompt))
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "" if reasoning_only else answer,
                **({"reasoning_content": "Let me think through the requested number."} if reasoning_only else {})},
                "finish_reason": "length" if reasoning_only else "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 128 if reasoning_only else 2}})
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(
        **kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(runner, "LLMClient", LLMClient)

    async def no_telemetry(*args, **kwargs):
        return {"status": "unavailable", "reason": "Mock has no exporter", "samples": [],
                "elapsed_seconds": 0, "unavailable": []}

    monkeypatch.setattr(telemetry, "scrape", no_telemetry)
    return calls


async def test_native_three_measurements_then_unchanged_normal_checks(monkeypatch, tmp_path):
    calls = mocked_http(monkeypatch)
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "print('issued')")))
    restart = observation(report)
    assert restart["status"] == "ready"
    assert restart["measurement_api"] == cold_start.NATIVE_API_LABEL
    assert restart["model_load_ms"] == 903
    assert restart["model_loaded_before_first_request"] == "No"
    assert restart["script_ms"] <= restart["service_answered_ms"] <= restart["readiness_ms"]
    assert len(restart["warm_requests"]) == 2
    inference = [(path, body) for method, path, body in calls if method == "POST"]
    assert [path for path, _ in inference[:3]] == ["/api/chat"] * 3
    assert all(path == "/v1/chat/completions" for path, _ in inference[3:])
    assert len(inference) == 6  # three measurements, usual initial and two serving requests
    native = [body for _, body in inference[:3]]
    prompts = [body["messages"][-1]["content"] for body in native]
    assert len(set(prompts)) == 3
    from giraffe.local_tokenizer import _builtin_encoding
    encoding = _builtin_encoding()
    assert len({len(encoding.encode(prompt, disallowed_special=())) for prompt in prompts}) == 1
    assert len({body["options"]["num_predict"] for body in native}) == 1
    assert all(body["stream"] is True for body in native)
    assert all(body["stream"] is False for _, body in inference[3:])
    assert next(check for check in report.checks if check.id == "serving").status == "pass"


@pytest.mark.parametrize("native", [True, False])
async def test_reasoning_only_length_response_is_ready_once_and_preserves_normal_verdicts(monkeypatch, tmp_path, native):
    calls = mocked_http(monkeypatch, native=native, reasoning_only=True, duration=867_000_000)
    ordinary = await runner.run_suite(settings(tmp_path))
    calls.clear()
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "pass"),
                                             readiness_timeout_seconds=.15))
    restart = observation(report)
    assert restart["status"] == "ready"
    measured = [r for r in report.requests if r.scenario in {"readiness", "cold_start_warm"}]
    assert len(measured) == 3
    assert all(r.http_status == 200 and not r.valid and r.finish_reason == "length" for r in measured)
    assert all(r.error == "reasoning exhausted the output budget before an answer" for r in measured)
    assert all(r.output == "" and r.reasoning and r.first_output_ms is None for r in measured)
    for summary, record in zip([restart["first_request"], *restart["warm_requests"]], measured):
        assert summary["first_output_kind"] == "reasoning"
        assert summary["first_output_ms"] == record.first_reasoning_ms
    first = measured[0]
    assert restart["script_ms"] <= restart["service_answered_ms"] <= restart["readiness_ms"]
    assert restart["readiness_ms"] <= first.completed_ms
    assert restart["first_output_kind"] == "reasoning"
    if native:
        assert restart["model_load_ms"] == 867
    assert [(c.id, c.status, c.summary) for c in ordinary.checks] == [
        (c.id, c.status, c.summary) for c in report.checks]


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 422])
async def test_nontransient_response_stops_readiness_after_one_attempt(monkeypatch, tmp_path, status):
    calls = mocked_http(monkeypatch, native=False, cold_failure=status)
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "pass"),
                                             readiness_timeout_seconds=1))
    restart = observation(report)
    assert restart["status"] == "blocked"
    assert len([method for method, _, _ in calls if method == "POST"]) == 1
    record = next(r for r in report.requests if r.scenario == "readiness")
    assert restart["reason"] == record.error
    assert restart["reason"] != "No successful inference within readiness budget"


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("failure", [429, 500, 503, "missing_model", "unavailable_model", "connect", "timeout"])
async def test_only_transient_errors_retry_then_measure_output(monkeypatch, tmp_path, native, failure):
    mocked_http(monkeypatch, native=native, cold_failure=failure)
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "pass"),
                                             readiness_timeout_seconds=2))
    assert observation(report)["status"] == "ready"
    assert len([r for r in report.requests if r.scenario == "readiness"]) == 2
    assert observation(report)["first_request"]["first_output_kind"] == "answer"


@pytest.mark.parametrize("native", [True, False])
async def test_warm_failure_is_not_measured_and_does_not_block_checks(monkeypatch, tmp_path, native):
    mocked_http(monkeypatch, native=native, warm_failure=True)
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "pass")))
    restart = observation(report)
    assert restart["status"] == "ready" and len(restart["warm_requests"]) == 2
    for item in restart["warm_requests"]:
        assert item["first_output_ms"] is item["elapsed_ms"] is None
        assert "503" in item["reason"]
    assert next(c for c in report.checks if c.id == "serving").status == "pass"


def test_cold_followups_are_visible_without_reserving_normal_request_budget(tmp_path):
    from giraffe.planning import request_plan

    config = settings(tmp_path, script_file(tmp_path, "pass"), max_requests=3)
    plan = request_plan(config)
    row = next(row for row in plan["rows"] if row["id"] == "cold_start_warm")
    assert row["requests_per_target"] == 2
    assert plan["known_per_target"] == 5
    assert plan["minimum_reserved_per_target"] == 3
    assert not any(item["kind"] == "fixed_cut" for item in plan["request_warnings"])
    timeline = {row["id"]: row for row in plan["time_timeline"]}
    assert timeline["restart_script"]["high_seconds"] == 600
    assert timeline["restart_readiness"]["high_seconds"] == config.readiness_timeout_seconds
    assert timeline["cold_start_warm"]["high_seconds"] == 2
    normal = request_plan(config.model_copy(update={"restart_target": None}))
    assert plan["budget_high_seconds"] > normal["budget_high_seconds"]
    assert not {"restart_script", "restart_readiness", "cold_start_warm"}.intersection(
        row["id"] for row in normal["time_timeline"])


async def test_metrics_discovery_cannot_delay_cold_start_measurements(monkeypatch, tmp_path):
    calls = mocked_http(monkeypatch)
    original = runner._metrics
    calls_at_discovery = []

    async def delayed_metrics(*args, **kwargs):
        calls_at_discovery.append(list(calls))
        await asyncio.sleep(.05)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runner, "_metrics", delayed_metrics)
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "pass")))
    assert observation(report)["status"] == "ready"
    before_discovery = [path for method, path, _ in calls_at_discovery[0] if method == "POST"]
    assert before_discovery == ["/api/chat"] * 3


async def test_resident_slow_first_answer_never_establishes_cold_load(monkeypatch, tmp_path):
    mocked_http(monkeypatch, resident=True, first_delay=.04, duration=88_000_000)
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "pass")))
    restart = observation(report)
    assert restart["first_request"]["elapsed_ms"] >= 35
    assert restart["model_load"] == "Model was already loaded"
    assert restart["model_loaded_before_first_request"] == "Yes"
    assert "model_load_ms" not in restart


async def test_generic_service_503_answer_is_timed_without_inventing_model_load(monkeypatch, tmp_path):
    calls = mocked_http(monkeypatch, native=False, models_status=503)
    report = await runner.run_suite(settings(tmp_path, script_file(tmp_path, "pass")))
    restart = observation(report)
    assert restart["status"] == "ready" and restart["service_answered_ms"] is not None
    assert restart["model_load"] == "Not reported by this server"
    assert restart["model_loaded_before_first_request"] == "Unknown"
    assert "model_load_ms" not in restart and "measurement_api" not in restart
    assert all(path == "/v1/chat/completions" for method, path, _ in calls if method == "POST")
    assert len([method for method, _, _ in calls if method == "POST"]) == 6


async def test_no_script_preserves_characterized_traffic_and_output(monkeypatch, tmp_path):
    calls = mocked_http(monkeypatch)
    ordinary = await runner.run_suite(settings(tmp_path))
    first_calls = list(calls)
    calls.clear()
    configured = settings(tmp_path, script_file(tmp_path, "raise RuntimeError('must not execute')"),
                          restart_target=None, restart_script_sha256=None)
    with_script_not_selected = await runner.run_suite(configured)
    assert first_calls == calls
    assert all(path == "/v1/chat/completions" for _, path, _ in calls)
    assert len(calls) == 3
    assert observation(ordinary) is None and observation(with_script_not_selected) is None
    fields = ("fixture_id", "scenario", "status", "output", "score", "http_status", "stream")
    assert [[getattr(r, field) for field in fields] for r in ordinary.requests] == [
        [getattr(r, field) for field in fields] for r in with_script_not_selected.requests]
    assert [(c.id, c.status, c.summary) for c in ordinary.checks] == [
        (c.id, c.status, c.summary) for c in with_script_not_selected.checks]
    assert not (tmp_path / "run" / "restart.log").exists()


async def test_nonzero_script_blocks_run_and_records_exit_and_output(tmp_path):
    script = script_file(tmp_path, "print('restart failed', flush=True)\nraise SystemExit(7)")
    report = await runner.run_suite(settings(tmp_path, script))
    result = observation(report)
    assert result["status"] == "blocked" and result["exit_code"] == 7
    assert "restart failed" in result["output_tail"]
    assert not report.requests
    serving = next(check for check in report.checks if check.id == "serving")
    assert serving.status == "blocked"


async def test_background_child_survives_run_deadline(tmp_path):
    """Exercise inherited file descriptors without binding any socket."""
    pid_file = tmp_path / "child.pid"
    body = ("import subprocess, sys, time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n")
    body += "time.sleep(30)\n"
    config = settings(tmp_path, script_file(tmp_path, body),
        restart_script_timeout_seconds=5, max_duration_seconds=2)
    stop = asyncio.Event()
    state = runner._Run(config, None, stop)
    task = asyncio.create_task(runner._restart(state, config.targets[0]))
    child_pid = None
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(.005)
        child_pid = int(pid_file.read_text())
        started = time.monotonic()
        assert not await asyncio.wait_for(task, 4)
        assert time.monotonic() - started < 3
        os.kill(child_pid, 0)
        result = state.observations["service"]["restart"]
        assert result["status"] == "blocked" and "timed out" in result["reason"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child_pid is None and pid_file.exists():
            child_pid = int(pid_file.read_text())
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
