"""Sustained dispatch and terminal records at cancellation boundaries."""
import asyncio
import time
import uuid

import httpx
import pytest

from giraffe import runner
from giraffe.models import RequestRecord, RequestSpec
from tests.test_runner import client as client, config, judged_pack_config


def spec(index=0):
    return RequestSpec(fixture_id=f"case-{index}", scenario="warm", check_ids=["serving"],
                       messages=[{"role": "user", "content": str(index)}])


class VariableClient:
    def __init__(self, *, failure=None, suppress_cancel=False):
        self.active = 0
        self.peak = 0
        self.events = []
        self.order = []
        self.failure = failure
        self.suppress_cancel = suppress_cancel

    async def execute(self, request, **kwargs):
        index = len(self.order)
        self.order.append(request.fixture_id)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.events.append((time.monotonic(), self.active))
        status = "completed"
        try:
            if self.failure and index == 1:
                raise self.failure
            await asyncio.sleep(.06 if index % 5 == 0 else .008)
        except asyncio.CancelledError:
            if not self.suppress_cancel:
                raise
            status = "cancelled"
        finally:
            self.active -= 1
            self.events.append((time.monotonic(), self.active))
        return RequestRecord(id=uuid.uuid4().hex, target="service", fixture_id=request.fixture_id,
                             scenario=request.scenario, check_ids=request.check_ids,
                             started_at=runner._now(), status=status, valid=status == "completed")


async def test_group_refills_each_slot_and_preserves_spec_order():
    state = runner._Run(config(checks=["correctness"], concurrency=3), None, None)
    transport = VariableClient()
    records = await state.group(state.config.targets[0], transport, [spec(i) for i in range(4)],
                                "warm", count=40, concurrency=3, checks=["serving"])
    assert len(records) == 40 and transport.peak == 3 and transport.active == 0
    assert transport.order == [f"case-{i % 4}" for i in range(40)]
    # Exclude the final drain. Slow requests must not hold up the other two slots.
    events = transport.events[:77]
    duration = events[-1][0] - events[0][0]
    full = sum(b[0] - a[0] for a, b in zip(events, events[1:]) if a[1] == 3)
    assert full / duration > .85


@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_cancel_group_propagates_after_terminal_records(monkeypatch, suppress_cancel):
    state = runner._Run(config(checks=["correctness"], concurrency=3), None, None)
    transport = VariableClient(suppress_cancel=suppress_cancel)
    finalized = []
    monkeypatch.setattr(state, "record_finished", finalized.append)
    task = asyncio.create_task(state.group(state.config.targets[0], transport, [spec()],
                                           "warm", count=100, concurrency=3, checks=["serving"]))
    await asyncio.sleep(.002)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert len(transport.order) == len(state.records) == len(finalized) == 3
    assert all(record.status == "cancelled" and record.completed_ms is not None for record in state.records)
    assert all(record.intentional is False for record in finalized)
    assert transport.active == 0
    await asyncio.sleep(.01)
    assert len(transport.order) == 3


async def test_group_failure_leaves_no_live_siblings():
    state = runner._Run(config(checks=["correctness"], concurrency=3), None, None)
    transport = VariableClient(failure=KeyError("broken implementation"))
    with pytest.raises(KeyError):
        await state.group(state.config.targets[0], transport, [spec()], "warm", count=12,
                          concurrency=3, checks=["serving"])
    assert transport.active == 0
    count = len(state.records)
    await asyncio.sleep(.08)
    assert len(state.records) == count


@pytest.mark.parametrize("error", [KeyError("secret"), AttributeError("secret"), ValueError("bug")])
async def test_unexpected_target_errors_are_logged_and_blocked(monkeypatch, caplog, error):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise error
    monkeypatch.setattr(runner, "LLMClient", Broken)
    report = await runner.run_suite(config())
    assert report.overall == "blocked"
    result = next(check for check in report.checks if check.id == "serving")
    assert result.summary == f"Internal error in Giraffe ({type(error).__name__}); see the Giraffe log."
    assert any(record.exc_info for record in caplog.records)
    assert "secret" not in result.summary


@pytest.mark.parametrize("error", [httpx.ConnectError("endpoint unavailable"),
    ValueError("API key environment variable MISSING is not set")])
async def test_expected_setup_errors_remain_setup_failures(monkeypatch, caplog, error):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise error
    monkeypatch.setattr(runner, "LLMClient", Broken)
    report = await runner.run_suite(config())
    result = next(check for check in report.checks if check.id == "serving")
    assert result.summary.startswith("Client setup failed (")
    assert not any(record.exc_info for record in caplog.records)


async def test_stop_event_closes_inflight_requests_within_one_second(monkeypatch):
    from tests.test_runner import FakeClient
    monkeypatch.setattr(FakeClient, "delay", 10)
    stop = asyncio.Event()
    started = asyncio.Event()
    def progress(event):
        if event["event"] == "request_started":
            started.set()
    task = asyncio.create_task(runner.run_suite(config(), stop_event=stop, progress=progress))
    await asyncio.wait_for(started.wait(), 1)
    stop.set()
    report = await asyncio.wait_for(task, 1)
    assert report.abort_reason == "cancelled by user"
    assert report.requests and all(record.status == "cancelled" for record in report.requests)
    assert FakeClient.active == 0


@pytest.mark.parametrize("external_cancel", [False, True])
async def test_judging_cannot_wait_unbounded_after_stop(monkeypatch, external_cancel):
    from giraffe import judging
    started = asyncio.Event()
    finished = asyncio.Event()
    async def uncooperative(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()  # Ignores stop_event, but honors task cancellation.
        finally:
            finished.set()
    monkeypatch.setattr(judging, "judge_records", uncooperative)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_suite(judged_pack_config(), stop_event=stop))
    await asyncio.wait_for(started.wait(), 1)
    if external_cancel:
        task.cancel()
        # Task cancellation propagates after the partial report is finalized;
        # event-based user stops retain the normal return path.
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, .9)
        report = cancelled.value.giraffe_report
        assert task.cancelling() == 0
    else:
        stop.set()
        report = await asyncio.wait_for(task, .9)
    assert report.abort_reason == ("run cancelled" if external_cancel else "cancelled by user")
    assert finished.is_set()


async def test_web_user_stop_completes_saved_partial_report(tmp_path, monkeypatch, client):
    from tests.test_web import browser, finished, start
    monkeypatch.setattr(client, "delay", 10)
    async with browser(tmp_path) as browser_client:
        identifier = await start(browser_client, config().model_dump(mode="json"))
        async with asyncio.timeout(1):
            while not client.active:
                await asyncio.sleep(.001)
        response = await browser_client.post(f"/api/runs/{identifier}/cancel", json={})
        assert response.status_code == 200
        detail = await asyncio.wait_for(finished(browser_client, identifier), 1)
        assert detail["state"] == "completed"
        assert detail["report"]["abort_reason"] == "cancelled by user"
        assert all(record["status"] == "cancelled" for record in detail["report"]["requests"])
        assert client.active == 0


async def test_enclosing_timeout_propagates_after_cancelling_group():
    state = runner._Run(config(checks=["correctness"], concurrency=3), None, None)
    transport = VariableClient(suppress_cancel=True)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.002):
            await state.group(state.config.targets[0], transport, [spec()], "warm", count=100,
                              concurrency=3, checks=["serving"])
    assert len(state.records) == len(transport.order) == 3
    assert transport.active == 0 and all(record.status == "cancelled" for record in state.records)


async def test_unexpected_request_error_is_internal_not_endpoint_failure(monkeypatch, caplog, client):
    async def broken(self, spec, **kwargs):
        raise KeyError("implementation defect")
    monkeypatch.setattr(client, "execute", broken)
    report = await runner.run_suite(config())
    assert report.overall == "blocked"
    assert len(report.requests) == 1 and report.requests[0].status == "failed"
    assert report.checks[1].summary == "Internal error in Giraffe (KeyError); see the Giraffe log."
    assert any(record.exc_info for record in caplog.records)


async def test_unexpected_cleanup_error_is_logged_and_blocked(monkeypatch, caplog):
    from giraffe import telemetry
    def broken(*args):
        raise KeyError("cleanup defect")
    monkeypatch.setattr(telemetry, "summarize", broken)
    report = await runner.run_suite(config())
    assert report.overall == "blocked"
    result = next(check for check in report.checks if check.id == "serving")
    assert result.summary == "Internal error in Giraffe (KeyError); see the Giraffe log."
    assert any(record.exc_info for record in caplog.records)
