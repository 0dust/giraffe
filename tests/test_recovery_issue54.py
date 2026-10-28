"""Restart resolves dead writers once heartbeat grace has elapsed."""

from datetime import datetime, timezone
import json
import socket
import subprocess
import sys

import httpx
import pytest

from giraffe.models import RunConfig
from giraffe.reporting import write_report
from giraffe.web import create_app
from tests.test_web import config, report


@pytest.mark.parametrize("state", ["running", "stopping"])
@pytest.mark.parametrize("has_report", [False, True])
async def test_immediate_restart_recovers_dead_writer_in_history_and_cancel(tmp_path, state, has_report):
    # Reap a real dead owner and place its heartbeat outside the liveness grace.
    with subprocess.Popen([sys.executable, "-c", "pass"]) as owner:
        owner.wait(timeout=5)
    directory = tmp_path / "crashed"
    if has_report:
        write_report(report(RunConfig.model_validate(config())), directory)
    else:
        directory.mkdir()
    marker = directory / ".web.json"
    marker.write_text(json.dumps({
        "id": "crashed", "state": state, "owner_pid": owner.pid,
        "owner_host": socket.gethostname(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_at": "2000-01-01T00:00:00Z",
    }))
    expected = "completed" if has_report else "interrupted"
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            detail = (await client.get("/api/runs/crashed")).json()
            assert detail["state"] == expected
            assert (detail["report"] is not None) is has_report
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["active_run_id"] is None
            history = (await client.get("/api/runs")).json()
            assert history["active_run_id"] is None
            assert [(row["id"], row["state"]) for row in history["runs"]] == [("crashed", expected)]
            cancelled = await client.post("/api/runs/crashed/cancel", json={})
            assert cancelled.status_code == 200
            assert cancelled.json() == {"id": "crashed", "state": expected}
            baseline = await client.post("/api/baselines", json={"run_id": "crashed", "name": "Recovered"})
            assert baseline.status_code == (201 if has_report else 409)
            plan = (await client.post("/api/storage/cleanup-preview", json={"keep_latest": 0})).json()
            if has_report:
                assert plan["candidates"] == []
                assert plan["protected"][0]["protected_reason"] == "source run of a saved baseline"
            else:
                assert [row["id"] for row in plan["candidates"]] == ["crashed"]
    assert json.loads(marker.read_text())["state"] == expected


async def test_fresh_heartbeat_retains_grace_then_recovers_dead_owner(tmp_path, monkeypatch):
    import asyncio
    from giraffe import web
    from giraffe.storage import write_metadata
    with subprocess.Popen([sys.executable, '-c', 'pass']) as owner:
        owner.wait(timeout=5)
    directory = tmp_path / 'fresh-crash'
    meta = {'id': directory.name, 'state': 'running', 'owner_pid': owner.pid,
            'owner_host': socket.gethostname(), 'heartbeat_at': web._now()}
    write_metadata(directory, meta)
    monkeypatch.setattr(web, '_RECOVERY_SECONDS', .01)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            assert (await client.get('/api/runs/fresh-crash')).json()['state'] == 'running'
            bootstrap = (await client.get('/api/bootstrap')).json()
            assert bootstrap['active_run_id'] is None
            assert bootstrap['disk_active_id'] == 'fresh-crash'
            write_metadata(directory, meta | {'heartbeat_at': '2000-01-01T00:00:00Z'})
            for _ in range(100):
                detail = (await client.get('/api/runs/fresh-crash')).json()
                if detail['state'] == 'interrupted':
                    break
                await asyncio.sleep(.01)
            assert detail['state'] == 'interrupted'
            assert (await client.get('/api/bootstrap')).json()['active_run_id'] is None
