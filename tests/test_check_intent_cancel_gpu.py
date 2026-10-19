"""Cancellation and GPU verdicts must follow endpoint evidence."""

import asyncio
import importlib
import json
import sys
import time
import types

import pytest

from giraffe import runner, telemetry
from giraffe.backend_cancellation import evaluate, exercise, server_stop_result
from giraffe.fixtures import builtin_fixtures
from giraffe.models import RequestRecord, Target
from giraffe.ollama_cancellation import evaluate_slot_reuse
from tests.test_backend_cancellation import admissions, control
from tests.test_gpu_reliability import snapshot
from tests.test_runner import FakeClient, check, config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    return FakeClient


@pytest.mark.parametrize("shape", ["legacy", "completed"])
@pytest.mark.parametrize("fixture,usage,status", [
    ("limits.output_cap", 8, "pass"), ("limits.output_cap", 7, "fail"),
    ("limits.output_cap", 9, "fail"), ("limits.output_cap", None, "inconclusive"),
    ("limits.stop.", 24, "inconclusive"), ("limits.stop_control", 24, "inconclusive"),
])
async def test_reasoning_budget_records_use_cap_usage_and_leave_stop_unverified(client, monkeypatch, shape, fixture, usage, status):
    original = client.execute
    async def reasoning(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.fixture_id.startswith(fixture):
            record.status, record.valid = ("failed" if shape == "legacy" else "completed"), False
            record.error = ("response contained no useful answer output" if shape == "legacy" else
                            "reasoning exhausted the output budget before an answer")
            record.output, record.reasoning = "", "Thinking through the task"
            record.answer_chunks, record.first_output_ms = 0, None
            record.completion_tokens = record.output_tokens = usage
            record.finish_reason = "length"
        return record
    monkeypatch.setattr(client, "execute", reasoning)
    result = check(await runner.run_suite(config(checks=["cancellation"])), "cancellation")
    assert result.status == status
    assert result.metrics["reasoning_budget_exhausted"]
    if fixture != "limits.output_cap":
        assert result.metrics["stop_enforcement"]["status"] == "inconclusive"


@pytest.mark.parametrize("output,chunks,status", [
    (" ".join(map(str, range(1, 101))), 1, "fail"),
    ("1", 9, "fail"), ("", 0, "inconclusive"), ("1 2 3 4", 2, "pass"),
])
async def test_output_cap_without_usage_checks_observed_lower_bound(client, monkeypatch, output, chunks, status):
    original = client.execute
    async def capped(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.fixture_id.startswith("limits.output_cap"):
            record.output, record.answer_chunks = output, chunks
            record.completion_tokens = record.output_tokens = None
            record.finish_reason = "length"
        return record
    monkeypatch.setattr(client, "execute", capped)
    result = check(await runner.run_suite(config(checks=["cancellation"])), "cancellation")
    assert result.status == status
    if status == "fail":
        assert result.metrics["output_cap_violations"]


@pytest.mark.parametrize("before,after,status", [(2, 1002, "pass"), (2, 1003, "inconclusive"),
    (1000, 3000, "pass"), (1000, 3001, "inconclusive"), (None, 2, "inconclusive")])
async def test_followup_latency_uses_same_fixture_precancel_baseline(client, monkeypatch, before, after, status):
    original, seen = client.execute, []
    async def probe(self, spec, **kwargs):
        seen.append((spec.scenario, spec.fixture_id))
        record = await original(self, spec, **kwargs)
        if spec.scenario == "cancellation_baseline":
            record.first_output_ms = before
        elif spec.scenario == "cancellation_probe":
            record.first_output_ms = after
        return record
    monkeypatch.setattr(client, "execute", probe)
    result = check(await runner.run_suite(config(checks=["cancellation"])), "cancellation")
    assert result.status == status
    scenarios = [scenario for scenario, _ in seen]
    cancel = scenarios.index("client_cancel")
    assert scenarios[cancel + 1] == "cancellation_probe"
    assert scenarios.index("cancellation_baseline") < cancel < scenarios.index("client_deadline")
    baseline = next(fixture for scenario, fixture in seen if scenario == "cancellation_baseline")
    assert seen[cancel + 1][1] == baseline
    assert result.metrics["followup_delay"]["baseline_first_output_ms"] == before
    assert result.metrics["followup_delay"]["followup_first_output_ms"] == after
    if status == "inconclusive" and before is not None:
        assert "follow-up delayed after cancellation" in result.summary


async def test_client_deadline_is_only_a_self_test(client, monkeypatch):
    original = client.execute
    async def deadline(self, spec, **kwargs):
        if spec.scenario == "client_deadline":
            record = await original(self, spec.model_copy(update={"scenario": "self_test"}), **kwargs)
            record.scenario = spec.scenario
            record.status, record.valid, record.score = "failed", False, False
            return record
        return await original(self, spec, **kwargs)
    monkeypatch.setattr(client, "execute", deadline)
    result = check(await runner.run_suite(config(checks=["cancellation"])), "cancellation")
    assert result.status == "pass"
    assert not result.metrics["client_deadline_self_test"]["timed_out"]
    assert "deadline" not in result.summary.lower()


async def test_deadline_self_test_score_cannot_fail_endpoint(client, monkeypatch):
    original = runner.score_response
    def score(spec, record):
        record = original(spec, record)
        if spec.scenario == "client_deadline":
            record.score = False
        return record
    monkeypatch.setattr(runner, "score_response", score)
    result = check(await runner.run_suite(config(checks=["cancellation"])), "cancellation")
    assert result.status == "pass"


async def test_protocol_failure_is_not_excused_as_reasoning_budget(client, monkeypatch):
    original = client.execute
    async def broken(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.fixture_id.startswith("limits.output_cap"):
            record.status, record.valid = "failed", False
            record.output, record.finish_reason, record.error = "", "length", "incomplete stream"
            record.output_tokens = record.completion_tokens = 8
        return record
    monkeypatch.setattr(client, "execute", broken)
    result = check(await runner.run_suite(config(checks=["cancellation"])), "cancellation")
    assert result.status == "fail"


async def test_inline_reasoning_is_not_stop_answer_evidence(client, monkeypatch):
    original = client.execute
    async def reasoning(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.fixture_id.startswith("limits.stop."):
            record.output = "<think>" + " ".join(map(str, range(1, 101))) + " [END] green"
            record.status, record.valid = "completed", False
            record.error = "reasoning exhausted the output budget before an answer"
            record.finish_reason = "length"
        return record
    monkeypatch.setattr(client, "execute", reasoning)
    result = check(await runner.run_suite(config(checks=["cancellation"])), "cancellation")
    assert result.status == "inconclusive"


def test_gpu_device_transient_absence_warns():
    first = snapshot({"0": [100, 30, 45, 0, 0], "1": [100, 30, 45, 0, 0]}, timestamp=time.time()*1000)
    metrics, status, summary = telemetry.assess_gpu([first, snapshot(timestamp=time.time()*1000), first], 60)
    assert status == "pass"
    assert any("transiently absent" in w.get("reason", "") for w in metrics["warnings"])
    assert metrics["observed_gpu_count"] == 2


def test_gpu_successful_scrape_without_gpu_families_is_ignored():
    first = snapshot(timestamp=time.time()*1000)
    empty = first | {"status": "available", "samples": []}
    metrics, status, summary = telemetry.assess_gpu([first, empty], 60)
    assert status == "pass"
    assert metrics["completed"] == 1
    assert metrics["observed_gpu_count"] == 1


@pytest.mark.parametrize("boundary", ["first", "last"])
def test_gpu_stale_boundary_cannot_pass_or_count_as_covered(boundary):
    fresh = snapshot(timestamp=time.time()*1000)
    stale = snapshot(timestamp=(time.time()-600)*1000)
    snaps = [stale, fresh] if boundary == "first" else [fresh, stale]
    metrics, status, _ = telemetry.assess_gpu(snaps, 60)
    assert status == "inconclusive"
    assert metrics["observed_gpu_count"] == 0
    assert not metrics["devices"][0]["fresh_in_first_and_last_snapshot"]


def test_gpu_failed_or_truncated_scrape_does_not_prove_disappearance():
    fresh = snapshot(timestamp=time.time()*1000)
    for failed in (fresh | {"status": "connection_failure", "samples": []},
                   fresh | {"status": "available", "samples": [], "series_limit_reached": True}):
        metrics, status, _ = telemetry.assess_gpu([fresh, failed, fresh], 60)
        assert status != "fail"
        assert not metrics["alerts"]


@pytest.mark.parametrize("end_status", ["connection_failure", "run_stopped", "stale_samples", "available"])
async def test_gpu_observation_window_requires_fresh_successful_end(monkeypatch, end_status):
    cfg = config(checks=["gpu"], gpu_min_observation_seconds=0)
    target = cfg.targets[0]
    start = snapshot(timestamp=time.time()*1000, elapsed=0)
    during = snapshot(timestamp=time.time()*1000, elapsed=2)
    end = snapshot(timestamp=(time.time()-600)*1000, elapsed=5) if end_status == "stale_samples" else (
        snapshot(timestamp=time.time()*1000, elapsed=5) if end_status == "available" else {"samples": [], "elapsed_seconds": 5})
    end["status"] = end_status
    async def scrape(*args, **kwargs):
        return end
    monkeypatch.setattr(telemetry, "scrape", scrape)
    run = runner._Run(cfg, None, asyncio.Event())
    run.observations[target.name]["serving_telemetry"]["snapshots"] = [start, during]
    run.observations[target.name]["metrics"] = [runner._gpu_snapshot(start), runner._gpu_snapshot(during)]
    await runner._finish_gpu_observation(run, target)
    window = run.observations[target.name]["gpu_observation_window"]
    assert window["complete"] == (end_status == "available")
    assert window["elapsed_seconds"] == (5 if end_status == "available" else 2)
    result = next(r for r in runner._checks(run, target) if r.id == "gpu")
    assert result.status == ("pass" if end_status == "available" else "inconclusive")


@pytest.mark.parametrize("initial_xid", [None, 0, 31])
def test_gpu_new_xid_is_not_hidden_by_error_labels(initial_xid):
    first = snapshot(timestamp=time.time()*1000)
    second = snapshot({"0": [100, 30, 45, 0, 79]}, timestamp=time.time()*1000)
    if initial_xid is None:
        first["samples"] = [s for s in first["samples"] if s["family"] != "device_errors"]
    else:
        first["samples"][-1]["value"] = initial_xid
        first["samples"][-1]["labels"].update(err_code=str(initial_xid), err_msg="initial")
    second["samples"][-1]["labels"].update(err_code="79", err_msg="fallen off bus")
    metrics, status, _ = telemetry.assess_gpu([first, second], 60)
    assert status == "fail"
    assert any(a["family"] == "device_errors" and a["value"] == 79 for a in metrics["alerts"])


@pytest.mark.parametrize("uuid", [False, True])
def test_gpu_identity_does_not_merge_hosts_or_borrow_freshness(uuid):
    snaps = [snapshot(), snapshot({"0": [101, 30, 45, 0, 0]})]
    for snap in snaps:
        other = snapshot()["samples"]
        for samples, instance in ((snap["samples"], "nodeA"), (other, "nodeB")):
            for sample in samples:
                sample["labels"].update(instance="shared" if uuid else instance, Hostname="shared")
                if uuid:
                    sample["labels"]["UUID"] = "GPU-" + instance
        snap["samples"].extend(other)
    metrics, status, _ = telemetry.assess_gpu(snaps, 60)
    assert status == "inconclusive"
    assert len(metrics["devices"]) == 2
    assert metrics["devices"][1]["freshness"] == "unknown"


def test_gpu_uuid_is_stable_across_instance_label_changes():
    snaps = [snapshot(timestamp=time.time()*1000), snapshot(timestamp=time.time()*1000)]
    for snap, instance in zip(snaps, ("before", "after")):
        for sample in snap["samples"]:
            sample["labels"].update(UUID="GPU-stable", instance=instance)
    metrics, status, _ = telemetry.assess_gpu(snaps, 60)
    assert status == "pass"
    assert len(metrics["devices"]) == 1


@pytest.mark.parametrize("tokens,finish,status", [(128, "length", "fail"), (128, "stop", "fail"),
    (127, "length", "inconclusive"), (None, "length", "inconclusive"), (128, "abort", "pass")])
def test_vllm_full_budget_natural_completion_after_cancel_is_not_release(tokens, finish, status):
    record = RequestRecord(id="p", target="v", fixture_id="p", scenario="backend_cancel", check_ids=["cancellation"],
        started_at="now", status="cancelled", server_request_id="server", observed_model="tiny",
        requested_max_tokens=128, answer_chunks=5, client_cancelled_at_ns=200)
    trace = {"schema_version": 1, "source": "vllm::scheduler", "request_id": "server", "model": "tiny",
        "started_at_ns": 100, "finished_at_ns": 300, "received_at_ns": 400, "finish_reasons": [finish]}
    if tokens is not None:
        trace["completion_tokens"] = tokens
    result = evaluate(record, trace, 90, 1, "vllm")
    assert result["status"] == status


@pytest.mark.parametrize("growth,status", [(126, "pass"), (127, "fail"), (128, "fail"), (129, "fail")])
def test_ollama_full_reference_cache_growth_after_cancel_fails(growth, status):
    result = evaluate_slot_reuse(admissions(cache=40+growth), 90, 200, 1, control())
    assert result["status"] == status
    assert result["cancelled_cache_growth_tokens"] == growth


async def test_ollama_full_budget_failure_reaches_backend_and_server_stop(monkeypatch):
    from giraffe import ollama_cancellation
    target = Target(name="local", url="http://localhost", model="tiny", metrics_profile="ollama")
    cfg = config(targets=[target], checks=["cancellation"], test_options={"cancellation": {
        "backend_verification": "required", "server_log": "unused", "samples": 1}})
    rows = []
    class Tail:
        def __init__(self, *args):
            pass

        def read(self):
            result, rows[:] = list(rows), []
            return result
    monkeypatch.setattr(ollama_cancellation, "OllamaLogTail", Tail)
    class Run:
        config = cfg
        observations = {"local": {}}
        deadline = time.monotonic() + 2
        stop = asyncio.Event()

        def emit(self, *args, **kwargs):
            pass

        async def request(self, target, client, spec):
            if spec.cancel_after_chunks:
                admitted = time.time_ns()
                cancelled = admitted + 1_000_000
                rows.extend([{"id": 0, "prompt": 40, "cache": 0, "admitted_at_ns": admitted},
                             {"id": 0, "prompt": 10, "cache": 168, "admitted_at_ns": cancelled + 1_000_000}])
                return control(id="cancel", status="cancelled", valid=False, output="1",
                    observed_model="tiny", server_request_id="rid", client_cancelled_at_ns=cancelled)
            return control(output="1 2", observed_model="tiny")
    run = Run()
    await exercise(run, target, None, builtin_fixtures(cfg))
    backend = run.observations["local"]["backend_cancellation"]
    assert backend["status"] == "fail"
    assert backend["probes"][0]["server_stop"]["status"] == "fail"
    assert server_stop_result(backend)["status"] == "fail"


def test_vllm_scheduler_records_native_completion_count(monkeypatch, tmp_path):
    class Scheduler:
        def _free_request(self, request, delay_free_blocks=False):
            del self.requests[request.request_id]
    native = types.ModuleType("vllm.v1.core.sched.scheduler")
    native.Scheduler = Scheduler
    monkeypatch.setitem(sys.modules, native.__name__, native)
    monkeypatch.delitem(sys.modules, "giraffe.vllm_cancellation_hooks", raising=False)
    hooks = importlib.import_module("giraffe.vllm_cancellation_hooks")
    path = tmp_path / "traces.jsonl"
    path.touch()
    monkeypatch.setenv("GIRAFFE_TRACE_FILE", str(path))
    monkeypatch.setenv("GIRAFFE_TRACE_MODEL", "tiny")
    request = types.SimpleNamespace(request_id="chatcmpl-native-1234abcd", arrival_time=1, num_output_tokens=128,
        status=types.SimpleNamespace(name="FINISHED_LENGTH_CAPPED"))
    scheduler = hooks.CancellationScheduler()
    scheduler.requests = {request.request_id: request}
    scheduler._free_request(request)
    assert json.loads(path.read_text())["completion_tokens"] == 128
    monkeypatch.delitem(sys.modules, "giraffe.vllm_cancellation_hooks", raising=False)
