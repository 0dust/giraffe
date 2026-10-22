"""Round-two scoring, telemetry, planning and traffic regressions (no sockets)."""

import math
import random
from dataclasses import replace

import httpx
import pytest

from giraffe import fixtures, planning, telemetry, traffic, workloads
from giraffe.models import ExpectedToolCall, Limits, RequestSpec, ToolChoiceRule, ToolExpectation
from tests.test_check_intent_load import _evidence
from tests.test_gpu_reliability import snapshot
from tests.test_runner import config
from tests.test_workload_reliability import MemoryEndpoint


@pytest.mark.parametrize("sign", ["+", "-", "−", "–", "—"])
def test_contains_distinguishes_signs_from_ranges(sign):
    assert not fixtures._contains(f"Answer: {sign}42", "42")
    assert fixtures._contains(f"3{sign}5", "5")
    assert fixtures._contains(f"2020{sign}2024", "2024")


@pytest.mark.parametrize("expected", ["K7P4", "Copy this sentence."])
def test_exact_preserves_expected_period_and_rejects_extra_periods(expected):
    assert fixtures.exact_matches(f"**{expected}**", expected)[0]
    assert fixtures.exact_matches(f"`{expected}`", expected)[0]
    assert not fixtures.exact_matches(expected + "..", expected)[0]
    assert fixtures.exact_matches(expected + ".", expected)[0] is (not expected.endswith("."))
    if not expected.endswith("."):
        assert fixtures.exact_matches(f"**{expected}.**", expected)[0]


@pytest.mark.parametrize("answer", ['{"a": 1.', '{"a": -', '1e', '1e+', '{"a": -1.2e-'])
def test_truncated_json_number_is_unscored(answer):
    assert fixtures._incomplete_json(answer)
    spec = RequestSpec(fixture_id="json", scenario="json", check_ids=["json"], messages=[],
                       scorer="json", schema={"type": "object"}, expected={"a": 1})
    record = _evidence((), 0).records[0][1].model_copy(update={"output": answer, "finish_reason": "length"})
    assert fixtures.score_response(spec, record).score is None


@pytest.mark.parametrize("answer", ['{"a": 1.x', '{"a": --', '1eX', '01.', '{"a": nope, "b": 1.'])
def test_invalid_number_is_not_mistaken_for_truncation(answer):
    assert not fixtures._incomplete_json(answer)


def tool_expectation(choice=False):
    return ToolExpectation(tools=[{"type": "function", "function": {"name": "f", "parameters": {
        "type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]}}}],
        expected_calls=[ExpectedToolCall(name="f", arguments={"a": 1})],
        choice=ToolChoiceRule(mode="required") if choice else None)


@pytest.mark.parametrize("choice", [False, True])
@pytest.mark.parametrize("arguments", ["", '{"a": 1.', '{"a": -'])
def test_last_truncated_tool_arguments_are_unverified(choice, arguments):
    record = _evidence((), 0).records[0][1].model_copy(update={"finish_reason": "length", "output": "",
        "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": arguments}}]})
    result = fixtures._score_tool_response(tool_expectation(choice), record)
    assert result.score is None
    assert result.tool_evaluation.calls.status == "inconclusive"


@pytest.mark.parametrize("choice", [False, True])
def test_complete_wrong_truncated_tool_arguments_still_fail(choice):
    record = _evidence((), 0).records[0][1].model_copy(update={"finish_reason": "length", "output": "",
        "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": '{}'}}]})
    assert fixtures._score_tool_response(tool_expectation(choice), record).score is False


@pytest.mark.parametrize("choice", [False, True])
def test_truncated_empty_arguments_are_unverified_even_if_empty_object_satisfies_schema(choice):
    expectation = tool_expectation(choice)
    expectation.tools[0]["function"]["parameters"].pop("required")
    record = _evidence((), 0).records[0][1].model_copy(update={"finish_reason": "length", "output": "",
        "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": " "}}]})
    assert fixtures._score_tool_response(expectation, record).score is None


def gpu_snap(util=10, xid=0, two=False):
    return snapshot({"0": [100, util, 40, 0, xid], **({"1": [100, util, 40, 0, 0]} if two else {})})


def test_serving_only_scrape_does_not_make_gpu_disappear():
    middle = {"status": "absent_families", "samples": []}
    metrics, status, _ = telemetry.assess_gpu([gpu_snap(), middle, gpu_snap(20)], 60)
    assert status == "pass" and not metrics["alerts"]


@pytest.mark.parametrize("misses,final,status", [(1, False, "pass"), (2, False, "fail"), (1, True, "fail")])
def test_gpu_absence_requires_final_or_consecutive_gpu_scrapes(misses, final, status):
    snaps = [gpu_snap(two=True), *[gpu_snap(20) for _ in range(misses)]]
    if not final:
        snaps.append(gpu_snap(30, two=True))
    metrics, actual, _ = telemetry.assess_gpu(snaps, 60)
    assert actual == status
    if status == "pass":
        assert any("absent" in warning.get("reason", "") for warning in metrics["warnings"])


def test_stale_xid_does_not_hide_later_fresh_change():
    mid = gpu_snap(20, 79)
    for sample in mid["samples"]:
        if sample["family"] == "device_errors":
            sample["stale"] = True
    metrics, status, _ = telemetry.assess_gpu([gpu_snap(), mid, gpu_snap(30, 79)], 60)
    assert status == "fail"
    assert any(a["family"] == "device_errors" and a["value"] == 79 for a in metrics["alerts"])


@pytest.mark.parametrize("unknown", ["sentinel", "truncated"])
def test_unknown_initial_xid_is_not_a_proven_new_error(unknown):
    first = gpu_snap(xid=2**63 - 1)
    if unknown == "truncated":
        first["samples"] = [s for s in first["samples"] if s["family"] != "device_errors"]
        first["series_limit_reached"] = True
    metrics, status, _ = telemetry.assess_gpu([first, gpu_snap(20, 79), gpu_snap(30, 79)], 60)
    assert status == "inconclusive"
    assert any("unknown" in w.get("reason", "").lower() for w in metrics["warnings"])


def test_later_truncation_cannot_erase_proven_initial_xid_absence():
    first = gpu_snap()
    first["samples"] = [s for s in first["samples"] if s["family"] != "device_errors"]
    middle = gpu_snap(20)
    middle["samples"] = [s for s in middle["samples"] if s["family"] != "device_errors"]
    middle["series_limit_reached"] = True
    metrics, status, _ = telemetry.assess_gpu([first, middle, gpu_snap(30, 79)], 60)
    assert status == "fail"
    assert any(a["family"] == "device_errors" for a in metrics["alerts"])


def test_poisson_plan_reports_expected_counts_and_seeded_schedule():
    from giraffe.load_test import planned_arrivals, test_points as load_points
    settings = config(checks=["load"], load_test={
        "workloads": {"combinations": {"selected": ["short_short"]}},
        "patterns": {"concurrent": {"enabled": False}, "constant": {"enabled": False},
                     "poisson": {"enabled": True, "seed": 41}, "rates": [.49, .5, .51]},
        "schedule": {"duration_seconds": 5}})
    plan = planning.request_plan(settings)
    assert plan["load_test"]["approximate"] is True
    assert plan["load_test"]["expected_scheduled_requests"] == 7.5
    for point in load_points(settings.load_test):
        assert planned_arrivals(settings.load_test, point) == planned_arrivals(settings.load_test, point)


@pytest.mark.parametrize("budget,expected", [(10, 0), (11, 1)])
def test_recovery_prime_plan_matches_runner_eligibility(budget, expected):
    settings = config(checks=["recovery", "arrivals"], max_requests=budget,
                      arrivals={"requests": 2}, limits=Limits(min_samples=2))
    row = next(r for r in planning.request_plan(settings)["rows"] if r["id"] == "recovery_prime")
    assert row["requests_per_target"] == expected


def test_prefix_admission_reserves_nonce_and_three_chars_per_token():
    assert not workloads.prefix_admitted(240, 24, 140)
    assert workloads.prefix_admitted(240, 24, 160)


@pytest.mark.parametrize("error,status", [("HTTP 400: context length rejection", "inconclusive"), ("HTTP 400", "fail")])
def test_prefix_context_rejection_is_unverified_but_other_errors_fail(error, status):
    from giraffe import runner
    settings = config(checks=["prefix"])
    run = runner._Run(settings, None, None)
    run.records.append(_evidence((), 0).records[0][1].model_copy(update={
        "target": settings.targets[0].name, "check_ids": ["prefix"], "scenario": "prefix_shared",
        "status": "failed", "valid": False, "score": None, "http_status": 400, "error": error}))
    result = next(r for r in workloads.results(run, settings.targets[0], runner._stats,
                  runner._timing_violations, runner._missing_timing) if r.id == "prefix")
    assert result.status == status


async def test_prefix_context_rejection_over_http_is_inconclusive(monkeypatch):
    from giraffe import runner

    class Endpoint(MemoryEndpoint):
        async def handle(self, request):
            self.requests.append(request)
            return httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}})

    endpoint = Endpoint(monkeypatch)
    settings = config(checks=["prefix"], context_limit=16384, stop_after_errors=1,
                      targets=[{"name": "service", "url": endpoint.url, "model": "test"}],
                      prefix={"prefix_chars": 100})
    report = await runner.run_suite(settings)
    result = next(r for r in report.checks if r.id == "prefix")
    assert result.status == "inconclusive" and report.overall != "fail"


async def test_prefix_context_rejection_does_not_poison_serving_health(monkeypatch):
    import json
    import re
    from giraffe import runner

    class Endpoint(MemoryEndpoint):
        async def handle(self, request):
            prompt = json.loads(request.content)["messages"][-1]["content"]
            if re.match(r"[0-9a-f]{32}\n", prompt):
                return httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}})
            return await super().handle(request)

    endpoint = Endpoint(monkeypatch)
    settings = config(checks=["access", "serving", "prefix"], context_limit=16384, stop_after_errors=1,
                      targets=[{"name": "service", "url": endpoint.url, "model": "test"}],
                      prefix={"prefix_chars": 100})
    report = await runner.run_suite(settings)
    assert next(r for r in report.checks if r.id == "prefix").status == "inconclusive"
    assert next(r for r in report.checks if r.id == "access").status == "pass"
    assert next(r for r in report.checks if r.id == "serving").status != "fail"
    assert report.overall != "fail"


@pytest.mark.parametrize("stall,status", [((.5, .6), "fail"), ((.1, 1.2), "inconclusive")])
def test_small_overlapping_stall_cannot_excuse_large_latency_overage(stall, status):
    result = traffic.finalize_stage(_evidence((stall,), 1), Limits(latency_ms=100, min_samples=2))
    assert result["status"] == status


def queue_evidence(load, duration, seed):
    rng = random.Random(seed)
    schedule = tuple((a, "short") for a in sorted(rng.random() * duration for _ in range(round(2 * load * duration))))
    end, records, lifetimes = 0, [], []
    template = _evidence((), 0).records[0][1]
    for index, (offset, _) in enumerate(schedule):
        end = max(offset, end) + .5 * math.exp(rng.gauss(0, .3) - .3**2 / 2)
        latency = (end - offset) * 1000
        records.append((index, template.model_copy(update={"id": str(index), "elapsed_ms": latency,
                                                          "arrival_elapsed_ms": latency})))
        lifetimes.append((offset, end))
    return replace(_evidence((), 0), rate_rps=2 * load, duration_seconds=duration,
                   observation_seconds=end, schedule=schedule, outcomes=("started",) * len(schedule),
                   records=tuple(records), lifetimes=tuple(lifetimes), arrival="poisson")


@pytest.mark.parametrize("load", [.6, .8, 1.2, 1.5])
@pytest.mark.parametrize("duration", [10, 20, 60])
def test_seeded_queue_guard_calibration(load, duration):
    results = [traffic.finalize_stage(queue_evidence(load, duration, seed), Limits(latency_ms=1e9, min_samples=2))
               for seed in range(200)]
    flagged = sum(result["queue_growth"] for result in results)
    if load <= .8:
        assert flagged <= 4
    else:
        assert sum(not result["accepted"] for result in results) >= 190


def test_queue_guard_detects_growth_despite_early_window_dip():
    evidence = queue_evidence(1.25, 10, 0)
    schedule = tuple((i / 5, "short") for i in range(50))
    latencies = [value for value in (2, 1, 3, 4, 5) for _ in range(10)]
    template = evidence.records[0][1]
    records = tuple((i, template.model_copy(update={"arrival_elapsed_ms": lat * 1000})) for i, lat in enumerate(latencies))
    evidence = replace(evidence, rate_rps=5, schedule=schedule, outcomes=("started",) * 50, arrival="steady",
                       records=records, lifetimes=tuple((a, a + lat) for (a, _), lat in zip(schedule, latencies)))
    result = traffic.finalize_stage(evidence, Limits(latency_ms=1e9, min_samples=2))
    assert result["queue_growth"] and not result["accepted"]

