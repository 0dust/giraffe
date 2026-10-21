"""Known duration components and optional coverage limits share the actual workload."""

import math

import pytest

from giraffe.fixtures import builtin_fixtures
from giraffe.latency import input_bucket, latency_summary
from giraffe.models import ConfigDraft, RunConfig
from giraffe.planning import request_plan


TARGET = {"name": "service", "url": "http://endpoint.invalid/v1", "model": "model"}


def config(**updates):
    return RunConfig.model_validate({"targets": [TARGET], "checks": ["correctness"], **updates})


def tail(p50, p90, *, n=200):
    # Unit-test cohort with an empirical upper tail, as emitted by summaries v2.
    return {"n": n, "p50_ms": p50, "p90_ms": p90, "p99_ms": p90, "max_ms": p90,
            "quantiles_ms": [p50] * 51 + [p50 + (p90 - p50) * index / 40 for index in range(1, 41)] + [p90] * 10,
            "cohort": "observed-run", "batch_timing_coverage": 1}


def observations(configuration, check="correctness", *, p50=10, p90=20, concurrency=None):
    options = configuration.effective_check(check)
    effective = configuration.model_copy(update={key: value for key, value in options.items() if key != "limits"})
    fixtures = builtin_fixtures(effective, check_id=check)[check]
    concurrency = concurrency or options.get("concurrency", 1)
    return {target.name: [{"family": check, "concurrency": concurrency, "stream": spec.stream,
                           "output_cap": spec.max_tokens, "input_bucket": input_bucket(spec.input_chars),
                           **tail(p50, p90)} for spec in fixtures]
            for target in configuration.targets}


def test_observed_finite_batches_use_empirical_tail_and_effective_concurrency():
    configured = config(default_concurrency=2, concurrency=40,
                        test_options={"correctness": {"samples": 9, "request_timeout_seconds": 60}})
    plan = request_plan(configured, observations=observations(configured, p50=100, p90=400))
    assert plan["estimated_low_seconds"] == .492
    assert plan["estimated_high_seconds"] == 1.896
    assert plan["estimated_seconds"] == 1.194
    assert plan["confidence"] == "low"
    assert plan["unknown_time_components"] == []
    component, = plan["time_components"]
    assert component["concurrency"] == 2
    assert component["requests"] == 9
    assert plan["estimate_exclusions"] == ["setup/connection", "AI evaluation", "report generation"]
    assert plan["time_estimate_assumptions"] == [
        "Finite batches use the expected maximum from comparable recorded distributions. A nominal 99% prediction "
        "interval combines batch variance and sample uncertainty across sequential groups, with a measured-skew "
        "correction on the upper tail. Medium confidence requires 150 samples from one run or at least 15 samples "
        "per setting across 3 consistent runs, at least 99% accounted timing coverage (including small measured "
        "inter-batch overhead only after 3 runs), no unknown components and a combined relative half-width at most "
        "50% (single-run components also require a normal half-width at most 40%). Each run has equal total weight, "
        "so recent runs weigh no less than older runs; pooling stops at a detected latency shift.",
        "service · Answer correctness: based on 1 earlier run of this endpoint."]


@pytest.mark.parametrize("field,value", [("family", "json"), ("concurrency", 8),
    ("stream", False), ("output_cap", 4096), ("input_bucket", "chars:999-9999")])
def test_incomparable_observations_never_enter_known_range(field, value):
    configured = config()
    measured = observations(configured)
    for row in measured["service"]:
        row[field] = value
    plan = request_plan(configured, observations=measured)
    assert plan["estimated_seconds"] is plan["estimated_low_seconds"] is plan["estimated_high_seconds"] is None
    assert plan["confidence"] == "low"
    assert plan["unknown_time_components"] == [{"id": "correctness", "title": "Answer correctness",
        "target": "service", "requests": 6, "request_timeout_seconds": 30, "timeout_bound_seconds": 60}]
    assert "6 requests at unknown speed" in plan["time_bound_reasons"][0]
    assert "up to 60 s if every request hits its 30 s timeout" in plan["time_bound_reasons"][0]


def test_unknown_requests_stay_out_of_fixed_window_headline():
    configured = config(checks=["correctness", "capacity"], request_timeout_seconds=1,
                        traffic={"rates": [1], "duration_seconds": 20, "drain_timeout_seconds": 1})
    plan = request_plan(configured)
    assert plan["estimated_low_seconds"] == 20
    assert plan["estimated_high_seconds"] == 21
    assert plan["confidence"] == "low"
    assert plan["confidence_details"]["fixed_fraction"] == pytest.approx(20 / 20.5)
    assert plan["confidence_details"]["drain_uncertainty_fraction"] == pytest.approx(1 / 20.5)
    assert plan["unknown_time_components"][0]["requests"] == 6
    assert plan["unknown_time_components"][0]["timeout_bound_seconds"] == 2


def test_medium_requires_every_finite_group_to_have_observations():
    configured = config(checks=["correctness", "json"], test_options={
        "correctness": {"samples": 64}, "json": {"samples": 64}})
    measured = observations(configured)
    assert request_plan(configured, observations=measured)["confidence"] == "low"
    measured["service"] += observations(configured, "json")["service"]
    assert request_plan(configured, observations=measured)["confidence"] == "medium"


def test_older_observations_without_tail_data_remain_low_confidence():
    configured = config()
    measured = observations(configured)
    for row in measured["service"]:
        for field in ("p99_ms", "max_ms", "quantiles_ms"):
            row.pop(field)
    plan = request_plan(configured, observations=measured)
    assert plan["confidence"] == "low"
    assert plan["unknown_time_components"] == []
    assert len(plan["time_components"]) == 1
    assert plan["estimated_low_seconds"] == .018
    assert plan["estimated_high_seconds"] == .043


def test_sequential_targets_sum_and_overlapping_targets_use_per_target_slots():
    configured = config(default_concurrency=4, concurrency=4, targets=[TARGET, {**TARGET, "name": "other"}],
                        test_options={"correctness": {"samples": 8}})
    sequential = request_plan(configured, observations=observations(configured, p50=100, p90=200))
    assert sequential["estimated_low_seconds"] == .446
    assert sequential["estimated_high_seconds"] == .93
    overlap = configured.model_copy(update={"overlap_models": True})
    measured = observations(overlap, p50=100, p90=200, concurrency=2)
    overlapping = request_plan(overlap, observations=measured)
    assert overlapping["estimated_low_seconds"] == .39
    assert overlapping["estimated_high_seconds"] == .807
    assert {item["concurrency"] for item in overlapping["time_components"]} == {2}


def test_time_limit_warns_when_cancellation_work_starts_before_cleanup_wait():
    configured = ConfigDraft.model_validate({"targets": [TARGET], "checks": ["cancellation", "recovery"],
        "max_duration_seconds": 2, "test_options": {"cancellation": {"cleanup_seconds": 5},
                                                  "recovery": {"sustained_seconds": 3}}})
    plan = request_plan(configured)
    assert plan["errors"] == []
    coverage = plan["time_warnings"][0]
    assert coverage["kind"] == "incomplete_coverage"
    assert "Recovery after load" in coverage["msg"]
    assert coverage["values"]["fully_known"] is False
    assert coverage["actions"][0] == {"label": "Use 38 s (covers fixed windows only)",
        "field": "max_duration_seconds", "value": 38}
    configured.max_duration_seconds = 6
    partial = request_plan(configured)
    assert partial["errors"] == []
    assert partial["time_warnings"][0]["kind"] == "incomplete_coverage"
    configured.max_duration_seconds = None
    assert request_plan(configured)["time_warnings"] == []


def test_concurrent_load_partial_fixed_windows_warn_instead_of_old_45_block():
    configured = config(checks=["concurrency"], max_duration_seconds=.5,
        concurrent_load={"combinations": {"selected": ["short_short"]}, "mixed": {"enabled": False},
                         "schedule": {"levels": [1], "duration_seconds": 1}})
    plan = request_plan(configured)
    assert plan["errors"] == []
    coverage, risk = plan["time_warnings"]
    assert coverage["actions"][0] == {"label": "Use 31 s (covers fixed windows only)", "field": "max_duration_seconds", "value": 31}
    assert "Concurrent load" in coverage["msg"]
    assert risk["kind"] == "estimate_risk"


def test_request_limit_names_cut_checks_and_provides_identical_action_data():
    configured = config(checks=["correctness", "json"], max_requests=7)
    plan = request_plan(configured)
    assert plan["planned_total"] == 12
    notice, = plan["request_warnings"]
    assert notice["kind"] == "incomplete_coverage"
    assert notice["cut_checks"] == ["json"]
    assert "JSON correctness" in notice["msg"]
    assert notice["actions"] == [{"label": "Use 12 total requests", "field": "max_requests", "value": 12},
                                  {"label": "Edit JSON correctness", "field": "test_options.json"}]
    assert configured.max_requests == 7
    configured.max_requests = None
    assert request_plan(configured)["request_warnings"] == []


def test_duration_requests_only_show_observation_counts_with_fully_comparable_measurements():
    configured = config(checks=["concurrency"], concurrent_load={
        "combinations": {"selected": ["short_short"]}, "mixed": {"enabled": False},
        "schedule": {"levels": [2], "duration_seconds": 5}})
    profile = configured.concurrent_load.profile.short
    measured = {"service": [{"family": "concurrency", "concurrency": 2, "stream": configured.stream,
        "output_cap": profile.output_tokens, "input_bucket": input_bucket(profile.input_tokens * 4),
        "n": 30, "p50_ms": 100, "p90_ms": 200}]}
    row = request_plan(configured, observations=measured)["rows"][0]
    assert row["kind"] == "duration"
    assert row["requests_per_target"] is None
    assert row["estimated_requests_per_target"] == 100
    assert row["estimate_source"] == "from your last run"
    for key, value in [("output_cap", profile.output_tokens * 2), ("input_bucket", "chars:0-255")]:
        changed = {"service": [{**measured["service"][0], key: value}]}
        assert "estimated_requests_per_target" not in request_plan(configured, observations=changed)["rows"][0]
    assert "estimated_requests_per_target" not in request_plan(configured)["rows"][0]
    configured.targets.append(configured.targets[0].model_copy(update={"name": "other"}))
    assert "estimated_requests_per_target" not in request_plan(configured, observations=measured)["rows"][0]
    both = measured | {"other": measured["service"]}
    assert request_plan(configured, observations=both)["rows"][0]["estimated_requests_per_target"] == 100
    differing = both | {"other": [{**measured["service"][0], "p50_ms": 200}]}
    assert "estimated_requests_per_target" not in request_plan(configured, observations=differing)["rows"][0]


async def test_no_budget_recovery_prime_count_matches_inprocess_run(monkeypatch):
    from collections import Counter
    from giraffe import runner
    from tests.test_runner import FakeClient

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    configured = config(checks=["recovery"], sustained_seconds=0, max_requests=None)
    preview = request_plan(configured)
    report = await runner.run_suite(configured)
    counts = Counter(record.scenario for record in report.requests)
    assert counts["recovery_prime"] == 1
    assert preview["planned_total"] == len(report.requests) == 13
    assert {row["id"]: row["requests_per_target"] for row in preview["rows"]} == counts


@pytest.mark.parametrize("confidence", ["high", "medium"])
def test_fixed_latency_inprocess_traffic_finishes_inside_estimated_range(monkeypatch, confidence):
    from tests.test_review26_phase4_accuracy import run_virtual

    async def scenario():
        from giraffe import runner
        from tests.test_runner import FakeClient

        class FixedLatency(FakeClient):
            delay = .02

        monkeypatch.setattr(runner, "LLMClient", FixedLatency)
        if confidence == "high":
            configured = config(checks=["capacity"], request_timeout_seconds=.05,
                                traffic={"rates": [50], "duration_seconds": 1, "drain_timeout_seconds": .05})
        else:
            configured = config(test_options={"correctness": {"samples": 192, "concurrency": 4}})
        previous = await runner.run_suite(configured)
        summary = latency_summary(previous)
        measured = {target["name"]: target["measurements"] for target in summary["targets"]}
        preview = request_plan(configured, observations=measured)
        report = await runner.run_suite(configured)
        elapsed = report.observations["inference_duration_seconds"]
        assert preview["confidence"] == confidence
        assert preview["estimated_low_seconds"] <= elapsed <= preview["estimated_high_seconds"]
        assert math.isfinite(elapsed)

    run_virtual(monkeypatch, scenario())

@pytest.mark.parametrize("confidence", ["high", "medium"])
def test_fixed_latency_httpx_endpoint_finishes_inside_estimated_range(monkeypatch, confidence):
    from tests.test_review26_phase4_accuracy import run_virtual

    async def scenario():
        import asyncio
        import json
        import httpx
        from giraffe import runner

        original = httpx.AsyncClient
        calls = []

        async def endpoint(request):
            payload = json.loads(request.content)
            calls.append(payload)
            await asyncio.sleep(.02)
            prompt = payload["messages"][-1]["content"]
            answer = "25" if "17 cards" in prompt else "B" if "dax means" in prompt else "T8R2"
            return httpx.Response(200, json={"model": "test", "choices": [
                {"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 1}})

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(endpoint), **kwargs))
        if confidence == "high":
            configured = config(checks=["capacity"], stream=False, request_timeout_seconds=.2,
                                traffic={"rates": [10], "duration_seconds": 1, "drain_timeout_seconds": .2})
        else:
            configured = config(stream=False, test_options={"correctness": {"samples": 192, "concurrency": 4}})
        previous = await runner.run_suite(configured)
        summary = latency_summary(previous)
        measured = {target["name"]: target["measurements"] for target in summary["targets"]}
        preview = request_plan(configured, observations=measured)
        report = await runner.run_suite(configured)
        assert calls and all(record.valid and record.status == "completed" for record in report.requests)
        assert preview["confidence"] == confidence
        # HTTPX setup can yield before the first dispatch. The headline explicitly
        # excludes setup/connection; retain all time after traffic starts.
        setup = min(record.dispatch_ms for record in report.requests) / 1000
        elapsed = round(report.observations["inference_duration_seconds"] - setup, 9)
        assert setup >= 0
        assert preview["estimated_low_seconds"] <= elapsed <= preview["estimated_high_seconds"]

    run_virtual(monkeypatch, scenario())

async def test_report_latency_summary_drives_next_finite_plan(monkeypatch):
    from giraffe import runner
    from tests.test_runner import FakeClient

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    configured = config(test_options={"correctness": {"samples": 8, "concurrency": 2,
                                                     "max_output_tokens": 512}})
    report = await runner.run_suite(configured)
    summary = latency_summary(report)
    measured = {target["name"]: target["measurements"] for target in summary["targets"]}
    plan = request_plan(configured, observations=measured)
    assert plan["confidence"] == "low"
    assert plan["unknown_time_components"] == []
    assert plan["estimated_high_seconds"] > 0


def test_long_traffic_plan_cap_and_optional_budgets_are_independent():
    configured = config(checks=["capacity"], traffic={"rates": [25], "duration_seconds": 3600},
                        max_duration_seconds=300, max_requests=100)
    plan = request_plan(configured)
    assert plan["planned_total"] == 90000
    assert plan["errors"] == []
    assert plan["time_warnings"][0]["kind"] == "incomplete_coverage"
    assert plan["time_warnings"][0]["actions"][0] == {"label": "Use 3,630 s",
        "field": "max_duration_seconds", "value": 3630}
    assert plan["request_warnings"][0]["kind"] == "incomplete_coverage"
    assert plan["request_warnings"][0]["actions"][0]["value"] == 90000
    assert configured.traffic.rates == [25]
    assert configured.traffic.duration_seconds == 3600
    configured.max_requests = configured.max_duration_seconds = None
    assert request_plan(configured)["request_warnings"] == request_plan(configured)["time_warnings"] == []


def test_budget_action_counts_other_checks_and_all_endpoints():
    configured = config(checks=["capacity", "correctness"], max_requests=20,
                        targets=[TARGET, {**TARGET, "name": "other"}],
                        traffic={"rates": [25], "duration_seconds": 2})
    plan = request_plan(configured)
    assert plan["planned_per_target"] == 56
    assert plan["planned_total"] == plan["required_request_limit"] == 112
    assert plan["target_quotas"] == {"service": 10, "other": 10}
    notice, = plan["request_warnings"]
    assert notice["cut_checks"] == ["capacity", "correctness"]
    assert notice["targets"] == [{"target": target, "quota": 10, "cut_checks": ["capacity", "correctness"]}
                                 for target in ("service", "other")]
    assert "service:" in notice["msg"] and "other:" in notice["msg"]
    assert notice["actions"] == [{"label": "Use 112 total requests", "field": "max_requests", "value": 112},
        {"label": "Edit Capacity under load", "field": "traffic.rates"},
        {"label": "Edit Answer correctness", "field": "test_options.correctness"}]
    assert plan["errors"] == []
    assert configured.max_requests == 20


async def test_issue_example_suite_fixed_counts_match_inprocess_run(monkeypatch):
    from collections import Counter
    from giraffe import runner
    from tests.test_runner import FakeClient

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    configured = config(checks=["cancellation", "recovery", "arrivals", "prefix", "concurrency", "sessions", "consistency", "tools"],
        max_requests=None, max_duration_seconds=None, stop_after_errors=100,
        forced_tool_diagnostic=True, sustained_seconds=.02,
        arrivals={"mode": "burst", "requests": 8, "burst_size": 4, "interval_seconds": .01},
        sessions={"delay_seconds": 0}, consistency={"levels": [1, 2]},
        test_options={"cancellation": {"cleanup_seconds": .001, "deadline_probe_ms": 5}},
        concurrent_load={"combinations": {"selected": ["short_short"]}, "mixed": {"enabled": False},
                         "schedule": {"levels": [1, 2], "duration_seconds": 1}})
    # Keep the duration-based part short; all finite counts retain issue settings.
    configured.concurrent_load.schedule.duration_seconds = .02
    preview = request_plan(configured)
    report = await runner.run_suite(configured)
    counts = Counter(record.scenario for record in report.requests)
    mapping = {"limits.stop_control": ["stop_control"], "cancellation": ["limits", "cancellation_baseline", "client_cancel", "client_deadline", "cancellation_probe"],
               "arrivals": ["arrival"], "prefix": [name for name in counts if name.startswith("prefix_")],
               "consistency": [name for name in counts if name.startswith("consistency_")],
               "tools": [name for name in counts if name.startswith("tools_")]}
    for row in preview["rows"]:
        if row["kind"] == "exact" and row["requests_per_target"]:
            assert sum(counts[name] for name in mapping.get(row["id"], [row["id"]])) == row["requests_per_target"], row
    assert counts["recovery_prime"] == 1
    assert preview["planned_total"] is None
    assert {row["kind"] for row in preview["rows"] if row["id"].startswith("load_") and row["id"] != "load_calibration"} == {"duration"}


async def test_concurrent_load_plan_windows_and_calibration_bounds_match_inprocess_run():
    from tests.test_concurrent_load import Client, configuration, perform

    configured = configuration()
    configured.max_requests = None
    configured.concurrent_load.schedule.duration_seconds = .2
    preview = request_plan(configured)
    run, _, _, _ = await perform(configured, Client())
    rows = {row["id"]: row for row in preview["rows"]}
    calibration_count = sum("calibration" in record.scenario for record in run.records)
    assert 0 < calibration_count <= rows["load_calibration"]["requests_per_target"]
    assert rows["load_calibration"]["kind"] == "bounded"
    load = run.observations["local"]["concurrent_load"]
    assert load["configured_load_seconds"] == preview["configured_load_seconds"] == .2
    assert len(load["load_types"]["combinations"]["points"]) == 1
    assert rows["load_combinations"]["kind"] == "duration"
    assert rows["load_combinations"]["requests_per_target"] is None
    assert len(run.records) > calibration_count


def test_finite_gather_batches_use_slowest_comparable_fixture_per_batch():
    configured = config(checks=["serving"], samples=3)
    fixtures = builtin_fixtures(configured, check_id="serving")["short"]
    measured = {"service": [{"family": "serving", "concurrency": 1, "stream": configured.stream,
        "output_cap": spec.max_tokens, "input_bucket": input_bucket(spec.input_chars),
        **tail(delay, 2 * delay)} for spec, delay in zip(fixtures, [10, 100])]}
    plan = request_plan(configured, observations=measured)
    assert plan["estimated_low_seconds"] == .056
    assert plan["estimated_high_seconds"] == .284
    assert plan["confidence"] == "low"


async def test_nonstream_consistency_summary_drives_its_next_estimate(monkeypatch):
    from giraffe import runner
    from tests.test_runner import FakeClient

    class NonstreamClient(FakeClient):
        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            record.stream = self.config.stream and spec.stream
            return record

    monkeypatch.setattr(runner, "LLMClient", NonstreamClient)
    configured = config(checks=["consistency"], stream=False, consistency={"levels": [1, 2],
                        "shapes": ["short"], "prompts_per_shape": 2, "repetitions": 2})
    report = await runner.run_suite(configured)
    summary = latency_summary(report)
    measured = {target["name"]: target["measurements"] for target in summary["targets"]}
    plan = request_plan(configured, observations=measured)
    assert plan["confidence"] == "low"
    assert plan["unknown_time_components"] == []
    assert plan["estimated_high_seconds"] > 0


def test_observed_initial_context_does_not_predict_adaptive_resized_inputs():
    configured = config(checks=["context"])
    plan = request_plan(configured, observations=observations(configured, "context"))
    assert plan["time_components"] == []
    assert [item["id"] for item in plan["unknown_time_components"]] == ["context_first_batch", "context"]
    assert plan["unknown_time_components"][0]["requests"] == 6
    assert plan["unknown_time_components"][1]["requests"] == 13
    assert plan["confidence"] == "low"


def test_standalone_tool_observations_do_not_predict_conversation_histories():
    from pathlib import Path
    import yaml
    from giraffe.tool_checks import default_tool_fixture, resolved_tool_fixtures, resolved_tool_modes

    values = yaml.safe_load((Path(__file__).parents[1] / "examples/tool-result-conversation.yaml").read_text())
    values["tool_checks"]["fixtures"] = [default_tool_fixture().model_dump(mode="json")]
    configured = RunConfig.model_validate(values)
    measured = {"local": [{"family": "tools", "concurrency": 1, "stream": mode == "stream",
        "output_cap": configured.max_output_tokens,
        "input_bucket": input_bucket(sum(len(message.content) for message in fixture.messages)),
        "n": 10, "p50_ms": 10, "p90_ms": 20}
        for fixture in resolved_tool_fixtures(configured) for mode in resolved_tool_modes(configured)]}
    plan = request_plan(configured, observations=measured)
    assert plan["estimated_seconds"] is None
    assert plan["unknown_time_components"][0]["id"] == "tools"
    assert plan["unknown_time_components"][0]["requests"] == 6
    assert plan["confidence"] == "low"


def test_validation_preview_does_not_build_large_effective_fixture_inputs(monkeypatch):
    from giraffe import planning

    configured = config(checks=["context", "correctness", "consistency", "capacity"], context_limit=1000000)
    calls, original = [], planning.builtin_fixtures

    def fixture_call(configuration, **kwargs):
        calls.append((configuration.context_limit, kwargs))
        return original(configuration, **kwargs)

    monkeypatch.setattr(planning, "builtin_fixtures", fixture_call)
    request_plan(configured)
    assert calls == [(128, {}), (512, {})]


async def test_overlapping_targets_preserve_configured_gather_batch_barriers(monkeypatch):
    import asyncio
    from giraffe import runner
    from tests.test_runner import FakeClient

    class PooledClient(FakeClient):
        delay = .01

        def __init__(self, target, configuration, *, max_connections=None):
            super().__init__(target, configuration, max_connections=max_connections)
            self.slots = asyncio.Semaphore(max_connections)

        async def execute(self, spec, **kwargs):
            async with self.slots:
                return await super().execute(spec, **kwargs)

    monkeypatch.setattr(runner, "LLMClient", PooledClient)
    configured = config(targets=[TARGET, {**TARGET, "name": "other"}], overlap_models=True, concurrency=4,
                        test_options={"correctness": {"concurrency": 3, "samples": 6}})
    preview = request_plan(configured, observations=observations(configured, concurrency=3, p50=9, p90=15))
    assert preview["estimated_low_seconds"] == .017
    assert preview["estimated_high_seconds"] == .068
    assert {item["configured_concurrency"] for item in preview["time_components"]} == {3}
    assert {item["concurrency"] for item in preview["time_components"]} == {3}
    previous = await runner.run_suite(configured)
    summary = latency_summary(previous)
    measured = {target["name"]: target["measurements"] for target in summary["targets"]}
    preview = request_plan(configured, observations=measured)
    report = await runner.run_suite(configured)
    assert preview["confidence"] == "low"
    assert preview["estimated_low_seconds"] <= report.observations["inference_duration_seconds"] <= preview["estimated_high_seconds"]


async def test_arrival_pending_queue_needs_multiple_drain_waves(monkeypatch):
    from giraffe import runner
    from tests.test_runner import FakeClient

    class FixedLatency(FakeClient):
        delay = .01

    monkeypatch.setattr(runner, "LLMClient", FixedLatency)
    configured = config(checks=["arrivals"], request_timeout_seconds=.2,
        arrivals={"mode": "burst", "requests": 20, "burst_size": 20, "max_in_flight": 1, "max_pending": 20})
    unmeasured = request_plan(configured)
    assert unmeasured["estimated_high_seconds"] == .2
    queued, = unmeasured["unknown_time_components"]
    assert queued["id"] == "arrivals_pending"
    assert queued["requests"] == 19
    assert queued["upper_bound"] is True
    assert queued["timeout_bound_seconds"] == pytest.approx(3.8)
    assert "up to 19 requests at unknown speed" in unmeasured["time_bound_reasons"][0]
    spec = builtin_fixtures(configured, check_id="arrivals")["short"][0]
    measured = {"service": [{"family": "arrivals", "concurrency": 1, "stream": configured.stream,
        "output_cap": spec.max_tokens, "input_bucket": input_bucket(spec.input_chars),
        **tail(9, 15)}]}
    preview = request_plan(configured, observations=measured)
    assert preview["estimated_low_seconds"] == .178  # Legacy quantiles have no minimum: zero lower initial drain.
    assert preview["estimated_high_seconds"] == .434
    assert preview["unknown_time_components"] == []
    previous = await runner.run_suite(configured)
    summary = latency_summary(previous)
    measured = {target["name"]: target["measurements"] for target in summary["targets"]}
    preview = request_plan(configured, observations=measured)
    report = await runner.run_suite(configured)
    assert len(report.requests) == 20
    assert preview["estimated_low_seconds"] <= report.observations["inference_duration_seconds"] <= preview["estimated_high_seconds"]


def test_canonical_warnings_include_time_coverage_and_estimate_risk_once():
    configured = config(checks=["capacity"], max_duration_seconds=5,
                        traffic={"rates": [1], "duration_seconds": 20})
    plan = request_plan(configured)
    assert {notice["kind"] for notice in plan["time_warnings"]} == {"incomplete_coverage", "estimate_risk"}
    for notice in plan["time_warnings"]:
        assert plan["warnings"].count(notice["msg"]) == 1


def test_more_endpoints_than_slots_accounts_for_serial_finite_traffic(monkeypatch):
    from tests.test_review26_phase4_accuracy import run_virtual

    async def scenario():
        from giraffe import runner
        from tests.test_runner import FakeClient

        class FixedLatency(FakeClient):
            delay = 3 / 256  # Exact binary delay keeps virtual clock boundaries stable.

        monkeypatch.setattr(runner, "LLMClient", FixedLatency)
        configured = config(targets=[TARGET, {**TARGET, "name": "other"}],
                            overlap_models=True, concurrency=1,
                            test_options={"correctness": {"samples": 3}})
        preview = request_plan(configured, observations=observations(configured, concurrency=1, p50=9, p90=15))
        assert preview["confidence"] == "medium"
        assert preview["estimated_low_seconds"] == .044
        assert preview["estimated_high_seconds"] == .089
        previous = await runner.run_suite(configured)
        summary = latency_summary(previous)
        measured = {target["name"]: target["measurements"] for target in summary["targets"]}
        preview = request_plan(configured, observations=measured)
        report = await runner.run_suite(configured)
        assert len(report.requests) == 6
        assert preview["estimated_low_seconds"] <= report.observations["inference_duration_seconds"] <= preview["estimated_high_seconds"]

    run_virtual(monkeypatch, scenario())


async def test_spaced_arrivals_do_not_assume_the_pending_queue_is_full(monkeypatch):
    from giraffe import runner
    from tests.test_runner import FakeClient

    class FixedLatency(FakeClient):
        delay = .01

    monkeypatch.setattr(runner, "LLMClient", FixedLatency)
    configured = config(checks=["arrivals"], request_timeout_seconds=.2,
        arrivals={"requests": 6, "requests_per_second": 25, "max_in_flight": 1, "max_pending": 20})
    spec = builtin_fixtures(configured, check_id="arrivals")["short"][0]
    measured = {"service": [{"family": "arrivals", "concurrency": 1, "stream": configured.stream,
        "output_cap": spec.max_tokens, "input_bucket": input_bucket(spec.input_chars),
        **tail(9, 15)}]}
    preview = request_plan(configured, observations=measured)
    drain = next(item for item in preview["time_components"] if item["id"] == "arrivals_pending")
    assert drain["upper_bound"] is True
    assert drain["low_seconds"] == 0
    assert preview["estimated_low_seconds"] == .2  # No recorded minimum in this legacy cohort.
    previous = await runner.run_suite(configured)
    summary = latency_summary(previous)
    measured = {target["name"]: target["measurements"] for target in summary["targets"]}
    preview = request_plan(configured, observations=measured)
    report = await runner.run_suite(configured)
    assert len(report.requests) == 6
    assert preview["estimated_low_seconds"] <= report.observations["inference_duration_seconds"] <= preview["estimated_high_seconds"]


def test_logprob_consistency_uses_exact_nonstream_fixture_observations():
    from giraffe.logprob_workload import logprob_fixtures

    configured = config(checks=["consistency"], consistency={"levels": [1, 3], "shapes": ["short"],
                        "repetitions": 2, "logprobs": {"priming_rounds": 1}})
    template = logprob_fixtures(configured, builtin_fixtures(configured))["short"]
    measured = {"service": [{"family": "consistency", "concurrency": level, "stream": False,
        "output_cap": template.max_tokens, "input_bucket": input_bucket(template.input_chars),
        **tail(10, 20, n=6)} for level in (1, 3)]}
    plan = request_plan(configured, observations=measured)
    assert plan["confidence"] == "low"
    assert plan["unknown_time_components"] == []
    assert plan["estimated_low_seconds"] == .112  # Independent groups combine variances, not interval widths.
    assert plan["estimated_high_seconds"] == .223


def test_recovery_cleanup_wait_remains_when_sustained_duration_is_zero():
    configured = config(checks=["cancellation", "recovery"], sustained_seconds=0,
                        test_options={"cancellation": {"cleanup_seconds": 4}})
    plan = request_plan(configured)
    assert plan["minimum_seconds"] == 4
    assert {item["id"] for item in plan["time_components"]} == {"recovery_wait"}
    assert plan["estimated_low_seconds"] == 4
    assert plan["estimated_high_seconds"] == 4
