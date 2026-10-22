"""Follow-up regressions for identity, retries, parsing and download isolation."""

import asyncio
import errno
import io
import json
import os
import socket
import threading

from anyio import CancelScope, create_task_group
import httpx
import pytest
from fastapi.testclient import TestClient

from giraffe.reporting import write_report
from giraffe.report_stream import _Reader
from giraffe.storage import apply_cleanup, automatic_cleanup, cleanup_plan
from giraffe.web import create_app
from scripts.measure_storage import synthetic_report
from tests.test_storage_web import completed
from tests.test_web import config, report as web_report


@pytest.mark.parametrize("meta_id", [None, "stale-other-id"])
def test_crashed_cli_baseline_source_uses_saved_report_identity(tmp_path, monkeypatch, meta_id):
    source = tmp_path / "custom-output"
    write_report(synthetic_report(2, 10, "all"), source)
    meta = {"id": source.name, "state": "running", "started_at": "2020-01-01T00:00:00Z",
            "owner_pid": 123456789, "owner_host": socket.gethostname()}
    if meta_id:
        meta["run_id"] = meta_id
    marker = source / ".web.json"
    marker.write_text(json.dumps(meta))
    os.utime(marker, (1, 1))
    (tmp_path / ".baselines").mkdir()
    (tmp_path / ".baselines" / "chosen.json").write_text('{"source_run_id":"synthetic"}')
    monkeypatch.setattr("giraffe.storage.os.kill", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    plan = cleanup_plan(tmp_path, older_than_days=30)
    assert not plan["candidates"]
    assert plan["protected"][0]["reason"] == "source run of a saved baseline"
    assert not apply_cleanup(tmp_path, [source.name], older_than_days=30)["deleted"]
    assert not automatic_cleanup(tmp_path, {"older_than_days": 30}, active_id="trigger")["deleted"]
    assert source.is_dir()


@pytest.mark.parametrize("schema", ["1", "2", "3"])
def test_four_generic_strings_are_not_giraffe_proof(tmp_path, schema):
    foreign = tmp_path / "coverage"
    foreign.mkdir()
    (foreign / "report.json").write_text(json.dumps({
        "run_id": "coverage", "started_at": "2020-01-01", "schema_version": schema,
        "suite_version": "foreign", "tests": []}))
    os.utime(foreign, (1, 1))
    assert cleanup_plan(tmp_path, older_than_days=0)["protected"][0]["reason"] == "not a Giraffe run"
    assert not automatic_cleanup(tmp_path, {"older_than_days": 0}, active_id="trigger")["deleted"]
    assert foreign.is_dir()


@pytest.mark.parametrize("failure", [False, True])
def test_successful_final_metadata_retry_clears_only_its_warning_and_cache(tmp_path, monkeypatch, failure):
    import giraffe.web as web

    async def runner(configuration, **kwargs):
        if failure:
            raise RuntimeError("hidden")
        return web_report(configuration)

    original, attempts = web._write, []

    def write(path, value):
        if path.name == ".web.json" and value.get("state") in {"completed", "error"}:
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(errno.ENOSPC, "disk full")
        return original(path, value)

    monkeypatch.setattr(web, "run_suite", runner)
    monkeypatch.setattr(web, "_write", write)
    with TestClient(create_app(tmp_path, min_free_disk=0), base_url="http://127.0.0.1:8787") as client:
        identifier = client.post("/api/runs", json={"config": config()}).json()["id"]
        detail = completed(client, identifier)
        marker = tmp_path / identifier / ".web.json"
        saved = json.loads(marker.read_text())
        assert len(attempts) == 2
        assert "Run status could not be saved" not in (saved.get("error") or "")
        assert "Run status could not be saved" not in (detail.get("error") or "")
        if failure:
            assert "RuntimeError" in saved["error"]
        saved["error"] = "read the current disk state"
        marker.write_text(json.dumps(saved))
        assert client.get(f"/api/runs/{identifier}").json()["error"] == saved["error"]


@pytest.mark.parametrize("extension", ["html", "json"])
async def test_stalled_client_does_not_hold_generation_lock(tmp_path, extension):
    write_report(synthetic_report(30, 1024, "all"), tmp_path / "saved")
    app = create_app(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_app(scope, receive, send):
        async def slow_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                entered.set()
                await release.wait()
            await send(message)
        await app(scope, receive, slow_send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(slow_app), base_url="http://127.0.0.1:8787") as slow, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787") as client:
        first = asyncio.create_task(slow.get(f"/api/runs/saved/report.{extension}"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = await asyncio.wait_for(client.get("/api/runs/saved/report.json"), 1)
            assert second.status_code == 200
            assert not first.done()
        finally:
            release.set()
            await first


async def test_repeated_cancellation_waits_once_for_thread_and_does_not_spin():
    import giraffe.web as web
    from starlette.concurrency import run_in_threadpool

    entered, release = threading.Event(), threading.Event()

    def work():
        entered.set()
        assert release.wait(2)
        return 42

    async def wait():
        return await web._finish_shielded(asyncio.create_task(run_in_threadpool(work)))

    task = asyncio.create_task(wait())
    assert await asyncio.to_thread(entered.wait, 1)
    for _ in range(20):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result, cancellation = await asyncio.wait_for(task, 1)
    assert result == 42 and isinstance(cancellation, asyncio.CancelledError)

    # AnyIO uses repeated level cancellation; the shield also protects that path.
    entered.clear()
    release.clear()
    results = []
    async with create_task_group() as group:
        async def run():
            results.append(await wait())
        group.start_soon(run)
        assert await asyncio.to_thread(entered.wait, 1)
        group.cancel_scope.cancel()
        with CancelScope(shield=True):
            await asyncio.sleep(.01)
            release.set()
    assert results[0][0] == 42


@pytest.mark.parametrize("extension", ["html", "json"])
def test_download_does_not_create_legacy_state_or_invent_existing_state(tmp_path, extension):
    source = tmp_path / "saved"
    write_report(synthetic_report(2, 10, "all"), source)
    marker = source / ".web.json"
    metadata = json.loads(marker.read_text())
    assert set(metadata) == {"latency_summary"}
    assert "state" not in metadata
    marker.unlink()  # A legacy report has no report-time observation metadata.
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        assert client.get(f"/api/runs/saved/report.{extension}").status_code == 200
        assert not marker.exists()
        marker.write_text('{"id":"saved","custom":"keep"}')
        assert client.get(f"/api/runs/saved/report.{extension}").status_code == 200
        saved = json.loads(marker.read_text())
        assert saved["custom"] == "keep" and "state" not in saved
        assert "download_" + extension in saved["resources"]["sampled_peak_rss_bytes"]


@pytest.mark.parametrize("value", ["a\\\"[]{}" * 300000, {"text": "a\\\"[]{}" * 300000}, [True, None, 1.2e40]])
def test_reader_decodes_each_large_value_once(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    reader = _Reader(io.StringIO(encoded + ","))
    calls = []
    decoder = json.JSONDecoder()

    class CountingDecoder:
        def raw_decode(self, text):
            calls.append(len(text))
            return decoder.raw_decode(text)

    reader.decoder = CountingDecoder()
    assert reader.value() == value
    assert calls == [len(encoded)]
    assert reader.peek() == ","


@pytest.mark.parametrize("size", [1, 7, 65536])
@pytest.mark.parametrize("value", ["x\\\"[]{}é", {"k": ["x\\\"", {"nested": "\\end"}, False]}, -1.2e20, None])
def test_reader_handles_escape_and_scalar_boundaries_without_rescanning(size, value):
    class Fragmented(io.StringIO):
        def read(self, n):
            return super().read(min(n, size))

    reader = _Reader(Fragmented(json.dumps(value) + ","))
    assert reader.value() == value
    assert reader.peek() == ","


async def test_execute_final_write_merges_download_samples_saved_during_cleanup(tmp_path, monkeypatch):
    import giraffe.web as web

    entered, release = threading.Event(), threading.Event()
    tasks = []
    original = web.automatic_cleanup

    async def runner(configuration, **kwargs):
        tasks.append(asyncio.current_task())
        return web_report(configuration)

    def cleanup(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(web, "run_suite", runner)
    monkeypatch.setattr(web, "automatic_cleanup", cleanup)
    app = create_app(tmp_path, min_free_disk=0, auto_cleanup={"keep_latest": 1})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787") as client:
            identifier = (await client.post("/api/runs", json={"config": config()})).json()["id"]
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                for extension in ("html", "json"):
                    assert (await client.get(f"/api/runs/{identifier}/report.{extension}")).status_code == 200
                marker = tmp_path / identifier / ".web.json"
                saved = json.loads(marker.read_text())
                assert saved["state"] == "completed"
                before = saved["resources"]["sampled_peak_rss_bytes"]
                assert {"export", "download_html", "download_json"} <= before.keys()
            finally:
                release.set()
                await tasks[0]
            saved = json.loads(marker.read_text())
            assert saved["resources"]["sampled_peak_rss_bytes"] == before
            assert "auto_cleanup" in saved


async def test_worker_error_does_not_lose_pending_cancellation():
    import giraffe.web as web
    from starlette.concurrency import run_in_threadpool

    entered, release = threading.Event(), threading.Event()

    def work():
        entered.set()
        assert release.wait(2)
        raise OSError(errno.ENOSPC, "disk full")

    async def wait():
        return await web._finish_shielded(asyncio.create_task(run_in_threadpool(work)))

    task = asyncio.create_task(wait())
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
