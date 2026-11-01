"""Final acceptance regressions for duration formatting and budget risk provenance."""

import json

import pytest

from giraffe.planning import request_plan
from tests.test_run_limits_phase4_planning import config, observations
from tests.test_ux_catalogue import browser_logic


def test_short_estimate_names_connection_risk_without_contradicting_headline():
    configured = config(checks=["access", "correctness"], max_duration_seconds=5)
    plan = request_plan(configured, observations=observations(configured, p50=10, p90=10))
    assert 0 < plan["estimated_high_seconds"] < 1
    assert plan["confidence"] == "medium"
    assert plan["unknown_time_components"] == []
    risk, = plan["time_warnings"]
    assert risk["msg"] == (
        "Endpoint access for service may wait up to 30 s if the server is slow to respond; "
        "this allowance shares the time limit and is excluded from the estimated duration.")
    assert risk["fields"] == ["max_duration_seconds", "test_options.access.request_timeout_seconds"]
    assert [item["id"] for item in risk["components"]] == ["initial"]
    browser_logic("const plan=" + json.dumps(plan) + r""";
const markup=renderPlanTime(plan);
assert.match(markup,/Confidence: medium/);
assert.match(markup,/Endpoint access for service may wait up to 30 s/);
assert.doesNotMatch(markup,/estimated traffic duration may exceed|Unknown requests/);
assert.match(markup,/data-setting-path="test_options.access.request_timeout_seconds"/);
""")


def test_no_budget_risk_when_connection_and_traffic_allowances_fit():
    configured = config(checks=["access", "correctness"], max_duration_seconds=5,
                        test_options={"access": {"request_timeout_seconds": 1}})
    plan = request_plan(configured, observations=observations(configured, p50=10, p90=10))
    assert plan["budget_high_seconds"] < configured.max_duration_seconds
    assert plan["time_warnings"] == []


def test_unknown_risk_names_check_and_timeout_instead_of_claiming_estimate_exceeds():
    plan = request_plan(config(max_duration_seconds=5))
    risk, = plan["time_warnings"]
    assert risk["msg"] == (
        "service · Answer correctness has no comparable timing measurements; its requests may take up to "
        "60 s in total if each reaches its 30 s timeout.")
    assert risk["fields"] == ["max_duration_seconds", "test_options.correctness"]


def test_headline_over_budget_is_explicit_without_fictitious_unknown_requests():
    configured = config(max_duration_seconds=.001)
    plan = request_plan(configured, observations=observations(configured, p50=10, p90=10))
    assert plan["estimated_high_seconds"] > configured.max_duration_seconds
    risk = next(item for item in plan["time_warnings"] if item["kind"] == "estimate_risk")
    assert risk["msg"] == "The estimated traffic duration may exceed the time limit, leaving checks unfinished."
    assert risk["components"] == []


@pytest.mark.parametrize("low,high,expected", [
    (.1, .1, "about 0.1 s"), (.1001, .1002, "about 0.1 s"),
    (.101, .109, "about 0.1 s–0.11 s"), (3, 5, "about 3 s–5 s"),
    (60, 60, "about 1m 0s"),
])
def test_duration_range_never_repeats_identical_formatted_endpoints(low, high, expected):
    browser_logic(f"""
const plan={{estimated_seconds:{(low + high) / 2},estimated_low_seconds:{low},estimated_high_seconds:{high}}};
const markup=renderPlanTime(plan);
assert(markup.includes('Estimated duration: {expected} · test traffic only'),markup);
assert.equal((markup.match(/Estimated duration:/g)||[]).length,1);
""")
