"""Traffic hard caps and optional capacity estimates have distinct remedies."""

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from giraffe.models import RunConfig, TrafficConfig
from giraffe.planning import request_plan


def configuration(**updates):
    return RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://localhost:1/v1", "model": "test"}],
        "checks": ["capacity"], **updates,
    })


def cap_error(traffic):
    with pytest.raises(ValidationError) as raised:
        configuration(traffic=traffic)
    error, = raised.value.errors(include_url=False)
    assert error["type"] == "traffic_plan_cap"
    assert error["ctx"]["fields"] == ["traffic.rates", "traffic.duration_seconds"]
    return error


def corrected_traffic(traffic, error):
    action, edit = error["ctx"]["actions"]
    assert action["field"] == "traffic.duration_seconds"
    assert edit == {"label": "Edit sending rate", "field": "traffic.rates"}
    updated = deepcopy(traffic)
    updated["duration_seconds"] = action["value"]
    assert updated["rates"] == traffic["rates"]
    return updated


def test_a12_single_stage_cap_explains_arithmetic_and_duration_only_correction():
    traffic = {"rates": [250], "duration_seconds": 3600}
    error = cap_error(traffic)
    assert error["msg"] == (
        "250 requests/second × 3,600 seconds = 900,000 planned arrivals. "
        "This traffic plan allows at most 100,000 per endpoint. "
        "At this rate, the longest stage is 400 seconds (6 minutes 40 seconds).")
    assert error["ctx"]["values"] == {
        "traffic.rates": [250], "traffic.duration_seconds": 3600,
        "planned_arrivals": 900000, "maximum_planned_arrivals": 100000,
        "longest_stage_seconds": 400,
    }
    assert error["ctx"]["actions"][0] == {
        "label": "Use 400 seconds", "field": "traffic.duration_seconds", "value": 400}
    assert error["ctx"]["note"] == (
        "This cap covers scheduled traffic only; suite time and request budgets are separate.")
    result = request_plan(configuration(traffic=corrected_traffic(traffic, error)))
    assert result["planned_total"] == 100000
    assert result["errors"] == []


def test_a14_multistage_cap_explains_aggregate_and_keeps_every_rate():
    traffic = {"rates": [50, 100, 250], "duration_seconds": 300}
    error = cap_error(traffic)
    assert error["msg"] == (
        "(50 + 100 + 250) requests/second × 300 seconds = 120,000 planned arrivals. "
        "This traffic plan allows at most 100,000 per endpoint. "
        "At these rates, the longest stage that fits is 250 seconds.")
    assert error["ctx"]["values"]["planned_arrivals"] == 120000
    assert error["ctx"]["actions"][0] == {
        "label": "Use 250 seconds", "field": "traffic.duration_seconds", "value": 250}
    result = request_plan(configuration(traffic=corrected_traffic(traffic, error)))
    assert result["planned_total"] == 100000
    assert result["errors"] == []


@pytest.mark.parametrize("budgets, warning_key, field", [
    ({"max_duration_seconds": 300}, "time_warnings", "max_duration_seconds"),
    ({"max_requests": 1000}, "request_warnings", "max_requests"),
])
def test_a13_valid_traffic_plan_reports_suite_budgets_separately(budgets, warning_key, field):
    traffic = {"rates": [25], "duration_seconds": 3600}
    result = request_plan(configuration(traffic=traffic, **budgets))
    assert result["planned_total"] == 90000
    assert result["errors"] == []
    coverage = next(item for item in result[warning_key] if item["kind"] == "incomplete_coverage")
    assert field in coverage["fields"]
    assert coverage["actions"][0]["field"] == field
    assert "traffic plan allows" not in coverage["msg"]


@pytest.mark.parametrize("traffic", [
    {"rates": [12.5, 37.5], "duration_seconds": 3600},
    {"rates": [9999.01, 9999.02, 9999.03, 9999.04, 9999.05,
               9999.06, 9999.07, 9999.08, 9999.09, 9999.10,
               9999.11, 9999.12, 9999.13, 9999.14, 9999.15, 9999.16],
     "duration_seconds": 1},
    {"rates": [250.00000000000003], "duration_seconds": 400},
])
def test_duration_correction_handles_fractional_and_float_boundary_rates(traffic):
    error = cap_error(traffic)
    updated = corrected_traffic(traffic, error)
    accepted = TrafficConfig.model_validate(updated)
    assert accepted.rates == traffic["rates"]
    assert accepted.duration_seconds > 0


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -1, 0, 10001])
def test_nonfinite_or_out_of_range_rates_are_validation_errors(rate):
    with pytest.raises(ValidationError):
        TrafficConfig(rates=[rate])


def test_capacity_risk_is_a_labelled_nonblocking_user_supplied_estimate():
    result = request_plan(configuration(traffic={"rates": [250], "duration_seconds": 1,
        "max_in_flight": 256, "expected_response_seconds": 2}))
    warning, = result["capacity_warnings"]
    assert warning["kind"] == "capacity_risk_estimate"
    assert warning["estimate"] is True
    assert warning["values"]["unfinished_requests"] == 500
    assert warning["fields"] == ["traffic.rates", "traffic.expected_response_seconds", "traffic.max_in_flight"]
    assert "≈ 250 requests/second × 2 seconds = 500 unfinished requests needed; in-flight limit 256" in warning["msg"]
    assert "estimate" in warning["msg"] and "dropped locally" in warning["msg"]
    assert warning["msg"] in result["warnings"]
    assert result["planned_total"] == 250
    assert result["errors"] == []


def test_capacity_risk_does_not_invent_response_times_or_change_workload():
    plain = configuration(traffic={"rates": [250], "duration_seconds": 1})
    estimated = configuration(traffic={"rates": [250], "duration_seconds": 1,
        "expected_response_seconds": 2})
    assert request_plan(plain)["capacity_warnings"] == []
    for key in ("rows", "planned_total", "required_concurrency", "request_settings"):
        assert request_plan(plain)[key] == request_plan(estimated)[key]
    enough = configuration(traffic={"rates": [1], "duration_seconds": 1,
        "expected_response_seconds": 1, "max_in_flight": 4})
    assert request_plan(enough)["capacity_warnings"] == []


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_capacity_expected_response_time_must_be_positive_and_finite(value):
    with pytest.raises(ValidationError):
        TrafficConfig(expected_response_seconds=value)


def test_extreme_but_finite_expected_response_time_keeps_plan_json_safe():
    result = request_plan(configuration(traffic={"rates": [250], "duration_seconds": 1,
        "expected_response_seconds": 1e308}))
    warning, = result["capacity_warnings"]
    assert warning["values"]["unfinished_requests"] == "2.500e+310"
    json.dumps(result, allow_nan=False)
