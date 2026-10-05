"""Runner invariants: shared budgets, actual overlap, partial evidence and honest coverage."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import pytest

from giraffe import runner
from giraffe.models import Limits, RequestRecord, RunConfig, Target


class FakeClient:
    delay = .004
    active = 0
    peak = 0
    closed = []
    scenarios = []
    bad_target = None
    useful_event = True
    input_usage = True
    bad_sustained = False
    bad_mixed_gap = False
    initial_slow = False

    def __init__(self, target, config):
        self.target, self.config = target, config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        type(self).closed.append(self.target.name)

    async def execute(self, spec, *, first_output_event=None):
        cls = type(self)
        cls.active += 1
        cls.peak = max(cls.peak, cls.active)
        cls.scenarios.append((self.target.name, spec.scenario))
        started = time.monotonic()
        status = "completed"
        try:
            await asyncio.sleep(cls.delay / 2)
            if first_output_event is not None and cls.useful_event:
                first_output_event.set()
            if spec.scenario == "client_deadline":
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(cls.delay / 2)
            if spec.cancel_after_ms is not None:
                status = "cancelled"
        except asyncio.CancelledError:
            # Mirrors the real transport, preserving partial evidence on close.
            status = "cancelled"
        finally:
            cls.active -= 1
        output = json.dumps(spec.expected) if spec.scorer == "json" else str(spec.expected or "answer")
        if spec.scorer == "none":
            output = "1 2"
        if cls.bad_target == self.target.name and spec.scorer != "none":
            output = "wrong"
        elapsed = (time.monotonic()-started)*1000
        if cls.bad_sustained and spec.scenario == "sustained":
            elapsed = 500
        first_output = 500 if cls.initial_slow and spec.scenario == "initial" else 2
        return RequestRecord(
            id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids,
            started_at=datetime.now(timezone.utc).isoformat(), status=status,
            http_status=200, valid=status == "completed", output=output,
            output_chars=len(output), output_tokens=2,
            input_tokens=max(1, spec.input_chars//4) if cls.input_usage else None,
            input_chars=spec.input_chars, elapsed_ms=elapsed,
            first_output_ms=first_output, last_output_ms=first_output + 2,
            answer_chunks=2 if spec.stream else 0,
            chunks=2, stream=spec.stream, stream_terminated=True,
            finish_reason="stop", requested_max_tokens=spec.max_tokens or 128,
            max_stream_gap_ms=500 if cls.bad_mixed_gap and spec.scenario == "fairness_short" else 2,
        )


@pytest.fixture(autouse=True)
def client(monkeypatch):
    for name, value in {"delay": .004, "active": 0, "peak": 0, "closed": [], "scenarios": [],
                        "bad_target": None, "useful_event": True, "input_usage": True,
                        "bad_sustained": False, "bad_mixed_gap": False,
                        "initial_slow": False}.items():
        monkeypatch.setattr(FakeClient, name, value)
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    return FakeClient


def config(**kwargs):
    values = dict(targets=[Target(name="service", model="local", url="http://localhost:1234")],
                  checks=["serving"], samples=2, concurrency=2, max_requests=100,
                  max_duration_seconds=5, request_timeout_seconds=2,
                  context_limit=512, sustained_seconds=.015,
                  limits=Limits(min_samples=2))
    return RunConfig(**(values | kwargs))


def check(report, id, target="service"):
    return next(c for c in report.checks if c.id == id and c.target == target)


async def test_shared_request_concurrency_budget_and_replica_separation(client):
    targets = [Target(name="service", model="a", url="http://localhost:1234"),
               Target(name="replica", model="b", url="http://localhost:1235",
                      route="replica", parent="service")]
    events = []
    report = await runner.run_suite(config(targets=targets, max_requests=5,
                                           overlap_models=True, samples=20), progress=events.append)
    assert len(report.requests) == 5
    assert client.peak <= 2 and client.active == 0
    assert sorted(client.closed) == ["replica", "service"]
    assert {r.target for r in report.requests} == {"service", "replica"}
    assert report.overall == "inconclusive" and "request budget" in report.abort_reason
    assert all(check(report, "serving", name).status == "inconclusive" for name in ("service", "replica"))
    assert max(e["attempted"] for e in events) == 5
    assert len(report.checks) == 24
    assert all(row["other_replicas"] == "unverified" for row in report.observations["replica_coverage"])


async def test_global_duration_closes_active_client_and_preserves_attempt(client):
    client.delay = 1
    start = time.monotonic()
    report = await runner.run_suite(config(max_duration_seconds=.035))
    assert time.monotonic()-start < .2
    assert "duration" in report.abort_reason
    assert len(report.requests) == 1
    assert report.requests[0].status in {"timeout", "cancelled"}
    assert client.active == 0 and client.closed == ["service"]


async def test_user_stop_stops_submissions_and_saves_partial(client):
    client.delay = .5
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_suite(config(), stop_event=stop))
    await asyncio.sleep(.01)
    stop.set()
    report = await task
    assert report.abort_reason == "cancelled by user"
    assert len(report.requests) == 1 and report.requests[0].status == "cancelled"
    assert client.active == 0 and client.closed == ["service"]


async def test_missing_optional_telemetry_does_not_block_api():
    report = await runner.run_suite(config(checks=["serving", "gpu"], metrics=True))
    assert check(report, "serving").status == "pass"
    assert check(report, "gpu").status == "inconclusive"
    assert report.overall == "pass"
    assert check(report, "gpu").metrics["unavailable_families"]


async def test_normal_run_never_invokes_configured_restart(tmp_path):
    marker = tmp_path / "unexpected"
    target = Target(name="service", model="local", url="http://localhost:1234",
                    restart_command=["touch", str(marker)])
    report = await runner.run_suite(config(targets=[target]))
    assert not marker.exists()
    assert report.observations["targets"]["service"]["restart"] is None


async def test_explicit_restart_missing_hook_blocks_only_named_target():
    targets = [Target(name=name, model="local", url="http://localhost:1234")
               for name in ("service", "other")]
    report = await runner.run_suite(config(targets=targets, restart_target="service"))
    assert check(report, "serving").status == "blocked"
    assert check(report, "serving", "other").status == "pass"
    assert {r.target for r in report.requests} == {"other"}


async def test_explicit_restart_executes_only_named_argv_hook(tmp_path):
    import sys
    marker, other_marker = tmp_path / "chosen", tmp_path / "other"
    targets = [Target(name="service", model="local", url="http://localhost:1234",
                      restart_command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('yes')"]),
               Target(name="other", model="local", url="http://localhost:1235",
                      restart_command=["touch", str(other_marker)])]
    report = await runner.run_suite(config(targets=targets, restart_target="service"))
    assert marker.read_text() == "yes" and not other_marker.exists()
    observation = report.observations["targets"]["service"]["restart"]
    assert observation["status"] == "ready" and observation["readiness_ms"] > 0


async def test_wrong_answer_does_not_contaminate_other_model(client):
    client.bad_target = "bad"
    targets = [Target(name=name, model=name, url="http://localhost:1234") for name in ("good", "bad")]
    report = await runner.run_suite(config(targets=targets, checks=["correctness"], overlap_models=True))
    assert check(report, "correctness", "good").status == "pass"
    assert check(report, "correctness", "bad").status == "fail"
    assert report.overall == "fail"


async def test_observational_timing_and_separate_initial_samples(client):
    client.initial_slow = True
    report = await runner.run_suite(config(checks=["first_output"],
                                          limits=Limits(first_output_ms=10, min_samples=2)))
    result = check(report, "first_output")
    assert result.status == "pass"
    assert result.metrics["initial"]["first_output_p95_ms"] == 500
    assert result.metrics["first_output_p95_ms"] == 2
    unbounded = await runner.run_suite(config(checks=["first_output"]))
    assert check(unbounded, "first_output").status == "inconclusive"


async def test_buffered_stream_is_not_mixed_fairness_evidence(client):
    client.useful_event = False
    report = await runner.run_suite(config(checks=["fairness"],
                                          limits=Limits(fairness_max_ratio=2, min_samples=2)))
    assert check(report, "fairness").status == "inconclusive"
    assert report.observations["targets"]["service"]["fairness"]["overlaps"] == 0
    assert not any(r.scenario == "fairness_long" for r in report.requests)


async def test_actual_mixed_overlap_and_stream_gap_violation(client):
    client.bad_mixed_gap = True
    report = await runner.run_suite(config(checks=["fairness"],
                                          limits=Limits(stream_gap_ms=10, min_samples=2)))
    assert check(report, "fairness").status == "fail"
    assert report.observations["targets"]["service"]["fairness"]["overlaps"] == 2


async def test_deliberate_deadline_and_cancellation_not_serving_failures():
    report = await runner.run_suite(config(checks=["serving", "cancellation"], stop_after_errors=1))
    assert report.abort_reason is None
    assert check(report, "serving").status == "pass"
    deadline = next(r for r in report.requests if r.scenario == "client_deadline")
    assert deadline.status == "timeout"
    result = check(report, "cancellation")
    assert result.status == "pass"
    assert result.metrics["backend_cancellation"] == "unverified"
    assert deadline.id in result.evidence_ids


@pytest.mark.parametrize("checks", [["context"], ["capacity", "context"]])
async def test_context_sizing_uses_actual_usage_and_records_reproduction(checks):
    report = await runner.run_suite(config(checks=checks))
    result = check(report, "context")
    assert result.status == "pass"
    assert result.metrics["near_limit_coverage"]
    assert result.metrics["observed_max_input_tokens"] >= 512*.8
    calibration = result.metrics["calibration"]
    assert calibration["fixture_context_limit"] > 512
    assert len({r.fixture_id for r in report.requests if r.scenario == "context"}) == 6
    if "capacity" in checks:
        assert not any(r.scenario == "context_calibration" for r in report.requests)
        source = next(r for r in report.requests if r.id == calibration["source_request_id"])
        assert source.scenario.startswith("capacity_")


async def test_context_without_token_usage_never_claims_declared_limit(client):
    client.input_usage = False
    report = await runner.run_suite(config(checks=["context"]))
    result = check(report, "context")
    assert result.status == "inconclusive" and not result.metrics["near_limit_coverage"]


async def test_context_corrects_undershoot_from_fixed_chat_template_tokens(client, monkeypatch):
    original = client.execute

    async def with_template_overhead(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        # Token counts have a fixed chat-template cost, not just chars / token.
        result.input_tokens = int(spec.input_chars / 3.4) + 35
        return result

    monkeypatch.setattr(client, "execute", with_template_overhead)
    report = await runner.run_suite(config(checks=["capacity", "context"], context_limit=1024))
    result = check(report, "context")
    context = [r for r in report.requests if r.scenario == "context"]
    assert max(r.input_tokens for r in context[:6]) < 1024 * .8
    assert result.status == "pass"
    assert result.metrics["observed_max_input_tokens"] >= 1024 * .8
    assert len(context) == 12
    adjustments = result.metrics["calibration"]["adjustments"]
    assert len(adjustments) == 1
    source = next(r for r in context if r.id == adjustments[0]["source_request_id"])
    assert source.input_tokens == adjustments[0]["source_input_tokens"]
    assert {r.fixture_id for r in context[6:]} == {r.fixture_id for r in context[:6]}


@pytest.mark.parametrize("max_requests", [10, 100])
async def test_context_corrections_are_bounded_and_respect_request_budget(
    client, monkeypatch, max_requests,
):
    original = client.execute

    async def fixed_usage(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        result.input_tokens = 100
        return result

    monkeypatch.setattr(client, "execute", fixed_usage)
    report = await runner.run_suite(config(checks=["context"], context_limit=1024,
                                          max_requests=max_requests))
    result = check(report, "context")
    assert result.status == "inconclusive"
    assert len(report.requests) <= max_requests
    context = [r for r in report.requests if r.scenario == "context"]
    assert len(context) == (6 if max_requests == 10 else 18)
    assert len(result.metrics["calibration"].get("adjustments", [])) <= 2


async def test_context_correction_does_not_erase_an_earlier_wrong_answer(client, monkeypatch):
    original = client.execute
    calls = 0

    async def wrong_first_context(self, spec, **kwargs):
        nonlocal calls
        result = await original(self, spec, **kwargs)
        result.input_tokens = int(spec.input_chars / 3.4) + 35
        if spec.scenario == "context":
            calls += 1
            if calls == 1:
                result.output = "wrong label"
        return result

    monkeypatch.setattr(client, "execute", wrong_first_context)
    report = await runner.run_suite(config(checks=["context"], context_limit=1024))
    result = check(report, "context")
    assert result.metrics["near_limit_coverage"]
    assert result.status == "fail"
    assert result.metrics["correct"] < result.metrics["scored"]


@pytest.mark.parametrize("check_id", ["capacity", "fairness", "cancellation", "recovery"])
async def test_answer_failure_summary_identifies_fixture_and_warmup_failure(
    client, monkeypatch, check_id,
):
    original = client.execute

    async def refused_copy(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.fixture_id == "short.copy.v2":
            result.output = "I cannot copy that sentence."
        return result

    monkeypatch.setattr(client, "execute", refused_copy)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        latency_ms=100, fairness_max_ratio=10, min_samples=2,
    )))
    result = check(report, check_id)
    assert result.status == "fail"
    assert "Answer checks failed" in result.summary
    assert "short.copy.v2" in result.summary
    assert "warm-up" in result.summary
    failure = next(f for f in result.metrics["answer_failures"]
                   if f["fixture_id"] == "short.copy.v2")
    assert failure["warmup_failed"] == 1
    assert failure["failed"] > 0


async def test_answer_failure_details_do_not_hide_timing_failure(client, monkeypatch):
    original = client.execute
    client.bad_sustained = True

    async def refused_copy(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.fixture_id == "short.copy.v2":
            result.output = "I cannot copy that sentence."
        return result

    monkeypatch.setattr(client, "execute", refused_copy)
    report = await runner.run_suite(config(checks=["recovery"],
                                          limits=Limits(latency_ms=100, min_samples=2)))
    result = check(report, "recovery")
    assert result.status == "fail"
    assert "timing limits exceeded" in result.summary
    assert "short.copy.v2" in result.summary


@pytest.mark.parametrize("fault,message", [
    ("request", "Request or protocol failures"),
    ("cap", "Output token caps were also exceeded"),
])
async def test_answer_failure_details_preserve_other_cancellation_failures(
    client, monkeypatch, fault, message,
):
    original = client.execute

    async def failed_limit_and_wrong_answer(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.fixture_id == "short.copy.v2":
            result.output = "Wrong answer"
        if spec.fixture_id == "limits.output_cap.v1":
            if fault == "request":
                result.status, result.valid = "failed", False
            else:
                result.output_tokens = spec.max_tokens + 1
        return result

    monkeypatch.setattr(client, "execute", failed_limit_and_wrong_answer)
    report = await runner.run_suite(config(checks=["cancellation"]))
    result = check(report, "cancellation")
    assert result.status == "fail"
    assert message in result.summary
    assert "short.copy.v2" in result.summary


async def test_generation_uses_dedicated_longer_output_samples():
    report = await runner.run_suite(config(checks=["generation"],
                                          limits=Limits(min_output_tokens_per_second=1, min_samples=2)))
    result = check(report, "generation")
    assert result.status == "pass"
    assert result.metrics["generation_rate_samples"] == 2
    assert {r.scenario for r in report.requests if r.id in result.evidence_ids} == {"generation"}


async def test_unstreamed_generation_cannot_pass_a_generation_rate_limit():
    report = await runner.run_suite(config(checks=["generation"], stream=False,
                                          limits=Limits(min_output_tokens_per_second=1, min_samples=2)))
    result = check(report, "generation")
    assert result.status == "inconclusive"
    assert result.metrics["generation_rate_samples"] == 0


async def test_sustained_timing_failure_visible_even_when_recovery_is_fast(client):
    client.bad_sustained = True
    report = await runner.run_suite(config(checks=["recovery"],
                                          limits=Limits(latency_ms=100, min_samples=2)))
    result = check(report, "recovery")
    assert result.status == "fail"
    assert result.metrics["recovery"]["p95_ms"] < 100
    assert result.metrics["sustained"]["p95_ms"] == 500


async def test_capacity_missing_configured_token_rate_is_inconclusive(client, monkeypatch):
    original = client.execute

    async def no_usage(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        result.output_tokens = None
        return result

    monkeypatch.setattr(client, "execute", no_usage)
    report = await runner.run_suite(config(checks=["capacity"],
                                          limits=Limits(min_output_tokens_per_second=1, min_samples=2)))
    result = check(report, "capacity")
    assert result.status == "inconclusive"
    assert result.metrics["highest_tested_acceptable_concurrency"] is None
    assert result.metrics["levels"]["2"]["missing_timing_metrics"] == ["generation_tokens_per_second"]


async def test_nonstreaming_cannot_pass_a_stream_gap_limit():
    report = await runner.run_suite(config(checks=["generation"], stream=False,
                                          limits=Limits(stream_gap_ms=10, min_samples=2)))
    result = check(report, "generation")
    assert result.status == "inconclusive"
    assert result.metrics["missing_timing_metrics"] == ["stream_gap_ms"]


async def test_failed_limit_response_cannot_pass_cancellation(client, monkeypatch):
    original = client.execute

    async def failed_limits(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.scenario == "limits":
            result.status, result.valid = "failed", False
        return result

    monkeypatch.setattr(client, "execute", failed_limits)
    report = await runner.run_suite(config(checks=["cancellation"]))
    result = check(report, "cancellation")
    assert result.status == "fail"
    assert len(result.metrics["failed_limit_requests"]) == 2


async def test_one_sample_still_exercises_both_output_and_stop_limits():
    report = await runner.run_suite(config(checks=["cancellation"], samples=1))
    assert {r.fixture_id for r in report.requests if r.scenario == "limits"} == {
        "limits.output_cap.v1", "limits.stop.v1"}
    assert check(report, "cancellation").status == "pass"


async def test_fairness_cannot_pass_when_long_requests_fail(client, monkeypatch):
    original = client.execute

    async def failed_long(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.scenario == "fairness_long":
            result.status, result.valid = "failed", False
        return result

    monkeypatch.setattr(client, "execute", failed_long)
    report = await runner.run_suite(config(checks=["fairness"],
                                          limits=Limits(fairness_max_ratio=2, min_samples=2)))
    assert check(report, "fairness").status == "fail"


async def test_sustained_semantic_corruption_fails_recovery(client, monkeypatch):
    original = client.execute

    async def wrong_sustained(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.scenario == "sustained":
            result.output = "corrupted answer"
        return result

    monkeypatch.setattr(client, "execute", wrong_sustained)
    report = await runner.run_suite(config(checks=["recovery"]))
    result = check(report, "recovery")
    assert result.status == "fail"
    assert result.metrics["recovery"]["correct"] == 2
    assert result.metrics["sustained"]["correct"] == 0


@pytest.mark.parametrize("same_origin", [True, False])
async def test_metrics_reuses_credentials_only_on_same_origin(monkeypatch, same_origin):
    import httpx

    seen = []
    monkeypatch.setenv("GIRAFFE_TEST_KEY", "local-test-key")
    actual_client = httpx.AsyncClient

    def respond(request):
        seen.append(request)
        return httpx.Response(200, text="DCGM_FI_DEV_GPU_UTIL 10\n")

    def mock_client(**kwargs):
        return actual_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(runner.httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics" if same_origin else "http://localhost:5555/metrics",
                    api_key_env="GIRAFFE_TEST_KEY")
    report = await runner.run_suite(config(targets=[target], checks=["gpu"], metrics=True))
    assert len(seen) == 2
    assert (seen[0].headers.get("Authorization") == "Bearer local-test-key") is same_origin
    assert "local-test-key" not in report.model_dump_json()
    assert check(report, "gpu").status == "inconclusive"  # Four missing observation families.


async def test_stale_gpu_samples_are_unavailable(monkeypatch):
    import httpx

    actual_client = httpx.AsyncClient
    timestamp = int((time.time()-300)*1000)
    text = "\n".join(f"{name} 1 {timestamp}" for name in [
        "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_GPU_TEMP",
        "DCGM_FI_DEV_CLOCKS_EVENT_REASONS", "DCGM_FI_DEV_XID_ERRORS"])

    def mock_client(**kwargs):
        return actual_client(**kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(200, text=text)))

    monkeypatch.setattr(runner.httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    report = await runner.run_suite(config(targets=[target], checks=["gpu"], metrics=True))
    result = check(report, "gpu")
    assert result.status == "inconclusive"
    assert len(result.metrics["unavailable_families"]) == 5
    assert all(sample["stale"] for snapshot in result.metrics["snapshots"] for sample in snapshot["samples"])


async def test_operator_stop_is_missing_evidence_not_endpoint_failure(client):
    client.delay = .5
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_suite(config(), stop_event=stop))
    await asyncio.sleep(.01)
    stop.set()
    report = await task
    result = check(report, "serving")
    assert report.overall == result.status == "inconclusive"
    assert result.metrics["cancelled"] == 1
    assert result.metrics["failed"] == result.metrics["timed_out"] == 0


async def test_run_duration_cutoff_is_not_a_request_timeout_failure(client):
    client.delay = .5
    report = await runner.run_suite(config(max_duration_seconds=.02))
    assert report.overall == "inconclusive"
    assert report.requests[0].status == "cancelled"
    assert check(report, "serving").metrics["timed_out"] == 0


async def test_actual_request_timeout_remains_endpoint_failure(client):
    client.delay = .1
    report = await runner.run_suite(config(request_timeout_seconds=.01, stop_after_errors=1))
    assert report.overall == "fail"
    assert report.requests[0].status == "timeout"
    assert check(report, "serving").metrics["timed_out"] == 1


async def test_oversized_metrics_response_is_unavailable_and_bounded(monkeypatch):
    import httpx

    actual_client = httpx.AsyncClient
    consumed = []

    class LargeBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(10):
                consumed.append(index)
                yield b"x" * (1024 * 1024)

    def mock_client(**kwargs):
        return actual_client(**kwargs, transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=LargeBody())))

    monkeypatch.setattr(runner.httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    report = await runner.run_suite(config(targets=[target], checks=["gpu"], metrics=True))
    result = check(report, "gpu")
    assert result.status == "inconclusive"
    assert len(consumed) == 10  # Exactly five 1 MiB chunks per attempted snapshot, not ten.
    assert all("unavailable" in snapshot["reason"].lower() for snapshot in result.metrics["snapshots"])


async def test_recovery_missing_configured_output_rate_is_inconclusive(client, monkeypatch):
    original = client.execute

    async def no_output_usage(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        result.output_tokens = None
        return result

    monkeypatch.setattr(client, "execute", no_output_usage)
    report = await runner.run_suite(config(checks=["recovery"],
                                          limits=Limits(min_output_tokens_per_second=1, min_samples=2)))
    result = check(report, "recovery")
    assert result.status == report.overall == "inconclusive"
    assert result.metrics["missing_timing_metrics"] == ["generation_tokens_per_second"]
    assert result.metrics["sustained"]["completed"] > 0
    assert result.metrics["recovery"]["completed"] == 2


async def test_nonstreaming_recovery_cannot_pass_configured_gap_limit():
    report = await runner.run_suite(config(checks=["recovery"], stream=False,
                                          limits=Limits(stream_gap_ms=10, min_samples=2)))
    result = check(report, "recovery")
    assert result.status == report.overall == "inconclusive"
    assert result.metrics["missing_timing_metrics"] == ["stream_gap_ms"]


async def test_fairness_missing_configured_gap_is_inconclusive(client, monkeypatch):
    original = client.execute

    async def no_mixed_gap(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.scenario == "fairness_short":
            result.max_stream_gap_ms = None
        return result

    monkeypatch.setattr(client, "execute", no_mixed_gap)
    report = await runner.run_suite(config(checks=["fairness"],
                                          limits=Limits(stream_gap_ms=10, fairness_max_ratio=10,
                                                        min_samples=2)))
    result = check(report, "fairness")
    assert result.status == report.overall == "inconclusive"
    assert result.metrics["overlaps"] == 2
    assert result.metrics["missing_timing_metrics"] == ["stream_gap_ms"]


@pytest.mark.parametrize("scenario,check_id", [("capacity_c2_long", "capacity"),
                                                ("fairness_long", "fairness")])
async def test_mixed_and_capacity_semantic_failures_are_not_acceptable_load(
        client, monkeypatch, scenario, check_id):
    original = client.execute

    async def wrong_answer(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.scenario == scenario:
            result.output = "incorrect known answer"
        return result

    monkeypatch.setattr(client, "execute", wrong_answer)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        latency_ms=1000, fairness_max_ratio=10, min_samples=2)))
    result = check(report, check_id)
    assert result.status == report.overall == "fail"
    if check_id == "capacity":
        assert result.metrics["highest_tested_acceptable_concurrency"] == 1
        assert result.metrics["levels"]["2"]["accuracy"] == .5
    else:
        assert result.metrics["accuracy"] == .5


@pytest.mark.parametrize("check_id", ["capacity", "fairness"])
async def test_unscored_valid_load_is_inconclusive(monkeypatch, check_id):
    original = runner.score_response

    def omit_score(spec, record):
        result = original(spec, record)
        if spec.scenario in {"capacity_c2_long", "fairness_long"}:
            result.score = None
        return result

    monkeypatch.setattr(runner, "score_response", omit_score)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        latency_ms=1000, fairness_max_ratio=10, min_samples=2)))
    result = check(report, check_id)
    assert result.status == report.overall == "inconclusive"
    if check_id == "capacity":
        assert result.metrics["highest_tested_acceptable_concurrency"] == 1
        assert result.metrics["levels"]["2"]["unscored_completions"] == 2
    else:
        assert result.metrics["unscored_completions"] == 2


async def test_output_cap_includes_reported_reasoning_tokens(client, monkeypatch):
    original = client.execute

    async def excessive_reasoning(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.fixture_id == "limits.output_cap.v1":
            result.completion_tokens, result.output_tokens = 20, 4
        return result

    monkeypatch.setattr(client, "execute", excessive_reasoning)
    report = await runner.run_suite(config(checks=["cancellation"]))
    result = check(report, "cancellation")
    assert result.status == report.overall == "fail"
    assert len(result.metrics["output_cap_violations"]) == 1


@pytest.mark.parametrize("check_id", ["first_output", "generation", "capacity", "fairness", "recovery"])
async def test_generator_saturation_makes_timing_violation_inconclusive(monkeypatch, check_id):
    original = runner._Run.monitor

    async def saturated(self, tasks):
        self.max_lag_ms = 500
        await original(self, tasks)

    monkeypatch.setattr(runner._Run, "monitor", saturated)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        first_output_ms=1, latency_ms=1, stream_gap_ms=1, fairness_max_ratio=1.01, min_samples=2)))
    result = check(report, check_id)
    assert result.status == report.overall == "inconclusive"
    if check_id == "capacity":
        assert result.metrics["highest_tested_acceptable_concurrency"] is None


@pytest.mark.parametrize("check_id", ["capacity", "fairness", "recovery"])
async def test_generator_saturation_does_not_hide_known_answer_failure(client, monkeypatch, check_id):
    client.bad_target = "service"
    original = runner._Run.monitor

    async def saturated(self, tasks):
        self.max_lag_ms = 500
        await original(self, tasks)

    monkeypatch.setattr(runner._Run, "monitor", saturated)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        latency_ms=1, fairness_max_ratio=1.01, min_samples=2)))
    assert check(report, check_id).status == report.overall == "fail"


async def test_metrics_trickle_has_a_total_deadline(monkeypatch):
    import httpx

    actual_client = httpx.AsyncClient
    chunks = []

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(1000):
                await asyncio.sleep(.003)
                chunks.append(index)
                yield b"DCGM_FI_DEV_GPU_UTIL 1\n"

    def mock_client(**kwargs):
        return actual_client(**kwargs, transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=Trickle())))

    monkeypatch.setattr(runner.httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    state = runner._Run(config(targets=[target], max_duration_seconds=.03), None, None)
    started = time.monotonic()
    await runner._metrics(state, target, "before")
    snapshot = state.observations["service"]["metrics"][0]
    assert time.monotonic()-started < .1
    assert 0 < len(chunks) < 20
    assert snapshot["reason"] == "Metrics unavailable (TimeoutError)"
    assert len(snapshot["unavailable"]) == 5


async def test_metrics_skips_when_stop_already_requested(monkeypatch):
    def unexpected_request(**kwargs):
        pytest.fail("A stopped run must not open a metrics client")

    monkeypatch.setattr(runner.httpx, "AsyncClient", unexpected_request)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    stop = asyncio.Event()
    stop.set()
    state = runner._Run(config(targets=[target]), None, stop)
    await runner._metrics(state, target, "before")
    assert "stopped" in state.observations["service"]["metrics"][0]["reason"]
