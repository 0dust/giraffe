"""Lifecycle collection stays bounded and distinct from deployment metadata."""

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from giraffe import deployment, telemetry
from giraffe.caps import MAX_DEPLOYMENT_BODY_BYTES, MAX_DEPLOYMENT_HISTORY, MAX_SANITIZE_ITEMS
from giraffe.models import RunConfig, ScaleDownSnapshot, Target


def lifecycle(**changes):
    return {
        "schema_version": 1, "target": "local", "source": "router audit",
        "clock_id": "router-boot-1", "through_sequence": 4,
        "captured_at": datetime.now(timezone.utc).isoformat(), "clock_uncertainty_ms": 5,
        "inventories": [{"event_id": "inventory-1", "sequence": 0, "time_ms": 100,
                         "replicas": ["replica-a-1", "replica-b-1"]}],
        "events": [{"event_id": "admission-1", "sequence": 1, "time_ms": 101,
                    "kind": "request_admitted", "replica_id": "replica-b-1",
                    "request_id": "request-1", "attempt_id": "attempt-1"}],
        "coverage": [{"replica_id": "replica-b-1", "start_sequence": 0,
                      "end_sequence": 4, "complete": True, "scope": "all"}],
        **changes,
    }


def settings(*, enabled=True, **target_changes):
    target = Target(name="local", url="http://inference/v1", model="m",
                    deployment={"collector_url": "http://collector/export"}, **target_changes)
    config = RunConfig(targets=[target], checks=["scale_down"] if enabled else ["serving"],
                       scale_down={"target": "local"} if enabled else None)
    return target, config


async def collect(monkeypatch, response, *, enabled=True, run_start=True):
    seen = []
    target, config = settings(enabled=enabled)
    actual = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        return response(request)

    monkeypatch.setattr(deployment, "_discovery_client", lambda config, timeout:
                        actual(transport=httpx.MockTransport(handler), timeout=timeout))
    result = await deployment.snapshot(target, config, time.monotonic() + 5, asyncio.Event(),
                                       run_start=time.monotonic() - 1 if run_start else None)
    return result, seen


async def test_collector_preserves_ids_source_clock_and_bounded_capture(monkeypatch):
    result, seen = await collect(monkeypatch, lambda request: httpx.Response(200, json={
        "runtime": {"version": "1"}, "scale_down_lifecycle": lifecycle()}))
    parsed = ScaleDownSnapshot.model_validate(result["scale_down_lifecycle"])
    assert len(seen) == 1
    assert parsed.export.events[0].request_id == "request-1"
    assert parsed.export.events[0].attempt_id == "attempt-1"
    assert parsed.export.source == "router audit"
    assert parsed.export.clock_id == "router-boot-1"
    assert parsed.export.events[0].time_ms == 101
    assert parsed.capture_start_ms <= parsed.collected_end_ms
    assert parsed.capture_end_ms >= parsed.collected_start_ms
    assert not result.get("scale_down_limitations")
    assert result["fields"]["runtime.version"]["reported"]["value"] == "1"


@pytest.mark.parametrize("changes", [
    {"captured_at": None}, {"clock_uncertainty_ms": None}, {"captured_at": "invalid"},
    {"captured_at": "2026-01-01T00:00:00"},
    {"captured_at": "2026-01-01T00:00:00+03:00"},
    {"captured_at": "2001-01-01T00:00:00Z"},
    {"captured_at": "2099-01-01T00:00:00Z"},
])
async def test_unmapped_capture_retains_source_claims_without_freshness(monkeypatch, changes):
    result, _ = await collect(monkeypatch, lambda request: httpx.Response(
        200, json={"scale_down_lifecycle": lifecycle(**changes)}))
    snap = result["scale_down_lifecycle"]
    assert snap["capture_start_ms"] is None and snap["capture_end_ms"] is None
    assert snap["export"]["events"][0]["event_id"] == "admission-1"
    assert result["scale_down_limitations"]


@pytest.mark.parametrize("changes", [
    {"target": "other"}, {"schema_version": 2}, {"schema_version": True},
    {"through_sequence": "4"}, {"capture_start_ms": 10},
    {"events": [{}]}, {"coverage": [{}] * (MAX_SANITIZE_ITEMS + 1)},
    {"source": "Bearer secret-value"}, {"clock_id": "https://user:secret@host/id"},
    {"events": [{"event_id": "secret=private", "sequence": 2, "time_ms": 1,
                 "kind": "drain_effective", "replica_id": "replica-b-1"}]},
])
async def test_invalid_export_cannot_create_evidence_or_leak_payload(monkeypatch, changes, caplog):
    result, _ = await collect(monkeypatch, lambda request: httpx.Response(
        200, json={"scale_down_lifecycle": lifecycle(**changes)}))
    assert "scale_down_lifecycle" not in result
    assert result["scale_down_limitations"]
    assert "secret-value" not in json.dumps(result) + caplog.text
    assert "private" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize("body", [{}, {"scale_down_lifecycle": None}, ["not an object"]])
async def test_missing_or_invalid_collector_export_is_explicit(monkeypatch, body):
    result, _ = await collect(monkeypatch, lambda request: httpx.Response(200, json=body))
    assert "scale_down_lifecycle" not in result
    assert result["scale_down_limitations"]


async def test_lifecycle_requires_selected_scenario_and_runner_origin(monkeypatch):
    def response(request):
        return httpx.Response(200, json={"scale_down_lifecycle": lifecycle()})
    result, _ = await collect(monkeypatch, response, enabled=False)
    assert "scale_down_lifecycle" not in result
    assert "scale_down_limitations" not in result
    result, _ = await collect(monkeypatch, response, run_start=False)
    assert "scale_down_lifecycle" not in result
    assert result["scale_down_limitations"]


def test_ordinary_metadata_does_not_become_lifecycle_evidence(tmp_path):
    payload = {"scale_down_lifecycle": lifecycle()}
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(payload))
    assert "scale_down_lifecycle" not in deployment.safe_metadata(payload)
    assert "scale_down_lifecycle" not in deployment.load_metadata(path)


def test_mapping_preserves_full_source_uncertainty_and_separate_event_clock():
    target, _ = settings()
    payload = lifecycle(captured_at=datetime.fromtimestamp(1000.005, timezone.utc).isoformat(),
                        clock_uncertainty_ms=10)
    snapshot, limitations = deployment._lifecycle_snapshot(payload, target, 99, 100, 100.01, 1000, 1000.01)
    assert not limitations
    assert snapshot["capture_start_ms"] == pytest.approx(995)
    assert snapshot["capture_end_ms"] == pytest.approx(1015)
    assert snapshot["collected_start_ms"] == pytest.approx(1000)
    assert snapshot["collected_end_ms"] == pytest.approx(1010)
    assert snapshot["export"]["events"][0]["time_ms"] == 101


@pytest.mark.parametrize("origin,wall_end", [(100, 1000.01), (99, 1001)])
def test_capture_before_run_or_local_wall_clock_jump_remains_unmapped(origin, wall_end):
    target, _ = settings()
    payload = lifecycle(captured_at=datetime.fromtimestamp(1000.005, timezone.utc).isoformat(),
                        clock_uncertainty_ms=10)
    snapshot, limitations = deployment._lifecycle_snapshot(payload, target, origin, 100, 100.01, 1000, wall_end)
    assert snapshot["capture_start_ms"] is None and snapshot["capture_end_ms"] is None
    assert limitations


@pytest.mark.parametrize("collector,stopped", [(None, False), ("http://collector/export", True)])
async def test_unavailable_collection_has_explicit_limitation(collector, stopped):
    target, config = settings()
    target.deployment.collector_url = collector
    stop = asyncio.Event()
    if stopped:
        stop.set()
    result = await deployment.snapshot(target, config, time.monotonic() + 1, stop,
                                       run_start=time.monotonic() - 1)
    assert "scale_down_lifecycle" not in result
    assert result["scale_down_limitations"]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *, slow=False):
        self.slow = slow
        self.closed = False
        self.remaining = 3

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.slow:
            await asyncio.sleep(10)
        if not self.remaining:
            raise StopAsyncIteration
        self.remaining -= 1
        return b" " * (MAX_DEPLOYMENT_BODY_BYTES // 2)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("slow", [False, True])
async def test_collector_chunked_or_slow_body_remains_bounded(monkeypatch, slow):
    stream = Chunks(slow=slow)
    actual = httpx.AsyncClient
    monkeypatch.setattr(deployment, "_discovery_client", lambda config, timeout:
                        actual(transport=httpx.MockTransport(lambda request:
                            httpx.Response(200, stream=stream))))
    target, config = settings()
    start = time.monotonic()
    result = await deployment.snapshot(target, config, start + .05, asyncio.Event(), run_start=start)
    assert time.monotonic() - start < 1
    assert stream.closed
    # HTTPX nests async generators while decoding. Let their scheduled
    # finalizers finish after the response's explicit close has been checked.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "scale_down_lifecycle" not in result
    assert result["scale_down_limitations"]


async def test_collector_clears_inference_headers_and_runtime_cookies(monkeypatch):
    monkeypatch.setenv("INFERENCE_KEY", "inference-private")
    seen = []
    actual = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "1"}, headers={"set-cookie": "secret=runtime-private"})
        return httpx.Response(200, json={"scale_down_lifecycle": lifecycle()})

    monkeypatch.setattr(deployment, "_discovery_client", lambda config, timeout:
                        actual(transport=httpx.MockTransport(handler)))
    target = Target(name="local", url="http://same-host/v1", model="m", api_key_env="INFERENCE_KEY",
                    deployment={"discovery": "vllm", "collector_url": "http://same-host/export"})
    config = RunConfig(targets=[target], checks=["scale_down"], scale_down={"target": "local"})
    result = await deployment.snapshot(target, config, time.monotonic() + 1, asyncio.Event(),
                                       run_start=time.monotonic() - 1)
    assert seen[0].headers["authorization"] == "Bearer inference-private"
    assert "authorization" not in seen[1].headers
    assert "cookie" not in seen[1].headers
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("enabled", [False, True])
async def test_existing_sampler_keeps_lifecycle_after_configuration_history_fills(monkeypatch, enabled):
    target, config = settings(enabled=enabled)
    config.metrics_max_samples = 40
    observations = {"serving_telemetry": {"snapshots": [], "collection_gaps_seconds": []},
                    "deployment_history": []}
    run = SimpleNamespace(config=config, observations={"local": observations}, active={},
                          start=time.monotonic(), deadline=time.monotonic() + 5, stop=asyncio.Event())
    calls = []

    async def scrape(run, target, phase):
        return {"elapsed_seconds": len(observations["serving_telemetry"]["snapshots"])}

    async def snapshot(target, config, deadline, stop, **kwargs):
        calls.append(kwargs)
        return {"scale_down_lifecycle": {"sequence": len(calls)}, "scale_down_limitations": [],
                "fields": {"runtime.version": "not copied to lifecycle history"}}

    async def interval(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(telemetry, "scrape", scrape)
    monkeypatch.setattr(deployment, "snapshot", snapshot)
    monkeypatch.setattr(telemetry.asyncio, "wait_for", interval)
    await telemetry.sample_during(run, target, stop_event=asyncio.Event())
    assert len(observations["deployment_history"]) == MAX_DEPLOYMENT_HISTORY
    assert observations["serving_telemetry"]["sample_limit_reached"]
    if enabled:
        history = observations["scale_down_deployment_history"]
        assert len(calls) == len(history) == 39
        assert all(call == {"run_start": run.start} for call in calls)
        assert all("fields" not in item for item in history)
        assert "sample limit" in history[-1]["scale_down_limitations"][0]
    else:
        assert len(calls) == MAX_DEPLOYMENT_HISTORY
        assert all(not call for call in calls)
        assert "scale_down_deployment_history" not in observations
