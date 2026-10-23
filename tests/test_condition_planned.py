"""The live condition skin starts from configured work, not observed requests."""
import asyncio

import httpx
import pytest


from giraffe.conditions import ConditionRequestIndex, planned_conditions
from giraffe.load_test import test_points as load_points
from giraffe.models import RunConfig, RunReport
from giraffe.web import create_app

pytestmark = pytest.mark.usefixtures("local_tokenizer")


def load_config(levels=(4, 8, 16)):
    config = RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://localhost:8000/v1", "model": "test"}],
        "checks": ["load"],
        "load_test": {"workloads": {"combinations": {"selected": ["short_short", "long_long"]}},
                      "patterns": {"concurrent": {"levels": [min(level, 512) for level in levels]}, "constant": {"enabled": False}}},
    })
    config.load_test.patterns.concurrent.levels = list(levels)
    return config


def test_load_plan_matches_executor_ids_and_waits_without_request_rows():
    config = load_config()
    rows = planned_conditions(config)
    expected = [point["id"] for point in load_points(config.load_test)]
    assert [row["condition_id"] for row in rows] == expected
    assert all(row["status"] == "waiting" and row["started"] == row["running"] == row["completed"] == 0 for row in rows)
    assert all(row["planned"] is None and row["planned_kind"] == "unknown" for row in rows)
    assert not any("calibration" in row["condition_id"] for row in rows)
    index = ConditionRequestIndex()
    for row in rows:
        index.summary(row)
    page = index.page(expected[-1])
    assert page["rows"] == [] and page["aggregate"]["status"] == "waiting"
    assert page["totals"]["not_started"] is None


@pytest.mark.parametrize("level", [32, 1000])
def test_concurrency_is_one_planned_condition(level):
    config = load_config((level,))
    config.load_test.workloads.combinations.selected = ["short_short"]
    rows = planned_conditions(config)
    assert len(rows) == 1
    assert rows[0]["concurrency_description"] == f"{level:,} at once"


def test_exact_consistency_plan_pages_waiting_placeholders_without_fabricated_requests():
    config = RunConfig.model_validate({**load_config().model_dump(mode="json"), "checks": ["consistency"]})
    rows = planned_conditions(config)
    assert rows
    index = ConditionRequestIndex()
    for row in rows:
        index.summary(row)
    page = index.page(rows[0]["condition_id"])
    assert page["aggregate"]["planned_kind"] == "exact"
    assert page["aggregate"]["not_started"] == page["total"] > 0
    assert all(row["request_id"] is None and row["status"] == "not_started" for row in page["rows"])


@pytest.mark.asyncio
async def test_run_exposes_all_waiting_conditions_before_first_event_and_updates_in_place(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    emitted = asyncio.Event()
    config = load_config()
    planned = [row["condition_id"] for row in planned_conditions(config)]

    async def held_runner(config, *, progress, stop_event):
        entered.set()
        await release.wait()
        first = planned_conditions(config)[0]
        progress({"event": "condition_started", "target": "local", "scenario": planned[0],
                  "condition_summary": {**first, "status": "running", "aggregate_status": "running"}})
        extra = {**first, "condition": {**first["condition"], "id": "unplanned-extra", "label": "Extra"},
                 "status": "running", "aggregate_status": "running"}
        progress({"event": "condition_started", "target": "local", "scenario": "unplanned-extra", "condition_summary": extra})
        emitted.set()
        await stop_event.wait()
        return RunReport(run_id="held", started_at="2026-10-22T00:00:00+00:00", finished_at="2026-10-22T00:00:01+00:00", overall="inconclusive", manifest={})

    monkeypatch.setattr("giraffe.web.run_suite", held_runner)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            started = await client.post("/api/runs", json={"config": config.model_dump(mode="json")})
            assert started.status_code == 202, started.text
            identifier = started.json()["id"]
            assert [row["condition_id"] for row in started.json()["live_conditions"]] == planned
            await entered.wait()
            initial = (await client.get(f"/api/runs/{identifier}")).json()["live_conditions"]
            assert [row["condition_id"] for row in initial] == planned
            assert all(row["aggregate_status"] == "waiting" for row in initial)
            paged = await client.get(f"/api/runs/{identifier}/conditions/{planned[-1]}/requests")
            assert paged.status_code == 200 and paged.json()["aggregate"]["status"] == "waiting"
            release.set()
            await emitted.wait()
            updated = (await client.get(f"/api/runs/{identifier}")).json()["live_conditions"]
            assert [row["condition_id"] for row in updated] == [*planned, "unplanned-extra"]
            assert updated[0]["aggregate_status"] == "running"
            assert all(row["aggregate_status"] == "waiting" for row in updated[1:-1])
            await client.post(f"/api/runs/{identifier}/cancel", json={})
            for _ in range(100):
                if (await client.get(f"/api/runs/{identifier}")).json()["state"] != "stopping":
                    break
                await asyncio.sleep(0)


def test_load_arrival_plans_keep_exact_and_unknown_semantics_and_interference_phases():
    config = load_config((4,))
    config.load_test.patterns.constant.enabled = True
    config.load_test.patterns.poisson.enabled = True
    config.load_test.workloads.interference.enabled = True
    rows = planned_conditions(config)
    assert [row["condition_id"] for row in rows] == [
        point["id"] + (f".{phase}" if phase else "")
        for point in load_points(config.load_test)
        for phase in (("baseline", "probe") if point["type"] == "interference" else (None,))]
    assert all(row["planned_kind"] == "exact" and row["planned"] > 0 for row in rows if "_constant_" in row["condition_id"])
    assert all(row["planned"] is None for row in rows if "_poisson_" in row["condition_id"])
    assert all(row["planned_reason"] == "Unknown — duration-based" for row in rows if "_concurrent_" in row["condition_id"] or "_interference_" in row["condition_id"])


def test_runtime_segment_does_not_shrink_whole_condition_plan_or_paging():
    from collections import OrderedDict
    from giraffe.web import _track_live_progress
    config = RunConfig.model_validate({**load_config().model_dump(mode="json"), "checks": ["consistency"]})
    plan = planned_conditions(config)[0]
    key = ("local", plan["condition_id"])
    job = {"conditions": {key: plan}, "condition_plans": {key: plan}, "progress": {}, "events": [],
           "requests": OrderedDict(), "phases": []}
    segment = {**plan, "status": "running", "planned": 1, "started": 1, "running": 1}
    _track_live_progress(job, {"event": "condition_started", "target": "local", "condition_summary": segment}, None)
    assert job["conditions"][key]["planned"] == plan["planned"]
    page = job["condition_requests"].page(plan["condition_id"])
    assert page["aggregate"]["planned"] == plan["planned"]
    assert page["aggregate"]["not_started"] == plan["planned"] - 1


def test_all_targets_get_distinct_target_local_plans_without_setup_cells():
    config = load_config()
    config.targets.append(config.targets[0].model_copy(update={"name": "second"}))
    rows = planned_conditions(config)
    local = [row for row in rows if row["target"] == "local"]
    second = [row for row in rows if row["target"] == "second"]
    assert [row["condition_id"] for row in local] == [row["condition_id"] for row in second]
    assert not any(word in row["condition_id"] for row in rows for word in ("calibration", "warm", "setup"))


@pytest.mark.asyncio
async def test_stopped_saved_run_preserves_unstarted_conditions_and_request_pages(tmp_path, monkeypatch):
    from tests.test_web import browser, finished, report
    config = load_config()
    planned = planned_conditions(config)
    first, last = planned[0], planned[-1]

    async def stopped(config, *, progress, stop_event):
        progress({"event": "condition_started", "target": "local", "condition_summary": {
            **first, "status": "running", "started": 1, "running": 1}})
        await stop_event.wait()
        result = report(config, cancelled=True)
        result.conditions = [{**first, "status": "failed", "aggregate_status": "failed",
                              "lifecycle_status": "finished", "started": 1, "completed": 1, "failed": 1}]
        return result

    monkeypatch.setattr("giraffe.web.run_suite", stopped)
    async with browser(tmp_path) as client:
        started = (await client.post("/api/runs", json={"config": config.model_dump(mode="json")})).json()
        identifier = started["id"]
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        detail = await finished(client, identifier)
        assert [row["condition_id"] for row in detail["report"]["conditions"]][:len(planned)] == [row["condition_id"] for row in planned]
        unstarted = next(row for row in detail["report"]["conditions"] if row["condition_id"] == last["condition_id"])
        assert unstarted["status"] == "skipped" and unstarted["reasons"] == ["cancelled by user"]
        observed = next(row for row in detail["report"]["conditions"] if row["condition_id"] == first["condition_id"])
        assert observed["status"] == "failed" and observed["failed"] == 1
        exported = (await client.get(f"/api/runs/{identifier}/report.json")).json()
        assert exported["conditions"] == detail["report"]["conditions"]
        page = await client.get(f"/api/runs/{identifier}/conditions/{last['condition_id']}/requests")
        assert page.status_code == 200
        assert page.json()["aggregate"]["status"] == "skipped" and page.json()["rows"] == []
    # Reopening uses persisted metadata, not the previous process's live map.
    async with browser(tmp_path) as client:
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        assert [row["condition_id"] for row in detail["report"]["conditions"]][:len(planned)] == [row["condition_id"] for row in planned]
        page = await client.get(f"/api/runs/{identifier}/conditions/{last['condition_id']}/requests")
        assert page.status_code == 200 and page.json()["aggregate"]["status"] == "skipped"
