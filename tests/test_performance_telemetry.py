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
    assert result["status"] == "not_enabled" and result["host"]["status"] == "available"
    assert len(requests) == 1 and "authorization" not in requests[0].headers
    assert result["host"]["host_identity"] == "host"
    assert result["host"]["collection_period_seconds"] == config.metrics_interval_seconds
    metadata = capabilities()["telemetry"]["host_metrics_url"]
    assert metadata["field"] == "targets[].host_metrics_url"


def test_summarize_preserves_legacy_rows_and_adds_derived_contract():
    rows = [snapshot(counters("a", 1, 1), 0), snapshot(counters("a", 3, 3), 2)]
    result = telemetry.summarize({"snapshots": rows}, .9)
    assert len(result["series"]) == 2
    assert result["derived"]["server_ttft_mean_ms"]["value"] == 1000


async def test_runner_host_sampling_does_not_enable_deselected_runtime_exporter(monkeypatch):
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
    assert len(requests) == 2 and all(request.url.host == "host" for request in requests)
    rows = run.observations[target.name]["serving_telemetry"]["snapshots"]
    assert len(rows) == 2 and all(row["status"] == "not_enabled" for row in rows)
    assert all(row["host"]["status"] == "available" for row in rows)


def test_runtime_support_is_distinct_from_missing_collection():
    missing = telemetry.derive_server_metrics({"snapshots": []})
    assert missing["server_ttft_mean_ms"]["state"] == "not_collected"
    tgi = snapshot("tgi_request_queue_duration_sum 1\ntgi_request_queue_duration_count 1\n", 0, "tgi")
    rows = telemetry.derive_server_metrics({"snapshots": [tgi]})
    assert rows["server_ttft_mean_ms"]["state"] == "not_supported"
    assert rows["server_queue_mean_ms"]["state"] == "insufficient_evidence"
