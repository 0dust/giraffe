"""Cancellation retains its evidence without consuming an older cancellation."""
import asyncio

import pytest

from giraffe import runner
from tests.test_runner import client as client, config
from tests.test_runner_stats_revision import VariableClient, spec


def _monitor_stop_report(monkeypatch, client, *, target_start_delay):
    from tests.test_review26_phase4_accuracy import run_virtual

    monkeypatch.setattr(client, "delay", 10)
    original_target, original_monitor = runner._target, runner._Run.monitor
    monitor_cancellations = []

    async def target(run, target, fixtures):
        await asyncio.sleep(target_start_delay)
        await original_target(run, target, fixtures)

    async def monitor(run, tasks):
        await original_monitor(run, tasks)
        monitor_cancellations.extend(task.cancelling() for task in tasks)

    monkeypatch.setattr(runner, "_target", target)
    monkeypatch.setattr(runner._Run, "monitor", monitor)

    async def run():
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert task.cancelling() == 1
        try:
            report = await runner.run_suite(config(max_duration_seconds=.2))
            assert report.abort_reason == "maximum run duration reached"
            assert report.observations["inference_duration_seconds"] == pytest.approx(.2)
            assert monitor_cancellations == [1]
            assert task.cancelling() == 1
            return report
        finally:
            task.uncancel()

    # Keep the native monitor and task cancellation, while CPU stalls during
    # setup cannot consume a test's entire 200 ms inference window.
    return run_virtual(monkeypatch, asyncio.wait_for(run(), 2))


def test_monitor_stop_preserves_preexisting_cancellation_count(monkeypatch, client):
    report = _monitor_stop_report(monkeypatch, client, target_start_delay=0)
    assert report.requests and all(r.status == "cancelled" and r.completed_ms is not None
                                   for r in report.requests)
    assert client.active == 0 and client.closed == ["service"]


def test_monitor_expiry_before_traffic_preserves_empty_report_and_older_cancellation(monkeypatch, client):
    report = _monitor_stop_report(monkeypatch, client, target_start_delay=.3)
    assert report.requests == []
    assert not client.scenarios and client.active == 0 and not client.closed


@pytest.mark.parametrize("preparation_delay", [0, .08])
async def test_new_timeout_preserves_preexisting_cancellation_count(monkeypatch, client, preparation_delay):
    from giraffe import deployment

    monkeypatch.setattr(client, "delay", 10)
    original_prepare = deployment.preparation_residency

    async def prepare(*args, **kwargs):
        await asyncio.sleep(preparation_delay)
        return await original_prepare(*args, **kwargs)

    monkeypatch.setattr(deployment, "preparation_residency", prepare)

    async def run():
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        try:
            with pytest.raises(TimeoutError) as timeout:
                async with asyncio.timeout(None) as deadline:
                    def progress(event):
                        if event["event"] == "request_started" and deadline.when() is None:
                            deadline.reschedule(asyncio.get_running_loop().time() + .05)
                    await runner.run_suite(config(), progress=progress)
            assert deadline.expired()
            report = timeout.value.__cause__.giraffe_report
            assert report.abort_reason == "run cancelled"
            assert report.requests and all(r.status == "cancelled" and r.completed_ms is not None
                                           for r in report.requests)
            assert client.active == 0 and client.closed == ["service"]
            assert task.cancelling() == 1
        finally:
            while task.cancelling():
                task.uncancel()

    await asyncio.wait_for(asyncio.create_task(run()), 2)


async def test_preparation_timeout_preserves_cancellation_without_inventing_a_report(monkeypatch, client):
    from giraffe import deployment

    async def run():
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        try:
            with pytest.raises(TimeoutError) as timeout:
                async with asyncio.timeout(None) as deadline:
                    async def prepare(*args, **kwargs):
                        deadline.reschedule(asyncio.get_running_loop().time())
                        await asyncio.Event().wait()
                    monkeypatch.setattr(deployment, "preparation_residency", prepare)
                    await runner.run_suite(config())
            assert deadline.expired()
            assert isinstance(timeout.value.__cause__, asyncio.CancelledError)
            assert not hasattr(timeout.value.__cause__, "giraffe_report")
            assert task.cancelling() == 1
            assert not client.scenarios and client.active == 0 and not client.closed
        finally:
            while task.cancelling():
                task.uncancel()

    await asyncio.wait_for(asyncio.create_task(run()), 2)


@pytest.mark.parametrize("event_name", ["request_completed", "run_stopping"])
async def test_emit_error_preserves_cancellation_and_attached_terminal_record(monkeypatch, caplog, event_name):
    def progress(event):
        if event["event"] == event_name:
            raise KeyError("broken progress callback")

    state = runner._Run(config(), progress, None)
    transport = VariableClient(suppress_cancel=True)
    finalized = []
    monkeypatch.setattr(state, "record_finished", finalized.append)
    task = asyncio.create_task(state.request(state.config.targets[0], transport, spec()))
    await asyncio.sleep(.002)
    if event_name == "run_stopping":
        state.stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    record = cancelled.value.giraffe_record
    assert state.records == finalized == [record]
    assert record.status == "cancelled" and record.completed_ms is not None
    assert transport.active == 0
    assert any(entry.exc_info and "progress" in entry.message.lower() for entry in caplog.records)


async def test_final_emit_error_preserves_cancelled_partial_report(monkeypatch, caplog, client):
    monkeypatch.setattr(client, "delay", 10)
    started = asyncio.Event()

    def progress(event):
        if event["event"] == "request_started":
            started.set()
        if event["event"] == "run_finished":
            raise KeyError("broken final callback")

    task = asyncio.create_task(runner.run_suite(config(), progress=progress))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await asyncio.wait_for(task, 1)
    report = cancelled.value.giraffe_report
    assert report.abort_reason == "run cancelled"
    assert report.requests and all(record.status == "cancelled" for record in report.requests)
    assert any(entry.exc_info and "progress" in entry.message.lower() for entry in caplog.records)


async def test_child_target_cancellation_explains_stop_and_joins_siblings(monkeypatch, client):
    monkeypatch.setattr(client, "delay", 10)
    sibling_started = asyncio.Event()
    original = runner._target
    events = []

    async def target(run, target, fixtures):
        if target.name == "child":
            await sibling_started.wait()
            raise asyncio.CancelledError
        await original(run, target, fixtures)

    def progress(event):
        events.append(event)
        if event["event"] == "request_started" and event.get("target") == "sibling":
            sibling_started.set()

    monkeypatch.setattr(runner, "_target", target)
    settings = config(targets=[{"name": name, "model": "local", "url": "http://server"}
                               for name in ("child", "sibling")], overlap_models=True)
    report = await asyncio.wait_for(runner.run_suite(settings, progress=progress), 1)
    assert report.abort_reason == "run cancelled"
    assert report.requests and all(record.target == "sibling" and record.status == "cancelled"
                                   and record.completed_ms is not None for record in report.requests)
    assert client.active == 0 and client.closed == ["sibling"]
    assert [event["abort_reason"] for event in events if event["event"] == "run_stopping"] == ["run cancelled"]
