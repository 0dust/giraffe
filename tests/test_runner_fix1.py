"""FIX1 lifecycle boundaries: preserve terminal evidence and cancellation semantics."""
import asyncio
import socket

import pytest

from giraffe import runner
from giraffe.models import CheckResult, Limits, RequestRecord, RunReport
from giraffe.reporting import write_report
from tests.test_runner import client as client, config
from tests.test_runner_stats_revision import VariableClient, spec


async def test_worker_failure_stops_dispatch_and_cancels_inflight_siblings():
    state = runner._Run(config(checks=["correctness"], concurrency=3), None, None)
    transport = VariableClient(failure=KeyError("worker defect"), suppress_cancel=True)
    with pytest.raises(KeyError):
        await state.group(state.config.targets[0], transport, [spec()], "warm", count=12,
                          concurrency=3, checks=["serving"])
    assert len(transport.order) <= 3
    assert len(state.records) == len(transport.order)
    assert transport.active == 0
    assert any(record.status == "cancelled" for record in state.records)
    count = len(transport.order)
    await asyncio.sleep(.02)
    assert len(transport.order) == count


async def test_transport_cancel_without_task_cancellation_stays_local():
    class LocallyCancelled:
        async def execute(self, *args, **kwargs):
            raise asyncio.CancelledError()
    state = runner._Run(config(), None, None)
    record = await state.request(state.config.targets[0], LocallyCancelled(), spec())
    assert record.status == "cancelled"
    assert state.reason is None and not state.stop.is_set()
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.parametrize("error", [OSError(), TimeoutError(), socket.gaierror(), ConnectionResetError()])
def test_os_errors_are_expected_operational_failures(error):
    assert runner._expected_setup_error(error)


@pytest.mark.parametrize("phase", ["score", "evaluate", "emit"])
async def test_record_finished_runs_once_after_terminal_fields_on_callback_error(monkeypatch, phase):
    selected = ["access", "serving", "first_output"]
    def emit(event):
        if phase == "emit" and event["event"] == "request_completed":
            raise KeyError("emit defect")
    state = runner._Run(config(checks=selected), emit, None)
    finalized = []
    def finished(record):
        assert record.completed_ms is not None and record.completed_at is not None
        assert record.intentional is False
        if phase != "emit":
            assert set(record.check_ids) == set(selected)
        finalized.append(record)
    monkeypatch.setattr(state, "record_finished", finished)
    def broken(*args):
        record = args[-1]
        record.valid = False
        raise KeyError("scoring defect")
    if phase == "score":
        monkeypatch.setattr(runner, "score_response", broken)
    with pytest.raises(KeyError):
        await state.request(state.config.targets[0], VariableClient(), spec(),
                            evaluate_record=broken if phase == "evaluate" else None)
    assert len(finalized) == len(state.records) == 1
    assert finalized[0] is state.records[0]
    assert all(row["running"] == 0 for row in state.conditions.values())


async def test_target_setup_error_does_not_replace_existing_failure_or_specific_block(monkeypatch):
    async def target(run, target, fixtures):
        run.observations[target.name]["client_error"] = "Client setup failed (OSError)"
    def checks(run, target):
        return [CheckResult(id="serving", title="Serving", target=target.name,
                            status="fail", summary="Observed invalid responses"),
                CheckResult(id="first_output", title="First output", target=target.name,
                            status="blocked", summary="Specific check setup unavailable")]
    monkeypatch.setattr(runner, "_target", target)
    monkeypatch.setattr(runner, "_checks", checks)
    report = await runner.run_suite(config(checks=["serving", "first_output"]))
    assert report.overall == "fail"
    assert [(check.status, check.summary) for check in report.checks] == [
        ("fail", "Observed invalid responses"), ("blocked", "Specific check setup unavailable")]


def test_first_output_verdict_uses_unrounded_p95():
    state = runner._Run(config(checks=["first_output"], limits=Limits(first_output_ms=500, min_samples=2)), None, None)
    state.records = [RequestRecord(id=str(i), target="service", fixture_id="short", scenario="warm",
        check_ids=["first_output"], started_at="now", status="completed", valid=True,
        elapsed_ms=1000, first_output_ms=500.0004, dispatch_ms=0, timing_start_ms=0, completed_ms=1000)
        for i in range(2)]
    result = next(check for check in runner._checks(state, state.config.targets[0]) if check.id == "first_output")
    assert result.metrics["first_output_p95_ms"] == 500.0004
    assert result.status == "fail"
    assert set(result.metrics["violations"]) == {"0", "1"}


async def test_enclosing_timeout_raises_and_preserves_partial_report_for_existing_writer(tmp_path, monkeypatch, client):
    monkeypatch.setattr(client, "delay", 10)
    events = []
    initial = asyncio.current_task().cancelling()
    with pytest.raises(TimeoutError) as timeout:
        async with asyncio.timeout(.2):
            await runner.run_suite(config(), progress=events.append)
    assert asyncio.current_task().cancelling() == initial
    cancellation = timeout.value.__cause__
    assert isinstance(cancellation, asyncio.CancelledError)
    partial = cancellation.giraffe_report
    assert partial.requests and all(record.status == "cancelled" for record in partial.requests)
    assert partial.abort_reason == "run cancelled"
    assert events[-1]["event"] == "run_finished"
    # The existing report writer/loader works on the partial report carried by
    # the cancellation; timeout conversion must not throw that evidence away.
    path, _ = write_report(partial, tmp_path)
    saved = RunReport.model_validate_json(path.read_text())
    assert saved.run_id == partial.run_id and saved.abort_reason == partial.abort_reason
    assert [record.status for record in saved.requests] == ["cancelled"]



@pytest.mark.parametrize("phase", ["preparation", "traffic"])
@pytest.mark.parametrize("preparation_delay", [0, .08])
@pytest.mark.parametrize("external_cancel", [False, True])
async def test_competing_external_cancel_is_not_converted_to_timeout(
    monkeypatch, client, phase, preparation_delay, external_cancel,
):
    from giraffe import deployment

    monkeypatch.setattr(client, "delay", 10)
    original_prepare = deployment.preparation_residency
    observed = {}

    async def run():
        loop, current = asyncio.get_running_loop(), asyncio.current_task()
        async with asyncio.timeout(None) as deadline:
            observed["timeout"] = deadline

            def arm():
                if deadline.when() is None:
                    observed["armed_at"] = loop.time()
                    when = observed["armed_at"] + .05
                    deadline.reschedule(when)
                    if external_cancel:
                        observed["external_handle"] = loop.call_at(when, current.cancel)

            async def prepare(*args, **kwargs):
                await asyncio.sleep(preparation_delay)
                if phase == "preparation":
                    arm()
                    await asyncio.Event().wait()
                return await original_prepare(*args, **kwargs)

            def progress(event):
                if phase == "traffic" and event["event"] == "request_started":
                    arm()

            monkeypatch.setattr(deployment, "preparation_residency", prepare)
            await runner.run_suite(config(), progress=progress)

    task = asyncio.create_task(run())
    try:
        expected = asyncio.CancelledError if external_cancel else TimeoutError
        with pytest.raises(expected) as stopped:
            await asyncio.wait_for(task, 2)
    finally:
        if handle := observed.get("external_handle"):
            handle.cancel()
    assert observed["timeout"].expired()
    assert observed["timeout"].when() - observed["armed_at"] == pytest.approx(.05)
    assert task.cancelling() == int(external_cancel)
    cancellation = stopped.value if external_cancel else stopped.value.__cause__
    assert isinstance(cancellation, asyncio.CancelledError)
    if phase == "traffic":
        partial = cancellation.giraffe_report
        assert partial.abort_reason == "run cancelled"
        assert partial.requests and all(r.status == "cancelled" and r.completed_ms is not None
                                        and r.score is None for r in partial.requests)
        assert client.active == 0 and client.closed == ["service"]
    else:
        assert not hasattr(cancellation, "giraffe_report")
        assert not client.scenarios and client.active == 0 and not client.closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_terminal_sink_failure_preserves_return_or_cancellation(monkeypatch, caplog, cancel):
    state = runner._Run(config(), None, None)
    seen = []

    def broken(record):
        seen.append(record)
        raise OSError("journal unavailable")

    monkeypatch.setattr(state, "record_sink", broken)
    if cancel:
        task = asyncio.create_task(state.request(state.config.targets[0], VariableClient(), spec()))
        await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        record = error.value.giraffe_record
        assert record.status == "cancelled"
    else:
        record = await state.request(state.config.targets[0], VariableClient(), spec())
        assert record.status == "completed"
    assert seen == [record] and state.records == [record]
    assert "Could not persist terminal request" in caplog.text
