import asyncio
import errno
import json
import os
import socket
import threading

import pytest

from giraffe.cli import main, parser
from giraffe.models import RunReport


def report(overall="pass"):
    return RunReport(run_id="saved-id", started_at="2020-01-01T00:00:00Z",
                     finished_at="2020-01-01T00:00:01Z", overall=overall, manifest={},
                     observations={"resources": {"sampled_peak_rss_bytes": {"traffic": 12, "scoring": 13}}})


def run_arguments(directory):
    return ["run", "--url", "http://localhost", "--model", "tiny", "--skip-preflight",
            "--output", str(directory), "--max-total-storage", "0", "--min-free-disk", "0",
            "--output-format", "json"]


def test_cli_symlinked_runs_root_for_run_storage_and_cleanup(tmp_path, monkeypatch, capsys):
    root = tmp_path / "runs"
    root.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)

    async def run(config, **kwargs):
        return report()

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(run_arguments(linked / "one")) == 0
    capsys.readouterr()
    assert main(["storage", "--runs-dir", str(linked), "--output-format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["id"] == "one"
    assert main(["cleanup", "--runs-dir", str(linked), "--older-than", "0"]) == 0
    assert (root / "one").exists() and "No runs deleted" in capsys.readouterr().out
    assert main(["cleanup", "--runs-dir", str(linked), "--older-than", "0", "--yes"]) == 0
    assert not (root / "one").exists()


@pytest.mark.parametrize("value", ["0", "0MB", "-1"])
def test_cleanup_max_total_requires_positive_size_without_deleting(tmp_path, capsys, value):
    directory = tmp_path / "one"
    directory.mkdir()
    (directory / "report.json").write_text(report().model_dump_json())
    assert main(["cleanup", "--runs-dir", str(tmp_path), "--max-total", value, "--yes"]) == 4
    assert "--older-than 0" in capsys.readouterr().err
    assert directory.exists()


def test_storage_command_reports_user_configured_limits(tmp_path, capsys):
    assert main(["storage", "--runs-dir", str(tmp_path), "--output-format", "json",
                 "--max-total-storage", "2GiB", "--min-free-disk", "0", "--max-run-storage", "3MB"]) == 0
    assert json.loads(capsys.readouterr().out)["limits"] == {
        "max_total_storage": 2 * 1024 ** 3, "min_free_disk": 0, "max_run_storage": 3000000}
    assert "protected runs count toward N" in parser()._subparsers._group_actions[0].choices["cleanup"].format_help()


def test_cli_heartbeat_refreshes_during_traffic_and_export_then_stops(tmp_path, monkeypatch, capsys):
    import giraffe.cli
    import giraffe.reporting
    from giraffe.storage import cleanup_plan

    directory = tmp_path / "one"
    path = directory / ".web.json"
    original_utime, original_write = os.utime, giraffe.reporting.write_report
    refreshed = threading.Event()
    calls = []
    monkeypatch.setattr(giraffe.cli, "_HEARTBEAT_SECONDS", 0.005)

    def touch(filename, times, **kwargs):
        original_utime(filename, times, **kwargs)
        if filename == path and times is None:
            calls.append(1)
            refreshed.set()

    monkeypatch.setattr(giraffe.cli.os, "utime", touch)

    def wait_for_heartbeat():
        refreshed.clear()
        original_utime(path, (1, 1))
        assert refreshed.wait(1)
        assert path.stat().st_mtime > 1
        metadata = json.loads(path.read_text())
        assert metadata["owner_host"] == socket.gethostname()
        assert metadata["owner_pid"] == os.getpid() and metadata["state"] == "running"
        assert not cleanup_plan(tmp_path, older_than_days=0)["candidates"]

    async def run(config, **kwargs):
        await asyncio.to_thread(wait_for_heartbeat)
        return report()

    def write(value, directory, **kwargs):
        wait_for_heartbeat()
        return original_write(value, directory, **kwargs)

    monkeypatch.setattr(giraffe.cli, "_run", run)
    monkeypatch.setattr(giraffe.reporting, "write_report", write)
    assert main(run_arguments(directory)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["resources"]["sampled_peak_rss_bytes"]["traffic"] == 12
    assert "export" in output["resources"]["sampled_peak_rss_bytes"]
    metadata = json.loads(path.read_text())
    assert metadata["state"] == "completed" and metadata["run_id"] == "saved-id"
    count = len(calls)
    assert count >= 2
    refreshed.clear()
    assert not refreshed.wait(0.03) and len(calls) == count


@pytest.mark.parametrize("overall, expected_code", [("pass", 0), ("fail", 1), ("inconclusive", 2)])
def test_cli_html_only_write_failure_keeps_json_and_original_verdict(tmp_path, monkeypatch, capsys, overall, expected_code):
    import giraffe.reporting

    directory = tmp_path / "one"
    original = giraffe.reporting._atomic_write

    async def run(config, **kwargs):
        return report(overall)

    def fail_html(path, contents):
        if path.name == "report.html":
            path.write_text("partial")
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(path, contents)

    monkeypatch.setattr("giraffe.cli._run", run)
    monkeypatch.setattr(giraffe.reporting, "_atomic_write", fail_html)
    assert main(run_arguments(directory) + ["--html"]) == expected_code
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == overall and output["report_paths"]["html"] is None
    assert any("HTML report could not be saved: disk full; report.json is complete" in w for w in output["warnings"])
    assert json.loads((directory / "report.json").read_text())["overall"] == overall
    metadata = json.loads((directory / ".web.json").read_text())
    assert metadata["state"] == "completed" and "report.json is complete" in metadata["error"]
    assert not (directory / "report.html").exists() and not (directory / "summary.json").exists()
    assert "export" in metadata["resources"]["sampled_peak_rss_bytes"]


def test_cli_metadata_write_failure_keeps_completed_verdict_and_json(tmp_path, monkeypatch, capsys):
    import giraffe.reporting

    directory = tmp_path / "one"
    original = giraffe.reporting._atomic_write

    async def run(config, **kwargs):
        return report()

    def fail_terminal_metadata(path, contents):
        if path.name == ".web.json" and json.loads(contents)["state"] == "completed":
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(path, contents)

    monkeypatch.setattr("giraffe.cli._run", run)
    monkeypatch.setattr(giraffe.reporting, "_atomic_write", fail_terminal_metadata)
    assert main(run_arguments(directory)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "pass" and any("metadata could not be saved" in w for w in output["warnings"])
    assert (directory / "report.json").exists()


def test_cli_heartbeat_does_not_touch_symlink_target(tmp_path, monkeypatch):
    from giraffe.cli import _heartbeat

    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    os.utime(target, (1, 1))
    marker = tmp_path / ".web.json"
    marker.symlink_to(target)
    touched = threading.Event()
    original_utime = os.utime

    def touch(path, times, **kwargs):
        original_utime(path, times, **kwargs)
        if path == marker:
            touched.set()

    monkeypatch.setattr("giraffe.cli.os.utime", touch)
    monkeypatch.setattr("giraffe.cli._HEARTBEAT_SECONDS", 0.005)
    with _heartbeat(marker):
        assert touched.wait(1)
    assert target.stat().st_mtime == 1 and target.read_text() == "unchanged"
