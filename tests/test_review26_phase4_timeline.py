"""Coverage remedies follow the same traffic order as the runner."""

from collections import Counter

import pytest


from giraffe import runner
from giraffe.fixtures import builtin_fixtures
from giraffe.latency import input_bucket, latency_summary
from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_runner import FakeClient


pytestmark = pytest.mark.usefixtures("local_tokenizer")


TARGET = {"name": "service", "url": "http://endpoint.invalid/v1", "model": "model"}


def config(**values):
    return RunConfig.model_validate({"targets": [TARGET], **values})


def observations(report):
    return {target["name"]: target["measurements"] for target in latency_summary(report)["targets"]}


def test_full_timeline_remedy_completes_windows_and_later_finite_work(monkeypatch):
    from tests.test_planning import load_config
    from tests.test_review26_phase4_accuracy import run_virtual
    async def scenario():
        class FixedLatency(FakeClient):
            delay = .005
        monkeypatch.setattr(runner, "LLMClient", FixedLatency)
        configured = load_config(duration=1, finish=.1, rates=[40], request_timeout_seconds=.1,
            test_options={"generation": {"samples": 220}, "json": {"samples": 4}})
        configured.checks = ["generation", "load", "json"]
        configured.structured_json = True
        reference = await runner.run_suite(configured)
        measured = observations(reference)
        limited = configured.model_copy(update={"max_duration_seconds": .03})
        plan = request_plan(limited, observations=measured)
        assert plan["errors"] == []
        assert [item["id"] for item in plan["time_timeline"]] == ["initial", "generation", "json", "load_combinations"]
        notice = next(item for item in plan["time_warnings"] if item["kind"] == "incomplete_coverage")
        assert notice["values"]["fully_known"] is True
        assert "local · Load test" in notice["cut_checks"]
        action = notice["actions"][0]
        assert action["value"] >= 2
        assert action == {"label": f"Use {action['value']} s", "field": "max_duration_seconds", "value": action["value"]}
        completed = await runner.run_suite(configured.model_copy(update={"max_duration_seconds": action["value"]}))
        assert Counter(record.scenario for record in completed.requests) == Counter(record.scenario for record in reference.requests)
        assert completed.limits_reached == []
        assert request_plan(configured.model_copy(update={"max_duration_seconds": action["value"]}), observations=measured)["time_warnings"] == []
    run_virtual(monkeypatch, scenario())


def test_unknown_after_covered_windows_is_risk_without_a_lower_budget_action():
    configured = config(checks=["capacity", "json"], max_duration_seconds=2,
                        structured_json=True,
                        traffic={"rates": [1], "duration_seconds": .5, "drain_timeout_seconds": .1})
    configured.load_test.request_timeout_seconds = .1
    plan = request_plan(configured)
    risk, = plan["time_warnings"]
    assert risk["kind"] == "estimate_risk"
    assert plan["errors"] == []
    assert not any(action.get("field") == "max_duration_seconds" for action in risk.get("actions", []))


@pytest.mark.parametrize("budget", [300, 600, 1200, 2400, 5000])
def test_default_suite_cut_detection_uses_known_highs_and_unknown_lower_bounds(budget):
    plan = request_plan(config(max_duration_seconds=budget, checks=list(RunConfig(targets=[TARGET]).checks)))
    assert plan["errors"] == []
    for notice in plan["time_warnings"]:
        for action in notice.get("actions", []):
            if action.get("field") == "max_duration_seconds":
                assert action["value"] > budget
    # Each window reserves the full 300 s request timeout for outstanding work.
    assert plan["budget_high_seconds"] == 4390
    assert any(notice["kind"] == "incomplete_coverage" for notice in plan["time_warnings"]) == (budget < 4390)
    assert any(notice["kind"] == "estimate_risk" for notice in plan["time_warnings"])


def test_cleanup_wait_does_not_hide_earlier_selected_work_that_can_start():
    configured = config(checks=["cancellation", "recovery"], max_duration_seconds=.1,
        test_options={"cancellation": {"cleanup_seconds": 5}, "recovery": {"sustained_seconds": 3}})
    plan = request_plan(configured)
    assert plan["errors"] == []
    ids = [item["id"] for item in plan["time_timeline"]]
    assert ids.index("cancellation") < ids.index("recovery_wait") < ids.index("recovery_prime")
    assert ids.index("recovery_baseline") < ids.index("sustained") < ids.index("recovery")
    coverage = plan["time_warnings"][0]
    assert coverage["kind"] == "incomplete_coverage"
    assert "service · Recovery after load" in coverage["cut_checks"]
    assert plan["confidence"] == "low"
    assert not any("limits.stop_control" in item or "recovery_baseline" in item
                   for item in plan["time_bound_reasons"])


async def test_cancellation_observations_cover_real_controls_and_probes(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    configured = config(checks=["cancellation"], samples=4)
    saved = await runner.run_suite(configured)
    plan = request_plan(configured, observations=observations(saved))
    assert {item["title"] for item in plan["time_components"]} == {
        "Stop-sequence control", "Cancellation & timeouts", "Cancellation health probes"}
    unknown, = plan["unknown_time_components"]
    assert unknown["id"] == "cancellation_intentional"
    assert unknown["requests"] == 2
    assert plan["confidence"] == "low"


async def test_context_observations_use_actual_calibrated_buckets(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    configured = config(checks=["context"], samples=4)
    saved = await runner.run_suite(configured)
    measured = observations(saved)
    plan = request_plan(configured, observations=measured)
    first, = plan["time_components"]
    assert first["id"] == "context_first_batch"
    assert first["requests"] == 6
    assert plan["unknown_time_components"] == []
    assert plan["confidence"] == "low"
    from giraffe.latency import calibrated_context_fixtures
    recovered = calibrated_context_fixtures(measured, "service", configured)
    assert {spec.fixture_id: spec.input_chars for spec in recovered} == {
        row.fixture_id: row.input_chars for row in saved.requests if "context" in row.check_ids}
    changed = configured.model_copy(deep=True)
    changed.targets[0].tokenizer = "different/tokenizer"
    assert calibrated_context_fixtures(measured, "service", changed) is None
    missing = {"service": [dict(row, context_profile={**row["context_profile"],
        "fixtures": row["context_profile"]["fixtures"][:-1]}) for row in measured["service"]]}
    assert calibrated_context_fixtures(missing, "service", configured) is None
    for row in measured["service"]:
        row.pop("context_profile", None)
    unknown = request_plan(configured, observations=measured)
    assert unknown["time_components"] == []
    assert {item["id"] for item in unknown["unknown_time_components"]} == {"context_first_batch"}


def test_one_component_cannot_mix_fixture_latencies_from_different_runs():
    configured = config(checks=["correctness"], samples=4)
    fixtures = builtin_fixtures(configured, check_id="correctness")["correctness"]
    # Supply distinct output caps to require two cohorts within one gather.
    fixtures[1].max_tokens = fixtures[0].max_tokens * 2
    from giraffe import planning
    original = planning.builtin_fixtures
    with pytest.MonkeyPatch.context() as patch:
        def custom(configuration, **kwargs):
            result = original(configuration, **kwargs)
            if kwargs.get("check_id") == "correctness":
                result["correctness"] = fixtures
            return result
        patch.setattr(planning, "builtin_fixtures", custom)
        measured = {"service": [{"family": "correctness", "concurrency": 4, "stream": True,
            "output_cap": fixture.max_tokens, "input_bucket": input_bucket(fixture.input_chars),
            "cohort": "second" if index == 1 else "first", "n": 8,
            "p50_ms": 5, "p90_ms": 10, "p99_ms": 15, "max_ms": 20}
            for index, fixture in enumerate(fixtures)]}
        result = request_plan(configured, observations=measured)
    assert result["time_components"] == []
    assert result["unknown_time_components"][0]["id"] == "correctness"
    assert result["confidence"] == "low"


def test_shared_request_budget_has_one_action_and_keeps_each_endpoint_explanation():
    configured = config(checks=["correctness", "json"], max_requests=12,
                        targets=[TARGET, {**TARGET, "name": "other"}])
    plan = request_plan(configured)
    notice, = plan["request_warnings"]
    assert notice["actions"][0] == {"label": "Use 24 total requests", "field": "max_requests", "value": 24}
    assert notice["targets"] == [{"target": name, "quota": 6, "cut_checks": ["json"]}
                                 for name in ("service", "other")]
    assert notice["cut_checks"] == ["json"]
    assert sum(action["field"] == "max_requests" for action in notice["actions"]) == 1
    assert "service:" in notice["msg"] and "other:" in notice["msg"]


def test_conversation_pause_follows_first_request_and_does_not_block_start():
    configured = config(checks=["sessions"], max_duration_seconds=.1,
                        sessions={"sessions": 2, "concurrency": 2, "modes": ["fixed"], "delay_seconds": 2})
    plan = request_plan(configured)
    assert plan["errors"] == []
    assert [item["id"] for item in plan["time_timeline"]] == [
        "sessions", "sessions_wait", "sessions"]
    assert plan["time_warnings"][0]["kind"] == "incomplete_coverage"


def test_cleanup_wait_remains_in_timeline_without_budget_for_prime():
    configured = config(checks=["cancellation", "recovery"], max_requests=10, sustained_seconds=0,
                        test_options={"cancellation": {"cleanup_seconds": 4}})
    plan = request_plan(configured)
    assert next(row for row in plan["rows"] if row["id"] == "recovery_prime")["requests_per_target"] == 0
    assert plan["minimum_seconds"] == 4
    ids = [item["id"] for item in plan["time_timeline"]]
    assert ids.index("cancellation") < ids.index("recovery_wait") < ids.index("recovery_baseline")


def test_context_precedes_pack_controls_when_capacity_is_not_selected():
    configured = config(checks=["context", "correctness"], max_duration_seconds=.1,
        request_source="request_pack", request_pack={"version": 1, "cases": [{
            "fixture_id": "question", "messages": [{"role": "user", "content": "Question?"}], "scorer": "none"}]})
    plan = request_plan(configured)
    assert [item["id"] for item in plan["time_timeline"]] == [
        "context_first_batch", "pack_control", "pack_loaded"]
    assert [row["id"] for row in plan["rows"]][:2] == ["pack_control", "pack_loaded"]
    assert [notice["kind"] for notice in plan["time_warnings"]] == ["estimate_risk"]
    assert plan["errors"] == []


def test_initial_timeout_is_reserved_for_budget_action_but_excluded_from_headline():
    from tests.test_planning import load_config
    configured = load_config(duration=1, finish=.15, rates=[10], max_duration_seconds=.5, request_timeout_seconds=.15)
    configured.checks = ["access", "load"]
    plan = request_plan(configured)
    assert plan["estimated_high_seconds"] == 1.15
    assert plan["budget_high_seconds"] == pytest.approx(1.3)
    assert plan["time_timeline"][0]["kind"] == "setup"
    assert {item["id"] for item in plan["time_components"]} == {"load_combinations"}
    action = plan["time_warnings"][0]["actions"][0]
    assert action == {"label": "Use 2 s", "field": "max_duration_seconds", "value": 2}
    assert plan["time_warnings"][0]["values"]["setup_allowance_seconds"] == .15


def test_setup_only_plan_does_not_claim_measured_traffic_confidence():
    plan = request_plan(config(checks=["access"]))
    assert plan["time_components"] == []
    assert plan["confidence"] == "low"
    assert plan["estimate_exclusions"] == ["setup/connection", "AI evaluation", "report generation"]


async def test_history_rejected_session_turns_leave_pauses_unknown(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    configured = config(checks=["sessions"], context_limit=192, sessions={
        "sessions": 2, "concurrency": 2, "modes": ["fixed"], "max_tokens": 16,
        "turns": ["Remember {value}.", "Repeat the value.", "Repeat it again.", "Repeat it once more."],
        "delay_seconds": .001})
    saved = await runner.run_suite(configured)
    assert len(saved.requests) == 2  # Each first turn fits; the next history does not.
    plan = request_plan(configured, observations=observations(saved))
    assert plan["confidence"] == "low"
    assert plan["time_components"] == []
    assert plan["estimated_low_seconds"] is plan["estimated_high_seconds"] is None
    pauses, requests = plan["unknown_time_components"]
    assert pauses["id"] == "sessions_wait"
    assert pauses["timeout_bound_seconds"] == .003
    assert requests["requests"] == 2
    assert "Conversation history admission determines the pauses" in plan["time_bound_reasons"][0]


@pytest.mark.parametrize("response_mode", ["stream", "complete"])
def test_logprob_gather_tail_uses_actual_batch_width_not_single_template(response_mode):
    from giraffe.logprob_workload import logprob_fixtures

    configured = config(checks=["consistency"], consistency={"levels": [1, 16], "shapes": ["short"],
        "repetitions": 2, "logprobs": {"priming_rounds": 1, "response_mode": response_mode}})
    template = logprob_fixtures(configured, builtin_fixtures(configured))["short"]
    measured = {"service": [{"family": "consistency", "concurrency": level, "stream": response_mode == "stream",
        "output_cap": template.max_tokens, "input_bucket": input_bucket(template.input_chars),
        "n": 100, "p50_ms": 10, "p90_ms": 10, "p99_ms": 100, "max_ms": 1000,
        "quantiles_ms": [10] * 91 + list(range(20, 101, 10)) + [1000], "cohort": "run"}
        for level in (1, 16)]}
    quiet, loaded = request_plan(configured, observations=measured)["time_components"]
    assert quiet["requests"] == loaded["requests"] == 48
    # Forty-eight serial requests and three 16-wide gather maxima are different
    # random sums. Their upper intervals include measured positive skew, not batch worst cases.
    from statistics import NormalDist
    mean = .01945
    variance = .9 * .01**2 + .09 * (.01**2 + .01 * .1 + .1**2) / 3 + .01 * (.1**2 + .1 + 1) / 3 - mean**2
    # Exact raw moments of the known atom + two uniform tail segments.
    second = variance + mean**2
    raw_third = (.9 * .01**3 + .09 * (.01**3 + .01**2 * .1 + .01 * .1**2 + .1**3) / 4
                 + .01 * (.1**3 + .1**2 + .1 + 1) / 4)
    third = raw_third - 3 * mean * second + 2 * mean**3
    predicted_variance = 48 * variance * 1.48
    predicted_third = 48 * third * 1.48**1.5
    z = NormalDist().inv_cdf(.995)
    expected_high = (48 * mean + z * predicted_variance**.5
                     + (z*z - 1) * predicted_third / (6 * predicted_variance))
    assert quiet["low_seconds"] == 0
    assert quiet["high_seconds"] == pytest.approx(expected_high, rel=.001)
    assert loaded["low_seconds"] < loaded["high_seconds"] < quiet["high_seconds"]
    assert loaded["concurrency"] == 16
