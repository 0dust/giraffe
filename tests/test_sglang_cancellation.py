import asyncio
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
import pytest

from giraffe import runner
from giraffe.client import LLMClient
from giraffe.fixtures import builtin_fixtures
from giraffe.models import DEFAULT_CHECKS, RequestRecord, RequestSpec, RunConfig, Target
from giraffe.backend_cancellation import evaluate
from giraffe.sglang_traces import TraceTail, terminal_spans


def trace(**changes):
    return {"schema_version": 1, "source": "sglang::request", "request_id": "rid",
            "model": "tiny", "finish_reasons": ["abort"], "trace_id": "abcd",
            "started_at_ns": 100, "finished_at_ns": 300, "received_at_ns": 400,
            "received_monotonic_ns": 400} | changes


def cancelled(**changes):
    return RequestRecord(id="client", target="local", fixture_id="cancel", scenario="backend_cancel",
                         check_ids=["cancellation"], started_at="now", status="cancelled",
                         server_request_id="rid", observed_model="tiny",
                         client_cancelled_at_ns=200,
                         client_cancelled_monotonic_ns=200).model_copy(update=changes)


def test_sglang_fixtures_are_opt_in_versioned_and_respect_output_ceiling():
    target = Target(name="local", url="http://localhost", model="tiny")
    assert "backend_cancellation" not in builtin_fixtures(RunConfig(targets=[target]))
    fixtures = builtin_fixtures(RunConfig(
        targets=[target], checks=["cancellation"], max_output_tokens=16,
        test_options={"cancellation": {"backend_verification": "required", "cancel_after_chunks": 2}}))
    probe, followup = fixtures["backend_cancellation"]
    assert probe.max_tokens == 16 and probe.cancel_after_chunks == 2
    assert probe.input_chars == len(probe.messages[0]["content"])
    assert probe.fixture_id.endswith(".v1") and followup.fixture_id.endswith(".v1")
    assert followup.scorer == "none" and followup.max_tokens == 8


@pytest.mark.parametrize("reason,received,status", [
    ("abort", 300, "pass"), ("abort", 2_000_000_200, "inconclusive"),
    ("length", 2_000_000_200, "inconclusive"), ("stop", 300, "inconclusive"),
])
def test_correlated_terminal_reason_and_local_receipt_bound_decide_verdict(reason, received, status):
    row = trace(finish_reasons=[reason], received_monotonic_ns=received)
    assert evaluate(cancelled(), row, 90, 1)["status"] == status


@pytest.mark.parametrize("changes", [
    {"request_id": "other"}, {"model": "other"}, {"source": "sglang::scheduler"},
    {"started_at_ns": 0}, {"finished_at_ns": 80}, {"finished_at_ns": "300"},
    {"received_at_ns": 0}, {"received_monotonic_ns": 150}, {"finish_reasons": ["abort_requested"]},
    {"schema_version": 2},
])
def test_unrelated_stale_malformed_or_dispatch_only_evidence_cannot_pass(changes):
    assert evaluate(cancelled(), trace(**changes), 90, 1) is None


def test_native_protobuf_collector_only_saves_terminal_metadata():
    proto = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    request = proto.ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    span = scope.spans.add(start_time_unix_nano=100, end_time_unix_nano=300, trace_id=b"1234")
    attrs = {"module": "sglang::request", "rid": "rid", "gen_ai.request.id": "rid",
             "gen_ai.response.model": "tiny", "gen_ai.response.finish_reasons": '["abort"]',
             "prompt": "private prompt", "Authorization": "private token"}
    for key, value in attrs.items():
        span.attributes.add(key=key).value.string_value = value
    # Child spans and frontend spans must not masquerade as terminal request evidence.
    child = scope.spans.add()
    child.CopyFrom(span)
    child.attributes[0].value.string_value = "sglang::scheduler"
    rows = terminal_spans(request.SerializeToString())
    assert len(rows) == 1 and rows[0]["finish_reasons"] == ["abort"]
    assert rows[0]["request_id"] == "rid" and rows[0]["finished_at_ns"] == 300
    assert 0 < rows[0]["received_monotonic_ns"] <= time.monotonic_ns()
    assert "private" not in json.dumps(rows) and "Authorization" not in json.dumps(rows)
    with pytest.raises(Exception):
        terminal_spans(b"not protobuf")


def test_collector_cli_receives_native_otlp_and_rejects_invalid_input(tmp_path):
    proto = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    request = proto.ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add(
        start_time_unix_nano=100, end_time_unix_nano=300)
    for key, value in {"module": "sglang::request", "rid": "rid", "gen_ai.request.id": "rid",
                       "gen_ai.response.model": "tiny", "gen_ai.response.finish_reasons": '["abort"]'}.items():
        span.attributes.add(key=key).value.string_value = value
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = tmp_path / "traces.jsonl"
    process = subprocess.Popen([sys.executable, "-m", "giraffe.cli", "sglang-traces",
                                "--output", str(path), "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=.2, trust_env=False) as client:
            for _ in range(100):
                try:
                    response = client.post("/v1/traces", content=request.SerializeToString(),
                                           headers={"Content-Type": "application/x-protobuf"})
                    break
                except httpx.ConnectError:
                    assert process.poll() is None
                    time.sleep(.02)
            else:
                pytest.fail("Trace receiver did not start")
            assert response.status_code == 200
            assert json.loads(path.read_text())["finish_reasons"] == ["abort"]
            assert client.post("/v1/traces", content=b"invalid", headers={
                "Content-Type": "application/x-protobuf"}).status_code == 400
            assert client.post("/v1/traces", content=b"invalid").status_code == 415
            assert client.post("/wrong", content=b"invalid").status_code == 404
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_collector_cli_rejects_invalid_port_without_network(capsys, tmp_path):
    pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    from giraffe.cli import main
    assert main(["sglang-traces", "--output", str(tmp_path / "traces"), "--port", "0"]) == 2
    assert "1..65535" in capsys.readouterr().err


def test_tail_ignores_prior_rows_handles_partial_writes_and_rejects_rotation(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(trace()) + "\n")
    tail = TraceTail(str(path))
    assert tail.read() == []
    with path.open("a") as sink:
        sink.write('{"request_id":')
    assert tail.read() == []
    with path.open("a") as sink:
        sink.write('"fresh"}\n')
    assert tail.read() == [{"request_id": "fresh"}]
    path.rename(tmp_path / "old")
    path.write_text("")
    with pytest.raises(ValueError, match="rotated"):
        tail.read()


def test_tail_rejects_unbounded_records_and_malformed_json(tmp_path):
    path = tmp_path / "traces"
    path.touch()
    tail = TraceTail(str(path))
    path.write_text("x" * 17000)
    with pytest.raises(ValueError, match="16 KiB"):
        tail.read()
    path.write_text("")
    tail = TraceTail(str(path))
    path.write_text("invalid\n")
    with pytest.raises(ValueError):
        tail.read()
    with pytest.raises(ValueError, match="regular file"):
        TraceTail(str(tmp_path))


async def test_real_client_closes_after_answer_chunks_and_keeps_exact_server_id(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for content in ("", "1", "2", "3"):
                chunk = {"id": "native-rid", "model": "tiny", "choices": [
                    {"delta": {"content": content}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            await asyncio.sleep(10)

        async def aclose(self):
            self.closed = True

    stream = Stream()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))))
    target = Target(name="local", url="http://localhost", model="tiny")
    async with LLMClient(target, RunConfig(targets=[target])) as client:
        result = await client.execute(RequestSpec(
            fixture_id="cancel", scenario="backend_cancel", check_ids=["cancellation"],
            messages=[{"role": "user", "content": "count"}], cancel_after_chunks=2))
    assert result.status == "cancelled" and result.answer_chunks == 2
    assert result.output == "12" and stream.closed
    assert result.server_request_id == "native-rid" and result.client_cancelled_at_ns
    assert 0 < result.client_cancelled_monotonic_ns <= time.monotonic_ns()
    assert not result.stream_terminated


@pytest.mark.parametrize("mode,expected", [
    ("abort", "pass"), ("late", "inconclusive"), ("missing", "inconclusive"),
    ("natural", "inconclusive"), ("followup_error", "fail"),
    ("wrong_id", "inconclusive"), ("budget", "inconclusive"), ("late_budget", "inconclusive"),
    ("answer_mismatch", "fail"), ("clock_skew", "pass"),
    ("generation_error", "fail"), ("generation_timeout", "fail"),
])
async def test_runner_preserves_probe_evidence_and_honest_coverage(monkeypatch, tmp_path, mode, expected):
    path = tmp_path / "traces.jsonl"
    path.touch()

    class Client:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, spec, **kwargs):
            if spec.scenario not in {"backend_cancel", "backend_cancel_followup"}:
                status = "cancelled" if spec.scenario == "client_cancel" else "timeout" if spec.scenario == "client_deadline" else "completed"
                output = ("blue [END] green" if spec.fixture_id.startswith("limits.stop_control")
                          else "blue" if spec.fixture_id == "limits.stop.v1"
                          else str(spec.expected or "answer") if spec.scorer != "none" else "1 2")
                if mode == "answer_mismatch" and spec.scenario == "cancellation_probe":
                    output = "Wrong follow-up answer"
                return RequestRecord(id=spec.fixture_id, fixture_id=spec.fixture_id, scenario=spec.scenario,
                    check_ids=spec.check_ids, target="local", started_at=datetime.now(timezone.utc).isoformat(),
                    status=status, valid=status == "completed", output=output, output_tokens=1,
                    first_output_ms=1, elapsed_ms=2, client_cancelled_ms=2 if status == "cancelled" else None,
                    answer_chunks=spec.cancel_after_chunks or 1,
                    finish_reason=None if status == "cancelled" else "stop",
                    client_cancelled_at_ns=time.time_ns() if spec.scenario == "client_cancel" else None)
            now = time.time_ns()
            cancel_mono = None
            if spec.cancel_after_chunks:
                if mode in {"generation_error", "generation_timeout"}:
                    return RequestRecord(
                        id=spec.fixture_id, fixture_id=spec.fixture_id, scenario=spec.scenario,
                        check_ids=spec.check_ids, target="local",
                        started_at=datetime.now(timezone.utc).isoformat(),
                        status="failed" if mode == "generation_error" else "timeout",
                        http_status=500 if mode == "generation_error" else None,
                        error="HTTP 500" if mode == "generation_error" else "request deadline exceeded",
                    )
                # Keep timestamps in their true order; receipt can lag termination.
                start = now
                await asyncio.sleep(.001)
                cancel = time.time_ns()
                cancel_mono = time.monotonic_ns()
                delay = .002 if mode in {"late", "late_budget"} else .0001
                await asyncio.sleep(delay)
                end = time.time_ns()
                if mode != "missing":
                    offset = -18_000_000 if mode == "clock_skew" else 0
                    with path.open("a") as sink:
                        sink.write(json.dumps(trace(
                            request_id="other" if mode == "wrong_id" else "rid",
                            started_at_ns=start+offset, finished_at_ns=end+offset, received_at_ns=time.time_ns(),
                            received_monotonic_ns=time.monotonic_ns(),
                            finish_reasons=["stop" if mode == "natural" else "abort"])) + "\n")
                status, output, timestamp = "cancelled", "1 2", cancel
            else:
                status = "failed" if mode == "followup_error" else "completed"
                output, timestamp = "Okay", None
            return RequestRecord(
                id=spec.fixture_id, fixture_id=spec.fixture_id, scenario=spec.scenario,
                check_ids=spec.check_ids, target="local", started_at=datetime.now(timezone.utc).isoformat(),
                status=status, valid=status == "completed", output=output, observed_model="tiny",
                server_request_id="rid", client_cancelled_at_ns=timestamp, http_status=200,
                client_cancelled_monotonic_ns=cancel_mono,
            )

    monkeypatch.setattr(runner, "LLMClient", Client)
    config = RunConfig(targets=[Target(name="local", url="http://localhost", model="tiny",
                                     metrics_profile="sglang")], checks=["cancellation"],
                       max_requests=8 if mode in {"budget", "late_budget"} else 20, max_duration_seconds=2,
                       test_options={"cancellation": {"backend_verification": "required", "samples": 1, "trace_file": str(path),
                           "cleanup_seconds": .001 if mode in {"late", "late_budget"} else 1, "trace_wait_seconds": .02}})
    report = await runner.run_suite(config)
    check = next(c for c in report.checks if c.id == "cancellation")
    assert check.status == expected and check.required
    backend = check.metrics["backend_cancellation"]
    assert backend["probes"][0]["server_request_id"] == (
        None if mode in {"generation_error", "generation_timeout"} else "rid")
    assert check.evidence_ids
    if mode in {"generation_error", "generation_timeout"}:
        assert backend["status"] == "fail"
        assert backend["probes"][0]["status"] == "fail"
        assert backend["probes"][0]["followup_valid"]
        assert check.metrics["cancellation_results"]["server_stopped"]["status"] == "inconclusive"
        assert check.metrics["cancellation_results"]["answer"]["status"] == "pass"
    if mode == "answer_mismatch":
        assert backend["status"] == "pass"
        assert check.metrics["cancellation_results"]["server_stopped"]["status"] == "pass"
        assert check.metrics["cancellation_results"]["answer"]["status"] == "fail"
        assert "protocol failures" not in check.summary
        from giraffe.reporting import write_report
        _, html_path = write_report(report, tmp_path / "rendered")
        html = html_path.read_text()
        assert 'Cancellation — verify server stopped</td><td><span class="badge pass"' in html
        assert 'Cancellation — verify answer</td><td><span class="badge fail"' in html
    if mode == "followup_error":
        assert check.metrics["cancellation_results"]["server_stopped"]["status"] == "pass"
        assert check.metrics["cancellation_results"]["answer"]["status"] == "pass"
    if mode == "late":
        assert check.metrics["cancellation_results"]["server_stopped"]["status"] == "inconclusive"
        assert check.metrics["cancellation_results"]["answer"]["status"] == "pass"
    if expected == "pass":
        assert backend["probes"][0]["trace"]["finish_reasons"] == ["abort"]
        assert backend["probes"][0]["followup_valid"]


@pytest.mark.parametrize("target,options,stream,expected", [
    ({"name": "sglang", "metrics_profile": "unknown"}, {}, True, "blocked"),
    ({"metrics_profile": "sglang", "deployment": {"runtime": {"engine": "vllm"}}}, {}, True, "blocked"),
    ({"metrics_profile": "sglang", "deployment": {"runtime": {"engine": ["sglang"]}}}, {}, True, "blocked"),
    ({"url": "http://remote", "metrics_profile": "sglang"}, {}, True, "blocked"),
    ({"metrics_profile": "sglang"}, {}, True, "inconclusive"),
    ({"metrics_profile": "sglang"}, {"trace_file": "/missing/traces"}, True, "inconclusive"),
    ({"metrics_profile": "sglang"}, {}, False, "inconclusive"),
])
async def test_unavailable_backend_evidence_never_passes_or_dispatches_requests(target, options, stream, expected):
    report = await runner.run_suite(RunConfig(
        targets=[{"name": "local", "url": "http://localhost:1", "model": "tiny"} | target],
        checks=["cancellation"], stream=stream,
        max_requests=8, max_duration_seconds=.5,
        test_options={"cancellation": {"backend_verification": "required"} | options}))
    check = next(c for c in report.checks if c.id == "cancellation")
    assert check.metrics["backend_cancellation"]["status"] == expected
    assert not any(r.scenario == "backend_cancel" for r in report.requests)
    assert "cancellation" in DEFAULT_CHECKS
