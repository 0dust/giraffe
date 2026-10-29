"""Arrival records cross the journal boundary only after scheduler annotation."""

import asyncio
import json
import time

import pytest

from giraffe import load_test, runner
from giraffe.evidence import EvidenceWriter, recover_evidence
from giraffe.models import RequestRecord, RequestSpec
from giraffe.runner import _Run
from tests.test_concurrent_load import configuration


ARRIVAL_FIELDS = {
    "traffic_stage", "traffic_rate_rps", "traffic_class", "scheduled_offset_ms",
    "scheduled_ms", "dispatch_lag_ms", "arrival_first_output_ms", "arrival_elapsed_ms",
    "load_point", "error", "termination_reason", "termination_limit_seconds",
}


class ArrivalClient:
    def __init__(self, cause, suppress_cancel):
        self.cause, self.suppress_cancel = cause, suppress_cancel
        self.started = asyncio.Event()

    async def execute(self, spec, first_output_event=None):
        begin = time.monotonic()
        self.started.set()
        status = "completed"
        if self.cause != "completed":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not self.suppress_cancel:
                    raise
                status = "cancelled"
        return RequestRecord(id="arrival", target="local", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now", status=status,
            elapsed_ms=(time.monotonic() - begin) * 1000, first_output_ms=0,
            output="K7P4", valid=status == "completed", stream=True)


@pytest.mark.parametrize("cause,suppress_cancel,pattern", [
    ("completed", False, "constant"), ("completed", False, "bursts"),
    ("user_stop", False, "constant"), ("user_stop", True, "constant"),
    ("deadline", False, "constant"), ("deadline", True, "constant"),
    ("stage_cancel", True, "constant"),
])
async def test_arrival_window_journals_finalized_records_once(tmp_path, cause, suppress_cancel, pattern):
    config = configuration(load={"patterns": {
        "concurrent": {"enabled": False}, "constant": {"enabled": pattern == "constant"}, "rates": [1],
        "bursts": {"enabled": pattern == "bursts", "size": 1, "first_seconds": 0, "gap_seconds": 10},
    }})
    run = _Run(config, None, None)
    client = ArrivalClient(cause, suppress_cancel)
    point = load_test.test_points(config.load_test)[0]
    snapshots = []
    writer = EvidenceWriter(tmp_path, {"config": {"retention": "all"}}, fsync_records=1)

    def persist(record):
        snapshots.append(record.model_dump(include=ARRIVAL_FIELDS))
        writer.record_finished(record)

    run.record_sink = persist
    task = asyncio.create_task(load_test.arrival_window(run, config.targets[0], client,
        config.load_test, point, {("short", "short_answer"): {"filler_chars": 0}}))
    try:
        await asyncio.wait_for(client.started.wait(), 1)
        if cause == "user_stop":
            run.stop.set()
        elif cause == "deadline":
            run.deadline = time.monotonic()
        elif cause == "stage_cancel":
            task.cancel()
        else:
            # Persist while the sending window is open, before its caller can
            # append any metadata to the returned request object.
            async with asyncio.timeout(1):
                while True:
                    path = tmp_path / "evidence.jsonl"
                    try:
                        rows = [json.loads(line) for line in path.read_text().splitlines()]
                    except (FileNotFoundError, json.JSONDecodeError):
                        rows = []
                    if any(row["type"] == "record" for row in rows):
                        break
                    await asyncio.sleep(.005)
            assert not task.done()
        records = await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        writer.close()

    assert len(records) == len(run.records) == len(snapshots) == 1
    record = records[0]
    assert record.status == ("completed" if cause == "completed" else "cancelled")
    assert record.traffic_stage == 0
    assert record.traffic_rate_rps == 1
    assert record.traffic_class == "short_short"
    assert record.scheduled_offset_ms == 0
    assert record.scheduled_ms is not None
    assert record.load_point["origin"] == ("burst" if pattern == "bursts" else "arrival")
    assert record.load_point["burst_index"] == (0 if pattern == "bursts" else None)
    if cause != "completed":
        assert record.load_point["cancellation_reason"] == record.error
        assert record.error == {
            "user_stop": "cancelled by user", "deadline": "maximum run duration reached",
            "stage_cancel": "Traffic stage cancelled.",
        }[cause]
    assert record.dispatch_lag_ms >= 0
    assert record.arrival_elapsed_ms == pytest.approx(record.dispatch_lag_ms + record.elapsed_ms)
    if record.first_output_ms is not None:
        assert record.arrival_first_output_ms == pytest.approx(
            record.dispatch_lag_ms + record.first_output_ms)
    else:
        assert record.arrival_first_output_ms is None
    expected = record.model_dump(include=ARRIVAL_FIELDS)
    assert snapshots == [expected]
    journal = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    assert sum(row["type"] == "record" for row in journal) == 1
    recovered = recover_evidence(tmp_path, {})
    assert len(recovered.requests) == 1
    assert recovered.requests[0].model_dump(include=ARRIVAL_FIELDS) == expected
    assert recovered.requests[0].status == record.status


@pytest.mark.parametrize("phase", ["scoring", "progress"])
async def test_arrival_journal_is_finalized_when_request_postprocessing_fails(tmp_path, monkeypatch, phase):
    config = configuration(load={"patterns": {
        "concurrent": {"enabled": False}, "constant": {"enabled": True}, "rates": [1],
    }})

    def fail(*args):
        raise ValueError("postprocessing failed")

    def progress(event):
        if phase == "progress" and event["event"] == "request_completed":
            fail()

    if phase == "scoring":
        monkeypatch.setattr(runner, "score_response", fail)
    run = _Run(config, progress, None)
    writer = EvidenceWriter(tmp_path, {"config": {"retention": "all"}})
    run.record_sink = writer.record_finished
    try:
        records = await load_test.arrival_window(run, config.targets[0], ArrivalClient("completed", False),
            config.load_test, load_test.test_points(config.load_test)[0],
            {("short", "short_answer"): {"filler_chars": 0}})
    finally:
        writer.close()
    assert records == []  # The scheduler records an execution error, not a completed offer.
    assert len(run.records) == 1
    record = run.records[0]
    assert record.traffic_stage == 0 and record.traffic_rate_rps == 1
    assert record.traffic_class == "short_short"
    assert record.arrival_elapsed_ms == pytest.approx(record.dispatch_lag_ms + record.elapsed_ms)
    recovered = recover_evidence(tmp_path, {})
    assert len(recovered.requests) == 1
    assert recovered.requests[0].model_dump(include=ARRIVAL_FIELDS) == record.model_dump(include=ARRIVAL_FIELDS)
    journal = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    assert sum(row["type"] == "record" for row in journal) == 1


@pytest.mark.parametrize("phase", ["none", "scoring", "progress", "cancellation"])
async def test_terminal_finalizer_failure_preserves_original_error_and_single_snapshot(monkeypatch, phase):
    original = ValueError("original request failure")
    finalizer_error = RuntimeError("finalizer failed")

    def fail(*args):
        raise original

    def progress(event):
        if phase == "progress" and event["event"] == "request_completed":
            fail()

    if phase == "scoring":
        monkeypatch.setattr(runner, "score_response", fail)
    config = configuration()
    run = _Run(config, progress, None)
    calls, persisted = [], []
    run.record_sink = persisted.append

    def finalize(record):
        calls.append(record)
        raise finalizer_error

    spec = RequestSpec(fixture_id="arrival", scenario="load_arrival", check_ids=["load"], messages=[])
    client = ArrivalClient("user_stop" if phase == "cancellation" else "completed", False)
    task = asyncio.create_task(run.request(config.targets[0], client, spec, finalize_record=finalize))
    if phase == "cancellation":
        await asyncio.wait_for(client.started.wait(), 1)
        task.cancel()
    expected = asyncio.CancelledError if phase == "cancellation" else (ValueError, RuntimeError)
    with pytest.raises(expected) as raised:
        await task
    if phase == "cancellation":
        assert raised.value.giraffe_record is run.records[0]
    else:
        assert raised.value is (finalizer_error if phase == "none" else original)
    assert calls == persisted == run.records and len(calls) == 1
