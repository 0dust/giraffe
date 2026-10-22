"""ASGI transport only: structured config errors and migration/planning I/O."""

import httpx
import pytest

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
