import json
import socket
from datetime import datetime, timezone

import pytest

from giraffe.cli import main
from giraffe.evidence import EvidenceWriter
from giraffe.models import RunReport
from giraffe.storage import apply_cleanup, cleanup_plan, set_kept, update_metadata
from tests.test_evidence import record


def crashed_run(root):
    directory = root / "crashed"
    directory.mkdir()
    update_metadata(directory, {
        "id": directory.name, "run_id": directory.name, "state": "running",
        "started_at": "2020-01-01T00:00:00Z", "heartbeat_at": "2020-01-01T00:00:00Z",
        "owner_host": socket.gethostname(), "owner_pid": 2147483647})
    return directory


@pytest.mark.parametrize("retention,expected", [("all", "sensitive answer"), ("none", "")])
def test_recovered_journal_can_be_cleaned_and_retains_original_bytes(tmp_path, capsys, retention, expected):
    directory = crashed_run(tmp_path)
    writer = EvidenceWriter(directory, {"config": {"retention": retention}})
    writer.record_finished(record())
    writer.close()
    journal = (directory / "evidence.jsonl").read_bytes()
    assert cleanup_plan(tmp_path, keep_latest=0)["protected"][0]["reason"] == "unrecovered request evidence"

    assert main(["report", str(directory), "--html"]) == 0
    recovered = RunReport.model_validate_json((directory / "report.json").read_text())
    assert recovered.overall == "inconclusive"
    assert recovered.requests[0].output == expected
    assert (directory / "evidence.jsonl").read_bytes() == journal
    assert json.loads((directory / ".web.json").read_text())["state"] == "interrupted"
    assert apply_cleanup(tmp_path, [directory.name], keep_latest=0)["deleted"] == [directory.name]


def test_failed_recovery_publication_leaves_journal_protected(tmp_path, monkeypatch, capsys):
    directory = crashed_run(tmp_path)
    writer = EvidenceWriter(directory)
    writer.record_finished(record())
    writer.close()
    before = (directory / "evidence.jsonl").read_bytes()

    def fail(*args, **kwargs):
        raise OSError("report publication failed")

    monkeypatch.setattr("giraffe.reporting._atomic_write_chunks", fail)
    assert main(["report", str(directory), "--html"]) == 4
    assert not (directory / "report.json").exists()
    assert (directory / "evidence.jsonl").read_bytes() == before
    assert cleanup_plan(tmp_path, keep_latest=0)["protected"][0]["reason"] == "unrecovered request evidence"


@pytest.mark.parametrize("contents", ["", "{broken\n"])
def test_cli_explicitly_releases_unrecoverable_journal_for_cleanup(tmp_path, capsys, contents):
    directory = crashed_run(tmp_path)
    (directory / "evidence.jsonl").write_text(contents)
    assert not cleanup_plan(tmp_path, keep_latest=0)["candidates"]
    assert main(["report", str(directory), "--html"]) == 4
    assert not (directory / "report.json").exists()

    assert main(["runs", "keep", str(directory), "--off"]) == 0
    assert "manual cleanup protection removed" in capsys.readouterr().out
    arguments = ["runs", "cleanup", "--runs-dir", str(tmp_path), "--keep-latest", "0"]
    assert main(arguments) == 0
    assert directory.exists()
    assert main([*arguments, "--yes"]) == 0
    assert not directory.exists()


@pytest.mark.parametrize("change", ["append", "replace"])
def test_changed_journal_is_protected_after_explicit_release(tmp_path, change):
    directory = crashed_run(tmp_path)
    journal = directory / "evidence.jsonl"
    journal.write_text("{broken\n")
    set_kept(directory, False)
    assert cleanup_plan(tmp_path, keep_latest=0)["candidates"]
    if change == "append":
        with journal.open("a") as stream:
            stream.write("new evidence\n")
    else:
        replacement = directory / "replacement"
        replacement.write_bytes(journal.read_bytes())
        replacement.replace(journal)
    assert cleanup_plan(tmp_path, keep_latest=0)["protected"][0]["reason"] == "unrecovered request evidence"
    assert not apply_cleanup(tmp_path, [directory.name], keep_latest=0)["deleted"]


@pytest.mark.parametrize("protection,reason", [
    ("active", "active run"), ("live", "run is running"),
    ("baseline", "source run of a saved baseline"),
    ("foreign", "contains files Giraffe did not write"), ("keep", "kept run")])
def test_explicit_journal_release_preserves_other_cleanup_protections(tmp_path, protection, reason):
    directory = crashed_run(tmp_path)
    (directory / "evidence.jsonl").write_text("{broken\n")
    set_kept(directory, False)
    criteria = {"keep_latest": 0}
    if protection == "active":
        criteria["active_id"] = directory.name
    elif protection == "live":
        update_metadata(directory, {"heartbeat_at": datetime.now(timezone.utc).isoformat()})
    elif protection == "baseline":
        (tmp_path / ".baselines").mkdir()
        (tmp_path / ".baselines" / "saved.json").write_text(json.dumps({"source_run_id": directory.name}))
    elif protection == "foreign":
        (directory / "notes.txt").write_text("unrelated")
    else:
        set_kept(directory, True)
    assert cleanup_plan(tmp_path, **criteria)["protected"][0]["reason"] == reason
    assert not apply_cleanup(tmp_path, [directory.name], **criteria)["deleted"]
