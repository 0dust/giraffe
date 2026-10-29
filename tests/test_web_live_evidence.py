"""Socket-free acceptance for isolated execution and incremental live updates."""
import asyncio
import threading

import pytest

from tests.test_web import browser, finished, report, start, successful_runner

pytestmark = pytest.mark.usefixtures("local_tokenizer")


async def test_live_events_after_sequence_and_completion(tmp_path, monkeypatch):
    ready = threading.Event()

    async def waiting(configuration, *, progress, stop_event):
        progress({"event": "request_started", "target": "local", "scenario": "warm", "request_slot": 0})
        progress({"event": "request_completed", "target": "local", "scenario": "warm", "request_slot": 0,
                  "request": {"id": "request-one", "status": "completed", "valid": True, "elapsed_ms": 2}})
        ready.set()
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        assert await asyncio.to_thread(ready.wait, 2)
        path = f"/api/runs/{identifier}"
        detail = (await client.get(path)).json()
        response = await client.get(path + "/events?after=0")
        assert response.status_code == 200
        delta = response.json()
        assert delta["seq"] >= 2 and not delta["reset"]
        assert [event["seq"] for event in delta["events"]] == sorted({event["seq"] for event in delta["events"]})
        assert delta["records"][-1]["id"] == "request-one"
        assert detail["seq"] == delta["seq"]
        pending = asyncio.create_task(client.get(path + f'/events?after={delta["seq"]}'))
        await asyncio.sleep(.02)
        assert not pending.done()
        await client.post(path + "/cancel", json={})
        update = (await asyncio.wait_for(pending, 2)).json()
        assert all(event["seq"] > delta["seq"] for event in update["events"])
        await finished(client, identifier)
        terminal = (await client.get(path + f'/events?after={update["seq"]}')).json()
        assert terminal["state"] == "completed" and terminal["seq"] > delta["seq"]


async def test_run_executes_off_server_loop_and_stop_on_its_loop(tmp_path, monkeypatch):
    server_thread = threading.get_ident()
    observed = []

    async def waiting(configuration, *, progress, stop_event):
        observed.append((threading.get_ident(), asyncio.get_running_loop()))
        progress({"event": "started", "target": "local"})
        await stop_event.wait()
        observed.append((threading.get_ident(), asyncio.get_running_loop()))
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        for _ in range(200):
            if observed:
                break
            await asyncio.sleep(.005)
        assert observed and observed[0][0] != server_thread
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)
        assert observed[0] == observed[1]


async def test_slow_tokenizer_preview_does_not_reserve_run_start(tmp_path, monkeypatch):
    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_preview(*args):
        entered.set()
        await release.wait()
        return {"available": True}

    monkeypatch.setattr("giraffe.local_tokenizer.preview_tokenizer", slow_preview)
    async with browser(tmp_path) as client:
        preview = asyncio.create_task(client.post("/api/targets/tokenizer", json={"model": "test"}))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            identifier = await asyncio.wait_for(start(client), 1)
        finally:
            release.set()
            await preview
        await finished(client, identifier)


async def test_unexpected_run_failure_has_traceback_and_matching_error_id(tmp_path, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("fixture traceback marker")

    monkeypatch.setattr("giraffe.web.run_suite", broken)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
    assert detail["state"] == "error"
    assert detail["error_id"] in detail["error"]
    log = (tmp_path / identifier / "giraffe.log").read_text()
    assert detail["error_id"] in log and "Traceback" in log and "fixture traceback marker" in log


async def test_evicted_cursor_requires_reset_and_current_cursor_only_gets_new_events(tmp_path, monkeypatch):
    ready = threading.Event()

    async def waiting(configuration, *, progress, stop_event):
        for index in range(17000):
            progress({"event": "progress", "completed": index})
        ready.set()
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        assert await asyncio.to_thread(ready.wait, 2)
        path = f"/api/runs/{identifier}"
        for _ in range(200):
            current = (await client.get(path)).json()
            if current["seq"] == 17000:
                break
            await asyncio.sleep(.005)
        stale = (await client.get(path + "/events?after=0")).json()
        assert stale["reset"] and stale["seq"] == 17000
        assert not stale["events"] and not stale["records"]
        retained = (await client.get(path + "/events?after=616")).json()
        assert not retained["reset"] and len(retained["events"]) == 16384
        assert retained["events"][0]["seq"] == 617
        invalid = (await client.get(path + "/events?after=17001")).json()
        assert invalid["reset"]
        assert (await client.get(path + "/events?after=-1")).status_code == 422
        await client.post(path + "/cancel", json={})
        await finished(client, identifier)


async def test_startup_recovers_records_from_unfinalized_runner(tmp_path):
    import json
    from giraffe.evidence import EvidenceWriter
    from giraffe.models import RunConfig
    from giraffe.runner import _Run, record_sink
    from giraffe.storage import write_metadata
    from tests.test_web import config

    directory = tmp_path / "crashed"
    directory.mkdir()
    configuration = RunConfig.model_validate(config(checks=["serving"], retention="all"))
    meta = {"id": directory.name, "state": "running", "owner_host": "old-host",
            "heartbeat_at": "2000-01-01T00:00:00Z", "started_at": "2026-10-01T00:00:00Z",
            "config": configuration.model_dump(mode="json")}
    write_metadata(directory, meta)
    writer = EvidenceWriter(directory, manifest={"config": meta["config"]}, started_at=meta["started_at"])
    token = record_sink.set(writer.record_finished)
    try:
        runner = _Run(configuration, None, None)
        for index in range(4):
            record = report(configuration).requests[0].model_copy(update={"id": f"request-{index}"})
            runner.record_finished(record)
        del runner
    finally:
        record_sink.reset(token)
        writer.close()
    assert not (directory / "report.json").exists()
    async with browser(tmp_path) as client:
        detail = (await client.get("/api/runs/crashed")).json()
        assert detail["state"] == "interrupted"
        assert detail["report"]["request_count"] == 4
        reason = "Giraffe stopped before this run finished; 4 requests recorded."
        assert all(check["status"] == "inconclusive" and check["summary"] == reason
                   for check in detail["report"]["checks"])
        assert len(json.loads((directory / "report.json").read_text())["requests"]) == 4
        assert not (directory / "evidence.jsonl").exists()
    async with browser(tmp_path) as client:
        reopened = (await client.get("/api/runs/crashed")).json()
        assert reopened["state"] == "interrupted" and reopened["report"]["request_count"] == 4


async def test_report_cache_tracks_file_identity_and_parses_on_worker(tmp_path, monkeypatch):
    import os
    from giraffe.models import RunConfig, RunReport
    from giraffe.reporting import write_report
    from giraffe.web import _parsed_report
    from tests.test_web import config

    directory = tmp_path / "saved"
    write_report(report(RunConfig.model_validate(config())), directory)
    _parsed_report.cache_clear()
    server_thread = threading.get_ident()
    original = RunReport.model_validate_json
    calls = []

    def parsed(*args, **kwargs):
        calls.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(RunReport, "model_validate_json", parsed)
    async with browser(tmp_path) as client:
        assert (await client.get("/api/runs/saved")).status_code == 200
        assert (await client.get("/api/runs/saved")).status_code == 200
        assert (await client.get("/api/runs/saved/performance")).status_code == 200
        assert len(calls) == 1 and calls[0] != server_thread
        path = directory / "report.json"
        info = path.stat()
        original_text = path.read_text()
        updated_text = original_text.replace("suite-run-id", "suite-new-id")
        assert len(updated_text) == len(original_text) and updated_text != original_text
        path.write_text(updated_text)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        updated = await client.get("/api/runs/saved")
        assert updated.json()["report"]["run_id"] == "suite-new-id"
        assert len(calls) == 2 and all(thread != server_thread for thread in calls)


async def test_web_error_response_id_matches_server_log_not_active_run(tmp_path, monkeypatch, caplog):
    ready = threading.Event()

    async def waiting(configuration, *, progress, stop_event):
        progress({"event": "started"})
        ready.set()
        await stop_event.wait()
        return report(configuration, cancelled=True)

    from giraffe.performance import build_performance

    def broken_performance(*args, **kwargs):
        if kwargs:
            raise RuntimeError("private unexpected web failure")
        return build_performance(*args, **kwargs)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    monkeypatch.setattr("giraffe.performance.build_performance", broken_performance)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        assert await asyncio.to_thread(ready.wait, 2)
        response = await client.get(f"/api/runs/{identifier}/performance")
        assert response.status_code == 500
        error = response.json()
        assert error["error_id"] in error["detail"] and "private unexpected" not in error["detail"]
        log = (tmp_path / identifier / "giraffe.log").read_text()
        assert error["error_id"] not in log
        assert error["error_id"] in caplog.text and "Traceback" in caplog.text
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)


async def test_log_open_failure_does_not_strand_active_run(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    original = os.open

    def log_unwritable(path, *args, **kwargs):
        if Path(path).name == "giraffe.log":
            raise PermissionError("log is read only")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("giraffe.diagnostics.os.open", log_unwritable)
    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert detail["state"] == "completed"
        assert (await client.get("/api/bootstrap")).json()["active_run_id"] is None


@pytest.mark.parametrize("initial_state", ["interrupted", "running"])
async def test_recovery_retries_interrupted_or_initially_fresh_owner(tmp_path, monkeypatch, initial_state):
    from datetime import datetime, timezone
    from giraffe.evidence import EvidenceWriter
    from giraffe.models import RunConfig
    from giraffe.storage import update_metadata, write_metadata
    from tests.test_web import config

    monkeypatch.setattr("giraffe.web._RECOVERY_SECONDS", .02)
    directory = tmp_path / "waiting-recovery"
    directory.mkdir()
    configuration = RunConfig.model_validate(config(retention="all"))
    write_metadata(directory, {"id": directory.name, "state": initial_state, "owner_host": "other-host",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(), "config": config()})
    writer = EvidenceWriter(directory, manifest={"config": configuration.model_dump(mode="json")})
    writer.record_finished(report(configuration).requests[0])
    writer.close()
    async with browser(tmp_path) as client:
        if initial_state == "running":
            assert (await client.get("/api/runs/waiting-recovery")).json()["state"] == "running"
            update_metadata(directory, {"heartbeat_at": "2000-01-01T00:00:00Z"})
        for _ in range(200):
            detail = (await client.get("/api/runs/waiting-recovery")).json()
            if detail["state"] == "interrupted" and detail.get("report"):
                break
            await asyncio.sleep(.005)
        assert detail["state"] == "interrupted" and detail["report"]["request_count"] == 1


async def test_recovery_failure_keeps_server_available_and_evidence_for_retry(tmp_path, monkeypatch):
    from giraffe.evidence import EvidenceWriter
    from giraffe.models import RunConfig
    from giraffe.storage import write_metadata
    from tests.test_web import config

    directory = tmp_path / "cannot-recover"
    directory.mkdir()
    write_metadata(directory, {"id": directory.name, "state": "interrupted", "config": config()})
    writer = EvidenceWriter(directory, manifest={"config": config()})
    writer.record_finished(report(RunConfig.model_validate(config())).requests[0])
    writer.close()

    def cannot_write(*args, **kwargs):
        raise OSError("temporary report write failure")

    monkeypatch.setattr("giraffe.web.write_report", cannot_write)
    async with browser(tmp_path) as client:
        assert (await client.get("/api/bootstrap")).status_code == 200
        detail = (await client.get("/api/runs/cannot-recover")).json()
        assert detail["state"] == "interrupted"
        assert detail["error_id"] in detail["error"]
        assert (directory / "evidence.jsonl").exists()


async def test_concurrent_baseline_saves_keep_name_unique(tmp_path, monkeypatch):
    from giraffe import web
    monkeypatch.setattr(web, "run_suite", successful_runner)
    original = web.save_baseline_report
    entered, release = threading.Event(), threading.Event()

    def slow_copy(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(web, "save_baseline_report", slow_copy)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await finished(client, identifier)
        body = {"run_id": identifier, "name": "one name"}
        first = asyncio.create_task(client.post("/api/baselines", json=body))
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(client.post("/api/baselines", json=body))
        await asyncio.sleep(.02)
        release.set()
        responses = await asyncio.gather(first, second)
        assert sorted(response.status_code for response in responses) == [201, 409]
        baselines = (await client.get("/api/baselines")).json()["baselines"]
        assert len(baselines) == 1 and baselines[0]["name"] == "one name"
