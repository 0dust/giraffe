"""Run-limit I/O, bounded live slots and workload-based baseline identities."""

from collections import OrderedDict
from copy import deepcopy

import pytest

from giraffe.models import RunConfig
from giraffe.reporting import _settings_reasons
from giraffe.web import BROWSER_DEFAULT_RUN_LIMITS, _configuration_errors, _track_live_request
from tests.test_reporting import make_report
from tests.test_web import browser, config, finished, report, start


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate", "plan"])
async def test_a4_null_limits_round_trip_web_config_channels(tmp_path, endpoint):
    values = config(checks=["access"], concurrency=None, max_requests=None, max_duration_seconds=None)
    async with browser(tmp_path) as client:
        first = await client.post(f"/api/config/{endpoint}", json=values)
        assert first.status_code == 200, first.text
        normalized = first.json()["config"]
        assert normalized["config_version"] == 2
        assert all(normalized[field] is None for field in
                   ("concurrency", "max_requests", "max_duration_seconds"))
        second = await client.post(f"/api/config/{endpoint}", json=normalized)
        assert second.status_code == 200, second.text
        assert second.json()["config"] == normalized
        assert second.json()["migration_notes"] == []


async def test_a19_legacy_web_config_migrates_before_validation_and_reports_notes(tmp_path):
    values = {"targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}],
              "checks": ["correctness"], "concurrency": 40}
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/validate", json=values)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["config"]["concurrency"] == result["config"]["default_concurrency"] == 40
    assert result["config"]["max_requests"] is result["config"]["max_duration_seconds"] is None
    assert len(result["migration_notes"]) == 3
    assert any("default_concurrency" in note for note in result["migration_notes"])


async def test_a4_fresh_bootstrap_has_browser_limits_without_migration_notes(tmp_path):
    async with browser(tmp_path) as client:
        result = (await client.get("/api/bootstrap")).json()
    assert result["config"]["config_version"] == 2
    assert {field: result["config"][field] for field in BROWSER_DEFAULT_RUN_LIMITS} == BROWSER_DEFAULT_RUN_LIMITS
    assert result["migration_notes"] == []


async def test_a19_start_preserves_migration_notes_and_null_limits_in_saved_config(tmp_path, monkeypatch):
    seen = []

    async def runner(configuration, *, progress, stop_event):
        seen.append(configuration)
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    values = {"targets": [{"name": "local", "url": "http://localhost/v1", "model": "test"}],
              "checks": ["access"]}
    async with browser(tmp_path) as client:
        identifier = await start(client, values)
        detail = await finished(client, identifier)
        saved = await client.get(f"/api/runs/{identifier}/config.json")
    assert len(seen[0]._migration_notes) == 3
    assert saved.status_code == 200
    assert saved.json()["max_requests"] is saved.json()["concurrency"] is None
    assert detail["config"]["config_version"] == 2


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate", "plan"])
async def test_a9_correctness_64_vs_40_conflict_reaches_each_api_boundary(tmp_path, endpoint):
    values = config(checks=["correctness"], concurrency=40)
    values["test_options"] = {"correctness": {"concurrency": 64}}
    async with browser(tmp_path) as client:
        response = await client.post(f"/api/config/{endpoint}", json=values)
    assert response.status_code == 422, response.text
    error = response.json()["detail"][0]
    assert error["fields"] == ["test_options.correctness.concurrency", "concurrency"]
    assert error["values"] == {"test_options.correctness.concurrency": 64, "concurrency": 40}
    assert "64 at once" in error["msg"] and "(40)" in error["msg"]


async def test_inherited_parallelism_notice_is_nonblocking_plan_warning_with_fields(tmp_path):
    values = config(checks=["correctness"], concurrency=2, default_concurrency=4)
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 200, response.text
    notice = response.json()["request_plan"]["concurrency_warnings"][0]
    assert notice["fields"] == ["default_concurrency", "concurrency"]
    assert notice["values"] == {"default_concurrency": 4, "concurrency": 2}
    assert notice["effective_concurrency"] == 2


def test_a9_structured_conflict_exposes_both_fields_and_only_numeric_values():
    fields = ["test_options.correctness.concurrency", "concurrency"]
    errors = _configuration_errors([{"loc": ("body",), "msg": "Correctness runs 64 at once, above Simultaneous limit (40).",
        "type": "concurrency_conflict", "ctx": {"fields": fields,
        "values": {fields[0]: 64, "concurrency": 40, "secret": "private"}}}], {})
    assert errors[0]["fields"] == fields
    assert errors[0]["values"] == {fields[0]: 64, "concurrency": 40}


def live_job():
    return {"requests": OrderedDict(), "live_request_summary": {
        "evicted": 0, "through_slot": None, "statuses": {}},
        "live_request_count": 0, "evicted_running": set()}


def test_a25_backend_unlimited_slots_keep_recent_window_and_complete_evicted_summary():
    job = live_job()
    for slot in range(4100):
        _track_live_request(job, {"event": "request_started", "request_slot": slot}, None)
    assert len(job["requests"]) == 4096
    assert list(job["requests"])[0] == 4
    assert job["live_request_count"] == 4100
    assert job["live_request_summary"] == {"evicted": 4, "through_slot": 3,
                                             "statuses": {"running": 4}}
    for slot in range(4):
        _track_live_request(job, {"event": "request_completed", "request_slot": slot,
                                 "request": {"status": "completed"}}, None)
    assert len(job["requests"]) == 4096
    assert list(job["requests"])[0] == 4
    assert job["evicted_running"] == set()
    assert job["live_request_summary"]["statuses"] == {"running": 0, "completed": 4}


def test_live_slots_respect_explicit_budget_and_do_not_reorder_on_completion():
    job = live_job()
    for slot in range(3):
        _track_live_request(job, {"event": "request_started", "request_slot": slot}, 2)
    _track_live_request(job, {"event": "request_completed", "request_slot": 0,
                             "request": {"status": "completed"}}, 2)
    assert list(job["requests"]) == [0, 1]
    assert job["live_request_count"] == 2


def test_a23_old_baseline_matches_v2_effective_workload_despite_optional_limits():
    current, old = make_report(), make_report("baseline")
    old_config = old.manifest["config"]
    old_config.pop("config_version")
    old_config.pop("default_concurrency", None)
    old_config.update(concurrency=4, max_requests=300, max_duration_seconds=300)
    for value in (current, old):
        value.manifest["effective_checks"] = {name: RunConfig.model_validate(current.manifest["config"]).effective_check(name)
                                               for name in current.manifest["config"]["checks"]}
    assert _settings_reasons(current, old) == []
    assert "config_version" not in old.manifest["config"]  # Comparing is read-only.


@pytest.mark.parametrize("field", ["max_requests", "max_duration_seconds", "concurrency"])
def test_baseline_limit_is_workload_setting_only_when_it_constrained_work(field):
    current, old = make_report(), make_report("baseline")
    old.manifest["config"][field] = 40
    assert _settings_reasons(current, old) == []
    current.limits_reached = [field]
    assert any(field in reason and "limit stopped work" in reason for reason in _settings_reasons(current, old))


def test_a23_legacy_arrivals_inflight_default_normalizes_without_losing_workload_changes():
    current, old = make_report(), make_report("baseline")
    current.manifest["config"].update(checks=["arrivals"], arrivals={"requests": 10, "max_in_flight": None})
    old.manifest["config"] = deepcopy(current.manifest["config"])
    old.manifest["config"].pop("config_version")
    old.manifest["config"].pop("default_concurrency", None)
    old.manifest["config"]["arrivals"].pop("max_in_flight")
    assert _settings_reasons(current, old) == []
    current.manifest["config"]["arrivals"]["max_in_flight"] = 8
    assert "Run setting arrivals differs." in _settings_reasons(current, old)


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate", "plan"])
async def test_inherited_parallelism_survives_api_roundtrip_and_raising_limit(tmp_path, endpoint):
    values = config(checks=["sessions", "consistency", "load"], concurrency=1)
    async with browser(tmp_path) as client:
        response = await client.post(f"/api/config/{endpoint}", json=values)
        assert response.status_code == 200, response.text
        initial = response.json()
        for _ in range(2):
            normalized = initial["config"]
            assert "concurrency" not in normalized["sessions"]
            assert "levels" not in normalized["consistency"]
            assert "levels" not in normalized["load_test"]["patterns"]["concurrent"]
            assert initial["request_plan"]["required_concurrency"]["by_check"] == {
                "sessions": 1, "consistency": 1, "load": 1}
            notices = initial["request_plan"]["concurrency_warnings"]
            assert {notice["check"] for notice in notices} == {"sessions", "consistency", "load"}
            assert all(notice["effective_concurrency"] == 1 for notice in notices)
            repeated = await client.post(f"/api/config/{endpoint}", json=normalized)
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["request_plan"]["concurrency_warnings"] == notices
            initial = repeated.json()
        normalized["concurrency"] = 4
        raised = await client.post(f"/api/config/{endpoint}", json=normalized)
    assert raised.status_code == 200, raised.text
    result = raised.json()
    assert result["request_plan"]["concurrency_warnings"] == []
    assert result["request_plan"]["required_concurrency"]["by_check"] == {
        "sessions": 2, "consistency": 2, "load": 4}
    assert result["request_plan"]["load_test"]["scheduled_tests"] == 12
    resolved = RunConfig.model_validate(result["config"])
    assert resolved.sessions.concurrency == 2
    assert resolved.consistency.levels == [1, 2]
    assert resolved.load_test.patterns.concurrent.levels == [1, 4]


@pytest.mark.parametrize("reached", [False, True])
def test_a23_real_load_shape_quotas_matter_only_when_request_limit_stopped_work(reached):
    current, old = make_report(), make_report("baseline")
    for value in (current, old):
        value.manifest["load_shape"] = {"arrival_rates_rps": [1, 4, 8], "arrival": "constant",
                                        "overlap_models": False, "target_request_quotas": {}}
    old.manifest["config"]["max_requests"] = 300
    old.manifest["load_shape"]["target_request_quotas"] = {"local": 300}
    if reached:
        old.limits_reached = ["max_requests"]
    reasons = _settings_reasons(current, old)
    if reached:
        assert "load_shape differs." in reasons
        assert any("max_requests differs" in reason for reason in reasons)
    else:
        assert reasons == []
    assert old.manifest["load_shape"]["target_request_quotas"] == {"local": 300}
