"""Deterministic Prometheus fixture contracts; no inference or socket binding."""

import asyncio
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
