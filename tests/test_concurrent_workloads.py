"""Public merged workload contract, exercised through the loopback HTTP server."""

import json

from giraffe.capabilities import capabilities
from giraffe.models import RunConfig
from giraffe.reporting import write_report
from giraffe.runner import run_suite
from tests.fake_endpoint import FakeEndpoint


def configuration(endpoint, **settings):
    return RunConfig.model_validate({
        "targets": [{"name": "local", "url": endpoint.url, "model": "fake-model"}],
        "checks": ["concurrency"], "concurrency": 2, "samples": 2,
        "max_requests": 150, "max_duration_seconds": 30,
        "request_timeout_seconds": 3, "max_output_tokens": 128,
        "context_limit": 2048, "limits": {"min_samples": 2, "fairness_max_ratio": 100},
        "concurrent_workloads": {"samples_per_bucket": 2, "mixed_pairs": 2, **settings},
    })


def test_one_public_check_and_legacy_configuration_migration():
    checks = capabilities()["checks"]
    assert len(checks) == 17
    assert {"concurrency"} <= {item["id"] for item in checks}
    assert not {"fairness", "buckets", "mixed"}.intersection(item["id"] for item in checks)
    old = RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://localhost:1", "model": "test"}],
        "checks": ["fairness", "buckets", "mixed"],
        "buckets": {"samples_per_bucket": 5},
    })
    assert old.checks == ["concurrency"]
    assert set(old.concurrent_workloads.scenarios) == {
        "streaming_fairness", "size_matrix", "mixed_interference"}
    assert old.concurrent_workloads.samples_per_bucket == 5
    assert RunConfig.model_validate(old.model_dump()) == old
    from giraffe.models import Buckets
    typed = RunConfig(targets=old.targets, checks=["buckets"], buckets=Buckets())
    assert typed.checks == ["concurrency"]
    assert typed.concurrent_workloads.scenarios == ["size_matrix"]


async def test_merged_result_retains_each_scenario_and_measurement(tmp_path):
    with FakeEndpoint(first_token_delay=.02, chunk_delay=.015) as endpoint:
        report = await run_suite(configuration(endpoint, scenarios=[
            "streaming_fairness", "size_matrix", "mixed_interference"]))
    active = [item for item in report.checks if item.status != "skipped"]
    assert [item.id for item in active] == ["concurrency"]
    result = active[0]
    scenarios = result.metrics["scenarios"]
    assert set(scenarios) == {"streaming_fairness", "size_matrix", "mixed_interference"}
    assert "max_stream_gap_ms" in scenarios["streaming_fairness"]["metrics"]
    assert "latency_ratio" in scenarios["streaming_fairness"]["metrics"]
    assert len(scenarios["size_matrix"]["metrics"]["buckets"]) == 18
    assert len(scenarios["mixed_interference"]["metrics"]["comparisons"]) == 4
    assert all(item["evidence_ids"] for item in scenarios.values())
    assert all(item["status"] in {"pass", "fail", "inconclusive"} for item in scenarios.values())
    assert not {"fairness", "buckets", "mixed"}.intersection(report.manifest["config"]["checks"])
    write_report(report, tmp_path)
    html = (tmp_path / "report.html").read_text()
    for item in scenarios.values():
        assert item["title"] in html
    saved = json.loads((tmp_path / "report.json").read_text())
    merged = next(item for item in saved["checks"] if item["id"] == "concurrency")
    assert merged["metrics"]["scenarios"] == scenarios


async def test_large_cases_are_visible_when_global_limits_prevent_them():
    with FakeEndpoint() as endpoint:
        report = await run_suite(configuration(endpoint, scenarios=["token_stress"]))
        assert endpoint.requests == []
    merged = next(item for item in report.checks if item.id == "concurrency")
    cases = merged.metrics["scenarios"]["token_stress"]["metrics"]["cases"]
    assert set(cases) == {"long_input_summary", "short_input_long_output"}
    assert cases["long_input_summary"]["requested_input_tokens"] == 10000
    assert cases["long_input_summary"]["requested_output_tokens"] == 50
    assert cases["short_input_long_output"]["requested_output_tokens"] == 10000
    assert all(case["status"] == "inconclusive" for case in cases.values())
    assert merged.status == "inconclusive"


async def test_token_stress_calibrates_input_and_reports_early_eos():
    with FakeEndpoint() as endpoint:
        report = await run_suite(configuration(endpoint, scenarios=["token_stress"],
            stress_cases=[{"name": "long_input_summary", "input_tokens": 200,
                           "output_tokens": 20, "min_output_tokens": 1},
                          {"name": "short_input_long_output", "output_tokens": 80,
                           "min_output_tokens": 80}]))
    result = next(item for item in report.checks if item.id == "concurrency")
    cases = result.metrics["scenarios"]["token_stress"]["metrics"]["cases"]
    summary = cases["long_input_summary"]
    assert summary["calibration_requests"]
    assert summary["actual_input_tokens"]
    assert all(180 <= value <= 220 for value in summary["actual_input_tokens"])
    generation = cases["short_input_long_output"]
    assert generation["status"] == "inconclusive"
    assert "output" in " ".join(generation["coverage_gaps"]).lower()
    assert result.status == "inconclusive"


async def test_wrong_summary_fact_is_not_hidden_by_incomplete_generation_length():
    with FakeEndpoint(mode="wrong") as endpoint:
        report = await run_suite(configuration(endpoint, scenarios=["token_stress"],
            stress_cases=[{"name": "long_input_summary", "input_tokens": 200,
                           "output_tokens": 20, "min_output_tokens": 1},
                          {"name": "short_input_long_output", "output_tokens": 80}]))
    result = next(item for item in report.checks if item.id == "concurrency")
    cases = result.metrics["scenarios"]["token_stress"]["metrics"]["cases"]
    assert cases["long_input_summary"]["status"] == "fail"
    assert cases["short_input_long_output"]["status"] == "inconclusive"
    assert result.status == report.overall == "fail"


async def test_token_stress_cannot_pass_a_partially_sent_plan():
    with FakeEndpoint() as endpoint:
        config = configuration(endpoint, scenarios=["token_stress"], levels=[1], samples_per_bucket=3,
            stress_cases=[{"name": "short_input_long_output", "output_tokens": 1}])
        config.max_requests = 2
        report = await run_suite(config)
    merged = next(item for item in report.checks if item.id == "concurrency")
    case = merged.metrics["scenarios"]["token_stress"]["metrics"]["cases"]["short_input_long_output"]
    assert case["levels"]["1"]["planned_requests"] == 3
    assert case["levels"]["1"]["attempted"] == 2
    assert case["status"] == merged.status == "inconclusive"
