"""Merge measured cohorts without inventing compatible work or hiding drift."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import random
import statistics

import pytest

from giraffe import runner
from giraffe.latency import comparable_latency, latency_summary, load_observations
from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_run_limits_phase4_latency import record, report, save_summary
from tests.test_review26_phase4_accuracy import recording_client, run_virtual, TARGET


MATCH = dict(family="correctness", concurrency=4, stream=True, output_cap=128, input_bucket="chars:0-255")


def measurement(values, cohort):
    saved = report([record(index, elapsed_ms=value) for index, value in enumerate(values)])
    saved.run_id = cohort
    return latency_summary(saved)["targets"][0]["measurements"][0]


def test_histogram_counts_merge_exactly_and_quantiles_remain_close():
    random_source = random.Random(926)
    values = [[random_source.lognormvariate(math.log(20), .6) for _ in range(50)] for _ in range(3)]
    entries = [measurement(batch, str(index)) for index, batch in enumerate(values)]
    original = deepcopy(entries)
    pooled = comparable_latency({"local": entries}, "local", **MATCH)
    reference = measurement([value for batch in values for value in batch], "all")
    assert pooled["n"] == 150
    assert pooled["run_count"] == 3
    assert pooled["cohorts"] == ["0", "1", "2"]
    assert pooled["histogram_ms"] == reference["histogram_ms"]
    assert pooled["min_ms"] == reference["min_ms"]
    assert pooled["max_ms"] == reference["max_ms"]
    for name in ("p50_ms", "p90_ms", "p99_ms"):
        assert pooled[name] == pytest.approx(reference[name], rel=.025)
    assert entries == original
    assert len(json.dumps(entries[0], separators=(",", ":"))) < 1800


def test_pooling_stops_at_recent_latency_shift_instead_of_cherry_picking_old_runs():
    entries = [measurement([value] * 50, cohort) for value, cohort in
               [(100, "recent1"), (101, "recent2"), (99, "recent3"), (20, "old1"), (100, "old2")]]
    pooled = comparable_latency({"local": entries}, "local", **MATCH)
    assert pooled["n"] == 150
    assert pooled["cohorts"] == ["recent1", "recent2", "recent3"]
    assert pooled["drift_detected"] is True
    assert pooled["p50_ms"] == pytest.approx(100, rel=.022)


@pytest.mark.parametrize("medians", [[122, 98, 78, 62, 50], [50, 62, 78, 98, 122]])
def test_gradual_trend_stops_pooling_even_when_individual_medians_are_uncertain(medians):
    # Broad, small cohorts pass pairwise sampling bounds. Their ordered movement
    # must not be treated as five independent observations of a stable endpoint.
    entries = [measurement([median * math.exp(offset) for offset in (-1, -.6, -.2, .2, .6, 1)], str(index))
               for index, median in enumerate(medians)]
    pooled = comparable_latency({"local": entries}, "local", **MATCH)
    assert pooled["latency_trending"] is True
    assert pooled["drift_detected"] is True
    assert pooled["cohorts"] == ["0", "1", "2", "3"]
    configured = RunConfig(targets=[TARGET], checks=["correctness"],
                           test_options={"correctness": {"max_output_tokens": 128}})
    plan = request_plan(configured, observations={TARGET["name"]: entries})
    assert plan["confidence"] == "low"
    assert any("latency is trending" in note for note in plan["confidence_details"]["insufficient_observations"])


def test_shift_comparisons_use_newest_run_not_the_accumulating_block(monkeypatch):
    from giraffe import latency

    entries = [measurement([100 + index] * 50, str(index)) for index in range(5)]
    compared = []
    original = latency._median_shift

    def compare(current, candidate):
        compared.append((current["cohort"], candidate["cohort"]))
        return original(current, candidate)

    monkeypatch.setattr(latency, "_median_shift", compare)
    assert comparable_latency({"local": entries}, "local", **MATCH)["run_count"] == 5
    assert compared == [("0", str(index)) for index in range(1, 5)]


def test_pooled_median_distance_is_bounded_even_with_large_sampling_uncertainty():
    entries = [measurement([median * math.exp(offset) for offset in (-3, -2, -1, 1, 2, 3)], str(index))
               for index, median in enumerate([100, 40, 40, 40, 40])]
    pooled = comparable_latency({"local": entries}, "local", **MATCH)
    assert max(pooled["p50_ms"], entries[0]["p50_ms"]) / min(pooled["p50_ms"], entries[0]["p50_ms"]) <= 2.5
    assert pooled["run_count"] < 5
    assert pooled["drift_detected"] is True


def test_precisely_measured_pooled_median_has_twenty_percent_distance_floor(monkeypatch):
    from giraffe import latency

    # Isolate the aggregate-distance guard from the individual and trend guards.
    monkeypatch.setattr(latency, "_median_shift", lambda *_: False)
    monkeypatch.setattr(latency, "_median_trend", lambda *_: False)
    entries = [measurement([median] * 50, str(index)) for index, median in enumerate([100, 90, 80, 70, 60])]
    pooled = comparable_latency({"local": entries}, "local", **MATCH)
    assert pooled["cohorts"] == ["0", "1", "2"]
    assert 100 / pooled["p50_ms"] <= 1.2
    assert pooled["drift_detected"] is True


def test_gradual_quality_drift_never_claims_medium(monkeypatch):
    configured = RunConfig(targets=[TARGET], checks=["correctness", "json", "consistency"])

    async def scenario():
        for seed in range(5):
            observed = {TARGET["name"]: []}
            for prior, milliseconds in enumerate([50, 62, 78, 98, 122]):
                monkeypatch.setattr(runner, "LLMClient", recording_client(50000 + seed * 101 + prior,
                                                                         milliseconds=milliseconds, sigma=.6))
                saved = latency_summary(await runner.run_suite(configured))
                observed[TARGET["name"]][:0] = saved["targets"][0]["measurements"]
            plan = request_plan(configured, observations=observed)
            assert plan["confidence"] == "low", plan["confidence_details"]
            assert any("Latency drift detected" in note for note in plan["time_estimate_assumptions"])

    run_virtual(monkeypatch, scenario())


def test_stable_quality_reaches_medium_after_three_runs(monkeypatch):
    configured = RunConfig(targets=[TARGET], checks=["correctness", "json", "consistency"])

    async def scenario():
        observed = {TARGET["name"]: []}
        for prior in range(3):
            monkeypatch.setattr(runner, "LLMClient", recording_client(1234 + prior, milliseconds=20, sigma=.6))
            saved = latency_summary(await runner.run_suite(configured))
            observed[TARGET["name"]][:0] = saved["targets"][0]["measurements"]
        plan = request_plan(configured, observations=observed)
        assert plan["confidence"] == "medium", plan["confidence_details"]
        assert any("based on 3 earlier runs" in note for note in plan["time_estimate_assumptions"])
        monkeypatch.setattr(runner, "LLMClient", recording_client(1234 + 777777, milliseconds=20, sigma=.6))
        actual = (await runner.run_suite(configured)).observations["inference_duration_seconds"]
        assert plan["estimated_low_seconds"] <= actual <= plan["estimated_high_seconds"]

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("recent_runs,status", [(3, "low"), (5, "medium")])
def test_small_post_shift_block_requires_five_stable_runs(recent_runs, status):
    entries = [measurement([150] * 6, str(index)) for index in range(recent_runs)]
    entries.append(measurement([50] * 6, "old"))
    for entry in entries:
        entry["batch_timing_coverage"] = 1
    configured = RunConfig(targets=[TARGET], checks=["correctness"],
                           test_options={"correctness": {"max_output_tokens": 128}})
    plan = request_plan(configured, observations={TARGET["name"]: entries})
    assert plan["confidence"] == status, plan["confidence_details"]
    assert any("Latency drift detected" in note for note in plan["time_estimate_assumptions"])
    if recent_runs == 3:
        assert any("a recent latency shift requires 5 consistent runs or 150 comparable samples" in note
                   for note in plan["confidence_details"]["insufficient_observations"])


def test_old_quantile_summaries_remain_readable_but_are_not_falsely_merged():
    entries = [measurement([10, 20, 30], cohort) for cohort in ("new", "old")]
    for entry in entries:
        del entry["histogram_ms"]
        del entry["min_ms"]
    selected = comparable_latency({"local": entries}, "local", **MATCH)
    assert selected["n"] == 3
    assert selected["cohort"] == "new"
    assert selected["quantiles_ms"] == entries[0]["quantiles_ms"]
    assert "run_count" not in selected


@pytest.mark.parametrize("broken", [None, {}, {"scale": 32, "zero": 0, "buckets": [[1, -1]]},
    {"scale": 32, "zero": 0, "buckets": [[1, 1], [1, 1]]},
    {"scale": 32, "zero": 0, "buckets": [[100000, 3]]}])
def test_malformed_histograms_use_legacy_reading_without_pooling(broken):
    entry = measurement([10, 20, 30], "only")
    entry["histogram_ms"] = broken
    assert comparable_latency({"local": [entry]}, "local", **MATCH)["n"] == 3
    assert "histogram_ms" not in comparable_latency({"local": [entry]}, "local", **MATCH)


def test_saved_lookup_pools_cached_metadata_and_excludes_error_state(tmp_path):
    configured = RunConfig(targets=[{"name": "local", "url": "http://localhost:8000", "model": "test"}], checks=["correctness"])
    for index in range(4):
        current = report([record(i, elapsed_ms=100) for i in range(10)])
        current.finished_at = f"2026-10-20T0{index}:00:00Z"
        path = save_summary(tmp_path, str(index), current)
        if index == 3:
            metadata = json.loads(path.read_text())
            metadata["state"] = "error"
            path.write_text(json.dumps(metadata))
    cache = {}
    observations = load_observations(tmp_path, configured, now=datetime(2026, 10, 21, tzinfo=timezone.utc), cache=cache)
    pooled = comparable_latency(observations, "local", **MATCH)
    assert pooled["n"] == 30
    assert pooled["run_count"] == 3
    assert len(cache) == 4


def test_arrivals_lower_bound_uses_last_offer_plus_minimum_observed_latency():
    configured = RunConfig(targets=[TARGET], checks=["arrivals"], arrivals={"requests": 3, "requests_per_second": 10, "max_pending": 1})
    entry = measurement([1, 10, 20], "run")
    entry.update(family="arrivals", concurrency=4, output_cap=24)
    plan = request_plan(configured, observations={TARGET["name"]: [entry]})
    component, = plan["time_components"]
    assert component["fixed_seconds"] == .2
    assert component["low_seconds"] == pytest.approx(.201)
    assert plan["estimated_low_seconds"] == .201


def test_budget_and_runtime_warning_sentences_are_readable_and_exact():
    configured = RunConfig(targets=[TARGET], checks=["correctness"], max_requests=1)
    warning, = request_plan(configured)["request_warnings"]
    assert warning["msg"] == ("service: incomplete coverage for Answer correctness; the planned requests exceed this endpoint's share of the request limit (Stop after this many requests: 1).")
    configured = RunConfig(targets=[TARGET], checks=["capacity"], max_duration_seconds=1, traffic={"rates": [1], "duration_seconds": 2})
    warning = next(item for item in request_plan(configured)["time_warnings"] if item["kind"] == "incomplete_coverage")
    assert warning["msg"] == ("Incomplete coverage: the traffic time limit (Stop test traffic after) may cut service · Capacity under load. The full traffic timeline including the initial connection allowance needs up to 32 seconds.")
    assert runner._budget_guidance(RunConfig(targets=[TARGET], checks=["concurrency"], max_requests=10), "concurrency") == (
        "Allow the configured Concurrent load sending windows plus calibration and drain; set the request limit (Stop after this many requests) using observed throughput. Duration-based requests have no fixed count.")


@pytest.mark.parametrize("suite", ["quality", "responsiveness"])
def test_ordinary_suites_learn_medium_ranges_from_five_small_runs(monkeypatch, suite):
    checks = (["correctness", "json", "consistency"] if suite == "quality" else
              ["serving", "first_output", "generation", "correctness"])
    configured = RunConfig(targets=[TARGET], checks=checks)

    async def repeat():
        results = []
        for seed in range(20):
            observations = {TARGET["name"]: []}
            for prior in range(5):
                monkeypatch.setattr(runner, "LLMClient", recording_client(10000 + seed * 131 + (prior + 1) * 17,
                                                                         milliseconds=20, sigma=.6))
                trained = await runner.run_suite(configured)
                summary = latency_summary(trained)
                observations[TARGET["name"]][:0] = summary["targets"][0]["measurements"]
            plan = request_plan(configured, observations=observations)
            monkeypatch.setattr(runner, "LLMClient", recording_client(99991 + seed * 97 + 5 * 31,
                                                                     milliseconds=20, sigma=.6))
            actual = (await runner.run_suite(configured)).observations["inference_duration_seconds"]
            assert not plan["unknown_time_components"]
            assert any("based on 5 earlier runs of this endpoint" in note for note in plan["time_estimate_assumptions"])
            results.append((plan["confidence"], plan["estimated_low_seconds"], plan["estimated_high_seconds"], actual))
        medium = [row for row in results if row[0] == "medium"]
        assert len(medium) >= 14, results
        assert sum(low <= actual <= high for _, low, high, actual in medium) >= math.ceil(.9 * len(medium)), medium
        assert statistics.median(high / actual for _, _, high, actual in medium) <= 1.6, medium

    run_virtual(monkeypatch, repeat())


def test_consistency_timing_coverage_excludes_intervening_levels(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", recording_client(19, milliseconds=20, sigma=.6))
    configured = RunConfig(targets=[TARGET], checks=["consistency"])

    async def scenario():
        saved = latency_summary(await runner.run_suite(configured))
        for entry in saved["targets"][0]["measurements"]:
            assert entry["batch_timing_coverage"] == 1

    run_virtual(monkeypatch, scenario())


def test_recent_small_run_has_same_total_weight_as_large_older_run():
    entries = [measurement([21] * 6, "recent"), measurement([20] * 300, "older")]
    pooled = comparable_latency({"local": entries}, "local", **MATCH)
    assert pooled["n"] == 306
    assert pooled["run_count"] == 2
    assert pooled["effective_n"] == pytest.approx(4 / (1 / 6 + 1 / 300))
    assert pooled["effective_n"] < 24
    assert [count for _, count in pooled["histogram_ms"]["buckets"]] == [153, 153]


def test_plan_explains_recent_block_and_detected_drift():
    entries = [measurement([value] * 50, cohort) for value, cohort in
               [(100, "new1"), (101, "new2"), (99, "new3"), (20, "old")]]
    for entry in entries:
        entry["batch_timing_coverage"] = 1
        entry["output_cap"] = 24
    configured = RunConfig(targets=[TARGET], checks=["correctness"])
    plan = request_plan(configured, observations={TARGET["name"]: entries})
    assert plan["confidence"] == "medium"
    assert ("service · Answer correctness: based on 3 earlier runs of this endpoint. "
            "Latency drift detected; only the most recent consistent block is used.") in plan["time_estimate_assumptions"]


def test_small_measured_batch_gaps_are_modeled_after_three_runs(monkeypatch):
    configured = RunConfig(targets=[TARGET], checks=["correctness"],
                           test_options={"correctness": {"concurrency": 1}})
    monkeypatch.setattr(runner, "LLMClient", recording_client(123, milliseconds=20))

    async def scenario():
        import asyncio

        def bookkeeping(event):
            if event["event"] == "request_completed":
                asyncio.get_running_loop().now += .0004

        observations = {TARGET["name"]: []}
        for _ in range(3):
            previous = await runner.run_suite(configured, progress=bookkeeping)
            entry, = latency_summary(previous)["targets"][0]["measurements"]
            assert .95 < entry["batch_timing_coverage"] < .99
            assert entry["batch_overhead_ms"] == pytest.approx(.4)
            observations[TARGET["name"]].insert(0, entry)
        plan = request_plan(configured, observations=observations)
        assert plan["confidence"] == "medium", plan["confidence_details"]
        actual = (await runner.run_suite(configured, progress=bookkeeping)).observations["inference_duration_seconds"]
        assert plan["estimated_low_seconds"] <= actual <= plan["estimated_high_seconds"]
        assert any("based on 3 earlier runs" in note for note in plan["time_estimate_assumptions"])

    run_virtual(monkeypatch, scenario())


def test_duration_row_discloses_pooled_observation_source_in_existing_ui():
    from tests.test_ux_catalogue import browser_logic

    browser_logic(r"""
assert.equal(planRowCount({kind:'duration',requests_per_target:null,estimated_requests_per_target:100,
  estimate_source:'from your recent runs'}),'Duration-based · ~100 (estimate, from your recent runs)');
assert.equal(planRowCount({kind:'duration',requests_per_target:null,estimated_requests_per_target:100}),
  'Duration-based · ~100 (estimate, from your last run)');
assert.equal(planRowCount({kind:'duration',requests_per_target:null}),'Duration-based');
""")


def test_equal_small_cohorts_reach_the_exact_fifteen_sample_boundary():
    pooled = comparable_latency({"local": [measurement([20] * 5, str(index)) for index in range(3)]}, "local", **MATCH)
    assert pooled["effective_n"] == pooled["n"] == 15
    assert pooled["run_count"] == 3
