"""Protocol and lifecycle regressions found by independent gateway review."""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import certifi
import httpx
import pytest
from pydantic import ValidationError

from giraffe.client import LLMClient
from giraffe.gateway_attribution import MAX_BODY_BYTES, execute, result
from giraffe.models import CheckResult, GatewayEvidence, RequestSpec, RunConfig, RunReport, Sessions, TestIdentity as GatewayIdentity
from giraffe.planning import request_plan
from giraffe.reporting import _settings_reasons, compare_baseline, write_report
from giraffe.runner import _Run, _checks, run_suite
from tests.test_gateway_attribution import config, immediate_spend_polls, spend_page


def setup(monkeypatch, observer, *, rejected=None, before_marked=None):
    for ref in ("ALICE", "BOB", "OBSERVER", "SHARED"):
        monkeypatch.setenv(ref, "secret-" + ref)
    cfg = config(stream=False)
    run = _Run(cfg, None, None)
    rows, reads = [], []

    async def handler(request):
        if request.method == "GET":
            if request.url.path != "/spend/logs/v2":
                return httpx.Response(404)
            reads.append(request)
            return await observer(request, rows)
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        if key == "secret-ALICE" and rejected:
            if rejected == "connection":
                raise httpx.ConnectError("unreachable", request=request)
            if rejected == "deep_json":
                return httpx.Response(401, content=b'{"error":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}')
            if rejected == "deep_success":
                return httpx.Response(200, content=b'{"choices":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}')
            if rejected == "invalid_unicode":
                return httpx.Response(200, content=b'{"choices":[{"message":{"content":"\\ud800"},'
                                      b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":5}}')
            return httpx.Response(401, json={"error": {"type": "expired_key"}})
        if "x-litellm-spend-logs-metadata" in request.headers:
            if before_marked:
                await before_marked(key)
            rows.append({"request_id": str(len(rows)), "session_id": request.headers["x-litellm-trace-id"],
                "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                "api_key": hashlib.sha256(key.encode()).hexdigest(), "prompt_tokens": 3,
                "completion_tokens": 5, "total_tokens": 8, "status": "success"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return cfg, run, reads


def streamed_gateway(monkeypatch, *, rejected_attempts=0, reject_marked_logprobs=False, post_finish_delta=None):
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, "secret-" + ref)
    cfg = config(stream=True, request_timeout_seconds=.3)
    immediate_spend_polls(monkeypatch, cfg)
    rows, posts = [], []
    compatibility_attempts = 0

    def handler(request):
        nonlocal compatibility_attempts
        if request.method == "GET":
            return spend_page(request, rows) if request.url.path == "/spend/logs/v2" else httpx.Response(404)
        body = json.loads(request.content)
        marker = request.headers.get("x-litellm-trace-id")
        posts.append((bool(marker), body))
        if marker:
            if reject_marked_logprobs and body.get("logprobs"):
                return httpx.Response(400, json={"error": {"param": "logprobs"}})
            key = request.headers["authorization"].removeprefix("Bearer ")
            rows.append({"request_id": str(len(rows)), "session_id": marker,
                         "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                         "api_key": hashlib.sha256(key.encode()).hexdigest(), "prompt_tokens": 3,
                         "completion_tokens": 5, "total_tokens": 8, "status": "success"})
        elif body["max_tokens"] == 1:
            compatibility_attempts += 1
            if compatibility_attempts <= rejected_attempts:
                # A gateway can repeatedly reject a known parameter after its first rejection.
                return httpx.Response(400, json={"error": {"param": "logprobs"}})
        tail = ("data: " + json.dumps({"choices": [{"delta": post_finish_delta}]}) + "\n\n"
                if post_finish_delta is not None else "")
        return httpx.Response(200, text=(
            'data: {"choices":[{"delta":{"content":"OK"},"logprobs":{"content":[{"token":"OK"}]}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
            + tail + 'data: [DONE]\n\n'))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return cfg, posts


async def test_late_delta_qualification_applies_only_to_marked_traffic(monkeypatch):
    cfg, _ = streamed_gateway(monkeypatch, post_finish_delta={
        "thinking_blocks": [{"type": "thinking", "thinking": "late reasoning", "index": 0}],
    })
    cfg.checks = ["access", "gateway_attribution"]
    cfg.targets[0].tokenizer = "builtin"

    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == "pass"
    attribution = checks["gateway_attribution"]
    assert attribution.status == "inconclusive" and attribution.metrics["matched_requests"] == 0
    assert [record.status for record in report.requests if record.identity] == ["failed", "failed"]


async def collect(cfg, run):
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(run, cfg.targets[0], client)
    return result(run, cfg.targets[0])


@pytest.mark.parametrize("mode,expected,started", [
    ("only", "pass", 2),
    ("mixed", "pass", 2),
    ("deferred", "fail", 2),
    ("budget", "inconclusive", 1),
])
async def test_live_attribution_condition_finishes_with_the_qualified_check(
        monkeypatch, local_tokenizer, mode, expected, started):
    events = []
    reads_during_scoring = []

    async def observer(request, rows):
        if rows:
            lifecycle = [event["event"] for event in events if event["scenario"] == "gateway_attribution"]
            reads_during_scoring.append(lifecycle[-1] == "condition_scoring")
        if mode == "deferred":
            rows = [{**row, "completion_tokens": 6, "total_tokens": 9} for row in rows]
        return spend_page(request, rows)

    cfg, _, _ = setup(monkeypatch, observer)
    immediate_spend_polls(monkeypatch, cfg)
    if mode in {"mixed", "deferred"}:
        cfg.checks = ["access", "gateway_attribution"]
        cfg.targets[0].api_key_env = "SHARED"
    if mode == "deferred":
        cfg.targets.append(cfg.targets[0].model_copy(
            update={"name": "second", "url": "http://second/v1", "gateway": None}))
    if mode == "budget":
        cfg.max_requests = 1

    report = await run_suite(cfg, progress=events.append)
    check = next(check for check in report.checks if check.target == "proxy" and check.id == "gateway_attribution")
    saved = next(row for row in report.conditions if row["condition_id"] == "gateway_attribution")
    assert check.status == expected
    assert reads_during_scoring and all(reads_during_scoring)
    scoring, finished = [event for event in events if event["target"] == "proxy"
                         and event["scenario"] == "gateway_attribution"
                         and event["event"] in {"condition_scoring", "condition_finished"}]
    assert scoring["event"] == "condition_scoring"
    assert scoring["condition_summary"]["condition_verdict"] == "pending"
    assert finished["event"] == "condition_finished"
    summary = finished["condition_summary"]
    assert summary["condition_verdict"] == check.status
    assert summary["aggregate_status"] == saved["status"]
    assert summary["planned"] == saved["planned"] == 2
    assert summary["started"] == summary["completed"] == saved["started"] == saved["completed"] == started
    assert summary["failed"] == saved["failed"] == 0
    assert events.index(finished) < next(i for i, event in enumerate(events) if event["event"] == "run_finished")


@pytest.mark.parametrize("row_limit", [1, 2])
async def test_row_limit_is_checked_before_marked_requests(monkeypatch, row_limit):
    keys = []

    async def before_marked(key):
        keys.append(key)

    async def observer(request, rows):
        return spend_page(request, rows)

    cfg, run, reads = setup(monkeypatch, observer, before_marked=before_marked)
    immediate_spend_polls(monkeypatch, cfg)
    cfg.metrics_max_series = row_limit
    check = await collect(cfg, run)

    assert check.metrics["planned_requests"] == 2
    if row_limit == 1:
        assert check.status == "inconclusive" and check.metrics["reason_code"] == "spend_log_row_limit"
        assert "require at least 2 spend-log rows" in check.summary and "at most 1" in check.summary
        assert not keys and not reads and not run.records
        assert check.metrics["collection_attempts"] == 0
    else:
        assert check.status == "pass" and check.metrics["matched_requests"] == 2
        assert len(keys) == 2 and all(req.url.params["page_size"] == "2" for req in reads)


@pytest.mark.parametrize("case", ["row_limit", "reader_unavailable", "preflight_stop", "no_gateway", "no_identity"])
async def test_zero_dispatch_reports_only_eligible_attribution_conditions(monkeypatch, case):
    events, marked = [], []
    stop = asyncio.Event()

    async def observer(request, rows):
        if case == "preflight_stop":
            stop.set()
        return httpx.Response(403)

    async def before_marked(key):
        marked.append(key)

    cfg, _, reads = setup(monkeypatch, observer, before_marked=before_marked)
    cfg.targets[0].tokenizer = "builtin"
    if case == "row_limit":
        cfg.metrics_max_series = 1
    elif case in {"reader_unavailable", "preflight_stop"}:
        cfg.checks = ["access", "gateway_attribution"]
        cfg.targets.append(cfg.targets[0].model_copy(
            update={"name": "second", "url": "http://second/v1", "gateway": None}))
    elif case == "no_gateway":
        cfg.targets[0].gateway = None
    else:
        cfg.targets[0].test_identities.pop("bob")

    report = await run_suite(cfg, progress=events.append, stop_event=stop)
    check = next(check for check in report.checks if check.target == "proxy" and check.id == "gateway_attribution")
    conditions = [row for row in report.conditions if row["condition_id"] == "gateway_attribution"]
    lifecycle = [event for event in events if event["scenario"] == "gateway_attribution"
                 and event["event"].startswith("condition_")]
    assert check.status == "inconclusive"
    assert not marked and not any(record.identity for record in report.requests)
    if case in {"no_gateway", "no_identity"}:
        assert not conditions and not lifecycle and not reads
        return

    assert len(reads) == (0 if case == "row_limit" else 1)
    assert check.metrics["state"] == ("unavailable" if case == "reader_unavailable" else "unverified")
    assert check.metrics["planned_requests"] == 2 and check.metrics["observed_requests"] == 0
    [row] = conditions
    assert row["target"] == "proxy" and row["status"] == "inconclusive"
    assert row["planned_kind"] == "exact" and row["planned"] == row["not_started"] == 2
    assert row["started"] == row["completed"] == row["failed"] == 0
    assert [event["event"] for event in lifecycle] == ["condition_planned", "condition_scoring", "condition_finished"]
    final = lifecycle[-1]["condition_summary"]
    assert final["condition_verdict"] == "inconclusive" and final["aggregate_status"] == row["status"]
    assert final["started"] == final["completed"] == 0
    assert final["reasons"] == row["reasons"] == [check.metrics["reason"]]
    assert events.index(lifecycle[-1]) < next(i for i, event in enumerate(events) if event["event"] == "run_finished")


async def test_litellm_required_utc_dates_cover_probes_and_are_stable_on_all_pages(monkeypatch):
    monkeypatch.setattr("giraffe.gateway_attribution.PAGE_SIZE", 1)
    # LiteLLM fca3d4f: public v2 requires both bounds in UTC second precision.
    async def observer(request, rows):
        if not all(request.url.params.get(key) for key in ("start_date", "end_date")):
            return httpx.Response(400)
        page = int(request.url.params["page"])
        return spend_page(request, rows[page-1:page], total=len(rows))
    cfg, run, reads = setup(monkeypatch, observer)
    cfg.gateway_attribution.settle_seconds = .2
    check = await collect(cfg, run)
    assert check.metrics["state"] == "verified"
    assert len(reads) == 6
    bounds = {(r.url.params["start_date"], r.url.params["end_date"]) for r in reads}
    assert len(bounds) == 1
    start, end = [datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) for value in bounds.pop()]
    assert len(run.records) == 2
    for record in run.records:
        assert start <= datetime.fromisoformat(record.started_at) <= datetime.fromisoformat(record.completed_at) < end
    assert (end - start).total_seconds() <= cfg.max_duration_seconds + cfg.gateway_attribution.settle_seconds + 2


@pytest.mark.parametrize("status,state", [(403, "unavailable"), (404, "unsupported"), (405, "unsupported"), (501, "unsupported")])
async def test_observer_denial_keeps_attempted_identity_coverage(monkeypatch, status, state):
    async def observer(request, rows):
        return httpx.Response(status)
    cfg, run, _ = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.required and check.metrics["state"] == state
    assert check.metrics["planned_requests"] == check.metrics["observed_requests"] == 2
    assert check.metrics["unverified_requests"] == 2
    assert {r["identity"] for r in check.metrics["identities"]} == {"alice", "bob"}
    assert {i for r in check.metrics["identities"] for i in r["request_ids"]} == {r.id for r in run.records}


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("status,state,required,overall", [
    (404, "unsupported", False, "pass"), (403, "unavailable", True, "inconclusive"),
])
async def test_mixed_run_distinguishes_unsupported_reader_from_denied_reader(monkeypatch, status, state, required, overall):
    async def observer(request, rows):
        return httpx.Response(status)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.checks = ["access", "serving", "gateway_attribution"]
    cfg.targets[0].api_key_env = "SHARED"
    cfg.limits.min_samples = 2
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == checks["serving"].status == "pass"
    attribution = checks["gateway_attribution"]
    assert reads and attribution.metrics["observed_requests"] == (0 if status == 404 else 2)
    assert attribution.metrics["state"] == state and attribution.status == "inconclusive"
    assert attribution.required is required and report.overall == overall


@pytest.mark.parametrize("port", ["bad", "65536", "0"])
def test_gateway_evidence_rejects_invalid_ports(port):
    with pytest.raises(ValidationError, match="gateway evidence URL must have a valid port"):
        GatewayEvidence(url=f"http://proxy:{port}/spend/logs/v2", api_key_env="OBSERVER")


@pytest.mark.parametrize("origin", ["https://proxy", "http://proxy:8080", "http://[::1]:65535"])
def test_gateway_evidence_preserves_valid_archived_urls(origin):
    url = origin + "/spend/logs/v2"
    evidence = GatewayEvidence(url=url, api_key_env="OBSERVER")
    assert GatewayEvidence.model_validate_json(evidence.model_dump_json()).url == url


@pytest.mark.parametrize("cap,marked", [(1, 0), (10, 0), (None, 2)])
async def test_invalid_gateway_reader_url_does_not_block_shared_access(monkeypatch, local_tokenizer, cap, marked):
    async def observer(request, rows):
        return spend_page(request, rows)

    cfg, _, reads = setup(monkeypatch, observer)
    immediate_spend_polls(monkeypatch, cfg)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = cap
    # Unvalidated model updates can still reach the runtime reader.
    cfg.targets[0].gateway = cfg.targets[0].gateway.model_copy(update={"url": "http://proxy:bad/spend/logs/v2"})

    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert report.overall == "inconclusive" and report.abort_reason is None
    assert checks["access"].status == "pass" and report.requests[0].valid
    attribution = checks["gateway_attribution"]
    assert attribution.status == "inconclusive" and attribution.required
    assert attribution.metrics["state"] == "unavailable"
    assert attribution.metrics["planned_requests"] == 2 and attribution.metrics["observed_requests"] == marked
    assert "reader URL is invalid" in attribution.finding_summary and ":bad" not in attribution.finding_summary
    assert attribution.remediation["action"] == "review_check"
    assert not reads and [record.identity for record in report.requests] == [None, *["alice", "bob"][:marked]]


@pytest.mark.parametrize("status,state,polls", [(200, "unverified", 3), (403, "unavailable", 1),
                                                (404, "unsupported", 1)])
async def test_gateway_reader_never_reads_compressed_spend_bodies(monkeypatch, local_tokenizer, status, state, polls):
    compressed = gzip.compress(b"x" * (MAX_BODY_BYTES + 1))
    opened, closed = [], []

    class GzipBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            opened.append(self)
            yield compressed

        async def aclose(self):
            closed.append(self)

    async def observer(request, rows):
        return httpx.Response(status, headers={"content-encoding": "gzip"}, stream=GzipBody())

    cfg, _, reads = setup(monkeypatch, observer)
    immediate_spend_polls(monkeypatch, cfg)

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert not opened and len(closed) == len(reads) == polls
    assert {request.headers["accept-encoding"] for request in reads} == {"identity"}
    assert check.status == "inconclusive" and check.metrics["state"] == state
    assert check.metrics["observed_requests"] == 2 and not check.metrics["collection_complete"]
    assert len(report.requests) == 2 and all(record.valid for record in report.requests)


@pytest.mark.parametrize("status,cap,state,planned,observed,read_count", [
    (200, 1, "unverified", 2, 0, 1), (200, 3, "verified", 2, 2, 4),
    (403, 1, "unavailable", 2, 0, 1), (403, 3, "unavailable", 2, 2, 2),
    (404, 1, "unsupported", 0, 0, 1),
])
async def test_mixed_attribution_retains_known_reader_state_at_exhausted_quota(
        monkeypatch, local_tokenizer, status, cap, state, planned, observed, read_count):
    async def observer(request, rows):
        if status == 200:
            return spend_page(request, rows)
        return httpx.Response(status)

    cfg, _, reads = setup(monkeypatch, observer)
    immediate_spend_polls(monkeypatch, cfg)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = cap

    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == "pass"
    attribution = checks["gateway_attribution"]
    assert attribution.metrics["state"] == state
    assert attribution.metrics["planned_requests"] == planned
    assert attribution.metrics["observed_requests"] == observed
    assert len(reads) == read_count
    assert len(report.requests) == 1 + observed
    if state == "unverified":
        assert attribution.metrics["reason_code"] == "budget_limited"
        assert attribution.remediation == {"action": "set_limit", "limit": "max_requests", "minimum": 3}
    elif state != "verified":
        assert "reason_code" not in attribution.metrics
        assert attribution.remediation["action"] == "review_check"
        assert "max_requests" not in attribution.summary


@pytest.mark.parametrize("wait_for_monitor", [False, True])
async def test_manual_stop_during_attribution_preflight_preserves_configured_probes(
        monkeypatch, local_tokenizer, wait_for_monitor):
    stop = asyncio.Event()

    async def observer(request, rows):
        stop.set()
        if wait_for_monitor:
            await asyncio.sleep(.2)
        return httpx.Response(404)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = 1
    cfg.gateway_attribution.settle_seconds = .5

    report = await run_suite(cfg, stop_event=stop)
    checks = {check.id: check for check in report.checks}
    check = checks["gateway_attribution"]
    assert report.abort_reason == "cancelled by user" and report.overall == "inconclusive"
    assert checks["access"].status == "pass"
    assert check.status == "inconclusive" and check.required
    assert check.metrics["state"] == "unverified"
    assert check.metrics["planned_requests"] == 2 and check.metrics["observed_requests"] == 0
    assert check.metrics["collection_attempts"] == 0 and len(reads) == 1
    assert "reader preflight" in check.finding_summary and "user" in check.finding_summary
    assert "reason_code" not in check.metrics and check.remediation["action"] == "review_check"
    assert len(report.requests) == 1 and report.requests[0].identity is None


async def test_run_deadline_during_attribution_preflight_preserves_configured_probes(monkeypatch, local_tokenizer):
    async def observer(request, rows):
        # A synchronous transport delay can cross the run deadline before the monitor resumes.
        time.sleep(.55)
        return httpx.Response(404)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_duration_seconds = .5
    cfg.request_timeout_seconds = .15
    cfg.gateway_attribution.settle_seconds = .4

    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    check = checks["gateway_attribution"]
    assert report.abort_reason == "maximum run duration reached" and checks["access"].status == "pass"
    assert check.status == "inconclusive" and check.required
    assert check.metrics["state"] == "unverified" and check.metrics["reason_code"] == "time_limited"
    assert check.metrics["planned_requests"] == 2 and check.metrics["observed_requests"] == 0
    assert check.metrics["collection_attempts"] == 0 and len(reads) == 1
    assert "reader preflight" in check.finding_summary
    assert check.remediation == {"action": "set_limit", "limit": "max_duration", "minimum": 2}
    assert len(report.requests) == 1 and report.requests[0].identity is None


@pytest.mark.parametrize("preflight", [False, True])
async def test_stop_while_building_spend_reader_sends_no_get(monkeypatch, local_tokenizer, preflight):
    stop = asyncio.Event()

    async def observer(request, rows):
        return spend_page(request, rows)

    cfg, _, reads = setup(monkeypatch, observer)
    original = httpx.AsyncClient

    def build_client(**kwargs):
        client = original(**kwargs)
        if httpx.Headers(kwargs.get("headers")).get("authorization") == "Bearer secret-OBSERVER":
            stop.set()
        return client

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    if preflight:
        cfg.checks = ["access", "gateway_attribution"]
        cfg.max_requests = 1
    cfg.gateway_attribution.settle_seconds = .5

    report = await run_suite(cfg, stop_event=stop)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert report.abort_reason == "cancelled by user" and not reads
    assert check.metrics["planned_requests"] == 2
    assert check.metrics["observed_requests"] == (0 if preflight else 2)
    assert check.metrics["collection_attempts"] == 0
    assert ("reader preflight" if preflight else "evidence collection") in check.finding_summary
    assert len(report.requests) == (1 if preflight else 2)


async def test_collection_reuses_loaded_tls_after_ca_file_disappears(monkeypatch, local_tokenizer, tmp_path):
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_bytes(Path(certifi.where()).read_bytes())

    async def before_marked(key):
        if key == "secret-BOB":
            ca_bundle.unlink()

    async def observer(request, rows):
        return spend_page(request, rows)

    cfg, _, reads = setup(monkeypatch, observer, before_marked=before_marked)
    cfg.ca_bundle = str(ca_bundle)
    immediate_spend_polls(monkeypatch, cfg)

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert report.abort_reason is None and len(reads) == 3
    assert len(report.requests) == 2 and all(record.valid for record in report.requests)
    assert check.metrics["state"] == "verified" and check.metrics["collection_complete"]


async def test_deferred_attribution_reuses_each_targets_loaded_tls(monkeypatch, local_tokenizer, tmp_path):
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_bytes(Path(certifi.where()).read_bytes())
    connections = {}
    original = LLMClient._create_client

    def create_client(client):
        shared = client.target.api_key_env is not None
        if not shared:
            ca_bundle.unlink(missing_ok=True)
        pool = original(client)
        connections[client.target.name, shared] = (client._tls_context, pool)
        return pool

    async def observer(request, rows):
        return spend_page(request, [row for row in rows if row["session_id"] == request.url.params["session_id"]])

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.ca_bundle = str(ca_bundle)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.targets[0].api_key_env = "SHARED"
    cfg.targets.append(cfg.targets[0].model_copy(update={"name": "proxy2"}, deep=True))
    immediate_spend_polls(monkeypatch, cfg)
    monkeypatch.setattr(LLMClient, "_create_client", create_client)

    report = await run_suite(cfg)
    assert report.overall == "pass" and report.abort_reason is None
    assert len(reads) == 8 and all(request.headers["authorization"] == "Bearer secret-OBSERVER" for request in reads)
    assert [record.identity for record in report.requests] == [None, None, "alice", "bob", "alice", "bob"]
    for target in cfg.targets:
        shared_context, shared_pool = connections[target.name, True]
        identity_context, identity_pool = connections[target.name, False]
        assert shared_context is identity_context and shared_pool is not identity_pool


@pytest.mark.parametrize("duration,delay,reason", [(.5, .55, "time_limited"), (10, .12, "budget_limited")])
async def test_preflight_rechecks_deadline_after_building_get(monkeypatch, local_tokenizer, duration, delay, reason):
    original = httpx.AsyncClient.build_request

    def build_request(self, *args, **kwargs):
        request = original(self, *args, **kwargs)
        if request.method == "GET" and request.url.path == "/spend/logs/v2":
            time.sleep(delay)
        return request

    async def observer(request, rows):
        return spend_page(request, rows)

    monkeypatch.setattr(httpx.AsyncClient, "build_request", build_request)
    cfg, _, reads = setup(monkeypatch, observer)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = 1 if reason == "budget_limited" else 10
    cfg.max_duration_seconds = duration
    cfg.request_timeout_seconds = .2
    cfg.gateway_attribution.settle_seconds = .05

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert not reads and len(report.requests) == 1
    assert check.metrics["reason_code"] == reason
    assert check.metrics["planned_requests"] == 2 and check.metrics["observed_requests"] == 0
    if reason == "time_limited":
        assert report.abort_reason == "maximum run duration reached"
        assert check.remediation["limit"] == "max_duration"
    else:
        assert check.remediation["limit"] == "max_requests"


async def test_preflight_discards_unsupported_status_received_after_its_cutoff(monkeypatch, local_tokenizer):
    async def observer(request, rows):
        time.sleep(.12)
        return httpx.Response(404)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = 1
    cfg.gateway_attribution.settle_seconds = .05

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert report.abort_reason is None and check.required
    assert check.metrics["state"] == "unverified" and check.metrics["reason_code"] == "budget_limited"
    assert check.metrics["planned_requests"] == 2 and check.metrics["observed_requests"] == 0
    assert len(reads) == len(report.requests) == 1


@pytest.mark.parametrize("interruption", ["settle", "duration", "user"])
async def test_collection_discards_late_status_and_retains_prior_read(
        monkeypatch, local_tokenizer, interruption):
    stop = asyncio.Event()
    polls = 0

    async def observer(request, rows):
        nonlocal polls
        polls += 1
        if polls == 1:
            return spend_page(request, [*rows, {"request_id": "unrelated"}])
        if interruption == "user":
            stop.set()
        else:
            time.sleep(.30)
        return httpx.Response(404)

    cfg, _, reads = setup(monkeypatch, observer)
    immediate_spend_polls(monkeypatch, cfg)
    if interruption == "duration":
        cfg.max_duration_seconds = .25
    if interruption == "settle":
        cfg.gateway_attribution.settle_seconds = .25

    report = await run_suite(cfg, stop_event=stop)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert len(report.requests) == 2 and all(record.valid for record in report.requests)
    assert len(reads) == check.metrics["collection_attempts"] == 2
    assert check.metrics["state"] == "unverified" and not check.metrics["collection_complete"]
    assert check.metrics["unrelated_rows"] == 1 and check.metrics["matched_requests"] == 0
    assert check.metrics.get("reason_code") == ("time_limited" if interruption == "duration" else None)
    assert report.abort_reason == {"settle": None, "duration": "maximum run duration reached",
                                   "user": "cancelled by user"}[interruption]


@pytest.mark.parametrize("duration,settle,reason", [(.25, .5, "time_limited"), (10, .25, None)])
async def test_collection_does_not_dispatch_next_page_after_deadline(monkeypatch, local_tokenizer, duration, settle, reason):
    monkeypatch.setattr("giraffe.gateway_attribution.PAGE_SIZE", 1)

    async def observer(request, rows):
        page = int(request.url.params["page"])
        if page == 1:
            time.sleep(.26)
        return spend_page(request, rows[page - 1:page], total=2)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.max_duration_seconds = duration
    cfg.gateway_attribution.settle_seconds = settle

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert [read.url.params["page"] for read in reads] == ["1"]
    assert len(report.requests) == 2 and all(record.valid for record in report.requests)
    assert check.metrics.get("reason_code") == reason and check.metrics["collection_attempts"] == 1
    assert check.metrics["planned_requests"] == check.metrics["observed_requests"] == 2
    assert check.metrics["collection_complete"] is False and check.metrics["matched_requests"] == 0
    assert report.abort_reason == ("maximum run duration reached" if reason else None)


@pytest.mark.parametrize("reader,cap", [("unsupported", 5), ("verified", 8), ("later_unsupported", 8)])
async def test_attribution_preserves_shared_sessions_and_uses_spare_budget(monkeypatch, local_tokenizer, reader, cap):
    cfg = config(stream=True)
    cfg.checks = ["access", "sessions", "gateway_attribution"]
    cfg.sessions = Sessions(sessions=2, concurrency=2, modes=["fixed"], delay_seconds=0)
    cfg.max_requests = cap
    cfg.gateway_attribution.settle_seconds = .2
    cfg.limits.min_samples = 2
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, "secret-" + ref)
    traffic, rows = [], []

    class Answer(httpx.AsyncByteStream):
        def __init__(self, answer):
            self.answer = answer

        async def __aiter__(self):
            for part in (self.answer[:1], self.answer[1:]):
                await asyncio.sleep(.01)
                yield ("data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\ndata: [DONE]\n\n'

    def handler(request):
        if request.method == "GET":
            if request.url.path != "/spend/logs/v2":
                return httpx.Response(404)
            traffic.append("reader")
            if reader == "unsupported" or reader == "later_unsupported" and rows:
                return httpx.Response(404)
            return spend_page(request, rows)
        if "x-litellm-trace-id" in request.headers:
            traffic.append("identity")
            key = request.headers["authorization"].removeprefix("Bearer ")
            rows.append({"request_id": str(len(rows)), "session_id": request.headers["x-litellm-trace-id"],
                "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                "api_key": hashlib.sha256(key.encode()).hexdigest(), "prompt_tokens": 3,
                "completion_tokens": 5, "total_tokens": 8, "status": "success"})
        else:
            traffic.append("shared")
        body = json.loads(request.content)
        marker = re.search(r"Session_\d+_fixed", body["messages"][0]["content"])
        return httpx.Response(200, stream=Answer(marker[0] if marker else "OK"))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == checks["sessions"].status == "pass"
    attribution = checks["gateway_attribution"]
    assert attribution.metrics["state"] == ("verified" if reader == "verified" else "unsupported")
    assert attribution.required is (reader == "verified")
    assert report.overall == "pass"
    shared_count = 5 if reader == "unsupported" else 6
    assert traffic[:shared_count + 1] == ["shared"] * shared_count + ["reader"]
    assert traffic.count("identity") == (0 if reader == "unsupported" else 2)


@pytest.mark.parametrize("overlap,reader_delay", [(False, .36), (False, .01), (True, .01)])
async def test_attribution_waits_for_all_shared_targets(monkeypatch, local_tokenizer, overlap, reader_delay):
    cfg = config(stream=False)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_duration_seconds = 1
    cfg.max_requests = 6
    cfg.gateway_attribution.settle_seconds = .5
    cfg.overlap_models = overlap
    first = cfg.targets[0]
    cfg.targets.append(first.model_copy(update={"name": "second", "url": "http://second/v1", "gateway": None}))
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, "secret-" + ref)
    traffic = []

    async def handler(request):
        if request.method == "GET":
            if request.url.path == "/spend/logs/v2":
                traffic.append("reader")
                await asyncio.sleep(reader_delay)
            return httpx.Response(404)
        assert "x-litellm-trace-id" not in request.headers
        await asyncio.sleep(.72 if request.url.host == "second" else .02)
        traffic.append(request.url.host)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 5}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    checks = {(check.target, check.id): check for check in report.checks}
    assert checks["proxy", "access"].status == checks["second", "access"].status == "pass"
    assert traffic == ["proxy", "second", "reader"]
    assert not report.abort_reason
    attribution = checks["proxy", "gateway_attribution"]
    if reader_delay > .1:
        assert attribution.metrics["state"] == "unavailable" and attribution.required
    else:
        assert attribution.metrics["state"] == "unsupported" and not attribution.required
        assert report.overall == "pass"


async def test_deferred_attribution_uses_only_selected_identities(monkeypatch, local_tokenizer):
    async def observer(request, rows):
        return spend_page(request, rows)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.gateway_attribution.settle_seconds = .2
    cfg.targets[0].api_key_env = "SHARED"
    cfg.targets.append(cfg.targets[0].model_copy(update={
        "name": "second", "url": "http://second/v1", "gateway": None,
    }))
    report = await run_suite(cfg)
    assert report.overall == "pass"
    assert [(record.target, record.identity) for record in report.requests] == [
        ("proxy", None), ("second", None), ("proxy", "alice"), ("proxy", "bob"),
    ]
    check = next(check for check in report.checks
                 if check.target == "proxy" and check.id == "gateway_attribution")
    assert check.metrics["state"] == "verified"
    assert len(reads) == 4


@pytest.mark.parametrize("restart", ["missing", "failed", "unready", "ready"])
async def test_deferred_attribution_obeys_restart_readiness(monkeypatch, local_tokenizer, restart):
    async def observer(request, rows):
        return spend_page(request, [row for row in rows if row["session_id"] == request.url.params["session_id"]])

    async def ready(self, deadline):
        return restart == "ready"

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.restart_target = "proxy"
    cfg.readiness_timeout_seconds = .05
    cfg.gateway_attribution.settle_seconds = .2
    first = cfg.targets[0]
    if restart != "missing":
        first.restart_command = [sys.executable, "-c", f"raise SystemExit({int(restart == 'failed')})"]
    cfg.targets.append(first.model_copy(update={"name": "second", "url": "http://second/v1",
        "gateway": first.gateway.model_copy(update={"url": "http://second/spend/logs/v2"})}))
    monkeypatch.setattr(LLMClient, "ready", ready)

    report = await run_suite(cfg)
    checks = {(check.target, check.id): check for check in report.checks}
    assert checks["second", "access"].status == checks["second", "gateway_attribution"].status == "pass"
    assert {record.identity for record in report.requests if record.target == "second"} == {None, "alice", "bob"}
    proxy_attribution = checks["proxy", "gateway_attribution"]
    if restart == "ready":
        assert checks["proxy", "access"].status == proxy_attribution.status == "pass"
        assert any(request.url.host == "proxy" for request in reads)
        assert [record.identity for record in report.requests if record.target == "proxy"] == [None, "alice", "bob"]
    else:
        assert checks["proxy", "access"].status == "blocked"
        assert proxy_attribution.metrics["state"] == "unverified"
        assert "readiness" in proxy_attribution.summary
        assert {request.url.host for request in reads} == {"second"}
        assert {record.target for record in report.requests} == {"second"}


async def test_attribution_does_not_reserve_shared_token_evidence_slot(monkeypatch, local_tokenizer):
    cfg = config(stream=True)
    cfg.checks = ["access", "generation", "gateway_attribution"]
    cfg.samples = 2
    cfg.limits.min_samples = 2
    cfg.limits.min_output_tokens_per_second = 1
    cfg.max_requests = 4
    cfg.gateway_attribution.settle_seconds = .2
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, "secret-" + ref)

    class Answer(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            for part in ("Hello ", "there."):
                await asyncio.sleep(.015)
                choice = {"delta": {"content": part}}
                if self.body.get("logprobs"):
                    choice["logprobs"] = {"content": [{"token": part}]}
                yield ("data: " + json.dumps({"choices": [choice]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            if self.body.get("stream_options", {}).get("include_usage"):
                yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            yield b'data: [DONE]\n\n'

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        assert "x-litellm-trace-id" not in request.headers
        return httpx.Response(200, stream=Answer(json.loads(request.content)))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == checks["generation"].status == "pass"
    assert checks["gateway_attribution"].metrics["state"] == "unsupported"
    assert [r.output_tokens for r in report.requests if r.scenario == "generation"] == [2, 2]
    assert sum(r.scenario == "performance_evidence_probe" for r in report.requests) == 1


@pytest.mark.parametrize("cap,rejections,setup_attempts,marked_count", [
    (1, 0, 0, 0), (2, 0, 0, 1), (3, 0, 1, 1), (4, 0, 1, 2), (5, 2, 3, 1), (6, 2, 3, 2),
])
async def test_attribution_request_limit_covers_earlier_streamed_setup(
        monkeypatch, local_tokenizer, cap, rejections, setup_attempts, marked_count):
    cfg, _ = streamed_gateway(monkeypatch, rejected_attempts=rejections)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = cap
    plan = request_plan(cfg)
    assert plan["planned_total"] == plan["known_total"] == plan["required_request_limit"] == 3
    assert plan["attribution_request_limit"] == 6
    if cap < 6:
        warning, = plan["request_warnings"]
        assert warning["cut_checks"] == ["gateway_attribution"]
        assert warning["actions"][0]["value"] == 6
        assert "up to 3 streamed compatibility attempts" in warning["msg"]
    else:
        assert not plan["request_warnings"]

    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == "pass"
    check = checks["gateway_attribution"]
    assert [record.scenario for record in report.requests] == (
        ["initial"] + ["performance_evidence_probe"] * setup_attempts + ["gateway_attribution"] * marked_count)
    assert check.metrics["matched_requests"] == marked_count
    if marked_count == 2:
        assert check.status == "pass"
    else:
        assert check.status == "inconclusive" and check.metrics["reason_code"] == "budget_limited"
        assert check.remediation == {"action": "set_limit", "limit": "max_requests", "minimum": 6}
        assert "--max-requests) to at least 6" in check.summary


@pytest.mark.parametrize("explicit", [False, True])
async def test_marked_requests_do_not_inherit_shared_logprobs(monkeypatch, local_tokenizer, explicit):
    cfg, posts = streamed_gateway(monkeypatch, reject_marked_logprobs=not explicit)
    cfg.checks = ["access", "serving", "gateway_attribution"]
    cfg.samples = 1
    if explicit:
        cfg.request_options = {"logprobs": True, "top_logprobs": 1}

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert check.status == "pass"
    assert report.observations["targets"]["proxy"]["token_evidence"]["logprobs"] == "supported"
    assert any(body.get("logprobs") is True for marked, body in posts if not marked and body["max_tokens"] != 1)
    marked_payloads = [body for marked, body in posts if marked]
    assert len(marked_payloads) == 2
    for body in marked_payloads:
        assert body["stream_options"] == {"include_usage": True}
        if explicit:
            assert body["logprobs"] is True and body["top_logprobs"] == 1
        else:
            assert "logprobs" not in body and "top_logprobs" not in body


async def test_attribution_does_not_reserve_requests_from_recovery(monkeypatch, local_tokenizer):
    from tests.test_runner import FakeClient

    class RecoveryClient(FakeClient):
        tls_context = httpx.create_ssl_context()

    async def observer(request, rows):
        return httpx.Response(404)

    cfg, _, reads = setup(monkeypatch, observer)
    monkeypatch.setattr("giraffe.runner.LLMClient", RecoveryClient)
    cfg.checks = ["recovery", "gateway_attribution"]
    cfg.concurrency = 1
    cfg.samples = 2
    cfg.limits.min_samples = 2
    cfg.max_requests = 8
    cfg.sustained_seconds = .1
    report = await run_suite(cfg)
    target = report.observations["targets"]["proxy"]
    assert target["sustained_budget"]["reserved_later"] == 2
    assert target["sustained_budget"]["allowance"] == 3
    assert "shared" in report.observations["request_limit_scopes"]
    assert len(reads) == 1
    assert not any(record.identity for record in report.requests)


@pytest.mark.parametrize("stream,coverage_limit", [(False, 5), (True, 11)])
def test_plan_does_not_reserve_attribution_on_unsupported_targets(stream, coverage_limit):
    cfg = config(stream=stream)
    isolated = request_plan(cfg)
    assert isolated["required_request_limit"] == 2 and "attribution_request_limit" not in isolated
    supported = cfg.targets[0]
    unsupported = supported.model_copy(update={"name": "other",
        "gateway": supported.gateway.model_copy(update={"profile": "unsupported"})})
    cfg.targets = [unsupported]
    plan = request_plan(cfg)
    assert plan["planned_total"] == plan["minimum_reserved_per_target"] == 0
    assert "attribution_request_limit" not in plan

    cfg.targets = [supported, unsupported]
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = 4
    plan = request_plan(cfg)
    row = next(row for row in plan["rows"] if row["id"] == "gateway_attribution")
    assert row["target_request_counts"] == {"proxy": 2, "other": 0}
    assert all(item["target"] == "proxy" for item in plan["unknown_time_components"])
    assert plan["planned_target_counts"] == plan["known_target_counts"] == {"proxy": 3, "other": 1}
    assert plan["known_total"] == plan["planned_total"] == 4
    assert plan["count_kind"] == "exact"
    assert plan["required_request_limit"] == 5
    assert plan["target_quotas"] == {"proxy": 2, "other": 2}
    warning, = plan["request_warnings"]
    assert warning["kind"] == "incomplete_coverage" and warning["cut_checks"] == ["gateway_attribution"]
    assert warning["msg"].startswith("proxy:")
    assert warning["actions"][0]["value"] == coverage_limit
    if stream:
        assert plan["attribution_request_limit"] == coverage_limit

    # Equal total work needs a larger cap when the high-count endpoint gets its remainder last.
    cfg.targets.reverse()
    reversed_plan = request_plan(cfg)
    assert reversed_plan["required_request_limit"] == 6
    if stream:
        assert reversed_plan["attribution_request_limit"] == 12


def test_attribution_fits_below_the_streamed_compatibility_floor():
    cfg = config(stream=True)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.gateway_attribution.identities = ["alice"]
    cfg.max_requests = 2
    plan = request_plan(cfg)
    assert plan["required_request_limit"] == 2
    assert "attribution_request_limit" not in plan and not plan["request_warnings"]


@pytest.mark.parametrize("fault", ["reused_id", "wrong_session", "missing_id", "unknown_local"])
async def test_ambiguous_correlated_rows_cannot_verify(monkeypatch, fault):
    async def observer(request, rows):
        data = json.loads(json.dumps(rows))
        if fault == "reused_id":
            data[1]["request_id"] = data[0]["request_id"]
        else:
            extra = json.loads(json.dumps(data[0]))
            if fault == "wrong_session":
                extra["session_id"] = "other-run"
            elif fault == "missing_id":
                extra.pop("request_id")
            else:
                extra["metadata"]["spend_logs_metadata"]["giraffe_request"] = "unknown-request"
            data.append(extra)
        return spend_page(request, data)
    cfg, run, _ = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.metrics["state"] == "unverified"
    assert check.metrics["matched_requests"] == 0
    assert check.metrics["completion_tokens"] == 0


@pytest.mark.parametrize("metadata", [None, "{", {"spend_logs_metadata": {"giraffe_run": "different-run"}}])
@pytest.mark.parametrize("same_session", [False, True])
async def test_unmarked_row_requires_independent_session_evidence(monkeypatch, metadata, same_session):
    async def observer(request, rows):
        extra = {**rows[0], "request_id": "unclassified", "metadata": metadata,
                 "api_key": "0" * 64, "completion_tokens": 9, "total_tokens": 12}
        if not same_session:
            extra["session_id"] = "unrelated-session"
        return spend_page(request, [*rows, extra])

    cfg, run, _ = setup(monkeypatch, observer)
    # Classify the same rows across three reads without relying on poll timing.
    monkeypatch.setattr("giraffe.gateway_attribution.POLL_FRACTIONS", (0, 0, 0))
    cfg.gateway_attribution.settle_seconds = 1
    check = await collect(cfg, run)
    assert check.metrics["collection_complete"] is True
    assert check.metrics["unrelated_rows"] == (0 if same_session else 1)
    assert check.metrics["invalid_rows"] == (1 if same_session else 0)
    assert check.metrics["state"] == ("unverified" if same_session else "verified")


@pytest.mark.parametrize("capped", [False, True])
async def test_empty_page_before_advertised_end_is_incomplete(monkeypatch, capped):
    monkeypatch.setattr("giraffe.gateway_attribution.PAGE_SIZE", 2)
    async def observer(request, rows):
        page = int(request.url.params["page"])
        return spend_page(request, rows if page == 1 else [], total=5, total_is_capped=capped)
    cfg, run, _ = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.metrics["state"] == "unverified"
    assert check.metrics["collection_complete"] is False


async def test_changed_total_between_pages_is_incomplete(monkeypatch):
    monkeypatch.setattr("giraffe.gateway_attribution.PAGE_SIZE", 1)
    async def observer(request, rows):
        page = int(request.url.params["page"])
        return spend_page(request, rows[page-1:page], total=2 if page == 1 else 3)
    cfg, run, _ = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.metrics["state"] == "unverified"
    assert check.metrics["collection_complete"] is False


@pytest.mark.parametrize("recovers", [False, True])
async def test_overlapping_spend_pages_cannot_hide_a_missing_row(monkeypatch, recovers):
    monkeypatch.setattr("giraffe.gateway_attribution.PAGE_SIZE", 3)
    polls = 0

    async def observer(request, rows):
        nonlocal polls
        if request.url.params["page"] == "1":
            polls += 1
            return spend_page(request, [*rows, {"request_id": "unrelated"}], total=4)
        extra_id = "distinct" if recovers and polls > 1 else "unrelated"
        return spend_page(request, [{"request_id": extra_id}], total=4)

    cfg, run, _ = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.metrics["collection_complete"] is recovers
    assert check.metrics["state"] == ("verified" if recovers else "unverified")
    assert check.metrics["matched_requests"] == (2 if recovers else 0)


async def test_late_logs_are_read_in_final_part_of_settle_window(monkeypatch):
    started = None
    times = []
    async def observer(request, rows):
        nonlocal started
        now = time.monotonic()
        started = now if started is None else started
        times.append(now-started)
        return spend_page(request, rows if now-started >= .28 else [])
    cfg, run, _ = setup(monkeypatch, observer)
    cfg.gateway_attribution.settle_seconds = .4
    check = await collect(cfg, run)
    assert check.metrics["state"] == "verified", times
    assert len(times) <= 3 and times[-1] >= .28
    assert time.monotonic() - started < .6


async def test_early_complete_read_cannot_hide_late_conflicting_rows(monkeypatch):
    started = None

    async def observer(request, rows):
        nonlocal started
        now = time.monotonic()
        started = now if started is None else started
        if now - started >= .02:
            rows = [*rows, {**rows[0], "request_id": "late-row", "api_key": "0" * 64}]
        return spend_page(request, rows)

    cfg, run, reads = setup(monkeypatch, observer)
    cfg.gateway_attribution.settle_seconds = .2
    check = await collect(cfg, run)
    assert len(reads) == 3 and check.metrics["collection_complete"]
    assert check.metrics["state"] == "unverified"
    assert check.metrics["matched_requests"] == check.metrics["unverified_requests"] == 1


async def test_final_read_timeout_does_not_reuse_early_complete_evidence(monkeypatch):
    polls = 0

    async def observer(request, rows):
        nonlocal polls
        polls += 1
        if polls == 3:
            await asyncio.Event().wait()
        return spend_page(request, rows)

    cfg, run, _ = setup(monkeypatch, observer)
    cfg.gateway_attribution.settle_seconds = .2
    check = await collect(cfg, run)
    assert polls == 3 and check.metrics["collection_complete"] is False
    assert check.metrics["state"] == "unverified"
    assert check.metrics["matched_requests"] == 0 and check.metrics["unverified_requests"] == 2
    assert "bounded window" in check.metrics["reason"] and "reason_code" not in check.metrics


async def test_shortened_collection_window_keeps_complete_pages_unverified(monkeypatch):
    async def observer(request, rows):
        return spend_page(request, rows)

    cfg, run, reads = setup(monkeypatch, observer)
    cfg.gateway_attribution.settle_seconds = 1
    run.deadline = time.monotonic() + .25
    check = await collect(cfg, run)
    assert reads and check.metrics["collection_complete"] is False
    assert check.metrics["state"] == "unverified" and check.metrics["unverified_requests"] == 2


@pytest.mark.parametrize("run_limited", [False, True])
async def test_final_reader_timeout_records_only_an_elapsed_run_deadline(monkeypatch, local_tokenizer, run_limited):
    polls = 0

    async def delayed_monitor(run, tasks):
        await asyncio.Event().wait()

    async def observer(request, rows):
        nonlocal polls
        polls += 1
        if polls == 3:
            await asyncio.Event().wait()
        return spend_page(request, rows)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.max_duration_seconds = .26 if run_limited else 5
    cfg.gateway_attribution.settle_seconds = .6 if run_limited else .2
    # Exercise the reader's own timeout before the periodic monitor sees it.
    monkeypatch.setattr(_Run, "monitor", delayed_monitor)

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert len(report.requests) == 2 and all(record.valid for record in report.requests)
    assert len(reads) == check.metrics["collection_attempts"] == 3
    assert check.metrics["state"] == "unverified" and not check.metrics["collection_complete"]
    assert report.abort_reason == ("maximum run duration reached" if run_limited else None)
    assert ("max_duration_seconds" in report.limits_reached) is run_limited
    assert check.metrics.get("reason_code") == ("time_limited" if run_limited else None)


async def test_duration_plan_and_remedy_cover_spend_log_settle(monkeypatch, local_tokenizer):
    async def observer(request, rows):
        return spend_page(request, rows)

    cfg, _, reads = setup(monkeypatch, observer)
    cfg.max_duration_seconds = .25
    cfg.request_timeout_seconds = .02
    cfg.gateway_attribution.settle_seconds = 1

    plan = request_plan(cfg)
    assert plan["estimated_low_seconds"] == .85
    assert plan["estimated_high_seconds"] == 1
    assert plan["budget_high_seconds"] == pytest.approx(1.04)
    assert not plan["errors"]
    warning = next(item for item in plan["time_warnings"] if item["kind"] == "incomplete_coverage")
    assert warning["cut_checks"] == ["proxy · Gateway spend-log settle"]
    assert warning["actions"][0]["field"] == "max_duration_seconds"

    report = await run_suite(cfg)
    assert reads and len(report.requests) == 2
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert check.status == "inconclusive"
    assert check.metrics["reason_code"] == "time_limited"
    assert "spend-log settle window" in check.metrics["reason"]
    assert check.remediation == {"action": "set_limit", "limit": "max_duration", "minimum": 2}
    assert "--max-duration" in check.summary
    assert "max_requests" not in check.summary


@pytest.mark.parametrize("cap,interruption", [(10, "transport"), (None, "transport"),
                                               (None, "dispatch"), (None, "user")])
async def test_interrupted_attribution_probes_retain_partial_evidence(monkeypatch, local_tokenizer, cap, interruption):
    stop = asyncio.Event()

    async def observer(request, rows):
        return spend_page(request, rows)

    async def before_marked(key):
        if key == "secret-BOB":
            if interruption == "user":
                stop.set()
            await asyncio.Event().wait()

    def progress(event):
        if (interruption == "dispatch" and event["scenario"] == "gateway_attribution"
                and event["event"] == "request_completed"):
            time.sleep(.55)

    cfg, _, reads = setup(monkeypatch, observer, before_marked=before_marked)
    cfg.max_requests = cap
    cfg.max_duration_seconds = 5 if interruption == "user" else .5
    cfg.request_timeout_seconds = 1
    cfg.gateway_attribution.samples = 2
    cfg.gateway_attribution.settle_seconds = .25

    report = await run_suite(cfg, progress=progress, stop_event=stop)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    expected_count = 1 if interruption == "dispatch" else 2
    assert check.status == "inconclusive"
    assert check.metrics["planned_requests"] == 4
    assert check.metrics["observed_requests"] == check.metrics["unverified_requests"] == expected_count
    assert check.metrics["matched_requests"] == 0 and not reads
    assert check.evidence_ids == [record.id for record in report.requests]
    assert report.requests[0].identity == "alice" and report.requests[0].valid
    if interruption == "user":
        assert report.abort_reason == "cancelled by user"
        assert report.requests[1].termination_reason == "user_stop"
        assert "reason_code" not in check.metrics and check.remediation.get("limit") != "max_duration"
    else:
        assert report.abort_reason == "maximum run duration reached"
        if interruption == "transport":
            assert report.requests[1].identity == "bob" and report.requests[1].termination_reason == "run_duration_limit"
        assert check.metrics["reason_code"] == "time_limited"
        assert check.metrics["collection_attempts"] == 0
        assert "interrupted the configured attribution probes" in check.finding_summary
        assert check.remediation == {"action": "set_limit", "limit": "max_duration", "minimum": 5}
        assert "--max-duration" in check.summary and "max_requests" not in check.summary


@pytest.mark.parametrize("interruption", ["duration", "user"])
async def test_marked_inference_rechecks_run_admission_after_building_request(
        monkeypatch, local_tokenizer, interruption):
    stop = asyncio.Event()
    original = httpx.AsyncClient.build_request
    marked_builds, sent_keys, progress_events = [], [], []

    def build_request(self, *args, **kwargs):
        request = original(self, *args, **kwargs)
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            marked_builds.append(request)
            if len(marked_builds) == 2:
                if interruption == "user":
                    stop.set()
                else:
                    time.sleep(.35)
        return request

    async def before_marked(key):
        sent_keys.append(key)

    async def observer(request, rows):
        return spend_page(request, rows)

    monkeypatch.setattr(httpx.AsyncClient, "build_request", build_request)
    cfg, _, reads = setup(monkeypatch, observer, before_marked=before_marked)
    cfg.max_duration_seconds = .30 if interruption == "duration" else 5

    report = await run_suite(cfg, progress=progress_events.append, stop_event=stop)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert len(marked_builds) == 2 and sent_keys == ["secret-ALICE"] and not reads
    assert [r.identity for r in report.requests] == ["alice"] and report.requests[0].valid
    assert len([e for e in progress_events if e["event"] == "request_started"]) == 1
    assert check.evidence_ids == [report.requests[0].id]
    assert check.metrics["planned_requests"] == 2 and check.metrics["observed_requests"] == 1
    assert check.metrics["collection_attempts"] == 0 and check.status == "inconclusive"
    if interruption == "duration":
        assert report.abort_reason == "maximum run duration reached"
        assert check.metrics["reason_code"] == "time_limited" and check.remediation["limit"] == "max_duration"
    else:
        assert report.abort_reason == "cancelled by user" and "reason_code" not in check.metrics
        assert "identity probes" in check.finding_summary


async def test_partial_probe_budget_does_not_hide_observed_mismatch(monkeypatch, local_tokenizer):
    async def observer(request, rows):
        return spend_page(request, [{**rows[0], "completion_tokens": 9, "total_tokens": 12}])

    cfg, _, _ = setup(monkeypatch, observer)
    immediate_spend_polls(monkeypatch, cfg)
    cfg.max_requests = 1

    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert check.status == "fail" and check.metrics["state"] == "mismatch"
    assert check.metrics["observed_requests"] == check.metrics["mismatched_requests"] == 1
    assert check.metrics["planned_requests"] == 2
    assert "reason_code" not in check.metrics


@pytest.mark.parametrize("stop_after,interrupted", [(1, "identity probes"), (2, "evidence collection")])
async def test_manual_stop_after_probe_retains_the_interrupted_stage(monkeypatch, local_tokenizer, stop_after, interrupted):
    stop = asyncio.Event()
    completed = 0

    async def observer(request, rows):
        return spend_page(request, rows)

    def progress(event):
        nonlocal completed
        if event["event"] == "request_completed" and event["scenario"] == "gateway_attribution":
            completed += 1
            if completed == stop_after:
                stop.set()

    cfg, _, reads = setup(monkeypatch, observer)
    report = await run_suite(cfg, progress=progress, stop_event=stop)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert report.abort_reason == "cancelled by user" and check.status == "inconclusive"
    assert check.metrics["planned_requests"] == 2
    assert check.metrics["observed_requests"] == check.metrics["unverified_requests"] == stop_after
    assert check.metrics["collection_attempts"] == 0 and not reads
    assert all(record.valid for record in report.requests)
    assert check.evidence_ids == [record.id for record in report.requests]
    assert interrupted in check.finding_summary and "reason_code" not in check.metrics
    assert check.remediation["action"] == "review_check"


async def test_duration_remedy_covers_restart_and_attribution(monkeypatch, local_tokenizer):
    async def observer(request, rows):
        return spend_page(request, rows)

    async def ready(self, deadline):
        await asyncio.sleep(2.28)
        return True

    cfg, _, reads = setup(monkeypatch, observer)
    monkeypatch.setattr(LLMClient, "ready", ready)
    cfg.targets[0].restart_command = [sys.executable, "-c", "pass"]
    cfg.restart_target = "proxy"
    cfg.readiness_timeout_seconds = 2.7
    cfg.max_duration_seconds = 3
    cfg.request_timeout_seconds = .15
    cfg.gateway_attribution.settle_seconds = 1

    report = await run_suite(cfg)
    assert report.observations["targets"]["proxy"]["restart"]["status"] == "ready"
    assert reads and len(report.requests) == 3  # Readiness and two attribution probes.
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert check.metrics["reason_code"] == "time_limited"
    assert check.remediation == {"action": "set_limit", "limit": "max_duration", "minimum": 5}
    assert "at least 5 seconds" in check.summary


@pytest.mark.parametrize("restart,cap,minimum", [(False, 3.9, 5), (True, 5.9, 7)])
async def test_duration_remedy_includes_streamed_compatibility(monkeypatch, local_tokenizer, restart, cap, minimum):
    for ref in ("ALICE", "BOB", "OBSERVER"):
        monkeypatch.setenv(ref, "secret-" + ref)
    rows = []

    async def handler(request):
        if request.method == "GET":
            if request.url.path == "/v1/models":
                await asyncio.sleep(2.27)
                return httpx.Response(200)
            return spend_page(request, rows) if request.url.path == "/spend/logs/v2" else httpx.Response(404)
        body = json.loads(request.content)
        if marker := request.headers.get("x-litellm-trace-id"):
            key = request.headers["authorization"].removeprefix("Bearer ")
            rows.append({"request_id": str(len(rows)), "session_id": marker,
                "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                "api_key": hashlib.sha256(key.encode()).hexdigest(), "prompt_tokens": 3,
                "completion_tokens": 5, "total_tokens": 8, "status": "success"})
        delay = .9
        if restart:
            delay = .88 if marker or body.get("max_tokens") == 1 else .03
        await asyncio.sleep(delay)
        return httpx.Response(200, content=(b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\ndata: [DONE]\n\n'))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    cfg = config(stream=True, request_timeout_seconds=1, readiness_timeout_seconds=2.4)
    if restart:
        cfg.restart_target = "proxy"
        cfg.targets[0].restart_command = [sys.executable, "-c", "import time; time.sleep(.87)"]
    else:
        cfg.checks = ["access", "gateway_attribution"]
        cfg.max_requests = None
    cfg.gateway_attribution.settle_seconds = .6
    cfg.max_duration_seconds = cap

    report = await run_suite(cfg)
    if restart:
        assert report.observations["targets"]["proxy"]["restart"]["status"] == "ready"
    assert [r.scenario for r in report.requests] == ["readiness" if restart else "initial",
                                                    "performance_evidence_probe", "gateway_attribution", "gateway_attribution"]
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert check.metrics["reason_code"] == "time_limited"
    assert check.remediation == {"action": "set_limit", "limit": "max_duration", "minimum": minimum}


@pytest.mark.parametrize("cap,eligible", [(None, True), (2, False), (3, True)])
def test_shared_compatibility_allowance_respects_quota_after_initial_request(cap, eligible):
    cfg = config(stream=True)
    cfg.checks = ["access", "gateway_attribution"]
    cfg.max_requests = cap
    planned = request_plan(cfg)
    assert any(item["id"] == "performance_evidence_probe" for item in planned["time_timeline"]) is eligible

    access_only = request_plan(cfg.model_copy(update={"checks": ["access"]}))
    assert [item["id"] for item in access_only["time_timeline"]] == ["initial"]
    cfg.targets[0].gateway = None
    unsupported = request_plan(cfg)
    assert [item["id"] for item in unsupported["time_timeline"]] == ["initial"]


@pytest.mark.parametrize("overlap", [False, True])
def test_duration_plan_keeps_attribution_after_all_shared_work(overlap):
    from tests.test_run_limits_phase4_planning import observations

    cfg = config()
    cfg.checks = ["correctness", "gateway_attribution"]
    cfg.max_requests = None
    cfg.gateway_attribution.settle_seconds = 1
    cfg.metrics_timeout_seconds = .2
    cfg.overlap_models = overlap
    cfg.targets.append(cfg.targets[0].model_copy(update={"name": "second", "gateway": None}))
    measured = observations(cfg, p50=100, p90=100)
    measured["second"] = observations(cfg, p50=2000, p90=2000)["second"]
    shared = request_plan(cfg.model_copy(update={"checks": ["correctness"]}), observations=measured)
    cfg.max_duration_seconds = shared["estimated_high_seconds"] + .5

    plan = request_plan(cfg, observations=measured)
    assert plan["estimated_high_seconds"] == pytest.approx(shared["estimated_high_seconds"] + 1.2, abs=.001)
    preflight = next(item for item in plan["time_components"] if item["id"] == "gateway_attribution_preflight")
    assert preflight["target"] == "proxy" and preflight["high_seconds"] == .2
    assert [(item["target"], item["id"]) for item in plan["time_timeline"]] == [
        ("proxy", "performance_evidence_probe"), ("proxy", "correctness"),
        ("second", "performance_evidence_probe"), ("second", "correctness"), ("proxy", "gateway_attribution_preflight"),
        ("proxy", "gateway_attribution"), ("proxy", "gateway_attribution_settle"),
    ]
    warning = next(item for item in plan["time_warnings"] if item["kind"] == "incomplete_coverage")
    assert warning["cut_checks"] == ["proxy · Gateway spend-log settle"]
    assert not plan["errors"]


async def test_cancelling_reader_keeps_probe_counts_and_clears_transient_hashes(monkeypatch):
    reading = asyncio.Event()
    async def observer(request, rows):
        reading.set()
        await asyncio.Event().wait()
    cfg, run, _ = setup(monkeypatch, observer)
    task = asyncio.create_task(collect(cfg, run))
    await asyncio.wait_for(reading.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    data = result(run, cfg.targets[0]).metrics
    assert data["state"] == "unverified"
    assert data["observed_requests"] == data["unverified_requests"] == 2
    assert "interrupt" in data["reason"].lower()
    assert all(record._gateway_key_hash is None for record in run.records)


@pytest.mark.parametrize("rejected", ["expired", "connection"])
async def test_identity_failure_does_not_change_shared_health_or_stop_other_work(monkeypatch, rejected):
    async def observer(request, rows):
        return spend_page(request, rows)
    cfg, run, _ = setup(monkeypatch, observer, rejected=rejected)
    cfg.checks = ["access", "serving", "gateway_attribution"]
    cfg.stop_after_errors = cfg.unresponsive_after_failures = 1
    cfg.limits.min_samples = 2
    target = cfg.targets[0]
    target.api_key_env = "SHARED"
    responses = []
    ordinary = RequestSpec(fixture_id="shared", scenario="initial", check_ids=["access", "serving"],
                           messages=[{"role": "user", "content": "OK"}], max_tokens=16)
    async with LLMClient(target, cfg) as client:
        client.on_response = lambda: responses.append("shared")
        await run.request(target, client, ordinary)
        await execute(run, target, client)
        assert run.allowed(target.name)
        await run.request(target, client, ordinary)
    assert run.errors == 0
    assert not run.endpoint_health[target.name]["abort_reason"]
    assert len(responses) == 2
    probes = [r for r in run.records if r.identity is not None]
    assert len(probes) == 2 and all(r.check_ids == ["gateway_attribution"] for r in probes)
    core = [c for c in _checks(run, target) if c.id in {"access", "serving"}]
    assert all(c.status == "pass" for c in core)


@pytest.mark.parametrize("across_polls", [False, True])
async def test_changing_unexpected_owner_is_unverified_and_fingerprints_stay_transient(monkeypatch, across_polls):
    if across_polls:
        monkeypatch.setattr("giraffe.gateway_attribution.PAGE_SIZE", 1)
    polls = 0
    async def observer(request, rows):
        nonlocal polls
        page = int(request.url.params["page"])
        if page == 1:
            polls += 1
        data = json.loads(json.dumps(rows))
        data[0]["api_key"] = "0" * 64 if polls == 1 else "1" * 64
        if across_polls and polls == 1:
            return spend_page(request, data[:1] if page == 1 else [], total=2)
        if across_polls:
            return spend_page(request, data[page-1:page], total=2)
        data.append({**data[0], "api_key": "1" * 64})
        return spend_page(request, data)
    cfg, run, _ = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.metrics["state"] == "unverified"
    serialized = check.model_dump_json() + json.dumps(run.observations)
    assert "0" * 64 not in serialized and "1" * 64 not in serialized


async def test_capped_collection_cannot_verify_even_when_all_probes_are_present(monkeypatch):
    async def observer(request, rows):
        return spend_page(request, rows, total_is_capped=True)
    cfg, run, reads = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.metrics["state"] == "unverified"
    assert check.metrics["collection_complete"] is False
    assert all(r.url.params["page"] == "1" for r in reads)


@pytest.mark.parametrize("failure", [TimeoutError, httpx.ConnectError])
async def test_runner_fallback_records_keep_selected_identity_and_health_isolation(failure):
    cfg = config()
    cfg.stop_after_errors = cfg.unresponsive_after_failures = 1
    run = _Run(cfg, None, None)
    class FailingClient:
        async def execute(self, spec, **kwargs):
            raise failure("unreachable")
    spec = RequestSpec(fixture_id="probe", scenario="gateway_attribution", identity="alice",
                       check_ids=["gateway_attribution"], messages=[{"role": "user", "content": "OK"}])
    record = await run.request(cfg.targets[0], FailingClient(), spec)
    assert record.identity == "alice" and run.attempted == 1 and len(run.records) == 1
    assert run.errors == 0 and run.allowed("proxy")
    assert not run.endpoint_health["proxy"]["abort_reason"]


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("rejected,http_status,evidence", [
    ("expired", 401, "authentication"), ("deep_json", 401, "unknown"),
    ("deep_success", 200, None), ("invalid_unicode", 200, None),
])
async def test_mixed_full_run_keeps_shared_checks_passing_with_rejected_identity(monkeypatch, rejected, http_status, evidence):
    async def observer(request, rows):
        return spend_page(request, rows)
    cfg, _, _ = setup(monkeypatch, observer, rejected=rejected)
    cfg.checks = ["access", "serving", "gateway_attribution"]
    cfg.stop_after_errors = 1
    cfg.limits.min_samples = 2
    cfg.targets[0].api_key_env = "SHARED"
    report = await run_suite(cfg)
    checks = {check.id: check for check in report.checks}
    assert checks["access"].status == checks["serving"].status == "pass"
    assert checks["gateway_attribution"].status == "inconclusive"
    assert len([record for record in report.requests if record.identity is not None]) == 2
    assert all(record.http_status == 200 for record in report.requests if record.identity is None)
    probe = next(record for record in report.requests if record.identity == "alice")
    assert probe.status == "failed" and probe.http_status == http_status
    assert probe.gateway_evidence.error_kind == evidence


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("change", [
    "unused_alias", "reader", "settings", "unsupported", "unconfigured", "enabled", "legacy_collection",
])
async def test_attribution_changes_do_not_veto_shared_baseline_evidence(monkeypatch, change):
    async def observer(request, rows):
        assert change not in {"unsupported", "unconfigured"}, "inactive attribution read gateway evidence"
        return spend_page(request, rows)

    async def run_variant(current):
        with monkeypatch.context() as env:
            cfg, _, _ = setup(env, observer)
            cfg.checks = ["access", "serving", "gateway_attribution"]
            cfg.targets[0].api_key_env = "SHARED"
            cfg.limits.min_samples = 2
            cfg.limits.regression_percent = 100000
            cfg.samples = 8
            cfg.max_requests = 30
            cfg.gateway_attribution.settle_seconds = .2
            if change == "unsupported" or (change == "enabled" and not current):
                cfg.targets[0].gateway.profile = "other"
            if change in {"settings", "unsupported"} and current:
                cfg.gateway_attribution.settle_seconds = .25
            if change == "unconfigured":
                cfg.gateway_attribution = None
                cfg.targets[0].gateway = None
            if change in {"unsupported", "unconfigured", "unused_alias"}:
                cfg.targets[0].test_identities["unused"] = GatewayIdentity(
                    api_key_env="UNUSED_NEW" if current else "UNUSED_OLD")
            if change == "reader" and current:
                cfg.targets[0].gateway.api_key_env = "OTHER_READER"
                env.setenv("OTHER_READER", "secret-OBSERVER")
            return await run_suite(cfg)

    baseline = await run_variant(False)
    current = await run_variant(True)
    assert baseline.overall == current.overall == "pass"
    if change == "legacy_collection":
        next(check for check in baseline.checks if check.id == "gateway_attribution").metrics["collection_version"] = 5
    target = compare_baseline(current, baseline).baseline["targets"][0]
    assert not target["reasons"]
    assert any(row["check"] == "serving" and row["metric"] == "valid_completion_rate"
               and row["status"] == "pass" for row in target["comparisons"])
    attribution = next(check for check in current.checks if check.id == "gateway_attribution")
    attribution_rows = [row for row in target["comparisons"] if row["check"] == "gateway_attribution"]
    if change in {"unsupported", "unconfigured"}:
        assert not attribution.required and not attribution_rows
        assert not any(record.identity for record in current.requests)
    else:
        assert attribution.required and len(attribution_rows) == 2
        expected = "pass" if change == "unused_alias" else "inconclusive"
        assert all(row["status"] == expected for row in attribution_rows)
        if change == "legacy_collection":
            assert all("Rerun the baseline" in row["reason"] for row in attribution_rows)
        if change == "enabled":
            assert target["status"] == "inconclusive" and "gateway_attribution" in target["summary"]


@pytest.mark.parametrize("supported", [True, False])
async def test_saved_baseline_compares_shared_checks_across_attribution_selection(
        monkeypatch, local_tokenizer, tmp_path, supported):
    async def observer(request, rows):
        assert supported
        return spend_page(request, rows)

    async def saved_run(checks, *, samples=8):
        with monkeypatch.context() as env:
            cfg, _, _ = setup(env, observer)
            immediate_spend_polls(env, cfg)
            cfg.checks = checks
            cfg.samples = samples
            cfg.max_requests = 30
            cfg.limits.min_samples = 2
            cfg.limits.regression_percent = 100000
            if not supported:
                cfg.targets[0].gateway.profile = "other"
            report = await run_suite(cfg)
            assert report.overall == "pass"
            path, _ = write_report(report, tmp_path)
            return RunReport.model_validate_json(path.read_bytes())

    ordinary = ["access", "serving"]
    mixed_checks = [*ordinary, "gateway_attribution"]
    plain = await saved_run(ordinary)
    mixed = await saved_run(mixed_checks)
    full_key, shared_key = "workload_fixture_sha256", "shared_workload_fixture_sha256"
    assert plain.manifest["fixture_pack"][full_key] == mixed.manifest["fixture_pack"][shared_key]
    assert [r.request_hash for r in plain.requests] == [r.request_hash for r in mixed.requests if r.identity is None]
    for current, baseline in ((mixed, plain), (plain, mixed)):
        target = compare_baseline(current, baseline).baseline["targets"][0]
        assert target["reasons"] == []
        assert {row["check"] for row in target["comparisons"]
                if row["metric"] == "valid_completion_rate" and row["status"] == "pass"} == set(ordinary)
        attribution = [row for row in target["comparisons"] if row["check"] == "gateway_attribution"]
        if supported and current is mixed:
            assert len(attribution) == 2 and all(row["status"] == "inconclusive" for row in attribution)
            assert target["status"] == "inconclusive" and "gateway_attribution" in target["summary"]
        else:
            assert not attribution and target["status"] == "pass"

    if not supported:
        return
    changed_checks = await saved_run(["access", "gateway_attribution"])
    changed_settings = await saved_run(mixed_checks, samples=7)
    for changed, reason in ((changed_checks, "Run setting checks differs."),
                            (changed_settings, "Run setting samples differs.")):
        target = compare_baseline(changed, plain).baseline["targets"][0]
        assert reason in target["reasons"]
        assert not any(row["check"] in ordinary for row in target["comparisons"])

    for replacement in (None, "unknown", "a" * 64):
        unattested = mixed.model_copy(deep=True)
        pack = unattested.manifest["fixture_pack"]
        if replacement is None:
            # An actually mixed archive must not fall back to its full digest.
            pack[full_key] = pack.pop(shared_key)
        else:
            pack[shared_key] = replacement
        target = compare_baseline(plain, unattested).baseline["targets"][0]
        assert "Fixture pack identity differs." in target["reasons"]
        assert not target["comparisons"]

    forged_plain = plain.model_copy(deep=True)
    pack = forged_plain.manifest["fixture_pack"]
    pack[shared_key], pack[full_key] = pack[full_key], "a" * 64
    assert "Fixture pack identity differs." in compare_baseline(mixed, forged_plain).baseline["targets"][0]["reasons"]


@pytest.mark.parametrize("cap", [2, 3, 4])
async def test_changed_request_cap_only_blocks_comparison_when_shared_work_was_cut(
        monkeypatch, local_tokenizer, tmp_path, cap):
    async def observer(request, rows):
        return spend_page(request, rows)

    client_class = httpx.AsyncClient
    original_send = client_class.send

    async def saved_run(max_requests, *, checks=None):
        with monkeypatch.context() as env:
            cfg, _, _ = setup(env, observer)
            immediate_spend_polls(env, cfg)
            cfg.checks = checks or ["serving"] + (["gateway_attribution"] if max_requests else [])
            cfg.samples = 2
            cfg.limits.min_samples = 2
            cfg.max_requests = max_requests

            async def send(client, request, **kwargs):
                if max_requests and request.method == "POST" and "x-litellm-trace-id" not in request.headers:
                    await asyncio.sleep(.13)
                return await original_send(client, request, **kwargs)

            env.setattr(client_class, "send", send)
            report = await run_suite(cfg)
            path, _ = write_report(report, tmp_path)
            return RunReport.model_validate_json(path.read_bytes())

    baseline, current = await saved_run(None), await saved_run(cap)
    assert baseline.overall == "pass" and "max_requests" in current.limits_reached
    target = compare_baseline(current, baseline).baseline["targets"][0]
    if cap == 2:
        assert "load_shape differs." in target["reasons"]
        assert any("max_requests differs" in reason for reason in target["reasons"])
        assert "shared" in current.observations["request_limit_scopes"]
        assert not any(row["check"] == "serving" for row in target["comparisons"])
        return
    assert current.observations["request_limit_scopes"] == ["gateway"]
    assert [record.request_hash for record in current.requests if record.identity is None] == [
        record.request_hash for record in baseline.requests]
    assert next(check for check in current.checks if check.id == "serving").status == "pass"
    assert target["reasons"] == [] and target["status"] == "fail"
    assert any(row["check"] == "serving" and row["metric"] == "latency_ms" and row["status"] == "fail"
               for row in target["comparisons"])
    assert all(row["status"] == "inconclusive" for row in target["comparisons"] if row["check"] == "gateway_attribution")
    if cap == 3:
        stopped = current.model_copy(update={"abort_reason": "maximum request budget reached"})
        assert compare_baseline(stopped, baseline).baseline["targets"][0]["reasons"] == []
        isolated = ["gateway_attribution"]
        isolated_baseline = await saved_run(None, checks=isolated)
        isolated_current = await saved_run(1, checks=isolated)
        assert isolated_current.observations["request_limit_scopes"] == ["gateway"]
        isolated_reasons = compare_baseline(isolated_current, isolated_baseline).baseline["targets"][0]["reasons"]
        assert any("max_requests differs" in reason for reason in isolated_reasons)
        for check_id, metrics in (
            ("load", {"points": [{"reason_code": "budget_limited"}]}),
            ("recovery", {"sustained_completion": {"reason": "allowance consumed with sustained concurrency"}}),
        ):
            constrained = current.model_copy(deep=True)
            constrained.checks.append(CheckResult(id=check_id, title=check_id, target="proxy", status="inconclusive",
                                                  summary="Shared work hit a reservation.", metrics=metrics))
            assert "load_shape differs." in _settings_reasons(constrained, baseline)
    current.observations.pop("request_limit_scopes")
    archived = compare_baseline(current, baseline).baseline["targets"][0]
    assert "load_shape differs." in archived["reasons"]
    assert any("max_requests differs" in reason for reason in archived["reasons"])


async def test_serving_comparison_qualifies_actual_stream_options_after_compatibility_probe(
        monkeypatch, local_tokenizer, tmp_path):
    client_class = httpx.AsyncClient
    original_send = client_class.send

    async def send(client, request, **kwargs):
        if request.method == "POST" and "x-litellm-trace-id" not in request.headers:
            if not json.loads(request.content).get("logprobs"):
                await asyncio.sleep(.13)
        return await original_send(client, request, **kwargs)

    async def saved_run(cap, rejections, checks, expected_options):
        with monkeypatch.context() as env:
            cfg, posts = streamed_gateway(env, rejected_attempts=rejections)
            cfg.checks = checks
            cfg.samples = cfg.limits.min_samples = 2
            cfg.max_requests = cap
            env.setattr(client_class, "send", send)
            report = await run_suite(cfg)
            assert next(check for check in report.checks if check.id == "serving").status == "pass"
            wire_options = [(body.get("logprobs") is True, body.get("stream_options", {}).get("include_usage") is True)
                            for marked, body in posts if not marked][-2:]
            assert wire_options == [expected_options] * 2
            path, _ = write_report(report, tmp_path)
            saved = RunReport.model_validate_json(path.read_bytes())
            for record in saved.requests:
                if record.scenario == "warm":
                    assert (record.requested_stream_logprobs, record.requested_stream_usage) == expected_options
            return saved

    ordinary = ["serving"]
    mixed = ["serving", "gateway_attribution"]
    baseline = await saved_run(6, 0, ordinary, (True, True))
    changed_runs = (await saved_run(3, 0, mixed, (False, False)),
                    await saved_run(6, 1, mixed, (False, True)))

    def warm_rows(current, previous):
        target = compare_baseline(current, previous).baseline["targets"][0]
        assert not target["reasons"] and target["status"] != "fail"
        return {row["metric"]: row for row in target["comparisons"]
                if row["check"] == "serving" and row["scenario"] == "warm"}

    for changed in changed_runs:
        rows = warm_rows(changed, baseline)
        latency = rows["latency_ms"]
        assert latency["current"] > latency["baseline"] + 100
        assert latency["status"] == "inconclusive"
        assert any(warning["code"] == "stream_options_changed" for warning in latency["warnings"])
        assert rows["valid_completion_rate"]["status"] == "pass"

    # Old uncapped/no-probe reports may lack these flags. Matching request hashes
    # still attest the same payload when comparing with a new report.
    unchanged = changed_runs[0]
    archive = unchanged.model_copy(deep=True)
    for record in archive.requests:
        record.requested_stream_logprobs = record.requested_stream_usage = None
        record.workload.pop("token_evidence", None)
    assert warm_rows(unchanged, archive)["latency_ms"]["status"] == "pass"


@pytest.mark.parametrize("targets,expiry", [(1, "gateway"), (2, "gateway"), (2, "shared")])
async def test_duration_limit_preserves_only_shared_work_completed_before_gateway_phase(
        monkeypatch, local_tokenizer, tmp_path, targets, expiry):
    client_class = httpx.AsyncClient
    original_send = client_class.send

    async def saved_run(*, mixed):
        async def observer(request, rows):
            if mixed and rows:
                await asyncio.sleep(1)
            return spend_page(request, rows)

        async def send(client, request, **kwargs):
            if mixed and expiry == "shared" and request.method == "POST" and request.url.host == "second":
                await asyncio.sleep(1)
            return await original_send(client, request, **kwargs)

        with monkeypatch.context() as env:
            cfg, _, reads = setup(env, observer)
            immediate_spend_polls(env, cfg)
            if targets == 2:
                first = cfg.targets[0]
                cfg.targets.append(first.model_copy(update={"name": "second", "url": "http://second/v1",
                    "gateway": first.gateway.model_copy(update={"url": "http://second/spend/logs/v2"})}))
            cfg.checks = ["serving"] + (["gateway_attribution"] if mixed else [])
            cfg.samples = cfg.limits.min_samples = 2
            cfg.max_requests = None
            cfg.max_duration_seconds = .55 if mixed else 1.5
            env.setattr(client_class, "send", send)
            report = await run_suite(cfg)
            path, _ = write_report(report, tmp_path)
            return RunReport.model_validate_json(path.read_bytes()), reads

    baseline, _ = await saved_run(mixed=False)
    current, reads = await saved_run(mixed=True)
    assert baseline.abort_reason is None
    assert current.abort_reason == "maximum run duration reached"
    assert "max_duration_seconds" in current.limits_reached
    assert current.observations["duration_limit_scope"] == expiry
    assert bool(reads) == (expiry == "gateway")
    if expiry == "gateway":
        assert [record.request_hash for record in current.requests if record.identity is None] == [
            record.request_hash for record in baseline.requests]
        assert all(check.status == "pass" for check in current.checks if check.id == "serving")
        assert next(check for check in current.checks if check.id == "gateway_attribution").metrics["reason_code"] == "time_limited"

    for report, previous in ((current, baseline), (baseline, current)):
        compared = compare_baseline(report, previous).baseline
        assert len(compared["targets"]) == targets
        for entry in compared["targets"]:
            if expiry == "gateway":
                assert not entry["reasons"]
                assert any(row["check"] == "serving" and row["metric"] == "valid_completion_rate"
                           and row["status"] == "pass" for row in entry["comparisons"])
            else:
                assert any("run was aborted" in reason for reason in entry["reasons"])
                assert any("max_duration_seconds differs" in reason for reason in entry["reasons"])
                assert not any(row["check"] == "serving" for row in entry["comparisons"])
    if expiry == "gateway" and targets == 1:
        limited = current.model_copy(deep=True)
        limited.manifest["config"]["concurrency"] = 1
        limited.limits_reached.append("concurrency")
        for scopes, comparable in ((["gateway"], True), (["gateway", "shared"], False), ([], False)):
            limited.observations["concurrency_limit_scopes"] = scopes
            reasons = _settings_reasons(limited, baseline)
            assert bool(reasons) != comparable
            if not comparable:
                assert any("concurrency differs" in reason for reason in reasons)
    current.observations.pop("duration_limit_scope")
    assert all(any("run was aborted" in reason for reason in entry["reasons"])
               for entry in compare_baseline(current, baseline).baseline["targets"])


@pytest.mark.parametrize("targets,stop_during", [(1, "gateway_fast"), (2, "gateway"), (2, "shared")])
async def test_user_stop_preserves_only_shared_work_completed_before_gateway_phase(
        monkeypatch, local_tokenizer, tmp_path, targets, stop_during):
    client_class = httpx.AsyncClient
    original_send = client_class.send
    stop = asyncio.Event()
    stop_in_gateway = stop_during.startswith("gateway")

    async def saved_run(*, mixed):
        async def observer(request, rows):
            if mixed and stop_in_gateway and rows:
                stop.set()
                if stop_during == "gateway":
                    await asyncio.Event().wait()
            return spend_page(request, rows)

        async def send(client, request, **kwargs):
            if mixed and stop_during == "shared" and request.method == "POST" and request.url.host == "second":
                stop.set()
                await asyncio.Event().wait()
            return await original_send(client, request, **kwargs)

        with monkeypatch.context() as env:
            cfg, _, _ = setup(env, observer)
            immediate_spend_polls(env, cfg)
            if targets == 2:
                cfg.targets.append(cfg.targets[0].model_copy(
                    update={"name": "second", "url": "http://second/v1", "gateway": None}))
            cfg.checks = ["serving"] + (["gateway_attribution"] if mixed else [])
            cfg.samples = cfg.limits.min_samples = 2
            cfg.max_requests = None
            env.setattr(client_class, "send", send)
            report = await run_suite(cfg, stop_event=stop)
            path, _ = write_report(report, tmp_path)
            return RunReport.model_validate_json(path.read_bytes())

    baseline = await saved_run(mixed=False)
    current = await saved_run(mixed=True)
    assert current.abort_reason == "cancelled by user" and current.overall == "inconclusive"
    assert current.observations.get("user_stop_scope") == ("gateway" if stop_in_gateway else None)
    if stop_in_gateway:
        attribution = next(check for check in current.checks if check.id == "gateway_attribution")
        assert attribution.status == "inconclusive" and attribution.metrics["matched_requests"] == 0
        assert all(check.status == "pass" for check in current.checks if check.id == "serving")
    for report, previous in ((current, baseline), (baseline, current)):
        compared = compare_baseline(report, previous).baseline
        assert len(compared["targets"]) == targets
        for target in compared["targets"]:
            if stop_in_gateway:
                assert not target["reasons"]
                assert any(row["check"] == "serving" and row["metric"] == "valid_completion_rate"
                           and row["status"] == "pass" for row in target["comparisons"])
            else:
                assert any("run was aborted" in reason for reason in target["reasons"])
                assert not any(row["check"] == "serving" for row in target["comparisons"])


@pytest.mark.parametrize("shared_pending,stop_before_gateway", [
    (True, False), (False, True),
])
def test_stop_scope_requires_complete_shared_work_and_a_new_stop(shared_pending, stop_before_gateway):
    cfg = config(stream=False)
    cfg.checks = ["serving", "correctness", "gateway_attribution"]
    cfg.targets.append(cfg.targets[0].model_copy(
        update={"name": "second", "url": "http://second/v1", "gateway": None}))
    run = _Run(cfg, None, None)
    run.finish_checks("proxy", {"serving", "correctness"})
    # Completed inference on the other target can still need judging.
    run.judging_pending = shared_pending
    run.finish_checks("second", {"serving", "correctness"})
    if stop_before_gateway:
        run.stop.set()
    with run.gateway_limit_phase():
        run.stop.set()
    assert run.reason == "cancelled by user"
    assert run.user_stop_scope is None


async def test_concurrency_limit_records_which_requests_waited_for_a_slot():
    cfg = config(stream=False)
    cfg.checks = ["serving", "gateway_attribution"]
    cfg.concurrency = 1
    run = _Run(cfg, None, None)
    run.ceiling_constrains_pool = True
    acquired = asyncio.Event()

    async def gateway():
        async with run.slot(False, gateway_probe=True):
            acquired.set()

    async with run.slot(False):
        waiting = asyncio.create_task(gateway())
        await asyncio.sleep(0)
        assert not acquired.is_set()
        assert run.limits_reached == {"concurrency"}
        assert run.concurrency_limit_scopes == {"gateway"}
        assert not run.try_acquire_slot()
        assert run.concurrency_limit_scopes == {"gateway", "shared"}
    await waiting
    assert acquired.is_set()


@pytest.mark.parametrize("targets,expiry", [(1, "gateway"), (2, "gateway"), (1, "shared")])
async def test_gateway_deadline_preserves_completed_judged_inference(
        monkeypatch, local_tokenizer, tmp_path, targets, expiry):
    client_class = httpx.AsyncClient
    original_send = client_class.send

    async def saved_run(*, mixed):
        async def observer(request, rows):
            if mixed and rows:
                await asyncio.sleep(1)
            return spend_page(request, rows)

        async def send(client, request, **kwargs):
            if request.url.host == "judge":
                return httpx.Response(200, request=request, json={"choices": [{
                    "message": {"role": "assistant", "content": json.dumps({"verdict": True, "reason": "Correct."})},
                    "finish_reason": "stop"}]})
            if mixed and expiry == "shared" and request.method == "POST" and b"PACK B" in request.content:
                await asyncio.sleep(1)
            return await original_send(client, request, **kwargs)

        with monkeypatch.context() as env:
            cfg, _, reads = setup(env, observer)
            immediate_spend_polls(env, cfg)
            if targets == 2:
                first = cfg.targets[0]
                cfg.targets.append(first.model_copy(update={"name": "second", "url": "http://second/v1",
                    "gateway": first.gateway.model_copy(update={"url": "http://second/spend/logs/v2"})}))
            cfg = RunConfig.model_validate({
                **cfg.model_dump(), "checks": ["serving", "correctness"] + (["gateway_attribution"] if mixed else []),
                "default_concurrency": 1, "samples": 2, "max_requests": None, "max_duration_seconds": .55 if mixed else 1.5,
                "limits": {**cfg.limits.model_dump(), "min_samples": 2},
                "request_source": "request_pack", "request_pack": {"version": 1, "cases": [
                    {"fixture_id": key, "scorer": "judge", "messages": [{"role": "user", "content": f"PACK {key}"}],
                     "expected": "OK"} for key in ("A", "B")]},
                "judging": {"judges": [{"name": "judge", "url": "http://judge/v1", "model": "judge"}]},
            })
            env.setattr(client_class, "send", send)
            report = await run_suite(cfg)
            path, _ = write_report(report, tmp_path)
            return RunReport.model_validate_json(path.read_bytes()), reads

    baseline, _ = await saved_run(mixed=False)
    current, reads = await saved_run(mixed=True)
    assert baseline.abort_reason is None
    assert current.abort_reason == "maximum run duration reached"
    assert current.observations["duration_limit_scope"] == expiry
    assert bool(reads) == (expiry == "gateway")
    for report in (current, baseline):
        assert all(check.status == "pass" for check in report.checks if check.id == "serving")
    compared = compare_baseline(current, baseline).baseline
    if expiry == "gateway":
        assert current.observations["judging"]["scored_requests"] == 4 * targets
        assert [(record.request_hash, record.score) for record in current.requests if record.identity is None] == [
            (record.request_hash, record.score) for record in baseline.requests]
        assert all(check.status == "pass" for check in current.checks if check.id == "correctness")
        for target in compared["targets"]:
            assert not target["reasons"]
            assert {row["check"] for row in target["comparisons"] if row["status"] == "pass"} >= {"serving", "correctness"}
    else:
        assert 0 < current.observations["judging"]["scored_requests"] < 4
        target, = compared["targets"]
        assert any("run was aborted" in reason for reason in target["reasons"])
        assert not any(row["check"] in {"serving", "correctness"} for row in target["comparisons"])


@pytest.mark.parametrize("targets,active_outage", [(1, False), (2, False), (1, True), (2, True)])
async def test_spend_reader_does_not_extend_shared_endpoint_watchdog(
        monkeypatch, local_tokenizer, tmp_path, targets, active_outage):
    client_class = httpx.AsyncClient
    original_send = client_class.send
    outage = "proxy" if targets == 1 else "second"

    async def saved_run(*, mixed):
        delayed_reader = False
        shared = Counter()
        events = []

        async def observer(request, rows):
            nonlocal delayed_reader
            if not delayed_reader:
                delayed_reader = True
                await asyncio.sleep(.45)
            return spend_page(request, rows)

        async def send(client, request, **kwargs):
            if request.method == "POST" and "x-litellm-trace-id" not in request.headers:
                host = request.url.host
                shared[host] += 1
                if shared[host] == 4:
                    if active_outage and host == outage:
                        await asyncio.sleep(.45)
                    raise httpx.ConnectError("controlled final shared failure", request=request)
            return await original_send(client, request, **kwargs)

        with monkeypatch.context() as env:
            cfg, _, reads = setup(env, observer)
            immediate_spend_polls(env, cfg)
            if targets == 2:
                first = cfg.targets[0]
                cfg.targets.append(first.model_copy(update={"name": "second", "url": "http://second/v1",
                    "gateway": first.gateway.model_copy(update={"url": "http://second/spend/logs/v2"})}))
            cfg.checks = ["serving"] + (["gateway_attribution"] if mixed else [])
            cfg.samples = 3
            cfg.limits.min_samples = 2
            cfg.limits.max_error_rate = .5
            cfg.unresponsive_after_failures = 1
            cfg.unresponsive_for_seconds = .3
            cfg.metrics_timeout_seconds = 1
            cfg.max_requests = None
            env.setattr(client_class, "send", send)
            report = await run_suite(cfg, progress=events.append)
            path, _ = write_report(report, tmp_path)
            return RunReport.model_validate_json(path.read_bytes()), reads, events

    baseline, _, _ = await saved_run(mixed=False)
    current, reads, events = await saved_run(mixed=True)
    expected_aborts = {outage} if active_outage else set()
    for report in (current, baseline):
        assert set(report.observations["target_abort_reasons"]) == expected_aborts
        for check in (check for check in report.checks if check.id == "serving"):
            assert check.status == ("inconclusive" if check.target in expected_aborts else "pass")
        assert all(target["endpoint_health"]["consecutive_failures"] == 1
                   and len(target["endpoint_health"]["failure_request_ids"]) == 1
                   for target in report.observations["targets"].values())
    assert {event["target"] for event in events if event["event"] == "target_stopping"} == expected_aborts
    observed = {request.url.host for request in reads}
    assert observed == set(current.observations["targets"]) - expected_aborts
    if not active_outage:
        assert current.abort_reason is None
        assert all(check.status == "pass" for check in current.checks if check.id == "gateway_attribution")
        for target in compare_baseline(current, baseline).baseline["targets"]:
            assert not target["reasons"]
            warm_rate = next(row for row in target["comparisons"] if row["check"] == "serving"
                             and row["scenario"] == "warm" and row["metric"] == "valid_completion_rate")
            assert warm_rate["current"] == warm_rate["baseline"] == pytest.approx(2 / 3)


@pytest.mark.parametrize("with_builtin", [False, True])
async def test_shared_baseline_still_requires_the_active_fixture_sources(monkeypatch, local_tokenizer, with_builtin):
    async def observer(request, rows):
        return spend_page(request, rows)

    async def pack_run(*, attribution, prompt="Return OK."):
        with monkeypatch.context() as env:
            cfg, _, _ = setup(env, observer)
            immediate_spend_polls(env, cfg)
            checks = (["access"] if with_builtin else []) + ["correctness"]
            if attribution:
                checks.append("gateway_attribution")
            cfg = RunConfig.model_validate({**cfg.model_dump(), "checks": checks, "default_concurrency": 1,
                "max_requests": 30, "request_source": "request_pack", "request_pack": {
                    "version": 1, "cases": [
                        {"fixture_id": f"case_{i}", "messages": [{"role": "user", "content": prompt}],
                         "scorer": "exact", "expected": "OK"} for i in range(2)]},
                "limits": {**cfg.limits.model_dump(), "min_samples": 2, "regression_percent": 100000}})
            report = await run_suite(cfg)
            assert report.overall == "pass"
            return report

    plain, mixed = await pack_run(attribution=False), await pack_run(attribution=True)
    builtin_key = "builtin_sha256"
    assert (plain.manifest["fixture_pack"][builtin_key] is not None) == with_builtin
    assert mixed.manifest["fixture_pack"][builtin_key]
    for current, baseline in ((mixed, plain), (plain, mixed)):
        target = compare_baseline(current, baseline).baseline["targets"][0]
        assert target["reasons"] == []
        assert any(row["check"] == "correctness" and row["status"] == "pass" for row in target["comparisons"])

    changed_pack = await pack_run(attribution=True, prompt="Return exactly OK.")
    assert "Request pack identity differs." in compare_baseline(plain, changed_pack).baseline["targets"][0]["reasons"]
    if with_builtin:
        mixed.manifest["fixture_pack"][builtin_key] = "a" * 64
        assert "Fixture pack identity differs." in compare_baseline(plain, mixed).baseline["targets"][0]["reasons"]


@pytest.mark.parametrize("fault", [None, "duplicate_page_member", "duplicate_metadata_member"])
async def test_duplicate_spend_json_cannot_override_attribution_evidence(monkeypatch, local_tokenizer, fault):
    async def observer(request, rows):
        if fault == "duplicate_page_member":
            page = spend_page(request, rows)
            return httpx.Response(200, content=b'{"total_is_capped":true,"total_is_capped":false,' + page.content[1:])
        encoded = []
        for row in rows:
            metadata = json.dumps(row["metadata"])
            if fault == "duplicate_metadata_member":
                metadata = ('{"spend_logs_metadata":{"giraffe_run":"wrong-run","giraffe_request":"wrong-id"},'
                            + metadata[1:])
            encoded.append({**row, "metadata": metadata})
        return spend_page(request, encoded)

    cfg, _, reads = setup(monkeypatch, observer)
    immediate_spend_polls(monkeypatch, cfg)
    report = await run_suite(cfg)
    check = next(item for item in report.checks if item.id == "gateway_attribution")
    assert len(report.requests) == 2 and all(record.valid for record in report.requests)
    assert len(reads) == check.metrics["collection_attempts"] == 3
    assert check.metrics["planned_requests"] == check.metrics["observed_requests"] == 2
    assert check.status == ("inconclusive" if fault else "pass")
    assert check.metrics["matched_requests"] == (0 if fault else 2)
    if fault == "duplicate_metadata_member":
        assert check.metrics["invalid_rows"] == 2
