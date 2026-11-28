"""Stop outcomes stay consistent through live views and report export."""

import asyncio
import json
import os
import socket
import threading
from datetime import datetime, timezone

import pytest

from giraffe import web
from giraffe.storage import write_metadata
from tests.test_runner import FakeClient, client as client, config
from tests.test_stop_cause_contract import assert_stop_contract, wait_event
from tests.test_web import browser, finished, start


@pytest.mark.parametrize("kind,text,announced", [
    ("request_budget", "maximum request budget reached", None),
    ("user", "cancelled by user", "user"),
])
async def test_live_cli_journal_uses_announced_cause(tmp_path, monkeypatch, kind, text, announced):
    monkeypatch.setattr(web, "_EVENT_WAIT_SECONDS", .001)
    directory = tmp_path / "cli"
    directory.mkdir()
    write_metadata(directory, {"id": directory.name, "state": "running",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config().model_dump(mode="json")})
    (directory / "evidence.jsonl").write_text(json.dumps({"type": "stop_cause",
        "stop_cause": {"kind": kind, "text": text, "scope": None}}) + "\n")
    async with browser(tmp_path) as api:
        detail = (await api.get("/api/runs/cli")).json()
        row = (await api.get("/api/runs")).json()["runs"][0]
        feed = (await api.get("/api/runs/cli/events?after=0")).json()
        assert detail["stop_kind"] == row["stop_kind"] == feed["stop_kind"] == announced
        for surface in (detail, row, feed):
            assert surface["abort_reason"] == (text if announced else None)


async def test_cli_listing_does_not_copy_request_rows(tmp_path, monkeypatch):
    directory = tmp_path / "cli"
    directory.mkdir()
    write_metadata(directory, {"id": directory.name, "state": "running",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config().model_dump(mode="json")})
    (directory / "evidence.jsonl").write_text(json.dumps({"type": "record", "request_slot": 0,
        "record": {"id": "request-sentinel", "target": "service", "status": "completed"}}) + "\n")
    original = web.deepcopy
    copied = []

    def copy(value, *args, **kwargs):
        if isinstance(value, dict) and any(isinstance(row, dict) and row.get("id") == "request-sentinel"
                                          for row in value.values()):
            copied.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(web, "deepcopy", copy)
    async with browser(tmp_path) as api:
        assert (await api.get("/api/runs")).status_code == 200
        assert copied == []
        assert (await api.get("/api/runs/cli")).json()["live_requests"][0]["id"] == "request-sentinel"
        assert len(copied) == 1
        copied.clear()
        assert (await api.get("/api/runs")).status_code == 200
        assert copied == []


@pytest.mark.parametrize("cause,expected", [
    ({"kind": "user", "text": "cancelled by user", "scope": None, "future_field": "ignored"}, "user"),
    ({"kind": [], "text": {}}, None),
    (["user"], None),
    ("user", None),
])
async def test_saved_malformed_cause_keeps_detail_and_listing_readable(tmp_path, client, cause, expected):
    async with browser(tmp_path) as api:
        identifier = await start(api, config(samples=2, concurrency=1).model_dump(mode="json"))
        await finished(api, identifier)
    path = tmp_path / identifier / "report.json"
    report = json.loads(path.read_text())
    report["observations"]["stop_cause"] = cause
    path.write_text(json.dumps(report))
    async with browser(tmp_path) as api:
        response = await api.get(f"/api/runs/{identifier}")
        assert response.status_code == 200
        detail = response.json()
        row = (await api.get("/api/runs")).json()["runs"][0]
        assert not detail["error"] and not row["error"]
        assert detail["stop_kind"] == row["stop_kind"] == expected


async def test_summary_only_export_failure_keeps_stop_outcome(tmp_path, client, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(web, "write_report", fail)
    FakeClient.delay = .2
    async with browser(tmp_path) as api:
        identifier = await start(api, config(samples=6, concurrency=1, max_requests=None).model_dump(mode="json"))
        await wait_event(api, identifier, "request_started")
        await api.post(f"/api/runs/{identifier}/cancel", json={})
        detail = await finished(api, identifier)
        feed = (await api.get(f"/api/runs/{identifier}/events?after=0")).json()
        row = (await api.get("/api/runs")).json()["runs"][0]
        summary = json.loads((tmp_path / identifier / "summary.json").read_text())
        metadata = json.loads((tmp_path / identifier / ".web.json").read_text())
        assert detail["state"] == "error" and detail["report"] is None
        for surface in (detail, feed, row, summary, metadata, feed["events"][-1]):
            assert surface["stop_kind"] == "user"
            assert surface["abort_reason"] == "cancelled by user"


async def test_cancellation_during_evidence_close_preserves_head_fallback_and_journals_cause(tmp_path, client, monkeypatch):
    from giraffe.evidence import EvidenceWriter

    entered, release = threading.Event(), threading.Event()
    workers = []
    close, initialize, write = EvidenceWriter.close, web._RunThread.__init__, web.write_report

    def hold_close(writer):
        entered.set()
        assert release.wait(5)
        close(writer)

    def worker_init(worker, *args, **kwargs):
        initialize(worker, *args, **kwargs)
        workers.append(worker)

    monkeypatch.setattr(EvidenceWriter, "close", hold_close)
    monkeypatch.setattr(web._RunThread, "__init__", worker_init)
    monkeypatch.setattr(web, "write_report", lambda *args, **kwargs: write(
        *args, **{**kwargs, "preserve_evidence": True}))
    try:
        async with browser(tmp_path) as api:
            identifier = await start(api, config(samples=2, concurrency=1, max_requests=None).model_dump(mode="json"))
            assert await asyncio.to_thread(entered.wait, 5)
            workers[0].request_stop(cancel=True)
            for _ in range(500):
                if workers[0].cancel_delivered:
                    break
                await asyncio.sleep(.001)
            assert workers[0].cancel_delivered
            release.set()
            detail, _ = await assert_stop_contract(api, tmp_path, identifier, "cancelled")
            assert detail["abort_reason"] == "run cancelled"
    finally:
        release.set()


@pytest.mark.parametrize("kind,reason", [("user", "cancelled by user"), ("duration", "maximum run duration reached")])
async def test_web_metadata_persists_announced_cause_before_cancel(tmp_path, client, monkeypatch, kind, reason):
    from giraffe import runner

    release, trigger = threading.Event(), threading.Event()
    emit, write = runner._Run.emit, web.write_report

    def hold_announced(run, event, *args, **kwargs):
        emit(run, event, *args, **kwargs)
        if event == "request_started" and kind == "duration":
            assert trigger.wait(5)
            run.deadline = 0
            run.stop_reason()
        if event == "run_stopping":
            assert release.wait(5)

    monkeypatch.setattr(runner._Run, "emit", hold_announced)
    monkeypatch.setattr(web, "write_report", lambda *args, **kwargs: write(
        *args, **{**kwargs, "preserve_evidence": True}))
    FakeClient.delay = .2
    try:
        async with browser(tmp_path) as api:
            identifier = await start(api, config(samples=6, concurrency=1, max_requests=None).model_dump(mode="json"))
            await wait_event(api, identifier, "request_started")
            path = tmp_path / identifier / ".web.json"
            initial = json.loads(path.read_text())
            assert initial["stop_kind"] is initial["abort_reason"] is None
            if kind == "duration":
                trigger.set()
            else:
                await api.post(f"/api/runs/{identifier}/cancel", json={})
            for _ in range(500):
                metadata = json.loads(path.read_text())
                if metadata["stop_kind"] == kind:
                    break
                await asyncio.sleep(.001)
            else:
                pytest.fail("Runner announcement was not saved")
            assert metadata["abort_reason"] == reason
            ack = (await api.post(f"/api/runs/{identifier}/cancel", json={})).json()
            assert ack["stop_kind"] == kind
            assert ack["abort_reason"] == reason
            release.set()
            await assert_stop_contract(api, tmp_path, identifier, kind)
    finally:
        trigger.set()
        release.set()


async def test_web_live_export_uses_runner_finished_fields(tmp_path, client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    write = web.write_report

    def export(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return write(*args, **kwargs)

    monkeypatch.setattr(web, "write_report", export)
    try:
        async with browser(tmp_path) as api:
            identifier = await start(api, config(samples=4, concurrency=1, max_requests=1).model_dump(mode="json"))
            assert await asyncio.to_thread(entered.wait, 5)
            detail = (await api.get(f"/api/runs/{identifier}")).json()
            row = (await api.get("/api/runs")).json()["runs"][0]
            feed = (await api.get(f"/api/runs/{identifier}/events?after=0")).json()
            for _ in range(500):
                meta = json.loads((tmp_path / identifier / ".web.json").read_text())
                if meta.get("stop_kind") == "request_budget":
                    break
                await asyncio.sleep(.001)
            for surface in (detail, row, feed, meta):
                assert surface["state"] == "running"
                assert surface["stop_kind"] == "request_budget"
                assert surface["abort_reason"] == "maximum request budget reached"
            release.set()
            assert (await finished(api, identifier))["stop_kind"] == "request_budget"
    finally:
        release.set()
