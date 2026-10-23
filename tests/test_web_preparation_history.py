"""Preparation evidence survives the bounded activity feed and server restarts."""

import asyncio

from giraffe.web import _track_live_progress
from tests.test_web import browser, finished, report, start


def test_preparation_cache_is_bounded_and_refreshes_existing_scenarios():
    job = {"progress": {}, "events": [], "phases": [], "conditions": {}}

    def emit(index, status="completed"):
        _track_live_progress(job, {"event": "preparation_finished", "target": "local",
            "scenario": f"input_{index}", "preparation": {"status": status}}, None)

    for index in range(70):
        emit(index)
    assert len(job["live_preparations"]) == 64
    assert ("local", "input_0") not in job["live_preparations"]
    emit(6, "unavailable")
    emit(70)
    assert len(job["live_preparations"]) == 64
    assert job["live_preparations"][("local", "input_6")]["status"] == "unavailable"
    assert ("local", "input_7") not in job["live_preparations"]


async def test_preparation_history_survives_event_eviction_and_saved_metadata(tmp_path, monkeypatch, local_tokenizer):
    ready = asyncio.Event()

    async def waiting(configuration, *, progress, stop_event):
        for scenario, status, reason in [
            ("context_preparation", "unavailable", "Context input could not be sized"),
            ("load_preparation_short_short_answer", "completed", None),
        ]:
            progress({"event": "preparation_finished", "target": "local", "scenario": scenario,
                "preparation": {"scenario": scenario, "status": status,
                                "step": "Preparing input locally", "reason": reason}})
        for _ in range(120):
            progress({"event": "progress", "target": "local"})
        ready.set()
        await stop_event.wait()
        return report(configuration, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await asyncio.wait_for(ready.wait(), 2)
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        assert len(detail["events"]) == 100
        assert not any(event.get("preparation") for event in detail["events"])
        preparations = detail["live_preparations"]
        assert [item["status"] for item in preparations] == ["unavailable", "completed"]
        assert preparations[0]["reason"] == "Context input could not be sized"
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)
    async with browser(tmp_path) as client:
        saved = (await client.get(f"/api/runs/{identifier}")).json()
        assert saved["live_preparations"] == preparations
