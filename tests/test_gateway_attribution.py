"""Read-only attribution uses marked requests, never shared token counters."""
import hashlib
import json
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.models import RunConfig
from giraffe.runner import _Run


def config(**changes):
    return RunConfig(targets=[{"name": "proxy", "url": "http://proxy/v1", "model": "test",
        "test_identities": {"alice": {"api_key_env": "ALICE"}, "bob": {"api_key_env": "BOB"}},
        "gateway": {"profile": "litellm", "url": "http://proxy/spend/logs/v2", "api_key_env": "OBSERVER"}}],
        checks=["gateway_attribution"], max_requests=10, max_duration_seconds=10, max_memory_mb=0,
        gateway_attribution={"identities": ["alice", "bob"], "samples": 1, "settle_seconds": .05}, **changes)


async def exercise(monkeypatch, mode="match", *, stream=True, full=False, missing_shared=False):
    from giraffe.gateway_attribution import execute, result
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, "secret-" + ref)
    cfg = config(stream=stream)
    if missing_shared:
        monkeypatch.delenv("MISSING_SHARED_KEY", raising=False)
        cfg.targets[0].api_key_env = "MISSING_SHARED_KEY"
    run = _Run(cfg, None, None)
    rows, requests, reads = [], [], []

    def handler(req):
        if req.method == "GET":
            if req.url.path != "/spend/logs/v2":
                return httpx.Response(404)
            reads.append(req)
            assert req.headers["authorization"] == "Bearer secret-OBSERVER"
            if mode == "unavailable":
                return httpx.Response(403, text="secret-OBSERVER")
            if mode == "missing" or (mode == "lag" and len(reads) == 1):
                return httpx.Response(200, json={"data": [], "total_pages": 1})
            if mode == "malformed":
                return httpx.Response(200, json={"data": [{"api_key": "secret-ALICE"}]})
            if mode == "reset":
                snapshot = rows[:1] if len(reads) == 1 else rows[1:] if len(reads) == 2 else rows
                return httpx.Response(200, json={"data": snapshot, "total_pages": 1})
            if mode == "duplicate":
                return httpx.Response(200, json={"data": [*rows, {**rows[0], "request_id": "retry"}], "total_pages": 1})
            if mode == "truncated":
                return httpx.Response(200, json={"data": rows, "total_pages": 1, "total_is_capped": True})
            return httpx.Response(200, json={"data": [*rows, *rows[:1], {"request_id": "other"}], "total_pages": 1})
        requests.append(req)
        marker = json.loads(req.headers["x-litellm-spend-logs-metadata"])
        key = req.headers["authorization"].removeprefix("Bearer ")
        row = {"request_id": "gateway-" + str(len(rows)), "session_id": req.headers["x-litellm-trace-id"],
               "metadata": {"spend_logs_metadata": marker, "user_api_key": key},
               "api_key": hashlib.sha256(key.encode()).hexdigest(), "prompt_tokens": 3, "completion_tokens": 5,
               "total_tokens": 8, "status": "success"}
        if mode == "wrong_identity":
            row["api_key"] = "0" * 64
        if mode == "wrong_tokens":
            row.update(completion_tokens=7, total_tokens=10)
        if mode == "missing_usage":
            row.pop("completion_tokens")
        rows.append(row)
        if not stream:
            return httpx.Response(200, json={"choices": [{"message": {"content": key}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5}})
        return httpx.Response(200, text=(
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
            'data: [DONE]\n\n'))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    if full:
        from giraffe.runner import run_suite
        report = await run_suite(cfg)
        check = next(check for check in report.checks if check.id == "gateway_attribution")
        return cfg, report, check, requests, reads
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(run, cfg.targets[0], client)
    check = result(run, cfg.targets[0])
    return cfg, run, check, requests, reads


@pytest.mark.parametrize("stream", [True, False])
async def test_marked_usage_matches_and_stream_cumulative_counts_once(monkeypatch, stream):
    cfg, run, check, requests, reads = await exercise(monkeypatch, stream=stream)
    assert check.status == "pass" and check.metrics["state"] == "verified"
    assert check.metrics["matched_requests"] == 2
    assert check.metrics["completion_tokens"] == 10
    assert len(requests) == 2 and all(req.method == "GET" for req in reads)
    assert len(run.records) == 2
    serialized = json.dumps(check.model_dump()) + json.dumps(run.observations) + json.dumps([r.model_dump() for r in run.records])
    for ref in ("ALICE", "BOB", "OBSERVER"):
        assert "secret-" + ref not in serialized
        assert hashlib.sha256(("secret-" + ref).encode()).hexdigest() not in serialized


@pytest.mark.parametrize("mode,state,status", [
    ("wrong_identity", "mismatch", "fail"), ("wrong_tokens", "mismatch", "fail"),
    ("missing_usage", "unverified", "inconclusive"), ("missing", "unverified", "inconclusive"),
    ("malformed", "unverified", "inconclusive"), ("unavailable", "unavailable", "inconclusive"),
    ("lag", "verified", "pass"),
    ("reset", "unverified", "inconclusive"), ("duplicate", "unverified", "inconclusive"),
    ("truncated", "unverified", "inconclusive"),
])
async def test_observed_mismatch_is_distinct_from_incomplete_evidence(monkeypatch, mode, state, status):
    _, _, check, _, reads = await exercise(monkeypatch, mode)
    assert check.status == status and check.metrics["state"] == state
    assert len(reads) <= 24


def test_attribution_selection_is_independent_and_uses_existing_planning():
    from giraffe.planning import request_plan
    ordinary = RunConfig(targets=[{"name": "local", "url": "http://local", "model": "test"}])
    assert "gateway_attribution" not in ordinary.checks
    assert "gateway_attribution" not in ordinary.model_dump()
    planned = request_plan(config())
    assert any(row["id"] == "gateway_attribution" and row["requests_per_target"] == 2 for row in planned["rows"])


@pytest.mark.usefixtures("local_tokenizer")
async def test_complete_runner_reports_and_exports_safe_identity_samples(monkeypatch, tmp_path, capsys):
    from giraffe.cli import _print_result
    from giraffe.reporting import reproduction_export, write_report, compare_baseline
    cfg, report, check, requests, reads = await exercise(monkeypatch, full=True)
    assert check.status == "pass" and len(requests) == 2
    assert all(r.identity in {"alice", "bob"} for r in report.requests)
    from giraffe.performance import build_performance
    from giraffe.reporting import _groups
    performance = build_performance(report)
    assert not performance["targets"][0]["request_series"]
    assert not _groups(report.requests, "proxy")
    json_path, html_path = write_report(report, tmp_path, html=True)
    exported = reproduction_export(report)
    assert exported["gateway_attribution"]["proxy"]["matched_requests"] == 2
    assert exported["config"]["targets"][0]["test_identities"]["alice"]["api_key_env"] == "ALICE"
    assert RunConfig.model_validate(exported["config"]).gateway_attribution == cfg.gateway_attribution
    _print_result(report, json_path, html_path, tmp_path, {})
    cli = capsys.readouterr().out
    assert "alice" in cli and "matched" in cli
    assert "alice" in html_path.read_text()
    for text in (json_path.read_text(), html_path.read_text(), json.dumps(exported), cli):
        assert "secret-" not in text
    altered = report.model_copy(deep=True)
    altered.manifest["config"]["targets"][0]["test_identities"]["alice"]["api_key_env"] = "NEW_ALICE"
    baseline = compare_baseline(altered, report)
    assert baseline.baseline["status"] == "inconclusive"
    assert any("identity references" in reason for reason in baseline.baseline["targets"][0]["reasons"])


@pytest.mark.parametrize("profile,state", [(None, "unavailable"), ("other", "unsupported")])
async def test_missing_gateway_never_sends_inference(profile, state):
    from giraffe.gateway_attribution import execute, result
    cfg = config()
    if profile is None:
        cfg.targets[0].gateway = None
    else:
        cfg.targets[0].gateway.profile = profile
    run = _Run(cfg, None, None)
    await execute(run, cfg.targets[0], None)
    assert not run.records and result(run, cfg.targets[0]).metrics["state"] == state
    cfg.checks = []
    assert result(run, cfg.targets[0]).metrics["state"] == "not_selected"


async def test_budget_cutoff_preserves_inconclusive_partial_attribution(monkeypatch):
    from giraffe import gateway_attribution
    real = config
    monkeypatch.setattr(__import__(__name__, fromlist=["config"]), "config", lambda **kwargs: real(**kwargs).model_copy(update={"max_requests": 1}))
    _, run, check, requests, reads = await exercise(monkeypatch)
    assert len(requests) == 1 and len(run.records) == 1
    assert check.status == "inconclusive"
    assert check.metrics["planned_requests"] == 2
    assert check.metrics["matched_requests"] == 1
    assert len(reads) <= gateway_attribution.MAX_PAGES * 3


@pytest.mark.usefixtures("local_tokenizer")
async def test_gateway_only_runner_does_not_resolve_unused_shared_credential(monkeypatch):
    _, report, check, requests, _ = await exercise(monkeypatch, full=True, missing_shared=True)
    assert check.status == "pass" and len(requests) == 2
    assert not report.abort_reason


async def test_reader_pages_individually_and_never_follows_redirects():
    from giraffe.gateway_attribution import _pages
    seen = []

    def handler(req):
        seen.append(req)
        page = int(req.url.params["page"])
        return httpx.Response(200, json={"data": [{"page": page}], "total_pages": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        rows, complete = await _pages(client, "http://proxy/spend/logs/v2", "marker", max_rows=4, stop=asyncio.Event())
    assert rows == [{"page": 1}, {"page": 2}] and complete
    assert [req.url.params["page"] for req in seen] == ["1", "2"]
    assert all(req.url.params["session_id"] == "marker" and req.method == "GET" for req in seen)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(302,
        headers={"location": "http://elsewhere/secret"})), follow_redirects=False) as client:
        rows, complete = await _pages(client, "http://proxy/spend/logs/v2", "marker", max_rows=4, stop=asyncio.Event())
    assert rows == "unavailable" and not complete


@pytest.mark.parametrize("reply,max_rows", [
    ({"data": [{"row": 1}] * 3, "total_pages": 1}, 2),
    ({"data": [{"row": 1}], "total_pages": True}, 2),
    ({"data": "unexpected", "total_pages": 1}, 2),
])
async def test_reader_limits_and_malformed_pages_are_incomplete(reply, max_rows):
    from giraffe.gateway_attribution import _pages
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=reply))) as client:
        _, complete = await _pages(client, "http://proxy/spend/logs/v2", "marker", max_rows=max_rows, stop=asyncio.Event())
    assert not complete


async def test_missing_observer_reference_sends_no_requests(monkeypatch):
    from giraffe.gateway_attribution import execute, result
    cfg = config()
    monkeypatch.delenv("OBSERVER", raising=False)
    run = _Run(cfg, None, None)
    await execute(run, cfg.targets[0], None)
    assert not run.records and result(run, cfg.targets[0]).metrics["state"] == "unavailable"


def test_conflicting_repeated_row_and_anonymous_attribution_are_unverified():
    from giraffe.gateway_attribution import _normalize, _assess
    record = SimpleNamespace(id="local", identity="alice", _gateway_key_hash="a"*64,
                             input_tokens=2, completion_tokens=3, status="completed", valid=True)
    row = {"request_id": "server", "session_id": "run", "metadata": {"spend_logs_metadata": {
        "giraffe_run": "run", "giraffe_request": "local"}}, "api_key": "a"*64,
        "prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5, "status": "success"}
    rows = [row, {**row, "prompt_tokens": 4, "total_tokens": 7}]
    matched, _, _ = _normalize(rows, "run", {"local": record})
    assert _assess([record], matched, 1, complete=True)["state"] == "unverified"
    matched, _, _ = _normalize([row], "run", {"local": record})
    record._gateway_key_hash = None
    assert _assess([record], matched, 1, complete=True)["state"] == "unverified"


async def test_unconfigured_selection_is_unavailable_without_traffic():
    from giraffe.runner import run_suite
    from giraffe.planning import request_plan
    cfg = RunConfig(targets=[{"name": "proxy", "url": "http://127.0.0.1:1", "model": "test"}],
                    checks=["gateway_attribution"], max_memory_mb=0)
    report = await run_suite(cfg)
    check = next(c for c in report.checks if c.id == "gateway_attribution")
    assert check.metrics["state"] == "unavailable" and not report.requests
    row = next(row for row in request_plan(cfg)["rows"] if row["id"] == "gateway_attribution")
    assert row["requests_per_target"] == 0 and "unavailable" in json.dumps(row)


@pytest.mark.parametrize("malformed", [False, True])
async def test_observer_transport_echo_is_suppressed_but_shared_logs_remain(monkeypatch, caplog, malformed):
    import logging
    from giraffe.gateway_attribution import execute, result
    from giraffe.models import RequestRecord
    secret = "observer-credential-echo"
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, secret)
    cfg = config()
    cfg.gateway_attribution.settle_seconds = .2
    run = _Run(cfg, None, None)
    reading = asyncio.Event()

    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        if b"/shared " in request:
            writer.write(b"HTTP/1.1 200 SharedVisible\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        else:
            reading.set()
            writer.write(f"HTTP/1.1 200 {secret}\r\nX-Echo: {secret}\r\n".encode())
            if malformed:
                writer.write(f"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n{secret}\r\n".encode())
            else:
                body = json.dumps({"data": [], "total_pages": 1, "echo": secret}).encode()
                writer.write(f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def fake_request(target, client, spec):
        return RequestRecord(id=spec.identity, target=target.name, identity=spec.identity,
            fixture_id=spec.fixture_id, scenario=spec.scenario, check_ids=spec.check_ids,
            started_at="2026-11-06T00:00:00Z", status="completed", valid=True,
            input_tokens=3, completion_tokens=5)

    monkeypatch.setattr(run, "request", fake_request)
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    cfg.targets[0].gateway.url = url + "/logs"
    caplog.set_level(logging.DEBUG)
    async def shared():
        await reading.wait()
        async with httpx.AsyncClient() as client:
            await client.get(url + "/shared")
    try:
        await asyncio.gather(execute(run, cfg.targets[0], None), shared())
    finally:
        server.close()
        await server.wait_closed()
    assert secret not in caplog.text
    assert "SharedVisible" in caplog.text
    assert secret not in result(run, cfg.targets[0]).model_dump_json()
    assert result(run, cfg.targets[0]).metrics["state"] == "unverified"
