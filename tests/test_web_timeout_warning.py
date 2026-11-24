"""Timeout warning state outlives activity eviction for web and CLI runs."""

import json
import os
import socket
from collections import OrderedDict

import pytest

from giraffe.evidence import EvidenceWriter
from giraffe.storage import write_metadata
from giraffe.web import _now, _track_live_progress
from tests.test_web import browser, config, detail_when, finished, report, start

pytestmark = pytest.mark.usefixtures("local_tokenizer")


def warning(index=0, timed_out=3):
    return {"key": f"local|{index + 1}|run", "target": "local",
            "deadline_seconds": index + 1, "deadline_source": "run",
            "setting": "request_timeout_seconds", "cli_flag": "--request-timeout",
            "finished": timed_out, "timed_out": timed_out, "answer_started": 0,
            "reasoning_only": 0, "no_output": timed_out, "checks": ["load"],
            "first_at_seconds": 1, "updated_at_seconds": 2, "update": timed_out - 2}


def emit(job, payload):
    _track_live_progress(job, {"event": "timeout_warning", "target": "local",
        "scenario": "load", "timeout_warning": payload}, None)


def test_warning_cache_is_bounded_and_refreshes_latest_group():
    job = {"progress": {}, "events": [], "phases": [], "conditions": {}}
    for index in range(20):
        emit(job, warning(index))
    assert isinstance(job["live_timeout_warnings"], OrderedDict)
    assert len(job["live_timeout_warnings"]) == 16
    emit(job, warning(4, 6))
    emit(job, warning(20))
    assert job["live_timeout_warnings"][warning(4)["key"]]["timed_out"] == 6
    assert warning(5)["key"] not in job["live_timeout_warnings"]


async def test_web_warning_survives_eviction_detail_events_and_restart(tmp_path, monkeypatch):
    async def waiting(configuration, *, progress, stop_event):
        progress({"event": "timeout_warning", "target": "local", "scenario": "load",
                  "timeout_warning": warning()})
        for _ in range(120):
            progress({"event": "progress", "target": "local"})
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await detail_when(client, identifier, lambda value: len(value["events"]) == 100)
        assert not any(event["event"] == "timeout_warning" for event in detail["events"])
        assert detail["timeout_warnings"] == [warning()]
        delta = (await client.get(f'/api/runs/{identifier}/events?after=0')).json()
        assert delta["timeout_warnings"] == [warning()]
        await client.post(f'/api/runs/{identifier}/cancel', json={})
        await finished(client, identifier)
    async with browser(tmp_path) as client:
        assert (await client.get(f'/api/runs/{identifier}')).json()["timeout_warnings"] == [warning()]


async def test_cli_writer_warning_reaches_live_detail_and_updates(tmp_path, monkeypatch):
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config()})
    def append(payload):
        writer = EvidenceWriter(directory)
        writer.progress({"event": "timeout_warning", "target": "local", "scenario": "load",
                         "timeout_warning": payload})
        writer.close()
    append(warning())
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        assert detail["stoppable"] is False
        assert detail["timeout_warnings"] == [warning()]
        append(warning(timed_out=6))
        detail = (await client.get('/api/runs/cli')).json()
        assert detail["timeout_warnings"] == [warning(timed_out=6)]
        delta = (await client.get('/api/runs/cli/events')).json()
        assert delta["timeout_warnings"] == detail["timeout_warnings"]


async def test_cli_journal_retries_partial_append_and_bounds_groups(tmp_path):
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config()})
    journal = directory / "evidence.jsonl"
    def line(payload):
        return json.dumps({"type": "progress", "progress": {
            "event": "timeout_warning", "timeout_warning": payload}})
    journal.write_text("\n".join(line(warning(index)) for index in range(20)) + "\n")
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        assert detail["timeout_warnings"] == [warning(index) for index in range(4, 20)]
        with journal.open("a") as stream:
            stream.write(line(warning(4, 6)))
        assert (await client.get('/api/runs/cli')).json()["timeout_warnings"] == detail["timeout_warnings"]
        with journal.open("a") as stream:
            stream.write("\n")
        latest = (await client.get('/api/runs/cli')).json()["timeout_warnings"]
        assert len(latest) == 16 and latest[-1] == warning(4, 6)


def test_writer_snapshots_timeout_warning(tmp_path):
    writer = EvidenceWriter(tmp_path)
    payload = warning()
    writer.progress({"event": "timeout_warning", "timeout_warning": payload})
    payload["checks"].append("mutated")
    payload["timed_out"] = 99
    writer.close()
    events = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    assert events[-1]["progress"]["timeout_warning"] == warning()


async def test_cli_journal_requests_are_bounded_measurements_in_detail_and_events(tmp_path, monkeypatch):
    from giraffe.models import RequestRecord

    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    monkeypatch.setattr("giraffe.web.MAX_LIVE_REQUESTS", 3)
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config()})

    def append(index):
        writer = EvidenceWriter(directory)
        writer.record_finished(RequestRecord(id=str(index), target="local", fixture_id="slow",
            scenario="warm", condition_id="serving.warm", check_ids=["serving"], started_at=_now(),
            status="timeout", request_timeout_seconds=2, termination_reason="request_deadline",
            elapsed_ms=2000, first_output_ms=10, answer_chars=7, output="private answer",
            reasoning="private reasoning"))
        writer.progress({"event": "timeout_warning", "timeout_warning": warning()})
        writer.close()

    for index in range(5):
        append(index)
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        assert detail["stoppable"] is False
        assert detail["timeout_warnings"] == [warning()]
        rows = detail["live_requests"]
        assert [row["id"] for row in rows] == ["2", "3", "4"]
        assert all(row["status"] == "timeout" and row["condition_id"] == "serving.warm" for row in rows)
        assert all(row["first_output_ms"] == 10 and row["elapsed_ms"] == 2000 for row in rows)
        assert not any(key in row for row in rows for key in ("output", "reasoning", "messages", "error"))
        assert detail["live_request_count"] == 5
        assert detail["live_request_summary"] == {"evicted": 2, "through_slot": 1, "statuses": {"timeout": 2}}
        delta = (await client.get('/api/runs/cli/events')).json()
        assert delta["records"] == rows
        assert delta["live_request_summary"] == detail["live_request_summary"]
        assert delta["live_request_count"] == 5
        append(5)
        detail = (await client.get('/api/runs/cli')).json()
        assert [row["id"] for row in detail["live_requests"]] == ["3", "4", "5"]
        assert detail["live_request_count"] == 6
        assert detail["live_request_summary"] == {"evicted": 3, "through_slot": 2, "statuses": {"timeout": 3}}
