"""Load summaries retain details in conditions and give one actionable cause."""

from copy import deepcopy

import pytest

from giraffe import load_test
from giraffe.models import RunConfig
from giraffe.runner import _Run
from tests.test_load_test_phase_a import values


def summary(monkeypatch, *, causes, pattern="constant", budget=3, targets=1, reason=None):
    data = values()
    data["max_requests"] = budget
    data["max_duration_seconds"] = 300
    data["targets"] = [{**data["targets"][0], "name": f"target-{i}"} for i in range(targets)]
    data["load_test"]["patterns"] = {
        "concurrent": {"enabled": pattern == "concurrent", "levels": [1]},
        "constant": {"enabled": pattern == "constant"},
        "poisson": {"enabled": pattern == "poisson"},
        "rates": [10],
    }
    config = RunConfig.model_validate(data)
    run = _Run(config, None, None)
    run.reason = reason
    run.budget_counts[config.targets[0].name] = budget // targets
    template = load_test.test_points(config.load_test)[0]
    points = []
    for index, cause in enumerate(causes):
        point = deepcopy(template)
        point.update(id=f"condition-{index}", status="inconclusive", reason_code=cause,
                     reason=f"Full condition reason {index}. More detail stays here.",
                     window_reason='maximum run duration reached' if cause == 'time_limited' else None,
                     dispatched=budget // targets if index == 0 else 0,
                     groups=[{"valid": budget // targets if index == 0 else 0}])
        points.append(point)
    run.observations[config.targets[0].name]["load_test"] = {"points": points}
    # Result aggregation must preserve the executor's complete condition records.
    monkeypatch.setattr(load_test, "assess_point", lambda *args: None)
    monkeypatch.setattr(load_test, "publish_point", lambda *args: None)
    return load_test.result(run, config.targets[0]), run


def test_budget_summary_counts_conditions_and_retains_details(monkeypatch):
    check, _ = summary(monkeypatch, causes=["budget_limited"] * 9)
    assert check.summary == (
        "9 of 9 load conditions unfinished: request budget reached. "
        "Raise --max-requests (max_requests) to at least 10 or select fewer rates."
    )
    assert "Full condition reason 0. More detail stays here." == check.metrics["points"][0]["reason"]
    assert len(check.summary.split(". ")) == 2
    assert ";" not in check.summary


def test_budget_summary_uses_seeded_arrival_count(monkeypatch):
    check, run = summary(monkeypatch, causes=["budget_limited"], pattern="poisson")
    point = check.metrics["points"][0]
    planned = len(load_test.planned_arrivals(run.config.load_test, point)[0])
    assert f"at least {max(4, planned)} or select fewer rates" in check.summary


def test_budget_summary_accounts_for_per_target_quota(monkeypatch):
    check, _ = summary(monkeypatch, causes=["budget_limited"], budget=6, targets=2)
    # Both targets need ten slots on a fresh rerun.
    assert "at least 20 or select fewer rates" in check.summary


def test_duration_concurrency_budget_advises_time_bound_without_a_request_number(monkeypatch):
    check, _ = summary(monkeypatch, causes=["budget_limited"], pattern="concurrent")
    assert "at least" not in check.summary
    assert "--max-duration (max_duration_seconds)" in check.summary
    assert "--no-max-requests (max_requests: null)" in check.summary
    assert "Duration-based" in check.summary
    assert len(check.summary.split(". ")) == 2


def test_summary_selects_dominant_cause_and_only_one_remedy(monkeypatch):
    check, _ = summary(monkeypatch, causes=["missing_timing", "missing_judgments", "missing_judgments"])
    assert check.summary == (
        "3 of 3 load conditions unfinished: answer judgments missing. "
        "Configure answer scorers and collect the required scored responses."
    )


def test_measure_only_summary_names_acceptance_keys(monkeypatch):
    check, _ = summary(monkeypatch, causes=["measure_only"])
    assert "load_test.pass_criteria.max_first_answer_ms" in check.summary
    assert "load_test.pass_criteria.max_completion_ms" in check.summary
    assert len(check.summary.split(". ")) == 2


def test_deadline_summary_names_duration_minimum(monkeypatch):
    check, _ = summary(monkeypatch, causes=["time_limited"])
    assert "run deadline reached" in check.summary
    assert "--max-duration (max_duration_seconds) to at least 301 seconds" in check.summary
    assert "max-requests" not in check.summary


def test_user_stop_never_proposes_budget_remedy(monkeypatch):
    check, _ = summary(monkeypatch, causes=["budget_limited"], reason="cancelled by user")
    assert check.summary == "1 of 1 load conditions unfinished: stopped by you."
    assert "Raise" not in check.summary


def test_advised_budget_covers_the_entire_selected_plan_on_rerun(monkeypatch):
    from giraffe.planning import request_plan
    check, run = summary(monkeypatch, causes=['budget_limited'], budget=3)
    config = run.config.model_copy(update={'checks': ['serving', 'load']})
    run = _Run(config, None, None)
    points = load_test.test_points(config.load_test)
    for point in points:
        point['dispatched'] = 1
        point['groups'] = [{'valid': 1}]
    advised, unknown = load_test._minimum_request_budget(run)
    assert advised is not None and unknown is None
    fresh = request_plan(config.model_copy(update={'max_requests': advised}))
    assert advised == fresh['planned_total']
    assert advised > 3
    assert not any(warning['kind'] == 'incomplete_coverage' for warning in fresh['request_warnings'])
    # Every planned request can be admitted on a fresh run, not just the remainder.
    assert sum(row['requests_per_target'] or 0 for row in fresh['rows']) <= fresh['target_quotas'][config.targets[0].name]
    rerun = _Run(config.model_copy(update={'max_requests': advised}), None, None)
    for target in config.targets:
        for _ in range(sum(row['requests_per_target'] for row in fresh['rows'])):
            assert rerun.reserve_offer(target.name)
    assert rerun.budget_used == advised
    assert not any(rerun.unfinished.values())



def test_one_deadline_condition_does_not_replace_dominant_missing_judgments(monkeypatch):
    check, _ = summary(monkeypatch, causes=['missing_judgments'] * 8 + ['time_limited'],
                       reason='maximum run duration reached')
    assert check.summary == (
        '9 of 9 load conditions unfinished: answer judgments missing. '
        'Configure answer scorers and collect the required scored responses.'
    )


def test_default_twelve_duration_conditions_never_advertise_a_request_minimum(monkeypatch):
    config = RunConfig(targets=[{'name': 'local', 'url': 'http://server', 'model': 'test'}],
                       checks=['load'], max_requests=165)
    run = _Run(config, None, None)
    points = load_test.test_points(config.load_test)
    assert len(points) == 12
    for point in points:
        point.update(status='inconclusive', reason_code='budget_limited',
                     reason='Request budget reached', groups=[])
    run.observations['local']['load_test'] = {'points': points}
    monkeypatch.setattr(load_test, 'assess_point', lambda *args: None)
    monkeypatch.setattr(load_test, 'publish_point', lambda *args: None)
    check = load_test.result(run, config.targets[0])
    assert check.summary.startswith('12 of 12 load conditions unfinished: request budget reached.')
    assert 'at least' not in check.summary
    assert '--no-max-requests (max_requests: null)' in check.summary
    assert '--max-duration (max_duration_seconds)' in check.summary
    assert len(check.summary.split('. ')) == 2


def test_poisson_overflow_never_advertises_a_sufficient_request_budget(monkeypatch):
    check, run = summary(monkeypatch, causes=['budget_limited'], pattern='poisson')
    monkeypatch.setattr(load_test, 'MAX_PLANNED_ARRIVALS', 10)
    _, overflow = load_test.planned_arrivals(run.config.load_test, check.metrics['points'][0])
    assert overflow
    check = load_test.result(run, run.config.targets[0])
    assert 'at least' not in check.summary
    assert 'load_test.patterns.rates' in check.summary
    assert 'load_test.schedule.duration_seconds' in check.summary
    assert 'Reduce' in check.summary
    assert '--no-max-requests' not in check.summary
    assert len(check.summary.split('. ')) == 2


def test_seeded_poisson_budget_admits_all_arrivals_and_fixed_work_for_each_target(monkeypatch):
    from giraffe.planning import request_plan
    _, run = summary(monkeypatch, causes=['budget_limited'], pattern='poisson', targets=2)
    config = run.config.model_copy(update={'checks': ['serving', 'load'], 'max_requests': None})
    run = _Run(config, None, None)
    advised, unknown = load_test._minimum_request_budget(run)
    assert unknown is None
    fixed = [row for row in request_plan(config)['rows'] if not row['id'].startswith('load_')]
    actual = sum(row['requests_per_target'] for row in fixed)
    for point in load_test.test_points(config.load_test):
        offers, overflow = load_test.planned_arrivals(config.load_test, point)
        assert not overflow
        actual += len(offers)
    rerun = _Run(config.model_copy(update={'max_requests': advised}), None, None)
    for target in config.targets:
        for _ in range(actual):
            assert rerun.reserve_offer(target.name)
    assert rerun.budget_used == advised == actual * len(config.targets)


@pytest.mark.parametrize(("reason_code", "cause", "remedy"), [
    ("missing_case_coverage", "saved questions lack scored responses",
     "Lengthen load_test.schedule.duration_seconds"),
    ("incomplete_delivery", "scheduled requests were not delivered",
     "Reduce the offered rates or raise --simultaneous-limit"),
])
def test_measure_only_load_keeps_detail_out_of_concise_stop_summary(
        monkeypatch, reason_code, cause, remedy):
    # Summary policy consumes recorded conditions; it must not depend on whether
    # a busy test runner can deliver every offer in a 50 ms traffic window.
    _, run = summary(monkeypatch, causes=[reason_code], budget=6)
    run.config.max_requests = None
    criteria = run.config.load_test.pass_criteria
    criteria.max_first_answer_ms = criteria.max_completion_ms = None
    target = run.config.targets[0]
    point = run.observations[target.name]["load_test"]["points"][0]
    group = point["groups"][0]
    group.update(scored=0, unscored=group["valid"])
    detail = point["reason"]
    result = load_test.result(run, target)

    assert result.status == "inconclusive"
    assert result.summary.startswith(f"1 of 1 load conditions unfinished: {cause}.")
    assert remedy in result.summary
    assert "--max-requests" not in result.summary
    assert "load_test.pass_criteria.max_first_answer_ms" in result.summary
    assert "load_test.pass_criteria.max_completion_ms" in result.summary
    assert "configure scorers for unscored answers" in result.summary
    assert len(result.summary.split(". ")) == 2
    assert detail not in result.summary
    recorded = result.metrics["points"][0]
    assert recorded["reason_code"] == reason_code
    assert recorded["reason"] == detail
    assert recorded["groups"][0]["scored"] == 0
    assert recorded["groups"][0]["unscored"] == recorded["groups"][0]["valid"] == 6
