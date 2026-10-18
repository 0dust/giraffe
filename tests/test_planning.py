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


@pytest.mark.parametrize("checks,expected", [
    (["access", "correctness"], 7),
    (["access", "buckets"], 100),
    (["access", "correctness", "capacity"], 42),
])
def test_basic_presets_have_complete_counts_before_caps(checks, expected):
    result = plan(config(checks=checks, max_requests=2))
    assert result["count_kind"] == "exact"
    assert result["planned_per_target"] == result["known_per_target"] == expected
    assert result["planned_total"] == result["required_request_limit"] == expected
    assert result["request_limit"] == 2
    assert any("share of the run limit" in warning for warning in result["warnings"])


@pytest.mark.parametrize("check,updates,expected", [
    ("serving", {}, 6),
    ("first_output", {}, 7),
    ("generation", {"test_options": {"generation": {"samples": 3}}}, 3),
    ("correctness", {"test_options": {"correctness": {"samples": 1}}}, 3),
    ("json", {}, 6),
    ("cancellation", {}, 14),
    ("recovery", {"sustained_seconds": 0}, 6),
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


@pytest.mark.parametrize("stream,concurrency,kind,known", [
    (False, 2, "exact", 6),
    (True, 1, "exact", 6),
    (True, 2, "bounded", 12),
])
def test_fairness_plan_only_includes_streaming_overlap_when_eligible(stream, concurrency, kind, known):
    result = plan(config(checks=["fairness"], stream=stream, concurrency=concurrency))
    assert result["count_kind"] == kind
    assert result["known_per_target"] == known
    assert result["required_request_limit"] == (known if kind == "exact" else None)
    assert any(row["requests_per_target"] is None for row in result["rows"]) == (kind == "bounded")


@pytest.mark.parametrize("concurrency,kind,known", [(1, "exact", 5), (2, "exact", 45)])
def test_mixed_plan_only_includes_interference_work_when_concurrency_allows_it(concurrency, kind, known):
    result = plan(config(checks=["mixed"], concurrency=concurrency))
    assert result["count_kind"] == kind
    assert result["known_per_target"] == known
    assert result["required_request_limit"] == (known if kind == "exact" else None)
    assert any(row["requests_per_target"] is None for row in result["rows"]) == (kind == "bounded")


def test_bucket_plan_covers_nine_real_prompts_and_full_batches():
    configuration = config(checks=["access", "buckets"], concurrency=8,
                           buckets={"levels": [1, 4, 8], "samples_per_bucket": 4})
    result = plan(configuration)
    assert result["planned_per_target"] == 145
    assert result["bucket_levels"] == [
        {"concurrency": 1, "requests_per_case": 4, "requests_per_target": 36},
        {"concurrency": 4, "requests_per_case": 4, "requests_per_target": 36},
        {"concurrency": 8, "requests_per_case": 8, "requests_per_target": 72},
    ]
    assert result["bucket_cases"] == [
        {"input_chars": chars, "actual_input_chars": min(chars, 11808) + extra, "output_tokens": cap}
        for chars in (64, 1024, 12288)
        for cap, extra in ((16, 0), (48, 32), (96, 32))
    ]
    assert configuration.max_requests == 160


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


def test_saved_dataset_does_not_replace_generated_buckets_or_activate_judges():
    configuration = ConfigDraft.model_validate(config().model_dump() | {
        "checks": ["access", "buckets"], "request_source": "request_pack",
        "request_pack": {"version": 1, "cases": [{"fixture_id": "own-question",
            "messages": [{"role": "user", "content": "Private question"}],
            "scorer": "judge", "expected": "Answer"}]},
    })
    result = plan(configuration)
    assert result["planned_per_target"] == 100
    assert next(row for row in result["rows"] if row["id"] == "buckets")["source"] == "generated examples"
    assert result["evaluator"] == {"active": False, "call_upper_bound": 0, "judge_count": 0,
                                   "ready": True, "readiness_errors": []}
    assert any("saved questions" in warning and "generated questions" in warning for warning in result["warnings"])
    assert not any("dataset is inactive" in warning for warning in result["warnings"])


@pytest.mark.parametrize("rate,duration", [(1, 2.5), (3, .7), (.3, 10), (7, 1)])
def test_steady_rate_counts_match_schedule_semantics(rate, duration):
    from giraffe.traffic import arrival_schedule

    configuration = config(checks=["capacity"], traffic={"rates": [rate], "duration_seconds": duration})
    result = plan(configuration)
    assert result["planned_per_target"] == len(arrival_schedule(configuration.traffic, rate)[0])
    assert result["count_kind"] == "exact"
    assert any("Scheduled requests use the run limit" in warning and "slots are busy" in warning
               for warning in result["warnings"])


def test_multiple_overlapping_targets_explain_the_shared_concurrency_limit():
    configuration = config(overlap_models=True, targets=[
        {"name": "first", "url": "http://localhost:1", "model": "a"},
        {"name": "second", "url": "http://localhost:2", "model": "b"},
    ])
    assert any("share the maximum requests at a time" in warning and "overlap" in warning
               for warning in plan(configuration)["warnings"])


def test_poisson_rates_are_labeled_expected_estimates_without_building_schedules(monkeypatch):
    monkeypatch.setattr("giraffe.traffic.arrival_schedule", lambda *a, **k: pytest.fail("Built schedule"))
    result = plan(config(checks=["access", "capacity"], traffic={
        "rates": [1, 3], "duration_seconds": 2.5, "arrival": "poisson"}))
    assert result["count_kind"] == "estimate"
    assert result["planned_per_target"] == 11
    assert result["known_per_target"] == 1
    assert result["required_request_limit"] is None
    assert "expected" in next(row for row in result["rows"] if row["id"] == "capacity")["summary"]


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
    ({"checks": ["access", "recovery"]}, 7),
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
    assert set(CHECK_NAMES) <= rows.keys()
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
