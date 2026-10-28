"""Caps discovery and exact lifetime peaks without walking active requests."""

import asyncio
import random

import pytest

from giraffe import runner
from giraffe.capabilities import capabilities
from giraffe.models import RequestRecord, RequestSpec, RunConfig

TARGET = {"name": "local", "url": "http://local/v1", "model": "fake"}


def test_peak_tracker_matches_lifetime_reference_across_churn():
    tracker = runner._InflightPeaks()
    rng = random.Random(7)
    active = {}
    assert len(tracker) == 0 and not tracker
    for _ in range(10000):
        target = rng.choice(["a", "b"])
        own = [token for token, (name, _) in active.items() if name == target]
        if not own or len(own) < 128 and rng.random() < .55:
            token = tracker.start(target)
            active[token] = (target, len(own) + 1)
            for previous, (name, peak) in list(active.items()):
                if name == target:
                    active[previous] = (name, max(peak, len(own) + 1))
        else:
            token = rng.choice(own)
            name, expected = active.pop(token)
            assert tracker.finish(name, token) == expected
        assert len(tracker) == len(active)
        assert bool(tracker) == bool(active)
    for token, (name, expected) in active.items():
        assert tracker.finish(name, token) == expected
    assert len(tracker) == 0 and not tracker
    assert not tracker.counts and not tracker.peaks


async def test_request_peak_cleanup_includes_cancellation_and_client_failure():
    config = RunConfig(targets=[TARGET], checks=["correctness"], default_concurrency=3)
    run = runner._Run(config, None, None)
    started = [asyncio.Event() for _ in range(3)]
    finish = [asyncio.Event() for _ in range(3)]

    class Client:
        async def execute(self, spec, **kwargs):
            index = int(spec.fixture_id)
            started[index].set()
            await finish[index].wait()
            if index == 1:
                raise ValueError("fake client failure")
            return RequestRecord(id=spec.fixture_id, target="local", fixture_id=spec.fixture_id,
                                 scenario="warm", check_ids=[], started_at="now", status="completed", valid=True)

    tasks = []
    for index in range(3):
        spec = RequestSpec(fixture_id=str(index), scenario="warm", check_ids=["serving"],
                           messages=[{"role": "user", "content": "hello"}])
        tasks.append(asyncio.create_task(run.request(config.targets[0], Client(), spec)))
        await started[index].wait()
    tasks[0].cancel()
    finish[1].set()
    finish[2].set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    # Cancellation and unexpected client bugs propagate after terminal records
    # and shared occupancy are finalized; joining every task proves cleanup.
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert isinstance(outcomes[1], ValueError)
    assert isinstance(outcomes[2], RequestRecord)
    assert tasks[0].cancelled() and tasks[1].exception() is outcomes[1]
    records = run.records
    assert len(records) == 3
    assert {r.status for r in records} == {"completed", "failed", "cancelled"}
    assert all(r.overlap == 3 for r in records)
    assert not run.inflight.counts and not run.inflight.peaks


def test_capabilities_publish_every_schema_bound():
    value = capabilities()
    limits = value["limits"]
    schema = value["config_schema"]

    def visit(node, path=""):
        if "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
        for choice in node.get("anyOf", []):
            if choice.get("type") != "null":
                visit(choice, path)
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                    "minItems", "maxItems", "minLength", "maxLength"):
            if key in node:
                assert limits[path][key] == node[key], (path, key)
        for key, child in node.get("properties", {}).items():
            visit(child, f"{path}.{key}" if path else key)
        if isinstance(node.get("items"), dict):
            visit(node["items"], path + "[]")
        if isinstance(node.get("additionalProperties"), dict):
            visit(node["additionalProperties"], path + ".*")

    visit(schema)
    assert limits["load_test.patterns.concurrent.levels"]["items"]["maximum"] == 512
    assert limits["consistency.levels"]["items"]["maximum"] == 512
    assert limits["judging.concurrency"]["maximum"] == 512
    assert limits["sessions.concurrency"]["maximum"] == 512
    assert limits["load_test.planned_arrivals"]["maximum"] == 100000
    from giraffe import caps
    for name, maximum in vars(caps).items():
        if name.startswith("MAX_"):
            assert limits[f"runtime.{name}"] == {"maximum": maximum}
    assert limits["runtime.DEFAULT_HTML_REQUEST_DETAILS"] == {"default": 25}


@pytest.mark.parametrize("model, field", [
    ("RunConfig", "concurrency"), ("RunConfig", "default_concurrency"),
    ("Arrival", "max_in_flight"), ("TrafficConfig", "max_in_flight"),
    ("CheckOptions", "concurrency"), ("JudgingConfig", "concurrency"), ("Sessions", "concurrency"),
])
def test_simultaneous_fields_share_measured_ceiling(model, field):
    from pydantic import TypeAdapter, ValidationError
    from giraffe import models
    from giraffe.caps import MAX_SIMULTANEOUS_REQUESTS

    adapter = TypeAdapter(getattr(models, model).model_fields[field].rebuild_annotation())
    assert adapter.validate_python(MAX_SIMULTANEOUS_REQUESTS) == 512
    with pytest.raises(ValidationError):
        adapter.validate_python(MAX_SIMULTANEOUS_REQUESTS + 1)


def test_arrival_and_session_decisions():
    from pydantic import ValidationError
    from giraffe.models import Arrival, Sessions, LoadSchedule, Consistency
    from giraffe.planning import request_plan

    assert Arrival(requests=10000, burst_size=1024, max_pending=256).burst_size == 1024
    # Legacy bursts load under their old safety settings; the merged run uses
    # the global admission ceiling and reports drops without a pending queue.
    config = RunConfig(targets=[TARGET], checks=["arrivals"], arrivals={
        "mode": "burst", "requests": 1024, "burst_size": 1024,
        "max_in_flight": 512, "max_pending": 256})
    assert config.checks == ["load"]
    assert config.load_test.patterns.bursts.size == 1024
    plan = request_plan(config)
    assert plan['load_test']['expected_scheduled_requests'] == 1024
    assert any('max_pending' in note and 'no pending queue' in note for note in config._migration_notes)
    assert not plan['errors']
    for field, value in (("requests", 10001), ("burst_size", 1025), ("max_pending", 257)):
        with pytest.raises(ValidationError):
            Arrival(**{field: value})
    assert Sessions(sessions=32, concurrency=512).concurrency == 512
    configured = RunConfig(targets=[TARGET], checks=["sessions"],
                           sessions={"sessions": 32, "concurrency": 512})
    from giraffe.models import required_concurrency
    assert required_concurrency(configured)["by_check"]["sessions"] == 32
    with pytest.raises(ValidationError):
        Sessions(concurrency=513)
    for field in ("sessions",):
        with pytest.raises(ValidationError):
            Sessions(**{field: 33})
    for model in (LoadSchedule, Consistency):
        assert model(levels=[1, 512]).levels == [1, 512]
        with pytest.raises(ValidationError):
            model(levels=[513])


def test_live_web_containers_stay_bounded_with_100000_conditions_and_requests():
    from collections import OrderedDict
    from giraffe.web import _track_live_progress
    from giraffe.caps import (MAX_LIVE_REQUESTS, MAX_LIVE_CONDITIONS,
                              MAX_LIVE_EVENTS, MAX_LIVE_PHASES)

    job = {"requests": OrderedDict(), "live_request_summary": {
        "evicted": 0, "through_slot": None, "statuses": {}}, "live_request_count": 0,
        "evicted_running": set(), "events": [], "conditions": {}, "phases": []}
    for slot in range(100000):
        summary = {"target": "local", "condition": {"id": str(slot)}, "completed": 1}
        _track_live_progress(job, {"event": "request_completed", "target": "local",
            "request_slot": slot, "request": {"status": "completed"},
            "condition_summary": summary}, None)
        _track_live_progress(job, {"event": "scenario_finished", "target": "local",
                                  "scenario": str(slot), "elapsed_seconds": slot}, None)
    assert len(job["requests"]) == MAX_LIVE_REQUESTS
    assert len(job["conditions"]) == MAX_LIVE_CONDITIONS
    assert len(job["events"]) == MAX_LIVE_EVENTS
    assert len(job["phases"]) == MAX_LIVE_PHASES
    assert job["live_request_count"] == 100000
    assert job["live_request_summary"]["evicted"] == 100000 - MAX_LIVE_REQUESTS
    # Retained conditions still receive updates once new-condition admission stops.
    _track_live_progress(job, {"event": "condition_finished", "target": "local",
                              "condition": {"id": "0"}, "completed": 2}, None)
    assert job["conditions"][("local", "0")]["completed"] == 2
