"""Protocol and lifecycle regressions found by independent gateway review."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time

import httpx
import pytest

from giraffe.client import LLMClient
from giraffe.gateway_attribution import execute, result
from giraffe.models import RequestSpec
from giraffe.runner import _Run, _checks
from tests.test_gateway_attribution import config, spend_page


def setup(monkeypatch, observer, *, rejected=None):
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
            return httpx.Response(401, json={"error": {"type": "expired_key"}})
        if "x-litellm-spend-logs-metadata" in request.headers:
            rows.append({"request_id": str(len(rows)), "session_id": request.headers["x-litellm-trace-id"],
                "metadata": {"spend_logs_metadata": json.loads(request.headers["x-litellm-spend-logs-metadata"])},
                "api_key": hashlib.sha256(key.encode()).hexdigest(), "prompt_tokens": 3,
                "completion_tokens": 5, "total_tokens": 8, "status": "success"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return cfg, run, reads


async def collect(cfg, run):
    async with LLMClient(cfg.targets[0], cfg) as client:
        await execute(run, cfg.targets[0], client)
    return result(run, cfg.targets[0])


async def test_litellm_required_utc_dates_cover_probes_and_are_stable_on_all_pages(monkeypatch):
    monkeypatch.setattr("giraffe.gateway_attribution.PAGE_SIZE", 1)
    # LiteLLM fca3d4f: public v2 requires both bounds in UTC second precision.
    async def observer(request, rows):
        if not all(request.url.params.get(key) for key in ("start_date", "end_date")):
            return httpx.Response(400)
        page = int(request.url.params["page"])
        return spend_page(request, rows[page-1:page], total=len(rows))
    cfg, run, reads = setup(monkeypatch, observer)
    before = datetime.now(timezone.utc)
    check = await collect(cfg, run)
    after = datetime.now(timezone.utc)
    assert check.metrics["state"] == "verified"
    assert len(reads) == 2
    bounds = {(r.url.params["start_date"], r.url.params["end_date"]) for r in reads}
    assert len(bounds) == 1
    start, end = [datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) for value in bounds.pop()]
    assert start <= before <= after < end
    assert (end - start).total_seconds() <= cfg.max_duration_seconds + cfg.gateway_attribution.settle_seconds + 2


@pytest.mark.parametrize("status,state", [(403, "unavailable"), (404, "unsupported"), (405, "unsupported"), (501, "unsupported")])
async def test_observer_denial_keeps_attempted_identity_coverage(monkeypatch, status, state):
    async def observer(request, rows):
        return httpx.Response(status)
    cfg, run, _ = setup(monkeypatch, observer)
    check = await collect(cfg, run)
    assert check.metrics["state"] == state
    assert check.metrics["planned_requests"] == check.metrics["observed_requests"] == 2
    assert check.metrics["unverified_requests"] == 2
    assert {r["identity"] for r in check.metrics["identities"]} == {"alice", "bob"}
    assert {i for r in check.metrics["identities"] for i in r["request_ids"]} == {r.id for r in run.records}


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
async def test_mixed_full_run_keeps_shared_checks_passing_with_expired_identity(monkeypatch):
    from giraffe.runner import run_suite
    async def observer(request, rows):
        return spend_page(request, rows)
    cfg, _, _ = setup(monkeypatch, observer, rejected="expired")
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
