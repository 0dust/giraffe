"""Per-launch UI access control, browser defenses and restart opt-in."""

import httpx
import pytest

from giraffe.models import RunConfig
from giraffe.reporting import write_report
from giraffe.web import create_app
from tests.test_web import config, report, saved_comparison_fixture


@pytest.fixture
def web_auto_auth():
    # These tests deliberately exercise requests without fixture credentials.
    return False


def client_for(app, **kwargs):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://localhost:8787", **kwargs)


@pytest.mark.parametrize("path", [
    "/api/bootstrap", "/api/capabilities", "/api/runs", "/api/storage",
    "/api/runs/missing/events?after=0", "/api/runs/missing/report.json",
    "/api/runs/missing/report.html", "/api/not-a-route",
])
async def test_every_api_path_requires_authentication(tmp_path, path):
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers={"X-Giraffe-Token": "wrong"})).status_code == 401


async def test_missing_origin_needs_authentication_for_mutations(tmp_path):
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app) as client:
        assert (await client.post("/api/config/validate", json=config())).status_code == 401
        response = await client.post("/api/config/validate", json=config(),
                                     headers={"X-Giraffe-Token": "launch-token"})
        assert response.status_code == 200
        for headers in ({"Origin": "http://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
            response = await client.post("/api/config/validate", json=config(), headers={
                "X-Giraffe-Token": "launch-token", **headers})
            assert response.status_code == 403


@pytest.mark.parametrize("credential", ["header", "cookie"])
async def test_saved_comparison_requires_auth_and_same_origin_before_writing(tmp_path, credential):
    saved_comparison_fixture(tmp_path, "before")
    saved_comparison_fixture(tmp_path, "after", latency=200)
    report_path = tmp_path / "after" / "report.json"
    original = report_path.read_bytes()
    app = create_app(tmp_path, token="launch-token")
    token = {"X-Giraffe-Token": "launch-token"}
    async with app.router.lifespan_context(app), client_for(app) as client:
        saved = await client.post("/api/baselines", json={"run_id": "before", "name": "Before"},
                                  headers=token)
        assert saved.status_code == 201
        baseline_id = saved.json()["id"]
        for headers, status in (
            ({}, 401),
            ({"X-Giraffe-Token": "wrong"}, 401),
            ({**token, "Origin": "http://attacker.example"}, 403),
            ({**token, "Origin": "https://localhost:8787"}, 403),
            ({**token, "Sec-Fetch-Site": "cross-site"}, 403),
        ):
            response = await client.post("/api/runs/after/compare",
                                         json={"baseline_id": baseline_id}, headers=headers)
            assert response.status_code == status, response.text
            assert report_path.read_bytes() == original
        if credential == "cookie":
            assert (await client.get("/?token=launch-token")).status_code == 303
            token = {}
        response = await client.post("/api/runs/after/compare", json={"baseline_id": baseline_id},
                                     headers={**token, "Origin": "http://localhost:8787"})
        assert response.status_code == 200, response.text
        listed = (await client.get("/api/runs", headers=token)).json()["runs"]
        assert next(row for row in listed if row["id"] == "after")["baseline_id"] == baseline_id
        exported = (await client.get("/api/runs/after/report.json", headers=token)).json()
        assert exported["baseline"]["run_id"] == "before"
        assert exported["baseline"]["saved_baseline_id"] == baseline_id


async def test_token_link_sets_http_only_cookie_and_plain_download_works(tmp_path):
    write_report(report(RunConfig.model_validate(config())), tmp_path / "saved")
    app = create_app(tmp_path, token="launch-token")
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.get("/")
            assert "Open the link printed by `giraffe ui` (it contains your access token)." in response.text
            assert "launch-token" not in response.text
            assert "app.js" not in response.text
            assert (await client.get("/?token=wrong")).status_code == 401
            response = await client.get("/?token=launch-token")
            assert response.status_code == 303
            assert response.headers["location"] == "/"
            cookie = response.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
            assert (await client.get("/")).status_code == 200
            # An ordinary <a href> request sends the cookie, without a custom header.
            response = await client.get("/api/runs/saved/report.json")
            assert response.status_code == 200
            assert response.json()["overall"] == "pass"


async def test_token_is_per_app_and_static_remains_public_with_csp(tmp_path):
    app = create_app(tmp_path)
    other = create_app(tmp_path)
    assert app.state.giraffe_token != other.state.giraffe_token
    assert len(app.state.giraffe_token) >= 32
    async with client_for(app) as client:
        for path in ("/", "/static/app.js", "/api/bootstrap"):
            response = await client.get(path)
            assert "content-security-policy" in response.headers
        assert (await client.get("/static/app.js")).status_code == 200
        assert (await client.get("/api/runs", headers={
            "X-Giraffe-Token": other.state.giraffe_token})).status_code == 401


@pytest.mark.parametrize("host,authenticated,status", [
    ("127.0.0.1", False, 400), ("127.0.0.1", True, 400),
    ("0.0.0.0", False, 401), ("0.0.0.0", True, 200),
    ("::", True, 200),
])
async def test_nonloopback_host_exception_needs_authentication(tmp_path, host, authenticated, status):
    app = create_app(tmp_path, token="launch-token", host=host)
    headers = {"Host": "shared.example:8787"}
    if authenticated:
        headers["X-Giraffe-Token"] = "launch-token"
    async with client_for(app) as client:
        assert (await client.get("/api/runs", headers=headers)).status_code == status
        if host == "0.0.0.0":
            response = await client.get("/?token=launch-token", headers={"Host": "shared.example:8787"})
            assert response.status_code == 303


@pytest.mark.parametrize("mode,hook", [("cold-start", None), ("cold-start", ["echo", "restart"])])
async def test_restart_requires_launch_opt_in(tmp_path, mode, hook):
    values = config()
    values["targets"][0]["restart_command"] = hook
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app, headers={"X-Giraffe-Token": "launch-token"}) as client:
        response = await client.post("/api/runs", json={"config": values, "mode": mode})
        assert response.status_code == 403
        assert "--allow-restart-command" in response.text
        assert (await client.get("/api/bootstrap")).json()["restart_allowed"] is False


async def test_restart_opt_in_exposed_and_preserves_cold_start_validation(tmp_path):
    app = create_app(tmp_path, token="launch-token", allow_restart_command=True)
    values = config()
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    async with client_for(app, headers={"X-Giraffe-Token": "launch-token"}) as client:
        assert (await client.get("/api/bootstrap")).json()["restart_allowed"] is True
        response = await client.post("/api/config/cli", json={
            "config": values, "mode": "cold-start", "restart_target": "local"})
        assert response.status_code == 200
        assert response.json()["command"].startswith("giraffe cold-start ")


@pytest.mark.parametrize("path", ["draft", "plan", "validate", "preview"])
async def test_submitted_restart_configuration_stays_editable(tmp_path, path):
    values = config()
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app, headers={"X-Giraffe-Token": "launch-token"}) as client:
        response = await client.post(f"/api/config/{path}", json=values)
        assert response.status_code == 200, response.text


async def test_copy_cold_start_cli_does_not_need_server_restart_permission(tmp_path):
    values = config()
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app, headers={"X-Giraffe-Token": "launch-token"}) as client:
        response = await client.post("/api/config/cli", json={
            "config": values, "mode": "cold-start", "restart_target": "local"})
        assert response.status_code == 200, response.text
        assert response.json()["command"].startswith("giraffe cold-start ")


@pytest.mark.parametrize("endpoint", ["/api/runs", "/api/runs/try-example"])
async def test_unused_restart_command_does_not_block_run_admission(tmp_path, monkeypatch, endpoint):
    from fastapi import HTTPException

    def reached_admission(config):
        raise HTTPException(409, "Reached normal run admission")

    monkeypatch.setattr("giraffe.web._validated_plan", reached_admission)
    values = config()
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    values.update(request_source="request_pack", request_pack={"version": 1, "cases": [{
        "fixture_id": "one", "messages": [{"role": "user", "content": "Hello"}], "scorer": "none"}]})
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app, headers={"X-Giraffe-Token": "launch-token"}) as client:
        response = await client.post(endpoint, json={"config": values,
            **({"fixture_id": "one", "target_name": "local"} if endpoint.endswith("try-example") else {})})
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Reached normal run admission"


async def test_try_example_rejects_restart_execution_intent(tmp_path):
    values = config()
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    values["restart_target"] = "local"
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app, headers={"X-Giraffe-Token": "launch-token"}) as client:
        response = await client.post("/api/runs/try-example", json={
            "config": values, "fixture_id": "one", "target_name": "local"})
        assert response.status_code == 403
        assert "--allow-restart-command" in response.text


async def test_cookie_credentials_for_two_ports_coexist(tmp_path):
    first = create_app(tmp_path, token="first-token", port=8765)
    second = create_app(tmp_path, token="second-token", port=8766)
    async with client_for(first) as one, client_for(second) as two:
        response = await one.get("http://localhost:8765/?token=first-token")
        assert response.headers["set-cookie"].startswith("giraffe_token_8765=")
        two.cookies.update(one.cookies)
        response = await two.get("http://localhost:8766/?token=second-token")
        assert response.headers["set-cookie"].startswith("giraffe_token_8766=")
        one.cookies.update(two.cookies)
        assert (await one.get("http://localhost:8765/api/capabilities")).status_code == 200
        assert (await two.get("http://localhost:8766/api/capabilities")).status_code == 200
        one.cookies.set("giraffe_token_8765", "wrong", domain="localhost.local", path="/")
        assert (await one.get("http://localhost:8765/api/capabilities")).status_code == 401


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "shared.example"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_nonlocal_bind_serves_access_instructions_before_auth(tmp_path, host, method):
    app = create_app(tmp_path, token="launch-token", host=host)
    async with client_for(app) as client:
        response = await client.request(method, "/", headers={"Host": "shared.example:8787"})
        assert response.status_code == 200
        if method == "GET":
            assert "Open the link printed by `giraffe ui`" in response.text
        else:
            assert response.content == b""
        assert "launch-token" not in response.text
        assert (await client.get("/api/bootstrap", headers={"Host": "shared.example:8787"})).status_code == 401


async def test_restart_target_in_execution_request_requires_opt_in(tmp_path):
    values = config()
    values["targets"][0]["restart_command"] = ["echo", "restart"]
    app = create_app(tmp_path, token="launch-token")
    async with client_for(app, headers={"X-Giraffe-Token": "launch-token"}) as client:
        response = await client.post("/api/runs", json={"config": values, "restart_target": "local"})
        assert response.status_code == 403
        assert "--allow-restart-command" in response.text
