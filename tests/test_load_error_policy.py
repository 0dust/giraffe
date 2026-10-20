"""Concurrent load failures stay scoped to their acceptance check."""

from types import SimpleNamespace

import pytest

from giraffe import runner
from giraffe.models import RequestRecord, RequestSpec, RunConfig, Target
from giraffe.planning import request_plan
from giraffe.workloads import results


def config(**changes):
    return RunConfig(**(dict(targets=[Target(name="local", url="http://localhost:1234", model="fake")],
        checks=["access", "serving", "concurrency"], concurrency=4,
        max_output_tokens=128, max_requests=200, max_duration_seconds=120) | changes))


@pytest.mark.parametrize("scenario", ["load_combo_long_short_c4", "load_stress_input_c4",
    "load_stress_output_c4", "load_calibration_long_short_answer"])
@pytest.mark.parametrize("fault", ["timeout", "context"])
async def test_load_overload_errors_do_not_poison_core_checks_or_watchdog(scenario, fault):
    settings = config(stop_after_errors=1, unresponsive_after_failures=1, unresponsive_for_seconds=.001)
    state = runner._Run(settings, None, None)
    state.endpoint_health["local"]["first_attempt_seconds"] = -1

    async def execute(spec, **kwargs):
        return RequestRecord(id="load", target="local", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now",
            status="timeout" if fault == "timeout" else "failed",
            http_status=None if fault == "timeout" else 400,
            error="request deadline exceeded" if fault == "timeout" else "maximum context length exceeded")

    spec = runner._spec(RequestSpec(fixture_id="load", scenario=scenario, messages=[], check_ids=["access", "serving"]),
        scenario, ["concurrency"])
    record = await state.request(settings.targets[0], SimpleNamespace(execute=execute), spec)
    assert record.check_ids == ["concurrency"]
    assert state.errors == 0 and state.stop_reason("local") is None
    assert state.endpoint_health["local"]["consecutive_failures"] == 0
    assert runner._stats([record])["error_rate"] == 1
    assert runner._request_failed(record)


@pytest.mark.parametrize("scenario", ["load_combo_long_short_c4", "load_stress_input_c4",
    "load_stress_output_c4", "load_calibration_long_short_answer"])
async def test_load_connection_failures_still_feed_unresponsive_watchdog(scenario):
    settings = config(stop_after_errors=100, unresponsive_after_failures=2, unresponsive_for_seconds=.001)
    state = runner._Run(settings, None, None)
    state.endpoint_health["local"]["first_attempt_seconds"] = -1

    async def execute(spec, **kwargs):
        return RequestRecord(id=str(len(state.records)), target="local", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now",
            status="failed", error="request failed (ConnectError)")

    spec = RequestSpec(fixture_id="load", messages=[], scenario=scenario, check_ids=["concurrency"])
    for _ in range(4):
        await state.request(settings.targets[0], SimpleNamespace(execute=execute), spec)
    assert len(state.records) == 2
    assert "unresponsive" in state.stop_reason("local")


@pytest.mark.parametrize("scenario", ["fairness_short", "bucket_short_short_c2", "mixed_short_c2", "stress_input"])
def test_legacy_load_scenarios_keep_their_error_policy(scenario):
    record = RequestRecord(id="old", target="local", fixture_id="old", scenario=scenario,
        check_ids=[], started_at="now", status="timeout")
    assert not runner._serving_failure(record, scenario)


def test_other_checks_keep_fixed_request_budget_guidance_with_concurrent_load_selected():
    settings = config(max_requests=1)
    plan = request_plan(settings)
    guidance = runner._budget_guidance(settings)
    assert "Concurrent load" not in guidance
    assert "to at least 16" in guidance
    assert f"at least {plan['known_total']} fixed requests" in guidance
    assert "sending windows" in runner._budget_guidance(settings, "concurrency")


@pytest.mark.parametrize("cached,source", [(0, "usage.prompt_tokens_details"), (32, "usage.cache_read_input_tokens"), (None, None)])
def test_prefix_scenario_keeps_per_request_cache_observations(cached, source):
    settings = config(checks=["prefix"])
    state = runner._Run(settings, None, None)
    state.records = [RequestRecord(id="prefix", target="local", fixture_id="prefix", scenario="prefix_shared",
        check_ids=["prefix"], started_at="now", status="completed", valid=True, input_tokens=128,
        cached_prompt_tokens=cached, cache_source=source)]
    check = results(state, settings.targets[0], runner._stats, runner._timing_violations, runner._missing_timing)[0]
    assert check.metrics["buckets"]["prefix_shared"]["cache_observations"] == [{
        "request_id": "prefix", "input_tokens": 128, "cached_tokens": cached, "source": source,
        "reuse": "reported" if cached is not None else "unverified"}]


def test_disconnected_summary_prefix_is_kept_for_concurrent_load():
    settings = config()
    state = runner._Run(settings, None, None)
    state.records = [RequestRecord(id="connection", target="local", fixture_id="connection", scenario="initial",
        check_ids=["access"], started_at="now", status="failed", error="request failed (ConnectError)")]
    check = next(check for check in runner._checks(state, settings.targets[0]) if check.id == "concurrency")
    assert check.summary.startswith("Could not connect to http://localhost:1234 (ConnectError); no checks could run.")
    assert "Start the server" in check.summary
