"""The CLI must not spend a shared request before explicit identity probes."""

import hashlib
import json

import httpx
import pytest

from giraffe.cli import main
from tests.test_gateway_attribution import config, spend_page


@pytest.mark.parametrize("shared", ["absent", "configured", "missing_env"])
def test_gateway_only_cli_sends_only_selected_identity_completions(shared, monkeypatch, tmp_path, capsys):
    run_cli(monkeypatch, tmp_path, capsys, shared=shared)


def test_mixed_cli_retains_shared_completion_preflight(monkeypatch, tmp_path, capsys):
    run_cli(monkeypatch, tmp_path, capsys, shared="configured", mixed=True)


def run_cli(monkeypatch, tmp_path, capsys, *, shared, mixed=False):
    cfg = config(stream=False)
    if shared != "absent":
        cfg.targets[0].api_key_env = "SHARED_KEY"
    if shared == "configured":
        monkeypatch.setenv("SHARED_KEY", "shared-secret")
    else:
        monkeypatch.delenv("SHARED_KEY", raising=False)
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, "secret-" + ref)
    if mixed:
        cfg.checks.insert(0, "access")
    rows, completions = [], []

    def handler(request):
        if request.method == "GET":
            if request.url.path == "/spend/logs/v2":
                assert request.headers["authorization"] == "Bearer secret-OBSERVER"
                return spend_page(request, rows)
            return httpx.Response(404)
        authorization = request.headers.get("authorization")
        completions.append((authorization, request.headers.get("x-giraffe-preflight")))
        if authorization not in {"Bearer secret-ALICE", "Bearer secret-BOB", "Bearer shared-secret"}:
            return httpx.Response(401, json={"error": {"type": "auth_error"}})
        if "x-litellm-spend-logs-metadata" in request.headers:
            rows.append({"request_id": f"request-{len(rows)}",
                         "session_id": request.headers["x-litellm-trace-id"],
                         "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                         "api_key": hashlib.sha256(authorization.removeprefix("Bearer ").encode()).hexdigest(),
                         "prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8, "status": "success"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 3, "completion_tokens": 5}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json())
    code = main(["run", "--config", str(path), "--output", str(tmp_path / "run"), "--output-format", "json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    report = json.loads((tmp_path / "run" / "report.json").read_text())
    check = next(row for row in report["checks"] if row["id"] == "gateway_attribution")
    assert check["metrics"]["state"] == "verified"
    assert [auth for auth, _ in completions if auth != "Bearer shared-secret"] == [
        "Bearer secret-ALICE", "Bearer secret-BOB"]
    if mixed:
        assert completions[0] == ("Bearer shared-secret", "1")
    else:
        assert len(completions) == 2
        assert all(preflight is None for _, preflight in completions)
