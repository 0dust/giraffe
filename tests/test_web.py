"""Exercise browser job boundaries and the real suite through its local HTTP API."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from tests.fake_endpoint import FakeEndpoint
from giraffe.models import CheckResult, RequestRecord, RunConfig, RunReport
from giraffe.reporting import write_report
from giraffe.web import create_app


def config(**updates):
    return RunConfig(targets=[{"name": "local", "url": "http://localhost:11434/v1",
                               "model": "test-model"}], **updates).model_dump(mode="json")


def report(configuration, *, cancelled=False):
    return RunReport(run_id="suite-run-id", started_at="2026-10-04T10:00:00+00:00",
                     finished_at="2026-10-04T10:00:01+00:00",
                     overall="inconclusive" if cancelled else "pass",
                     manifest={"config": configuration.model_dump(mode="json")},
                     abort_reason="cancelled by user" if cancelled else None,
                     checks=[CheckResult(id="serving", target="local", title="Serving success",
                                         status="inconclusive" if cancelled else "pass",
                                         summary="Interrupted" if cancelled else "Completed")],
                     requests=[RequestRecord(id="request-one", target="local", fixture_id="short",
                                             scenario="warm", check_ids=["serving"],
                                             started_at="2026-10-04T10:00:00+00:00",
                                             status="completed", valid=True, score=True,
                                             output="private successful answer", output_chars=25)])


async def successful_runner(configuration, *, progress, stop_event):
    progress({"event": "request_completed", "target": "local", "scenario": "warm",
              "completed": 1, "attempted": 1})
    await asyncio.sleep(0.01)
    return report(configuration)


@asynccontextmanager
async def browser(tmp_path, **kwargs):
    app = create_app(tmp_path, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost:8787") as client:
            yield client


async def finished(client, identifier):
    for _ in range(200):
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        if detail["state"] not in {"running", "stopping"}:
            return detail
        await asyncio.sleep(0.01)
    pytest.fail("Run did not finish")


async def start(client, configuration=None, **values):
    response = await client.post("/api/runs", json={"config": configuration or config(), **values})
    assert response.status_code == 202, response.text
    return response.json()["id"]


@pytest.fixture
def fake_runner(monkeypatch):
    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)


async def test_bootstrap_has_blank_model_and_no_automatic_traffic(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Bootstrap must not send inference requests")
    monkeypatch.setattr("giraffe.web.run_suite", forbidden)
    async with browser(tmp_path) as client:
        result = (await client.get("/api/bootstrap")).json()
        assert result["config"]["targets"][0]["model"] == ""
        assert result["config"]["restart_target"] is None
        assert len(result["checks"]) == 12
        assert {c["id"] for c in result["checks"] if c["optional"]} == {"json", "gpu"}
        assert result["active_run_id"] is None
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_job_lifecycle_reads_retained_artifacts(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert detail["state"] == "completed"
        assert detail["report"]["requests"][0]["output"] == ""
        assert detail["report"]["run_id"] == "suite-run-id"
        listing = (await client.get("/api/runs")).json()
        assert listing["active_run_id"] is None
        assert listing["runs"][0]["id"] == identifier
        assert listing["runs"][0]["overall"] == "pass"
        assert listing["runs"][0]["request_count"] == 1
        assert listing["runs"][0]["check_counts"]["pass"] == 1
        saved = await client.get(f"/api/runs/{identifier}/report.json")
        assert saved.status_code == 200
        assert "attachment" in saved.headers["content-disposition"]
        assert saved.json() == detail["report"]
        html = await client.get(f"/api/runs/{identifier}/report.html")
        assert html.status_code == 200
        assert "private successful answer" not in html.text
        assert (await client.get(f"/api/runs/{identifier}/report.exe")).status_code == 404
    async with browser(tmp_path) as client:
        assert (await client.get(f"/api/runs/{identifier}")).json()["state"] == "completed"


async def test_one_active_run_progress_and_cancel_preserves_partial_report(tmp_path, monkeypatch):
    async def waiting(configuration, *, progress, stop_event):
        for i in range(150):
            progress({"event": "request_completed", "completed": i, "scenario": "warm"})
        await stop_event.wait()
        return report(configuration, cancelled=True)
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await asyncio.sleep(0)
        second = await client.post("/api/runs", json={"config": config()})
        assert second.status_code == 409
        detail = (await client.get(f"/api/runs/{identifier}")).json()
        assert detail["progress"]["completed"] == 149
        assert len(detail["events"]) == 100
        assert detail["report"] is None
        assert (await client.get("/api/bootstrap")).json()["active_run_id"] == identifier
        assert (await client.post(f"/api/runs/{identifier}/cancel", json={})).status_code == 200
        detail = await finished(client, identifier)
        assert detail["report"]["abort_reason"] == "cancelled by user"
        assert detail["report"]["overall"] == "inconclusive"
        # Repeated cancellation cannot mutate the completed report.
        assert (await client.post(f"/api/runs/{identifier}/cancel", json={})).json()["state"] == "completed"


async def test_cancel_still_stops_traffic_when_status_write_fails(tmp_path, monkeypatch):
    from giraffe.web import _write

    stopped = asyncio.Event()

    async def waiting(configuration, *, progress, stop_event):
        await stop_event.wait()
        stopped.set()
        return report(configuration, cancelled=True)

    def fail_stopping_write(path, value):
        if value.get("state") == "stopping":
            raise OSError("disk full")
        return _write(path, value)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        monkeypatch.setattr("giraffe.web._write", fail_stopping_write)
        response = await client.post(f"/api/runs/{identifier}/cancel", json={})
        assert response.status_code == 200
        assert response.json()["state"] == "stopping"
        await asyncio.wait_for(stopped.wait(), timeout=1)
        detail = await finished(client, identifier)
        assert detail["report"]["abort_reason"] == "cancelled by user"


async def test_final_state_remains_visible_when_status_write_fails(tmp_path, monkeypatch):
    release = asyncio.Event()

    async def waiting(configuration, *, progress, stop_event):
        await release.wait()
        return report(configuration)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        monkeypatch.setattr("giraffe.web._write", fail_write)
        release.set()
        detail = await finished(client, identifier)
        assert detail["state"] == "completed"
        assert detail["report"]["overall"] == "pass"
        assert "status could not be saved" in detail["error"]
        assert (await client.get("/api/runs")).json()["active_run_id"] is None


async def test_shutdown_cooperatively_saves_report(tmp_path, monkeypatch):
    async def waiting(configuration, *, progress, stop_event):
        await stop_event.wait()
        return report(configuration, cancelled=True)
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
    saved = json.loads((tmp_path / identifier / "report.json").read_text())
    assert saved["abort_reason"] == "cancelled by user"
    assert json.loads((tmp_path / identifier / ".web.json").read_text())["state"] == "completed"


async def test_unexpected_failure_is_visible_without_exception_contents(tmp_path, monkeypatch):
    async def failing(*args, **kwargs):
        raise RuntimeError("secret-key-hidden")
    monkeypatch.setattr("giraffe.web.run_suite", failing)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert detail["state"] == "error"
        assert "RuntimeError" in detail["error"]
        assert "secret-key" not in json.dumps(detail)
        assert (await client.get("/api/runs")).json()["active_run_id"] is None
        assert detail["report"] is None


async def test_shutdown_timeout_marks_interrupted(tmp_path, monkeypatch):
    async def waiting(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    monkeypatch.setattr("giraffe.web._SHUTDOWN_SECONDS", 0.01)
    async with browser(tmp_path) as client:
        identifier = await start(client)
    assert json.loads((tmp_path / identifier / ".web.json").read_text())["state"] == "interrupted"


async def test_existing_cli_runs_and_interrupted_jobs_survive_restart(tmp_path):
    write_report(report(RunConfig.model_validate(config())), tmp_path / "from-cli")
    interrupted = tmp_path / "old-web"
    interrupted.mkdir()
    (interrupted / ".web.json").write_text(json.dumps({"state": "running", "config": config(),
                                                      "started_at": "2026-10-04T12:00:00Z"}))
    # A standalone baseline JSON is not a run directory.
    (tmp_path / "baseline.json").write_text("{}")
    async with browser(tmp_path) as client:
        rows = (await client.get("/api/runs")).json()["runs"]
        assert {row["id"] for row in rows} == {"from-cli", "old-web"}
        assert rows[0]["state"] == "interrupted"
        assert rows[1]["state"] == "completed"


async def test_baselines_are_explicit_named_copies_and_never_replaced_by_run(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        first = await start(client)
        await finished(client, first)
        assert (await client.get("/api/baselines")).json()["baselines"] == []
        response = await client.post("/api/baselines", json={"run_id": first, "name": "Before upgrade"})
        assert response.status_code == 201
        baseline = response.json()
        saved_path = tmp_path / ".baselines" / baseline["id"] / "report.json"
        original = saved_path.read_bytes()
        second = await start(client, baseline_id=baseline["id"])
        detail = await finished(client, second)
        assert detail["baseline_id"] == baseline["id"]
        assert detail["report"]["baseline"] is not None
        assert saved_path.read_bytes() == original
        assert (await client.post("/api/baselines", json={"run_id": second,
               "name": "Before upgrade"})).status_code == 409
        replaced = await client.post("/api/baselines", json={"run_id": second,
                                    "name": "Before upgrade", "replace": True})
        assert replaced.status_code == 201
        assert replaced.json()["id"] == baseline["id"]
        assert (await client.get("/api/baselines")).json()["baselines"][0]["run_id"] == second


async def test_unknown_baseline_rejected_before_any_run_starts(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        result = await client.post("/api/runs", json={"config": config(), "baseline_id": "unknown"})
        assert result.status_code == 404
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_invalid_configuration_and_custom_fixtures_preflight(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        invalid = config()
        invalid["targets"][0]["url"] = "https://secret-key@example.com"
        response = await client.post("/api/runs", json={"config": invalid})
        assert response.status_code == 422
        assert "secret-key" not in response.text
        missing = await client.post("/api/runs", json={"config": config(custom_fixtures="/missing")})
        assert missing.status_code == 422
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_normal_run_never_runs_restart_config(tmp_path, fake_runner):
    values = config(restart_target="local")
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    async with browser(tmp_path, defaults=values) as client:
        assert (await client.get("/api/bootstrap")).json()["config"]["restart_target"] is None
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == 422
        response = await client.post("/api/runs", json={"config": values, "mode": "cold-start"})
        assert response.status_code == 422
        identifier = await start(client, values, mode="cold-start", restart_target="local")
        assert (await finished(client, identifier))["config"]["restart_target"] == "local"


async def test_cold_start_requires_matching_target_with_hook(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", json={"config": config(), "mode": "cold-start",
                                                       "restart_target": "local"})
        assert response.status_code == 422
        values = config(restart_target="local")
        values["targets"].append({"name": "other", "url": "http://localhost:1234/v1",
                                  "model": "test", "restart_command": ["echo", "restart"]})
        response = await client.post("/api/runs", json={"config": values, "mode": "cold-start",
                                                       "restart_target": "other"})
        assert response.status_code == 422


async def test_mutations_require_json_and_same_origin(tmp_path, fake_runner):
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", content=json.dumps({"config": config()}))
        assert response.status_code == 415
        response = await client.post("/api/runs", json={"config": config()},
                                     headers={"Origin": "https://elsewhere.example"})
        assert response.status_code == 403
        malformed = await client.post("/api/runs", json={"config": config()},
                                      headers={"Origin": "http://["})
        assert malformed.status_code == 403
        response = await client.post("/api/runs", json={"config": config()},
                                     headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403
        response = await client.post("/api/runs", json={"config": config()},
                                     headers={"Origin": "http://localhost:8787"})
        assert response.status_code == 202
        await finished(client, response.json()["id"])


async def test_paths_and_symlinks_do_not_expose_unrelated_files(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write_report(report(RunConfig.model_validate(config())), outside)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "linked").symlink_to(outside, target_is_directory=True)
    (runs / "bad-artifact").mkdir()
    (runs / "bad-artifact" / "report.json").symlink_to(outside / "report.json")
    async with browser(runs) as client:
        assert (await client.get("/api/runs/linked")).status_code == 404
        assert (await client.get("/api/runs/linked/report.json")).status_code == 404
        assert (await client.get("/api/runs/bad-artifact/report.json")).status_code == 404
        assert (await client.get("/api/runs/%2E%2E%2Foutside")).status_code == 404
        assert all(row["id"] != "linked" for row in (await client.get("/api/runs")).json()["runs"])


async def test_real_api_to_runner_to_http_endpoint(tmp_path):
    with FakeEndpoint() as endpoint:
        values = config(checks=["access", "serving", "correctness"], concurrency=2,
                        samples=2, max_requests=30, max_duration_seconds=15)
        values["targets"][0]["url"] = endpoint.url
        async with browser(tmp_path) as client:
            identifier = await start(client, values)
            detail = await finished(client, identifier)
            assert detail["state"] == "completed"
            assert detail["report"]["overall"] == "pass"
            assert len(endpoint.requests) > 0
            assert len(detail["report"]["requests"]) == len(endpoint.requests)
            assert {check["id"] for check in detail["report"]["checks"]
                    if check["status"] != "skipped"} == {
                "access", "serving", "correctness"}
