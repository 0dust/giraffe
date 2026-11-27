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
        record = RequestRecord(id=str(index), target="local", fixture_id="slow",
            scenario="warm", condition_id="serving.warm", check_ids=["serving"], started_at=_now(),
            status="timeout", request_timeout_seconds=2, termination_reason="request_deadline",
            elapsed_ms=2000, first_output_ms=10, answer_chars=7, output="private answer",
            reasoning="private reasoning")
        record._request_slot = index
        record._budget_request_slot = index
        writer.record_finished(record)
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


async def test_cli_journal_event_cursor_sends_only_new_rows_and_resets(tmp_path, monkeypatch):
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config()})
    journal = directory / "evidence.jsonl"

    def append(start, count):
        with journal.open("a") as stream:
            for index in range(start, start + count):
                stream.write(json.dumps({"type": "record", "request_slot": index, "budget_request_slot": index, "budget_exempt": False, "record": {
                    "id": str(index), "target": "local", "scenario": "warm",
                    "condition_id": "serving.warm", "status": "timeout" if index < 3 else "completed",
                    "valid": index >= 3, "elapsed_ms": 2000, "first_output_ms": 10}}) + "\n")

    append(0, 4096)
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        assert len(detail["live_requests"]) == 4096
        cursor = detail["seq"]
        for _ in range(2):
            response = await client.get(f'/api/runs/cli/events?after={cursor}')
            idle = response.json()
            assert idle["seq"] == cursor and idle["idle"] and not idle["reset"]
            assert idle["records"] == []
            assert len(response.content) < 10_000
        append(4096, 2)
        delta = (await client.get(f'/api/runs/cli/events?after={cursor}')).json()
        assert delta["seq"] > cursor and not delta["reset"] and not delta["idle"]
        assert [row["id"] for row in delta["records"]] == ["4096", "4097"]
        current = delta["seq"]
        # Warning-only appends also advance the cursor without replaying rows.
        with journal.open("a") as stream:
            stream.write(json.dumps({"type": "progress", "progress": {
                "event": "timeout_warning", "timeout_warning": warning()}}) + "\n")
        delta = (await client.get(f'/api/runs/cli/events?after={current}')).json()
        assert delta["seq"] > current and delta["records"] == [] and not delta["reset"]
        assert delta["timeout_warnings"] == [warning()]
        current = delta["seq"]
        for after in (0, cursor - 4095, current + 1):
            reset = (await client.get(f'/api/runs/cli/events?after={after}')).json()
            assert reset["reset"] and len(reset["records"]) == 4096
            assert reset["records"][0]["id"] == "2"
        # Replacing a journal invalidates a previously valid cursor, even if the
        # replacement has exactly the same byte length and number of rows.
        replacement = directory / "replacement.jsonl"
        replacement.write_bytes(journal.read_bytes())
        replacement.replace(journal)
        reset = (await client.get(f'/api/runs/cli/events?after={current}')).json()
        assert reset["reset"] and reset["seq"] > current
        assert len(reset["records"]) == 4096
        latest = (await client.get('/api/runs/cli')).json()
        assert latest["seq"] == reset["seq"] and latest["live_requests"] == reset["records"]


@pytest.mark.parametrize("state", ["queued", "running", "stopping"])
@pytest.mark.parametrize("journal_state", ["missing", "unreadable", "symlink"])
async def test_external_run_without_readable_journal_waits_and_returns_idle(
        tmp_path, monkeypatch, state, journal_state):
    import time
    from pathlib import Path

    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", .02)
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": state, "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config(), "seq": 17})
    journal = directory / "evidence.jsonl"
    if journal_state == "unreadable":
        journal.write_text("\n")
        original_open = Path.open

        def open_path(path, *args, **kwargs):
            if path == journal:
                raise PermissionError("journal unavailable")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_path)
    elif journal_state == "symlink":
        source = tmp_path / "other.jsonl"
        source.write_text("\n")
        journal.symlink_to(source)
    async with browser(tmp_path) as client:
        begin = time.monotonic()
        events = (await client.get('/api/runs/cli/events?after=17')).json()
        assert time.monotonic() - begin >= .02
        assert events["reset"] is False
        assert events["idle"] is True
        assert events["records"] == []
        assert events["seq"] == 17


async def test_cli_live_config_comes_from_safe_journal_header(tmp_path):
    from giraffe.deployment import safe_config

    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname()})
    safe = safe_config(config(request_options={"api_key": "secret-value"}))
    writer = EvidenceWriter(directory, manifest={"config": safe})
    writer.close()
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        assert detail["config"] == safe
        assert detail["config"]["targets"][0]["name"] == "local"
        assert "secret-value" not in json.dumps(detail["config"])
        response = await client.post('/api/config/draft', json=detail["config"])
        assert response.status_code == 200, response.text
        assert (await client.get('/api/runs/cli')).json()["config"] == safe


async def test_cli_journal_preserves_out_of_order_runner_slots(tmp_path):
    from giraffe.models import RequestRecord
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config()})
    writer = EvidenceWriter(directory)
    for slot, budget_slot, exempt in ((2, 1, False), (1, 0, False), (0, None, True)):
        record = RequestRecord(id=f"request-{slot}", target="local", fixture_id="slow", scenario="warm",
            check_ids=["serving"], started_at=_now(), status="timeout")
        record._request_slot = slot
        record._budget_request_slot = budget_slot
        record._budget_exempt = exempt
        writer.record_finished(record)
    writer.close()
    lines = [json.loads(line) for line in (directory / "evidence.jsonl").read_text().splitlines()]
    assert [line["request_slot"] for line in lines if line["type"] == "record"] == [2, 1, 0]
    assert all("request_slot" not in line["record"] for line in lines if line["type"] == "record")
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        assert [(row["id"], row["slot"]) for row in detail["live_requests"]] == [("request-1", 0), ("request-2", 1)]
        assert detail["live_request_count"] == 2


async def test_cli_journal_late_timeout_counts_once_after_eviction(tmp_path):
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config()})
    journal = directory / "evidence.jsonl"

    def line(slot):
        return json.dumps({"type": "record", "request_slot": slot, "budget_request_slot": slot, "record": {
            "id": f"request-{slot}", "target": "local", "scenario": "warm",
            "status": "timeout" if slot == 0 else "completed", "valid": slot != 0}}) + "\n"

    journal.write_text(''.join(line(slot) for slot in range(1, 4098)) + line(0))
    expected = {"evicted": 2, "through_slot": 1, "statuses": {"completed": 1, "timeout": 1}}
    async with browser(tmp_path) as client:
        for _ in range(2):
            detail = (await client.get('/api/runs/cli')).json()
            assert detail["live_request_count"] == 4098
            assert len(detail["live_requests"]) == 4096
            assert detail["live_request_summary"] == expected


def test_web_runner_evicted_running_completion_and_duplicates_count_once():
    from giraffe.web import _track_live_request, MAX_LIVE_REQUESTS

    job = {"requests": OrderedDict(), "live_request_count": 0, "evicted_running": set(),
           "live_request_summary": {"evicted": 0, "through_slot": None, "statuses": {}}}

    def complete(slot, status="completed"):
        _track_live_request(job, {"event": "request_completed", "request_slot": slot, "budget_request_slot": slot,
            "request": {"id": str(slot), "status": status}}, None)

    _track_live_request(job, {"event": "request_started", "request_slot": 0, "budget_request_slot": 0}, None)
    for slot in range(1, MAX_LIVE_REQUESTS + 2):
        _track_live_request(job, {"event": "request_started", "request_slot": slot, "budget_request_slot": slot}, None)
        complete(slot)
    assert job["live_request_summary"]["evicted"] == 2
    assert job["live_request_summary"]["statuses"] == {"running": 1, "completed": 1}
    complete(0, "timeout")
    for slot in (0, 1, MAX_LIVE_REQUESTS + 1):
        complete(slot, "timeout" if slot == 0 else "completed")
    assert job["live_request_summary"] == {
        "evicted": 2, "through_slot": 1, "statuses": {"running": 0, "completed": 1, "timeout": 1}}
    assert job["evicted_running"] == set()
    assert len(job["requests"]) == MAX_LIVE_REQUESTS


async def test_cli_journal_restart_probe_and_preparation_failure_do_not_take_mosaic_slots(tmp_path):
    from giraffe.models import RequestRecord
    from giraffe.evidence import recover_evidence

    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config()})
    writer = EvidenceWriter(directory, manifest={"config": config()}, started_at=_now())
    writer.request_counts_start({})
    for raw_slot, budget_slot, scenario, sent, exemption in (
            (0, None, "readiness", True, "restart_readiness"),
            (1, None, "initial", False, None),
            (2, 0, "initial", True, None),
            (3, 1, "warm", True, None)):
        record = RequestRecord(id=f"request-{raw_slot}", target="local", fixture_id="short",
            scenario=scenario, check_ids=["serving"], started_at=_now(),
            status="completed" if sent else "failed", sent=sent)
        record._request_slot = raw_slot
        record._budget_request_slot = budget_slot
        record._budget_exempt = bool(exemption)
        record._budget_exemption = exemption
        writer.record_finished(record)
    target_counts = {"sent": 3, "budget_counted": 2, "budget_counted_unsent": 0,
        "budget_exempt": 1, "saved_observations": 0,
        "budget_exempt_by_reason": {"restart_readiness": 1}}
    writer.request_counts_snapshot({**target_counts, "available": True, "complete": True,
        "targets": {"local": target_counts}})
    writer.close()
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        assert [(row["id"], row["slot"]) for row in detail["live_requests"]] == [("request-2", 0), ("request-3", 1)]
        assert detail["live_request_count"] == 2
        replay = (await client.get('/api/runs/cli/events')).json()
        assert [row["slot"] for row in replay["records"]] == [0, 1]
    recovered = recover_evidence(directory, {"config": config(), "started_at": _now()})
    assert recovered is not None
    failure = next(record for record in recovered.requests if record.id == "request-1")
    assert failure.sent is False
    from giraffe.reporting import write_report
    write_report(recovered, directory)
    saved = json.loads((directory / "report.json").read_text())
    assert next(record for record in saved["requests"] if record["id"] == "request-1")["sent"] is False
    counts = recovered.observations["request_counts"]
    assert counts["sent"] == 3 and counts["budget_exempt"] == 1
    assert counts["budget_exempt_by_reason"] == {"restart_readiness": 1}


async def test_cli_journal_replays_real_restart_readiness_dispatch_slots(tmp_path, monkeypatch):
    import httpx
    from giraffe.client import LLMClient
    from giraffe.runner import _Run, record_sink, request_counts_sink
    from tests.test_request_counts import configuration, request_spec

    configuration = configuration()
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(),
        "config": configuration.model_dump(mode="json")})
    original = httpx.AsyncClient

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(lambda request: httpx.Response(200,
            json={"choices": [{"message": {"content": "42"}, "finish_reason": "stop"}]})))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    writer = EvidenceWriter(directory, manifest={"config": configuration.model_dump(mode="json")})
    writer.request_counts_start({})
    token = record_sink.set(writer.record_finished)
    counts_token = request_counts_sink.set(writer.request_counts_snapshot)
    try:
        run = _Run(configuration, writer.progress, None)
        target = configuration.targets[0]
        async with LLMClient(target, configuration) as transport:
            await run.request(target, transport, request_spec("readiness"),
                              budget_exemption="restart_readiness")
            await run.request(target, transport, request_spec("initial"))
            await run.request(target, transport, request_spec("warm"))
    finally:
        record_sink.reset(token)
        request_counts_sink.reset(counts_token)
        writer.close()
    async with browser(tmp_path) as session:
        detail = (await session.get('/api/runs/cli')).json()
        assert [record["slot"] for record in detail["live_requests"]] == list(range(run.budget_used))
        assert [record["scenario"] for record in detail["live_requests"]] == ["initial", "warm"]
        assert detail["live_request_count"] == 2


async def test_cli_journal_progress_counters_reach_detail_events_and_live_heading(tmp_path, monkeypatch):
    from tests.test_ux_results import run_render_checks

    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    directory = tmp_path / "cli"
    write_metadata(directory, {"id": "cli", "state": "running", "heartbeat_at": _now(),
        "owner_pid": os.getpid(), "owner_host": socket.gethostname(), "config": config(),
        "progress": {"budget_used": 0, "sent": 0, "budget_exempt": 0, "completed": 0}})
    target = {"sent": 2, "budget_counted": 1, "budget_counted_unsent": 0, "budget_exempt": 1,
              "saved_observations": 1, "budget_exempt_by_reason": {"restart_readiness": 1}}
    counts = {**target, "available": True, "complete": True, "targets": {"local": target}}
    writer = EvidenceWriter(directory)
    writer.request_counts_snapshot(counts)
    writer.progress({"event": "request_completed", "budget_used": 1, "sent": 2,
        "budget_exempt": 1, "completed": 2, "attempted": 2, "request_counts": counts,
        "request": {"output": "private answer"}, "condition": {"id": "stale"},
        "condition_summary": {"private": "condition payload"}})
    writer.close()
    async with browser(tmp_path) as client:
        detail = (await client.get('/api/runs/cli')).json()
        for progress in (detail["progress"], (await client.get('/api/runs/cli/events')).json()["progress"]):
            assert progress == {"budget_used": 1, "sent": 2, "budget_exempt": 1,
                                "completed": 2, "attempted": 2, "request_counts": counts}
        assert detail["live_requests"] == []
        cursor = detail["seq"]
        idle = (await client.get(f'/api/runs/cli/events?after={cursor}')).json()
        assert idle["idle"] and idle["records"] == [] and idle["progress"] == detail["progress"]
        writer = EvidenceWriter(directory)
        writer.progress({"event": "timeout_warning", "timeout_warning": warning()})
        writer.close()
        delta = (await client.get(f'/api/runs/cli/events?after={cursor}')).json()
        assert delta["progress"] == detail["progress"] and delta["timeout_warnings"] == [warning()]
    detail["request_plan"] = {"planned_total": 2}
    run_render_checks("state.detail=" + json.dumps(detail) + ";renderLive();"
        "assert.match($('.live-progress').textContent,/1 of ~2 planned requests/);"
        "assert.match($('.live-progress').textContent,/1 exempt/);", dom=True)
