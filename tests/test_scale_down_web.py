"""Operator markers cross the real measurement-thread boundary."""
import asyncio
import json
import threading
import time

import pytest

from giraffe.scale_down import ScaleDownController
from giraffe.web import _RunThread


def marker(control, **updates):
    return dict(event_id="remove-one", nonce=control.nonce, target="local", source="operator", **updates)


async def test_marker_is_accepted_on_worker_loop():
    ready = threading.Event()
    observed = []
    control = None
    async def operation(stop):
        nonlocal control
        control = ScaleDownController("report-id", "local", run_start=time.monotonic(), stop=stop)
        control.set_phase("armed")
        original = control.accept_marker
        def accept(value):
            observed.append(threading.get_ident())
            return original(value)
        control.accept_marker = accept
        worker.bind_scale_down_control(control)
        ready.set()
        await stop.wait()
    worker = _RunThread(operation)
    worker.start()
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        ack = await worker.submit_scale_down_marker(marker(control), timeout=.5)
        assert ack["accepted"] and ack["marker"]["nonce"] == control.nonce
        assert observed == [worker.thread.ident]
        assert observed[0] != threading.get_ident()
        assert await worker.submit_scale_down_marker(marker(control), timeout=.5) == ack
        conflict = marker(control)
        conflict["event_id"] = "other"
        assert not (await worker.submit_scale_down_marker(conflict, timeout=.5))["accepted"]
    finally:
        worker.request_stop()
        await asyncio.wrap_future(worker.future)
        await asyncio.to_thread(worker.owner.close)


async def test_timed_out_delivery_cannot_later_accept():
    ready, block, release = threading.Event(), threading.Event(), threading.Event()
    control = None
    async def operation(stop):
        nonlocal control
        control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=stop)
        control.set_phase("armed")
        worker.bind_scale_down_control(control)
        ready.set()
        await stop.wait()
    worker = _RunThread(operation)
    worker.start()
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        def blocked():
            block.set()
            release.wait(2)
        worker.loop.call_soon_threadsafe(blocked)
        assert await asyncio.to_thread(block.wait, 2)
        with pytest.raises(TimeoutError):
            await worker.submit_scale_down_marker(marker(control), timeout=.01)
        release.set()
        await asyncio.sleep(.03)
        assert control.status()["acknowledgment"] is None
    finally:
        release.set()
        worker.request_stop()
        await asyncio.wrap_future(worker.future)
        await asyncio.to_thread(worker.owner.close)


@pytest.mark.parametrize("phase,change,reason", [
    ("baseline", {}, "not armed"),
    ("armed", {"nonce": "old-run"}, "active scenario"),
    ("armed", {"target": "other"}, "active scenario"),
    ("finalizing", {}, "stopping or finished"),
    ("finished", {}, "stopping or finished"),
])
async def test_worker_rejects_inapplicable_markers(phase, change, reason):
    ready = threading.Event()
    control = None
    async def operation(stop):
        nonlocal control
        control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=stop)
        control.set_phase(phase)
        worker.bind_scale_down_control(control)
        ready.set()
        await stop.wait()
    worker = _RunThread(operation)
    worker.start()
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        ack = await worker.submit_scale_down_marker({**marker(control), **change}, timeout=.5)
        assert not ack["accepted"] and reason in ack["reason"]
        assert control.status()["acknowledgment"] is None
    finally:
        worker.request_stop()
        await asyncio.wrap_future(worker.future)
        await asyncio.to_thread(worker.owner.close)


async def test_stop_racing_queued_marker_rejects():
    ready, blocked, release = threading.Event(), threading.Event(), threading.Event()
    control = None
    async def operation(stop):
        nonlocal control
        control = ScaleDownController("report", "local", run_start=time.monotonic(), stop=stop)
        control.set_phase("armed")
        worker.bind_scale_down_control(control)
        ready.set()
        await stop.wait()
    worker = _RunThread(operation)
    worker.start()
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        def pause():
            blocked.set()
            release.wait(2)
        worker.loop.call_soon_threadsafe(pause)
        assert await asyncio.to_thread(blocked.wait, 2)
        pending = asyncio.create_task(worker.submit_scale_down_marker(marker(control), timeout=.5))
        await asyncio.sleep(.01)
        worker.request_stop()
        release.set()
        assert not (await pending)["accepted"]
        assert control.status()["acknowledgment"] is None
    finally:
        release.set()
        worker.request_stop()
        await asyncio.wrap_future(worker.future)
        await asyncio.to_thread(worker.owner.close)


@pytest.mark.usefixtures("local_tokenizer")
async def test_api_marker_ack_status_and_job_identity(tmp_path, monkeypatch):
    from tests.test_web import browser, config, detail_when, finished, report, start
    observed = []
    async def measured(configuration, *, progress, stop_event, on_scale_down_control):
        control = ScaleDownController("different-report-id", "local", run_start=time.monotonic(), stop=stop_event,
            on_change=lambda status: progress(dict(event="scale_down_updated", target="local",
                scale_down_control=status, scale_down={}, scale_down_snapshots=[])))
        original = control.accept_marker
        def accept(value):
            observed.append(threading.get_ident())
            return original(value)
        control.accept_marker = accept
        on_scale_down_control(control)
        control.set_phase("armed")
        # Later request progress must not erase the actionable control status.
        progress(dict(event="request_started", target="local", request_slot=0, scenario="warm"))
        await stop_event.wait()
        control.set_phase("finished")
        return report(configuration)
    monkeypatch.setattr("giraffe.web.run_suite", measured)
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", .01)
    values = config(checks=["scale_down"], scale_down={"target": "local"})
    async with browser(tmp_path) as client:
        identifier = await start(client, values)
        detail = await detail_when(client, identifier, lambda d: d.get("scale_down_control"))
        status = detail["scale_down_control"]
        assert status["run_id"] == "different-report-id" != identifier
        events_path = f"/api/runs/{identifier}/events"
        initial = (await client.get(events_path)).json()
        control_cursor = max(event["seq"] for event in initial["events"] if "scale_down_control" in event)
        unrelated = (await client.get(events_path + f"?after={control_cursor}")).json()
        assert unrelated["events"] and all("scale_down_control" not in event for event in unrelated["events"])
        assert unrelated["scale_down_control"] == status
        idle = (await client.get(events_path + f"?after={unrelated['seq']}")).json()
        assert idle["events"] == [] and idle["scale_down_control"] == status
        path = f"/api/runs/{identifier}/scale-down/event"
        payload = dict(event_id="remove", nonce=status["nonce"], target="local", source="operator")
        assert (await client.post(path, json={**payload, "event_id": "x" * 257})).status_code == 422
        assert (await client.post(path, json=payload, headers={"Origin": "https://elsewhere.example"})).status_code == 403
        assert (await client.post(path, content="{}")).status_code == 415
        assert (await client.post(path, json=payload, headers={"X-Giraffe-Token": "wrong"})).status_code == 401
        wrong = await client.post(path, json={**payload, "nonce": "old"})
        assert wrong.status_code == 200 and not wrong.json()["accepted"]
        accepted = await client.post(path, json=payload)
        assert accepted.status_code == 200 and accepted.json()["accepted"]
        assert (await client.post(path, json=payload)).json() == accepted.json()
        assert (await client.post("/api/runs/different-report-id/scale-down/event", json=payload)).status_code == 409
        detail = await detail_when(client, identifier, lambda d: (d.get("scale_down_control") or {}).get("acknowledgment"))
        assert detail["scale_down_control"]["acknowledgment"] == accepted.json()
        assert all(identity != threading.get_ident() for identity in observed)
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        terminal = await finished(client, identifier)
        assert terminal["scale_down_control"]["phase"] == "finished"
        terminal_feed = (await client.get(events_path + f"?after={terminal['seq']}")).json()
        assert terminal_feed["events"] == []
        assert terminal_feed["scale_down_control"] == terminal["scale_down_control"]
        assert (await client.post(path, json=payload)).status_code == 409


@pytest.mark.parametrize("state,phase", [("running", "armed"), ("completed", "finished")])
async def test_standalone_event_feed_reads_current_control_file(tmp_path, monkeypatch, state, phase):
    from tests.test_web import browser
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", .01)
    async with browser(tmp_path) as client:
        directory = tmp_path / "standalone"
        directory.mkdir()
        (directory / ".web.json").write_text(json.dumps(dict(id=directory.name, state=state, run_id="report-run", seq=3,
            config={"scale_down": {"target": "local"}})))
        status = dict(run_id="report-run", target="local", nonce="current", phase=phase, acknowledgment=None)
        (directory / ".scale-down-status.json").write_text(json.dumps(status))
        detail = (await client.get("/api/runs/standalone")).json()
        assert detail["scale_down_control"] == status
        events = (await client.get("/api/runs/standalone/events?after=3")).json()
        assert events["events"] == [] and events["scale_down_control"] == status
        assert events.get("idle", False) is (state == "running")


@pytest.mark.parametrize("invalid", ["run", "target", "historical", "symlink", "oversize"])
async def test_standalone_feed_does_not_expose_unbound_control(tmp_path, monkeypatch, invalid):
    from tests.test_web import browser
    monkeypatch.setattr("giraffe.web._EVENT_WAIT_SECONDS", .01)
    async with browser(tmp_path) as client:
        directory = tmp_path / "standalone"
        directory.mkdir()
        (directory / ".web.json").write_text(json.dumps(dict(id=directory.name, state="completed", run_id="current-run", seq=3,
            config={"scale_down": {"target": "local"}})))
        status = dict(run_id="old-run" if invalid == "run" else "current-run",
            target="other" if invalid == "target" else "local", nonce="nonce",
            phase="armed" if invalid == "historical" else "finished", acknowledgment=None)
        path = directory / ".scale-down-status.json"
        if invalid == "symlink":
            outside = tmp_path / "private.json"
            outside.write_text(json.dumps(status))
            path.symlink_to(outside)
        elif invalid == "oversize":
            path.write_text(json.dumps({**status, "extra": "x" * 65536}))
        else:
            path.write_text(json.dumps(status))
        events = (await client.get("/api/runs/standalone/events?after=3")).json()
        assert events["events"] == [] and events["scale_down_control"] is None
