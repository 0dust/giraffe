"""Host routing and finalization budget regressions at the HTTP boundary."""

import asyncio
import json

import httpx
import pytest

from giraffe import web
from giraffe.models import RunConfig
from tests.test_review26_phase4_accuracy import run_virtual
from tests.test_web import config, report, start


@pytest.fixture
def web_auto_auth():
    return False


async def test_public_bind_rejects_nonlocal_host_after_auth_routing(tmp_path):
    app = web.create_app(tmp_path, token="launch-token", host="0.0.0.0")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://shared.example:8787") as client:
        response = await client.get("/static/app.js")
        assert response.status_code == 400
        assert response.json() == {"detail": "Use a local Giraffe host"}
        authenticated = await client.get("/static/app.js", headers={
            "X-Giraffe-Token": "launch-token"})
        assert authenticated.status_code == 200
        assert "javascript" in authenticated.headers["content-type"]


def test_finalization_budget_releases_run_and_preserves_saved_report(tmp_path, monkeypatch, caplog):
    configuration = RunConfig.model_validate(config(checks=["serving"]))

    class Writer:
        dropped_count = 0
        stopped = False

        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            self.stopped = True

    class Heartbeat:
        def __init__(self, directory):
            pass

        def start(self):
            return self

        def close(self):
            pass

    async def scenario():
        closing = asyncio.Event()
        blocked = asyncio.Event()
        workers = []
        real_create_task = asyncio.create_task

        def capture_task(coroutine, *args, **kwargs):
            task = real_create_task(coroutine, *args, **kwargs)
            if coroutine.__qualname__.endswith(".<locals>.execute"):
                workers.append(task)
            return task

        async def inline(function, *args, **kwargs):
            if isinstance(getattr(function, "__self__", None), Heartbeat) and function.__name__ == "close":
                closing.set()
                await blocked.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(web, "run_in_threadpool", inline)
        monkeypatch.setattr(asyncio, "create_task", capture_task)
        app = web.create_app(tmp_path, token="launch-token", auto_cleanup={})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost:8787",
                                     headers={"X-Giraffe-Token": "launch-token"}) as client:
            identifier = await start(client, configuration.model_dump(mode="json"))
            await closing.wait()
            began = asyncio.get_running_loop().time()
            await workers[0]
            assert asyncio.get_running_loop().time() - began == web._FINALIZE_SECONDS
            saved = json.loads((tmp_path / identifier / "report.json").read_text())
            assert saved["overall"] == "pass"
            status = (await client.get(f"/api/runs/{identifier}")).json()
            assert status["state"] == "completed"
            assert (await client.get("/api/bootstrap")).json()["active_run_id"] is None
            events = (await client.get(f"/api/runs/{identifier}/events")).json()
            assert events["state"] == "completed"
            assert any(row["event"] == "run_finished" for row in events["events"])
        assert "Run finalization exceeded its time limit" in caplog.text

    monkeypatch.setattr(web, "EvidenceWriter", Writer)
    monkeypatch.setattr(web, "Heartbeat", Heartbeat)
    monkeypatch.setattr(web._RunThread, "start", lambda worker: worker.future.set_result(
        (report(configuration), None)))
    run_virtual(monkeypatch, scenario())
