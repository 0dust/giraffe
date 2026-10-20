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

    async def ready(self, deadline):
        return True

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
            if spec.cancel_after_ms is not None or spec.cancel_after_chunks is not None:
                status = "cancelled"
        except asyncio.CancelledError:
            # Mirrors the real transport, preserving partial evidence on close.
            status = "cancelled"
        finally:
            cls.active -= 1
        output = json.dumps(spec.expected) if spec.scorer == "json" else str(spec.expected or "answer")
        if spec.scorer == "none":
            output = ("blue [END] green" if spec.fixture_id.startswith("limits.stop_control")
                      else "blue" if spec.fixture_id.startswith("limits.stop.") else "1 2")
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
            input_chars=spec.input_chars, elapsed_ms=elapsed, stream_end_ms=elapsed if status == "completed" else None,
            first_output_ms=first_output, last_output_ms=first_output + 2,
            answer_chunks=(spec.cancel_after_chunks or 2) if spec.stream else 0,
            answer_reads=2 if spec.stream else 0, first_read_answer_chars=max(1, len(output)//2),
            chunks=2, stream=spec.stream, stream_terminated=True,
            finish_reason=None if status == "cancelled" else "stop",
            client_cancelled_at_ns=time.time_ns() if spec.cancel_after_chunks and status == "cancelled" else None,
            requested_max_tokens=spec.max_tokens or 128,
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
                  traffic={"rates": [20, 40], "duration_seconds": .2},
                  limits=Limits(min_samples=2))
    return RunConfig(**(values | kwargs))


async def test_overlap_verdict_explains_plain_reason_and_rerun_settings(client):
    report = await runner.run_suite(config(checks=["correctness"], stream=False))
    check = next(c for c in report.checks if c.id == "correctness")
    assert check.status == "inconclusive"
    assert "streaming was disabled" in check.summary
    assert "could not be checked under concurrent load" in check.summary
    assert "OLLAMA_NUM_PARALLEL" in check.summary and "--concurrency 1" in check.summary
    assert "sequentially" in check.summary
    assert "--concurrency 2" not in check.summary and "--samples" not in check.summary


@pytest.mark.parametrize("check_id", ["correctness", "json"])
@pytest.mark.parametrize("concurrency", [1, 2, 4])
async def test_serialized_server_overlap_remedy_preserves_sequential_scope(client, monkeypatch, check_id, concurrency):
    original, lock = client.execute, asyncio.Lock()

    async def serialized(self, spec, **kwargs):
        queued = time.monotonic()
        async with lock:
            waited = (time.monotonic() - queued) * 1000
            record = await original(self, spec, **kwargs)
            return record.model_copy(update={field: getattr(record, field) + waited
                for field in ("elapsed_ms", "first_output_ms", "last_output_ms", "stream_end_ms")})

    monkeypatch.setattr(client, "execute", serialized)
    report = await runner.run_suite(config(checks=[check_id], structured_json=True, concurrency=concurrency))
    check = next(c for c in report.checks if c.id == check_id)
    assert check.status == ("pass" if concurrency == 1 else "inconclusive")
    assert all(r.score is True for r in report.requests)
    if concurrency > 1:
        assert "could not be checked under concurrent load" in check.summary
        assert "one request at a time" in check.summary
        assert "OLLAMA_NUM_PARALLEL" in check.summary
        assert "--concurrency 1" in check.summary and "sequentially" in check.summary
        assert "--concurrency 2" not in check.summary and "--samples" not in check.summary


@pytest.mark.parametrize("check_id", ["first_output", "generation"])
async def test_serial_timing_sample_remedy_changes_the_serial_sample_count(client, check_id):
    report = await runner.run_suite(config(checks=[check_id], samples=2,
        limits=Limits(min_samples=5, first_output_ms=1000, latency_ms=1000)))
    check = next(c for c in report.checks if c.id == check_id)
    assert check.status == "inconclusive"
    assert "--samples" in check.summary and "at least 5" in check.summary
    assert "idle load-generator" not in check.summary
    assert "concurrency of at least 2" not in check.summary
    if check_id == "first_output":
        assert "test_options.first_output.samples" not in check.summary
    else:
        assert "test_options.generation.samples" in check.summary
    enough = await runner.run_suite(config(checks=[check_id], samples=5,
        limits=Limits(min_samples=5, first_output_ms=1000, latency_ms=1000)))
    assert next(c for c in enough.checks if c.id == check_id).status == "pass"


async def test_serial_generation_measurement_remedy_does_not_raise_concurrency(client):
    report = await runner.run_suite(config(checks=["generation"], stream=False,
        limits=Limits(min_samples=2, stream_gap_ms=1000)))
    check = next(c for c in report.checks if c.id == "generation")
    assert check.status == "inconclusive"
    assert "stream: true" in check.summary and "buffering" in check.summary
    assert "concurrency of at least 2" not in check.summary


async def test_fixed_fixture_output_budget_remedy_does_not_raise_ineffective_ceiling(client, monkeypatch):
    original = client.execute

    async def reasoning_only(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        return record.model_copy(update={"output": "", "reasoning": "still thinking",
            "reasoning_tokens": spec.max_tokens, "completion_tokens": spec.max_tokens,
            "valid": False, "finish_reason": "length",
            "error": "reasoning exhausted the output budget before an answer"})

    monkeypatch.setattr(client, "execute", reasoning_only)
    report = await runner.run_suite(config(checks=["serving"], max_output_tokens=128))
    check = next(c for c in report.checks if c.id == "serving")
    assert check.status == "inconclusive"
    assert "increasing max_output_tokens alone will not" in check.summary
    assert "above 128" not in check.summary


async def test_missing_cap_usage_remedy_does_not_raise_output_budget(client, monkeypatch):
    original = client.execute

    async def no_cap_usage(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.fixture_id.startswith("limits.output_cap"):
            record.output_tokens = record.completion_tokens = None
        return record

    monkeypatch.setattr(client, "execute", no_cap_usage)
    report = await runner.run_suite(config(checks=["cancellation"]))
    check = next(c for c in report.checks if c.id == "cancellation")
    assert check.status == "inconclusive"
    assert "Output token usage" in check.summary
    assert "completion token usage" in check.summary
    assert "raise max_output_tokens" not in check.summary


async def test_fairness_overlap_remedy_targets_server_parallelism(client, monkeypatch):
    original, lock = client.execute, asyncio.Lock()

    async def serialized(self, spec, **kwargs):
        queued = time.monotonic()
        async with lock:
            waited = (time.monotonic() - queued) * 1000
            record = await original(self, spec, **kwargs)
            return record.model_copy(update={field: getattr(record, field) + waited
                for field in ("elapsed_ms", "first_output_ms", "last_output_ms", "stream_end_ms")})

    monkeypatch.setattr(client, "execute", serialized)
    report = await runner.run_suite(config(checks=["concurrency"],
        concurrent_workloads={"scenarios": ["streaming_fairness"]},
        limits=Limits(min_samples=2, fairness_max_ratio=3)))
    check = next(c for c in report.checks if c.id == "concurrency")
    assert check.status == "inconclusive"
    assert "enable parallel serving on the server" in check.summary
    assert "raising client concurrency alone cannot" in check.summary
    assert "--concurrency 1" not in check.summary


async def test_budget_verdict_has_real_plan_minimum(client):
    from giraffe.planning import request_plan

    configured = config(checks=["access", "serving", "correctness"], max_requests=1)
    report = await runner.run_suite(configured)
    minimum = request_plan(configured)["known_total"]
    check = next(c for c in report.checks if c.id == "correctness")
    assert check.status == "inconclusive"
    assert f"max_requests (or --max-requests) to at least {minimum}" in check.summary
    assert "select fewer checks" in check.summary


@pytest.mark.parametrize("answers", [False, True])
async def test_fairness_failure_cause_survives_public_summary(client, answers):
    client.bad_mixed_gap = True
    if answers:
        client.bad_target = "service"
    report = await runner.run_suite(config(checks=["concurrency"],
        concurrent_workloads={"scenarios": ["streaming_fairness"]},
        limits=Limits(min_samples=2, stream_gap_ms=50, fairness_max_ratio=3)))
    check = next(c for c in report.checks if c.id == "concurrency")
    assert check.status == "fail"
    assert "stream gap 500 ms > allowed 50 ms" in check.summary
    assert "To conclude," in check.summary
    assert "address the slowdown" in check.summary
    assert ("rerun after changing the model or its settings" in check.summary) == answers
    if answers:
        assert check.summary.index("check the model's answers") < check.summary.index("address the slowdown")


async def test_sustained_verdict_names_required_duration(client):
    client.delay = .03
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=.5,
        max_duration_seconds=.12, limits=Limits(min_samples=2, recovery_max_ratio=3)))
    check = next(c for c in report.checks if c.id == "recovery")
    assert check.status == "inconclusive"
    assert "0.5 seconds" in check.summary
    assert "2 successful" in check.summary


async def test_dead_endpoint_keeps_verdicts_and_reports_connection_cause(client, monkeypatch):
    async def disconnected(self, spec, **kwargs):
        return RequestRecord(id=uuid.uuid4().hex, target=self.target.name,
            fixture_id=spec.fixture_id, scenario=spec.scenario, check_ids=spec.check_ids,
            started_at="2026-10-19T00:00:00Z", status="failed", error="request failed (ConnectError)")

    monkeypatch.setattr(client, "execute", disconnected)
    report = await runner.run_suite(config(checks=["access", "serving", "capacity"]))
    assert report.overall == "fail"
    expected = "Could not connect to http://localhost:1234 (ConnectError); no checks could run"
    assert report.observations["summary"] == expected
    assert expected in next(c for c in report.checks if c.id == "capacity").summary
    assert next(c for c in report.checks if c.id == "access").status == "blocked"


async def test_timing_failure_summary_names_observed_limit_and_server_remedy(client):
    report = await runner.run_suite(config(checks=["first_output"], limits=Limits(min_samples=2, first_output_ms=1)))
    check = next(c for c in report.checks if c.id == "first_output")
    assert check.status == "fail"
    assert "first output 2 ms > allowed 1 ms" in check.summary
    assert "fix the first-output delay" in check.summary


async def test_serving_failure_summary_names_error_rate(client, monkeypatch):
    async def invalid(self, spec, **kwargs):
        return RequestRecord(id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="2026-10-19T00:00:00Z",
            status="failed", http_status=503, error="HTTP 503")

    monkeypatch.setattr(client, "execute", invalid)
    report = await runner.run_suite(config(checks=["serving"]))
    check = next(c for c in report.checks if c.id == "serving")
    assert check.status == "fail"
    assert "error rate 100.0% > allowed 0.0%" in check.summary
    assert "Could not connect" not in check.summary


async def test_wrong_answer_summary_has_expected_and_received(client):
    client.bad_target = "service"
    report = await runner.run_suite(config(checks=["correctness"]))
    check = next(c for c in report.checks if c.id == "correctness")
    assert check.status == "fail"
    assert "Expected:" in check.summary and 'received: "wrong"' in check.summary
    assert "correctness 0.0% < allowed 100.0%" in check.summary
    assert "fix the mismatch" in check.summary


async def test_all_unfinished_default_verdicts_offer_a_remedy(client):
    report = await runner.run_suite(config(checks=RunConfig.model_fields["checks"].get_default(call_default_factory=True),
        max_requests=1))
    assert all("To conclude," in check.summary for check in report.checks
               if check.status in {"fail", "inconclusive"})


def test_fairness_ratio_failure_names_metric_values():
    from giraffe.concurrent_workloads import consolidate_checks, execution_config

    configured = config(checks=["concurrency"], concurrent_workloads={"scenarios": ["streaming_fairness"]},
        limits=Limits(min_samples=2, fairness_max_ratio=3))
    state = runner._Run(execution_config(configured), None, None)
    state.observations["service"]["fairness"].update(pairs=2, overlaps=2)
    for scenario in ("fairness_baseline", "fairness_short", "fairness_long"):
        for index in range(2):
            state.records.append(RequestRecord(id=f"{scenario}-{index}", target="service",
                fixture_id="fairness", scenario=scenario, check_ids=["fairness"],
                started_at="2026-10-19T00:00:00Z", status="completed", valid=True, score=True,
                elapsed_ms=20, first_output_ms=2, last_output_ms=10 if scenario == "fairness_short" else 4,
                answer_chunks=2, stream_end_ms=20))
    checks = consolidate_checks(state, configured, runner._checks(state, configured.targets[0]))
    check = next(c for c in checks if c.id == "concurrency")
    assert check.status == "fail"
    assert "short-stream decoding ratio 4 > allowed 3" in check.summary
    assert "address the slowdown" in check.summary


async def test_capacity_guidance_uses_duration_and_real_sample_gate(client):
    report = await runner.run_suite(config(checks=["capacity"], limits=Limits(min_samples=5, latency_ms=100)))
    check = next(c for c in report.checks if c.id == "capacity")
    assert check.status == "inconclusive"
    assert "traffic.duration_seconds of at least 0.25 seconds per rate" in check.summary
    assert "at least 5 scored completions" in check.summary


async def test_fairness_output_budget_names_actual_required_cap(client):
    report = await runner.run_suite(config(checks=["concurrency"], max_output_tokens=8,
        concurrent_workloads={"scenarios": ["streaming_fairness"]}))
    check = next(c for c in report.checks if c.id == "concurrency")
    assert check.status == "inconclusive"
    assert "max_output_tokens (or --max-output-tokens) to at least 60" in check.summary


async def test_versioned_recovery_prompt_has_stable_builtin_fingerprint(client):
    report = await runner.run_suite(config())
    assert report.manifest["fixture_pack"]["builtin_sha256"] == (
        "9af0ebdc853200070007d5c0c38cd9f4e9cf8f6f259b8a4b006e504c4a8eb70c")


@pytest.mark.parametrize("selected", [False, True])
async def test_resolved_default_tool_fixture_changes_only_selected_identity(monkeypatch, selected):
    from giraffe import tool_checks
    from giraffe.reporting import _settings_reasons

    settings = config(checks=["tools"] if selected else ["serving"], stream=False)
    baseline = await runner.run_suite(settings)
    original = tool_checks.default_tool_fixture

    def changed_fixture():
        fixture = original()
        fixture.messages[0].content += " Explain why you select this tool."
        return fixture

    monkeypatch.setattr(tool_checks, "default_tool_fixture", changed_fixture)
    current = await runner.run_suite(settings)
    assert (current.manifest["fixture_pack"] != baseline.manifest["fixture_pack"]) is selected
    assert ("Fixture pack identity differs." in _settings_reasons(current, baseline)) is selected


async def test_resolved_tool_modes_are_part_of_workload_identity(monkeypatch):
    from giraffe import tool_checks, workloads

    settings = config(checks=["tools"])
    baseline = await runner.run_suite(settings)
    monkeypatch.setattr(tool_checks, "resolved_tool_modes", lambda config: ["complete"])
    monkeypatch.setattr(workloads, "resolved_tool_modes", tool_checks.resolved_tool_modes)
    current = await runner.run_suite(settings)
    assert (baseline.manifest["fixture_pack"]["workload_fixture_sha256"]
            != current.manifest["fixture_pack"]["workload_fixture_sha256"])


def check(report, id, target="service"):
    if id == "fairness":
        from giraffe.models import CheckResult
        merged = next(c for c in report.checks if c.id == "concurrency" and c.target == target)
        return CheckResult.model_validate(merged.metrics["scenarios"]["streaming_fairness"])
    return next(c for c in report.checks if c.id == id and c.target == target)


async def test_bucket_window_expires_while_waiting_for_shared_concurrency(client):
    from giraffe.workloads import buckets
    from giraffe.concurrent_workloads import execution_config

    configuration = config(checks=["buckets"], concurrency=8,
                           buckets={"levels": [8], "samples_per_bucket": 2,
                                    "window_seconds": .01})
    run = runner._Run(execution_config(configuration), None, None)
    target = configuration.targets[0]
    for _ in range(configuration.concurrency):
        run.slots.get_nowait()

    async def release_competing_requests():
        await asyncio.sleep(.04)
        for _ in range(configuration.concurrency):
            run.release_slot()

    await asyncio.gather(buckets(run, target, client(target, configuration)),
                         release_competing_requests())
    assert not any(record.scenario == "bucket_c8_short_short" for record in run.records)
    assert run.records  # Later cells can still use the released slots.
    assert "buckets" in run.unfinished[target.name]
    assert run.slots.qsize() == configuration.concurrency


async def test_bucket_request_uses_remaining_window_after_queue_admission(client, monkeypatch):
    from giraffe.workloads import bucket_spec

    configuration = config(checks=["buckets"])
    run = runner._Run(configuration, None, None)
    target = configuration.targets[0]
    request = bucket_spec(configuration.buckets, "short", "short", "bucket", "buckets", 1)
    monkeypatch.setattr(client, "delay", 1)
    for _ in range(configuration.concurrency):
        run.slots.get_nowait()
    deadline = time.monotonic() + .2
    pending = asyncio.create_task(run.request(target, client(target, configuration), request,
                                             timeout_override=.2, deadline_override=deadline))
    await asyncio.sleep(.1)
    for _ in range(configuration.concurrency):
        run.release_slot()
    record = await pending
    assert record.status == "timeout"
    assert record.elapsed_ms < 150
    assert run.slots.qsize() == configuration.concurrency


@pytest.mark.parametrize("selected,allowed", [
    ("correctness", {"correctness_c2"}), ("generation", {"initial", "generation"}),
    ("json", {"json_c2"}), ("capacity", {"traffic_stage_0", "traffic_stage_1"}),
    ("gpu", set()),
])
async def test_selected_check_runs_only_its_workload(selected, allowed):
    report = await runner.run_suite(config(checks=[selected]))
    assert {record.scenario for record in report.requests} <= allowed
    assert all(set(record.check_ids) <= {selected} for record in report.requests)
    assert all(result.status == "skipped" for result in report.checks if result.id != selected)
    if selected != "gpu":
        assert report.requests
    if selected == "json":
        assert check(report, "json").required and check(report, "json").status == "pass"


async def test_disabled_custom_correctness_fixture_is_not_loaded():
    report = await runner.run_suite(config(checks=["generation"], custom_fixtures="/does-not-exist.json"))
    assert len(report.requests) == 3
    assert report.manifest["fixture_pack"]["custom_sha256"] is None


async def test_baseline_only_compares_selected_check_groups():
    from giraffe.reporting import compare_baseline

    report = await runner.run_suite(config(checks=["correctness"]))
    compared = compare_baseline(report, report.model_copy(deep=True))
    rows = compared.baseline["targets"][0]["comparisons"]
    assert rows and {row["check"] for row in rows} == {"correctness"}


async def test_disabled_access_timeout_does_not_change_first_output_prerequisite():
    report = await runner.run_suite(config(checks=["first_output"], test_options={
        "access": {"request_timeout_seconds": .001},
    }))
    assert all(record.status == "completed" for record in report.requests)


async def test_per_check_options_change_workload_and_verdict_without_changing_global_config():
    configured = config(checks=["generation", "correctness", "first_output"],
                        limits=Limits(first_output_ms=1, min_samples=2),
                        test_options={"generation": {"samples": 3, "max_output_tokens": 7},
                                      "correctness": {"samples": 4, "concurrency": 1},
                                      "first_output": {"limits": {"first_output_ms": 10}}})
    report = await runner.run_suite(configured)
    generation = [r for r in report.requests if r.scenario == "generation"]
    assert len(generation) == 3 and {r.requested_max_tokens for r in generation} == {7}
    assert len([r for r in report.requests if r.scenario == "correctness_c1"]) == 4
    assert check(report, "first_output").status == "pass"
    assert configured.max_output_tokens == 128 and configured.limits.first_output_ms == 1
    assert report.manifest["effective_checks"]["generation"]["samples"] == 3


async def test_access_timeout_override_is_used(client):
    client.delay = .05
    report = await runner.run_suite(config(checks=["access"],
                                          test_options={"access": {"request_timeout_seconds": .001}}))
    assert report.requests[0].status == "timeout"
    assert not report.requests[0].valid and report.requests[0].elapsed_ms < 20


async def test_local_drops_consume_budget_but_admitted_requests_drain(client):
    client.delay = .12
    report = await runner.run_suite(config(checks=["capacity"], max_requests=3, concurrency=1,
                                          traffic={"rates": [100], "duration_seconds": .1},
                                          limits=Limits(latency_ms=1000, min_samples=2)))
    stage = check(report, "capacity").metrics["traffic_stages"][0]
    assert report.observations["request_budget_used"] == 3
    assert stage["scheduled"] == 10
    assert stage["started"] == stage["completed"] == 1
    assert stage["dropped_local"] == 2 and stage["not_offered"] == 7
    assert stage["good_fraction"] == .1 and not stage["accepted"]
    assert report.requests[0].status == "completed" and client.active == 0


async def test_context_override_controls_declared_limit_and_fixture_sizes():
    report = await runner.run_suite(config(checks=["context"], context_limit=256,
                                          test_options={"context": {"context_limit": 1024}}))
    result = check(report, "context")
    assert result.metrics["declared_context_limit"] == 1024
    assert result.metrics["observed_max_input_tokens"] >= 1024 * .8
    assert not any(r.scenario in {"initial", "warm"} for r in report.requests)


async def test_recovery_honors_correctness_allowance_without_hiding_protocol_failure(client, monkeypatch):
    original = client.execute

    wrong_probe_ids = set()

    async def one_wrong_probe(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario == "recovery" and not wrong_probe_ids:
            record.output = "wrong answer"
            wrong_probe_ids.add(record.id)
        return record

    monkeypatch.setattr(client, "execute", one_wrong_probe)
    configured = config(checks=["recovery"], sustained_seconds=.1, test_options={
        "recovery": {"limits": {"min_correctness": .5, "latency_ms": 1000}},
    })
    report = await runner.run_suite(configured)
    result = check(report, "recovery")
    assert .5 <= result.metrics["accuracy"] < 1
    assert result.status == "pass"

    wrong_probe_ids.clear()

    async def broken_probe(self, spec, **kwargs):
        record = await one_wrong_probe(self, spec, **kwargs)
        if record.id in wrong_probe_ids:
            record.valid = False
            record.status = "failed"
        return record

    monkeypatch.setattr(client, "execute", broken_probe)
    report = await runner.run_suite(configured)
    result = check(report, "recovery")
    assert result.status == "fail" and result.metrics["error_rate"] > 0
    assert "Answer checks failed" not in result.summary


async def test_recovery_missing_answer_scores_cannot_pass(monkeypatch):
    original = runner.score_response

    def unscored_probe(spec, record):
        record = original(spec, record)
        if spec.scenario == "recovery":
            record.score = None
        return record

    monkeypatch.setattr(runner, "score_response", unscored_probe)
    report = await runner.run_suite(config(checks=["recovery"]))
    assert check(report, "recovery").status == "inconclusive"


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
    report = await runner.run_suite(config(checks=["serving", "gpu"], metrics=True, gpu_min_observation_seconds=0))
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


async def test_actual_mixed_overlap_and_stream_gap_violation(client, monkeypatch):
    original = client.execute
    async def overlapping_prefill(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario == "fairness_long":
            record.first_output_ms = 0
        return record
    monkeypatch.setattr(client, "execute", overlapping_prefill)
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
    assert result.metrics["backend_cancellation"]["status"] == "unverified"
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
        assert source.traffic_class == "long_input"


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
    report = await runner.run_suite(config(checks=["capacity", "context"], context_limit=1024,
                                          traffic={"rates": [20, 40], "duration_seconds": .2,
                                                   "long_input_chars": 1024}))
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
    failed_fixture = ("sustained.copy.v3" if check_id == "recovery" else
                      "fairness.copy.v1" if check_id == "fairness" else "short.copy.v2")

    async def refused_copy(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.fixture_id == failed_fixture:
            result.output = "I cannot copy that sentence."
        return result

    monkeypatch.setattr(client, "execute", refused_copy)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        latency_ms=100, fairness_max_ratio=10, min_samples=2,
    )))
    result = check(report, check_id)
    assert result.status == "fail"
    assert "Answer checks failed" in result.summary
    assert failed_fixture in result.summary
    assert "warm-up" not in result.summary
    failure = next(f for f in result.metrics["answer_failures"]
                   if f["fixture_id"] == failed_fixture)
    assert failure["warmup_failed"] == 0
    assert failure["failed"] > 0
    if check_id == "fairness":
        assert "rerun after changing the model or its settings" in result.summary
        assert "address the slowdown" not in result.summary


async def test_answer_failure_details_do_not_hide_timing_failure(client, monkeypatch):
    original = client.execute
    client.bad_sustained = True

    async def refused_copy(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.fixture_id == "sustained.copy.v3":
            result.output = "I cannot copy that sentence."
        return result

    monkeypatch.setattr(client, "execute", refused_copy)
    report = await runner.run_suite(config(checks=["recovery"],
                                          limits=Limits(latency_ms=100, min_samples=2)))
    result = check(report, "recovery")
    assert result.status == "fail"
    assert "timing limits exceeded" in result.summary
    assert "sustained.copy.v3" in result.summary


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


async def test_sustained_timing_is_observational_when_recovery_is_fast(client):
    client.bad_sustained = True
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=.1,
                                          limits=Limits(latency_ms=100, min_samples=2)))
    result = check(report, "recovery")
    assert result.status == "pass", (result.summary, result.metrics["sustained_completion"])
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
    assert result.metrics["highest_acceptable_rate_rps"] is None
    stage = result.metrics["traffic_stages"][1]
    assert stage["missing_timing"] == stage["completed"] == 8


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
    assert {r.fixture_id for r in report.requests if r.scenario in {"limits", "stop_control"}} == {
        "limits.output_cap.v1", "limits.stop.v1", "limits.stop_control.v1"}
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
async def test_metrics_never_inherits_inference_credentials(monkeypatch, same_origin):
    import httpx

    seen = []
    monkeypatch.setenv("GIRAFFE_TEST_KEY", "local-test-key")
    actual_client = httpx.AsyncClient

    def respond(request):
        seen.append(request)
        return httpx.Response(200, text="DCGM_FI_DEV_GPU_UTIL 10\n")

    def mock_client(**kwargs):
        return actual_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics" if same_origin else "http://localhost:5555/metrics",
                    api_key_env="GIRAFFE_TEST_KEY")
    report = await runner.run_suite(config(targets=[target], checks=["gpu"], metrics=True, gpu_min_observation_seconds=0))
    assert 2 <= len(seen) <= 256
    assert all("Authorization" not in request.headers for request in seen)
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

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    report = await runner.run_suite(config(targets=[target], checks=["gpu"], metrics=True, gpu_min_observation_seconds=0))
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

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    report = await runner.run_suite(config(targets=[target], checks=["gpu"], metrics=True, gpu_min_observation_seconds=0))
    result = check(report, "gpu")
    assert result.status == "inconclusive"
    assert len(consumed) == 2 * len(result.metrics["snapshots"])  # 1 MiB retained + rejecting chunk per scrape.
    assert all(snapshot["status"] == "response_limit" for snapshot in result.metrics["snapshots"])


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
        elif spec.scenario == "fairness_long":
            result.first_output_ms = 0
        return result

    monkeypatch.setattr(client, "execute", no_mixed_gap)
    report = await runner.run_suite(config(checks=["fairness"],
                                          limits=Limits(stream_gap_ms=10, fairness_max_ratio=10,
                                                        min_samples=2)))
    result = check(report, "fairness")
    assert result.status == report.overall == "inconclusive"
    assert result.metrics["overlaps"] == 2
    assert result.metrics["missing_timing_metrics"] == ["stream_gap_ms"]


@pytest.mark.parametrize("scenario,check_id", [("traffic_stage_1", "capacity"),
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
        assert result.metrics["highest_acceptable_rate_rps"] == 20
        assert result.metrics["traffic_stages"][1]["correctness"] == 0
    else:
        assert result.metrics["accuracy"] == pytest.approx(2/3)


@pytest.mark.parametrize("check_id", ["capacity", "fairness"])
async def test_unscored_valid_load_is_inconclusive(monkeypatch, check_id):
    original = runner.score_response

    def omit_score(spec, record):
        result = original(spec, record)
        if spec.scenario in {"traffic_stage_1", "fairness_long"}:
            result.score = None
        return result

    monkeypatch.setattr(runner, "score_response", omit_score)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        latency_ms=1000, fairness_max_ratio=10, min_samples=2)))
    result = check(report, check_id)
    assert result.status == report.overall == "inconclusive"
    if check_id == "capacity":
        assert result.metrics["highest_acceptable_rate_rps"] == 20
        assert result.metrics["traffic_stages"][1]["unscored_completions"] == 8
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
        self.timing_health.windows.append({"start_ms": 0, "end_ms": 10000, "lag_ms": 500})
        await original(self, tasks)

    monkeypatch.setattr(runner._Run, "monitor", saturated)
    report = await runner.run_suite(config(checks=[check_id], limits=Limits(
        first_output_ms=1, latency_ms=1, stream_gap_ms=1, fairness_max_ratio=1.01, min_samples=2)))
    result = check(report, check_id)
    assert result.status == report.overall == "inconclusive"
    if check_id == "capacity":
        assert result.metrics["highest_acceptable_rate_rps"] is None


async def test_delay_outside_requests_does_not_disqualify_timing(monkeypatch):
    original = runner._checks

    def delayed(self, target):
        self.max_lag_ms = 500
        self.timing_health.windows[:] = [{"start_ms": 10000, "end_ms": 10550, "lag_ms": 500}]
        return original(self, target)

    monkeypatch.setattr(runner, "_checks", delayed)
    report = await runner.run_suite(config(checks=["first_output", "generation"],
        limits=Limits(first_output_ms=100, latency_ms=100, min_samples=2)))
    assert check(report, "first_output").status == "pass"
    assert check(report, "generation").status == "pass"
    assert report.observations["generator_max_lag_ms"] == 500
    assert not check(report, "generation").metrics["timing_quality"]["affected_request_ids"]


async def test_delay_in_generation_does_not_disqualify_earlier_first_output(monkeypatch):
    original = runner._checks

    def delayed(self, target):
        request = next(r for r in self.records if r.scenario == "generation")
        self.max_lag_ms = 500
        self.timing_health.windows[:] = [{
            "start_ms": request.dispatch_ms + request.first_output_ms + .1,
            "end_ms": request.completed_ms, "lag_ms": 500,
        }]
        return original(self, target)

    monkeypatch.setattr(runner, "_checks", delayed)
    report = await runner.run_suite(config(checks=["first_output", "generation"],
        limits=Limits(first_output_ms=100, latency_ms=100, min_samples=2)))
    assert check(report, "first_output").status == "pass"
    result = check(report, "generation")
    assert result.status == "inconclusive"
    assert result.metrics["timing_quality"]["affected_request_ids"]
    assert result.metrics["completed"] == 2  # Delayed samples are retained.


async def test_blocking_final_callback_is_captured_before_monitor_cancellation(client, monkeypatch):
    original = client.execute
    completed = 0

    async def execute(self, spec, **kwargs):
        nonlocal completed
        record = await original(self, spec, **kwargs)
        completed += 1
        if completed == 3:
            time.sleep(.18)
            record.elapsed_ms += 180
        return record

    monkeypatch.setattr(client, "execute", execute)
    report = await runner.run_suite(config(checks=["generation"], concurrency=1,
        limits=Limits(latency_ms=1000, min_samples=2)))
    health = report.observations["client_timing_health"]
    assert health["delayed_sample_count"] >= 1
    assert health["max_lag_ms"] > 100
    assert check(report, "generation").status == "inconclusive"
    assert len(report.requests) == 3 and report.requests[-1].elapsed_ms >= 180


async def test_delay_on_another_target_does_not_change_completed_target(monkeypatch):
    original = runner._checks

    def delayed(self, target):
        request = next(r for r in self.records if r.target == "second" and r.scenario == "generation")
        self.max_lag_ms = 500
        self.timing_health.windows[:] = [{"start_ms": request.dispatch_ms,
            "end_ms": request.completed_ms, "lag_ms": 500}]
        return original(self, target)

    monkeypatch.setattr(runner, "_checks", delayed)
    report = await runner.run_suite(config(checks=["generation"], targets=[
        Target(name=name, model="local", url="http://localhost:1234") for name in ["service", "second"]],
        limits=Limits(latency_ms=1000, min_samples=2)))
    assert check(report, "generation", "service").status == "pass"
    assert check(report, "generation", "second").status == "inconclusive"


async def test_client_dispatch_anchor_excludes_slow_progress_callback(monkeypatch):
    from giraffe.client import LLMClient
    from tests.fake_endpoint import FakeEndpoint

    monkeypatch.setattr(runner, "LLMClient", LLMClient)

    def progress(event):
        if event["event"] == "request_started":
            time.sleep(.02)

    with FakeEndpoint(first_token_delay=.001, chunk_delay=0) as endpoint:
        report = await runner.run_suite(config(checks=["generation"], targets=[
            Target(name="service", model="fake-model", url=endpoint.url)]), progress=progress)
    assert len(report.requests) == 3
    assert [r.scenario for r in report.requests] == ["initial", "generation", "generation"]
    assert all(r.timing_start_ms - r.dispatch_ms >= 20 for r in report.requests)
    assert all("_timing_started_monotonic" not in r.model_dump() for r in report.requests)


@pytest.mark.parametrize("check_id", ["capacity", "fairness", "recovery"])
async def test_generator_saturation_does_not_hide_known_answer_failure(client, monkeypatch, check_id):
    client.bad_target = "service"
    original = runner._Run.monitor

    async def saturated(self, tasks):
        self.max_lag_ms = 500
        self.timing_health.windows.append({"start_ms": 0, "end_ms": 10000, "lag_ms": 500})
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

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    state = runner._Run(config(targets=[target], max_duration_seconds=.03), None, None)
    started = time.monotonic()
    await runner._metrics(state, target, "before")
    snapshot = state.observations["service"]["metrics"][0]
    assert time.monotonic()-started < .1
    assert 0 < len(chunks) < 20
    assert snapshot["status"] == "connection_failure"
    assert "TimeoutError" in snapshot["reason"]
    assert len(snapshot["unavailable"]) == 5


async def test_metrics_skips_when_stop_already_requested(monkeypatch):
    import httpx
    def unexpected_request(**kwargs):
        pytest.fail("A stopped run must not open a metrics client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_request)
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    stop = asyncio.Event()
    stop.set()
    state = runner._Run(config(targets=[target]), None, stop)
    await runner._metrics(state, target, "before")
    assert "stopped" in state.observations["service"]["metrics"][0]["reason"]


async def test_gpu_freshness_override_survives_shared_telemetry_collection(monkeypatch):
    import httpx

    actual_client = httpx.AsyncClient
    timestamp = int((time.time() - 30) * 1000)
    metrics = "\n".join(f"{name} 1 {timestamp}" for name in [
        "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_GPU_TEMP",
        "DCGM_FI_DEV_CLOCKS_EVENT_REASONS", "DCGM_FI_DEV_XID_ERRORS",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(
        **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, text=metrics))))
    target = Target(name="service", model="local", url="http://localhost:1234",
                    metrics_url="http://localhost:1234/metrics")
    baseline = await runner.run_suite(config(targets=[target], checks=["gpu"], gpu_min_observation_seconds=0))
    strict = await runner.run_suite(config(targets=[target], checks=["gpu"], gpu_min_observation_seconds=0,
        test_options={"gpu": {"metrics_max_age_seconds": 5}}))
    assert not baseline.requests and not strict.requests
    assert check(baseline, "gpu").status == "inconclusive"
    assert check(baseline, "gpu").metrics["warnings"][0]["historical_xid"] == 1
    assert check(strict, "gpu").status == "inconclusive"
    assert len(check(strict, "gpu").metrics["unavailable_families"]) == 5
    assert strict.observations["targets"]["service"]["serving_telemetry"]["snapshots"]


async def test_capacity_and_opt_in_arrivals_share_budget_and_keep_distinct_reports():
    report = await runner.run_suite(config(
        checks=["capacity", "arrivals"],
        traffic={"rates": [20], "duration_seconds": .1},
        arrivals={"requests": 2, "requests_per_second": 100},
    ))
    assert {record.scenario for record in report.requests} == {"traffic_stage_0", "arrival"}
    assert check(report, "capacity").metrics["traffic_stages"]
    assert check(report, "arrivals").metrics["attempted"] == 2
    assert report.manifest["workload_selection"]["arrivals"] == "selected"
    assert report.manifest["effective_traffic"]["rates"] == [20]
    assert report.manifest["fixture_pack"]["workload_fixture_sha256"]
    assert report.observations["request_budget_used"] == len(report.requests)


async def test_saved_inactive_workload_drafts_do_not_change_executed_fixture_identity():
    baseline = await runner.run_suite(config(checks=["capacity"]))
    changed = await runner.run_suite(config(checks=["capacity"], sessions={"sessions": 3},
        forced_tool_diagnostic=True))
    assert baseline.manifest["fixture_pack"] == changed.manifest["fixture_pack"]
    assert changed.manifest["config"]["sessions"]["sessions"] == 3
    assert changed.manifest["config"]["forced_tool_diagnostic"] is True
    assert {r.scenario for r in changed.requests} == {"traffic_stage_0", "traffic_stage_1"}


async def test_selected_workload_changes_executed_fixture_identity():
    baseline = await runner.run_suite(config(checks=["arrivals"],
        arrivals={"requests": 2, "requests_per_second": 100}))
    changed = await runner.run_suite(config(checks=["arrivals"],
        arrivals={"requests": 3, "requests_per_second": 100}))
    assert (baseline.manifest["fixture_pack"]["workload_fixture_sha256"]
            != changed.manifest["fixture_pack"]["workload_fixture_sha256"])


def pack_config(**overrides):
    return config(**({
        "checks": ["capacity", "correctness"], "request_source": "request_pack",
        "request_pack": {"version": 1, "cases": [
            {"fixture_id": key, "messages": [{"role": "user", "content": key}],
             "scorer": "exact", "expected": key} for key in ("alpha", "beta")
        ]},
        "traffic": {"rates": [40], "duration_seconds": .1},
        "limits": Limits(latency_ms=1000, min_samples=2),
    } | overrides))


async def test_pack_control_is_serial_and_reused_for_correctness(client, monkeypatch):
    observed = []
    execute = client.execute

    async def capture(self, spec, **kwargs):
        observed.append(spec.model_dump())
        return await execute(self, spec, **kwargs)

    monkeypatch.setattr(client, "execute", capture)
    report = await runner.run_suite(pack_config(max_output_tokens=7))
    quiet = [r for r in report.requests if r.scenario == "pack_control"]
    loaded = [r for r in report.requests if r.traffic_stage is not None]
    assert len(quiet) == 2 and len(loaded) == 4
    assert all(r.overlap == 1 for r in quiet)
    assert all(r.requested_max_tokens == 7 for r in quiet + loaded)
    assert check(report, "correctness").metrics["attempted"] == 6
    assert check(report, "capacity").metrics["request_pack"]["control_status"] == "pass"
    assert report.manifest["request_pack"]
    assert {r.fixture_id for r in report.requests} == {"pack:alpha", "pack:beta"}


async def test_quiet_wrong_answer_does_not_filter_pack_or_stop_load(client, monkeypatch):
    execute = client.execute

    async def quiet_wrong(self, spec, **kwargs):
        record = await execute(self, spec, **kwargs)
        if spec.scenario == "pack_control" and spec.fixture_id == "pack:alpha":
            record.output = "wrong"
        return record

    monkeypatch.setattr(client, "execute", quiet_wrong)
    report = await runner.run_suite(pack_config(stop_after_errors=1))
    result = check(report, "capacity")
    assert result.status == "fail"
    assert result.metrics["traffic_stages"][0]["accepted"]
    assert result.metrics["highest_acceptable_rate_rps"] == 40
    assert result.metrics["request_pack"]["control_status"] == "fail"
    assert result.metrics["traffic_stages"][0]["case_counts"]["alpha"]["correct"] == 2
    assert report.abort_reason is None


async def test_correctness_only_pack_uses_quiet_and_concurrent_pass_and_case_coverage_floor():
    report = await runner.run_suite(pack_config(checks=["correctness"], samples=20,
                                               limits=Limits(min_samples=5)))
    result = check(report, "correctness")
    assert len(report.requests) == 4 and result.status == "pass"
    assert not result.metrics.get("low_confidence", False)
    assert result.metrics["request_pack"]["coverage_complete"]


async def test_pack_quiet_budget_shortfall_prevents_capacity_claim():
    report = await runner.run_suite(pack_config(max_requests=1))
    result = check(report, "capacity")
    assert result.status == "inconclusive"
    assert result.metrics["highest_acceptable_rate_rps"] is None
    assert not result.metrics["request_pack"]["coverage_complete"]
    assert "every request-pack case" in result.summary
    assert "--samples" not in result.summary


@pytest.mark.parametrize("scorer", ["exact", "none"])
async def test_rare_case_missing_loaded_evidence_blocks_full_pack_claim(scorer):
    configured = pack_config()
    cases = configured.request_pack.model_dump(mode="json")["cases"]
    cases[0]["weight"], cases[1]["weight"] = 99, 1
    cases[1]["scorer"] = scorer
    if scorer == "none":
        cases[1]["expected"] = None
    configured = pack_config(request_pack={"version": 1, "cases": cases},
                             traffic={"rates": [20], "duration_seconds": .1})
    result = check(await runner.run_suite(configured), "capacity")
    stage = result.metrics["traffic_stages"][0]
    assert result.status == "inconclusive" and result.metrics["highest_acceptable_rate_rps"] is None
    assert stage["case_counts"]["beta"]["planned"] == 0
    assert not stage["coverage_complete"]
    assert stage["all_cases_scored"] is (scorer != "none")


async def test_pack_capacity_missing_stream_gap_timing_stays_inconclusive():
    report = await runner.run_suite(pack_config(stream=False,
        limits=Limits(latency_ms=1000, stream_gap_ms=100, min_samples=2)))
    result = check(report, "capacity")
    assert result.status == "inconclusive"
    assert result.metrics["traffic_stages"][0]["missing_timing"] == 4


async def test_pack_config_is_frozen_before_progress_can_mutate_caller():
    configured = pack_config()

    def mutate(event):
        if event["event"] == "run_started":
            configured.request_pack.cases[0].expected = "mutated"
            configured.request_pack.cases[0].messages[0].content = "mutated"

    report = await runner.run_suite(configured, progress=mutate)
    assert report.manifest["config"]["request_pack"]["cases"][0]["expected"] == "alpha"
    assert all(record.score for record in report.requests)


async def test_pack_target_quotas_include_quiet_and_loaded_requests():
    targets = [Target(name=name, model="local", url="http://localhost:1234") for name in ("one", "two")]
    report = await runner.run_suite(pack_config(targets=targets, max_requests=8))
    for target in ("one", "two"):
        records = [record for record in report.requests if record.target == target]
        assert len(records) == 4
        assert sum(record.scenario == "pack_control" for record in records) == 2
        stage = check(report, "capacity", target).metrics["traffic_stages"][0]
        assert stage["started"] == stage["not_offered"] == 2


def judged_pack_config(**overrides):
    return pack_config(**({
        "request_pack": {"version": 1, "cases": [
            {"fixture_id": "reference", "messages": [{"role": "user", "content": "Refund window?"}],
             "scorer": "judge", "expected": "Within 30 days."},
        ]},
        "judging": {"judges": [{"name": "reviewer", "url": "http://localhost:9999/v1", "model": "judge"}]},
    } | overrides))


def judge_summary(records, *, duration=0, stopped=None):
    scored = sum(record.score is not None for record in records)
    return {"eligible_requests": len(records), "scored_requests": scored,
            "passed_requests": sum(record.score is True for record in records),
            "failed_requests": sum(record.score is False for record in records),
            "inconclusive_requests": len(records) - scored,
            "judge_calls_total": len(records), "judge_calls_completed": scored,
            "judge_calls_errors": 0, "ties": 0, "duration_seconds": duration, "stop_reason": stopped}


async def test_judging_waits_for_every_target_telemetry_and_monitor(client, monkeypatch):
    from giraffe import judging, telemetry

    closed_telemetry, monitor_done = set(), asyncio.Event()
    original_scrape, original_monitor = telemetry.scrape, runner._Run.monitor

    async def scrape(run, target, phase, **kwargs):
        result = await original_scrape(run, target, phase, **kwargs)
        if phase == "not collected":
            closed_telemetry.add(target.name)
        return result

    async def monitor(self, tasks):
        try:
            await original_monitor(self, tasks)
        finally:
            monitor_done.set()

    snapshots = []

    async def evaluate(config, records, specs, *, stop_event=None, progress=None):
        assert set(client.closed) == closed_telemetry == {"one", "two"}
        assert monitor_done.is_set() and client.active == 0
        snapshots.extend((r.id, r.dispatch_ms, r.completed_ms, r.elapsed_ms) for r in records)
        progress({"event": "judging_started", "judge_calls_total": len(records), "judge_calls_completed": 0})
        await asyncio.sleep(.12)
        for record in records:
            record.score = record.scenario == "pack_control"
        progress({"event": "judging_finished", "judge_calls_total": len(records),
                  "judge_calls_completed": len(records)})
        return judge_summary(records, duration=.12)

    monkeypatch.setattr(telemetry, "scrape", scrape)
    monkeypatch.setattr(runner._Run, "monitor", monitor)
    monkeypatch.setattr(judging, "judge_records", evaluate)
    events = []
    targets = [Target(name=name, model="local", url="http://localhost:1234") for name in ("one", "two")]
    report = await runner.run_suite(judged_pack_config(targets=targets, concurrency=8, overlap_models=True),
                                    progress=events.append)
    assert snapshots == [(r.id, r.dispatch_ms, r.completed_ms, r.elapsed_ms) for r in report.requests]
    assert len(report.requests) == report.observations["request_budget_used"] == 10
    assert report.observations["judging"]["duration_seconds"] == .12
    assert report.observations["generator_max_lag_ms"] < 100
    for target in ("one", "two"):
        result = check(report, "capacity", target)
        stage = result.metrics["traffic_stages"][0]
        assert result.status == "fail" and stage["status"] == "fail"
        assert stage["case_counts"]["reference"]["scored"] == 4
        assert stage["case_counts"]["reference"]["correct"] == 0
        assert stage["scored_subset_correct_goodput_rps"] == 0 < stage["valid_throughput_rps"]
        assert check(report, "correctness", target).status == "fail"
    kinds = [event["event"] for event in events]
    assert max(i for i, kind in enumerate(kinds) if kind == "request_completed") < kinds.index("judging_started")
    assert kinds[-1] == "run_finished"


async def test_missing_judgments_do_not_become_serving_errors(monkeypatch):
    from giraffe import judging

    async def evaluate(config, records, specs, **kwargs):
        return judge_summary(records, stopped="phase_deadline")

    monkeypatch.setattr(judging, "judge_records", evaluate)
    report = await runner.run_suite(judged_pack_config(checks=["capacity", "correctness", "serving"]))
    assert check(report, "serving").status == "pass"
    assert check(report, "correctness").status == "inconclusive"
    result = check(report, "capacity")
    assert result.status == "inconclusive" and result.metrics["highest_acceptable_rate_rps"] is None
    assert result.metrics["traffic_stages"][0]["error_rate"] == 0
    assert not result.metrics["traffic_stages"][0]["accepted"]
    assert report.abort_reason is None and all(record.valid for record in report.requests)


async def test_inference_deadline_does_not_skip_completed_answer_judging(monkeypatch):
    from giraffe import judging

    evaluated = []

    async def evaluate(config, records, specs, **kwargs):
        evaluated.extend(record for record in records if record.valid)
        for record in evaluated:
            record.score = True
        return judge_summary(evaluated)

    monkeypatch.setattr(judging, "judge_records", evaluate)
    report = await runner.run_suite(judged_pack_config(max_duration_seconds=.035))
    assert report.abort_reason == "maximum run duration reached"
    assert evaluated and all(record.score for record in evaluated)
    assert report.observations["judging"]["scored_requests"] == len(evaluated)


async def test_user_stop_during_inference_skips_judging(monkeypatch):
    from giraffe import judging

    async def forbidden(*args, **kwargs):
        pytest.fail("Explicitly stopped inference must not start evaluation")

    monkeypatch.setattr(judging, "judge_records", forbidden)
    stop = asyncio.Event()

    def progress(event):
        if event["event"] == "request_completed":
            stop.set()

    report = await runner.run_suite(judged_pack_config(), stop_event=stop, progress=progress)
    assert report.abort_reason == "cancelled by user"
    assert "judging" not in report.observations
    assert all(record.score is None for record in report.requests)


@pytest.mark.parametrize("external_cancel", [False, True])
async def test_cancelling_evaluation_preserves_partial_report(monkeypatch, external_cancel):
    from giraffe import judging

    started, stopped = asyncio.Event(), asyncio.Event()

    async def evaluate(config, records, specs, *, stop_event=None, progress=None):
        records[0].score = True
        started.set()
        await stop_event.wait()
        stopped.set()
        return judge_summary(records, stopped="cancelled")

    monkeypatch.setattr(judging, "judge_records", evaluate)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_suite(judged_pack_config(), stop_event=stop))
    await asyncio.wait_for(started.wait(), 1)
    if external_cancel:
        task.cancel()
    else:
        stop.set()
    report = await asyncio.wait_for(task, 1)
    assert stopped.is_set() and report.abort_reason == "cancelled by user"
    assert report.observations["judging"]["scored_requests"] == 1
    assert report.observations["judging"]["inconclusive_requests"] == 4
    assert check(report, "correctness").status == "inconclusive"
    assert not check(report, "capacity").metrics["traffic_stages"][0]["accepted"]


@pytest.mark.parametrize("wrong_answers,expected_status", [(1, "inconclusive"), (3, "fail")])
async def test_saturated_capacity_preserves_only_proven_partial_judgment_failure(
    monkeypatch, wrong_answers, expected_status,
):
    from giraffe import judging

    original = runner._Run.monitor

    async def saturated(self, tasks):
        self.max_lag_ms = 500
        self.timing_health.windows.append({"start_ms": 0, "end_ms": 10000, "lag_ms": 500})
        await original(self, tasks)

    async def evaluate(config, records, specs, **kwargs):
        loaded = [record for record in records if record.traffic_stage is not None]
        for record in records:
            if record.scenario == "pack_control":
                record.score = True
        loaded[0].score = True
        for record in loaded[1:1 + wrong_answers]:
            record.score = False
        return judge_summary(records)

    monkeypatch.setattr(runner._Run, "monitor", saturated)
    monkeypatch.setattr(judging, "judge_records", evaluate)
    report = await runner.run_suite(judged_pack_config(
        traffic={"rates": [100], "duration_seconds": .1},
        limits=Limits(latency_ms=1, min_correctness=.8, min_samples=2),
    ))
    result = check(report, "capacity")
    stage = result.metrics["traffic_stages"][0]
    assert stage["completed"] == 10 and stage["scored"] == 1 + wrong_answers
    assert stage["timing_violations"] and stage["generator_limited"]
    assert stage["status"] == result.status == expected_status
    assert result.metrics["highest_acceptable_rate_rps"] is None


async def test_recursive_schema_does_not_abort_remaining_examples(client):
    configured = config(checks=["correctness"], concurrency=1, limits=Limits(min_samples=2),
                        request_source="request_pack", request_pack={"version": 1, "cases": [
                            {"fixture_id": "a-recursive", "messages": [{"role": "user", "content": "Return JSON"}],
                             "scorer": "json", "schema": {"$ref": "#"}},
                            {"fixture_id": "b-valid", "messages": [{"role": "user", "content": "Say Paris"}],
                             "scorer": "exact", "expected": "Paris"},
                        ]})
    report = await runner.run_suite(configured)
    assert len(report.requests) == 4
    first, second = sorted((r for r in report.requests if r.scenario == "pack_control"),
                           key=lambda record: record.fixture_id)
    assert first.valid and first.output == "null" and first.score is None
    assert first.evaluation_error == "schema_recursion"
    assert second.score is True
    assert first.evaluation.messages == [{"role": "user", "content": "Return JSON"}]
    assert second.evaluation.expected == "Paris"
    assert check(report, "correctness").status == "inconclusive"
    assert report.abort_reason is None


async def test_live_request_events_keep_identity_and_exclude_response_bodies(client):
    events = []
    result = await runner.run_suite(config(checks=["access", "correctness"]), progress=events.append)
    started = [event for event in events if event["event"] == "request_started"]
    completed = [event for event in events if event["event"] == "request_completed"]
    assert len(started) == len(completed) == len(result.requests)
    assert {event["request_slot"] for event in started} == set(range(len(started)))
    assert {event["request_slot"] for event in completed} == set(range(len(started)))
    assert {event["request"]["id"] for event in completed} == {r.id for r in result.requests}
    by_id = {record.id: record for record in result.requests}
    for event in completed:
        record = event["request"]
        assert record["elapsed_ms"] == by_id[record["id"]].elapsed_ms
        assert record["score"] == by_id[record["id"]].score
        assert set(record) == {"id", "target", "fixture_id", "scenario", "status", "valid",
                               "score", "elapsed_ms", "first_output_ms", "check_ids"}
        assert event["budget_used"] >= event["attempted"]


@pytest.mark.parametrize("stream,concurrency", [(True, 1), (False, 1), (False, 2)])
async def test_fairness_prerequisite_remedy_names_missing_configuration(client, stream, concurrency):
    report = await runner.run_suite(config(checks=["fairness"], stream=stream, concurrency=concurrency))
    result = check(report, "fairness")
    assert result.status == "inconclusive"
    remedy = result.summary.split("To conclude, ")[-1]
    assert ("--concurrency 2 or more" in remedy) == (concurrency < 2)
    assert ("stream: true" in remedy) == (not stream)
    assert "completion token usage" not in remedy and "max_requests" not in remedy


@pytest.mark.parametrize("check_id,summary,options", [
    ("first_output", "Timing observed; no acceptance limit configured.", ["first_output_ms"]),
    ("generation", "Timing observed; no acceptance limit configured.",
     ["latency_ms", "stream_gap_ms", "min_output_tokens_per_second"]),
    ("fairness", "Mixed-traffic effect observed; no acceptance ratio configured.", ["fairness_max_ratio"]),
    ("mixed", "Mixed-load ratios measured; no acceptance ratio configured.", ["fairness_max_ratio"]),
    ("recovery", "Recovery observed; no acceptance limit configured.",
     ["recovery_max_ratio", "first_output_ms", "latency_ms", "stream_gap_ms", "min_output_tokens_per_second"]),
    ("arrivals", "Arrivals observed; no acceptance limit configured.",
     ["latency_ms", "first_output_ms", "stream_gap_ms", "min_output_tokens_per_second"]),
])
def test_no_acceptance_limit_remedy_only_names_limits_the_check_reads(check_id, summary, options):
    import re
    from giraffe.models import CheckResult

    result = CheckResult(id=check_id, target="service", title=check_id,
                         status="inconclusive", summary=summary, metrics={} if check_id in {"arrivals", "mixed"}
                         else {"reason_code": "missing_acceptance_limit"})
    summary = runner._verdict_summary(result, config(), [], "Raise max_requests")
    assert set(re.findall(r"limits\.([a-z_]+)", summary)) == set(options)


def test_timing_client_delay_remedy_uses_quality_instead_of_summary_wording():
    from giraffe.models import CheckResult

    result = CheckResult(id="generation", target="service", title="Generation", status="inconclusive",
                         summary="Timing attribution is uncertain.",
                         metrics={"reason_code": "client_delayed", "timing_quality": {"status": "client_delayed"}})
    summary = runner._verdict_summary(result, config(), [], "Raise max_requests")
    assert "idle load-generator" in summary
    assert "enable streaming" not in summary


@pytest.mark.parametrize("reason,remedy", [
    ("missing_acceptance_limit", "set limits.latency_ms"),
    ("insufficient_timing_samples", "set samples (or --samples)"),
])
def test_timing_remedy_uses_reason_code_with_reworded_summary(reason, remedy):
    from giraffe.models import CheckResult

    result = CheckResult(id="generation", target="service", title="Generation", status="inconclusive",
                         summary="Streaming evidence is inconclusive.", metrics={"reason_code": reason,
                                  "timing_quality": {"status": "client_delayed"}})
    assert remedy in runner._verdict_summary(result, config(), [], "Raise max_requests")


async def test_disconnected_sustained_endpoint_aborts_and_keeps_partial_evidence(client, monkeypatch):
    original = client.execute

    async def silent(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario == "sustained":
            record.status, record.valid = "failed", False
            record.http_status, record.output, record.chunks = None, "", 0
            record.error = "request failed (ConnectError)"
        return record

    monkeypatch.setattr(client, "execute", silent)
    events = []
    settings = config(checks=["recovery", "sessions"], sustained_seconds=1, max_requests=500,
                      stop_after_errors=500, unresponsive_after_failures=3, unresponsive_for_seconds=.03)
    started = time.monotonic()
    report = await runner.run_suite(settings, progress=events.append)
    assert time.monotonic() - started < .3
    assert report.abort_reason.startswith("Endpoint service unresponsive:")
    assert any(r.valid for r in report.requests)
    assert any(r.status == "failed" for r in report.requests)
    assert check(report, "sessions").status != "pass"
    assert any(e.get("abort_reason") == report.abort_reason for e in events)


async def test_run_deadline_during_readiness_is_reported_before_monitor_tick(client, monkeypatch):
    async def restart(run, target):
        run.observations[target.name]["restart"] = {
            "status": "hook_completed", "started_monotonic": time.monotonic()}
        return True

    async def not_ready(self, deadline):
        await asyncio.sleep(1)
        return False

    monkeypatch.setattr(runner, "_restart", restart)
    monkeypatch.setattr(client, "ready", not_ready)
    report = await runner.run_suite(config(max_duration_seconds=.02, restart_target="service"))
    assert report.abort_reason == "maximum run duration reached"
    assert not report.requests
    assert check(report, "serving").status != "pass"


def mock_inference_transport(monkeypatch, handler):
    import httpx
    from giraffe.client import LLMClient

    actual_client = httpx.AsyncClient
    monkeypatch.setattr(runner, "LLMClient", LLMClient)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(
        **kwargs, transport=httpx.MockTransport(handler)))


def endpoint_answer(request):
    import httpx
    from tests.fake_endpoint import FakeEndpoint

    payload = json.loads(request.content)
    answer = FakeEndpoint._answer("\n".join(m.get("content") or "" for m in payload["messages"]))
    return httpx.Response(200, json={"choices": [{"message": {"content": answer},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2}})


@pytest.mark.parametrize("fault", ["refused", "reset", "silent"])
async def test_endpoint_outage_through_transport_stops_and_saves_records(monkeypatch, tmp_path, fault):
    import httpx
    from giraffe.reporting import write_report

    calls, active = 0, 0

    async def handler(request):
        nonlocal calls, active
        calls += 1
        if calls <= 2:
            return endpoint_answer(request)
        active += 1
        try:
            if fault == "silent":
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(.008)
                raise (httpx.ConnectError("refused") if fault == "refused" else httpx.ReadError("reset"))
        finally:
            active -= 1

    mock_inference_transport(monkeypatch, handler)
    events = []
    started = time.monotonic()
    report = await runner.run_suite(config(checks=["serving", "sessions"], samples=50,
        max_requests=500, stop_after_errors=500, stream=False, request_timeout_seconds=.015,
        unresponsive_after_failures=3, unresponsive_for_seconds=.025), progress=events.append)
    assert time.monotonic() - started < .5
    assert active == 0
    assert report.abort_reason.startswith("Endpoint service unresponsive:")
    assert report.overall != "pass" and check(report, "sessions").status != "pass"
    assert sum(r.valid for r in report.requests) == 2
    failures = [r for r in report.requests if r.status in {"timeout", "failed"}]
    assert len(failures) >= 3 and all(r.score is None and r.error for r in failures)
    assert any(e["event"] == "run_stopping" and e["abort_reason"] == report.abort_reason for e in events)
    json_path, html_path = write_report(report, tmp_path)
    saved = json.loads(json_path.read_text())
    assert saved["abort_reason"] == report.abort_reason
    assert len(saved["requests"]) == len(report.requests)
    assert saved["observations"]["targets"]["service"]["endpoint_health"]["consecutive_failures"] >= 3
    assert report.abort_reason in html_path.read_text()


@pytest.mark.parametrize("failures,quiet_seconds,slow", [(1, .005, 0), (2, .005, 0), (3, 1, 0), (0, .005, .03)])
async def test_transient_failures_short_burst_and_slow_request_do_not_abort(monkeypatch, failures, quiet_seconds, slow):
    import httpx

    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if 2 <= calls < failures + 2:
            await asyncio.sleep(.006)
            raise httpx.ConnectError("transient")
        if calls == 2 and slow:
            await asyncio.sleep(slow)
        return endpoint_answer(request)

    mock_inference_transport(monkeypatch, handler)
    report = await runner.run_suite(config(samples=6, stream=False, stop_after_errors=500,
        unresponsive_after_failures=3, unresponsive_for_seconds=quiet_seconds))
    assert report.abort_reason is None
    assert len(report.requests) == 7 and report.requests[-1].valid
    assert report.observations["targets"]["service"]["endpoint_health"]["consecutive_failures"] == 0


async def test_http_error_responses_reset_unresponsiveness(monkeypatch):
    import httpx

    async def handler(request):
        await asyncio.sleep(.006)
        return httpx.Response(503, json={"error": "busy"})

    mock_inference_transport(monkeypatch, handler)
    report = await runner.run_suite(config(samples=5, stream=False, stop_after_errors=500,
        unresponsive_after_failures=1, unresponsive_for_seconds=.001))
    assert report.abort_reason is None
    assert len(report.requests) == 6 and all(r.http_status == 503 for r in report.requests)
    assert report.observations["targets"]["service"]["endpoint_health"]["consecutive_failures"] == 0


async def test_silence_policy_cancels_capacity_drain_even_after_offer_budget_is_full(monkeypatch):
    import httpx

    calls, active = 0, 0

    async def handler(request):
        nonlocal calls, active
        calls += 1
        active += 1
        try:
            if calls <= 2:
                await asyncio.sleep(.001)
                raise httpx.ConnectError("down")
            await asyncio.sleep(1)
        finally:
            active -= 1

    mock_inference_transport(monkeypatch, handler)
    started = time.monotonic()
    report = await runner.run_suite(config(checks=["capacity"], max_requests=8,
        traffic={"rates": [500], "duration_seconds": .1, "drain_timeout_seconds": 1},
        unresponsive_after_failures=2, unresponsive_for_seconds=.025))
    assert time.monotonic() - started < .5
    assert active == 0 and report.abort_reason.startswith("Endpoint service unresponsive:")
    assert any(r.status == "cancelled" for r in report.requests)
    assert all(r.score is None for r in report.requests)
    assert check(report, "capacity").status != "pass"
    stage = report.observations["targets"]["service"]["capacity"][0]
    assert stage["status"] != "pass"


@pytest.mark.parametrize("intent", ["deadline_probe", "phase_deadline", "cancel_ms", "cancel_chunks", "user_stop"])
async def test_intentional_no_response_timeouts_do_not_trigger_watchdog(client, monkeypatch, intent):
    from giraffe.models import RequestSpec

    async def silent(self, spec, **kwargs):
        await asyncio.sleep(.005)
        return RequestRecord(id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now", status="timeout",
            error="request deadline exceeded")

    monkeypatch.setattr(client, "execute", silent)
    settings = config(stop_after_errors=500, unresponsive_after_failures=1, unresponsive_for_seconds=.001)
    state = runner._Run(settings, None, None)
    target = settings.targets[0]
    spec = RequestSpec(fixture_id="intent", scenario="client_deadline", check_ids=["cancellation"],
        messages=[{"role": "user", "content": "hello"}],
        **({"cancel_after_ms": 1} if intent == "cancel_ms" else
           {"cancel_after_chunks": 2} if intent == "cancel_chunks" else {}))
    arguments = ({"timeout_override": .02} if intent == "deadline_probe" else
                 {"deadline_override": time.monotonic() + .02} if intent == "phase_deadline" else {})
    if intent == "user_stop":
        async def stopped(self, spec, **kwargs):
            state.stop.set()
            return await silent(self, spec, **kwargs)
        monkeypatch.setattr(client, "execute", stopped)
    async with client(target, settings) as transport:
        record = await state.request(target, transport, spec, **arguments)
    assert record is not None
    assert state.endpoint_health[target.name]["consecutive_failures"] == 0
    assert not state.reason or "unresponsive" not in state.reason


async def test_endpoint_health_is_per_target_and_starts_at_first_attempt():
    targets = [Target(name=name, model="test", url="http://localhost") for name in ("silent", "active")]
    state = runner._Run(config(targets=targets, unresponsive_after_failures=2,
                               unresponsive_for_seconds=.005), None, None)
    await asyncio.sleep(.01)
    state.endpoint_attempt("silent")
    state.endpoint_failure("silent", "first")
    state.endpoint_failure("silent", "second")
    assert state.stop_reason() is None
    state.endpoint_response("active")
    await asyncio.sleep(.01)
    assert state.stop_reason("silent").startswith("Endpoint silent unresponsive:")
    assert state.stop_reason() is None


@pytest.mark.parametrize("phase,settings", [
    ("initial", {}), ("warm", {}), ("context_calibration", {"checks": ["context"]}),
    ("bucket_", {"checks": ["concurrency"], "concurrent_workloads": {"scenarios": ["size_matrix"]}}),
    ("stress_", {"checks": ["concurrency"], "concurrent_workloads": {"scenarios": ["token_stress"],
        "stress_cases": [{"name": "long_input_summary", "input_tokens": 128, "output_tokens": 50}]}}),
    ("session_", {"checks": ["sessions"], "context_limit": 4096}),
    ("traffic_stage_", {"checks": ["capacity"], "traffic": {"rates": [100], "duration_seconds": .1,
                                                           "drain_timeout_seconds": 1}}),
])
async def test_run_deadline_bounds_each_target_traffic_phase(client, monkeypatch, phase, settings):
    original = client.execute
    entered = []

    async def slow_phase(self, spec, **kwargs):
        if spec.scenario.startswith(phase):
            entered.append(spec.scenario)
            await asyncio.sleep(1)
        return await original(self, spec, **kwargs)

    monkeypatch.setattr(client, "execute", slow_phase)
    started = time.monotonic()
    report = await runner.run_suite(config(max_duration_seconds=.045, **settings))
    assert entered, [r.scenario for r in report.requests]
    assert time.monotonic() - started < .25
    assert report.abort_reason == "maximum run duration reached"
    interrupted = [r for r in report.requests if r.scenario in entered]
    assert interrupted and all(r.status == "cancelled" and r.score is None for r in interrupted)
    assert report.overall != "pass"


async def test_slot_wait_is_bounded_by_run_deadline_without_monitor(client):
    from giraffe.models import RequestSpec

    settings = config(concurrency=1, max_duration_seconds=.01)
    state = runner._Run(settings, None, None)
    assert state.try_acquire_slot()
    spec = RequestSpec(fixture_id="queued", scenario="warm", check_ids=["serving"], messages=[])
    async with client(settings.targets[0], settings) as transport:
        record = await asyncio.wait_for(state.request(settings.targets[0], transport, spec), .1)
    assert record is None and state.stop_reason() == "maximum run duration reached"
    assert "serving" in state.unfinished["service"]


async def test_slow_cold_start_does_not_consume_watchdog_window(monkeypatch):
    import httpx

    polls = 0

    async def restart(run, target):
        run.observations[target.name]["restart"] = {
            "status": "hook_completed", "started_monotonic": time.monotonic()}
        return True

    async def handler(request):
        nonlocal polls
        if request.method == "GET":
            polls += 1
            if polls < 3:
                raise httpx.ConnectError("model is still loading")
            return httpx.Response(200)
        return endpoint_answer(request)

    monkeypatch.setattr(runner, "_restart", restart)
    mock_inference_transport(monkeypatch, handler)
    events = []
    report = await runner.run_suite(config(restart_target="service", stream=False,
        readiness_timeout_seconds=1, unresponsive_after_failures=1,
        unresponsive_for_seconds=.02), progress=events.append)
    assert polls == 3 and report.abort_reason is None
    assert check(report, "serving").status == "pass"
    health = report.observations["targets"]["service"]["endpoint_health"]
    assert health["first_attempt_seconds"] >= .3
    assert health["consecutive_failures"] == 0
    assert not any(e["event"] in {"target_stopping", "run_stopping"} for e in events)


async def test_run_deadline_terminates_restart_hook():
    import sys

    target = Target(name="service", model="test", url="http://localhost",
        restart_command=[sys.executable, "-c", "import time; time.sleep(2)"])
    started = time.monotonic()
    report = await runner.run_suite(config(targets=[target], restart_target="service", max_duration_seconds=.03))
    assert time.monotonic() - started < .3
    assert report.abort_reason == "maximum run duration reached"
    assert not report.requests


async def test_monitor_abort_during_target_cleanup_keeps_duration_reason(monkeypatch):
    async def metrics(run, target, phase):
        if phase == "end":
            await asyncio.sleep(1)

    monkeypatch.setattr(runner, "_metrics", metrics)
    report = await runner.run_suite(config(checks=["gpu"], max_duration_seconds=.02))
    assert report.abort_reason == "maximum run duration reached"


async def test_repeated_sse_headers_without_body_abort_hung_engine(monkeypatch):
    import httpx
    from tests.test_client import Chunks

    streams = []

    def handler(request):
        stream = Chunks([1.0])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    mock_inference_transport(monkeypatch, handler)
    started = time.monotonic()
    report = await runner.run_suite(config(samples=50, max_requests=100, stop_after_errors=100,
        request_timeout_seconds=.015, unresponsive_after_failures=3, unresponsive_for_seconds=.025))
    assert time.monotonic() - started < .5
    assert report.abort_reason and "unresponsive" in report.abort_reason
    assert len(report.requests) >= 3
    assert all(r.http_status == 200 and r.status == "timeout" and r.score is None for r in report.requests)
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("overlap", [False, True])
async def test_silent_target_stops_independently_and_healthy_target_finishes(monkeypatch, tmp_path, overlap):
    import httpx
    from giraffe.reporting import write_report
    from tests.test_client import Chunks

    streams = []

    async def handler(request):
        if request.url.host == "silent":
            stream = Chunks([1.0])
            streams.append(stream)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        await asyncio.sleep(.01)
        return endpoint_answer(request)

    mock_inference_transport(monkeypatch, handler)
    targets = [Target(name=name, model="test", url="http://" + name) for name in ("silent", "healthy")]
    events = []
    report = await runner.run_suite(config(targets=targets, overlap_models=overlap, stream=False,
        checks=["serving"], samples=8, stop_after_errors=100, request_timeout_seconds=.025,
        unresponsive_after_failures=2, unresponsive_for_seconds=.03), progress=events.append)
    assert report.abort_reason is None
    assert report.overall != "pass"
    assert check(report, "serving", "silent").status != "pass"
    assert check(report, "serving", "healthy").status == "pass"
    assert len([r for r in report.requests if r.target == "healthy"]) == 9
    assert all(stream.closed for stream in streams)
    reasons = report.observations["target_abort_reasons"]
    assert list(reasons) == ["silent"] and "unresponsive" in reasons["silent"]
    assert any(e["event"] == "target_stopping" and e["target"] == "silent"
               and e["abort_reason"] == reasons["silent"] for e in events)
    assert not any(e["event"] == "run_stopping" for e in events)
    json_path, html_path = write_report(report, tmp_path)
    assert json.loads(json_path.read_text())["observations"]["target_abort_reasons"] == reasons
    assert reasons["silent"] in html_path.read_text()


@pytest.mark.parametrize("scenario", ["traffic_stage_0", "sustained", "context", "stress_calibration_case", "fairness_long"])
@pytest.mark.parametrize("fault", ["timeout", "connect"])
async def test_stress_watchdog_counts_connection_failures_but_not_overload_timeouts(client, monkeypatch, scenario, fault):
    from giraffe.models import RequestSpec

    async def unavailable(self, spec, **kwargs):
        await asyncio.sleep(.003)
        return RequestRecord(id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now",
            status="timeout" if fault == "timeout" else "failed",
            error="request deadline exceeded" if fault == "timeout" else "request failed (ConnectError)")

    monkeypatch.setattr(client, "execute", unavailable)
    settings = config(stop_after_errors=100, unresponsive_after_failures=2, unresponsive_for_seconds=.001)
    state = runner._Run(settings, None, None)
    spec = RequestSpec(fixture_id="stress", scenario=scenario, check_ids=[], messages=[])
    async with client(settings.targets[0], settings) as transport:
        for _ in range(4):
            await state.request(settings.targets[0], transport, spec)
    reason = state.stop_reason("service")
    if fault == "timeout":
        assert reason is None and len(state.records) == 4
    else:
        assert "unresponsive" in reason and len(state.records) == 2


@pytest.mark.parametrize("disabled", [0, None])
async def test_watchdog_can_be_disabled(monkeypatch, disabled):
    import httpx

    def handler(request):
        raise httpx.ConnectError("offline")

    mock_inference_transport(monkeypatch, handler)
    report = await runner.run_suite(config(samples=6, stop_after_errors=100,
        unresponsive_after_failures=disabled, unresponsive_for_seconds=.001))
    assert len(report.requests) == 7 and report.abort_reason is None
    assert not report.observations["target_abort_reasons"]


@pytest.mark.parametrize("overlap", [False, True])
async def test_target_outage_cancels_only_its_capacity_requests(monkeypatch, overlap):
    import httpx
    from tests.test_client import Chunks

    silent_calls, healthy_calls, streams = 0, 0, []

    async def handler(request):
        nonlocal silent_calls, healthy_calls
        if request.url.host == "silent":
            silent_calls += 1
            if silent_calls <= 2:
                await asyncio.sleep(.001)
                raise httpx.ConnectError("offline")
            stream = Chunks([1.0])
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        healthy_calls += 1
        await asyncio.sleep(.005)
        return endpoint_answer(request)

    mock_inference_transport(monkeypatch, handler)
    targets = [Target(name=name, model="test", url="http://" + name) for name in ("silent", "healthy")]
    report = await runner.run_suite(config(targets=targets, overlap_models=overlap, stream=False,
        checks=["capacity"], concurrency=4, max_requests=100, stop_after_errors=100,
        traffic={"rates": [100], "duration_seconds": .15, "drain_timeout_seconds": 1},
        unresponsive_after_failures=2, unresponsive_for_seconds=.04))
    assert report.abort_reason is None and list(report.observations["target_abort_reasons"]) == ["silent"]
    assert streams and all(stream.closed for stream in streams)
    interrupted = [r for r in report.requests if r.target == "silent" and r.status == "cancelled"]
    assert interrupted and all(r.score is None and "unresponsive" in r.error for r in interrupted)
    healthy = [r for r in report.requests if r.target == "healthy"]
    assert healthy_calls == len(healthy) == 15
    assert all(r.status == "completed" and r.valid for r in healthy)
    assert report.observations["targets"]["healthy"]["endpoint_health"]["abort_reason"] is None


@pytest.mark.parametrize("overlap", [False, True])
async def test_run_reason_is_set_once_every_target_is_unresponsive(monkeypatch, overlap):
    import httpx

    async def handler(request):
        await asyncio.sleep(.006)
        raise httpx.ConnectError("offline")

    mock_inference_transport(monkeypatch, handler)
    targets = [Target(name=name, model="test", url="http://" + name) for name in ("first", "second")]
    report = await runner.run_suite(config(targets=targets, overlap_models=overlap,
        unresponsive_after_failures=1, unresponsive_for_seconds=.001))
    assert set(report.observations["target_abort_reasons"]) == {"first", "second"}
    assert "Endpoint first unresponsive" in report.abort_reason
    assert "Endpoint second unresponsive" in report.abort_reason
    assert report.overall != "pass"


@pytest.mark.parametrize("stream", [False, True])
async def test_serving_transport_diagnostics_do_not_classify_requests_as_workloads(monkeypatch, tmp_path, stream):
    import httpx
    from giraffe.reporting import write_report
    from tests.test_client import Chunks, event

    def handler(request):
        response = endpoint_answer(request)
        if not stream:
            return response
        answer = response.json()["choices"][0]["message"]["content"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            stream=Chunks([event(answer, finish="stop"), b"data: [DONE]\n\n"]))

    mock_inference_transport(monkeypatch, handler)
    report = await runner.run_suite(config(checks=["serving"], stream=stream,
        arrivals={}, prefix={}, buckets={}, sessions={}, consistency={}, tool_calling=True))
    assert {r.scenario for r in report.requests} == {"initial", "warm"}
    assert all(r.valid for r in report.requests)
    assert not any(r.workload for r in report.requests)
    assert all(r.response_body_bytes > 0 for r in report.requests)
    json_path, _ = write_report(report, tmp_path)
    saved = json.loads(json_path.read_text())["requests"]
    assert all(not r["workload"] and r["response_body_bytes"] > 0 for r in saved)
