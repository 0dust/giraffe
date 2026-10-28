"""Disk controls through the in-process HTTP API."""

import errno
import json
import os
import time
from collections import namedtuple

import pytest
from fastapi.testclient import TestClient

from giraffe.models import RunConfig
from giraffe.reporting import write_report
from giraffe.web import create_app
from tests.test_web import config, report


def saved(root, identifier):
    write_report(report(RunConfig.model_validate(config())), root / identifier)


def completed(client, identifier):
    for _ in range(100):
        detail = client.get(f"/api/runs/{identifier}").json()
        if detail["state"] not in {"running", "stopping"}:
            return detail
        time.sleep(0.01)
    pytest.fail("Run did not leave active state")


def test_storage_endpoints_preview_keep_and_stale_confirm(tmp_path):
    saved(tmp_path, "one")
    saved(tmp_path, "two")
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        summary = client.get("/api/storage").json()
        assert summary["runs_bytes"] == sum(row["bytes"] for row in summary["runs"])
        assert summary["free_bytes"] > 0
        assert summary["limits"]["max_total_storage"] == 5 * 1024**3
        rows = client.get("/api/runs").json()["runs"]
        assert all(row["bytes"] > 0 and not row["kept"] for row in rows)
        preview = client.post("/api/storage/cleanup-preview", json={"keep_latest": 0}).json()
        assert {row["id"] for row in preview["candidates"]} == {"one", "two"}
        assert (tmp_path / "one").exists()
        assert client.post("/api/runs/one/keep", json={"keep": True}).json()["kept"]
        applied = client.post("/api/storage/cleanup", json={"keep_latest": 0, "ids": ["one", "two"]}).json()
        assert applied["deleted"] == ["two"]
        assert applied["skipped"][0]["id"] == "one"
        assert (tmp_path / "one" / ".keep").is_file()
        assert client.get("/api/runs").json()["runs"][0]["kept"]
        assert not client.post("/api/runs/one/keep", json={"keep": False}).json()["kept"]


@pytest.mark.parametrize("body", [{"keep_latest": -1}, {"older_than_days": -1},
                                  {"max_total_bytes": -1}, {"keep_latest": 1.5}])
def test_cleanup_validates_criteria(tmp_path, body):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        assert client.post("/api/storage/cleanup-preview", json=body).status_code == 422
        assert client.post("/api/storage/cleanup", json={**body, "ids": []}).status_code == 422


def test_cleanup_validates_ids_and_requires_json(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        assert client.post("/api/storage/cleanup", json={"keep_latest": 0, "ids": ["../outside"]}).status_code == 404
        assert client.post("/api/storage/cleanup-preview", content="{}").status_code == 415
        assert client.post("/api/runs/missing/keep", json={"keep": True}).status_code == 404
        assert client.post("/api/runs/missing/keep", json={"keep": "false"}).status_code == 422


@pytest.mark.parametrize("limit", ["total", "free"])
def test_start_refuses_disk_limits_before_runner(tmp_path, monkeypatch, limit):
    saved(tmp_path, "existing")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda path: Usage(10000, 9999, 1))
    monkeypatch.setattr("giraffe.web.run_suite", lambda *a, **kw: pytest.fail("Started despite disk limit"))
    limits = {"max_total_storage": 1 if limit == "total" else 0,
              "min_free_disk": 0 if limit == "total" else 2}
    with TestClient(create_app(tmp_path, **limits), base_url="http://127.0.0.1:8787") as client:
        response = client.post("/api/runs", json={"config": config()})
        assert response.status_code == 507
        assert "1" in response.json()["detail"]
        assert "cleanup" in response.json()["detail"].lower()
        assert "disk" in response.json()["detail"].lower()
        assert client.get("/api/runs").json()["active_run_id"] is None


@pytest.mark.parametrize("summary_fails", [False, True])
def test_disk_full_export_sets_error_and_saves_summary(tmp_path, monkeypatch, summary_fails, local_tokenizer):
    async def runner(configuration, **kwargs):
        return report(configuration)

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    monkeypatch.setattr("giraffe.web.write_report", full)
    if summary_fails:
        from pathlib import Path
        original = Path.replace

        def fail_summary(path, target):
            if Path(target).name == "summary.json":
                full()
            return original(path, target)
        monkeypatch.setattr(Path, "replace", fail_summary)
    with TestClient(create_app(tmp_path, min_free_disk=0), base_url="http://127.0.0.1:8787") as client:
        response = client.post("/api/runs", json={"config": config()})
        assert response.status_code == 202
        identifier = response.json()["id"]
        detail = completed(client, identifier)
        assert detail["state"] == "error"
        assert detail["report"] is None
        assert "disk full" in detail["error"]
        assert "1 requests" in detail["error"]
        assert client.get("/api/runs").json()["active_run_id"] is None
    if summary_fails:
        assert "no summary could be saved" in detail["error"]
    else:
        summary = json.loads((tmp_path / identifier / "summary.json").read_text())
        assert summary["overall"] == "pass"  # Recorded result survives, run state stays error.
        assert summary["request_count"] == 1
        (tmp_path / identifier / ".web.json").unlink()
        with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
            detail = client.get(f"/api/runs/{identifier}").json()
            assert detail["state"] == "error"
            assert detail["summary"]["checks"][0]["summary"] == "Completed"


def test_export_error_takes_priority_over_partial_report_that_cannot_be_removed(tmp_path, monkeypatch, local_tokenizer):
    from pathlib import Path

    async def runner(configuration, **kwargs):
        return report(configuration)

    def partial(value, directory, **kwargs):
        write_report(value, directory)
        raise OSError(errno.ENOSPC, "No space left")

    original = Path.unlink

    def cannot_remove(path, *args, **kwargs):
        if path.name == "report.json":
            raise PermissionError(errno.EACCES, "Cannot remove partial report")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    monkeypatch.setattr("giraffe.web.write_report", partial)
    monkeypatch.setattr(Path, "unlink", cannot_remove)
    with TestClient(create_app(tmp_path, min_free_disk=0), base_url="http://127.0.0.1:8787") as client:
        identifier = client.post("/api/runs", json={"config": config()}).json()["id"]
        detail = completed(client, identifier)
        assert detail["state"] == "error"
        assert detail["report"] is None
        assert detail["summary"]["overall"] == "pass"


def test_web_startup_preserves_cleanup_protection_of_live_cli_writer(tmp_path):
    import os

    directory = tmp_path / "live-cli"
    directory.mkdir()
    marker = {"id": "live-cli", "state": "running", "owner_pid": os.getpid()}
    (directory / ".web.json").write_text(json.dumps(marker))
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        assert client.get("/api/runs/live-cli").json()["state"] == "running"
        plan = client.post("/api/storage/cleanup-preview", json={"keep_latest": 0}).json()
        assert plan["candidates"] == []
        assert plan["protected"][0]["id"] == "live-cli"
    assert json.loads((directory / ".web.json").read_text()) == marker


def test_restart_recovers_complete_report_even_if_metadata_disk_is_readonly(tmp_path, monkeypatch):
    saved(tmp_path, "partial")
    marker = tmp_path / "partial" / ".web.json"
    marker.write_text(json.dumps({"state": "running", "owner_pid": 123456789}))
    os.utime(marker, (1, 1))

    def readonly(*args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr("giraffe.web._write", readonly)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        detail = client.get("/api/runs/partial").json()
        assert detail["state"] == "completed"
        assert detail["report"]["overall"] == "pass"
        assert client.get("/api/runs").json()["runs"][0]["overall"] == "pass"
