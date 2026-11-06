"""The CLI must not spend a shared request before explicit identity probes."""

import hashlib
import json

import httpx
import pytest

from giraffe.cli import main
from tests.test_gateway_attribution import config, immediate_spend_polls, spend_page


@pytest.mark.parametrize("shared", ["absent", "configured", "missing_env"])
def test_gateway_only_cli_sends_only_selected_identity_completions(shared, monkeypatch, tmp_path, capsys):
    run_cli(monkeypatch, tmp_path, capsys, shared=shared)


def test_gpu_with_attribution_does_not_require_a_shared_key(monkeypatch, tmp_path, capsys):
    run_cli(monkeypatch, tmp_path, capsys, shared="missing_env", gpu=True)


def test_mixed_cli_retains_shared_completion_preflight(monkeypatch, tmp_path, capsys):
    run_cli(monkeypatch, tmp_path, capsys, shared="configured", mixed=True)


@pytest.mark.parametrize("usage_case", ["completion_duplicate", "sse_duplicate", "sse_conflict", "sse_repeated"])
def test_gateway_cli_requires_unambiguous_final_usage(monkeypatch, tmp_path, capsys, usage_case):
    run_cli(monkeypatch, tmp_path, capsys, shared="absent", usage_case=usage_case)


@pytest.mark.parametrize("caller_session", ["body", "header"])
def test_gateway_cli_uses_own_session_only_for_marked_probes(monkeypatch, tmp_path, capsys, caller_session):
    run_cli(monkeypatch, tmp_path, capsys, shared="configured", mixed=True, caller_session=caller_session)


def run_cli(monkeypatch, tmp_path, capsys, *, shared, mixed=False, gpu=False, usage_case="", caller_session=""):
    stream = usage_case.startswith("sse_")
    cfg = config(stream=stream)
    immediate_spend_polls(monkeypatch, cfg)
    if caller_session:
        cfg.request_options.update(litellm_session_id="existing-session", metadata={"project": "ordinary"})
        cfg.targets[0].test_identities["alice"].headers_env["X-Proxy-Credential"] = "CUSTOM_CREDENTIAL"
        monkeypatch.setenv("CUSTOM_CREDENTIAL", "secret-custom")
    if caller_session == "header":
        cfg.targets[0].headers_env["X-LiteLLM-Session-ID"] = "SHARED_SESSION"
        monkeypatch.setenv("SHARED_SESSION", "shared-header-session")
        for identity in cfg.targets[0].test_identities.values():
            identity.headers_env["X-LiTeLLM-SeSSion-Id"] = "IDENTITY_SESSION"
        monkeypatch.setenv("IDENTITY_SESSION", "identity-header-session")
        bob = cfg.targets[0].test_identities["bob"]
        bob.api_key_env = None
        bob.headers_env["X-LiteLLM-API-Key"] = "BOB"
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
    if gpu:
        cfg.checks.append("gpu")
    rows, completions, marked_sessions, shared_sessions = [], [], [], []

    def handler(request):
        if request.method == "GET":
            if request.url.path == "/spend/logs/v2":
                assert request.headers["authorization"] == "Bearer secret-OBSERVER"
                return spend_page(request, [row for row in rows if row["session_id"] == request.url.params["session_id"]])
            return httpx.Response(404)
        authorization = request.headers.get("authorization")
        credential = authorization or request.headers.get("x-litellm-api-key")
        completions.append((credential, request.headers.get("x-giraffe-preflight")))
        if credential not in {"Bearer secret-ALICE", "Bearer secret-BOB", "secret-BOB", "Bearer shared-secret"}:
            return httpx.Response(401, json={"error": {"type": "auth_error"}})
        body = json.loads(request.content)
        session_header = request.headers.get("x-litellm-session-id")
        if "x-litellm-spend-logs-metadata" in request.headers:
            marker = request.headers["x-litellm-trace-id"]
            # The spend logger prioritizes a caller session header beside the trace header.
            session = session_header or body.get("litellm_session_id") or marker
            marked_sessions.append((marker, session, body.get("litellm_session_id"), body.get("metadata")))
            if caller_session and authorization == "Bearer secret-ALICE":
                assert request.headers["x-proxy-credential"] == "secret-custom"
            rows.append({"request_id": f"request-{len(rows)}",
                         "session_id": session,
                         "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                         "api_key": hashlib.sha256(credential.removeprefix("Bearer ").encode()).hexdigest(),
                         "prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8, "status": "success"})
            if usage_case == "completion_duplicate":
                return httpx.Response(200, content=(
                    '{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}],'
                    '"usage":{"prompt_tokens":3,"completion_tokens":99,"completion_tokens":5}}'))
            if stream:
                usage = '{"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}'
                final_events = [usage]
                if usage_case == "sse_duplicate":
                    final_events = [usage.replace('"completion_tokens":5', '"completion_tokens":99,"completion_tokens":5')]
                elif usage_case == "sse_conflict":
                    final_events = [usage.replace('"completion_tokens":5', '"completion_tokens":99'), usage]
                else:
                    final_events = [usage, usage]
                events = ['{"choices":[{"delta":{"content":"OK"}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}',
                          '{"choices":[{"delta":{},"finish_reason":"stop"}]}', *final_events, "[DONE]"]
                return httpx.Response(200, content="".join(f"data: {event}\n\n" for event in events),
                                      headers={"Content-Type": "text/event-stream"})
        elif authorization == "Bearer shared-secret":
            shared_sessions.append((body.get("litellm_session_id"), body.get("metadata"), session_header))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 3, "completion_tokens": 5}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json())
    code = main(["run", "--config", str(path), "--output", str(tmp_path / "run"), "--output-format", "json"])
    output = json.loads(capsys.readouterr().out)
    invalid_usage = usage_case in {"completion_duplicate", "sse_duplicate", "sse_conflict"}
    assert code == (2 if invalid_usage else 0), output
    report = json.loads((tmp_path / "run" / "report.json").read_text())
    check = next(row for row in report["checks"] if row["id"] == "gateway_attribution")
    assert check["metrics"]["state"] == ("unverified" if invalid_usage else "verified")
    assert check["metrics"]["matched_requests"] == (0 if invalid_usage else 2)
    assert check["metrics"]["observed_requests"] == 2
    assert [auth for auth, _ in completions if auth != "Bearer shared-secret"] == [
        "Bearer secret-ALICE", "secret-BOB" if caller_session == "header" else "Bearer secret-BOB"]
    if mixed:
        assert completions[0] == ("Bearer shared-secret", "1")
    else:
        assert len(completions) == 2
        assert all(preflight is None for _, preflight in completions)
    if caller_session:
        assert len(marked_sessions) == 2 and all(
            marker == session == body_session and marker.startswith("giraffe-") and metadata == {"project": "ordinary"}
            for marker, session, body_session, metadata in marked_sessions)
        # The CLI's preliminary connectivity probe has no workload options.
        assert shared_sessions[-1] == ("existing-session", {"project": "ordinary"},
                                      "shared-header-session" if caller_session == "header" else None)
