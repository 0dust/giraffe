"""Server admission precedence and concurrent HTTP request boundaries."""

import asyncio
from inspect import getclosurevars
from itertools import combinations
import threading

import pytest

from giraffe import states, web
from giraffe.evidence import EvidenceWriter
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import write_report
from giraffe.storage import write_metadata
from tests.test_evidence import record
from tests.test_web import browser, config, finished, report, start, successful_runner

pytestmark = pytest.mark.usefixtures("local_tokenizer")

OPERATIONS = ("start_run", "test_connection", "tokenizer_preview", "compare", "recovery")


def reference_conflict(operation, activities):
    if "run_active" in activities:
        return "run_active"
    if operation in {"test_connection", "recovery"} and "run_starting" in activities:
        return "run_starting"
    if operation in {"start_run", "recovery"} and "run_stopping" in activities:
        return "run_stopping"
    if operation in {"start_run", "test_connection"} and "connection_test_running" in activities:
        return "connection_test_running"
    return None


@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_activity_subset(operation):
    assert states.SERVER_ACTIVITIES == (
        "run_active", "run_starting", "run_stopping", "connection_test_running")
    for size in range(len(states.SERVER_ACTIVITIES) + 1):
        for subset in combinations(states.SERVER_ACTIVITIES, size):
            assert states.admission_conflict(operation, frozenset(subset)) == reference_conflict(operation, subset)


def test_unknown_operation():
    with pytest.raises(KeyError):
        states.admission_conflict("unknown", frozenset())


def test_recovery_stays_pending_while_active_then_recovers_when_idle(tmp_path):
    directory = tmp_path / "interrupted"
    write_metadata(directory, {"id": directory.name, "state": states.RUN_INTERRUPTED, "config": config()})
    writer = EvidenceWriter(directory, manifest={"config": config()})
    row = record()
    writer.record_finished(row)
    writer.close()
    journal = directory / "evidence.jsonl"
    evidence = journal.read_bytes()
    marker = directory / ".web.json"
    metadata = marker.read_bytes()

    app = web.create_app(tmp_path)
    lifespan = getclosurevars(app.router.lifespan_context.__wrapped__).nonlocals
    recover = lifespan["recover_interrupted"]
    active = lifespan["active"]
    active["id"] = "active"
    try:
        assert recover() is True
        assert not (directory / "report.json").exists()
        assert journal.read_bytes() == evidence
        assert marker.read_bytes() == metadata
    finally:
        active.clear()

    assert recover() is False
    recovered = RunReport.model_validate_json((directory / "report.json").read_text())
    assert recovered.run_id == directory.name
    assert recovered.observations["interrupted"] is True
    assert [request.id for request in recovered.requests] == [row.id]
    assert not journal.exists()


def conflict(response, code, detail):
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": detail}
    assert response.headers["X-Giraffe-Error-Code"] == code


async def connection(client):
    return await client.post("/api/targets/test-connection", json={"url": "http://localhost/v1", "model": "test"})


@pytest.fixture
def connection_ok(monkeypatch):
    async def check(body):
        return {"status": "ok", "model": body.model}
    monkeypatch.setattr(web, "check_connection", check)


async def test_active_run_conflicts(tmp_path, monkeypatch):
    entered = threading.Event()

    async def waiting(configuration, *, progress, stop_event):
        entered.set()
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr(web, "run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            conflict(await client.post("/api/runs", json={"config": config()}), "run_active",
                     "A run is already active; stop it or wait for its report")
            conflict(await connection(client), "run_active",
                     "A run is active. Wait for it to finish or stop it before testing another connection.")
            conflict(await client.post("/api/targets/tokenizer", json={"token_id": "builtin"}), "run_active",
                     "A run is active. Wait for it to finish or stop it before preparing another tokenizer.")
            conflict(await client.post(f"/api/runs/{identifier}/compare", json={"baseline_id": "saved"}),
                     "run_active", "Wait for the active run to finish before comparing saved reports")
        finally:
            await client.post(f"/api/runs/{identifier}/cancel", json={})
            await finished(client, identifier)


async def test_connection_reservation_and_tokenizer_nonblock(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(body):
        entered.set()
        await release.wait()
        return {"status": "ok", "model": body.model}

    monkeypatch.setattr(web, "check_connection", held)
    async with browser(tmp_path) as client:
        probe = asyncio.create_task(connection(client))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            conflict(await connection(client), "connection_test_running",
                     "A connection test is already running. Wait for its result.")
            conflict(await client.post("/api/runs", json={"config": config()}), "connection_test_running",
                     "A connection test is running. Wait for its result before starting a run.")
            preview = await client.post("/api/targets/tokenizer", json={"token_id": "builtin"})
            assert preview.status_code == 200, preview.text
            assert preview.json()["tokenizer"] == "builtin"
        finally:
            release.set()
            assert (await probe).status_code == 200


async def test_connection_conflicts_while_start_prepares(tmp_path, monkeypatch, connection_ok):
    entered, release = asyncio.Event(), asyncio.Event()

    async def preparing(configuration):
        entered.set()
        await release.wait()

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", preparing)
    monkeypatch.setattr(web, "run_suite", successful_runner)
    async with browser(tmp_path) as client:
        beginning = asyncio.create_task(client.post("/api/runs", json={"config": config()}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            conflict(await connection(client), "run_starting",
                     "A run is active. Wait for it to finish or stop it before testing another connection.")
        finally:
            release.set()
            response = await beginning
            assert response.status_code == 202, response.text
            await finished(client, response.json()["id"])
        assert (await connection(client)).status_code == 200


async def test_cancelled_start_releases_starting(tmp_path, monkeypatch, connection_ok):
    entered = asyncio.Event()

    async def preparing(configuration):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", preparing)
    async with browser(tmp_path) as client:
        beginning = asyncio.create_task(client.post("/api/runs", json={"config": config()}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            beginning.cancel()
            with pytest.raises(asyncio.CancelledError):
                await beginning
        assert (await connection(client)).status_code == 200


@pytest.mark.parametrize("failure", ["validation", "storage", "tokenizer", "metadata"])
async def test_failed_start_releases_starting(tmp_path, monkeypatch, connection_ok, failure):
    values = config()
    expected = 422
    if failure == "validation":
        values["custom_fixtures"] = "/missing/admission-fixtures.json"
        values["checks"] = ["correctness"]
    elif failure == "storage":
        def full(*args):
            raise ValueError("disk full")
        monkeypatch.setattr(web, "check_start", full)
        expected = 507
    elif failure == "tokenizer":
        from giraffe.local_tokenizer import TokenizerUnavailable
        async def unavailable(configuration):
            raise TokenizerUnavailable("Missing tokenizer")
        monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", unavailable)
    else:
        def unwritable(path, value):
            raise OSError("metadata unavailable")
        monkeypatch.setattr(web, "_write", unwritable)
        expected = 500
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == expected, response.text
        assert (await connection(client)).status_code == 200


@pytest.mark.parametrize("operation", ["cleanup", "compare"])
async def test_connection_allowed_during_storage_work(tmp_path, monkeypatch, connection_ok, operation):
    entered, release = threading.Event(), threading.Event()
    original = web.apply_cleanup if operation == "cleanup" else web.compare_baseline

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    async with browser(tmp_path) as client:
        if operation == "cleanup":
            monkeypatch.setattr(web, "apply_cleanup", held)
            path, body = "/api/storage/cleanup", {"ids": []}
        else:
            for identifier in ("before", "after"):
                value = report(RunConfig.model_validate(config()))
                value.run_id = identifier
                write_report(value, tmp_path / identifier)
            saved = await client.post("/api/baselines", json={"run_id": "before", "name": "Before"})
            assert saved.status_code == 201, saved.text
            monkeypatch.setattr(web, "compare_baseline", held)
            path, body = "/api/runs/after/compare", {"baseline_id": saved.json()["id"]}
        storage = asyncio.create_task(client.post(path, json=body))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            response = await connection(client)
            assert response.status_code == 200, response.text
        finally:
            release.set()
            response = await storage
            assert response.status_code == 200, response.text


async def test_stopping_blocks_start_but_allows_read_only_probes(tmp_path, monkeypatch, connection_ok):
    tasks = []
    entered, stopping, release = threading.Event(), threading.Event(), threading.Event()
    original = web._RunThread.start

    def capture(worker):
        tasks.append(asyncio.current_task())
        original(worker)

    async def held(configuration, *, progress, stop_event):
        entered.set()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            stopping.set()
            assert await asyncio.to_thread(release.wait, 5)
        return report(configuration)

    monkeypatch.setattr(web, "_PARTIAL_REPORT_SECONDS", .01)
    monkeypatch.setattr(web._RunThread, "start", capture)
    monkeypatch.setattr(web, "run_suite", held)
    async with browser(tmp_path) as client:
        await start(client)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            tasks[0].cancel()
            assert await asyncio.to_thread(stopping.wait, 2)
            await asyncio.wait_for(asyncio.gather(tasks[0], return_exceptions=True), 3)
            conflict(await client.post("/api/runs", json={"config": config()}), "run_stopping",
                     "Previous run still stopping; wait for it to finish")
            assert (await connection(client)).status_code == 200
            preview = await client.post("/api/targets/tokenizer", json={"token_id": "builtin"})
            assert preview.status_code == 200, preview.text
        finally:
            release.set()
