"""Setup counts describe the runner's workload without executing it."""

import pytest

from giraffe.models import CHECK_NAMES, ConfigDraft, RunConfig


@pytest.fixture(autouse=True)
def isolated_endpoint_preparation(monkeypatch):
    async def unavailable(*args, **kwargs):
        return {"status": "unavailable", "reason": "Fake endpoint has no exporter",
                "samples": [], "elapsed_seconds": 0, "unavailable": []}
    monkeypatch.setattr("giraffe.telemetry.scrape", unavailable)
    monkeypatch.setattr("giraffe.client.LLMClient.token_evidence_probe", lambda self: None)


def config(**updates):
    return RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://localhost:11434/v1", "model": "test"}],
        "checks": ["access", "correctness"], "request_timeout_seconds": 30, **updates,
    })


def plan(configuration):
    from giraffe.planning import request_plan

    return request_plan(configuration)


def load_config(*, duration=10, finish=2, rates=None, concurrent=False, poisson=False, **updates):
    """One public #45 workload, with a known dataset so setup adds no calibration."""
    return config(checks=["load"], request_source="request_pack", request_pack={
        "version": 1, "cases": [{"fixture_id": "one", "messages": [{"role": "user", "content": "Return A"}],
                                  "scorer": "exact", "expected": "A"}]},
        load_test={"workloads": {"combinations": {"selected": ["short_short"]}},
                   "patterns": {"concurrent": {"enabled": concurrent, "levels": [1]},
                                "constant": {"enabled": not poisson},
                                "poisson": {"enabled": poisson}, "rates": rates or [2]},
                   # Keep the shared fixture argument for existing callers; the
                   # allowance now comes only from each request's timeout.
                   "schedule": {"duration_seconds": duration},
                   "request_timeout_seconds": max(finish, .000001),
                   "pass_criteria": {"max_completion_ms": 1000}}, **updates)


def test_time_plan_separates_unmeasured_sequential_phases_and_check_timeouts():
    result = plan(config(checks=["access", "generation", "correctness"], samples=2,
                         request_timeout_seconds=10, test_options={
                             "access": {"request_timeout_seconds": 4},
                             "generation": {"samples": 3},
                             "correctness": {"samples": 4, "concurrency": 2}}))
    assert result["estimated_seconds"] is result["worst_case_seconds"] is None
    assert [(row["id"], row["timeout_bound_seconds"]) for row in result["unknown_time_components"]] == [
        ("generation", 30), ("correctness", 20)]
    assert result["minimum_seconds"] == 0
    assert result["errors"] == []
    assert "setup/connection" in result["estimate_exclusions"]


def test_time_plan_partial_capacity_windows_warn_and_timeouts_warn_separately():
    # #36/#45 formerly blocked partial windows. Phase 4 makes coverage explicit.
    configuration = load_config(duration=5, finish=3, rates=[1, 2], max_duration_seconds=9)
    result = plan(configuration)
    assert result["minimum_seconds"] == 10
    assert result["worst_case_seconds"] == 16
    assert result["errors"] == []
    coverage, risk = result["time_warnings"]
    assert coverage["fields"] == ["max_duration_seconds", "load_test.schedule.duration_seconds"]
    assert coverage["cut_checks"] == ["local · Load test"]
    assert coverage["actions"][0] == {"label": "Use 16 s", "field": "max_duration_seconds", "value": 16}
    assert risk["kind"] == "estimate_risk"
    # Every test includes its finish allowance; the second window is still cut at 12 s.
    configuration.max_duration_seconds = 12
    assert plan(configuration)["time_warnings"][0]["kind"] == "incomplete_coverage"
    configuration.max_duration_seconds = 16
    assert plan(configuration)["time_warnings"] == []


def test_adaptive_work_reports_unknown_time_with_reason():
    result = plan(config(checks=["correctness"], custom_fixtures="/not/imported"))
    assert result["estimated_seconds"] is None
    assert result["worst_case_seconds"] is None
    assert result["time_bound_reasons"]


def test_readiness_is_excluded_and_unmeasured_target_work_stays_separate():
    values = dict(checks=["access", "serving"], samples=2, restart_target="local",
                  request_timeout_seconds=10, readiness_timeout_seconds=20,
                  targets=[{"name": "local", "url": "http://localhost:1", "model": "a"},
                           {"name": "other", "url": "http://localhost:2", "model": "b"}])
    result = plan(config(**values))
    assert result["worst_case_seconds"] is result["estimated_seconds"] is None
    assert [(row["target"], row["id"], row["timeout_bound_seconds"])
            for row in result["unknown_time_components"]] == [("local", "warm", 20), ("other", "warm", 20)]
    assert "setup/connection" in result["estimate_exclusions"]
    overlap = plan(config(**values, overlap_models=True))
    assert overlap["unknown_time_components"] == result["unknown_time_components"]


def test_context_unknown_timeout_bounds_and_sessions_include_fixed_pauses():
    result = plan(config(checks=["context"], samples=2, request_timeout_seconds=10))
    first = next(row["requests_per_target"] for row in result["rows"] if row["id"] == "context_first_batch")
    assert sum(row["timeout_bound_seconds"] for row in result["unknown_time_components"]) == first * 10
    assert result["estimated_seconds"] is None
    sessions = plan(config(checks=["sessions"], request_timeout_seconds=10,
                           sessions={"modes": ["live"], "sessions": 2, "delay_seconds": 3}))
    assert sessions["estimated_seconds"] == 3  # Only known conversation pauses enter the headline.
    assert sessions["unknown_time_components"][0]["timeout_bound_seconds"] == 20


def test_empty_poisson_stage_still_warns_about_its_cut_sending_window():
    result = plan(load_config(duration=2, rates=[.01], poisson=True, max_duration_seconds=1))
    assert result["load_test"]["expected_scheduled_requests"] == .02
    assert result["load_test"]["approximate"] is True
    assert result["known_total"] == 0
    assert result["minimum_seconds"] == 2
    assert result["errors"] == []
    assert result["time_warnings"][0]["kind"] == "incomplete_coverage"


def test_arrivals_estimate_follows_schedule_with_one_timeout_drain():
    result = plan(load_config(duration=60, finish=30, rates=[10], concurrency=16))
    assert result["estimated_low_seconds"] == 60
    assert result["estimated_high_seconds"] == 90
    assert result["load_test"]["expected_scheduled_requests"] == 600
    assert not any(row["id"] == "arrivals_pending" for row in result["time_components"])


def test_consistency_unknown_bounds_use_each_levels_pool_and_sequential_phases():
    result = plan(config(checks=["consistency"], concurrency=8, consistency={"levels": [1, 8]}))
    assert result["planned_total"] == 78
    assert result["estimated_seconds"] is None
    assert sum(row["timeout_bound_seconds"] for row in result["unknown_time_components"]) == 36 * 30


def test_sessions_fixed_pauses_pool_conversations_and_keep_turns_in_order():
    result = plan(config(checks=["sessions"], concurrency=4, sessions={"sessions": 8,
        "concurrency": 4, "modes": ["fixed", "live"], "delay_seconds": 3}))
    assert result["estimated_seconds"] == 12  # Fixed pauses exclude unknown request durations.
    assert result["unknown_time_components"][0]["timeout_bound_seconds"] == 8 * 30
    result = plan(config(checks=["sessions"], concurrency=4, sessions={"profile": "performance",
        "modes": ["live"], "sessions": 8, "concurrency": 4, "delay_seconds": 3,
        "workloads": [{"name": "chat", "turns": ["First", "Second", "Third"]}]}))
    assert result["estimated_seconds"] == 12  # Fixed pauses exclude unknown request durations.
    assert result["unknown_time_components"][0]["timeout_bound_seconds"] == 6 * 30


@pytest.mark.parametrize("checks,expected", [
    (["access", "correctness"], 7),
    (["access", "correctness", "capacity"], 42),
])
def test_basic_presets_have_complete_counts_before_caps(checks, expected):
    if "capacity" in checks:
        configuration = load_config(duration=5, finish=0, rates=[7], max_requests=2)
        configuration.checks = ["access", "correctness", "load"]
        result = plan(configuration)
        # One initial + one quiet control + 35 scheduled load offers; load supplies the loaded pass.
        expected = 37
    else:
        result = plan(config(checks=checks, max_requests=2))
    assert result["count_kind"] == "exact"
    assert result["planned_per_target"] == result["known_per_target"] == expected
    assert result["planned_total"] == result["required_request_limit"] == expected
    assert result["request_limit"] == 2
    assert any("share of the request limit (Maximum requests:" in warning for warning in result["warnings"])


@pytest.mark.parametrize("check,updates,expected", [
    ("serving", {}, 7),
    ("first_output", {}, 7),
    ("generation", {"test_options": {"generation": {"samples": 3}}}, 4),
    ("correctness", {"test_options": {"correctness": {"samples": 1}}}, 3),
    ("json", {}, 6),
    ("cancellation", {}, 16),
    ("recovery", {"sustained_seconds": 0}, 13),
    ("gpu", {}, 0),
    ("arrivals", {}, 8),
    ("prefix", {}, 15),
    ("consistency", {"consistency": {"levels": [1, 2]}}, 66),
    ("tools", {"forced_tool_diagnostic": True}, 4),
])
def test_fixed_specialist_workloads_follow_their_own_runner_controls(check, updates, expected):
    result = plan(load_config(duration=4, rates=[2]) if check == "arrivals" else config(checks=[check], **updates))
    assert result["count_kind"] == "exact"
    assert result["planned_per_target"] == result["known_per_target"] == expected








def test_target_quotas_warn_for_the_smaller_share():
    result = plan(config(max_requests=13, targets=[
        {"name": "first", "url": "http://localhost:1", "model": "a"},
        {"name": "second", "url": "http://localhost:2", "model": "b"},
    ]))
    assert result["target_quotas"] == {"first": 7, "second": 6}
    assert result["planned_total"] == result["known_total"] == 14
    assert result["required_request_limit"] == 14
    assert any("second" in warning and "6" in warning for warning in result["warnings"])


def test_initial_warm_and_dataset_controls_are_reused_once():
    from giraffe.request_pack import pack_preview

    configuration = config(checks=["access", "serving", "first_output", "correctness", "capacity"],
                           request_source="request_pack", request_pack={"version": 1, "cases": [
                               {"fixture_id": "one", "messages": [{"role": "user", "content": "A?"}],
                                "scorer": "exact", "expected": "A"},
                               {"fixture_id": "two", "messages": [{"role": "user", "content": "B?"}],
                                "scorer": "judge", "expected": "B"},
                           ]}, traffic={"rates": [1], "duration_seconds": 3},
                           judging={"judges": [{"name": "judge", "url": "http://localhost", "model": "j"}]})
    result = plan(configuration)
    assert result["planned_per_target"] == 1 + 6 + 2 + 3
    assert sum(row["requests_per_target"] for row in result["rows"]) == 12
    controls = next(row for row in result["rows"] if row["id"] == "pack_control")
    assert controls["requests_per_target"] == 2
    assert "dataset" in controls["source"]
    expected = pack_preview(configuration)["judging"]
    assert result["evaluator"]["call_upper_bound"] == expected["planned_call_upper_bound"]
    assert result["evaluator"]["ready"]
    assert result["evaluator"]["judge_count"] == 1




@pytest.mark.parametrize("rate,duration", [(1, 2.5), (3, .7), (.3, 10), (7, 1)])
def test_steady_rate_counts_match_schedule_semantics(rate, duration):
    import math
    from giraffe.load_test import planned_arrivals, test_points
    # #45 windows are integral seconds; legacy fractional windows migrate with a note.
    duration = math.ceil(duration)
    configuration = load_config(duration=duration, rates=[rate])
    result = plan(configuration)
    offers, _ = planned_arrivals(configuration.load_test, test_points(configuration.load_test)[0])
    assert result["planned_per_target"] == len(offers)
    assert result["count_kind"] == "exact"
    row = next(row for row in result["rows"] if row["id"] == "load_combinations")
    assert row["known_scheduled_requests"] == len(offers)
    assert "Scheduled arrivals are exact" in row["summary"]


def test_multiple_overlapping_targets_explain_the_shared_concurrency_limit():
    configuration = config(overlap_models=True, targets=[
        {"name": "first", "url": "http://localhost:1", "model": "a"},
        {"name": "second", "url": "http://localhost:2", "model": "b"},
    ])
    assert any("share Simultaneous limit" in warning and "overlap" in warning
               for warning in plan(configuration)["warnings"])


def test_poisson_rates_have_exact_counts_without_building_schedules(monkeypatch):
    monkeypatch.setattr("giraffe.traffic.arrival_schedule", lambda *a, **k: pytest.fail("Built schedule"))
    result = plan(load_config(duration=3, rates=[1, 3], poisson=True))
    assert result["count_kind"] == "estimate"
    assert result["planned_per_target"] == 12
    assert result["known_per_target"] == 0
    assert result["required_request_limit"] is None
    assert result["request_count_label"] == "≈ 12"
    assert "Poisson requests are approximate" in result["rows"][0]["summary"]


def test_capacity_without_a_timing_goal_explains_the_inconclusive_verdict():
    configuration = load_config()
    configuration.load_test.pass_criteria.max_completion_ms = None
    configuration.load_test.pass_criteria.max_first_answer_ms = None
    assert any("Measure only" in warning and "highest tested" in warning for warning in plan(configuration)["warnings"])
    for field in ("max_completion_ms", "max_first_answer_ms"):
        with_goal = configuration.model_copy(deep=True)
        setattr(with_goal.load_test.pass_criteria, field, 1000)
        assert not any("Measure only" in warning for warning in plan(with_goal)["warnings"])


@pytest.mark.parametrize("updates,known", [
    ({"checks": ["access", "recovery"]}, 14),
    ({"checks": ["access"], "restart_target": "local"}, 1),
    ({"checks": ["access", "correctness"], "custom_fixtures": "/unread/questions.json"}, 1),
])
def test_adaptive_and_unread_workloads_do_not_claim_a_full_count(updates, known):
    result = plan(config(**updates))
    assert result["count_kind"] == "bounded"
    assert result["planned_per_target"] is result["planned_total"] is None
    assert result["required_request_limit"] is None
    assert result["known_per_target"] == known
    assert any(row["requests_per_target"] is None for row in result["rows"])


def test_context_plan_counts_one_locally_prepared_batch():
    result = plan(config(checks=["access", "context"]))
    assert result["count_kind"] == "exact"  # Local sizing adds no inference requests.
    assert result["planned_total"] == result["known_per_target"] == 7
    assert not result["adaptive_estimates"].get("context")
    assert next(row for row in result["rows"] if row["id"] == "context")["requests_per_target"] == 0


def test_every_check_has_an_explained_row_and_planning_has_no_execution_side_effects(monkeypatch):
    import httpx
    import os
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("Planner attempted external work")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr("giraffe.fixtures.load_custom_fixtures", forbidden)
    configuration = config(checks=list(CHECK_NAMES))
    result = plan(configuration)
    rows = {row["id"]: row for row in result["rows"]}
    assert set(CHECK_NAMES) - {"load"} <= rows.keys()
    assert {"load_combinations", "load_calibration"} <= rows.keys()
    assert all(row["source"] and row["summary"] for row in rows.values())
    assert result["count_kind"] == "bounded"




def test_empty_dataset_and_blank_model_remain_editable_with_unknown_active_count():
    draft = ConfigDraft.model_validate(config().model_dump() | {
        "targets": [{"name": "local", "url": "http://localhost", "model": ""}],
        "request_source": "request_pack", "request_pack": None,
    })
    result = plan(draft)
    assert result["known_per_target"] == 1
    assert result["planned_per_target"] is None
    assert result["warnings"]
    empty = plan(ConfigDraft.model_validate(draft.model_dump() | {"checks": []}))
    assert empty["count_kind"] == "exact"
    assert empty["planned_total"] == 0


@pytest.mark.parametrize("arrival", ["steady", "poisson"])
async def test_dataset_draft_plan_builds_each_schedule_once_without_full_preview(tmp_path, monkeypatch, arrival):
    import httpx
    from giraffe import request_pack, traffic, web
    from giraffe.planning import request_plan

    configuration = ConfigDraft.model_validate(config().model_dump() | {
        "checks": ["access", "correctness", "capacity"], "request_source": "request_pack",
        "request_pack": {"version": 1, "cases": [
            {"fixture_id": "judged", "messages": [{"role": "user", "content": "A?"}],
             "scorer": "judge", "expected": "A", "weight": 3},
            {"fixture_id": "exact", "messages": [{"role": "user", "content": "B?"}],
             "scorer": "exact", "expected": "B", "weight": 1},
        ]},
        "traffic": {"rates": [1.5, 3], "duration_seconds": 2, "arrival": arrival},
        "targets": [{"name": name, "url": "http://localhost/v1", "model": ""} for name in ("a", "b")],
        "judging": {"judges": [{"name": "judge"}]}, "max_requests": 3,
    })
    expected = request_plan(configuration, pack_details=request_pack.pack_preview(configuration))
    guard = {"supported": True, "need": 64, "errors": [], "warnings": []}
    monkeypatch.setattr(web, "open_file_guard", lambda *args, **kwargs: guard)
    expected.update(open_files=guard, resource_warnings=[], resource_issues=[])

    def forbidden(*args, **kwargs):
        pytest.fail("Draft plan constructed full request-pack preview details or identities")

    for name in ("pack_preview", "pack_specs", "pack_identity", "judging_identity", "_digest"):
        monkeypatch.setattr(request_pack, name, forbidden)
    monkeypatch.setattr(web, "pack_preview", forbidden)
    schedule_calls = []
    schedule = traffic.arrival_schedule

    def counted_schedule(settings, rate, stage_index=0):
        schedule_calls.append(stage_index)
        return schedule(settings, rate, stage_index)

    monkeypatch.setattr(traffic, "arrival_schedule", counted_schedule)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.create_app(tmp_path)),
                                 base_url="http://127.0.0.1:8787") as client:
        response = await client.post("/api/config/draft", json=configuration.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert response.json()["request_plan"] == expected
    assert schedule_calls == []  # #45 plans expectations without materializing arrival schedules.


@pytest.mark.parametrize('dataset', [False, True])
async def test_plan_fixed_rows_match_real_inprocess_run_per_scenario(monkeypatch, dataset, local_tokenizer):
    from collections import Counter
    from giraffe import runner
    from giraffe.models import DEFAULT_CHECKS
    from tests.test_runner import FakeClient, config as run_config
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    updates = {}
    if dataset:
        updates = {'request_source': 'request_pack', 'request_pack': {'version': 1, 'cases': [
            {'fixture_id': f'case{index}', 'messages': [{'role': 'user', 'content': 'Return A'}],
             'scorer': 'exact', 'expected': 'A'} for index in range(3)]}}
    configured = (run_config(checks=['correctness'], **updates) if dataset else
                  RunConfig(targets=run_config().targets, checks=list(DEFAULT_CHECKS), max_requests=1000))
    report = await runner.run_suite(configured)
    counts = Counter(record.scenario for record in report.requests)
    preview = plan(configured)
    scenarios = {'context_first_batch': ['context'],
                 'correctness': [f'correctness_c{configured.effective_check("correctness")["concurrency"]}'],
                 'json': [f'json_c{configured.effective_check("json")["concurrency"]}'],
                 'capacity': [name for name in counts if name.startswith('traffic_stage_')],
                 'cancellation': ['limits', 'cancellation_baseline', 'client_cancel', 'client_deadline', 'cancellation_probe'],
                 'limits.stop_control': ['stop_control'],
                 'buckets': [name for name in counts if name.startswith('bucket_c')],
                 'mixed': [name for name in counts if name.startswith('mixed_') and name != 'mixed_control']}
    for row in preview['rows']:
        if row['kind'] == 'exact' and row['requests_per_target']:
            assert sum(counts[name] for name in scenarios.get(row['id'], [row['id']])) == row['requests_per_target'], row
    assert 'client_error' not in report.observations['targets']['service']
    assert preview['known_per_target'] <= len(report.requests)
    if dataset:
        assert preview['planned_per_target'] == len(report.requests) == 6


def test_adaptive_traffic_budget_warning_includes_reserved_baselines_and_load():
    configured = config(checks=['access', 'concurrency', 'recovery'], max_requests=28,
                        concurrency=4, samples=4)
    preview = plan(configured)
    assert preview['known_per_target'] == 9
    assert preview['conservative_requests_per_target'] == 17
    assert preview['adaptive_estimates']['sustained'] >= 8
    assert preview["request_warnings"][-1]["kind"] == "adaptive_risk"
    assert "unfinished" in preview["request_warnings"][-1]["msg"]


@pytest.mark.parametrize("quota", [300, 400, 1000])
def test_local_recovery_warmup_is_reserved_once_in_adaptive_estimate(quota):
    from pathlib import Path
    import json
    snapshots = json.loads((Path(__file__).resolve().parent / "fixtures/run-limits-phase3-defaults.json").read_text())
    values = dict(snapshots["cases"]["examples/local.yaml"]["source"])
    values.pop("concurrent_load")
    values.update(config_version=2, default_concurrency=2,
                  checks=["access", "serving", "first_output", "generation", "load", "context",
                          "correctness", "cancellation", "recovery", "json"],
                  load_test={"patterns": {"concurrent": {"levels": [1, 2]}}})
    # Keep the recovery reservation scenario independent of the quickstart defaults.
    configuration = RunConfig.model_validate({**values, "max_requests": quota})
    result = plan(configuration)
    warmup = next(row for row in result["rows"] if row["id"] == "recovery_prime")
    assert warmup["kind"] == "exact" and warmup["requests_per_target"] == 1
    # Recovery priming is exact once; three shapes × (1+2)/s × 15 s remain scheduled.
    assert result["known_per_target"] == 60 + 135
    assert result["minimum_reserved_per_target"] > result["known_per_target"]
    assert result["adaptive_estimates"] == {"sustained": 5}
    assert result["conservative_requests_per_target"] == sum(row["requests_per_target"] if row["requests_per_target"] is not None else row.get("expected_scheduled_requests", 0) for row in result["rows"]) + 5
    assert result["request_warnings"][-1]["kind"] == "adaptive_risk"


def test_capacity_accounting_note_does_not_warn_when_budgets_and_limits_fit():
    result = plan(load_config(duration=2, finish=0, rates=[1], max_requests=20))
    assert result["request_warnings"] == result["time_warnings"] == []
    row = next(row for row in result["rows"] if row["id"] == "load_combinations")
    assert row["requests_per_target"] == row["known_scheduled_requests"] == 2
    assert "Scheduled arrivals are exact" in row["summary"]

@pytest.mark.parametrize("poisson", [False, True])
def test_unknown_refills_preserve_additive_arrival_counts_in_global_headline(poisson):
    configuration = load_config(duration=10, rates=[2], concurrent=True, poisson=poisson,
        targets=[{"name": name, "url": "http://unused.test", "model": "test"} for name in ("a", "b")])
    result = plan(configuration)
    assert result["known_per_target"] == (0 if poisson else 20)
    assert result["known_total"] == (0 if poisson else 40)
    assert result["planned_total"] is None
    assert result["conservative_requests_per_target"] == 20
    assert result["request_count_label"] == ("Unknown + ≈ 40" if poisson else "Unknown + 40")
    assert result["load_test"]["expected_requests_label"] == ("Unknown + ≈ 20" if poisson else "Unknown + 20")


def test_load_budget_action_excludes_local_preparation_and_groups_visible_names():
    from tests.test_load_test_phase_a import values

    settings = values()
    settings["max_requests"] = 1
    settings["load_test"]["patterns"] = {"concurrent": {"enabled": False},
        "constant": {"enabled": True}, "rates": [20]}
    result = plan(RunConfig.model_validate(settings))
    assert result["known_total"] == 20
    assert result["planned_total"] == 20  # Local preparation sends no target requests.
    coverage = next(row for row in result["request_warnings"] if row["kind"] == "incomplete_coverage")
    assert coverage["actions"] == [
        {"label": "Use 20 total requests", "field": "max_requests", "value": 20},
        {"label": "Edit Load test", "field": "load_test.schedule.duration_seconds"}]
    assert coverage["cut_checks"] == ["load_combinations"]
    assert coverage["msg"].count("Load test") == 1


def test_load_dataset_controls_count_matches_real_runner_without_extra_loaded_pass(monkeypatch):
    from collections import Counter
    from giraffe import runner
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient

    async def scenario():
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        configuration = load_config(duration=1, rates=[3])
        configuration.checks = ["access", "serving", "first_output", "correctness", "load"]
        preview = plan(configuration)
        report = await runner.run_suite(configuration)
        counts = Counter(record.scenario for record in report.requests)
        assert counts["initial"] == 1
        assert counts["warm"] == 6
        assert counts["pack_control"] == 1
        assert counts["pack_loaded"] == 0
        assert sum(value for key, value in counts.items() if key.startswith("load_primary_")) == 3
        assert preview["planned_total"] == preview["known_total"] == len(report.requests) == 11

    run_virtual(monkeypatch, scenario())


def test_fractional_poisson_budget_remedy_is_a_valid_whole_request_limit():
    configuration = load_config(duration=1, rates=[1.3], poisson=True, max_requests=1)
    result = plan(configuration)
    coverage = next(row for row in result["request_warnings"] if row["kind"] == "incomplete_coverage")
    action = coverage["actions"][0]
    assert action == {"label": "Use 2 total requests", "field": "max_requests", "value": 2}
    updated = RunConfig.model_validate(configuration.model_dump() | {action["field"]: action["value"]})
    assert updated.max_requests == 2
    assert any(row["kind"] == "adaptive_risk" for row in result["request_warnings"])


def test_saved_question_shape_warning_is_exposed_in_shared_plan_before_running():
    configuration = config(checks=['load'], request_source='request_pack', request_pack={
        'version': 1, 'cases': [{'fixture_id': 'short',
            'messages': [{'role': 'user', 'content': 'Return A'}],
            'scorer': 'exact', 'expected': 'A'}]},
        load_test={'workloads': {'combinations': {'selected': ['long_short']}},
                   'patterns': {'concurrent': {'levels': [1]}}})
    result = plan(configuration)
    warning = next(row for row in result['load_warnings']
                   if 'No saved question is long enough for Long input' in row['msg'])
    assert 'Long →' in warning['msg'] and 'results will be inconclusive' in warning['msg']
    assert warning['msg'] in result['warnings']
    assert warning['msg'] in result['load_test']['saved_shape_warnings']
    assert next(row for row in result['rows'] if row['id'] == 'load_combinations')['source'] == 'your dataset'
