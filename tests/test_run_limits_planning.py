"""Optional suite bounds leave each check's own workload intact."""

import json

import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.request_pack import pack_preview, pack_summary


def config(**updates):
    return RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}],
        "checks": ["correctness"], **updates,
    })


@pytest.mark.parametrize("checks", [["correctness"], ["capacity"], ["arrivals"], ["recovery"], ["concurrency"]])
def test_unbounded_plan_has_no_quota_or_time_budget_warning(checks):
    configured = config(checks=checks, max_requests=None, max_duration_seconds=None, concurrency=None)
    result = request_plan(configured)
    assert result["request_limit"] is None
    # The 300-second request default allows more arrivals to remain in flight;
    # capacity still caps its own in-flight population at 512.
    assert result["required_concurrency"]["peak"] == (512 if checks == ["capacity"] else 300 if checks == ["arrivals"] else 4)
    assert result["target_quotas"] == {"local": None}
    assert result["errors"] == result["time_warnings"] == []
    assert not any("quota" in warning or "Raise the total request limit" in warning
                   for warning in result["warnings"])
    assert not any("max_requests" in warning["fields"] for warning in result["load_warnings"])
    json.dumps(result, allow_nan=False)


def test_recovery_without_request_budget_always_plans_one_prime_even_after_adaptive_work():
    configured = config(checks=["context", "recovery"], max_requests=None)
    result = request_plan(configured)
    prime = next(row for row in result["rows"] if row["id"] == "recovery_prime")
    assert prime["requests_per_target"] == 1
    assert prime["kind"] == "exact"
    assert "sustained" not in result["adaptive_estimates"]


def test_recovery_explicit_budget_keeps_prime_gate_and_starvation_guidance():
    result = request_plan(config(checks=["recovery"], max_requests=1))
    prime = next(row for row in result["rows"] if row["id"] == "recovery_prime")
    assert prime["requests_per_target"] == 0
    assert any("share of the request limit (Stop after this many requests:" in warning for warning in result["warnings"])


def test_correctness_estimate_uses_effective_parallelism_instead_of_ceiling():
    common = {"default_concurrency": 2, "test_options": {"correctness": {"samples": 8}}}
    unbounded = request_plan(config(**common))
    bounded = request_plan(config(**common, concurrency=40))
    assert bounded["estimated_seconds"] is unbounded["estimated_seconds"] is None
    assert bounded["unknown_time_components"] == unbounded["unknown_time_components"]
    assert bounded["unknown_time_components"][0]["timeout_bound_seconds"] == 4 * 300


def test_inherited_concurrency_notice_is_structured_and_nonblocking_in_plan():
    result = request_plan(config(default_concurrency=64, concurrency=40))
    notice = next(warning for warning in result["concurrency_warnings"]
                  if "default_concurrency" in warning["fields"])
    assert "concurrency" in notice["fields"]
    assert notice["values"]
    assert "40" in notice["msg"]
    assert notice["msg"] in result["warnings"]
    assert result["errors"] == []


def test_burst_warning_shows_inflight_and_queue_arithmetic():
    from pydantic import ValidationError
    from tests.test_planning import load_config
    configuration = load_config(concurrency=4)
    configuration.load_test.patterns.bursts.enabled = True
    configuration.load_test.patterns.bursts.size = 20
    configuration.load_test.patterns.bursts.first_seconds = 0
    with pytest.raises(ValidationError) as raised:
        RunConfig.model_validate(configuration.model_dump())
    error = raised.value.errors(include_url=False)[0]
    assert error["type"] == "load_test"
    assert error["ctx"]["fields"] == ["load_test.patterns.bursts.size", "concurrency"]
    assert "concurrency ceiling" in error["msg"]


def test_burst_that_fits_inflight_and_pending_needs_no_drop_warning():
    result = request_plan(config(checks=["arrivals"], arrivals={
        "mode": "burst", "requests": 10, "burst_size": 10, "max_in_flight": 4, "max_pending": 6}))
    assert not any("arrivals.burst_size" in warning["fields"] for warning in result["concurrency_warnings"])


def test_unbounded_pack_judging_summary_uses_planned_calls_without_quota_clamp():
    configured = config(checks=["capacity", "correctness", "access"], request_source="request_pack",
                        max_requests=None, max_duration_seconds=None,
                        targets=[{"name": name, "url": "http://localhost", "model": "test"}
                                 for name in ("first", "second")],
                        request_pack={"version": 1, "cases": [{
                            "fixture_id": "one", "messages": [{"role": "user", "content": "Question"}],
                            "scorer": "judge", "expected": "Answer"}]},
                        judging={"judges": [{"name": "judge", "url": "http://localhost", "model": "test"}]},
                        traffic={"rates": [2], "duration_seconds": 2})
    preview = pack_preview(configured)
    summary = pack_summary(configured)
    assert preview["per_target_request_quota"] == summary["per_target_request_quota"] == {"first": None, "second": None}
    assert preview["request_cap"] is None
    assert preview["judging"]["planned_call_upper_bound"] == preview["judging"]["quota_call_upper_bound"] == 10
    assert summary["judging"]["quota_call_upper_bound"] == 10
    assert not any("exceed a target request quota" in warning or "exceeds the run time budget" in warning or "consume additional shared" in warning
                   for warning in preview["warnings"])
    json.dumps(preview, allow_nan=False)
