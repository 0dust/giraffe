"""Recovery and filesystem work must preserve complete evidence and a responsive API."""

import asyncio
import errno
import json
import os
import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from giraffe.models import RunConfig
from giraffe.reporting import write_report
from giraffe.web import create_app
from tests.test_storage_web import completed
from tests.test_web import config, report


def saved(root, identifier="saved"):
    write_report(report(RunConfig.model_validate(config())), root / identifier)
    return root / identifier


@pytest.mark.parametrize("owner", ["self", "dead", "foreign", "legacy"])
def test_stale_running_with_valid_report_recovers_complete_evidence(tmp_path, owner):
    directory = saved(tmp_path)
    metadata = {"state": "running", "owner_pid": os.getpid(), "owner_host": socket.gethostname()}
    if owner == "dead":
        metadata["owner_pid"] = 123456789
    elif owner == "foreign":
        metadata.update(owner_pid=1, owner_host="other-container")
    elif owner == "legacy":
        metadata.pop("owner_host")
    marker = directory / ".web.json"
    marker.write_text(json.dumps(metadata))
    os.utime(marker, (time.time() - 181, time.time() - 181))
    with TestClient(create_app(tmp_path)) as client:
        detail = client.get("/api/runs/saved").json()
        assert detail["state"] == "completed"
        assert detail["report"]["run_id"] == "suite-run-id"
        assert detail["report"]["requests"][0]["id"] == "request-one"
    assert json.loads(marker.read_text())["state"] == "completed"


def test_stale_running_without_report_recovers_interrupted_and_cleanable(tmp_path):
    directory = tmp_path / "orphan"
    directory.mkdir()
    marker = directory / ".web.json"
    marker.write_text(json.dumps({"id": "orphan", "state": "running", "owner_pid": 1, "owner_host": "other-container"}))
    os.utime(marker, (1, 1))
    with TestClient(create_app(tmp_path)) as client:
        detail = client.get("/api/runs/orphan").json()
        assert detail["state"] == "interrupted" and detail["report"] is None
        plan = client.post("/api/storage/cleanup-preview", json={"older_than_days": 0}).json()
        assert [row["id"] for row in plan["candidates"]] == ["orphan"]


def test_valid_report_is_not_hidden_by_error_metadata_without_failure_summary(tmp_path):
    directory = saved(tmp_path)
    (directory / ".web.json").write_text(json.dumps({"state": "error", "error": "Status write warning"}))
    with TestClient(create_app(tmp_path)) as client:
        detail = client.get("/api/runs/saved").json()
        assert detail["report"]["overall"] == "pass"
        assert detail["error"] == "Status write warning"


@pytest.mark.parametrize("failed_file", ["report.json", "baseline.json"])
def test_failed_new_baseline_save_removes_its_directory(tmp_path, monkeypatch, failed_file):
    import giraffe.web

    saved(tmp_path)
    original = giraffe.web._write

    def full(path, value):
        if ".baselines" in path.parts and path.name == failed_file:
            raise OSError(errno.ENOSPC, "Disk full")
        return original(path, value)

    monkeypatch.setattr(giraffe.web, "_write", full)
    if failed_file == "report.json":
        from pathlib import Path
        original_replace = Path.replace

        def fail_publish(path, target):
            if ".baselines" in Path(target).parts and Path(target).name == "report.json":
                raise OSError(errno.ENOSPC, "Disk full")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_publish)
    with TestClient(create_app(tmp_path)) as client:
        result = client.post("/api/baselines", json={"run_id": "saved", "name": "new"})
        assert result.status_code == 507
        assert list((tmp_path / ".baselines").iterdir()) == []
        preview = client.post("/api/storage/cleanup-preview", json={"older_than_days": 0}).json()
        assert [row["id"] for row in preview["candidates"]] == ["saved"]


def test_json_only_export_ignores_html_disk_failure_and_downloads_on_demand(tmp_path, monkeypatch, local_tokenizer):
    import giraffe.reporting

    async def runner(configuration, **kwargs):
        return report(configuration)

    original = giraffe.reporting._atomic_write

    def full_html(path, contents):
        if path.name == "report.html":
            path.write_text("partial")
            raise OSError(errno.ENOSPC, "Disk full")
        return original(path, contents)

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    monkeypatch.setattr(giraffe.reporting, "_atomic_write", full_html)
    with TestClient(create_app(tmp_path, min_free_disk=0)) as client:
        identifier = client.post("/api/runs", json={"config": config()}).json()["id"]
        detail = completed(client, identifier)
        assert detail["state"] == "completed" and detail["report"]["overall"] == "pass"
        assert detail["summary"] is None
        assert detail["error"] is None
        assert "export" in detail["resources"]["sampled_peak_rss_bytes"]
        assert client.get(f"/api/runs/{identifier}/report.json").status_code == 200
        assert client.get(f"/api/runs/{identifier}/report.html").status_code == 200
        assert not (tmp_path / identifier / "report.html").exists()
        assert not (tmp_path / identifier / "summary.json").exists()
        assert client.post("/api/baselines", json={"run_id": identifier, "name": "JSON evidence"}).status_code == 201


@pytest.mark.parametrize("target", ["cleanup-preview", "cleanup"])
@pytest.mark.parametrize("value", [0, -1])
def test_cleanup_total_requires_positive_api_value(tmp_path, target, value):
    saved(tmp_path)
    body = {"max_total_bytes": value}
    if target == "cleanup":
        body["ids"] = ["saved"]
    with TestClient(create_app(tmp_path)) as client:
        response = client.post(f"/api/storage/{target}", json=body)
        assert response.status_code == 422
        assert "--older-than 0" in response.text
        assert (tmp_path / "saved").exists()


@pytest.mark.parametrize("path, function, body", [
    ("/api/storage", "storage_summary", None),
    ("/api/storage/cleanup-preview", "cleanup_plan", {}),
    ("/api/storage/cleanup", "apply_cleanup", {"ids": []}),
    ("/api/runs/saved/keep", "set_kept", {"keep": True}),
])
async def test_filesystem_endpoints_use_threadpool(tmp_path, monkeypatch, path, function, body):
    import giraffe.web

    saved(tmp_path)
    loop_thread = threading.get_ident()
    calls = []
    original = getattr(giraffe.web, function)

    def observed(*args, **kwargs):
        calls.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(giraffe.web, function, observed)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await (client.get(path) if body is None else client.post(path, json=body))
    assert response.status_code == 200
    assert calls and all(value != loop_thread for value in calls)


async def test_prestart_storage_check_yields_loop_without_double_start(tmp_path, monkeypatch, local_tokenizer):
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    thread_ids = []

    def check(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(2)

    async def runner(configuration, *, stop_event, **kwargs):
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.check_start", check)
    monkeypatch.setattr("giraffe.web.run_suite", runner)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            first = asyncio.create_task(client.post("/api/runs", json={"config": config()}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert (await asyncio.wait_for(client.get("/api/bootstrap"), .5)).status_code == 200
                second = asyncio.create_task(client.post("/api/runs", json={"config": config()}))
            finally:
                release.set()
            results = await asyncio.gather(first, second)
            assert [result.status_code for result in results] == [202, 409]
            identifier = results[0].json()["id"]
            marker = json.loads((tmp_path / identifier / ".web.json").read_text())
            assert marker["owner_host"] == socket.gethostname()
            plan = (await client.post("/api/storage/cleanup-preview", json={"older_than_days": 0})).json()
            assert plan["candidates"] == []
            assert plan["protected"][0]["id"] == identifier
            assert thread_ids == [thread_ids[0]] and thread_ids[0] != loop_thread
            await client.post(f"/api/runs/{identifier}/cancel", json={})
