"""Forced task cancellation keeps the runner's terminal partial report on disk."""

import asyncio
import inspect
import json
import threading

import pytest

from giraffe import web
from tests.test_runner import client as client

from giraffe.cli import main
from giraffe.models import CheckResult, RunConfig, RunReport
from giraffe.web import create_app

pytestmark = pytest.mark.usefixtures("local_tokenizer")


def partial_report(config, reason="cancelled by user"):
    return RunReport(run_id="forced-stop", started_at="now", finished_at="later",
                     overall="inconclusive", abort_reason=reason,
                     manifest={"config": config.model_dump(mode="json")},
                     checks=[CheckResult(id="serving", target="local", title="Serving",
                                         status="inconclusive", summary="Stopped")])


def execute_job(tmp_path, config, **options):
    app = create_app(tmp_path, **options)
    launch = next(route.endpoint for route in app.routes
                  if getattr(route, "path", None) == "/api/runs" and "POST" in route.methods)
    execute = inspect.getclosurevars(launch).nonlocals["execute"]
    directory = tmp_path / "job"
    directory.mkdir()
    worker = web._RunThread(None)
    job = {"id": "job", "directory": directory,
           "meta": {"state": "stopping", "started_at": web._now(),
                    "config": config.model_dump(mode="json")},
           "worker": worker, "future": worker.future,
           "open_files": {"test": True}, "conditions": {},
           "progress": {}, "events": [], "phases": [], "live_request_summary": {},
           "live_request_count": 0, "seq": 0, "deltas": [], "changed": asyncio.Event(),
           "checks": {}, "live_preparations": {}}
    return execute, job


@pytest.mark.parametrize("cancel_execute", [False, True])
async def test_web_forced_cancel_after_stop_grace_saves_partial_report(tmp_path, monkeypatch, cancel_execute):
    config = RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}])
    started = threading.Event()
    partial = partial_report(config)
    cancellation = None
    exports = []
    cleanup_calls = []
    original_write = web.write_report

    def write(report, *args, **kwargs):
        exports.append(report)
        return original_write(report, *args, **kwargs)

    async def run(config, *, progress, stop_event):
        nonlocal cancellation
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            assert stop_event.is_set()
            cancellation = error
            error.giraffe_report = partial
            raise

    monkeypatch.setattr(web, "run_suite", run)
    monkeypatch.setattr(web, "write_report", write)
    monkeypatch.setattr(web, "automatic_cleanup", lambda *a, **k: cleanup_calls.append(True))
    execute, job = execute_job(tmp_path, config, auto_cleanup={"keep_latest": 0})
    task = asyncio.create_task(execute(job, config, None))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        job["worker"].request_stop()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.01)
        if cancel_execute:
            task.cancel()
        else:
            job["worker"].request_stop(cancel=True)
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value is cancellation
    finally:
        await asyncio.to_thread(job["worker"].owner.close)
    directory = job["directory"]
    saved = json.loads((directory / "report.json").read_text())
    assert len(exports) == 1 and exports[0] is partial
    assert cleanup_calls == []
    assert saved["run_id"] == partial.run_id
    assert saved["abort_reason"] == "cancelled by user"
    assert saved["manifest"]["invocation"]["source"] == "web_api"
    assert saved["manifest"]["open_files"] == {"test": True}
    metadata = json.loads((directory / ".web.json").read_text())
    assert metadata["state"] == "completed"
    assert not metadata.get("error")


@pytest.mark.parametrize("forced", [False, True])
async def test_real_web_run_journals_every_terminal_request(tmp_path, monkeypatch, forced):
    config = RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}],
                       checks=["correctness"] if forced else ["serving"], samples=8, max_requests=16,
                       concurrency=4, test_options={"correctness": {"concurrency": 4}})
    ready = threading.Event()
    if forced:
        from giraffe import runner
        active = 0
        async def held(self, spec, **kwargs):
            nonlocal active
            active += 1
            if active == 4:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        monkeypatch.setattr(runner.LLMClient, "execute", held)
    captured = []
    original_write = web.write_report

    def write(report, directory, **kwargs):
        # Successful export removes the journal, so inspect the flushed file at
        # its public export boundary. Exercise real run_suite/request(), not a
        # fake runner that calls the sink itself.
        rows = [json.loads(line) for line in (directory / "evidence.jsonl").read_text().splitlines()]
        records = [row["record"] for row in rows if row["type"] == "record"]
        assert len(records) == len(report.requests) > 0
        assert [r["id"] for r in records] == [r.id for r in report.requests]
        if forced:
            assert len(records) == 4
            assert all(r["status"] == "cancelled" for r in records)
        captured.append(records)
        return original_write(report, directory, **kwargs)

    monkeypatch.setattr(web, "write_report", write)
    execute, job = execute_job(tmp_path, config)
    task = asyncio.create_task(execute(job, config, None))
    try:
        if forced:
            assert await asyncio.to_thread(ready.wait, 2)
            job["worker"].request_stop(cancel=True)
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    finally:
        await asyncio.to_thread(job["worker"].owner.close)
        await asyncio.gather(task, return_exceptions=True)
    assert job["state"] == "completed"
    assert len(captured) == 1
    assert not (job["directory"] / "evidence.jsonl").exists()


async def test_web_cancel_during_export_finishes_one_write(tmp_path, monkeypatch):
    config = RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}],
                       checks=["serving"], samples=5, max_requests=10)
    entered, release = threading.Event(), threading.Event()
    exports = []
    original_write = web.write_report

    def write(report, directory, **kwargs):
        exports.append(report)
        entered.set()
        assert release.wait(3)
        return original_write(report, directory, **kwargs)

    monkeypatch.setattr(web, "write_report", write)
    execute, job = execute_job(tmp_path, config)
    task = asyncio.create_task(execute(job, config, None))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(job["worker"].owner.close)
    assert len(exports) == 1
    assert job["state"] == "completed"
    assert json.loads((job["directory"] / ".web.json").read_text())["state"] == "completed"
    assert (job["directory"] / "report.json").is_file()


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_cli_forced_cancel_without_signal_handlers_saves_partial_report(tmp_path, monkeypatch, capsys, output_format):
    signals = []
    cleanup_calls = []

    def unsupported(signal, *args):
        signals.append(signal)
        raise NotImplementedError

    loop = asyncio.new_event_loop()
    loop_type = type(loop)
    loop.close()
    monkeypatch.setattr(loop_type, "add_signal_handler", unsupported)

    async def run(config, *, progress, stop_event):
        assert not stop_event.is_set()
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            error.giraffe_report = partial_report(config, "run cancelled")
            error.giraffe_report.observations["summary"] = "Partial evidence saved."
            raise

    def cleanup(*args, **kwargs):
        cleanup_calls.append((args, kwargs))
        return {"deleted": []}

    monkeypatch.setattr("giraffe.runner.run_suite", run)
    monkeypatch.setattr("giraffe.storage.automatic_cleanup", cleanup)
    directory = tmp_path / "stopped"
    assert main(["run", "--url", "http://server", "--model", "tiny", "--skip-preflight",
                 "--min-free-disk", "0", "--output", str(directory), "--runs-dir", str(tmp_path),
                 "--auto-cleanup-keep-latest", "0", *(["--json"] if output_format == "json" else [])]) == 130
    assert cleanup_calls == []
    assert len(signals) == 2
    saved = json.loads((directory / "report.json").read_text())
    assert saved["run_id"] == "forced-stop"
    assert saved["abort_reason"] == "run cancelled"
    assert saved["manifest"]["invocation"]["source"] == "cli"
    metadata = json.loads((directory / ".web.json").read_text())
    assert metadata["state"] == "completed"
    assert not metadata.get("error")
    output = capsys.readouterr()
    assert "Traceback" not in output.err
    if output_format == "json":
        result = json.loads(output.out)
        assert result["outcome"] == "inconclusive" and result["exit_code"] == 130
        assert result["run_id"] == saved["run_id"]
        assert result["abort_reason"] == "run cancelled"
        assert result["summary"] == "Partial evidence saved."
        assert result["report_paths"] == {"json": str(directory / "report.json"), "html": None}
        assert result["auto_cleanup"] is None
    else:
        assert f"INCONCLUSIVE  0 checks passed · 0 requests · report: {directory / 'report.json'}" in output.out
        assert "Stopped: run cancelled" in output.out
        assert "Partial evidence saved." in output.out


@pytest.mark.parametrize("exception", [KeyboardInterrupt, asyncio.CancelledError])
def test_cli_cancel_without_partial_report_is_quiet(tmp_path, monkeypatch, capsys, exception):
    async def interrupted(*args, **kwargs):
        raise exception()

    monkeypatch.setattr("giraffe.cli._run", interrupted)
    directory = tmp_path / "stopped-without-report"
    assert main(["run", "--url", "http://server", "--model", "tiny", "--skip-preflight",
                 "--min-free-disk", "0", "--output", str(directory)]) == 130
    captured = capsys.readouterr()
    assert captured.err.splitlines()[-1] == "Stopped."
    assert "Traceback" not in captured.err
    log = (directory / "giraffe.log").read_text()
    assert "Traceback" in log and exception.__name__ in log


@pytest.mark.parametrize("expires", [False, True])
async def test_slow_partial_build_keeps_writer_until_engine_finishes(tmp_path, monkeypatch, expires):
    from giraffe.evidence import EvidenceWriter, recover_evidence
    from giraffe.models import RequestRecord
    from giraffe.runner import record_sink

    config = RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}])
    started, building, release = threading.Event(), threading.Event(), threading.Event()
    writers, exports = [], []
    budget = .08 if expires else .5
    monkeypatch.setattr(web, "_PARTIAL_REPORT_SECONDS", budget, raising=False)

    def writer(*args, **kwargs):
        instance = EvidenceWriter(*args, **kwargs)
        writers.append(instance)
        return instance

    async def run(config, *, progress, stop_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            rows = [RequestRecord(id=str(i), target="local", fixture_id="one", scenario="warm",
                                  check_ids=["serving"], started_at="now", status="cancelled") for i in range(2)]
            record_sink.get()(rows[0])
            building.set()
            # A synchronous validator can hold the engine loop during shutdown.
            assert release.wait(5)
            record_sink.get()(rows[1])
            error.giraffe_report = partial_report(config)
            error.giraffe_report.requests = rows
            raise

    original_write = web.write_report
    def write(report, *args, **kwargs):
        exports.append(report)
        return original_write(report, *args, **kwargs)

    monkeypatch.setattr(web, "EvidenceWriter", writer)
    monkeypatch.setattr(web, "run_suite", run)
    monkeypatch.setattr(web, "write_report", write)
    execute, job = execute_job(tmp_path, config)
    task = asyncio.create_task(execute(job, config, None))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(building.wait, 2)
        if expires:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), .5)
            assert job["state"] == "interrupted"
            assert not (job["directory"] / "report.json").exists()
        else:
            assert job["progress"].get("message") == "Saving partial report…"
        assert not writers[0]._closed, "server closed the journal while the engine was writing"
        release.set()
        if not expires:
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.to_thread(job["future"].result, 2)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(job["worker"].owner.close)
    assert writers[0].stopped and writers[0].dropped_count == 0
    if expires:
        assert exports == []
        recovered = recover_evidence(job["directory"], job["meta"])
        assert {r.id for r in recovered.requests} == {"0", "1"}
    else:
        assert len(exports) == 1 and job["state"] == "completed"


@pytest.mark.parametrize("reason", ["cancelled by user", "run cancelled"])
async def test_saved_stopped_run_exposes_reason_in_history(tmp_path, reason):
    from tests.test_web import browser
    from giraffe.reporting import write_report
    from giraffe.storage import write_metadata

    config = RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}])
    directory = tmp_path / "stopped"
    write_report(partial_report(config, reason), directory)
    write_metadata(directory, {"id": "stopped", "state": "completed"})
    async with browser(tmp_path) as browser_client:
        detail = (await browser_client.get("/api/runs/stopped")).json()
        row = (await browser_client.get("/api/runs")).json()["runs"][0]
    assert detail["state"] == row["state"] == "completed"
    assert detail["abort_reason"] == row["abort_reason"] == reason


async def test_engine_cancel_during_journal_flush_keeps_built_report(tmp_path, monkeypatch):
    from giraffe.evidence import EvidenceWriter
    entered, release = threading.Event(), threading.Event()
    original_close = EvidenceWriter.close

    def close(writer):
        entered.set()
        assert release.wait(3)
        original_close(writer)

    monkeypatch.setattr(EvidenceWriter, "close", close)
    config = RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}],
                       checks=["serving"], samples=2)
    execute, job = execute_job(tmp_path, config)
    task = asyncio.create_task(execute(job, config, None))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        job["worker"].request_stop(cancel=True)
        await asyncio.sleep(.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(job["worker"].owner.close)
    assert job["state"] == "completed"
    assert (job["directory"] / "report.json").is_file()


def test_late_engine_progress_keeps_partial_save_message(tmp_path):
    config = RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}])
    _, job = execute_job(tmp_path, config)
    job["partial_report_deadline"] = 1.0
    web._track_live_progress(job, {"event": "run_completed", "completed": 4}, None)
    assert job["progress"]["completed"] == 4
    assert job["progress"]["message"] == "Saving partial report…"
    assert job["progress"]["phase"] == "saving_partial_report"
