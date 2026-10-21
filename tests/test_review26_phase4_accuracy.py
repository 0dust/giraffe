"""Seeded end-to-end checks of ranges learned from actual runner observations."""

import asyncio
import math
from importlib import import_module
import random
import time
import statistics
import sys
from types import SimpleNamespace

import pytest

from giraffe import runner
from giraffe.latency import latency_summary
from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_runner import FakeClient


TARGET = {"name": "service", "url": "http://endpoint.invalid/v1", "model": "model"}


def recording_client(seed, *, milliseconds=2, sigma=0):
    """Preserve the real runner, batches and recorded request settings; use no sockets."""
    random_source = random.Random(seed)

    class LatencyClient(FakeClient):
        delay = 0
        active = 0
        peak = 0
        closed = []
        scenarios = []

        async def execute(self, spec, **kwargs):
            delay = (random_source.lognormvariate(math.log(milliseconds), sigma)
                     if sigma else milliseconds) / 1000
            started = asyncio.get_running_loop().time()
            await asyncio.sleep(delay)
            record = await super().execute(spec, **kwargs)
            record.elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000
            record.stream = self.config.stream and spec.stream
            return record

    return LatencyClient


async def observed_range(config, *, seed, milliseconds=1, sigma=0):
    """Run -> measured summary -> plan -> independent next run, as in the review."""
    runner.LLMClient = recording_client(seed, milliseconds=milliseconds, sigma=sigma)
    training = config.model_copy(deep=True)
    if "correctness" in training.checks:
        training.test_options["correctness"].samples = 192
    previous = await runner.run_suite(training)
    summary = latency_summary(previous)
    observations = {target["name"]: target["measurements"] for target in summary["targets"]}
    plan = request_plan(config, observations=observations)
    runner.LLMClient = recording_client(seed + 10000, milliseconds=milliseconds, sigma=sigma)
    current = await runner.run_suite(config)
    actual = current.observations["inference_duration_seconds"]
    low, high = plan["estimated_low_seconds"], plan["estimated_high_seconds"]
    return {"seed": seed, "confidence": plan["confidence"], "low": low, "high": high,
            "actual": actual, "hit": low is not None and low <= actual <= high,
            "unknowns": len(plan["unknown_time_components"])}, summary, plan, current


CASES = {
    "gather4": {"targets": [TARGET], "checks": ["correctness"],
                "test_options": {"correctness": {"samples": 128, "concurrency": 4}}},
    "gather8": {"targets": [TARGET], "checks": ["correctness"],
                "test_options": {"correctness": {"samples": 128, "concurrency": 8}}},
    "overlap4": {"targets": [TARGET, {**TARGET, "name": "other", "url": "http://other.invalid/v1"}],
                 "overlap_models": True, "checks": ["correctness"],
                 "test_options": {"correctness": {"samples": 128, "concurrency": 4}}},
}


class VirtualLoop(asyncio.SelectorEventLoop):
    """Run the real scheduling/batching code without spending seconds asleep."""

    now = 0

    def time(self):
        return self.now

    def _run_once(self):
        if not self._ready and self._scheduled:
            self.now = max(self.now, min((handle._when for handle in self._scheduled
                                          if not handle.cancelled()), default=self.now))
        super()._run_once()


def run_virtual(monkeypatch, coroutine):
    """Keep elapsed records and runner deadlines on the same deterministic clock."""
    for name in ("client", "traffic", "workloads", "concurrent_load", "deployment", "telemetry", "timing"):
        import_module("giraffe." + name)
    with asyncio.Runner(loop_factory=VirtualLoop) as execution:
        loop = execution.get_loop()
        clock = SimpleNamespace(**{name: getattr(time, name) for name in dir(time)
                                   if not name.startswith("_")})
        clock.monotonic = clock.perf_counter = loop.time
        clock.monotonic_ns = clock.perf_counter_ns = lambda: int(loop.time() * 1e9)
        for name, module in list(sys.modules.items()):
            if ((name.startswith("giraffe.") and name != "giraffe.resources")
                    or name == "tests.test_runner") and getattr(module, "time", None) is time:
                monkeypatch.setattr(module, "time", clock)
            if name.startswith("giraffe.") and getattr(module, "monotonic", None) is time.monotonic:
                monkeypatch.setattr(module, "monotonic", loop.time)
        return execution.run(coroutine)


@pytest.mark.parametrize("case", CASES)
def test_seeded_variable_batch_ranges_cover_independent_runs(monkeypatch, case):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    config = RunConfig.model_validate(CASES[case])

    async def repeat():
        results = []
        for seed in range(20):
            result, summary, plan, _ = await observed_range(config, seed=seed, sigma=.6)
            assert result["unknowns"] == 0
            if result["confidence"] == "low":
                assert any("too wide for medium" in reason for reason in
                           plan["confidence_details"]["insufficient_observations"])
            else:
                assert result["confidence"] == "medium", plan["confidence_details"]
            measurements = [entry for target in summary["targets"] for entry in target["measurements"]]
            assert measurements
            assert all(entry["p50_ms"] <= entry["p90_ms"] <= entry["p99_ms"] <= entry["max_ms"]
                       for entry in measurements)
            assert all(len(entry["quantiles_ms"]) == 21 for entry in measurements)
            assert all(component["concurrency"] >= 4 for component in plan["time_components"])
            results.append(result)
        assert sum(row["confidence"] == "medium" for row in results) >= 18, results
        assert sum(result["hit"] for result in results) >= math.ceil(.9 * len(results)), results
        assert statistics.median(row["high"] / row["actual"] for row in results) <= 1.6, results
        assert statistics.median(row["low"] / row["actual"] for row in results) >= .6, results

    run_virtual(monkeypatch, repeat())


@pytest.mark.parametrize("sigma", [0, .6])
async def test_seeded_high_confidence_open_loop_ranges(monkeypatch, sigma):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    config = RunConfig.model_validate({
        "targets": [TARGET], "checks": ["capacity"], "request_timeout_seconds": .004,
        "traffic": {"rates": [200], "duration_seconds": .04, "drain_timeout_seconds": .004},
    })
    results = []
    for seed in range(20 if sigma else 5):
        result, _, _, _ = await observed_range(config, seed=seed, milliseconds=1, sigma=sigma)
        assert result["unknowns"] == 0
        assert result["confidence"] == "high"
        results.append(result)
    assert sum(result["hit"] for result in results) >= math.ceil(.9 * len(results)), results
    assert statistics.median(row["high"] / row["actual"] for row in results) <= 1.6, results
    assert statistics.median(row["low"] / row["actual"] for row in results) >= .6, results


@pytest.mark.parametrize("samples", [6, 40, 100])
async def test_sparse_observed_cohorts_cannot_claim_medium(monkeypatch, samples):
    monkeypatch.setattr(runner, "LLMClient", recording_client(71))
    configured = RunConfig(targets=[TARGET], checks=["correctness"],
                           test_options={"correctness": {"samples": samples, "concurrency": 4}})
    previous = await runner.run_suite(configured)
    summary = latency_summary(previous)
    plan = request_plan(configured, observations={TARGET["name"]: summary["targets"][0]["measurements"]})
    assert plan["confidence"] == "low"
    assert "150" in " ".join(plan["confidence_details"]["insufficient_observations"])


def test_local_batch_overhead_cannot_claim_medium(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", recording_client(11, milliseconds=1))
    configured = RunConfig(targets=[TARGET], checks=["correctness"],
                           test_options={"correctness": {"samples": 192, "concurrency": 1}})

    async def scenario():
        def bookkeeping(event):
            if event["event"] == "request_completed":
                asyncio.get_running_loop().now += .001

        saved = await runner.run_suite(configured, progress=bookkeeping)
        summary = latency_summary(saved)
        measurement, = summary["targets"][0]["measurements"]
        assert measurement["n"] == 192
        assert measurement["p50_ms"] == pytest.approx(1)
        assert measurement["batch_timing_coverage"] < .99
        plan = request_plan(configured, observations={TARGET["name"]: [measurement]})
        assert plan["confidence"] == "low"
        assert "99%" in " ".join(plan["confidence_details"]["insufficient_observations"])

    run_virtual(monkeypatch, scenario())
