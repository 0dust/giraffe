"""Arrival pressure and upgrade decisions through real loopback HTTP sockets."""

import asyncio

from giraffe.models import Limits, RunConfig, Target, TrafficConfig
from giraffe.reporting import compare_baseline
from giraffe.runner import run_suite
from tests.fake_endpoint import FakeEndpoint


def traffic_config(endpoint, **overrides):
    values = {
        "targets": [Target(name="service", url=endpoint.url, model="fake-model")],
        "checks": ["capacity"],
        "concurrency": 32,
        "max_requests": 100,
        "max_duration_seconds": 10,
        "request_timeout_seconds": 3,
        "stop_after_errors": 100,
        "traffic": TrafficConfig(
            rates=[10, 50], duration_seconds=.6, drain_timeout_seconds=3,
            mix={"short": 1, "long_input": 0, "long_output": 0},
        ),
        "limits": Limits(latency_ms=250, min_samples=5),
    }
    return RunConfig(**(values | overrides))


def capacity(report):
    return next(check for check in report.checks if check.id == "capacity")


async def test_arrivals_reach_http_server_before_prior_requests_complete():
    with FakeEndpoint(first_token_delay=.25, chunk_delay=0) as endpoint:
        config = traffic_config(
            endpoint,
            traffic=TrafficConfig(
                rates=[20], duration_seconds=.3,
                mix={"short": 1, "long_input": 0, "long_output": 0},
            ),
            limits=Limits(latency_ms=1000, min_samples=5),
        )
        report = await run_suite(config)
        assert endpoint.request_started_at[2] < min(endpoint.request_finished_at), (
            "The third arrival must reach the server while the first request is still running"
        )
        assert endpoint.peak_active >= 3
        assert len(endpoint.requests) == 6, "A capacity-only run must not send hidden warmups"

    stage = capacity(report).metrics["traffic_stages"][0]
    assert stage["scheduled"] == stage["started"] == stage["good"] == 6
    assert stage["accepted"] and stage["dropped_local"] == 0
    assert all(record.traffic_stage == 0 and record.score for record in report.requests)


async def test_mixed_http_workload_scores_short_long_input_and_long_output():
    with FakeEndpoint(first_token_delay=.001, chunk_delay=0) as endpoint:
        config = traffic_config(
            endpoint,
            traffic=TrafficConfig(
                rates=[30], duration_seconds=1, seed=42,
                long_input_chars=512, long_output_words=16,
            ),
            limits=Limits(latency_ms=1000, min_samples=5),
        )
        report = await run_suite(config)

    assert {record.traffic_class for record in report.requests} == {
        "short", "long_input", "long_output",
    }
    assert all(record.score is True for record in report.requests)
    assert max(record.input_chars for record in report.requests) >= 512
    assert max(record.output_tokens for record in report.requests) >= 16
    assert capacity(report).metrics["traffic_stages"][0]["good"] == 30


async def test_slower_upgrade_reduces_accepted_rate_as_http_queue_grows():
    # One serving slot models finite endpoint capacity. The HTTP acceptor remains
    # concurrent, so waiting occurs on the service, not inside Giraffe.
    with FakeEndpoint(first_token_delay=.002, chunk_delay=0, service_slots=1) as endpoint:
        config = traffic_config(endpoint)
        baseline = await run_suite(config)
        endpoint.first_token_delay = .05
        candidate = await run_suite(config)

    old = capacity(baseline).metrics
    current = capacity(candidate).metrics
    assert old["highest_acceptable_rate_rps"] == 50
    assert current["highest_acceptable_rate_rps"] == 10
    low, high = current["traffic_stages"]
    assert low["accepted"] and high["status"] == "fail"
    assert high["fully_offered"] and high["dropped_local"] == high["dropped_late"] == 0
    assert high["arrival_latency_p95_ms"] > low["arrival_latency_p95_ms"] * 3
    assert high["goodput_rps"] < high["achieved_rps"]
    assert all(record.valid and record.score is True for record in candidate.requests), (
        "The injected upgrade changes service capacity, not HTTP success or answer correctness"
    )
    compared = compare_baseline(candidate, baseline)
    assert compared.baseline["status"] == "fail"
    assert compared.overall == "fail"


async def test_stop_during_http_arrivals_preserves_partial_accounting():
    stop = asyncio.Event()

    def progress(event):
        if event["event"] == "request_started" and event["attempted"] >= 3:
            stop.set()

    with FakeEndpoint(first_token_delay=.3, chunk_delay=0) as endpoint:
        config = traffic_config(
            endpoint,
            traffic=TrafficConfig(rates=[20], duration_seconds=2),
        )
        report = await asyncio.wait_for(run_suite(config, progress=progress, stop_event=stop), 3)

    assert report.abort_reason == "cancelled by user"
    stage = capacity(report).metrics["traffic_stages"][0]
    assert stage["scheduled"] == (
        stage["started"] + stage["dropped_local"] + stage["dropped_late"] + stage["not_offered"]
    )
    assert stage["not_offered"] > 0 and not stage["accepted"]
    assert len(report.requests) == stage["started"] <= 3
