import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from giraffe.storage import (DEFAULT_LIMITS, apply_cleanup, check_start, cleanup_plan,
                             parse_size, run_size, set_kept, storage_summary)


def saved(root, identifier, days=0, **metadata):
    directory = root / identifier
    directory.mkdir(parents=True)
    started = datetime.now(timezone.utc) - timedelta(days=days)
    (directory / "report.json").write_text(json.dumps({"run_id": identifier, "started_at": started.isoformat(), "schema_version": "2", "suite_version": "0.1.0", "finished_at": started.isoformat(), "overall": "pass", "manifest": {}}))
    if metadata:
        (directory / ".web.json").write_text(json.dumps({"id": identifier, **metadata}))
    return directory


def test_run_size_counts_regular_files_without_following_symlinks(tmp_path):
    run = saved(tmp_path, "one")
    (run / "report.html").write_bytes(b"abc")
    nested = run / "nested"
    nested.mkdir()
    (nested / "other").write_bytes(b"four")
    outside = tmp_path / "outside"
    outside.write_bytes(b"x" * 100)
    (run / "linked").symlink_to(outside)
    (run / "loop").symlink_to(run, target_is_directory=True)
    assert run_size(run) == (run / "report.json").stat().st_size + 7
    assert run_size(run / "loop") == 0


def test_summary_excludes_unrelated_root_children_and_separates_baselines(tmp_path):
    run = saved(tmp_path, "one")
    saved(tmp_path, ".hidden")
    (tmp_path / "linked").symlink_to(run, target_is_directory=True)
    (tmp_path / "unrelated").mkdir()
    (tmp_path / "unrelated" / "notes.txt").write_text("unrelated")
    (tmp_path / "notes.txt").write_text("unrelated")
    baseline = tmp_path / ".baselines" / "one"
    baseline.mkdir(parents=True)
    (baseline / "baseline.json").write_text('{"run_id":"one"}')
    result = storage_summary(tmp_path, limits={"max_total_storage": 123})
    assert [row["id"] for row in result["runs"]] == ["one"]
    assert result["runs_bytes"] == run_size(run)
    assert result["baselines_bytes"] == run_size(tmp_path / ".baselines")
    assert result["limits"] == DEFAULT_LIMITS | {"max_total_storage": 123}
    assert result["free_bytes"] > 0
    assert result["runs"][0]["protected_reason"] == "source run of a saved baseline"


def test_cleanup_criteria_combine_and_only_delete_as_needed_for_total(tmp_path):
    saved(tmp_path, "old", days=40)
    saved(tmp_path, "middle", days=10)
    saved(tmp_path, "new", days=1)
    assert cleanup_plan(tmp_path)["candidates"] == []
    assert [r["id"] for r in cleanup_plan(tmp_path, older_than_days=20)["candidates"]] == ["old"]
    assert [r["id"] for r in cleanup_plan(tmp_path, keep_latest=1)["candidates"]] == ["old", "middle"]
    combined = cleanup_plan(tmp_path, older_than_days=20, keep_latest=2)
    assert [r["id"] for r in combined["candidates"]] == ["old"]
    assert combined["candidates"][0]["criterion"] == "older_than_days, keep_latest"
    newest = run_size(tmp_path / "new")
    plan = cleanup_plan(tmp_path, max_total_bytes=newest)
    assert [r["id"] for r in plan["candidates"]] == ["old", "middle"]
    assert plan["bytes_freed"] == run_size(tmp_path / "old") + run_size(tmp_path / "middle")
    assert plan["runs_bytes_after"] == newest
    plan = cleanup_plan(tmp_path, older_than_days=20, max_total_bytes=newest)
    assert [r["id"] for r in plan["candidates"]] == ["old", "middle"]
    assert plan["candidates"][1]["criterion"] == "max_total_bytes"


def test_cleanup_protects_active_kept_baseline_busy_and_unrecognized_runs(tmp_path):
    for identifier in ("active", "kept", "baseline", "foreign", "nested", "symlink", "temporary", "eligible"):
        saved(tmp_path, identifier, days=30)
    saved(tmp_path, "running", days=30, state="running")
    saved(tmp_path, "stopping", days=30, state="stopping")
    set_kept(tmp_path / "kept", True)
    (tmp_path / "foreign" / "notes").write_text("mine")
    (tmp_path / "nested" / "folder").mkdir()
    (tmp_path / "symlink" / "report.html").symlink_to(tmp_path / "eligible" / "report.json")
    (tmp_path / "temporary" / "report.json.tmp").write_text("atomic")
    (tmp_path / "temporary" / ".web.json.token_123.tmp").write_text("atomic")
    baseline = tmp_path / ".baselines" / "saved"
    baseline.mkdir(parents=True)
    (baseline / "report.json").write_text('{"run_id":"baseline"}')
    plan = cleanup_plan(tmp_path, keep_latest=0, active_id="active")
    assert {r["id"] for r in plan["candidates"]} == {"eligible", "temporary"}
    reasons = {r["id"]: r["protected_reason"] for r in plan["protected"]}
    assert reasons == {"active": "active run", "kept": "kept run", "baseline": "source run of a saved baseline",
                       "foreign": "contains files Giraffe did not write", "nested": "contains files Giraffe did not write",
                       "symlink": "contains files Giraffe did not write", "running": "run is running", "stopping": "run is stopping"}


@pytest.mark.parametrize("kind", ["malformed", "symlink"])
def test_unknown_baseline_source_conservatively_protects_runs(tmp_path, kind):
    run = saved(tmp_path, "run")
    baseline = tmp_path / ".baselines" / "saved"
    baseline.mkdir(parents=True)
    if kind == "symlink":
        (baseline / "baseline.json").symlink_to(run / "report.json")
    else:
        (baseline / "baseline.json").write_text("{")
    plan = cleanup_plan(tmp_path, keep_latest=0)
    assert plan["candidates"] == []
    assert plan["protected"][0]["protected_reason"] == "saved baseline source could not be read"


@pytest.mark.parametrize("protection", ["kept", "active", "baseline", "running", "unrecognized"])
def test_apply_rechecks_stale_preview_protections(tmp_path, protection):
    run = saved(tmp_path, "run", days=30)
    assert [r["id"] for r in cleanup_plan(tmp_path, keep_latest=0)["candidates"]] == ["run"]
    criteria = {"keep_latest": 0}
    if protection == "kept":
        set_kept(run, True)
    elif protection == "active":
        criteria["active_id"] = "run"
    elif protection == "baseline":
        baseline = tmp_path / ".baselines" / "saved"
        baseline.mkdir(parents=True)
        (baseline / "baseline.json").write_text('{"run_id":"run"}')
    elif protection == "running":
        (run / ".web.json").write_text('{"state":"running"}')
    else:
        (run / "unrelated.txt").write_text("mine")
    result = apply_cleanup(tmp_path, ["run"], **criteria)
    assert result["deleted"] == []
    assert result["skipped"][0]["id"] == "run"
    assert run.exists()


def test_apply_only_confirmed_ids_and_safe_paths_and_set_kept(tmp_path):
    one = saved(tmp_path, "one", days=30)
    two = saved(tmp_path, "two", days=30)
    set_kept(one, True)
    assert storage_summary(tmp_path, limits={})["runs"][0]["id"] == "two"
    set_kept(one, False)
    size = run_size(one)
    result = apply_cleanup(tmp_path, ["one", "../two"], keep_latest=0)
    assert result["deleted"] == ["one"]
    assert result["bytes_freed"] == size
    assert result["skipped"] == [{"id": "../two", "reason": "invalid run id"}]
    assert not one.exists() and two.exists()
    result = apply_cleanup(tmp_path, ["two"])
    assert result["deleted"] == []
    assert result["bytes_freed"] == 0


def test_age_falls_back_to_directory_mtime(tmp_path):
    run = saved(tmp_path, "run")
    (run / "report.json").write_text("{}")
    (run / ".web.json").write_text('{"id":"run"}')
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(run, (old, old))
    assert cleanup_plan(tmp_path, older_than_days=20)["candidates"][0]["id"] == "run"


@pytest.mark.parametrize("value, expected", [("500MB", 500_000_000), ("2GiB", 2 * 1024 ** 3),
                                             ("1.5KiB", 1536), ("0", 0), (" 12 b ", 12), (45, 45)])
def test_size_parsing(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize("value", ["-1", "2watts", "", "1.2B", -1])
def test_size_parsing_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_size(value)


@pytest.mark.parametrize("criteria", [{"keep_latest": -1}, {"older_than_days": 1.5}, {"max_total_bytes": True}])
def test_invalid_cleanup_criteria(criteria, tmp_path):
    with pytest.raises(ValueError):
        cleanup_plan(tmp_path, **criteria)


def test_start_refuses_total_or_free_disk_and_zero_disables(tmp_path, monkeypatch):
    run = saved(tmp_path, "run")
    size = run_size(run)
    with pytest.raises(ValueError, match=rf"{size} bytes on disk.*1-byte.*cleanup"):
        check_start(tmp_path, {"max_total_storage": 1, "min_free_disk": 0})
    monkeypatch.setattr("giraffe.storage.shutil.disk_usage", lambda path: SimpleNamespace(free=2))
    with pytest.raises(ValueError, match="2 bytes.*3-byte.*cleanup"):
        check_start(tmp_path, {"max_total_storage": 0, "min_free_disk": 3})
    check_start(tmp_path, {"max_total_storage": 0, "min_free_disk": 0})


@pytest.mark.parametrize("layout", ["directory", "file"])
def test_baseline_matches_report_identity_when_output_directory_has_another_name(tmp_path, layout):
    run = saved(tmp_path, "custom-output")
    (run / "report.json").write_text('{"run_id":"actual-run-id","started_at":"2020-01-01","schema_version":"2","suite_version":"0.1.0","finished_at":"2020-01-01","overall":"pass","manifest":{}}')
    baseline = tmp_path / ".baselines"
    baseline.mkdir()
    if layout == "directory":
        baseline = baseline / "saved"
        baseline.mkdir()
        baseline = baseline / "report.json"
    else:
        baseline = baseline / "saved.json"
    baseline.write_text('{"run_id":"actual-run-id","started_at":"2020-01-01","schema_version":"2","suite_version":"0.1.0","finished_at":"2020-01-01","overall":"pass","manifest":{}}')
    plan = cleanup_plan(tmp_path, keep_latest=0)
    assert plan["candidates"] == []
    assert plan["protected"][0]["protected_reason"] == "source run of a saved baseline"


def test_unreadable_flat_baseline_conservatively_protects_runs(tmp_path):
    saved(tmp_path, "run")
    (tmp_path / ".baselines").mkdir()
    (tmp_path / ".baselines" / "saved.json").write_text("{")
    assert cleanup_plan(tmp_path, keep_latest=0)["candidates"] == []


def test_symlinked_runs_root_is_resolved_for_summary_cleanup_and_start(tmp_path):
    root = tmp_path / "actual"
    root.mkdir()
    saved(root, "one", days=30)
    link = tmp_path / "alias"
    link.symlink_to(root, target_is_directory=True)
    assert storage_summary(link, limits={})["runs"][0]["id"] == "one"
    check_start(link, {"max_total_storage": 10000, "min_free_disk": 0})
    assert cleanup_plan(link, keep_latest=0)["candidates"][0]["id"] == "one"
    assert apply_cleanup(link, ["one"], keep_latest=0)["deleted"] == ["one"]


def test_disabled_start_limits_do_not_inspect_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("giraffe.storage.storage_summary", lambda *a, **k: pytest.fail("storage inspected"))
    check_start(tmp_path, {"max_total_storage": 0, "min_free_disk": 0})


@pytest.mark.parametrize("kind,alive", [("active", True), ("legacy", False), ("foreign", False),
                                       ("self", False), ("dead", False), ("local", True),
                                       ("denied", True), ("heartbeat", True), ("recent", True),
                                       ("recent-self", True), ("recent-dead", True),
                                       ("heartbeat-dead", True), ("active-dead", True),
                                       ("recent-foreign", True), ("recent-legacy", True),
                                       ("recent-overflow", True), ("overflow", False),
                                       ("recent-unknown-error", True), ("unknown-error", False)])
def test_run_liveness_uses_host_pid_and_recent_heartbeat(tmp_path, monkeypatch, kind, alive):
    import socket
    from giraffe.storage import run_alive

    metadata = {"state": "running", "owner_host": socket.gethostname(), "owner_pid": os.getpid() + 12345}
    if kind in {"legacy", "recent-legacy"}:
        metadata.pop("owner_host")
    elif kind in {"foreign", "recent-foreign"}:
        metadata["owner_host"] = "another-host"
        metadata["owner_pid"] = 1
    elif kind in {"self", "recent-self"}:
        metadata["owner_pid"] = os.getpid()
    elif kind == "heartbeat":
        metadata["owner_host"] = "another-host"
        metadata["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    if kind == "heartbeat-dead":
        metadata["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    run = saved(tmp_path, "run", **metadata)
    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).timestamp()
    if not kind.startswith("recent"):
        os.utime(run / ".web.json", (old, old))
    def kill(pid, signal):
        assert signal == 0 and pid > 0 and pid != os.getpid()
        assert metadata.get("owner_host") == socket.gethostname()
        if kind in {"overflow", "recent-overflow"}:
            raise OverflowError()
        if kind in {"unknown-error", "recent-unknown-error"}:
            raise OSError()
        if kind in {"dead", "recent-dead", "heartbeat-dead", "active-dead"}:
            raise ProcessLookupError()
        if kind == "denied":
            raise PermissionError()
    monkeypatch.setattr("giraffe.storage.os.kill", kill)
    active_id = "run" if kind in {"active", "active-dead"} else None
    assert run_alive(run, active_id=active_id) is alive
    plan = cleanup_plan(tmp_path, keep_latest=0, active_id=active_id)
    assert bool(plan["protected"]) is alive


@pytest.mark.parametrize("pid", [None, True, "123", 0, -1])
@pytest.mark.parametrize("recent", [False, True])
def test_unknown_owner_pid_uses_freshness_without_probing_process_groups(tmp_path, monkeypatch, pid, recent):
    import socket
    from giraffe.storage import run_alive

    run = saved(tmp_path, "unknown", state="running", owner_pid=pid, owner_host=socket.gethostname())
    if not recent:
        os.utime(run / ".web.json", (1, 1))
    monkeypatch.setattr("giraffe.storage.os.kill", lambda *args: pytest.fail("invalid PID was probed"))
    assert run_alive(run) is recent
    assert bool(cleanup_plan(tmp_path, keep_latest=0)["protected"]) is recent


@pytest.mark.parametrize("temporary", [False, True])
def test_empty_or_temporary_only_baseline_does_not_protect_runs(tmp_path, temporary):
    saved(tmp_path, "run")
    baseline = tmp_path / ".baselines" / "unfinished"
    baseline.mkdir(parents=True)
    if temporary:
        (baseline / "baseline.json.tmp").write_text("{")
    assert cleanup_plan(tmp_path, keep_latest=0)["candidates"][0]["id"] == "run"


def test_apply_builds_only_one_plan_and_rechecks_protection_after_each_delete(tmp_path, monkeypatch):
    import giraffe.storage as storage
    saved(tmp_path, "one", days=30)
    two = saved(tmp_path, "two", days=20)
    three = saved(tmp_path, "three", days=10)
    plans = []
    original_plan, original_delete = storage.cleanup_plan, storage.shutil.rmtree
    def plan(*args, **kwargs):
        plans.append(1)
        return original_plan(*args, **kwargs)
    def delete(directory):
        original_delete(directory)
        set_kept(two, True)
        baseline = tmp_path / ".baselines" / "new"
        baseline.mkdir(parents=True)
        (baseline / "baseline.json").write_text('{"run_id":"three"}')
    monkeypatch.setattr(storage, "cleanup_plan", plan)
    monkeypatch.setattr(storage.shutil, "rmtree", delete)
    result = apply_cleanup(tmp_path, ["one", "two", "three"], keep_latest=0)
    assert len(plans) == 1
    assert result["deleted"] == ["one"]
    assert {row["reason"] for row in result["skipped"]} == {"kept run", "source run of a saved baseline"}
    assert two.exists() and three.exists()


def test_report_fields_are_cached_and_web_metadata_avoids_reading_report(tmp_path, monkeypatch):
    import giraffe.storage as storage
    run = saved(tmp_path, "run")
    storage._report_fields.cache_clear()
    reads = []
    original = storage._report_fields.__wrapped__
    from functools import lru_cache
    @lru_cache(maxsize=256)
    def fields(*args):
        reads.append(args)
        return original(*args)
    monkeypatch.setattr(storage, "_report_fields", fields)
    storage_summary(tmp_path, limits={})
    storage_summary(tmp_path, limits={})
    assert len(reads) == 1
    (run / "report.json").write_text('{"run_id":"changed", "started_at":"2020-01-01"}')
    storage_summary(tmp_path, limits={})
    assert len(reads) == 2
    (run / ".web.json").write_text('{"run_id":"changed", "started_at":"2020-01-01"}')
    storage_summary(tmp_path, limits={})
    assert len(reads) == 2


@pytest.mark.parametrize("error", [FileNotFoundError, NotADirectoryError])
def test_vanished_directory_is_skipped_during_size_and_summary(tmp_path, monkeypatch, error):
    from pathlib import Path
    run = saved(tmp_path, "run")
    original = Path.iterdir
    def entries(path):
        if path == run:
            (run / "report.json").unlink(missing_ok=True)
            run.rmdir()
            raise error()
        return original(path)
    monkeypatch.setattr(Path, "iterdir", entries)
    assert run_size(run) == 0
    saved(tmp_path, "run")
    assert storage_summary(tmp_path, limits={})["runs"] == []
    assert apply_cleanup(tmp_path, ["run"], keep_latest=0)["deleted"] == []


@pytest.mark.parametrize("maximum", [0, -1])
def test_cleanup_rejects_disabled_or_negative_total_with_explicit_delete_guidance(tmp_path, maximum):
    with pytest.raises(ValueError, match="--older-than 0"):
        cleanup_plan(tmp_path, max_total_bytes=maximum)


def test_legacy_temporary_files_are_recognized_but_similar_unrelated_files_are_protected(tmp_path):
    run = saved(tmp_path, "legacy")
    (run / "tmpabcd1234").write_text("partial atomic write")
    saved(tmp_path, "other")
    (tmp_path / "other" / "tmpnotes").write_text("mine")
    plan = cleanup_plan(tmp_path, keep_latest=0)
    assert [row["id"] for row in plan["candidates"]] == ["legacy"]
    assert plan["protected"][0]["id"] == "other"


def test_keep_newest_counts_protected_runs(tmp_path):
    saved(tmp_path, "old", days=30)
    newest = saved(tmp_path, "newest", days=1)
    set_kept(newest, True)
    assert [row["id"] for row in cleanup_plan(tmp_path, keep_latest=1)["candidates"]] == ["old"]


@pytest.mark.parametrize("change,reason", [("running", "run is running"),
                                           ("unrecognized", "contains files Giraffe did not write")])
def test_apply_revalidates_each_directory_after_plan(tmp_path, monkeypatch, change, reason):
    import giraffe.storage as storage
    saved(tmp_path, "one", days=30)
    two = saved(tmp_path, "two", days=20)
    original_delete = storage.shutil.rmtree
    def delete(directory):
        original_delete(directory)
        if change == "running":
            (two / ".web.json").write_text('{"state":"running"}')
        else:
            (two / "notes.txt").write_text("mine")
    monkeypatch.setattr(storage.shutil, "rmtree", delete)
    result = apply_cleanup(tmp_path, ["one", "two"], keep_latest=0)
    assert result["deleted"] == ["one"]
    assert result["skipped"] == [{"id": "two", "reason": reason}]
