"""Runtime-specific evidence behind the single Limits and cancellation check."""

import importlib
import json
import sys
import types
from datetime import datetime, timezone

import pytest

from giraffe.backend_cancellation import evaluate, server_stop_result
from giraffe.models import CHECK_NAMES, RequestRecord, Target
from giraffe.ollama_cancellation import OllamaLogTail, evaluate_slot_reuse


def control(**changes):
    return RequestRecord(id="reference", target="o", fixture_id="reference", scenario="control", check_ids=["cancellation"],
                         started_at="now", status="completed", valid=True,
                         finish_reason="length", completion_tokens=128, requested_max_tokens=128).model_copy(update=changes)


def admissions(**changes):
    return [{"source": "ollama::runner", "id": 0, "prompt": 40, "cache": 100, "admitted_at_ns": 100},
            {"source": "ollama::runner", "id": 0, "prompt": 10, "cache": 48, "admitted_at_ns": 300} | changes]


def test_cancellation_catalog_has_one_runtime_independent_check():
    assert CHECK_NAMES["cancellation"] == "Cancellation & limits"
    assert not any("sglang" in key or "ollama" in key or "vllm" in key for key in CHECK_NAMES)


@pytest.mark.parametrize("runtime,backend_status,verdicts,expected", [
    ("sglang", "pass", ["pass", "pass", "pass"], "pass"),
    ("vllm", "fail", ["pass", "pass", "pass"], "pass"),
    ("sglang", "fail", ["pass", "fail", "pass"], "fail"),
    ("sglang", "inconclusive", ["pass"], "inconclusive"),
    ("sglang", "inconclusive", ["pass", "inconclusive", "pass"], "inconclusive"),
    ("ollama", "pass", ["inconclusive"] * 3, "inconclusive"),
    ("sglang", "unverified", [], "unverified"),
    (None, "blocked", [], "blocked"),
])
def test_server_stop_verdict_requires_all_native_terminations_not_combined_backend_status(
    runtime, backend_status, verdicts, expected,
):
    evidence = {"runtime": runtime, "status": backend_status, "planned": 3,
                "attempted": len(verdicts), "summary": "Evidence unavailable",
                "probes": [{"server_stop": {"status": status}} for status in verdicts]}
    result = server_stop_result(evidence)
    assert result["status"] == expected
    assert result["verified"] == verdicts.count("pass")


@pytest.mark.parametrize("rows,reference,expected", [
    (admissions(), control(), "pass"),
    (admissions(admitted_at_ns=2_000_000_200), control(), "inconclusive"),
    (admissions(id=1), control(), None),
    (admissions(cache=168), control(), "fail"),
    (admissions(), control(finish_reason="stop"), None),
    (admissions(), control(valid=False), None),
    (admissions(), control(requested_max_tokens=256), None),
    (admissions() + admissions(), control(), None),
    (admissions(admitted_at_ns=150), control(), None),
])
def test_ollama_requires_isolated_same_slot_early_release_and_capped_reference(rows, reference, expected):
    result = evaluate_slot_reuse(rows, 90, 200, 1, reference)
    assert (result["status"] if result else None) == expected
    if result:
        assert result["termination_ms"] is None
        assert result["evidence_kind"] == "single_slot_reuse"


def test_ollama_native_logs_require_single_slot_startup_and_ignore_prior_admissions(tmp_path):
    path = tmp_path / "server.log"
    path.write_text('Listening on 127.0.0.1:11435\nmsg=load request="{Operation:commit Parallel:1 KvSize:1024}"\n')
    target = Target(name="arbitrary-label", url="http://127.0.0.1:11435/v1", model="tiny",
                    deployment={"serving": {"parallelism": 1, "exclusive": True}})
    tail = OllamaLogTail(str(path), target)
    assert tail.read() == []
    with path.open("a") as sink:
        sink.write('time=2026-10-11T10:00:00.123Z level=DEBUG msg="loading cache slot" id=0 cache=40 prompt=25 used=0 remaining=25\n')
        sink.write('time=2026-10-11T10:00:00.124Z level=INFO msg="aborting completion request due to client closing the connection"\n')
    rows = tail.read()
    assert len(rows) == 1 and rows[0]["cache"] == 40
    assert rows[0]["admitted_at_ns"] == int(datetime(2026, 10, 11, 10, 0, 0, 123000, timezone.utc).timestamp() * 1e9)
    with path.open("a") as sink:
        sink.write('time=2026-10-11T10:00:00.125Z level=DEBUG msg="context limit hit - shifting" id=0 limit=128\n')
    with pytest.raises(ValueError, match="shifting"):
        tail.read()
    with pytest.raises(ValueError, match="single-slot"):
        OllamaLogTail(str(path), target.model_copy(update={"url": "http://localhost:11434"}))


def test_vllm_terminal_evidence_does_not_accept_sglang_or_dispatch_events():
    record = RequestRecord(id="probe", target="v", fixture_id="probe", scenario="backend_cancel", check_ids=["cancellation"],
                           started_at="now", server_request_id="chatcmpl-abc", observed_model="tiny",
                           client_cancelled_at_ns=200, status="cancelled")
    row = {"schema_version": 1, "source": "vllm::scheduler", "request_id": "chatcmpl-abc",
           "model": "tiny", "finish_reasons": ["abort"], "started_at_ns": 100,
           "finished_at_ns": 300, "received_at_ns": 400}
    assert evaluate(record, row, 90, 1, "vllm")["status"] == "pass"
    assert evaluate(record, row, 90, 1, "sglang") is None
    assert evaluate(record, row | {"finish_reasons": ["abort_requested"]}, 90, 1, "vllm") is None


@pytest.mark.parametrize("deferred,missing,expected", [(False, False, True), (True, False, False), (False, True, False)])
def test_vllm_scheduler_observer_only_writes_after_native_cache_release(monkeypatch, tmp_path, deferred, missing, expected):
    class Scheduler:
        def _free_request(self, request, delay_free_blocks=False):
            assert delay_free_blocks == deferred
            if not deferred:
                del self.requests[request.request_id]
            return (None, None)

    native = types.ModuleType("vllm.v1.core.sched.scheduler")
    native.Scheduler = Scheduler
    monkeypatch.setitem(sys.modules, native.__name__, native)
    monkeypatch.delitem(sys.modules, "giraffe.vllm_cancellation_hooks", raising=False)
    hooks = importlib.import_module("giraffe.vllm_cancellation_hooks")
    path = tmp_path / "traces.jsonl"
    if not missing:
        path.touch()
    monkeypatch.setenv("GIRAFFE_TRACE_FILE", str(path))
    monkeypatch.setenv("GIRAFFE_TRACE_MODEL", "tiny")
    request = types.SimpleNamespace(request_id="chatcmpl-native-1234abcd", arrival_time=1,
                                    status=types.SimpleNamespace(name="FINISHED_ABORTED"))
    scheduler = hooks.CancellationScheduler()
    scheduler.requests = {request.request_id: request}
    assert scheduler._free_request(request, delay_free_blocks=deferred) == (None, None)
    assert bool(path.exists() and path.read_text()) == expected
    if expected:
        row = json.loads(path.read_text())
        assert row["request_id"] == "chatcmpl-native" and row["finish_reasons"] == ["abort"]
    monkeypatch.delitem(sys.modules, "giraffe.vllm_cancellation_hooks", raising=False)


async def test_failed_ollama_reference_fails_without_dispatching_cancellation(tmp_path):
    from giraffe.backend_cancellation import exercise
    from giraffe.fixtures import builtin_fixtures
    from giraffe.models import RunConfig

    path = tmp_path / "server.log"
    path.write_text('Listening on 127.0.0.1:11435\nmsg=load request="{Operation:commit Parallel:1 KvSize:1024}"\n')
    target = Target(name="local", url="http://127.0.0.1:11435/v1", model="tiny",
                    metrics_profile="ollama", deployment={"serving": {"parallelism": 1, "exclusive": True}})
    config = RunConfig(targets=[target], checks=["cancellation"], test_options={"cancellation": {
        "backend_verification": "required", "server_log": str(path), "samples": 1}})

    class Run:
        observations = {"local": {}}
        calls = []

        def emit(self, *args, **kwargs):
            pass

        async def request(self, target, client, spec):
            self.calls.append(spec)
            return control(valid=False, status="failed")

    run = Run()
    run.config = config
    await exercise(run, target, None, builtin_fixtures(config))
    assert len(run.calls) == 1 and run.calls[0].cancel_after_chunks is None
    evidence = run.observations["local"]["backend_cancellation"]
    assert evidence["status"] == "fail" and evidence["probes"][0]["stage"] == "reference"
