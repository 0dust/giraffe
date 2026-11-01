"""Session pauses survive terminal evidence snapshots and follow-up cancellation."""

import asyncio
import json

import pytest

from giraffe.evidence import EvidenceWriter, recover_evidence
from giraffe.models import RequestRecord, RunConfig
from giraffe.runner import _Run
from giraffe.workloads import sessions


class SessionClient:
    def __init__(self, termination):
        self.termination = termination
        self.followup_started = asyncio.Event()
        self.calls = 0

    async def execute(self, spec, first_output_event=None):
        self.calls += 1
        identifier = f"request-{self.calls}"
        status = "completed"
        if spec.workload["turn_id"] and self.termination != "completed":
            self.followup_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if self.termination != "partial_cancel":
                    raise
                status = "cancelled"
        return RequestRecord(id=identifier, target="local", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now",
            status=status, valid=status == "completed", output=spec.expected or "answer",
            output_tokens=2, finish_reason="stop" if status == "completed" else None)


@pytest.mark.parametrize("profile", ["correctness", "performance"])
@pytest.mark.parametrize("termination", ["completed", "cancelled", "partial_cancel"])
async def test_session_pause_is_persisted_before_return_or_cancellation(tmp_path, profile, termination):
    count = 2 if termination == "completed" else 1
    settings = {"profile": profile, "modes": ["live"], "sessions": count, "concurrency": count,
                "sampling": {"think_time_seconds": {"values": [.01]}}}
    if profile == "performance":
        settings["workloads"] = [{"name": "chat", "turns": ["Hello", "Tell me more"]}]
    config = RunConfig(targets=[{"name": "local", "model": "test", "url": "http://localhost"}],
                       checks=["sessions"], sessions=settings, context_limit=2048,
                       max_requests=10, max_duration_seconds=5)
    run, client = _Run(config, None, None), SessionClient(termination)
    snapshots = []
    writer = EvidenceWriter(tmp_path, {"config": {"retention": "all"}})

    def persist(record):
        # Serialize immediately so a later mutation cannot repair this snapshot.
        snapshots.append(json.loads(record.model_dump_json()))
        writer.record_finished(record)

    run.record_sink = persist
    task = asyncio.create_task(sessions(run, config.targets[0], client))
    try:
        if termination == "completed":
            await asyncio.wait_for(task, 2)
        else:
            await asyncio.wait_for(client.followup_started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        writer.close()

    assert len(snapshots) == len(run.records) == client.calls == count * 2
    first_turns = {record["workload"]["session_id"]: record for record in snapshots
                   if record["workload"]["turn_id"] == 0}
    for record in snapshots:
        metadata = record["workload"]
        if metadata["turn_id"] == 0:
            assert metadata.get("actual_pause_seconds") is None
        else:
            assert "actual_pause_seconds" in metadata
            prior = first_turns[metadata["session_id"]]
            expected = (record["dispatch_ms"] - prior["completed_ms"]) / 1000
            assert expected > 0
            assert metadata["actual_pause_seconds"] == pytest.approx(expected)
            assert record["status"] == ("completed" if termination == "completed" else "cancelled")
    recovered = recover_evidence(tmp_path, {})
    assert len(recovered.requests) == len(snapshots)
    assert [record.workload for record in recovered.requests] == [record["workload"] for record in snapshots]
    assert [record.workload for record in run.records] == [record["workload"] for record in snapshots]
