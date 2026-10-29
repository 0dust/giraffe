"""Internal load-worker defects stop their window and retain terminal evidence."""
import asyncio

import pytest

from giraffe import load_test, runner
from tests.test_concurrent_load import configuration
from tests.test_runner import FakeClient


pytestmark = pytest.mark.usefixtures("local_tokenizer")


@pytest.mark.parametrize("when", ["sending", "drain", "sibling_cleanup"])
async def test_worker_defect_stops_suite_promptly_and_joins_siblings(monkeypatch, caplog, when):
    started, release, joined, draining = (asyncio.Event() for _ in range(4))
    workers = []
    defect = KeyError("secret implementation detail")
    begin_drain = load_test.ScheduleObservation.begin_drain

    def observe_drain(observation, *args, **kwargs):
        result = begin_drain(observation, *args, **kwargs)
        draining.set()
        return result

    class BrokenClient(FakeClient):
        async def execute(self, spec, **kwargs):
            if not spec.load_point:
                return await super().execute(spec, **kwargs)
            workers.append(asyncio.current_task())
            if len(workers) == 1:
                await release.wait()
                raise defect
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                joined.set()
                if when == "sibling_cleanup":
                    raise RuntimeError("secondary cleanup defect")

    monkeypatch.setattr(runner, "LLMClient", BrokenClient)
    monkeypatch.setattr(load_test.ScheduleObservation, "begin_drain", observe_drain)
    config = configuration(max_duration_seconds=60, load={
        "patterns": {"concurrent": {"levels": [2]}},
        "schedule": {"duration_seconds": 1 if when == "drain" else 30},
        "request_timeout_seconds": 60,
    })
    task = asyncio.create_task(runner.run_suite(config))
    try:
        await asyncio.wait_for(started.wait(), 3)
        if when == "drain":
            await asyncio.wait_for(draining.wait(), 3)
        release.set()
        done, _ = await asyncio.wait({task}, timeout=2)
        assert done, "An internal worker failure waited for the sending window or request deadline"
        report = task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert joined.is_set() and len(workers) == 2 and all(worker.done() for worker in workers)
    diagnostic = "Internal error in Giraffe (KeyError); see the Giraffe log."
    assert report.observations["targets"]["local"]["client_error"] == diagnostic
    assert report.overall == "blocked"
    result = next(check for check in report.checks if check.id == "load")
    assert result.status == "blocked" and diagnostic in result.summary
    point = result.metrics["points"][0]
    assert point["reason_code"] == "internal_error" and point["execution_error"] == diagnostic
    assert point["not_run"] is False
    assert len(report.requests) == len({record.id for record in report.requests}) == 2
    assert set(point["measurement_windows"][0]["request_ids"]) == {record.id for record in report.requests}
    assert point["failed"] == (2 if when == "sibling_cleanup" else 1)
    assert point["cancelled"] == (0 if when == "sibling_cleanup" else 1)
    assert all(condition["running"] == 0 for condition in report.observations["targets"]["local"]["conditions"])
    assert "secret implementation detail" not in report.model_dump_json()
    assert any(record.exc_info and record.exc_info[1] is defect for record in caplog.records)


async def test_request_timeouts_remain_endpoint_failures(monkeypatch):
    class TimeoutClient(FakeClient):
        async def execute(self, spec, **kwargs):
            raise TimeoutError("request deadline")

    monkeypatch.setattr(runner, "LLMClient", TimeoutClient)
    config = configuration(max_requests=2)
    report = await runner.run_suite(config)
    result = next(check for check in report.checks if check.id == "load")
    assert not report.observations["targets"]["local"].get("client_error")
    assert report.overall == result.status == "fail"
    assert result.metrics["points"][0]["timed_out"] == 2
    assert result.metrics["points"][0]["reason_code"] == "error_rate"
    assert "endpoint error allowance exceeded" in result.summary


async def test_internal_worker_error_does_not_hide_another_points_endpoint_failure(monkeypatch):
    class BrokenClient(FakeClient):
        async def execute(self, spec, **kwargs):
            if spec.load_point and spec.load_point["level"] == 2:
                raise KeyError("internal worker defect")
            record = await super().execute(spec, **kwargs)
            record.output = "wrong answer"
            return record

    monkeypatch.setattr(runner, "LLMClient", BrokenClient)
    report = await runner.run_suite(configuration(max_requests=4, load={
        "patterns": {"concurrent": {"levels": [1, 2]}},
    }))
    result = next(check for check in report.checks if check.id == "load")
    assert report.overall == result.status == "fail"
    assert [(point["status"], point["reason_code"]) for point in result.metrics["points"]] == [
        ("fail", "correctness"), ("blocked", "internal_error")]


async def test_interference_probe_failure_is_rethrown_after_short_stream_cleanup():
    joined = asyncio.Event()
    defect = KeyError("probe defect")
    config = configuration(load={"combinations": {"enabled": False},
        "interference": {"enabled": True}, "schedule": {"levels": [2], "duration_seconds": 30}})
    run = runner._Run(config, None, None)
    point = load_test.test_points(config.load_test)[0]
    calibration = {(size, "short_answer"): {"filler_chars": 0} for size in ("short", "long")}

    class Client:
        async def execute(self, spec, first_output_event=None):
            if spec.load_point["role"] == "probe":
                raise defect
            first_output_event.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                joined.set()

    with pytest.raises(KeyError) as failure:
        await asyncio.wait_for(load_test.concurrent_window(
            run, config.targets[0], Client(), config.load_test, point, calibration, phase="probe"), 2)
    assert failure.value is defect and joined.is_set()
    assert len(run.records) == 2 and not run.inflight
    assert point["measurement_windows"][0]["request_ids"] == [record.id for record in run.records]
