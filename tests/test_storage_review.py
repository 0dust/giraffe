"""Regression coverage for cleanup ownership, bounded downloads and publication."""

import asyncio
import errno
import json
import os
from pathlib import Path
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from giraffe.cli import load_report, main
from giraffe.reporting import write_report
from giraffe.storage import apply_cleanup, automatic_cleanup, cleanup_plan
from giraffe.web import create_app
from scripts.measure_storage import synthetic_report
from tests.test_web import config, report as web_report


@pytest.mark.parametrize("proof", ["foreign-report", "foreign-state", "missing-versions"])
def test_foreign_report_directories_are_never_cleanup_candidates(tmp_path, proof):
    foreign = tmp_path / "coverage"
    foreign.mkdir()
    value = {"tests": [], "created": "2020-01-01"}
    if proof == "missing-versions":
        value = {"run_id": "coverage", "started_at": "2020-01-01"}
    (foreign / "report.json").write_text(json.dumps(value))
    (foreign / "report.html").write_text("pytest coverage")
    if proof == "foreign-state":
        (foreign / ".web.json").write_text('{"id":"different","state":"completed"}')
    os.utime(foreign, (1, 1))
    original = {path.name: path.read_bytes() for path in foreign.iterdir()}
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        for extension in ("html", "json"):
            assert client.get(f"/api/runs/coverage/report.{extension}").status_code == 404
    assert {path.name: path.read_bytes() for path in foreign.iterdir()} == original
    write_report(synthetic_report(2, 10, "none"), tmp_path / "real")
    plan = cleanup_plan(tmp_path, older_than_days=30)
    assert "coverage" not in {row["id"] for row in plan["candidates"]}
    assert next(row for row in plan["protected"] if row["id"] == "coverage")["reason"] == "not a Giraffe run"
    assert not apply_cleanup(tmp_path, ["coverage"], older_than_days=30)["deleted"]
    assert "coverage" not in automatic_cleanup(tmp_path, {"older_than_days": 30}, active_id="real")["deleted"]
    assert {path.name: path.read_bytes() for path in foreign.iterdir()} == original


def test_cli_auto_cleanup_rejects_arbitrary_output_parent_before_running(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage" / "report.json").write_text('{"tests":[]}')
    monkeypatch.setattr("giraffe.cli._run", lambda *a, **k: pytest.fail("must refuse before running"))
    assert main(["run", "--url", "http://localhost", "--model", "tiny", "--skip-preflight",
                 "--output", "myrun", "--auto-cleanup-older-than", "30"]) == 4
    assert "--runs-dir" in capsys.readouterr().err
    assert (tmp_path / "coverage" / "report.json").exists()
    assert not (tmp_path / "myrun").exists()


def test_cleanup_maximum_scans_each_directory_constant_times(tmp_path, monkeypatch):
    import giraffe.storage as storage

    for index in range(40):
        write_report(synthetic_report(1, 10, "none"), tmp_path / f"r{index:03}")
    scans = []
    original = storage._file_sizes

    def scan(path):
        scans.append(path)
        return original(path)

    monkeypatch.setattr(storage, "_file_sizes", scan)
    plan = cleanup_plan(tmp_path, max_total_bytes=1)
    assert len(plan["candidates"]) == 40
    assert len(scans) <= 2 * 40


@pytest.mark.parametrize("extension", ["html", "json"])
def test_input_hash_tampering_fails_loaders_and_downloads(tmp_path, extension):
    path, _ = write_report(synthetic_report(2, 10, "all"), tmp_path / "saved")
    raw = json.loads(path.read_text())
    next(iter(raw["inputs"].values()))[0]["content"] = "changed evidence"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_report(path)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        result = client.get(f"/api/runs/saved/report.{extension}")
        assert result.status_code == 404
        assert "SHA-256 mismatch" in result.json()["detail"]


def test_cli_baseline_export_is_expanded_current_schema_with_previous_mode(tmp_path, capsys):
    source = tmp_path / "saved"
    write_report(synthetic_report(3, 10, "failures"), source)
    destination = tmp_path / "portable.json"
    assert main(["baseline", "save", str(source), str(destination)]) == 0
    capsys.readouterr()
    value = json.loads(destination.read_text())
    assert value["schema_version"] == "4"
    assert "inputs" not in value and "report_format" not in value
    assert all("input_ref" not in row and "messages" in row["evaluation"] for row in value["requests"])
    assert destination.stat().st_mode & 0o777 == 0o644
    assert destination.stat().st_ino != (source / "report.json").stat().st_ino
    assert json.loads((source / "report.json").read_text())["schema_version"] == "4"


@pytest.mark.parametrize("fault", ["link_publish", "copy_write", "invalid_source", "validation_error"])
def test_real_web_baseline_failure_removes_new_directory(tmp_path, monkeypatch, fault):
    import giraffe.reporting as reporting

    source = tmp_path / "saved"
    write_report(synthetic_report(2, 10, "all"), source)
    replace = Path.replace
    if fault == "copy_write":
        def no_link(*args, **kwargs):
            raise OSError(errno.EXDEV, "different filesystem")
        def no_space(incoming, outgoing, **kwargs):
            outgoing.write(b"partial copy")
            raise OSError(errno.ENOSPC, "disk full")
        monkeypatch.setattr(reporting.os, "link", no_link)
        monkeypatch.setattr(reporting.shutil, "copyfileobj", no_space)
    else:
        def publish(path, destination):
            if ".baselines" in Path(destination).parts:
                raise OSError(errno.ENOSPC, "disk full")
            return replace(path, destination)
        monkeypatch.setattr(Path, "replace", publish)
    if fault in {"invalid_source", "validation_error"}:
        # Change the saved evidence after the endpoint's completed-run check.
        def invalid(report):
            if fault == "validation_error":
                from giraffe.models import RunReport
                RunReport.model_validate({})
            raise ValueError("changed or invalid source")
        monkeypatch.setattr(reporting, "_retained", invalid)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        result = client.post("/api/baselines", json={"run_id": "saved", "name": "chosen"})
        assert result.status_code == (422 if fault in {"invalid_source", "validation_error"} else 507)
    assert list((tmp_path / ".baselines").iterdir()) == []
    assert load_report(source).requests


@pytest.mark.parametrize("extensions", [("html", "html"), ("json", "html"), ("json", "json")])
async def test_downloads_are_single_flight_sampled_and_json_avoids_full_loader(tmp_path, monkeypatch, extensions):
    import giraffe.web as web

    write_report(synthetic_report(30, 1024, "all"), tmp_path / "saved")
    (tmp_path / "saved" / ".web.json").write_text('{"id":"saved","state":"completed"}')
    entered, release = threading.Event(), threading.Event()
    original_html, original_json = web._render, web.expanded_report_chunks
    calls = []
    guard = threading.Lock()

    def enter(kind):
        with guard:
            calls.append(kind)
            ordinal = len(calls)
        if ordinal == 1:
            entered.set()
            assert release.wait(2)

    def html(report):
        enter("html")
        return original_html(report)

    def chunks(path):
        enter("json")
        yield from original_json(path)

    monkeypatch.setattr(web, "_render", html)
    monkeypatch.setattr(web, "expanded_report_chunks", chunks)
    expected = load_report(tmp_path / "saved").model_dump(mode="json")
    if extensions == ("json", "json"):
        monkeypatch.setattr(web, "_report", lambda *a: pytest.fail("JSON download materialized RunReport"))
        read_text = Path.read_text

        def no_full_read(path, *args, **kwargs):
            assert path.name != "report.json", "JSON download read the whole report"
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", no_full_read)
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787") as client:
        first = asyncio.create_task(client.get(f"/api/runs/saved/report.{extensions[0]}"))
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(client.get(f"/api/runs/saved/report.{extensions[1]}"))
        await asyncio.sleep(.02)
        assert len(calls) == 1
        release.set()
        results = await asyncio.gather(first, second)
        assert len(calls) == 2
        for extension, result in zip(extensions, results):
            assert result.status_code == 200
            assert result.headers["content-disposition"] == f'attachment; filename="giraffe-saved.{extension}"'
            assert result.headers["content-type"].startswith("application/json" if extension == "json" else "text/html")
            if extension == "json":
                assert result.json() == expected
    meta = json.loads((tmp_path / "saved" / ".web.json").read_text())
    assert all("download_" + extension in meta["resources"]["sampled_peak_rss_bytes"] for extension in extensions)


async def test_download_cancellation_waits_for_worker_before_releasing_lock(tmp_path, monkeypatch):
    import giraffe.web as web

    write_report(synthetic_report(2, 10, "none"), tmp_path / "saved")
    entered, release = threading.Event(), threading.Event()
    renders = []
    original = web._render

    def render(report):
        renders.append(1)
        if len(renders) == 1:
            entered.set()
            assert release.wait(2)
        return original(report)

    monkeypatch.setattr(web, "_render", render)
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787") as client:
        first = asyncio.create_task(client.get("/api/runs/saved/report.html"))
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        second = asyncio.create_task(client.get("/api/runs/saved/report.html"))
        await asyncio.sleep(.02)
        assert len(renders) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second).status_code == 200


async def test_cancelled_run_skips_cleanup_and_always_persists_final_state(tmp_path, monkeypatch, local_tokenizer):
    import giraffe.web as web
    captured, entered = [], threading.Event()

    original_start = web._RunThread.start

    def capture_execution(worker):
        captured.append(asyncio.current_task())
        original_start(worker)

    monkeypatch.setattr(web._RunThread, "start", capture_execution)

    async def runner(configuration, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    monkeypatch.setattr("giraffe.web.automatic_cleanup", lambda *a, **k: pytest.fail("cancelled run cleaned up"))
    app = create_app(tmp_path, min_free_disk=0, auto_cleanup={"older_than_days": 0})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787") as client:
            identifier = (await client.post("/api/runs", json={"config": config()})).json()["id"]
            assert await asyncio.to_thread(entered.wait, 2)
            captured[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await captured[0]
            assert (await client.get("/api/bootstrap")).json()["active_run_id"] is None
    meta = json.loads((tmp_path / identifier / ".web.json").read_text())
    assert meta["state"] == "interrupted"
    assert "auto_cleanup" not in meta


async def test_cancellation_during_cleanup_waits_and_records_log_before_clearing_active(tmp_path, monkeypatch, local_tokenizer):
    import giraffe.web as web

    write_report(synthetic_report(2, 10, "none"), tmp_path / "old")
    captured = []
    entered, release = threading.Event(), threading.Event()
    original = web.automatic_cleanup

    original_start = web._RunThread.start

    def capture_execution(worker):
        captured.append(asyncio.current_task())
        original_start(worker)

    monkeypatch.setattr(web._RunThread, "start", capture_execution)

    async def runner(configuration, **kwargs):
        return web_report(configuration)

    def cleanup(*args, **kwargs):
        identifier = kwargs["active_id"]
        meta = json.loads((tmp_path / identifier / ".web.json").read_text())
        assert meta["state"] == "completed"  # Final result is durable before cleanup starts.
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(web, "run_suite", runner)
    monkeypatch.setattr(web, "automatic_cleanup", cleanup)
    app = create_app(tmp_path, min_free_disk=0, auto_cleanup={"older_than_days": 0})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787") as client:
            identifier = (await client.post("/api/runs", json={"config": config()})).json()["id"]
            assert await asyncio.to_thread(entered.wait, 1)
            captured[0].cancel()
            await asyncio.sleep(.01)
            assert (await client.get("/api/bootstrap")).json()["active_run_id"] == identifier
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await captured[0]
            assert (await client.get("/api/bootstrap")).json()["active_run_id"] is None
    meta = json.loads((tmp_path / identifier / ".web.json").read_text())
    assert meta["state"] == "completed"
    assert meta["auto_cleanup"]["deleted"] == ["old"]
    assert not (tmp_path / "old").exists()


async def test_cancelled_json_stream_closes_lookup_and_releases_download_lease(tmp_path, monkeypatch):
    import giraffe.report_stream as report_stream

    write_report(synthetic_report(30, 1024, "all"), tmp_path / "saved")
    entered = asyncio.Event()
    temporary_paths = []
    temporary_directory = report_stream.tempfile.TemporaryDirectory

    def track(*args, **kwargs):
        directory = temporary_directory(*args, **kwargs)
        temporary_paths.append(Path(directory.name))
        return directory

    monkeypatch.setattr(report_stream.tempfile, "TemporaryDirectory", track)
    app = create_app(tmp_path)

    async def blocked_app(scope, receive, send):
        async def slow_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                entered.set()
                await asyncio.Event().wait()
            await send(message)
        await app(scope, receive, slow_send)

    # The streaming wrapper hides app.state from the shared client fixture.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(blocked_app), base_url="http://127.0.0.1:8787",
                                 headers={"X-Giraffe-Token": app.state.giraffe_token}) as blocked, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787") as client:
        first = asyncio.create_task(blocked.get("/api/runs/saved/report.json"))
        await asyncio.wait_for(entered.wait(), 1)
        assert temporary_paths and temporary_paths[0].exists()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not temporary_paths[0].exists()
        result = await asyncio.wait_for(client.get("/api/runs/saved/report.json"), 1)
        assert result.status_code == 200
        assert all(not path.exists() for path in temporary_paths)


def test_default_runs_root_cleanup_does_not_adopt_cwd(tmp_path, monkeypatch, capsys, local_tokenizer):
    from tests.test_storage_cli_regressions import run_arguments

    monkeypatch.chdir(tmp_path)
    write_report(synthetic_report(2, 10, "none"), tmp_path / "runs" / "old")
    # Even another genuine Giraffe-looking directory in cwd stays outside the chosen root.
    write_report(synthetic_report(2, 10, "none"), tmp_path / "other-project")

    async def runner(configuration, **kwargs):
        return web_report(configuration)

    monkeypatch.setattr("giraffe.cli._run", runner)
    assert main(run_arguments(Path("runs") / "new") + ["--auto-cleanup-older-than", "0"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["auto_cleanup"]["deleted"] == ["old"]
    assert (tmp_path / "other-project" / "report.json").is_file()
    assert (tmp_path / "runs" / "new" / "report.json").is_file()


def test_managed_baseline_upgrades_unversioned_format_two_without_changing_source(tmp_path):
    from giraffe.reporting import save_baseline_report

    path, _ = write_report(synthetic_report(3, 10, "all"), tmp_path / "old")
    raw = json.loads(path.read_text())
    raw["schema_version"] = "2"  # Earlier PR #46 report format before the review fix.
    path.write_text(json.dumps(raw, separators=(",", ":")))
    original = path.read_bytes()
    baseline = tmp_path / ".baselines" / "chosen" / "report.json"
    save_baseline_report(path, baseline)
    assert json.loads(baseline.read_text())["schema_version"] == "4"
    assert path.read_bytes() == original
    assert load_report(path).requests == load_report(baseline).requests


def test_recovery_does_not_adopt_foreign_state_file(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    marker = foreign / ".web.json"
    original = b'{"id":"another-directory","state":"running","owner_pid":1,"owner_host":"other-host"}'
    marker.write_bytes(original)
    os.utime(marker, (1, 1))
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        plan = client.post("/api/storage/cleanup-preview", json={"older_than_days": 0}).json()
        assert not plan["candidates"]
        assert plan["protected"][0]["reason"] == "not a Giraffe run"
    assert marker.read_bytes() == original
