"""Cold-start admission and preview use ASGI transports without binding sockets."""
import httpx
import pytest

from giraffe.models import RunReport
from giraffe.reporting import _cold_start_html, _render, write_report
from giraffe.web import create_app
from tests.test_cold_start_script import script


def values(path=None):
    return {"config_version": 2, "checks": ["access"], "targets": [{
        "name": "local", "url": "http://localhost:11434/v1", "model": "tiny",
        "restart_script": str(path) if path else None}]}


def client(app, token=True):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:8765", headers={"X-Giraffe-Token": "test"} if token else {})


@pytest.fixture
def web_auto_auth():
    return False


async def test_preview_requires_auth_and_executable_file_and_uses_existing_csp(tmp_path):
    path = script(tmp_path, "echo '<script>alert(1)</script>'\n")
    app = create_app(tmp_path / "runs", token="test")
    async with client(app, False) as http:
        assert (await http.post("/api/targets/restart-script/preview", json={"path": str(path)})).status_code == 401
    async with client(app) as http:
        preview = await http.post("/api/targets/restart-script/preview", json={"path": str(path)})
        assert preview.status_code == 200
        assert preview.json()["contents"] == path.read_text()
        assert preview.json()["path"] == str(path.resolve())
        assert "script-src 'self'" in (await http.get("/")).headers["content-security-policy"]
        path.chmod(0o600)
        invalid = await http.post("/api/targets/restart-script/preview", json={"path": str(path)})
        assert invalid.status_code == 422 and "executable" in invalid.json()["detail"][0]["msg"]


@pytest.mark.parametrize("contents,message", [
    ("#!/bin/sh\n# hidden\u200btext\n", "Restart script contains control or invisible characters. Remove them to review it safely."),
    ("echo not-a-script\n", "Restart script must start with a #! line.")])
async def test_preview_and_run_reject_unsafe_script_even_with_matching_hash(tmp_path, contents, message):
    import hashlib

    path = script(tmp_path, "")
    path.write_bytes(contents.encode())
    async with client(create_app(tmp_path / "runs", token="test")) as http:
        preview = await http.post("/api/targets/restart-script/preview", json={"path": str(path)})
        assert preview.status_code == 422
        assert preview.json()["detail"][0]["msg"] == message
        response = await http.post("/api/runs", json={"config": values(path), "mode": "cold-start",
            "restart_target": "local", "restart_script_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        assert response.status_code == 422
        assert message in str(response.json())


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.2.3.4"])
async def test_loopback_allows_scripts_by_default(tmp_path, host):
    async with client(create_app(tmp_path, token="test", host=host)) as http:
        assert (await http.get("/api/capabilities")).json()["restart_allowed"] is True


@pytest.mark.parametrize("enabled", [False, True])
async def test_nonloopback_needs_flag_for_preview_and_run(tmp_path, enabled):
    path = script(tmp_path, "exit 0\n")
    app = create_app(tmp_path / "runs", token="test", host="0.0.0.0", allow_restart_script=enabled)
    async with client(app) as http:
        assert (await http.get("/api/bootstrap")).json()["restart_allowed"] is enabled
        response = await http.post("/api/targets/restart-script/preview", json={"path": str(path)})
        assert response.status_code == (200 if enabled else 403)
        response = await http.post("/api/runs", json={"config": values(path), "mode": "cold-start", "restart_target": "local"})
        assert response.status_code == (422 if enabled else 403)


async def test_api_missing_confirmation_config_hash_and_changed_script_never_run(tmp_path):
    marker = tmp_path / "ran"
    path = script(tmp_path, f"touch '{marker}'\n")
    app = create_app(tmp_path / "runs", token="test")
    async with client(app) as http:
        preview = (await http.post("/api/targets/restart-script/preview", json={"path": str(path)})).json()
        body = {"config": values(path), "mode": "cold-start", "restart_target": "local"}
        response = await http.post("/api/runs", json=body)
        assert response.status_code == 422 and "confirm" in response.text
        body["config"]["restart_script_sha256"] = preview["sha256"]
        assert (await http.post("/api/runs", json=body)).status_code == 422
        body["restart_script_sha256"] = preview["sha256"]
        path.write_text(path.read_text()+"echo changed\n")
        response = await http.post("/api/runs", json=body)
        assert response.status_code == 409
        assert response.json()["detail"] == "The script changed. Review it again."
        assert not marker.exists()


async def test_migration_is_visible_and_drops_old_restart_intent(tmp_path):
    config = values()
    config["targets"][0]["restart_command"] = ["never", "run"]
    config["restart_target"] = "local"
    async with client(create_app(tmp_path, token="test", defaults=config)) as http:
        bootstrap = (await http.get("/api/bootstrap")).json()
        assert "restart_command" not in bootstrap["config"]["targets"][0]
        assert bootstrap["config"]["restart_target"] is None
        assert any("put it in a script" in note for note in bootstrap["migration_notes"])


def test_html_json_cold_measurements_escape_script_output_and_keep_no_script_output(tmp_path):
    report = RunReport(run_id="cold", started_at="then", finished_at="now", overall="pass", manifest={})
    assert _cold_start_html(report) == ""
    cold = {"status": "ready", "script_ms": 15, "service_answered_ms": 20, "readiness_ms": 30,
        "model_load": "Model was already loaded", "model_loaded_before_first_request": "Yes",
        "first_request": {"first_output_ms": 4, "elapsed_ms": 6},
        "warm_requests": [{"first_output_ms": 2, "elapsed_ms": 3}] * 2,
        "output_tail": "<script>alert('secret')</script>"}
    report.observations = {"targets": {"<b>local</b>": {"restart": cold}}}
    html = _render(report)
    assert "Cold start" in html and "Service answered after" in html and "Warm request 2" in html
    assert "<script>alert(" not in html and "&lt;script&gt;" in html
    assert "<b>local</b>" not in html and "&lt;b&gt;local&lt;/b&gt;" in html
    assert "Model was already loaded" in html and "Some servers load weights" in html
    write_report(report, tmp_path)
    loaded = RunReport.model_validate_json((tmp_path / "report.json").read_text())
    assert loaded.observations["targets"]["<b>local</b>"]["restart"] == cold
