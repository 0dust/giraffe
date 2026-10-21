"""Setup counts describe the runner's workload without executing it."""

import pytest

from giraffe.models import CHECK_NAMES, ConfigDraft, RunConfig


def config(**updates):
    return RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://localhost:11434/v1", "model": "test"}],
        "checks": ["access", "correctness"], **updates,
    })


def plan(configuration):
    from giraffe.planning import request_plan

    return request_plan(configuration)


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
    values = dict(checks=["capacity"], request_timeout_seconds=10,
                  traffic={"rates": [1, 2], "duration_seconds": 5,
                           "drain_timeout_seconds": 3})
    result = plan(config(**values, max_duration_seconds=9))
    assert result["minimum_seconds"] == 10
    assert result["worst_case_seconds"] == 2 * (5 + 3)  # Windows plus drain; setup is excluded.
    assert result["errors"] == []
    coverage, risk = result["time_warnings"]
    assert coverage["fields"] == ["max_duration_seconds", "traffic.duration_seconds"]
    assert coverage["cut_checks"] == ["local · Capacity under load 2"]
    assert coverage["actions"][0] == {"label": "Use 16 s", "field": "max_duration_seconds", "value": 16}
    assert risk["kind"] == "estimate_risk"
    result = plan(config(**values, max_duration_seconds=12))
    assert result["errors"] == []
    # Both fixed windows fit; only possible timeout drains exceed 12 seconds.
    risk, = result["time_warnings"]
    assert risk == {"kind": "estimate_risk", "fields": ["max_duration_seconds"],
                    "msg": "The estimated traffic duration may exceed the time limit, leaving checks unfinished.",
                    "components": [],
                    "actions": [{"label": "Edit settings", "fields": ["max_duration_seconds"]}],
                    "values": {"max_duration_seconds": 12}}
    assert result["minimum_seconds"] == 10
    assert result["worst_case_seconds"] == 16


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
    assert sum(row["timeout_bound_seconds"] for row in result["unknown_time_components"]) == (3 * first + 1) * 10
    assert result["estimated_seconds"] is None
    sessions = plan(config(checks=["sessions"], request_timeout_seconds=10,
                           sessions={"modes": ["live"], "sessions": 2, "delay_seconds": 3}))
    assert sessions["estimated_seconds"] == 3  # Only known conversation pauses enter the headline.
    assert sessions["unknown_time_components"][0]["timeout_bound_seconds"] == 20


def test_empty_poisson_stage_still_warns_about_its_cut_sending_window():
    result = plan(config(checks=["capacity"], max_duration_seconds=1,
                         traffic={"rates": [.01], "duration_seconds": 2, "arrival": "poisson"}))
    assert result["planned_total"] == 0
    assert result["minimum_seconds"] == 2
    assert result["errors"] == []
    assert result["time_warnings"][0]["kind"] == "incomplete_coverage"


def test_arrivals_estimate_follows_schedule_with_one_timeout_drain():
    result = plan(config(checks=["arrivals"], concurrency=16,
                         arrivals={"requests": 600, "requests_per_second": 10}))
    assert result["estimated_low_seconds"] == 59.9
    assert result["estimated_high_seconds"] == 59.9 + 30  # Last offer plus one timeout drain.


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
    result = plan(config(checks=checks, max_requests=2))
    assert result["count_kind"] == "exact"
    assert result["planned_per_target"] == result["known_per_target"] == expected
    assert result["planned_total"] == result["required_request_limit"] == expected
    assert result["request_limit"] == 2
    assert any("share of the request limit (Stop after this many requests:" in warning for warning in result["warnings"])


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
    result = plan(config(checks=[check], **updates))
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
    from giraffe.traffic import arrival_schedule

    configuration = config(checks=["capacity"], traffic={"rates": [rate], "duration_seconds": duration})
    result = plan(configuration)
    assert result["planned_per_target"] == len(arrival_schedule(configuration.traffic, rate)[0])
    assert result["count_kind"] == "exact"
    capacity = next(row for row in result["rows"] if row["id"] == "capacity")
    assert "Scheduled requests use the run limit" in capacity["summary"]
    assert "slots are busy" in capacity["summary"]


def test_multiple_overlapping_targets_explain_the_shared_concurrency_limit():
    configuration = config(overlap_models=True, targets=[
        {"name": "first", "url": "http://localhost:1", "model": "a"},
        {"name": "second", "url": "http://localhost:2", "model": "b"},
    ])
    assert any("share Limit simultaneous requests" in warning and "overlap" in warning
               for warning in plan(configuration)["warnings"])


def test_poisson_rates_have_exact_counts_without_building_schedules(monkeypatch):
    monkeypatch.setattr("giraffe.traffic.arrival_schedule", lambda *a, **k: pytest.fail("Built schedule"))
    result = plan(config(checks=["access", "capacity"], traffic={
        "rates": [1, 3], "duration_seconds": 2.5, "arrival": "poisson"}))
    assert result["count_kind"] == "exact"
    assert result["planned_per_target"] == 11
    assert result["known_per_target"] == 11
    assert result["required_request_limit"] == 11
    assert "Starts 10 requests" in next(row for row in result["rows"] if row["id"] == "capacity")["summary"]


def test_capacity_without_a_timing_goal_explains_the_inconclusive_verdict():
    configuration = config(checks=["capacity"])
    assert any("Without a time target" in warning and "measurements only" in warning
               for warning in plan(configuration)["warnings"])
    with_goal = config(checks=["capacity"], test_options={
        "capacity": {"limits": {"latency_ms": 1000}}})
    assert not any("Without a time target" in warning for warning in plan(with_goal)["warnings"])
    with_first_text_goal = config(checks=["capacity"], test_options={
        "capacity": {"limits": {"first_output_ms": 1000}}})
    assert not any("Without a time target" in warning for warning in plan(with_first_text_goal)["warnings"])


@pytest.mark.parametrize("updates,known", [
    ({"checks": ["access", "recovery"]}, 14),
    ({"checks": ["access", "context"]}, 7),
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
    assert set(CHECK_NAMES) - {"concurrency"} <= rows.keys()
    assert {"load_combinations", "load_mixed", "load_calibration"} <= rows.keys()
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
                                 base_url="http://test") as client:
        response = await client.post("/api/config/draft", json=configuration.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert response.json()["request_plan"] == expected
    assert schedule_calls == [0, 1]


@pytest.mark.parametrize('dataset', [False, True])
async def test_plan_fixed_rows_match_real_inprocess_run_per_scenario(monkeypatch, dataset):
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
    assert preview['conservative_requests_per_target'] == 26
    assert preview['adaptive_estimates']['sustained'] >= 8
    assert preview["request_warnings"][-1]["kind"] == "adaptive_risk"
    assert "unfinished" in preview["request_warnings"][-1]["msg"]


@pytest.mark.parametrize("quota", [300, 400, 1000])
def test_local_recovery_warmup_is_reserved_once_in_adaptive_estimate(quota):
    from pathlib import Path
    import yaml

    values = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples/local.yaml").read_text())
    configuration = RunConfig.model_validate({**values, "max_requests": quota})
    result = plan(configuration)
    warmup = next(row for row in result["rows"] if row["id"] == "recovery_prime")
    assert warmup["kind"] == "bounded" and warmup["requests_per_target"] == 1
    assert result["known_per_target"] == 94
    assert result["minimum_reserved_per_target"] > result["known_per_target"]
    # Unknown-speed load no longer invents a request count at 1 request/second.
    assert result["conservative_requests_per_target"] == 122
    assert result["adaptive_estimates"] == {"context": 13, "sustained": 5}
    assert result["request_warnings"][-1]["kind"] == "adaptive_risk"
    assert not any("Some tests may be incomplete" in warning for warning in result["warnings"])


def test_capacity_accounting_note_does_not_warn_when_budgets_and_limits_fit():
    result = plan(config(checks=["capacity"], max_requests=20,
                         traffic={"rates": [1], "duration_seconds": 2},
                         limits={"latency_ms": 1000}))
    assert not result["warnings"]
    capacity = next(row for row in result["rows"] if row["id"] == "capacity")
    assert capacity["requests_per_target"] == 2
    assert "Scheduled requests use the run limit" in capacity["summary"]
