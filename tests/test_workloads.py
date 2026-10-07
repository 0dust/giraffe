"""Workload contracts through a real local HTTP endpoint, including hostile behavior."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from giraffe.models import RequestRecord, RunConfig, Target
from giraffe.reporting import write_report
from giraffe.runner import _checks, _missing_timing, _stats, _timing_violations, run_suite
from giraffe.workloads import results, spec
from giraffe.fixtures import score_response
from tests.fake_endpoint import FakeEndpoint


def config(endpoint, **extra):
    return RunConfig.model_validate(
        {
            "targets": [{"name": "local", "url": endpoint.url, "model": "fake-model"}],
            "checks": ["serving"],
            "concurrency": 2,
            "samples": 2,
            "context_limit": 2048,
            "max_output_tokens": 128,
            "max_requests": 150,
            "max_duration_seconds": 20,
            "request_timeout_seconds": 3,
            "limits": {"min_samples": 2},
            **extra,
        }
    )


def check(report, name):
    return next(c for c in report.checks if c.id == name)


@pytest.mark.parametrize("mode", ["fixed", "live"])
async def test_concurrent_session_history_exact_marker_and_no_cross_contamination(tmp_path, mode):
    with FakeEndpoint(first_token_delay=0.02) as endpoint:
        settings = {"sessions": 2, "concurrency": 2, "modes": [mode], "delay_seconds": 0.01}
        report = await run_suite(config(endpoint, sessions=settings, retention="none"))
        session_calls = [p for p in endpoint.requests if "bakery" in p["messages"][-1]["content"]]
    followups = [p for p in session_calls if p["messages"][-1]["content"].startswith("Repeat")]
    assert len(followups) == 2
    histories = [p["messages"][1]["content"] for p in followups]
    first_records = [r for r in report.requests if r.scenario == f"session_{mode}_turn0"]
    assert len({r.output for r in first_records}) == 2
    if mode == "live":
        assert set(histories) == {r.output for r in first_records}
        assert len(set(histories)) == 2
    else:
        assert histories == ["Golden Crust", "Golden Crust"]
        assert all(r.output != "Golden Crust" for r in first_records)
    assert check(report, "sessions").status == "pass"
    turns = [r for r in report.requests if r.scenario == f"session_{mode}_turn1"]
    assert all(
        r.client_wait_ms is not None and r.client_wait_ms >= 0 and r.history_hash and r.request_hash
        for r in turns
    )
    write_report(report, tmp_path)
    stored = json.loads((tmp_path / "report.json").read_text())
    assert all(not r["output"] for r in stored["requests"])
    assert all(r["answer_hash"] for r in stored["requests"])


@pytest.mark.parametrize("mode", ["truncated", "empty", "http_error"])
async def test_session_failure_stops_without_substituting_saved_answer(mode):
    with FakeEndpoint(mode=mode, fault_contains="bakery") as endpoint:
        report = await run_suite(config(endpoint, sessions={"modes": ["live"], "sessions": 2}))
        calls = [p for p in endpoint.requests if "bakery" in p["messages"][-1]["content"]]
    assert len(calls) == 2
    assert all(len(p["messages"]) == 1 for p in calls)
    assert all(
        s["stop_reason"] for s in report.observations["targets"]["local"]["sessions"]["sessions"]
    )


async def test_session_history_budget_never_silently_crops():
    with FakeEndpoint() as endpoint:
        report = await run_suite(
            config(endpoint, context_limit=128, sessions={"modes": ["live"], "sessions": 1})
        )
    assert check(report, "sessions").status == "inconclusive"
    assert any(
        "not cropped" in s["stop_reason"]
        for s in report.observations["targets"]["local"]["sessions"]["sessions"]
    )


async def test_failed_semantic_turn_stops_session_before_next_user_turn():
    with FakeEndpoint(mode="wrong", fault_contains="Repeat exactly") as endpoint:
        report = await run_suite(
            config(
                endpoint,
                sessions={
                    "modes": ["live"],
                    "sessions": 1,
                    "turns": [
                        "Suggest a name for a bakery. Reply with just the name.",
                        "Repeat exactly the bakery name you just suggested, with no extra text.",
                        "Now repeat the bakery name once more.",
                    ],
                },
            )
        )
        calls = [p for p in endpoint.requests if "bakery" in p["messages"][-1]["content"]]
    assert len(calls) == 2
    assert check(report, "sessions").status == "fail"
    assert (
        "failed task" in check(report, "sessions").metrics["sessions"]["sessions"][0]["stop_reason"]
    )


def test_incomplete_later_mixed_group_does_not_hide_observed_failure():
    settings = RunConfig(
        targets=[Target(name="local", url="http://localhost:1", model="m")],
        checks=["serving"],
        buckets={"mixed_pairs": 1},
        limits={"min_samples": 2, "fairness_max_ratio": 2},
    )
    records = [
        RequestRecord(
            id=name,
            target="local",
            fixture_id=name,
            scenario=name,
            check_ids=["mixed"],
            started_at="2026-10-08T00:00:00Z",
            status="completed",
            valid=True,
            elapsed_ms=elapsed,
            overlap=overlap,
        )
        for name, elapsed, overlap in [
            ("mixed_control", 10, 1),
            ("mixed_control", 10, 1),
            ("mixed_short_failure", 100, 2),
            ("mixed_short_failure", 100, 2),
            ("mixed_short_unobserved", 10, 1),
        ]
    ]
    run = SimpleNamespace(
        config=settings,
        records=records,
        max_lag_ms=0,
        observations={"local": {}},
        unfinished={"local": set()},
    )
    outcome = results(run, settings.targets[0], _stats, _timing_violations, _missing_timing)
    assert next(c for c in outcome if c.id == "mixed").status == "fail"


@pytest.mark.parametrize("overlap,control_samples,mixed_samples", [(1, 2, 2), (2, 1, 2), (2, 2, 1)])
def test_mixed_latency_ratio_requires_control_samples_and_actual_overlap(
    overlap, control_samples, mixed_samples
):
    settings = RunConfig(
        targets=[Target(name="local", url="http://localhost:1", model="m")],
        buckets={"mixed_pairs": 2},
        limits={"min_samples": 2, "fairness_max_ratio": 2},
    )
    records = [
        RequestRecord(
            id=f"{scenario}-{index}", target="local", fixture_id=scenario,
            scenario=scenario, check_ids=["mixed"], started_at="2026-10-08T00:00:00Z",
            status="completed", valid=True, elapsed_ms=elapsed, overlap=achieved,
        )
        for scenario, count, elapsed, achieved in [
            ("mixed_control", control_samples, 10, 1),
            ("mixed_short_unqualified", mixed_samples, 100, overlap),
        ]
        for index in range(count)
    ]
    run = SimpleNamespace(config=settings, records=records, max_lag_ms=0,
                          observations={"local": {}}, unfinished={"local": set()})
    outcome = results(run, settings.targets[0], _stats, _timing_violations, _missing_timing)
    mixed = next(c for c in outcome if c.id == "mixed")
    assert mixed.status == "inconclusive"
    comparison = mixed.metrics["comparisons"][0]
    assert comparison["latency_ratio"] == 10
    assert comparison["samples_and_overlap_qualified"] is False


@pytest.mark.parametrize(
    "lag,warm_first,correct,expected",
    [(150, 2000, True, "pass"), (150, 100, True, "inconclusive"),
     (500, 100, False, "fail")],
)
def test_workload_timing_uses_core_generator_qualification(lag, warm_first, correct, expected):
    settings = RunConfig(
        targets=[Target(name="local", url="http://localhost:1", model="m")],
        checks=["serving"], prefix={"repeats": 2},
        limits={"min_samples": 2, "latency_ms": 1000},
    )
    records = [
        RequestRecord(
            id="warm", target="local", fixture_id="warm", scenario="warm", check_ids=[],
            started_at="2026-10-08T00:00:00Z", status="completed", valid=True,
            first_output_ms=warm_first, elapsed_ms=warm_first + 100,
        ),
        *[
            RequestRecord(
                id=f"prefix-{index}", target="local", fixture_id="prefix",
                scenario="prefix_shared", check_ids=["prefix"],
                started_at="2026-10-08T00:00:00Z", status="completed", valid=True,
                elapsed_ms=100, first_output_ms=20, score=correct,
            )
            for index in range(2)
        ],
    ]
    run = SimpleNamespace(config=settings, records=records, max_lag_ms=lag,
                          observations={"local": {"restart": None}},
                          unfinished={"local": set()})
    outcome = _checks(run, settings.targets[0])
    assert next(c for c in outcome if c.id == "prefix").status == expected


async def test_independent_prefix_controls_use_reproducible_matched_prose():
    with FakeEndpoint() as endpoint:
        settings = config(endpoint, prefix={"prefix_chars": 256, "repeats": 3, "history_turns": 2})
        report = await run_suite(settings)
        first = [p["messages"][0]["content"] for p in endpoint.requests if "Trial" in p["messages"][0]["content"]]
        endpoint.requests.clear()
        await run_suite(settings)
        second = [p["messages"][0]["content"] for p in endpoint.requests if "Trial" in p["messages"][0]["content"]]
    assert first == second and len(first) == 6
    comparison = check(report, "prefix").metrics["first_repeat"]
    assert comparison["shared_first"]["attempted"] == 1
    assert comparison["shared_repeats"]["attempted"] == 2
    assert comparison["independent_controls"]["attempted"] == 3
    assert "not proof" in comparison["interpretation"]
    assert len({prompt[:256] for prompt in first[1::2]}) == 3
    for shared, independent in zip(first[::2], first[1::2]):
        assert len(shared) == len(independent)
        assert "The rock is gray." in shared and " The " in independent
        assert "Reply with the box label only." in independent
        assert shared[:256] != independent[:256]


async def test_loaded_workload_source_identity_is_part_of_baseline_fixture_hash(monkeypatch):
    from giraffe import workloads
    from giraffe.reporting import compare_baseline
    with FakeEndpoint() as endpoint:
        settings = config(endpoint, prefix={"repeats": 2, "history_turns": 2})
        baseline = await run_suite(settings)
        monkeypatch.setattr(workloads, "FIXTURE_SOURCE_SHA256", "different-generator-content")
        current = await run_suite(settings)
    assert (baseline.manifest["fixture_pack"]["workload_fixture_sha256"]
            != current.manifest["fixture_pack"]["workload_fixture_sha256"])
    compared = compare_baseline(current, baseline)
    assert "Fixture pack identity differs." in compared.baseline["targets"][0]["reasons"]


@pytest.mark.parametrize(
    "arguments,finish",
    [('```json\n{"city":"Delhi"}\n```', "tool_calls"), ('{"city":"Delhi"}', "length")],
)
def test_fenced_arguments_and_truncated_tool_completion_do_not_pass(arguments, finish):
    request = spec("weather", "tools", "tools")
    request.scorer = "tool"
    record = RequestRecord(
        id="tool",
        target="local",
        fixture_id="tools",
        scenario="tools",
        check_ids=["tools"],
        started_at="2026-10-08T00:00:00Z",
        status="completed",
        valid=True,
        finish_reason=finish,
        tool_calls=[
            {
                "id": "call1",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": arguments,
                },
            }
        ],
    )
    assert score_response(request, record).score is False


async def test_missing_configured_workload_timing_is_inconclusive():
    with FakeEndpoint() as endpoint:
        report = await run_suite(
            config(
                endpoint,
                stream=False,
                prefix={"repeats": 2},
                limits={"min_samples": 2, "min_output_tokens_per_second": 1},
            )
        )
    result = check(report, "prefix")
    assert result.status == "inconclusive"
    assert "generation_tokens_per_second" in result.metrics["missing_timing_metrics"]


@pytest.mark.parametrize("mode", ["steady", "burst"])
async def test_bounded_arrivals_report_unissued_and_actual_dispatch(mode):
    with FakeEndpoint(first_token_delay=0.03) as endpoint:
        report = await run_suite(
            config(
                endpoint,
                concurrency=1,
                arrivals={
                    "mode": mode,
                    "requests": 12,
                    "requests_per_second": 1000,
                    "burst_size": 6,
                    "interval_seconds": 0.005,
                    "max_pending": 2,
                },
            )
        )
    rows = [r for r in report.requests if r.scenario == "arrival"]
    observation = report.observations["targets"]["local"]["arrivals"]
    assert observation["issued"] == len(rows) < 12
    assert observation["issued"] + sum(row.get("count", 1) for row in observation["unissued"]) == 12
    assert all(
        r.scheduled_ms is not None
        and r.dispatch_ms >= r.scheduled_ms
        and r.completed_ms >= r.dispatch_ms
        for r in rows
    )
    assert all(r.overlap == 1 for r in rows)
    assert observation["configured_rate_achieved"] is False
    assert check(report, "arrivals").status == "inconclusive"


async def test_global_budget_limits_all_workloads():
    with FakeEndpoint() as endpoint:
        report = await run_suite(
            config(
                endpoint,
                max_requests=7,
                arrivals={"requests": 8},
                sessions={},
                consistency={},
                prefix={},
            )
        )
        assert len(endpoint.requests) == len(report.requests) <= 7
    assert report.overall == "inconclusive"
    assert all(
        c.status == "inconclusive"
        for c in report.checks
        if c.id in {"prefix", "sessions", "consistency"}
    )


async def test_cancellation_stops_arrival_scheduler_and_saves_partial_evidence():
    with FakeEndpoint(first_token_delay=0.1) as endpoint:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_suite(
                config(endpoint, arrivals={"requests": 100, "requests_per_second": 100}),
                stop_event=stop,
            )
        )
        await asyncio.sleep(0.25)
        start = time.monotonic()
        stop.set()
        report = await task
        assert time.monotonic() - start < 0.5
        assert len(endpoint.requests) < 100
    assert report.abort_reason == "cancelled by user"
    assert report.requests


@pytest.mark.parametrize(
    "mode", ["healthy", "variable_correct", "wrong", "http_error", "overlap_variation"]
)
async def test_consistency_hashes_before_retention_and_separates_task_correctness(tmp_path, mode):
    with FakeEndpoint(mode=mode, first_token_delay=0.02) as endpoint:
        report = await run_suite(
            config(endpoint, consistency={"repetitions": 4, "shapes": ["short"]}, retention="none")
        )
        calls = [
            p for p in endpoint.requests if p["messages"][-1]["content"].startswith("The box label")
        ]
    result = check(report, "consistency")
    buckets = result.metrics["buckets"]
    assert len(buckets) == 2
    assert all(len(p["messages"]) == 1 for p in calls)
    if mode in {"wrong", "http_error"}:
        assert result.status == "fail"
    elif mode == "healthy":
        assert all(b["agreement_rate"] == 1 for b in buckets.values())
    elif mode == "variable_correct":
        assert all(b["agreement_rate"] < 1 and b["correctness"] == 1 for b in buckets.values())
        assert result.status == "pass"
    else:
        assert buckets["consistency_c1_short"]["agreement_rate"] == 1
        assert buckets["consistency_c2_short"]["agreement_rate"] < 1
        assert result.status == "pass"
    write_report(report, tmp_path)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert all(not r["output"] for r in saved["requests"])


async def test_strict_consistency_and_insufficient_samples_are_explicit():
    with FakeEndpoint(mode="variable_correct") as endpoint:
        strict = await run_suite(
            config(endpoint, consistency={"strict": True, "shapes": ["short"]})
        )
        short = await run_suite(config(endpoint, max_requests=4, consistency={"shapes": ["short"]}))
    assert check(strict, "consistency").status == "fail"
    assert check(short, "consistency").status == "inconclusive"


async def test_buckets_keep_shape_overlap_lengths_and_eos_coverage_separate():
    with FakeEndpoint(first_token_delay=0.02, chunk_delay=0.015) as endpoint:
        report = await run_suite(
            config(endpoint, buckets={"samples_per_bucket": 2, "mixed_pairs": 1})
        )
    result = check(report, "buckets")
    assert len(result.metrics["buckets"]) == 18
    assert result.status == "inconclusive"  # Early EOS does not exercise long output.
    assert all(b["actual_input_tokens"] for b in result.metrics["buckets"].values())
    assert all(
        b["achieved_overlap"] >= 2 for key, b in result.metrics["buckets"].items() if "c2_" in key
    )
    assert len(check(report, "mixed").metrics["comparisons"]) == 4
    assert any(r.workload.get("short_stream_active_at_dispatch") for r in report.requests)


@pytest.mark.parametrize(
    "mode,fragment",
    [
        ("healthy", None),
        ("tool_wrong", "Wrong tool function"),
        ("tool_malformed", "Malformed tool argument"),
        ("tool_schema", "schema mismatch"),
        ("tool_multiple", "count"),
        ("tool_missing", "Missing parsed"),
        ("tool_rejection", "HTTP 400"),
        ("tool_truncated", "without [DONE]"),
    ],
)
async def test_complete_and_fragmented_tool_calls(mode, fragment, tmp_path):
    with FakeEndpoint(mode=mode) as endpoint:
        report = await run_suite(config(endpoint, tool_calling=True, retention="none"))
        requests = [p for p in endpoint.requests if p.get("tools")]
    assert len(requests) == 2
    assert all(p["tool_choice"] == "auto" for p in requests)
    records = [r for r in report.requests if "tools" in r.check_ids]
    if mode == "healthy":
        assert check(report, "tools").status == "pass"
        assert all(r.valid and r.score is True and not r.output for r in records)
        assert all(r.tool_calls[0]["function"]["name"] == "get_weather" for r in records)
    else:
        assert check(report, "tools").status == "fail"
        assert any(fragment in (r.error or r.score_message or "") for r in records)
    write_report(report, tmp_path)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert all(not r["tool_calls"] for r in saved["requests"])


async def test_tools_disabled_and_forced_diagnostic_remain_separate():
    with FakeEndpoint() as endpoint:
        disabled = await run_suite(config(endpoint))
        assert not any(p.get("tools") for p in endpoint.requests)
        forced = await run_suite(config(endpoint, tool_calling=True, forced_tool_diagnostic=True))
    assert "tools" not in {c.id for c in disabled.checks}
    assert disabled.manifest["workload_selection"]["tools"] == "not selected"
    assert forced.manifest["workload_selection"]["tools"] == "selected"
    assert (
        len([r for r in forced.requests if r.workload.get("selection") == "forced diagnostic"]) == 2
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"buckets": {"levels": [3]}},
        {"sessions": {"concurrency": 3}},
        {"sessions": {"turns": ["one"]}},
        {"consistency": {"repetitions": 1}},
        {"buckets": {"output_tokens": {"short": 16, "medium": 48, "long": 256}}},
    ],
)
def test_invalid_workloads_rejected_before_network(extra):
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "targets": [{"name": "local", "url": "http://localhost:1", "model": "test"}],
                "concurrency": 2,
                **extra,
            }
        )
