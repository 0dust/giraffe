"""Deterministic Prometheus fixture contracts; no inference or socket binding."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from giraffe import telemetry
from giraffe.capabilities import capabilities
from giraffe.models import RunConfig, Target


def settings(profile="vllm-v1", **kwargs):
    target = Target(name="test", url="http://inference/v1", model="test", metrics_profile=profile, **kwargs)
    return target, RunConfig(targets=[target], metrics=True)


def snapshot(body, elapsed, profile="vllm-v1", *, host=False):
    target, config = settings(profile)
    return telemetry.parse(body, target, config, host=host) | {
        "elapsed_seconds": elapsed, "collected_at": f"2026-10-22T00:00:{int(elapsed):02d}+00:00", "phase": "load"}


def counters(replica, total, count):
    return (f'vllm:time_to_first_token_seconds_sum{{replica="{replica}"}} {total}\n'
            f'vllm:time_to_first_token_seconds_count{{replica="{replica}"}} {count}\n')


def test_means_pool_matching_replica_deltas_and_convert_seconds_to_ms():
    first = snapshot(counters("a", 10, 5) + counters("b", 50, 10), 0)
    last = snapshot(counters("a", 12, 7) + counters("b", 62, 14), 3)
    row = telemetry.derive_server_metrics({"snapshots": [first, last]})["server_ttft_mean_ms"]
    assert row["value"] == pytest.approx(14000 / 6)
    assert row["sample_count"] == 6 and row["unit"] == "ms"
    assert row["scope"] == "server-wide" and row["details"]["duration_seconds"] == 3


@pytest.mark.parametrize("last", [counters("a", 1, 1), counters("a", 11, 5),
                                   counters("new", 12, 7),
                                   'vllm:time_to_first_token_seconds_sum{replica="a"} 12\n'
                                   'vllm:time_to_first_token_seconds_count{replica="other"} 7\n'])
def test_reset_zero_count_and_changed_or_mismatched_labels_are_unavailable(last):
    rows = [snapshot(counters("a", 10, 5), 0), snapshot(last, 5)]
    row = telemetry.derive_server_metrics({"snapshots": rows})["server_ttft_mean_ms"]
    assert row["value"] is None and row["state"] == "insufficient_evidence"


@pytest.mark.parametrize("problem", ["stale", "failed", "truncated", "missing"])
def test_stale_failed_missing_and_truncated_samples_do_not_bridge_counter_windows(problem):
    samples = [snapshot(counters("a", 10, 5), 0), snapshot(counters("a", 12, 7), 1),
               snapshot(counters("a", 14, 9), 2)]
    if problem == "stale":
        samples[1]["samples"][0]["stale"] = True
    elif problem == "failed":
        samples[1]["status"] = "connection_failure"
    elif problem == "truncated":
        samples[1]["series_limit_reached"] = True
    else:
        samples[1]["samples"] = []
    row = telemetry.derive_server_metrics({"snapshots": samples})["server_ttft_mean_ms"]
    assert row["value"] is None and row["details"]["excluded_intervals"] == 2


def histogram(prefix, buckets, count, total, replica="a"):
    return "".join(f'{prefix}_bucket{{replica="{replica}",le="{bound}"}} {value}\n'
                   for bound, value in buckets) + (
        f'{prefix}_count{{replica="{replica}"}} {count}\n'
        f'{prefix}_sum{{replica="{replica}"}} {total}\n')


def test_supported_histogram_interpolates_bucket_deltas_instead_of_replica_percentiles():
    name = "vllm:time_to_first_token_seconds"
    first = snapshot(histogram(name, [("1", 0), ("2", 0), ("+Inf", 0)], 0, 0), 0)
    last = snapshot(histogram(name, [("1", 80), ("2", 100), ("+Inf", 100)], 100, 120), 5)
    rows = telemetry.derive_server_metrics({"snapshots": [first, last]})
    assert rows["server_ttft_p95_ms"]["value"] == pytest.approx(1750)
    assert rows["server_ttft_p99_ms"]["value"] == pytest.approx(1950)
    assert rows["server_ttft_p95_ms"]["state"] == "estimated"
    assert rows["server_ttft_p95_ms"]["details"]["bucket_bounds_seconds"] == [1, 2]
    assert "maximum" in rows["server_ttft_p95_ms"]["details"]["approximation"]


@pytest.mark.parametrize("buckets", [[("1", 8), ("+Inf", 10)], [("1", 10)],
                                      [("1", 11), ("2", 10), ("+Inf", 10)]])
def test_histogram_unbounded_quantile_missing_inf_and_nonmonotonic_buckets_are_not_finite_percentiles(buckets):
    name = "vllm:time_to_first_token_seconds"
    first = snapshot(histogram(name, [(b, 0) for b, _ in buckets], 0, 0), 0)
    last = snapshot(histogram(name, buckets, 10, 10), 2)
    row = telemetry.derive_server_metrics({"snapshots": [first, last]})["server_ttft_p95_ms"]
    assert row["value"] is None


def test_tgi_explicit_queue_inference_contract_and_no_guessed_runtime_mapping():
    first = snapshot("tgi_request_queue_duration_sum 2\ntgi_request_queue_duration_count 2\n"
                     "tgi_request_inference_duration_sum 10\ntgi_request_inference_duration_count 2\n", 0, "tgi")
    last = snapshot("tgi_request_queue_duration_sum 3\ntgi_request_queue_duration_count 4\n"
                    "tgi_request_inference_duration_sum 14\ntgi_request_inference_duration_count 4\n", 2, "tgi")
    rows = telemetry.derive_server_metrics({"snapshots": [first, last]})
    assert rows["server_queue_mean_ms"]["value"] == 500
    assert rows["server_inference_mean_ms"]["value"] == 2000
    assert rows["server_queue_p95_ms"]["value"] is None
    assert snapshot("tgi_request_queue_duration_sum 2\n", 0)["samples"] == []


def test_token_rates_use_actual_duration_and_cache_ratio_pairs_token_scopes():
    first = snapshot("vllm:prompt_tokens_total 100\nvllm:generation_tokens_total 50\n"
                     "vllm:prefix_cache_hits_total 10\nvllm:prefix_cache_queries_total 20\n", 2)
    last = snapshot("vllm:prompt_tokens_total 200\nvllm:generation_tokens_total 90\n"
                    "vllm:prefix_cache_hits_total 25\nvllm:prefix_cache_queries_total 50\n", 6)
    rows = telemetry.derive_server_metrics({"snapshots": [first, last]})
    assert rows["server_prompt_tokens_per_second"]["value"] == 25
    assert rows["server_generated_tokens_per_second"]["value"] == 10
    assert rows["prefix_cache_hit_ratio"]["value"] == .5


def test_host_node_exporter_cpu_and_ram_are_distinct_from_runner_or_cpu_cache():
    def body(idle, user, guest, available):
        return (f'node_cpu_seconds_total{{cpu="0",mode="idle"}} {idle}\n'
                f'node_cpu_seconds_total{{cpu="0",mode="user"}} {user}\n'
                f'node_cpu_seconds_total{{cpu="0",mode="guest"}} {guest}\n'
                'node_memory_MemTotal_bytes 1000\n'
                f'node_memory_MemAvailable_bytes {available}\n')
    first, last = snapshot(body(10, 2, 1, 600), 0, host=True), snapshot(body(16, 6, 2, 400), 5, host=True)
    rows = telemetry.derive_server_metrics({"snapshots": [{"host": first}, {"host": last}]})
    assert rows["host_cpu_percent"]["value"] == 40
    assert rows["host_ram_used_bytes"]["value"] == 600
    assert rows["host_ram_total_bytes"]["unit"] == "bytes"
    first["samples"][0]["stale"] = True
    assert telemetry.derive_server_metrics({"snapshots": [{"host": first}, {"host": last}]})["host_cpu_percent"]["value"] is None
    last["samples"][-1]["stale"] = True
    assert telemetry.derive_server_metrics({"snapshots": [{"host": first}, {"host": last}]})["host_ram_used_bytes"]["value"] is None
    row = telemetry.derive_server_metrics({"snapshots": []})["host_ram_used_bytes"]
    assert row["state"] == "not_collected" and "not configured" in row["reason"]


@pytest.mark.parametrize("url", ["", "ftp://host/metrics", "http://user:password@host/metrics", "http://host/metrics?secret=1", "http://host/#a", "http://host:0/metrics", "http://bad host/metrics"])
def test_host_exporter_url_validation(url):
    with pytest.raises(ValidationError, match="host_metrics_url"):
        settings(host_metrics_url=url)


async def test_host_collection_is_independent_of_runtime_enabled_and_never_forwards_credentials(monkeypatch):
    target, config = settings(host_metrics_url="http://host/metrics", metrics_api_key_env="RUNTIME_SECRET")
    config.metrics = False
    monkeypatch.setenv("RUNTIME_SECRET", "secret-runtime-token")
    actual, requests = httpx.AsyncClient, []
    def reply(request):
        requests.append(request)
        return httpx.Response(200, text="node_memory_MemTotal_bytes 1000\nnode_memory_MemAvailable_bytes 400\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(**kw, transport=httpx.MockTransport(reply)))
    run = SimpleNamespace(config=config, start=time.monotonic(), deadline=time.monotonic()+5,
                          stop=asyncio.Event(), observations={target.name: {}})
    result = await telemetry.scrape(run, target, "load")
    assert result["status"] == "absent_families" and result["host"]["status"] == "available"
    assert len(requests) == 2
    host_request = next(request for request in requests if request.url.host == "host")
    assert "authorization" not in host_request.headers
    assert result["host"]["host_identity"] == "host"
    assert result["host"]["collection_period_seconds"] == config.metrics_interval_seconds
    metadata = capabilities()["telemetry"]["host_metrics_url"]
    assert metadata["field"] == "targets[].host_metrics_url"


def test_summarize_preserves_legacy_rows_and_adds_derived_contract():
    rows = [snapshot(counters("a", 1, 1), 0), snapshot(counters("a", 3, 3), 2)]
    result = telemetry.summarize({"snapshots": rows}, .9)
    assert len(result["series"]) == 2
    assert result["derived"]["server_ttft_mean_ms"]["value"] == 1000


async def test_runner_collects_configured_runtime_and_host_independently_of_gpu_check(monkeypatch):
    from giraffe.runner import _Run, _metrics

    target, config = settings(host_metrics_url="http://host/metrics", metrics_url="http://runtime/metrics")
    config.metrics, config.checks = False, ["serving"]
    actual, requests = httpx.AsyncClient, []
    def reply(request):
        requests.append(request)
        return httpx.Response(200, text="node_memory_MemTotal_bytes 1000\nnode_memory_MemAvailable_bytes 400\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(**kw, transport=httpx.MockTransport(reply)))
    run = _Run(config, None, None)
    await _metrics(run, target, "start")
    await _metrics(run, target, "end")
    assert len(requests) == 4
    assert [request.url.host for request in requests].count("runtime") == 2
    assert [request.url.host for request in requests].count("host") == 2
    rows = run.observations[target.name]["serving_telemetry"]["snapshots"]
    assert len(rows) == 2 and all(row["status"] == "absent_families" for row in rows)
    assert all(row["host"]["status"] == "available" for row in rows)


@pytest.mark.parametrize("cancel_during", ["scrape", "collector"])
async def test_finished_target_stops_sampler_after_consumed_cancellation_without_stopping_peer(
        monkeypatch, cancel_during):
    from giraffe import deployment
    from giraffe.runner import _Run

    first = Target(name="first", url="http://first/v1", model="test",
                   deployment={"collector_url": "http://first/collector"})
    peer = Target(name="peer", url="http://peer/v1", model="test")
    config = RunConfig(targets=[first, peer], metrics_interval_seconds=60)
    run = _Run(config, None, None)
    entered, release_peer, peer_sampled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    consumed = []

    async def consume_cancellation():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            consumed.append(True)

    async def scrape(run, target, phase):
        if target is first and cancel_during == "scrape":
            await consume_cancellation()
        elif target is peer:
            await release_peer.wait()
            peer_sampled.set()
        return {"elapsed_seconds": 1, "phase": phase, "samples": []}

    async def collect(target, *_):
        assert target is first and cancel_during == "collector"
        await consume_cancellation()
        return {"status": "observed"}

    monkeypatch.setattr(telemetry, "scrape", scrape)
    monkeypatch.setattr(deployment, "snapshot", collect)
    stops = [asyncio.Event(), asyncio.Event()]
    tasks = [asyncio.create_task(telemetry.sample_during(run, target, stop_event=stop))
             for target, stop in zip((first, peer), stops)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stops[0].set()
        tasks[0].cancel()
        # Completion cannot wait for the 60-second collection interval.
        done, _ = await asyncio.wait({tasks[0]}, timeout=1)
        assert tasks[0] in done
        assert tasks[0].result() is None and consumed == [True]
        assert not run.stop.is_set() and not stops[1].is_set()
        release_peer.set()
        await asyncio.wait_for(peer_sampled.wait(), 2)
        assert not tasks[1].done()
        assert len(run.observations[peer.name]["serving_telemetry"]["snapshots"]) == 1
        assert len(run.observations[first.name]["serving_telemetry"]["snapshots"]) == 1
        assert len(run.observations[first.name]["deployment_history"]) == (cancel_during == "collector")
    finally:
        for stop, task in zip(stops, tasks):
            stop.set()
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_runtime_support_is_distinct_from_missing_collection():
    missing = telemetry.derive_server_metrics({"snapshots": []})
    assert missing["server_ttft_mean_ms"]["state"] == "not_collected"
    tgi = snapshot("tgi_request_queue_duration_sum 1\ntgi_request_queue_duration_count 1\n", 0, "tgi")
    rows = telemetry.derive_server_metrics({"snapshots": [tgi]})
    assert rows["server_ttft_mean_ms"]["state"] == "not_supported"
    assert rows["server_queue_mean_ms"]["state"] == "insufficient_evidence"


@pytest.mark.parametrize("host", [False, True])
async def test_scrape_decoding_and_parse_do_not_block_inference_loop(monkeypatch, host):
    import threading

    target, config = settings(host_metrics_url="http://host/metrics")
    actual_client, actual_parse = httpx.AsyncClient, telemetry.parse
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    parse_threads = []
    def parse(body, *args, **kwargs):
        parse_threads.append(threading.get_ident())
        entered.set()
        release.wait(1)
        return actual_parse(body, *args, **kwargs)
    monkeypatch.setattr(telemetry, "parse", parse)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200,
            text="node_memory_MemTotal_bytes 1000\nnode_memory_MemAvailable_bytes 400\n"))))
    run = SimpleNamespace(config=config, start=time.monotonic(), deadline=time.monotonic() + 5,
                          stop=asyncio.Event(), observations={target.name: {}})
    scraper = telemetry._scrape_host if host else telemetry._scrape_runtime
    task = asyncio.create_task(scraper(run, target, "load"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # Request I/O must still resume while parsing is held on the executor.
        resumed = False
        async def inference_read():
            nonlocal resumed
            await asyncio.sleep(0)
            resumed = True
        await asyncio.wait_for(inference_read(), timeout=0.025)
        assert resumed and not task.done()
        assert parse_threads and all(identity != loop_thread for identity in parse_threads)
    finally:
        release.set()
        await task


def test_optional_series_overflow_does_not_erase_existing_serving_counter_intervals():
    from tests.test_gpu_reliability import combined_snapshot

    first, _ = combined_snapshot(elapsed=0)
    last, _ = combined_snapshot(elapsed=2)
    assert first["series_limit_reached"]
    assert all(f.startswith("spec_decode_") for f in first["truncated_families"])
    result = telemetry.derive_server_metrics({"snapshots": [first, last]})
    assert result["server_ttft_mean_ms"]["value"] == 500
    assert result["server_ttft_mean_ms"]["details"]["excluded_intervals"] == 0


def mtplx_body(latest):
    return json.dumps({"latest": latest, "recent": [], "tool_parse_counters": {}})


@pytest.mark.parametrize("profile", ["unknown", "mtplx"])
def test_mtplx_latest_completed_request_maps_distinct_gauges_without_private_fields(profile):
    latest = {"ttft_s": .2, "request_elapsed_s": 2, "decode_tok_s": 50,
              "prefill_tok_s": 100, "prompt_tokens": 20, "completion_tokens": 40,
              "cached_tokens": 5, "active_memory_bytes": 999, "request_id": "private-id",
              "request_last_user_preview": "private-prompt"}
    snap = snapshot(mtplx_body(latest), 0, profile)
    assert snap["status"] == "available" and snap["profile"] == "mtplx"
    values = {s["family"]: s for s in snap["samples"]}
    assert values["mtplx_latest_ttft_seconds"]["value"] == .2
    assert values["mtplx_latest_decode_tokens_per_second"]["unit"] == "tokens/s"
    assert values["mtplx_latest_completion_tokens"]["kind"] == "gauge"
    assert len(values) == 7
    assert {"running", "waiting", "cache_occupancy", "preemptions"} <= set(snap["missing_families"])
    saved = json.dumps(snap)
    assert "private-prompt" not in saved and "private-id" not in saved
    assert "active_memory_bytes" not in saved
    derived = telemetry.derive_server_metrics({"snapshots": [snap, snap]})
    row = derived["mtplx_latest_completion_tokens"]
    assert row["value"] == 40 and row["sample_count"] == 1
    assert "latest completed request" in row["source"]
    assert "may predate" in row["source"]
    for key in ("server_generated_tokens_per_second", "server_ttft_mean_ms", "running", "memory"):
        assert derived[key]["value"] is None and derived[key]["state"] == "not_supported"


@pytest.mark.parametrize("latest", [None, {}, {"ttft_s": None},
    {"ttft_s": True, "request_elapsed_s": "2", "decode_tok_s": -1,
     "prefill_tok_s": float("inf"), "prompt_tokens": float("nan"),
     "completion_tokens": [], "cached_tokens": {}}, {"prompt_tokens": 1.5}])
def test_mtplx_missing_or_invalid_latest_fields_stay_unavailable(latest):
    snap = snapshot(mtplx_body(latest), 0, "unknown")
    assert snap["profile"] == "mtplx" and snap["status"] == "available"
    assert snap["samples"] == []
    row = telemetry.derive_server_metrics({"snapshots": [snap]})["mtplx_latest_ttft_seconds"]
    assert row["value"] is None and row["state"] == "insufficient_evidence"
    assert "two" not in row["action"].lower()


@pytest.mark.parametrize("body", ['{"latest": {"ttft_s": 1}}', '{"latest": [], "recent": [], "tool_parse_counters": {}}',
                                      '{"latest": null, "recent": {}, "tool_parse_counters": {}}', '{broken'])
def test_unrelated_or_malformed_json_does_not_invent_mtplx(body):
    snap = snapshot(body, 0, "unknown")
    assert snap["profile"] == "unknown" and snap["status"] == "unsupported_format"
    assert snap["samples"] == []


def test_mtplx_final_snapshot_missing_failure_and_source_age_do_not_reuse_old_values(monkeypatch):
    monkeypatch.setattr(telemetry.time, "time", lambda: 1000)
    first = snapshot(mtplx_body({"ttft_s": .2, "completed_at_s": 999}), 0, "unknown")
    assert first["samples"][0]["age_seconds"] == 1
    assert first["samples"][0]["sample_timestamp_ms"] == 999000
    stale = snapshot(mtplx_body({"ttft_s": .3, "completed_at_s": 1}), 2, "unknown")
    assert stale["status"] == "stale_samples"
    for last in (snapshot(mtplx_body(None), 2, "unknown"), stale,
                 {"status": "connection_failure", "profile": "mtplx", "samples": []}):
        row = telemetry.derive_server_metrics({"snapshots": [first, last]})["mtplx_latest_ttft_seconds"]
        assert row["value"] is None and row["sample_count"] == 0
    unknown = snapshot(mtplx_body({"ttft_s": .3}), 2, "unknown")
    assert "unknown" in unknown["samples"][0]["freshness"]


@pytest.mark.parametrize("profile", ["unknown", "mtplx"])
@pytest.mark.parametrize("completed_at", [
    "private-timestamp", "999", True, {"value": "private-timestamp"}, -1, -.5,
    float("nan"), float("inf"), float("-inf"), 10 ** 400, 1e308,
], ids=["malformed", "numeric-string", "boolean", "object", "negative-int", "negative-float",
        "nan", "infinity", "negative-infinity", "number-overflow", "milliseconds-overflow"])
def test_mtplx_invalid_completion_timestamp_withholds_all_measurements(profile, completed_at, monkeypatch):
    monkeypatch.setattr(telemetry.time, "time", lambda: 1000)
    flags = {"stream_error": False, "request_cancelled": False}
    receipt = {"ttft_s": .2, "request_elapsed_s": 2, "decode_tok_s": 50,
               "prefill_tok_s": 100, "prompt_tokens": 20, "completion_tokens": 40,
               "cached_tokens": 5, "completed_at_s": completed_at,
               "request_id": "private-id", "request_last_user_preview": "private-prompt", **flags}
    first = snapshot(mtplx_body({"ttft_s": .1, "completed_at_s": 999}), 0, profile)
    last = snapshot(mtplx_body(receipt), 1, profile)
    assert last["status"] == "available" and last["samples"] == []
    assert last["latest_completed_at_s"] is None
    assert last["latest_outcome"] == "completed" and last["latest_outcome_flags"] == flags
    assert "invalid completion timestamp" in last["reason"]
    rows = telemetry.derive_server_metrics({"snapshots": [first, last]})
    for family, _, _ in telemetry.MTPLX.values():
        row = rows[family]
        assert row["value"] is None and row["state"] == "insufficient_evidence"
        assert row["sample_count"] == 0
        assert "invalid completion timestamp" in row["reason"]
    saved = json.dumps({"snapshots": [first, last], "derived": rows}, allow_nan=False)
    assert not any(private in saved for private in ("private-timestamp", "private-id", "private-prompt"))


@pytest.mark.parametrize("timestamp", [{}, {"completed_at_s": None}], ids=["absent", "null"])
def test_mtplx_missing_completion_timestamp_preserves_unknown_freshness(timestamp):
    snap = snapshot(mtplx_body({"ttft_s": .2, **timestamp}), 0, "unknown")
    assert snap["status"] == "available" and snap["latest_completed_at_s"] is None
    assert snap["samples"][0]["sample_timestamp_ms"] is None
    assert snap["samples"][0]["age_seconds"] is None
    assert "unknown" in snap["samples"][0]["freshness"]
    row = telemetry.derive_server_metrics({"snapshots": [snap]})["mtplx_latest_ttft_seconds"]
    assert row["value"] == .2 and row["state"] == "measured"


async def test_mtplx_invalid_timestamp_is_withheld_in_saved_report_and_later_success_keeps_polling(monkeypatch, tmp_path):
    from giraffe.cli import load_report
    from giraffe.reporting import write_report
    from tests.test_performance import record, report, summary

    target, config = settings("unknown")
    config.metrics = False
    receipts = iter([None, {"ttft_s": .1}, {"ttft_s": .2, "completed_at_s": "private-timestamp"},
                     {"ttft_s": .3}])
    actual, requests = httpx.AsyncClient, []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, text=mtplx_body(next(receipts)))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(**kw, transport=httpx.MockTransport(reply)))
    run = SimpleNamespace(config=config, start=time.monotonic(), deadline=time.monotonic()+5,
                          stop=asyncio.Event(), observations={target.name: {}})
    snaps = [await telemetry.scrape(run, target, phase) for phase in ("idle", "success", "invalid")]
    value = report([record()], observations={"targets": {"t": {"serving_telemetry": {"snapshots": snaps}}}})
    value.manifest["config"]["targets"][0]["url"] = "http://inference/v1"
    path, html_path = write_report(value, tmp_path, html=True)
    rows = {row["id"]: row for row in summary(load_report(path))["sections"]["server"]}
    row = rows["mtplx_latest_ttft_seconds"]
    assert row["value"] is None and row["state"] == "insufficient_evidence"
    assert "invalid completion timestamp" in row["reason"] and row["reason"] in html_path.read_text()
    assert "private-timestamp" not in path.read_text() + html_path.read_text()
    final = await telemetry.scrape(run, target, "recovered")
    assert len(requests) == 4 and final["status"] == "available"
    row = telemetry.derive_server_metrics({"snapshots": snaps + [final]})["mtplx_latest_ttft_seconds"]
    assert row["value"] == .3


async def test_mtplx_idle_discovery_keeps_polling_without_forwarding_inference_credentials(monkeypatch):
    target, config = settings("unknown", api_key_env="INFERENCE_ONLY")
    config.metrics = False
    monkeypatch.setenv("INFERENCE_ONLY", "private-inference-key")
    actual, requests = httpx.AsyncClient, []
    def reply(request):
        requests.append(request)
        return httpx.Response(200, text=mtplx_body(None if len(requests) == 1 else {"ttft_s": .2}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(**kw, transport=httpx.MockTransport(reply)))
    run = SimpleNamespace(config=config, start=time.monotonic(), deadline=time.monotonic()+5,
                          stop=asyncio.Event(), observations={target.name: {}})
    first = await telemetry.scrape(run, target, "start")
    last = await telemetry.scrape(run, target, "end")
    assert len(requests) == 2 and all(r.url.path == "/metrics" for r in requests)
    assert all("authorization" not in r.headers for r in requests)
    assert first["status"] == "available" and first["samples"] == []
    assert last["profile"] == "mtplx" and last["samples"][0]["value"] == .2


def test_mtplx_latest_values_are_visible_server_observations_in_saved_html(tmp_path):
    from giraffe.cli import load_report
    from giraffe.reporting import write_report
    from tests.test_performance import record, report, summary

    snap = snapshot(mtplx_body({"ttft_s": .2, "completion_tokens": 40,
                               "request_last_user_preview": "private-prompt"}), 0, "unknown")
    value = report([record()], observations={"targets": {"t": {"serving_telemetry": {"snapshots": [snap]}}}})
    value.manifest["config"]["targets"][0]["url"] = "http://inference/v1"
    path, html_path = write_report(value, tmp_path, html=True)
    rows = {r["id"]: r for r in summary(load_report(path))["sections"]["server"]}
    row = rows["mtplx_latest_ttft_seconds"]
    assert row["value"] == .2 and row["sample_count"] == row["eligible_count"] == 1
    assert "latest completed request" in row["source"]
    assert "not linked" in row["source"]
    assert rows["mtplx_latest_prompt_tokens"]["value"] is None
    assert "MTPLX latest completed request TTFT" in html_path.read_text()
    assert "private-prompt" not in path.read_text() + html_path.read_text()


def test_mtplx_recent_records_do_not_fill_missing_latest_and_explicit_profiles_win():
    body = json.dumps({"latest": None, "recent": [{"ttft_s": .2}], "tool_parse_counters": {}})
    assert snapshot(body, 0, "unknown")["samples"] == []
    assert snapshot(body, 0, "vllm-v1")["profile"] == "vllm-v1"
    assert snapshot("vllm:num_requests_running 2\n", 0, "mtplx")["status"] == "unsupported_format"


def test_mtplx_series_limit_preserves_only_complete_fields():
    target, config = settings("mtplx")
    config.metrics_max_series = 1
    snap = telemetry.parse(mtplx_body({"ttft_s": 0, "completion_tokens": 40}), target, config)
    assert len(snap["samples"]) == 1 and snap["samples"][0]["value"] == 0
    assert snap["series_limit_reached"]
    assert snap["truncated_families"] == ["mtplx_latest_completion_tokens"]
    rows = telemetry.derive_server_metrics({"snapshots": [snap]})
    assert rows["mtplx_latest_ttft_seconds"]["value"] == 0
    assert rows["mtplx_latest_completion_tokens"]["value"] is None


@pytest.mark.parametrize("flags,outcome", [
    ({"stream_error": True, "request_cancelled": False}, "failed"),
    ({"request_cancelled": True}, "cancelled"),
    ({"stream_cancelled_by_client": True}, "cancelled"),
    ({"memory_refusal": True}, "failed"),
    ({"stream_error": True, "stream_cancelled_by_client": True}, "failed"),
])
def test_mtplx_partial_receipts_keep_outcome_but_withhold_completed_request_values(flags, outcome):
    # MTPLX v2.12.3 error receipts divide partial tokens by total request time.
    receipt = {"prompt_tokens": 20, "completion_tokens": 5, "request_elapsed_s": 10,
               "decode_tok_s": .5, "request_id": "private-id", "error_detail": "private-error",
               "request_last_user_preview": "private-prompt", **flags}
    success = snapshot(mtplx_body({"decode_tok_s": 50}), 0, "unknown")
    snap = snapshot(mtplx_body(receipt), 1, "unknown")
    assert snap["status"] == "available" and snap["samples"] == []
    assert snap["latest_outcome"] == outcome
    assert snap["latest_outcome_flags"] == flags
    assert outcome in snap["reason"]
    rows = telemetry.derive_server_metrics({"snapshots": [success, snap]})
    for family, _, _ in telemetry.MTPLX.values():
        row = rows[family]
        assert row["value"] is None and row["state"] == "insufficient_evidence"
        assert row["sample_count"] == 0
        assert row["details"]["latest_outcome"] == outcome
        assert row["details"]["latest_outcome_flags"] == flags
        assert outcome in row["reason"]
    saved = json.dumps({"snapshot": snap, "derived": rows})
    assert not any(private in saved for private in ("private-id", "private-error", "private-prompt"))


@pytest.mark.parametrize("bad_flag", ["private-error", 1, None, {"private": "private-error"}])
def test_mtplx_malformed_outcome_is_unknown_without_saving_its_content(bad_flag):
    snap = snapshot(mtplx_body({"stream_error": bad_flag, "decode_tok_s": .5}), 0, "mtplx")
    assert snap["status"] == "available" and snap["samples"] == []
    assert snap["latest_outcome"] == "unknown"
    assert snap["latest_outcome_flags"] == {}
    assert snap["invalid_outcome_flags"] == ["stream_error"]
    row = telemetry.derive_server_metrics({"snapshots": [snap]})["mtplx_latest_decode_tokens_per_second"]
    assert row["value"] is None and "outcome" in row["reason"]
    assert "private-error" not in json.dumps({"snapshot": snap, "row": row})


def test_mtplx_false_outcome_flags_preserve_successful_measurements():
    flags = {"stream_error": False, "request_cancelled": False,
             "stream_cancelled_by_client": False, "memory_refusal": False}
    snap = snapshot(mtplx_body({"decode_tok_s": 50, **flags}), 0, "unknown")
    assert snap["latest_outcome"] == "completed"
    assert snap["latest_outcome_flags"] == flags
    row = telemetry.derive_server_metrics({"snapshots": [snap]})["mtplx_latest_decode_tokens_per_second"]
    assert row["value"] == 50 and row["state"] == "measured"
    assert row["details"]["latest_outcome_flags"] == flags


async def test_mtplx_error_receipt_is_withheld_in_saved_report_and_later_success_keeps_polling(monkeypatch, tmp_path):
    from giraffe.cli import load_report
    from giraffe.reporting import write_report
    from tests.test_performance import record, report, summary

    target, config = settings("unknown")
    config.metrics = False
    error = {"mode": "stream", "request_cancelled": False, "stream_error": True,
             "stream_cancelled_by_client": False, "memory_refusal": False,
             "prompt_tokens": 20, "completion_tokens": 5, "request_elapsed_s": 10,
             "decode_tok_s": .5, "request_id": "private-id", "session_id": "private-session",
             "error_detail": "private-error", "request_last_user_preview": "private-prompt"}
    receipts = iter([None, {"decode_tok_s": 50}, error, {"decode_tok_s": 60}])
    actual, requests = httpx.AsyncClient, []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, text=mtplx_body(next(receipts)))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual(**kw, transport=httpx.MockTransport(reply)))
    run = SimpleNamespace(config=config, start=time.monotonic(), deadline=time.monotonic()+5,
                          stop=asyncio.Event(), observations={target.name: {}})
    snaps = [await telemetry.scrape(run, target, phase) for phase in ("idle", "success", "error")]
    value = report([record()], observations={"targets": {"t": {"serving_telemetry": {"snapshots": snaps}}}})
    value.manifest["config"]["targets"][0]["url"] = "http://inference/v1"
    value.manifest["config"]["targets"][0]["model"] = "test"
    path, html_path = write_report(value, tmp_path, html=True)
    rows = {row["id"]: row for row in summary(load_report(path))["sections"]["server"]}
    row = rows["mtplx_latest_decode_tokens_per_second"]
    assert row["value"] is None and row["state"] == "insufficient_evidence"
    assert row["details"]["latest_outcome_flags"]["stream_error"] is True
    assert "failed" in row["reason"] and row["reason"] in html_path.read_text()
    saved = path.read_text() + html_path.read_text()
    assert not any(private in saved for private in
                   ("private-id", "private-session", "private-error", "private-prompt"))
    final = await telemetry.scrape(run, target, "recovered")
    assert len(requests) == 4 and final["status"] == "available"
    row = telemetry.derive_server_metrics({"snapshots": snaps + [final]})["mtplx_latest_decode_tokens_per_second"]
    assert row["value"] == 60
