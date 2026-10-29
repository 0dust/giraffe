"""Bounded regressions for worker-loop reuse and server shutdown."""
import asyncio
import json
import threading

import pytest

from giraffe.web import _RunThread
from tests.test_web import browser, finished, report, start, successful_runner

pytestmark = pytest.mark.usefixtures("local_tokenizer")


async def test_sequential_runs_reuse_dedicated_measurement_loop(tmp_path, monkeypatch):
    observed = []

    async def measured(configuration, *, progress, stop_event):
        observed.append((threading.get_ident(), asyncio.get_running_loop()))
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", measured)
    server_loop = asyncio.get_running_loop()
    async with browser(tmp_path) as client:
        for _ in range(2):
            identifier = await asyncio.wait_for(start(client), 3)
            detail = await asyncio.wait_for(finished(client, identifier), 3)
            assert detail["state"] == "completed"
    assert len(observed) == 2 and observed[0] == observed[1]
    assert observed[0][1] is not server_loop
    assert server_loop.get_debug() and observed[0][1].get_debug()


async def test_lifespan_worker_exception_finishes_metadata_and_evidence(tmp_path, monkeypatch):
    entered = threading.Event()
    closed = []
    from giraffe.evidence import EvidenceWriter
    original_close = EvidenceWriter.close

    def close(writer):
        original_close(writer)
        closed.append(writer)

    async def failing(configuration, *, progress, stop_event):
        entered.set()
        await stop_event.wait()
        raise RuntimeError("shutdown worker regression")

    monkeypatch.setattr("giraffe.web.run_suite", failing)
    monkeypatch.setattr(EvidenceWriter, "close", close)

    async def shutdown():
        async with browser(tmp_path) as client:
            identifier = await asyncio.wait_for(start(client), 3)
            assert await asyncio.to_thread(entered.wait, 2)
        return identifier

    identifier = await asyncio.wait_for(shutdown(), 3)
    metadata = json.loads((tmp_path / identifier / ".web.json").read_text())
    assert metadata["state"] == "error"
    assert metadata["error_id"] in (tmp_path / identifier / "giraffe.log").read_text()
    assert closed and all(writer.stopped for writer in closed)


async def test_repeated_execute_cancellation_releases_active_run(tmp_path, monkeypatch):
    from giraffe.storage import Heartbeat

    tasks = []
    started = threading.Event()
    finalizing = threading.Event()
    release = threading.Event()
    original_start = _RunThread.start
    original_close = Heartbeat.close

    def capture_start(worker):
        tasks.append(asyncio.current_task())
        original_start(worker)

    def close(heartbeat):
        original_close(heartbeat)
        finalizing.set()
        release.wait(timeout=2)

    async def waiting(configuration, *, progress, stop_event):
        started.set()
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr(_RunThread, "start", capture_start)
    monkeypatch.setattr(Heartbeat, "close", close)
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        await asyncio.wait_for(start(client), 3)
        assert await asyncio.to_thread(started.wait, 2)
        tasks[0].cancel()
        try:
            assert await asyncio.to_thread(finalizing.wait, 2)
            tasks[0].cancel()
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(tasks[0], return_exceptions=True), 3)
        assert (await client.get("/api/bootstrap")).json()["active_run_id"] is None
        monkeypatch.setattr("giraffe.web.run_suite", successful_runner)
        identifier = await asyncio.wait_for(start(client), 3)
        assert (await asyncio.wait_for(finished(client, identifier), 3))["state"] == "completed"


async def test_late_progress_tolerates_closed_server_loop(tmp_path, monkeypatch):
    callbacks = []

    async def measured(configuration, *, progress, stop_event):
        callbacks.append(progress)
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", measured)
    async with browser(tmp_path) as client:
        identifier = await asyncio.wait_for(start(client), 3)
        await asyncio.wait_for(finished(client, identifier), 3)

    class ClosedLoop:
        def call_soon_threadsafe(self, *args):
            raise RuntimeError("Event loop is closed")

    callback = callbacks[0]
    cells = dict(zip(callback.__code__.co_freevars, callback.__closure__))
    cells["server_loop"].cell_contents = ClosedLoop()
    callback({"event": "progress", "completed": 1})


async def test_shutdown_shows_partial_save_and_waits_for_slow_builder(tmp_path, monkeypatch):
    from giraffe import web
    from giraffe.runner import record_sink
    from tests.test_evidence import record

    started, building, release = threading.Event(), threading.Event(), threading.Event()
    identifiers = []

    async def slow(configuration, *, progress, stop_event):
        progress({"event": "scenario_started", "target": "local", "scenario": "warm",
                  "completed": 1, "attempted": 1})
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            building.set()
            assert release.wait(3)
            row = record()
            record_sink.get()(row)
            error.giraffe_report = report(configuration, cancelled=True)
            error.giraffe_report.requests = [row]
            raise

    monkeypatch.setattr(web, "run_suite", slow)
    monkeypatch.setattr(web, "_SHUTDOWN_SECONDS", .01)
    monkeypatch.setattr(web, "_PARTIAL_REPORT_SECONDS", .5)

    async def shutdown():
        async with browser(tmp_path) as client:
            identifiers.append(await start(client))
            assert await asyncio.to_thread(started.wait, 2)

    task = asyncio.create_task(shutdown())
    try:
        assert await asyncio.to_thread(building.wait, 2)
        directory = tmp_path / identifiers[0]
        meta = json.loads((directory / ".web.json").read_text())
        assert meta["state"] == "stopping"
        assert meta["progress"]["message"] == "Saving partial report…"
        assert meta["progress"]["event"] == "saving_partial_report"
        assert meta["progress"]["completed"] == 1
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    saved = json.loads((directory / "report.json").read_text())
    assert len(saved["requests"]) == 1
    assert saved["observations"]["evidence_dropped_count"] == 0
    assert json.loads((directory / ".web.json").read_text())["state"] == "completed"
