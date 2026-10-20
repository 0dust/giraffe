import errno
import json
from collections import namedtuple

import pytest

from giraffe.cli import main, parser
from giraffe.models import CheckResult, RequestRecord, RunReport
from giraffe.storage import DEFAULT_LIMITS


def saved(root, name="old"):
    directory = root / name
    directory.mkdir()
    report = RunReport(run_id=name, started_at="2020-01-01T00:00:00Z", finished_at="2020-01-01T00:00:01Z",
                       overall="pass", manifest={})
    (directory / "report.json").write_text(report.model_dump_json())
    return directory


def test_storage_prints_json_and_disk_totals(tmp_path, capsys):
    directory = saved(tmp_path)
    assert main(["storage", "--runs-dir", str(tmp_path), "--output-format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["runs_bytes"] == (directory / "report.json").stat().st_size
    assert result["runs"][0]["id"] == "old"
    assert result["limits"] == DEFAULT_LIMITS
    assert main(["storage", "--runs-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Saved runs on disk:" in text and "Baselines on disk:" in text and "Free disk:" in text
    assert "old:" in text and "max_run_storage:" in text


def test_cleanup_previews_and_only_yes_deletes_with_keep_protection(tmp_path, capsys):
    old, kept = saved(tmp_path), saved(tmp_path, "kept")
    assert main(["keep", str(kept)]) == 0
    assert (kept / ".keep").exists()
    arguments = ["cleanup", "--runs-dir", str(tmp_path), "--keep-latest", "0"]
    assert main(arguments) == 0
    assert old.exists() and kept.exists()
    text = capsys.readouterr().out
    assert "No runs deleted" in text and "Protected kept" in text
    assert main(arguments + ["--yes"]) == 0
    assert not old.exists() and kept.exists()
    assert "Deleted 1 run" in capsys.readouterr().out
    assert main(["keep", str(kept), "--off"]) == 0
    assert not (kept / ".keep").exists()


@pytest.mark.parametrize("count", [0, 1, 3])
def test_cleanup_pluralizes_counts_and_newest_reason(tmp_path, capsys, count):
    for index in range(count):
        saved(tmp_path, str(index))
    assert main(["cleanup", "--runs-dir", str(tmp_path), "--keep-latest", "1", "--older-than", "0", "--yes"]) == 0
    text = capsys.readouterr().out
    noun = "run" if count == 1 else "runs"
    assert f"Cleanup preview: {count} {noun}," in text
    assert f"Deleted {count} {noun}, freed" in text
    if count > 1:
        assert "Beyond the newest run" in text
        assert "newest 1 runs" not in text
    assert "on disk" in text


def test_cleanup_reports_newly_kept_skipped_run_and_actual_freed_bytes(tmp_path, monkeypatch, capsys):
    import giraffe.storage

    kept, deleted = saved(tmp_path, "newly-kept"), saved(tmp_path, "deleted")
    size = (deleted / "report.json").stat().st_size
    original = giraffe.storage.apply_cleanup

    def protect_before_apply(root, ids, **criteria):
        giraffe.storage.set_kept(kept, True)
        return original(root, ids, **criteria)

    monkeypatch.setattr(giraffe.storage, "apply_cleanup", protect_before_apply)
    assert main(["cleanup", "--runs-dir", str(tmp_path), "--older-than", "0", "--yes"]) == 0
    text = capsys.readouterr().out
    assert f"Deleted 1 run, freed {size} B on disk" in text
    assert "Skipped 1:" in text
    assert "newly-kept — kept run" in text
    assert kept.exists() and not deleted.exists()


def test_storage_text_uses_binary_sizes_and_exact_default_limits(tmp_path, monkeypatch, capsys):
    import giraffe.storage

    monkeypatch.setattr(giraffe.storage, "storage_summary", lambda *args, **kwargs: {
        "runs_bytes": 1024, "baselines_bytes": 2 * 1024**2, "free_bytes": 3 * 1024**4,
        "baselines_shared_bytes": 0, "auto_cleanup": {}, "limits": DEFAULT_LIMITS, "runs": [{"id": "one", "bytes": 1536, "protected": False}]})
    assert main(["storage", "--runs-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    for expected in ("Saved runs on disk: 1 KiB", "Baselines on disk: 2 MiB", "Free disk: 3 TiB",
                     "max_total_storage: 5 GiB", "max_run_storage: 256 MiB", "one: 1.5 KiB on disk"):
        assert expected in text


@pytest.mark.parametrize("arguments", [
    ["cleanup", "--older-than", "-1"], ["cleanup", "--keep-latest", "-1"],
    ["cleanup", "--max-total", "-1"], ["run", "--max-run-storage", "-1"],
])
def test_storage_arguments_reject_negative_values(arguments, capsys):
    assert main(arguments) == 4
    assert "giraffe:" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "cold-start", "try-example", "ui", "web"])
def test_all_exporting_commands_offer_storage_limits(command):
    arguments = [command, "--max-total-storage", "2GiB", "--min-free-disk", "0", "--max-run-storage", "500MB"]
    if command in {"cold-start", "try-example"}:
        arguments += ["--restart", "local"] if command == "cold-start" else [
            "--config", "unused", "--case", "one", "--target", "local"]
    args = parser().parse_args(arguments)
    assert args.max_total_storage == 2 * 1024 ** 3
    assert args.min_free_disk == 0 and args.max_run_storage == 500000000


@pytest.mark.parametrize("constraint", ["total", "free"])
def test_storage_start_limits_refuse_before_network(tmp_path, monkeypatch, capsys, constraint):
    root = tmp_path / "runs"
    root.mkdir()
    saved(root)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("giraffe.storage.shutil.disk_usage", lambda *a: usage(1000, 999, 1))
    monkeypatch.setattr("giraffe.preflight.check_target", lambda *a: pytest.fail("Preflight started"))
    monkeypatch.setattr("giraffe.cli._run", lambda *a: pytest.fail("Run started"))
    flags = ["--max-total-storage", "1", "--min-free-disk", "0"] if constraint == "total" else [
        "--max-total-storage", "0", "--min-free-disk", "10"]
    assert main(["run", "--url", "http://localhost", "--model", "tiny", "--output", str(root / "new"),
                 *flags]) == 4
    error = capsys.readouterr().err
    assert "cleanup" in error and "bytes" in error
    assert not (root / "new").exists()


def test_disk_full_export_saves_summary_and_error_metadata(tmp_path, monkeypatch, capsys):
    async def run(config, **kwargs):
        return RunReport(run_id="full", started_at="now", finished_at="now", overall="pass", manifest={},
                         checks=[CheckResult(id="correctness", target="local", title="Correctness", status="pass", summary="All good")],
                         requests=[RequestRecord(id="one", target="local", fixture_id="f", scenario="correctness",
                                                 check_ids=["correctness"], started_at="now", status="completed")])

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("giraffe.cli._run", run)
    monkeypatch.setattr("giraffe.reporting.write_report", disk_full)
    directory = tmp_path / "failed"
    assert main(["run", "--url", "http://localhost", "--model", "tiny", "--skip-preflight",
                 "--output", str(directory), "--min-free-disk", "0", "--output-format", "json"]) == 4
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "error" and "disk full" in output["message"]
    assert "1 requests was lost" in output["message"]
    assert json.loads((directory / ".web.json").read_text())["state"] == "error"
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["overall"] == "pass" and summary["checks"][0]["summary"] == "All good"
    assert not (directory / "report.json").exists()


def test_cli_marks_running_during_export_and_forwards_per_run_limit(tmp_path, monkeypatch, capsys):
    import giraffe.reporting
    from giraffe.storage import cleanup_plan

    directory = tmp_path / "run"
    original = giraffe.reporting.write_report

    async def run(config, **kwargs):
        meta = json.loads((directory / ".web.json").read_text())
        assert set(meta) == {"id", "state", "started_at", "owner_pid", "owner_host"} and meta["state"] == "running"
        assert meta["owner_pid"] > 0
        return RunReport(run_id="done", started_at="now", finished_at="now", overall="pass", manifest={})

    def export(report, path, **kwargs):
        assert kwargs == {"max_run_storage": 7, "html": False}
        plan = cleanup_plan(tmp_path, keep_latest=0)
        assert not plan["candidates"] and plan["protected"][0]["id"] == "run"
        return original(report, path, **kwargs)

    monkeypatch.setattr("giraffe.cli._run", run)
    monkeypatch.setattr(giraffe.reporting, "write_report", export)
    assert main(["run", "--url", "http://localhost", "--model", "tiny", "--skip-preflight",
                 "--output", str(directory), "--min-free-disk", "0", "--max-run-storage", "7"]) == 0
    assert json.loads((directory / ".web.json").read_text())["state"] == "completed"
