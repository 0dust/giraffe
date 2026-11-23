"""Timeout warnings bypass stage throttling without overwhelming CLI progress."""

import json

import pytest

from giraffe.cli import _run
from giraffe.models import RunConfig, RunReport


def warning(count, *, key="local|30.0|run"):
    return {
        "event": "timeout_warning", "target": "local", "scenario": "generation",
        "timeout_warning": {
            "key": key, "target": "local", "deadline_seconds": 30.0,
            "deadline_source": "run", "setting": "request_timeout_seconds",
            "cli_flag": "--request-timeout", "finished": count + 2,
            "timed_out": count, "answer_started": 0, "reasoning_only": 0,
            "no_output": count, "checks": ["generation"],
            "first_at_seconds": 1, "updated_at_seconds": count, "update": count - 2,
        },
    }


async def emit_events(monkeypatch, events, *, final_payloads=None, **options):
    async def run(config, *, progress, stop_event):
        for event in events:
            progress(event)
        return RunReport(run_id="timeouts", started_at="now", finished_at="now", overall="pass", manifest={},
                         observations={"targets": {"local": {"timeout_warnings": final_payloads or []}}})

    monkeypatch.setattr("giraffe.runner.run_suite", run)
    await _run(RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}]), **options)


async def test_timeout_progress_bypasses_throttle_and_only_reprints_at_doubling(monkeypatch, capsys):
    monkeypatch.setattr("giraffe.cli.time.monotonic", lambda: 10.0)
    events = [{"event": "request_completed", "target": "local", "scenario": "generation"}]
    events += [warning(count) for count in [3, 4, 5, 6, 7, 11, 12]]
    events += [warning(3, key="other|30.0|run")]
    await emit_events(monkeypatch, events)
    output = capsys.readouterr().err
    lines = [line for line in output.splitlines() if line.startswith("Warning: ")]
    assert len(lines) == 4
    assert [line.split(" requests hit")[0].rsplit(": ", 1)[-1] for line in lines] == [
        "3 of 5", "6 of 8", "12 of 14", "3 of 5",
    ]
    assert "--request-timeout (request_timeout_seconds)" in lines[0]
    assert output.count("  Press Ctrl+C to stop; completed results are kept.") == 1


@pytest.mark.parametrize("options", [{"quiet": True}, {"quiet": True, "json_progress": True}])
async def test_timeout_progress_quiet_has_no_live_output(monkeypatch, capsys, options):
    await emit_events(monkeypatch, [warning(3), warning(6)], **options)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


async def test_timeout_progress_json_is_unchanged(monkeypatch, capsys):
    events = [warning(3), warning(4), warning(6)]
    await emit_events(monkeypatch, events, json_progress=True)
    assert [json.loads(line) for line in capsys.readouterr().err.splitlines()] == events


async def test_final_timeout_counts_print_after_live_doubling(monkeypatch, capsys):
    await emit_events(monkeypatch, [warning(count) for count in [3, 4, 5, 6, 7]])
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("Warning: ")]
    assert [line.split(" requests hit")[0].rsplit(": ", 1)[-1] for line in lines] == [
        "3 of 5", "6 of 8", "7 of 9"]


async def test_final_warning_updates_finished_count_and_does_not_repeat_identical_line(monkeypatch, capsys):
    events = [warning(3), warning(6)]
    changed = warning(6)
    changed["timeout_warning"]["finished"] = 12
    await emit_events(monkeypatch, events + [changed])
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("Warning: ")]
    assert len(lines) == 3 and "6 of 12 requests" in lines[-1]
    await emit_events(monkeypatch, events)
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("Warning: ")]
    assert len(lines) == 2


async def test_final_warning_uses_latest_report_counts(monkeypatch, capsys):
    await emit_events(monkeypatch, [warning(3), warning(6)], final_payloads=[warning(7)["timeout_warning"]])
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("Warning: ")]
    assert len(lines) == 3 and "7 of 9 requests" in lines[-1]


@pytest.mark.parametrize("options", [{"quiet": True}, {"json_progress": True}])
async def test_final_report_warnings_stay_silent_in_quiet_and_json(monkeypatch, capsys, options):
    await emit_events(monkeypatch, [], final_payloads=[warning(7)["timeout_warning"]], **options)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
