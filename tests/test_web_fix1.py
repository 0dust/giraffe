"""Failure and lifecycle regressions for round-one web fixes, without sockets."""
import asyncio
import errno
import json
import threading
import time

import pytest

from giraffe import web
from giraffe.evidence import EvidenceWriter
from giraffe.models import RunConfig
from giraffe.storage import write_metadata
from tests.test_web import browser, config, finished, report, start, successful_runner

pytestmark = pytest.mark.usefixtures("local_tokenizer")


@pytest.mark.parametrize("error", [OSError(errno.ENOSPC, "journal full"), ValueError("non-finite journal"), RuntimeError("retention failed")])
async def test_journal_close_failure_keeps_finished_report(tmp_path, monkeypatch, error):
    original = EvidenceWriter.close

    def broken_close(self):
        original(self)
        raise error

    monkeypatch.setattr(EvidenceWriter, "close", broken_close)
    monkeypatch.setattr(web, "run_suite", successful_runner)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert detail["state"] == "completed" and detail["report"]["request_count"] == 1
        assert (tmp_path / identifier / "report.json").is_file()
        assert any("Live evidence journal failed" in warning for warning in detail["report"]["warnings"])
        assert "exporting final report" in (tmp_path / identifier / "giraffe.log").read_text()


async def test_external_active_run_events_wait_and_return_idle(tmp_path, monkeypatch):
    directory = tmp_path / "external"
    directory.mkdir()
    write_metadata(directory, {"id": directory.name, "state": "running", "heartbeat_at": web._now(), "config": config()})
    monkeypatch.setattr(web, "_EVENT_WAIT_SECONDS", .06)
    async with browser(tmp_path) as client:
        begin = time.monotonic()
        response = await asyncio.wait_for(client.get("/api/runs/external/events?after=0"), 1)
        assert time.monotonic() - begin >= .05
        assert response.json()["idle"] and not response.json()["reset"]
        assert response.json()["events"] == []


async def test_thousand_events_and_check_statuses_are_incremental(tmp_path, monkeypatch):
    ready = threading.Event()

    async def waiting(configuration, *, progress, stop_event):
        for index in range(1000):
            progress({"event": "progress", "completed": index})
        progress({"event": "check_finished", "target": "local", "check": {
            "id": "serving", "target": "local", "status": "pass", "title": "Serving", "summary": "Finished"}})
        ready.set()
        await stop_event.wait()
        return report(configuration)

    monkeypatch.setattr(web, "run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        assert await asyncio.to_thread(ready.wait, 2)
        delta = (await client.get(f"/api/runs/{identifier}/events?after=0")).json()
        assert not delta["reset"] and delta["seq"] == 1001
        assert len(delta["events"]) == 1001
        assert delta["checks"][0]["id"] == "serving" and delta["checks"][0]["status"] == "pass"
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)


async def test_completed_runner_publishes_final_check_results(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "run_suite", successful_runner)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await finished(client, identifier)
        delta = (await client.get(f"/api/runs/{identifier}/events?after=0")).json()
        assert delta["state"] == "completed" and not delta["reset"]
        assert delta["checks"][0]["id"] == "serving"
        assert delta["checks"][0]["status"] == "pass"
        assert any(event["event"] == "check_finished" for event in delta["events"])


async def test_condition_handshake_uses_thread_safe_test_gates(tmp_path, monkeypatch):
    from tests.test_condition_planned import test_run_exposes_all_waiting_conditions_before_first_event_and_updates_in_place as handshake
    await asyncio.wait_for(handshake(tmp_path, monkeypatch), timeout=3)


async def test_late_progress_from_finished_run_does_not_mutate_next_run(tmp_path, monkeypatch):
    callbacks = []
    ready = threading.Event()

    async def waiting(configuration, *, progress, stop_event):
        callbacks.append(progress)
        progress({"event": "run_started"})
        ready.set()
        await stop_event.wait()
        return report(configuration)

    monkeypatch.setattr(web, "run_suite", waiting)
    async with browser(tmp_path) as client:
        first = await start(client)
        assert await asyncio.to_thread(ready.wait, 2)
        await client.post(f"/api/runs/{first}/cancel", json={})
        await finished(client, first)
        ready.clear()
        second = await start(client)
        assert await asyncio.to_thread(ready.wait, 2)
        callbacks[0]({"event": "progress", "completed": 9999})
        await asyncio.sleep(.02)
        detail = (await client.get(f"/api/runs/{second}")).json()
        assert detail["progress"].get("completed") != 9999
        await client.post(f"/api/runs/{second}/cancel", json={})
        await finished(client, second)
    callbacks[0]({"event": "progress", "completed": 9999})


async def test_error_run_and_final_verdict_summary_are_not_recovered(tmp_path):
    for name, state in (("failed", "error"), ("summarized", "interrupted")):
        directory = tmp_path / name
        directory.mkdir()
        write_metadata(directory, {"id": name, "state": state, "error": "original failure", "config": config()})
        writer = EvidenceWriter(directory, manifest={"config": config()})
        writer.record_finished(report(RunConfig.model_validate(config())).requests[0])
        writer.close()
        if name == "summarized":
            (directory / "summary.json").write_text(json.dumps({"state": "error", "overall": "fail", "error": "final verdict"}))
    async with browser(tmp_path) as client:
        assert (await client.get("/api/runs/failed")).json()["error"] == "original failure"
        assert (await client.get("/api/runs/summarized")).json()["summary"]["overall"] == "fail"
    for name in ("failed", "summarized"):
        assert (tmp_path / name / "evidence.jsonl").exists()
        assert not (tmp_path / name / "report.json").exists()
    assert json.loads((tmp_path / "summarized" / "summary.json").read_text())["overall"] == "fail"


async def test_listing_does_not_populate_full_report_cache(tmp_path, monkeypatch):
    from giraffe.reporting import write_report
    write_report(report(RunConfig.model_validate(config())), tmp_path / "saved")
    web._report_cache.clear()

    def never_parse(*args, **kwargs):
        pytest.fail("listing parsed a full RunReport")

    monkeypatch.setattr(web.RunReport, "model_validate_json", never_parse)
    async with browser(tmp_path) as client:
        response = await client.get("/api/runs")
        assert response.status_code == 200
        assert response.json()["runs"][0]["request_count"] == 1
    assert web._report_cache.bytes == 0


def test_report_cache_evicts_by_total_estimated_bytes(monkeypatch):
    monkeypatch.setattr(web, "_REPORT_CACHE_BYTES", 20)
    cache = web._ReportCache()
    first = cache.get("first", 12, lambda: object())
    cache.get("second", 12, lambda: object())
    assert cache.bytes <= 20
    assert cache.get("first", 12, lambda: object()) is not first
    cache.get("oversized", 21, lambda: object())
    assert "oversized" not in cache.entries and cache.bytes <= 20
