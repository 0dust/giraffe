"""ASGI transport only: structured config errors and migration/planning I/O."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from giraffe.latency import input_bucket
from giraffe.models import RunConfig
from giraffe.web import create_app
from tests.test_load_test_phase_a import values


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate"])
async def test_load_config_api_preserves_dedicated_settings(tmp_path, endpoint):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        payload = values()
        payload["load_test"]["patterns"].update(poisson={"enabled": True, "seed": 37}, rates=[10])
        response = await client.post("/api/config/" + endpoint, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["config"]["checks"] == ["load"]
    assert body["config"]["load_test"]["patterns"]["poisson"]["seed"] == 37
    assert body["request_plan"]["load_test"]["approximate"]
    assert body["request_plan"]["required_concurrency"]["by_check"]["load"] == 512
    assert "load_combinations" in [r["id"] for r in body["request_plan"]["rows"]]


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate"])
async def test_load_burst_ceiling_error_names_both_fields_and_actions(tmp_path, endpoint):
    payload = values()
    payload["concurrency"] = 4
    payload["load_test"]["patterns"]["bursts"] = {
        "enabled": True,
        "size": 8,
        "first_seconds": 0,
        "gap_seconds": 1,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/" + endpoint, json=payload)
    assert response.status_code == 422, response.text
    error = response.json()["detail"][0]
    assert error["fields"] == ["load_test.patterns.bursts.size", "concurrency"]
    assert {a["field"] for a in error["actions"]} == set(error["fields"])
    assert error["values"]["concurrency"] == 4


async def test_load_api_migration_notes_and_independent_schedules_roundtrip(tmp_path):
    payload = {
        "targets": values()["targets"],
        "config_version": 2,
        "checks": ["capacity", "arrivals"],
        "traffic": {"rates": [3], "duration_seconds": 5, "arrival": "poisson"},
        "arrivals": {"mode": "burst", "burst_size": 2, "requests": 8, "interval_seconds": 3},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/validate", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["migration_notes"]
        assert "not blended" in " ".join(body["migration_notes"])
        assert body["config"]["checks"] == ["load"]
        assert body["config"]["load_test"]["schedule"]["duration_seconds"] == 5
        assert (
            body["config"]["load_test"]["migrated_tests"][0]["settings"]["schedule"][
                "duration_seconds"
            ]
            == 12
        )
        again = await client.post("/api/config/validate", json=body["config"])
    assert again.status_code == 200, again.text
    assert again.json()["config"]["load_test"] == body["config"]["load_test"]


async def test_budget_and_time_conflicts_warn_before_start_with_field_actions(tmp_path):
    payload = values()
    payload.update(max_requests=2, max_duration_seconds=0.5)
    payload["load_test"]["patterns"] = {
        "concurrent": {"enabled": False},
        "constant": {"enabled": True},
        "rates": [10],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/validate", json=payload)
    assert response.status_code == 200, response.text
    plan = response.json()["request_plan"]
    assert any("max_requests" in w["fields"] and w["actions"] for w in plan["load_warnings"])
    assert any(
        "max_duration_seconds" in w["fields"] and w["actions"] for w in plan["time_warnings"]
    )


@pytest.mark.parametrize(("duration", "latency_ms", "estimated_requests"), [
    (20, 6400, 4),
    (20, 5000, 4),  # A request ready to start exactly at the cutoff is too late.
    (26, 6400, None),  # The fifth request can finish after sending stops.
    (20, None, None),
])
async def test_preview_warns_when_a_concurrent_condition_is_likely_to_miss_its_sample_minimum(
    tmp_path, duration, latency_ms, estimated_requests,
):
    payload = values()
    load = payload["load_test"]
    load["workloads"]["combinations"]["selected"] = ["short_short", "short_long"]
    load["patterns"]["concurrent"]["levels"] = [1, 2]
    load["schedule"]["duration_seconds"] = duration
    del load["pass_criteria"]["min_valid_responses"]
    config = RunConfig.model_validate(payload)
    profile = config.load_test.profile

    if latency_ms is not None:
        previous = tmp_path / "previous"
        previous.mkdir()
        (previous / ".web.json").write_text(json.dumps({"latency_summary": {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "targets": [{"url": "http://unused.test/v1/chat/completions", "model": "test",
                         "measurements": [{
                             "family": "load.combinations.concurrent",
                             "concurrency": 1,
                             "stream": config.stream,
                             "output_cap": profile.long.output_tokens,
                             "input_bucket": input_bucket(profile.short.input_tokens * 4),
                             "n": 4,
                             "p50_ms": latency_ms,
                             "p90_ms": latency_ms,
                         }]}],
        }}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/preview", json=payload)
    assert response.status_code == 200, response.text
    plan = response.json()["request_plan"]
    warnings = [row for row in plan["load_warnings"] if row.get("kind") == "load_sample_shortfall"]
    if estimated_requests is None:
        assert warnings == []
        return

    warning, = warnings
    assert warning["msg"] in plan["warnings"]
    assert "concurrency 1" in warning["msg"]
    assert warning["values"]["estimated_requests"] == estimated_requests
    assert warning["values"]["min_valid_responses"] == 5
    assert warning["fields"] == ["load_test.schedule.duration_seconds"]
    assert warning["actions"]
