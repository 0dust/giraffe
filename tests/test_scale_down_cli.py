"""Standalone file-based markers use the same run-owned controller."""
import asyncio
import json
import os
import socket
import time
from datetime import datetime, timezone

import pytest

from giraffe.scale_down import ScaleDownController


def active_directory(path):
    (path / ".web.json").write_text(json.dumps(dict(state="running", owner_pid=os.getpid(),
        owner_host=socket.gethostname(), heartbeat_at=datetime.now(timezone.utc).isoformat())))


async def test_file_submission_ack_and_rejected_slot_reuse(tmp_path):
    from giraffe.scale_down_control import ScaleDownFileControl, submit_marker
    active_directory(tmp_path)
    control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=asyncio.Event())
    adapter = ScaleDownFileControl(tmp_path)
    adapter.bind(control)
    try:
        await adapter.ready.wait()
        rejected = await asyncio.to_thread(submit_marker, tmp_path, event_id="too-early", wait_seconds=2)
        assert rejected["outcome"] == "rejected"
        assert "not armed" in rejected["acknowledgment"]["reason"]
        control.set_phase("armed")
        accepted = await asyncio.to_thread(submit_marker, tmp_path, event_id="remove", wait_seconds=2)
        assert accepted["outcome"] == "accepted"
        assert await asyncio.to_thread(submit_marker, tmp_path, event_id="remove", wait_seconds=0) == accepted
        with pytest.raises(ValueError, match="different marker"):
            await asyncio.to_thread(submit_marker, tmp_path, event_id="conflict", wait_seconds=0)
    finally:
        control.set_phase("finished")
        await adapter.close()


async def test_lost_ack_is_submitted_not_accepted(tmp_path):
    from giraffe.scale_down_control import submit_marker
    active_directory(tmp_path)
    (tmp_path / ".scale-down-status.json").write_text(json.dumps(dict(run_id="run", target="local",
        nonce="new-run", phase="armed", acknowledgment=None)))
    result = await asyncio.to_thread(submit_marker, tmp_path, event_id="remove", wait_seconds=0)
    assert result["outcome"] == "submitted_without_acknowledgment"
    assert result["acknowledgment"] is None
    assert json.loads((tmp_path / ".scale-down-event.json").read_text())["nonce"] == "new-run"


@pytest.mark.parametrize("bad_input", [b'{"event_id":', b"x" * (16 * 1024 + 1)])
async def test_malformed_file_rejected_then_corrected(tmp_path, bad_input):
    from giraffe.scale_down_control import ScaleDownFileControl, submit_marker
    active_directory(tmp_path)
    (tmp_path / ".scale-down-event.json").write_bytes(bad_input)
    control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=asyncio.Event())
    control.set_phase("armed")
    adapter = ScaleDownFileControl(tmp_path)
    adapter.bind(control)
    try:
        await adapter.ready.wait()
        status = json.loads((tmp_path / ".scale-down-status.json").read_text())
        assert not status["submission_acknowledgment"]["accepted"]
        assert not (tmp_path / ".scale-down-event.json").exists()
        assert (await asyncio.to_thread(submit_marker, tmp_path, event_id="correct", wait_seconds=2))["outcome"] == "accepted"
    finally:
        control.set_phase("finished")
        await adapter.close()


async def test_symlink_marker_cannot_read_or_modify_target(tmp_path):
    from giraffe.scale_down_control import ScaleDownFileControl
    target = tmp_path / "private.txt"
    target.write_text("secret")
    (tmp_path / ".scale-down-event.json").symlink_to(target)
    control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=asyncio.Event())
    control.set_phase("armed")
    adapter = ScaleDownFileControl(tmp_path)
    adapter.bind(control)
    try:
        await adapter.ready.wait()
        status = (tmp_path / ".scale-down-status.json").read_text()
        assert "secret" not in status
        assert not json.loads(status)["submission_acknowledgment"]["accepted"]
        assert target.read_text() == "secret"
    finally:
        control.set_phase("finished")
        await adapter.close()


@pytest.mark.parametrize("operator_at", [None, "2026-11-05T12:34:56+00:00"])
async def test_standalone_marking_command_accepts_and_inspects_same_event(tmp_path, operator_at):
    import subprocess
    import sys
    from giraffe.scale_down_control import ScaleDownFileControl
    active_directory(tmp_path)
    control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=asyncio.Event())
    control.set_phase("armed")
    adapter = ScaleDownFileControl(tmp_path)
    adapter.bind(control)
    try:
        await adapter.ready.wait()
        command = [sys.executable, "-c", "from giraffe.cli import main; raise SystemExit(main())",
                   "mark-scale-down", str(tmp_path),
                   "--event-id", "external-removal", "--replica-id", "replica-b", "--output-format", "json"]
        if operator_at is not None:
            command.extend(["--operator-at", operator_at])
        first = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10)
        assert first.returncode == 0, first.stderr
        result = json.loads(first.stdout)
        assert result["outcome"] == "accepted"
        assert result["acknowledgment"]["marker"]["replica_id"] == "replica-b"
        assert result["acknowledgment"]["marker"]["operator_at"] == operator_at
        second = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10)
        assert second.returncode == 0 and json.loads(second.stdout) == result
    finally:
        control.set_phase("finished")
        await adapter.close()


def test_cli_rejects_historical_run_without_publishing(tmp_path, capsys):
    from giraffe.cli import main
    (tmp_path / ".web.json").write_text(json.dumps(dict(state="completed")))
    (tmp_path / ".scale-down-status.json").write_text(json.dumps(dict(run_id="old", target="local",
        nonce="old-nonce", phase="finished", acknowledgment=None)))
    assert main(["mark-scale-down", str(tmp_path), "--event-id", "replay"]) == 4
    assert "not active" in capsys.readouterr().err
    assert not (tmp_path / ".scale-down-event.json").exists()


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_cli_marking_ui_run_explains_browser_control(tmp_path, capsys, output_format):
    from giraffe.cli import main
    active_directory(tmp_path)
    message = ("This run directory has no CLI scale-down control. For a run started from the UI, "
               "use Mark removal in the browser or POST /api/runs/<run-id>/scale-down/event.")
    assert main(["mark-scale-down", str(tmp_path), "--event-id", "remove",
                 "--output-format", output_format]) == 4
    output = capsys.readouterr()
    if output_format == "text":
        assert output.err == f"giraffe: {message}\n" and output.out == ""
    else:
        assert output.err == ""
        assert json.loads(output.out) == dict(kind="mark-scale-down", outcome="error", exit_code=4,
            errors=[dict(loc=[], message=message, type="ValueError")])
    assert not (tmp_path / ".scale-down-event.json").exists()


@pytest.mark.parametrize("wait", ["-1", "nan", "inf", "61"])
def test_cli_wait_is_bounded(tmp_path, wait, capsys):
    from giraffe.cli import main
    assert main(["mark-scale-down", str(tmp_path), "--event-id", "x", "--wait-seconds", wait]) == 4
    assert "wait-seconds" in capsys.readouterr().err


@pytest.mark.parametrize("cancelled", [False, True])
async def test_quiet_run_keeps_journal_and_terminal_status(tmp_path, monkeypatch, capsys, cancelled):
    from giraffe.cli import _run
    from giraffe.models import RunConfig
    from giraffe.runner import record_sink
    from tests.test_web import report as make_report
    configuration = RunConfig(targets=[dict(name="local", url="http://localhost/v1", model="test")],
                              checks=["scale_down"], scale_down={"target": "local"})
    active_directory(tmp_path)
    async def measured(config, *, progress, stop_event, on_scale_down_control):
        control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=stop_event,
            on_change=lambda status: progress(dict(event="scale_down_updated", target="local",
                scale_down_control=status, scale_down={}, scale_down_snapshots=[])))
        on_scale_down_control(control)
        control.set_phase("armed")
        result = make_report(config, cancelled=cancelled)
        record_sink.get()(result.requests[0])
        control.set_phase("finished")
        if cancelled:
            error = asyncio.CancelledError()
            error.giraffe_report = result
            raise error
        return result
    monkeypatch.setattr("giraffe.runner.run_suite", measured)
    if cancelled:
        with pytest.raises(asyncio.CancelledError) as error:
            await _run(configuration, quiet=True, directory=tmp_path)
        assert error.value.giraffe_report.abort_reason == "cancelled by user"
    else:
        await _run(configuration, quiet=True, directory=tmp_path)
    assert capsys.readouterr().err == ""
    rows = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    assert any(row.get("type") == "record" for row in rows)
    assert any(row.get("progress", {}).get("event") == "scale_down_updated" for row in rows)
    assert json.loads((tmp_path / ".scale-down-status.json").read_text())["phase"] == "finished"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("cancelled", [False, True])
def test_configured_cli_run_preserves_journal_with_report(tmp_path, monkeypatch, capsys, cancelled):
    from giraffe.cli import main
    from giraffe.runner import record_sink
    from tests.test_web import config as make_config, report as make_report
    values = make_config(checks=["scale_down"], scale_down={"target": "local"})
    configuration = tmp_path / "config.json"
    configuration.write_text(json.dumps(values))
    directory = tmp_path / "run"
    async def measured(config, *, progress, stop_event, on_scale_down_control):
        control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=stop_event,
            on_change=lambda status: progress(dict(event="scale_down_updated", target="local",
                scale_down_control=status, scale_down={}, scale_down_snapshots=[])))
        on_scale_down_control(control)
        control.set_phase("armed")
        report = make_report(config, cancelled=cancelled)
        record_sink.get()(report.requests[0])
        control.set_phase("finished")
        if cancelled:
            error = asyncio.CancelledError()
            error.giraffe_report = report
            raise error
        return report
    monkeypatch.setattr("giraffe.runner.run_suite", measured)
    result = main(["run", "--config", str(configuration), "--output", str(directory),
                   "--skip-preflight", "--quiet"])
    assert result == (130 if cancelled else 0), capsys.readouterr().err
    assert (directory / "report.json").is_file()
    assert (directory / "evidence.jsonl").is_file()
    assert json.loads((directory / ".scale-down-status.json").read_text())["phase"] == "finished"
    saved = json.loads((directory / "report.json").read_text())
    assert saved["manifest"]["config"]["scale_down"] == values["scale_down"]
    assert bool(saved.get("abort_reason")) is cancelled


async def test_same_event_can_be_retried_after_arming(tmp_path):
    from giraffe.scale_down_control import ScaleDownFileControl, submit_marker
    active_directory(tmp_path)
    control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=asyncio.Event())
    adapter = ScaleDownFileControl(tmp_path)
    adapter.bind(control)
    try:
        await adapter.ready.wait()
        first = await asyncio.to_thread(submit_marker, tmp_path, event_id="retry", wait_seconds=2)
        assert first["outcome"] == "rejected"
        control.set_phase("armed")
        second = await asyncio.to_thread(submit_marker, tmp_path, event_id="retry", wait_seconds=2)
        assert second["outcome"] == "accepted"
        assert second["acknowledgment"]["marker"] == first["acknowledgment"]["marker"]
        assert second["acknowledgment"]["received_ms"] > first["acknowledgment"]["received_ms"]
    finally:
        control.set_phase("finished")
        await adapter.close()


async def test_new_submission_does_not_return_cached_rejection(tmp_path):
    from giraffe.scale_down_control import ScaleDownFileControl, submit_marker
    active_directory(tmp_path)
    control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=asyncio.Event())
    adapter = ScaleDownFileControl(tmp_path)
    adapter.bind(control)
    try:
        await adapter.ready.wait()
        first = await asyncio.to_thread(submit_marker, tmp_path, event_id="retry", wait_seconds=2)
        assert first["outcome"] == "rejected"
    finally:
        await adapter.close()
    # A fresh file with the same marker still needs its own worker ACK.
    retry = await asyncio.to_thread(submit_marker, tmp_path, event_id="retry", wait_seconds=0)
    assert retry["outcome"] == "submitted_without_acknowledgment"
    assert retry["acknowledgment"] is None
    assert (tmp_path / ".scale-down-event.json").is_file()


@pytest.mark.usefixtures("local_tokenizer")
async def test_api_marker_preserves_same_operator_timestamp(tmp_path, monkeypatch):
    from tests.test_web import browser, config, detail_when, finished, report, start
    operator_at = "2026-11-05T12:34:56+00:00"
    async def measured(configuration, *, progress, stop_event, on_scale_down_control):
        control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=stop_event,
            on_change=lambda status: progress(dict(event="scale_down_updated", target="local",
                scale_down_control=status, scale_down={}, scale_down_snapshots=[])))
        on_scale_down_control(control)
        control.set_phase("armed")
        await stop_event.wait()
        control.set_phase("finished")
        return report(configuration)
    monkeypatch.setattr("giraffe.web.run_suite", measured)
    async with browser(tmp_path) as client:
        identifier = await start(client, config(checks=["scale_down"], scale_down={"target": "local"}))
        detail = await detail_when(client, identifier, lambda value: value.get("scale_down_control"))
        marker = dict(event_id="timestamp", nonce=detail["scale_down_control"]["nonce"],
            target="local", source="operator", operator_at=operator_at)
        path = f"/api/runs/{identifier}/scale-down/event"
        ack = (await client.post(path, json=marker)).json()
        assert ack["accepted"] and ack["marker"]["operator_at"] == operator_at
        assert (await client.post(path, json=marker)).json() == ack
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)
