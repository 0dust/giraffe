"""Bounded regressions for worker-loop reuse and server shutdown."""
import asyncio
import json
import threading

import httpx
import pytest

from giraffe.models import RunConfig
from giraffe.web import _RunThread
from tests.test_web import browser, config, finished, report, start, successful_runner

pytestmark = pytest.mark.usefixtures("local_tokenizer")


@pytest.mark.parametrize("boundary,expected_requests", [("before_dispatch", 1), ("in_flight", 2)])
async def test_cancelled_token_probe_download_is_unavailable(tmp_path, monkeypatch, boundary, expected_requests):
    from giraffe import states
    from giraffe.runner import _Run
    from tests.test_automatic_performance_evidence import completion

    reached = threading.Event()
    sent = []

    async def endpoint(request):
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        if body.get("max_tokens") == 16:
            sent.append(body)
            if body.get("logprobs"):
                return httpx.Response(400, json={"error": {"param": "logprobs"}})
            reached.set()
            await asyncio.Event().wait()
        return completion()

    actual_client = httpx.AsyncClient

    def http_client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(endpoint))
        return actual_client(**kwargs)

    original_request = _Run.request

    async def request(run, target, client, spec, **kwargs):
        if (boundary == "before_dispatch" and spec.scenario == "performance_evidence_probe"
                and "logprobs" not in spec.options):
            reached.set()
            await run.stop.wait()
        return await original_request(run, target, client, spec, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", http_client)
    monkeypatch.setattr(_Run, "request", request)
    values = config(checks=["serving"], samples=1, max_requests=100, metrics=False,
                    request_timeout_seconds=10, max_duration_seconds=20)
    async with browser(tmp_path) as client:
        identifier = await start(client, values)
        assert await asyncio.to_thread(reached.wait, 3)
        response = await client.post(f"/api/runs/{identifier}/cancel", json={})
        assert response.status_code == 200
        detail = await finished(client, identifier)
        assert detail["state"] == states.RUN_COMPLETED
        downloaded = await client.get(f"/api/runs/{identifier}/report.json")
        assert downloaded.status_code == 200
        saved = downloaded.json()

    assert saved["abort_reason"] == states.STOP_CANCELLED_BY_USER
    probes = [row for row in saved["requests"] if row["scenario"] == "performance_evidence_probe"]
    assert len(probes) == len(sent) == expected_requests
    assert probes[0]["http_status"] == 400
    assert probes[0]["workload"]["rejected_evidence_field"] == "logprobs"
    if boundary == "in_flight":
        assert probes[-1]["status"] == "cancelled"
        assert probes[-1]["termination_reason"] == "user_stop"
    observation = saved["observations"]["targets"]["local"]
    evidence = observation["token_evidence"]
    assert evidence["status"] == states.TOKEN_EVIDENCE_UNAVAILABLE
    assert evidence["probe_requests"] == expected_requests
    assert evidence["logprobs"] == states.TOKEN_EVIDENCE_REJECTED
    assert evidence["usage"] == states.TOKEN_EVIDENCE_UNVERIFIED
    assert "Endpoint rejected logprobs (HTTP 400)." in evidence["reason"]
    assert states.STOP_CANCELLED_BY_USER in evidence["reason"]
    assert "budget" not in evidence["reason"].lower()
    preparation = observation["preparations"]["performance_evidence_probe"]
    assert preparation["status"] == "unavailable"
    assert preparation["reason"] == evidence["reason"]
    condition = next(row for row in saved["conditions"] if row["condition_id"] == "performance_evidence_probe")
    assert condition["status"] == states.CONDITION_AGGREGATE_INCONCLUSIVE
    assert condition["reason"] == evidence["reason"]
    assert condition["started"] == condition["completed"] == expected_requests
    live = next(row for row in detail["report"]["conditions"] if row["condition_id"] == "performance_evidence_probe")
    assert live["status"] == states.CONDITION_AGGREGATE_INCONCLUSIVE
    assert live["reason"] == evidence["reason"]


async def test_run_terminal_and_cancel_results(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    async def waiting(configuration, *, progress, stop_event):
        progress({"event": "scenario_started", "target": "local", "scenario": "warm"})
        entered.set()
        await stop_event.wait()
        while not release.is_set():
            await asyncio.sleep(.001)
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        try:
            response = await client.post("/api/runs", json={"config": config()})
            assert response.status_code == 202
            assert response.json()["terminal"] is False
            identifier = response.json()["id"]
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get(f"/api/runs/{identifier}")).json()["terminal"] is False
            assert (await client.get(f"/api/runs/{identifier}/events")).json()["terminal"] is False
            assert (await client.get("/api/runs")).json()["runs"][0]["terminal"] is False
            conflict = await client.post("/api/runs", json={"config": config()})
            assert conflict.status_code == 409
            assert conflict.json()["detail"] == "A run is already active; stop it or wait for its report"
            assert conflict.headers["X-Giraffe-Error-Code"] == "run_active"
            for result in ("stopping", "already_stopping"):
                response = await client.post(f"/api/runs/{identifier}/cancel", json={})
                assert response.status_code == 200
                assert response.json() == {"id": identifier, "state": "stopping",
                                           "terminal": False, "result": result}
        finally:
            release.set()
        assert (await finished(client, identifier))["terminal"] is True
        assert (await client.get(f"/api/runs/{identifier}/events")).json()["terminal"] is True
        assert (await client.get("/api/runs")).json()["runs"][0]["terminal"] is True
        response = await client.post(f"/api/runs/{identifier}/cancel", json={})
        assert response.json() == {"id": identifier, "state": "completed",
                                   "terminal": True, "result": "already_finished"}


@pytest.mark.parametrize("state,terminal", [("queued", False), ("running", False), ("stopping", False),
                                          ("completed", True), ("interrupted", True), ("error", True)])
async def test_saved_run_terminal_events_and_cancel_result(tmp_path, monkeypatch, state, terminal):
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    async with browser(tmp_path) as client:
        directory = tmp_path / "saved"
        directory.mkdir()
        (directory / ".web.json").write_text(json.dumps({"id": "saved", "state": state}))
        detail = (await client.get("/api/runs/saved")).json()
        assert detail["state"] == state and detail["terminal"] is terminal
        events = (await client.get("/api/runs/saved/events")).json()
        assert events["state"] == state and events["terminal"] is terminal
        assert events["reset"] is terminal
        if not terminal:
            assert events["idle"] is True
        response = await client.post("/api/runs/saved/cancel", json={})
        assert response.json() == {"id": "saved", "state": state, "terminal": terminal,
                                   "result": "already_finished" if terminal else "not_owned"}


@pytest.mark.parametrize("state", ["queued", "running", "stopping", "completed", "interrupted", "error"])
@pytest.mark.parametrize("journal", [False, True])
async def test_recoverable_is_reported_beside_terminal(tmp_path, monkeypatch, state, journal):
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    async with browser(tmp_path) as client:
        # Created after startup, so startup recovery does not consume the journal.
        directory = tmp_path / "saved"
        directory.mkdir()
        (directory / ".web.json").write_text(json.dumps({"id": "saved", "state": state}))
        if journal:
            (directory / "evidence.jsonl").write_text("")
        expected = journal and state == "interrupted"
        assert (await client.get("/api/runs/saved")).json()["recoverable"] is expected
        assert (await client.get("/api/runs/saved/events")).json()["recoverable"] is expected
        if state in {"completed", "interrupted", "error"}:
            row = next(run for run in (await client.get("/api/runs")).json()["runs"] if run["id"] == "saved")
            assert row["recoverable"] is expected


@pytest.mark.parametrize("saved_report", ["absent", "valid", "unreadable", "invalid_schema"])
async def test_recoverable_distinguishes_valid_report_from_retained_journal(tmp_path, saved_report):
    from giraffe.reporting import write_report
    from giraffe.storage import write_metadata

    async with browser(tmp_path) as client:
        # Create after startup to inspect pending work before recovery runs.
        directory = tmp_path / "saved"
        write_metadata(directory, {"id": directory.name, "state": "interrupted"})
        if saved_report in {"valid", "invalid_schema"}:
            write_report(report(RunConfig.model_validate(config())), directory)
            if saved_report == "invalid_schema":
                contents = json.loads((directory / "report.json").read_text())
                contents["requests"][0]["status"] = "invalid-status"
                (directory / "report.json").write_text(json.dumps(contents))
        elif saved_report == "unreadable":
            (directory / "report.json").write_text("invalid JSON")
        (directory / "evidence.jsonl").write_text("")
        for endpoint in ("/api/runs/saved", "/api/runs/saved/events"):
            result = (await client.get(endpoint)).json()
            # Unreadable reports retain their existing error presentation.
            assert result["state"] == ("error" if saved_report in {"unreadable", "invalid_schema"} else "interrupted")
            assert result["recoverable"] is (saved_report != "valid")
        row = next(row for row in (await client.get("/api/runs")).json()["runs"] if row["id"] == "saved")
        # The listing parser can read a summary from a schema-invalid report;
        # that summary must not count as successful recovery publication.
        assert row["recoverable"] is (saved_report != "valid")


@pytest.mark.parametrize("saved_report", ["absent", "unreadable"])
@pytest.mark.parametrize("final_summary", ["valid", "unreadable", "symlink"])
async def test_final_summary_owns_interrupted_journal_recovery(tmp_path, saved_report, final_summary):
    from giraffe.storage import write_metadata

    async with browser(tmp_path) as client:
        directory = tmp_path / "saved"
        write_metadata(directory, {"id": directory.name, "state": "interrupted"})
        (directory / "evidence.jsonl").write_text("")
        if saved_report == "unreadable":
            (directory / "report.json").write_text("invalid JSON")
        summary_path = directory / "summary.json"
        if final_summary == "symlink":
            target = tmp_path / "retained-summary.json"
            target.write_text(json.dumps({"overall": "fail", "error": "final verdict"}))
            summary_path.symlink_to(target)
        else:
            summary_path.write_text(json.dumps({"overall": "fail", "error": "final verdict"})
                                    if final_summary == "valid" else "invalid JSON")
        for endpoint in ("/api/runs/saved", "/api/runs/saved/events"):
            result = (await client.get(endpoint)).json()
            assert result["terminal"] is True and result["recoverable"] is False
        row = next(row for row in (await client.get("/api/runs")).json()["runs"] if row["id"] == "saved")
        assert row["terminal"] is True and row["recoverable"] is False


@pytest.mark.parametrize("cleanup", ["unlink_failure", "crash_before_unlink"])
@pytest.mark.parametrize("isolated", [False, True])
async def test_published_recovery_report_with_retained_journal_finishes_polling(tmp_path, monkeypatch, cleanup, isolated):
    import errno
    from pathlib import Path

    from giraffe.evidence import EvidenceWriter, recover_evidence
    from giraffe.reporting import ReportWriteError, write_report
    from giraffe.storage import write_metadata

    directory = tmp_path / "recovered"
    meta = {"id": directory.name, "state": "interrupted", "config": config()}
    write_metadata(directory, meta)
    writer = EvidenceWriter(directory, manifest={"config": config()})
    writer.record_finished(report(RunConfig.model_validate(config())).requests[0])
    writer.close()
    recovered = recover_evidence(directory, meta)
    assert recovered is not None and recovered.observations["interrupted"] is True
    journal = directory / "evidence.jsonl"
    if cleanup == "unlink_failure":
        original_unlink = Path.unlink

        def fail_journal_unlink(path, *args, **kwargs):
            if path == journal:
                raise OSError(errno.EACCES, "journal cleanup denied")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_journal_unlink)
            with pytest.raises(ReportWriteError) as failure:
                write_report(recovered, directory)
        assert failure.value.json_complete is True
        assert failure.value.failed_file == "evidence.jsonl"
    else:
        # Same on-disk state as a crash after publication and before journal cleanup.
        write_report(recovered, directory, preserve_evidence=True)
    assert journal.is_file() and (directory / "report.json").is_file()
    published = (directory / "report.json").read_bytes()
    evidence = journal.read_bytes()

    if isolated:
        monkeypatch.setattr("giraffe.web._ISOLATED_REPORT_BYTES", 0)
    async with browser(tmp_path) as client:
        # The documented polling condition must finish despite retained evidence.
        for _ in range(3):
            detail = (await client.get("/api/runs/recovered")).json()
            if detail["terminal"] and not detail["recoverable"]:
                break
        else:
            pytest.fail("Published recovery report still advertises pending recovery")
        assert detail["state"] == "interrupted" and detail["report"]["request_count"] == 1
        events = (await client.get("/api/runs/recovered/events")).json()
        assert events["terminal"] is True and events["recoverable"] is False
        row = next(row for row in (await client.get("/api/runs")).json()["runs"] if row["id"] == "recovered")
        assert row["state"] == "interrupted" and row["terminal"] is True
        assert row["recoverable"] is False
        assert (await client.post("/api/runs/recovered/cancel", json={})).json()["result"] == "already_finished"
    assert journal.read_bytes() == evidence
    assert (directory / "report.json").read_bytes() == published


async def test_interrupted_event_feed_tracks_recovery_report_publication(tmp_path, monkeypatch):
    from giraffe.evidence import recover_evidence
    from giraffe.reporting import write_report

    async def interrupted(configuration, *, progress, stop_event):
        raise asyncio.CancelledError

    monkeypatch.setattr("giraffe.web.run_suite", interrupted)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert detail["state"] == "interrupted" and detail["recoverable"] is True
        path = f"/api/runs/{identifier}/events"
        events = (await client.get(path)).json()
        assert events["reset"] is False and events["recoverable"] is True
        directory = tmp_path / identifier
        recovered = recover_evidence(directory, json.loads((directory / ".web.json").read_text()))
        assert recovered is not None
        write_report(recovered, directory, preserve_evidence=True)
        events = (await client.get(path)).json()
        assert events["reset"] is False and events["terminal"] is True
        assert events["state"] == "interrupted" and events["recoverable"] is False
        assert (directory / "evidence.jsonl").is_file()


async def test_completed_run_with_unreadable_report_exposes_error(tmp_path):
    async with browser(tmp_path) as client:
        directory = tmp_path / "saved"
        directory.mkdir()
        metadata = json.dumps({"id": "saved", "state": "completed"})
        (directory / ".web.json").write_text(metadata)
        (directory / "report.json").write_text("unreadable")
        detail = (await client.get("/api/runs/saved")).json()
        assert detail["state"] == "error" and detail["terminal"] is True
        assert (directory / ".web.json").read_text() == metadata


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
