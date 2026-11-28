import json
from datetime import datetime
import threading
import time

import pytest

from giraffe import states
from giraffe.cli import main
from giraffe.evidence import EvidenceWriter
from giraffe.storage import update_metadata, write_metadata
from tests.test_evidence import record
from tests.test_web import browser, finished, report, start


@pytest.mark.parametrize("overrides,expected", [
    ({}, "pending"),
    ({"journal_present": False}, "none"),
    ({"report_available": True}, "none"),
    ({"report_available": True, "attempts": 5}, "none"),
    ({"summary_present": True, "attempts": 5}, "none"),
    ({"released": True, "attempts": 5}, "none"),
    *[({"attempts": attempt}, "retrying") for attempt in range(1, 5)],
    ({"attempts": 5}, "retrying"),
    ({"attempts": 6}, "retrying"),
    ({"state": "error", "attempts": 5}, "given_up"),
    ({"state": "error"}, "none"),
    ({"state": "error", "attempts": 4}, "none"),
    *[({"state": state, "attempts": 5}, "none") for state in
      ("queued", "running", "stopping", "completed", None)],
])
def test_recovery_derivation(overrides, expected):
    inputs = {"state": "interrupted", "journal_present": True, "report_available": False} | overrides
    assert states.recovery_state(**inputs) == expected
    assert states.run_recoverable(**inputs) is (expected in {"pending", "retrying"})


async def test_cli_recovers_given_up_run_under_strict_transitions(tmp_path, capsys):
    directory = tmp_path / "given-up"
    write_metadata(directory, {"id": directory.name, "state": "error",
        "heartbeat_at": "2000-01-01T00:00:00+00:00", "recovery_attempts": 5,
        "recovery_retry_at": 9999999999, "error": "Recovery failed: OSError (attempt 5/5). [error recovery-id]",
        "error_id": "recovery-id"})
    writer = EvidenceWriter(directory, {"config": {"retention": "all"}})
    writer.record_finished(record())
    writer.close()
    assert states.STRICT_TRANSITIONS is True
    assert main(["report", str(directory), "--summary-md", str(tmp_path / "summary.md")]) == 0
    meta = json.loads((directory / ".web.json").read_text())
    assert meta["state"] == "interrupted"
    assert not {"error", "error_id", "recovery_attempts", "recovery_retry_at"} & meta.keys()
    async with browser(tmp_path) as client:
        detail = (await client.get("/api/runs/given-up")).json()
        assert detail["recovery"]["state"] == "none"
        assert detail["recovery"]["attempts"] == 0
        assert detail["recovery"]["retry_at"] is None
        assert detail["error"] is None and detail["error_id"] is None
    assert main(["runs", "list", "--runs-dir", str(tmp_path), "--output-format", "json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["runs"][0]["recovery"] == detail["recovery"]


def interrupted_journal(root, *, state="interrupted", attempts=0):
    directory = root / "saved"
    write_metadata(directory, {"id": directory.name, "state": state,
        "heartbeat_at": "2000-01-01T00:00:00+00:00", "recovery_attempts": attempts})
    writer = EvidenceWriter(directory, {"config": {"retention": "all"}})
    writer.record_finished(record())
    writer.close()
    return directory


async def recovery_responses(client):
    detail = (await client.get("/api/runs/saved")).json()
    history = (await client.get("/api/runs")).json()["runs"][0]
    events = (await client.get("/api/runs/saved/events")).json()
    assert detail["recovery"] == history["recovery"] == events["recovery"]
    assert detail["recoverable"] == history["recoverable"] == events["recoverable"]
    return detail


async def test_server_exposes_retry_backoff_and_gives_up_after_five_failures(tmp_path, monkeypatch):
    from giraffe import web

    directory = interrupted_journal(tmp_path)
    evidence = (directory / "evidence.jsonl").read_bytes()
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise OSError("could not recover")

    monkeypatch.setattr(web, "recover_evidence", fail)
    for attempt in range(1, 6):
        before = time.time()
        async with browser(tmp_path) as client:
            detail = await recovery_responses(client)
            recovery = detail["recovery"]
            assert recovery["state"] == ("retrying" if attempt < 5 else "given_up")
            assert recovery["attempts"] == attempt and recovery["max_attempts"] == 5
            assert detail["state"] == ("interrupted" if attempt < 5 else "error")
            assert detail["recoverable"] is (attempt < 5) and detail["terminal"] is True
            assert f"(attempt {attempt}/5). [error {detail['error_id']}]" in detail["error"]
            if attempt < 5:
                retry_at = datetime.fromisoformat(recovery["retry_at"])
                assert retry_at.utcoffset().total_seconds() == 0
                assert retry_at.timestamp() >= before + web._RECOVERY_SECONDS * 2 ** (attempt - 1)
            else:
                assert recovery["retry_at"] is None
        # Make the existing backoff eligible for the next startup, without waiting in this test.
        meta = json.loads((directory / ".web.json").read_text())
        meta["recovery_retry_at"] = 0
        write_metadata(directory, meta)
    async with browser(tmp_path) as client:
        assert (await recovery_responses(client))["recovery"]["state"] == "given_up"
    assert len(attempts) == 5
    assert (directory / "evidence.jsonl").read_bytes() == evidence


@pytest.mark.parametrize("saved_report", [False, True])
@pytest.mark.parametrize("attempts,error,expected_error_id", [
    (4, "Recovery failed: OSError (attempt 4/5). [error old-error-id]", None),
    (4, "Recovered report with a stale recovery error ID", None),
    (0, "Other error", "old-error-id"),
])
async def test_server_success_clears_stale_recovery_attempts(tmp_path, saved_report, attempts, error, expected_error_id):
    from giraffe.evidence import recover_evidence
    from giraffe.reporting import write_report

    directory = interrupted_journal(tmp_path, attempts=attempts)
    update_metadata(directory, {"recovery_retry_at": 0, "error_id": "old-error-id", "error": error})
    if saved_report:
        write_report(recover_evidence(directory, {}), directory, preserve_evidence=True)
    async with browser(tmp_path) as client:
        detail = await recovery_responses(client)
        assert detail["recovery"] == {"state": "none", "attempts": 0, "max_attempts": 5, "retry_at": None}
        assert detail["recoverable"] is False and detail["report"] is not None
        assert detail["error_id"] == expected_error_id
        assert not (detail["error"] or "").startswith("Recovery failed:")
    assert (directory / "report.json").is_file()
    meta = json.loads((directory / ".web.json").read_text())
    assert not {"recovery_attempts", "recovery_retry_at"} & meta.keys()
    assert meta.get("error_id") == expected_error_id


@pytest.mark.usefixtures("local_tokenizer")
async def test_active_run_with_stale_heartbeat_has_no_recovery(tmp_path, monkeypatch):
    import asyncio

    entered, release = threading.Event(), threading.Event()

    async def held_runner(configuration, *, progress, stop_event):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", held_runner)
    monkeypatch.setattr("giraffe.web.Heartbeat.start", lambda self: self)
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            update_metadata(tmp_path / identifier, {"heartbeat_at": "2000-01-01T00:00:00+00:00"})
            detail = (await client.get(f"/api/runs/{identifier}")).json()
            history = (await client.get("/api/runs")).json()["runs"][0]
            for result in (detail, history,
                           (await client.get(f"/api/runs/{identifier}/events")).json(),
                           (await client.get(f"/api/runs/{identifier}/events?after=999999")).json()):
                assert result["state"] == "running"
                assert result["recovery"]["state"] == "none"
                assert result["recoverable"] is False
        finally:
            release.set()
        assert (await finished(client, identifier))["state"] == "completed"


@pytest.mark.parametrize("field,value,expected_state,expected_attempts", [
    *[("recovery_attempts", value, "pending", 0) for value in
      (None, True, False, "3", 2.5, {}, [], -1)],
    *[("recovery_retry_at", value, "retrying", 1) for value in
      (None, True, "1700000000", {}, [], 10 ** 1000, 1e100, float("inf"), float("nan"), -1e100)],
])
async def test_malformed_recovery_metadata_does_not_break_readers(
        tmp_path, capsys, field, value, expected_state, expected_attempts):
    async with browser(tmp_path) as client:
        directory = interrupted_journal(tmp_path, attempts=1)
        update_metadata(directory, {field: value})
        detail = await recovery_responses(client)
        assert detail["recovery"] == {"state": expected_state, "attempts": expected_attempts,
            "max_attempts": 5, "retry_at": None}
        assert type(detail["recovery"]["attempts"]) is int
        storage = (await client.get("/api/storage")).json()["runs"][0]
        assert storage["recovery"] == detail["recovery"]
        assert main(["runs", "list", "--runs-dir", str(tmp_path), "--output-format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["runs"][0]["recovery"] == detail["recovery"]


@pytest.mark.parametrize("attempts,expected", [(0, "retrying"), (4, "given_up")])
async def test_retry_state_uses_unsaved_metadata_cache(tmp_path, monkeypatch, attempts, expected):
    from giraffe import web

    directory = interrupted_journal(tmp_path, attempts=attempts)

    def fail_recovery(*args, **kwargs):
        raise OSError("recovery failed")

    def fail_metadata(*args, **kwargs):
        raise OSError("metadata write failed")

    monkeypatch.setattr(web, "recover_evidence", fail_recovery)
    monkeypatch.setattr(web, "_write", fail_metadata)
    async with browser(tmp_path) as client:
        detail = await recovery_responses(client)
        assert detail["recovery"]["state"] == expected
        assert detail["recovery"]["attempts"] == attempts + 1
        storage = (await client.get("/api/storage")).json()["runs"][0]
        assert storage["recovery"] == detail["recovery"]
        if expected == "retrying":
            assert datetime.fromisoformat(detail["recovery"]["retry_at"]).timestamp() > time.time()
        else:
            assert detail["recovery"]["retry_at"] is None
        assert json.loads((directory / ".web.json").read_text())["recovery_attempts"] == attempts


@pytest.mark.parametrize("state", ["running", "stopping"])
async def test_dead_writer_reads_pending_before_next_recovery_pass(tmp_path, monkeypatch, state):
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", 0)
    async with browser(tmp_path) as client:
        interrupted_journal(tmp_path, state=state)
        detail = await recovery_responses(client)
        assert detail["state"] == state and detail["terminal"] is False
        assert detail["recovery"]["state"] == "pending" and detail["recoverable"] is True
        events = (await client.get("/api/runs/saved/events")).json()
        # Main's journal feed reports unseen journal rows, so the first poll is not idle.
        assert events["recovery"]["state"] == "pending" and events["recoverable"] is True


@pytest.mark.parametrize("attempts,expected", [(0, "pending"), (1, "retrying"), (4, "retrying"), (5, "given_up")])
def test_runs_list_recovery_and_release(tmp_path, capsys, attempts, expected):
    from giraffe.storage import set_kept

    directory = interrupted_journal(tmp_path, state="error" if attempts == 5 else "interrupted", attempts=attempts)
    args = ["runs", "list", "--runs-dir", str(tmp_path), "--output-format", "json"]
    assert main(args) == 0
    row = json.loads(capsys.readouterr().out)["runs"][0]
    assert row["recovery"]["state"] == expected
    assert row["recovery"]["attempts"] == attempts
    assert row["protected_reason"] == "unrecovered request evidence"
    set_kept(directory, False)
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["recovery"]["state"] == "none"


async def test_released_journal_reads_none_until_evidence_changes(tmp_path):
    from giraffe.storage import set_kept

    async with browser(tmp_path) as client:
        directory = interrupted_journal(tmp_path)
        set_kept(directory, False)
        assert (await recovery_responses(client))["recovery"]["state"] == "none"
        with (directory / "evidence.jsonl").open("a") as stream:
            stream.write("\n")
        assert (await recovery_responses(client))["recovery"]["state"] == "pending"
