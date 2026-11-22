"""Run-plan estimates require complete evidence; deadline allowances retain phase math."""

import pytest

from giraffe.models import ConfigDraft
from giraffe.planning import request_plan
from tests.test_run_limits_phase4_planning import observations


TARGET = {"name": "service", "url": "http://endpoint.invalid/v1", "model": "model"}


def configuration(**updates):
    return ConfigDraft.model_validate({"targets": [TARGET], "request_timeout_seconds": 300, **updates})


def check_time(plan, identifier, target="service"):
    return next(item for item in plan["check_time_summaries"]
                if item["check_id"] == identifier and item["target"] == target)


@pytest.mark.parametrize("checks,low,high", [
    (["load"], 180, 3780),
    (["load", "correctness"], 180, 3780),
    (["first_output", "generation", "load", "multi_user"], 225, 4725),
])
def test_configured_windows_and_deadline_bounds_are_not_complete_estimates(checks, low, high):
    plan = request_plan(configuration(checks=checks))

    assert plan["has_complete_time_estimate"] is False
    # These existing values remain available to budget and coverage consumers.
    assert plan["estimated_low_seconds"] == low
    assert plan["estimated_high_seconds"] == high
    load = check_time(plan, "load")
    assert load["configured_seconds"] == 180
    assert load["allowance_seconds"] == 3600
    assert load["total_seconds"] == 3780
    phase, = load["phases"]
    assert phase["batches"] == 12
    assert phase["request_timeout_seconds"] == 300


def test_eta_requires_comparable_timings_for_every_request_component():
    configured = configuration(checks=["correctness", "json"])
    measured = observations(configured)
    partial = request_plan(configured, observations=measured)
    assert partial["has_complete_time_estimate"] is False

    measured["service"] += observations(configured, "json")["service"]
    complete = request_plan(configured, observations=measured)
    assert complete["has_complete_time_estimate"] is True
    assert complete["unknown_time_components"] == []
    assert complete["estimated_seconds"] > 0

    for row in measured["service"]:
        row["family"] = "unrelated"
    assert request_plan(configured, observations=measured)["has_complete_time_estimate"] is False


def test_initial_selected_request_is_not_silently_excluded_from_eta_coverage():
    configured = configuration(checks=["access", "correctness"])
    measured = observations(configured)
    plan = request_plan(configured, observations=measured)

    assert plan["unknown_time_components"] == []
    assert plan["estimated_seconds"] > 0
    assert plan["has_complete_time_estimate"] is False
    assert check_time(plan, "access")["phases"][0]["id"] == "initial"
    assert plan["setup_time_summaries"] == []


def test_known_conversation_pauses_do_not_supply_request_timing_evidence():
    plan = request_plan(configuration(checks=["sessions"], sessions={"delay_seconds": 3}))

    assert plan["estimated_seconds"] == 18
    assert plan["has_complete_time_estimate"] is False
    summary = check_time(plan, "sessions")
    assert summary["configured_seconds"] == 18
    assert summary["allowance_seconds"] == 3600
    assert summary["total_seconds"] == 3618


def test_conversation_allowance_counts_all_batches_once():
    plan = request_plan(configuration(checks=["sessions"], max_duration_seconds=1000))
    summary = check_time(plan, "sessions")
    phase, = summary["phases"]

    assert phase["requests"] == 20
    assert phase["batches"] == 2 * 3 * 2
    assert phase["request_timeout_seconds"] == 300
    assert summary["total_seconds"] == phase["total_seconds"] == 3600
    assert len([item for item in plan["time_timeline"] if item["id"] == "sessions"]) == 12
    risk, = plan["time_warnings"]
    assert "3,600 s in total" in risk["msg"]
    assert "300 s in total" not in risk["msg"]
    assert risk["check_time_summaries"] == plan["check_time_summaries"]


def test_recovery_total_retains_equal_before_and_after_probe_groups():
    plan = request_plan(configuration(checks=["recovery"], max_duration_seconds=1000))
    summary = check_time(plan, "recovery")
    phases = {item["id"]: item for item in summary["phases"]}

    assert phases["recovery_prime"]["allowance_seconds"] == 300
    assert phases["recovery_baseline"]["allowance_seconds"] == 1800
    assert phases["recovery"]["allowance_seconds"] == 1800
    assert phases["recovery_baseline"]["title"] != phases["recovery"]["title"]
    assert sum(phases[name]["allowance_seconds"] for name in
               ("recovery_prime", "recovery_baseline", "recovery")) == 3900
    assert phases["sustained"]["configured_seconds"] == 5
    assert phases["sustained"]["allowance_seconds"] == 300
    assert summary["configured_seconds"] == 5
    assert summary["total_seconds"] == 4205
    risk, = plan["time_warnings"]
    assert "4,205 s in total" in risk["msg"]


@pytest.mark.parametrize("limit,exceeds", [(100, True), (180, False), (181, False), (1000, False)])
def test_configured_schedule_overrun_is_distinct_from_timeout_risk(limit, exceeds):
    plan = request_plan(configuration(checks=["load"], max_duration_seconds=limit))

    assert plan["minimum_seconds"] == 180
    assert plan["schedule_exceeds_limit"] is exceeds
    assert plan["could_exceed_limit"] is not exceeds
    risk = next(item for item in plan["time_warnings"] if item["kind"] == "estimate_risk")
    assert risk["schedule_exceeds_limit"] is exceeds
    assert risk["could_exceed_limit"] is not exceeds
    assert ("already exceeds" in risk["msg"]) is exceeds
    assert "estimated traffic duration" not in risk["msg"]


@pytest.mark.parametrize("overlap,risk", [(False, True), (True, False)])
def test_endpoint_allowances_stay_separate_and_overlap_uses_existing_budget_math(overlap, risk):
    plan = request_plan(configuration(checks=["sessions"], max_duration_seconds=5000,
        overlap_models=overlap, targets=[TARGET, {**TARGET, "name": "other"}]))

    assert [(item["target"], item["total_seconds"]) for item in plan["check_time_summaries"]] == [
        ("service", 3600), ("other", 3600)]
    assert plan["schedule_exceeds_limit"] is False
    assert plan["could_exceed_limit"] is risk
    assert any(item["kind"] == "estimate_risk" for item in plan["time_warnings"]) is risk


@pytest.mark.parametrize("overlap,minimum,exceeds", [(False, 360, True), (True, 180, False)])
def test_configured_schedule_limit_uses_endpoint_overlap(overlap, minimum, exceeds):
    plan = request_plan(configuration(checks=["load"], max_duration_seconds=200,
        overlap_models=overlap, targets=[TARGET, {**TARGET, "name": "other"}]))

    assert plan["minimum_seconds"] == minimum
    assert plan["schedule_exceeds_limit"] is exceeds
    assert plan["could_exceed_limit"] is not exceeds
    assert [item["total_seconds"] for item in plan["check_time_summaries"]] == [3780, 3780]


def test_first_answer_total_includes_initial_and_all_followup_deadlines():
    plan = request_plan(configuration(checks=["first_output"], max_duration_seconds=1000))
    summary, = plan["check_time_summaries"]

    assert summary["check_id"] == "first_output"
    assert summary["title"] == "First answer"
    assert [(phase["title"], phase["requests"], phase["allowance_seconds"]) for phase in summary["phases"]] == [
        ("Initial request", 1, 300), ("Follow-up requests", 6, 1800)]
    assert summary["total_seconds"] == 2100
    assert plan["setup_time_summaries"] == []
    assert plan["known_per_target"] == 7
    assert plan["has_complete_time_estimate"] is False


@pytest.mark.parametrize("checks,owner", [
    (["access", "serving", "first_output", "generation"], "access"),
    (["serving", "first_output"], "serving"),
    (["first_output", "generation"], "first_output"),
    (["generation"], "generation"),
])
def test_shared_initial_deadline_has_one_selected_owner(checks, owner):
    plan = request_plan(configuration(checks=checks))
    owners = [summary["check_id"] for summary in plan["check_time_summaries"]
              for phase in summary["phases"] if phase["id"] == "initial"]

    assert owners == [owner]
    assert plan["setup_time_summaries"] == []
    assert next(item for item in plan["time_timeline"] if item["id"] == "initial")["kind"] == "setup"
    if checks == ["generation"]:
        summary = check_time(plan, "generation")
        assert summary["total_seconds"] == 2100
        assert sum(phase["requests"] for phase in summary["phases"]) == 7


def test_external_restart_allowances_stay_separate_from_selected_request_phases():
    plan = request_plan(configuration(checks=["first_output"], restart_target="service", stream=False,
        readiness_timeout_seconds=120, targets=[{**TARGET, "restart_command": ["restart-service"]}]))
    setup, = plan["setup_time_summaries"]

    assert setup["check_id"] is None
    assert setup["total_seconds"] == 420
    assert {phase["id"] for phase in setup["phases"]} == {"restart_hook", "restart_readiness"}
    assert all(phase["kind"] == "setup" for phase in setup["phases"])
    assert all(phase["id"] != "initial" for item in plan["check_time_summaries"] for phase in item["phases"])
    assert plan["has_complete_time_estimate"] is False


def test_unbounded_request_work_never_gains_a_finite_total_or_eta():
    plan = request_plan(configuration(checks=["correctness"], custom_fixtures="/not/imported",
                                      max_duration_seconds=1000))
    summary = check_time(plan, "correctness")

    assert summary["unbounded"] is True
    assert summary["total_seconds"] is summary["allowance_seconds"] is None
    assert plan["has_complete_time_estimate"] is False
    assert plan["could_exceed_limit"] is True


def test_context_warning_has_a_stable_kind_and_explains_the_consequence():
    plan = request_plan(configuration(checks=["load"]))
    warning, = [item for item in plan["load_warnings"] if item.get("kind") == "context_window_unknown"]

    assert warning["msg"] == "Giraffe can't verify that your longest request fits this endpoint."
    assert warning["fields"] == ["load_test.profile.long.input_tokens"]
    assert warning["actions"]
