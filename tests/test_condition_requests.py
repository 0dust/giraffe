"""Condition-level progress and bounded request inspection without sockets."""
import asyncio
from collections import OrderedDict

import httpx
import pytest

from giraffe.conditions import ConditionRequestIndex, aggregate_conditions, condition_aggregate, condition_for
from giraffe.models import RequestRecord, RunReport
from giraffe.reporting import write_report
from giraffe.web import _track_live_progress, create_app


def lifecycle(planned=32, kind="exact", **updates):
    return {"target": "local", "condition": condition_for("bucket_c32_short_short"),
            "planned": planned, "planned_kind": kind, "status": "finished", "started": 3,
            "running": 0, "completed": 3, "failed": 1, "reason": "sending window expired", **updates}


def record(number, **updates):
    return {"id": f"request-{number}", "target": "local", "fixture_id": "fixture",
            "scenario": "bucket_c32_short_short", "condition_id": "bucket_c32_short_short",
            "check_ids": ["buckets"], "started_at": "2026-10-22T00:00:00+00:00",
            "status": "completed", "valid": True, "score": True, **updates}


def saved_report(records, summary=None, checks=()):
    return RunReport(run_id="saved", started_at="2026-10-22T00:00:00+00:00",
                     finished_at="2026-10-22T00:01:00+00:00", overall="pass", manifest={},
                     requests=[RequestRecord(**row) for row in records], checks=list(checks),
                     observations={"targets": {"local": {"conditions": [summary] if summary else []}}})


@pytest.mark.parametrize("concurrency", [32, 1000])
def test_one_aggregate_per_condition_not_per_request(concurrency):
    report = saved_report([record(i, scenario=f"bucket_c{concurrency}_short_short",
                                  condition_id=f"bucket_c{concurrency}_short_short") for i in range(concurrency)])
    conditions = aggregate_conditions(report)
    assert len(conditions) == 1
    assert conditions[0]["started"] == concurrency
    assert conditions[0]["status"] == "passed"
    assert conditions[0]["planned"] is None  # Legacy records do not invent a plan.


def test_finished_unscored_is_inconclusive_and_explicit_verdict_wins():
    result = condition_aggregate(lifecycle(failed=0, reason=None))
    assert result["status"] == "inconclusive"
    assert condition_aggregate(lifecycle(failed=0), verdict="fail")["status"] == "failed"
    assert condition_aggregate(lifecycle(failed=0), verdict="inconclusive")["status"] == "inconclusive"
    assert result["check_id"] == "buckets"
    assert result["concurrency_description"] == "32 at once"


def test_saved_explicit_condition_verdict_survives_request_paging():
    from giraffe.models import CheckResult
    check = CheckResult(id="buckets", target="local", title="Buckets", status="fail", summary="Too slow",
                        metrics={"buckets": {"bucket_c32_short_short": {"status": "fail"}}})
    report = saved_report([record(0)], lifecycle(planned=1, started=1, completed=1, failed=0, reason=None), [check])
    assert aggregate_conditions(report)[0]["status"] == "failed"
    assert ConditionRequestIndex.from_report(report).page("bucket_c32_short_short")["aggregate"]["status"] == "failed"


def test_single_condition_uses_check_verdict_but_matrix_does_not_share_it():
    from giraffe.models import CheckResult
    check = CheckResult(id="generation", target="local", title="Pace", status="fail", summary="Too slow")
    report = saved_report([record(0, scenario="generation", condition_id="generation")], checks=[check])
    assert aggregate_conditions(report)[0]["status"] == "failed"
    check = CheckResult(id="buckets", target="local", title="Buckets", status="fail", summary="One slow point")
    report = saved_report([record(0), record(1, scenario="bucket_c1_short_short", condition_id="bucket_c1_short_short")], checks=[check])
    assert all(row["status"] == "passed" for row in aggregate_conditions(report))


def test_cancelled_and_unscored_remain_real_statuses_and_completed_subset():
    report = saved_report([record(0, status="cancelled"), record(1, score=None)])
    result = ConditionRequestIndex.from_report(report).page("bucket_c32_short_short")
    assert result["totals"]["cancelled"] == result["totals"]["unscored"] == 1
    assert result["aggregate"]["completed"] == 2
    assert result["aggregate"]["failed"] == 0
    assert result["aggregate"]["status"] == "inconclusive"


def test_upper_bound_window_expiry_is_not_a_stopped_verdict():
    result = condition_aggregate(lifecycle(kind="upper_bound", failed=0,
        request_statuses={"passed": 3}, reason="sending window ended"))
    assert result["status"] == "passed"
    result = condition_aggregate(lifecycle(planned=None, kind="duration", failed=0, reason="run stopped"))
    assert result["status"] == "stopped"


def test_exact_placeholders_counts_filters_and_page_clamping():
    report = saved_report([record(0), record(1, status="timeout"), record(2, score=False)], lifecycle(planned=32, failed=2))
    index = ConditionRequestIndex.from_report(report)
    last = index.page("bucket_c32_short_short", page=100)
    assert last["page"] == 4 and last["pages"] == 4
    assert [row["number"] for row in last["rows"]] == [31, 32]
    assert all(row["result_text"] == "Not sent: sending window expired" for row in last["rows"])
    assert last["aggregate"]["completed"] == 3
    assert last["aggregate"]["failed"] == 2
    assert last["aggregate"]["not_started"] == 29
    failed = index.page("bucket_c32_short_short", status="failed")
    assert [row["number"] for row in failed["rows"]] == [3]
    assert index.page("bucket_c32_short_short", status="timed_out")["rows"][0]["number"] == 2
    assert index.page("bucket_c32_short_short", status="running", page=90)["rows"] == []
    assert index.page("bucket_c32_short_short", status="running", page=90)["page"] == 1


@pytest.mark.parametrize("kind,planned", [("upper_bound", 32), ("duration", None), ("unknown", None)])
def test_nonexact_plans_never_fabricate_rows(kind, planned):
    index = ConditionRequestIndex.from_report(saved_report([record(0)], lifecycle(planned=planned, kind=kind, started=1, completed=1, failed=0)))
    result = index.page("bucket_c32_short_short", status="not_started")
    assert result["rows"] == [] and result["total"] == 0
    assert result["aggregate"]["planned_kind"] == (kind if kind == "upper_bound" else "unknown")


def test_old_reports_group_scenarios_and_dispatch_numbering():
    records = [record(2, dispatch_ms=20, condition_id=None), record(1, dispatch_ms=10, condition_id=None)]
    index = ConditionRequestIndex.from_report(saved_report(records))
    rows = index.page("bucket_c32_short_short")["rows"]
    assert [(row["number"], row["request_id"]) for row in rows] == [(1, "request-1"), (2, "request-2")]


def test_live_index_keeps_evicted_requests_and_updates_running_filters(monkeypatch):
    monkeypatch.setattr("giraffe.web.MAX_LIVE_REQUESTS", 2)
    job = {"progress": {}, "events": [], "conditions": {}, "requests": OrderedDict(), "phases": [],
           "live_request_summary": {"evicted": 0, "through_slot": None, "statuses": {}},
           "live_request_count": 0, "evicted_running": set()}
    summary = lifecycle(planned=1000, status="running", started=1000, running=1000, completed=0, failed=0, reason=None)
    for slot in range(1000):
        _track_live_progress(job, {"event": "request_started", "request_slot": slot,
                                  "target": "local", "scenario": "bucket_c32_short_short",
                                  "condition_summary": summary}, None)
    index = job["condition_requests"]
    assert len(job["requests"]) == 2
    assert len(index.page("bucket_c32_short_short", page=100)["rows"]) == 10
    assert index.page("bucket_c32_short_short", page=100)["rows"][-1]["number"] == 1000
    _track_live_progress(job, {"event": "request_completed", "request_slot": 0, "request": record(0)}, None)
    assert index.page("bucket_c32_short_short", status="passed")["rows"][0]["request_id"] == "request-0"
    assert index.page("bucket_c32_short_short", status="running")["total"] == 999
    assert job["conditions"]["local", "bucket_c32_short_short"]["aggregate_status"] == "running"


async def test_saved_paging_api_bounded_validation_and_target_identity(tmp_path):
    report = saved_report([record(i) for i in range(32)], lifecycle(planned=32, started=32, completed=32, failed=0, reason=None))
    write_report(report, tmp_path / "saved")
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.get("/api/runs/saved/conditions/bucket_c32_short_short/requests?page=4")
        assert result.status_code == 200, result.text
        assert len(result.json()["rows"]) == 2
        assert result.json()["total"] == 32
        for query in ("page=0", "page_size=1000", "page_size=11", "status=nonsense"):
            assert (await client.get("/api/runs/saved/conditions/bucket_c32_short_short/requests?" + query)).status_code == 422
        assert (await client.get("/api/runs/saved/conditions/missing/requests")).status_code == 404


async def test_live_api_uses_full_condition_index(tmp_path, monkeypatch):
    async def waiting(config, *, progress, stop_event):
        for slot in range(32):
            progress({"event": "request_started", "request_slot": slot, "target": "local",
                      "scenario": "bucket_c32_short_short", "condition_summary": lifecycle(planned=32, started=32, running=32, completed=0, status="running")})
        await stop_event.wait()
        return saved_report([])
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    monkeypatch.setattr("giraffe.web.MAX_LIVE_REQUESTS", 2)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post("/api/runs", json={"config": {"targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}], "checks": ["serving"]}})
            assert response.status_code == 202, response.text
            identifier = response.json()["id"]
            await asyncio.sleep(0)
            result = await client.get(f"/api/runs/{identifier}/conditions/bucket_c32_short_short/requests?page=4")
            assert result.status_code == 200, result.text
            assert [row["number"] for row in result.json()["rows"]] == [31, 32]
            await client.post(f"/api/runs/{identifier}/cancel", json={})


def test_ambiguous_target_local_condition_requires_selector():
    report = saved_report([record(0), record(1, target="other")])
    index = ConditionRequestIndex.from_report(report)
    with pytest.raises(ValueError, match="target-local"):
        index.page("bucket_c32_short_short")
    assert index.page("bucket_c32_short_short", target="other")["rows"][0]["request_id"] == "request-1"
