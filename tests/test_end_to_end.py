"""Fault injection through a real loopback HTTP connection, without model claims."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from giraffe.client import LLMClient
from giraffe.fixtures import builtin_fixtures, score_response
from giraffe.models import Limits, RunConfig, Target
from tests.fake_endpoint import FakeEndpoint


def run_config(endpoint, **overrides):
    values = {
        "targets": [Target(name="test-service", url=endpoint.url, model="fake-model")],
        "checks": ["serving", "first_output", "generation", "capacity", "correctness"],
        "samples": 6,
        "concurrency": 2,
        "context_limit": 512,
        "max_requests": 100,
        "max_duration_seconds": 30,
        "request_timeout_seconds": 3,
        "sustained_seconds": 0,
        "stop_after_errors": 100,
        "limits": Limits(min_samples=5, regression_percent=20),
    }
    return RunConfig(**(values | overrides))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_real_http_and_builtin_scoring(stream):
    with FakeEndpoint() as endpoint:
        config = run_config(endpoint, stream=stream, structured_json=True)
        fixtures = builtin_fixtures(config)
        selected = fixtures["correctness"] + fixtures["json"] + fixtures["context"]
        async with LLMClient(config.targets[0], config) as client:
            records = await asyncio.gather(*(client.execute(spec) for spec in selected))
        scored = [score_response(spec, result) for spec, result in zip(selected, records, strict=True)]
        assert all(result.valid and result.score is True for result in scored)
        assert all(result.http_status == 200 and result.first_output_ms is not None for result in scored)
        assert all(result.stream is stream for result in scored)
        assert len(endpoint.requests) == len(selected)
        assert endpoint.peak_active <= config.concurrency


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,error_fragment", [
    ("empty", "no useful answer"), ("truncated", "without [DONE]"),
])
async def test_http_200_is_not_enough_for_a_valid_completion(mode, error_fragment):
    with FakeEndpoint(mode=mode) as endpoint:
        config = run_config(endpoint)
        spec = builtin_fixtures(config)["short"][0]
        async with LLMClient(config.targets[0], config) as client:
            result = await client.execute(spec)
        assert result.http_status == 200
        assert result.status == "failed" and not result.valid
        assert error_fragment in result.error
        assert score_response(spec, result).score is None


@pytest.mark.asyncio
async def test_client_disconnect_then_new_real_http_request():
    with FakeEndpoint(chunk_delay=0.01) as endpoint:
        config = run_config(endpoint)
        fixtures = builtin_fixtures(config)
        cancel_spec = next(spec for spec in fixtures["limits"] if spec.cancel_after_ms)
        async with LLMClient(config.targets[0], config) as client:
            cancelled = await client.execute(cancel_spec)
            probe = await client.execute(fixtures["short"][0])
        assert cancelled.status == "cancelled" and not cancelled.valid
        assert cancelled.output
        assert probe.valid
        assert score_response(fixtures["short"][0], probe).score is True


def read_report(path):
    return json.loads(path.read_text())


def cli_run(tmp_path, config, name, *, baseline=None):
    configuration = tmp_path / f"{name}.yaml"
    configuration.write_text(json.dumps(config.model_dump(mode="json")))
    output = tmp_path / name
    command = [
        sys.executable, "-m", "giraffe.cli", "run", "--config", str(configuration),
        "--output", str(output),
    ]
    if baseline is not None:
        command += ["--baseline", str(baseline)]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=45,
        cwd=Path(__file__).resolve().parents[1],
    )
    report_path = output / "report.json"
    assert report_path.exists(), result.stdout + result.stderr
    return result, read_report(report_path), output


def test_cli_detects_latency_regression_and_preserves_baseline(tmp_path):
    with FakeEndpoint() as endpoint:
        config = run_config(
            endpoint, checks=["serving", "first_output", "correctness"],
            limits=Limits(min_samples=5, regression_percent=20, first_output_ms=500),
        )
        initial_result, initial, initial_dir = cli_run(tmp_path, config, "baseline")
        assert initial_result.returncode == 0, initial_result.stdout + initial_result.stderr
        assert initial["overall"] == "pass"
        baseline_path = initial_dir / "report.json"
        original = baseline_path.read_bytes()
        endpoint.first_token_delay = 0.08
        result, current, output = cli_run(tmp_path, config, "slower", baseline=baseline_path)

    assert result.returncode == 1
    assert current["overall"] == "fail"
    assert baseline_path.read_bytes() == original
    assert current["baseline"]["status"] == "fail"
    assert any(
        comparison["metric"] == "first_output_ms" and comparison["status"] == "fail"
        and comparison["current_samples"] >= 5 and comparison["baseline_samples"] >= 5
        for target in current["baseline"]["targets"] for comparison in target["comparisons"]
    )
    # The seed violates the baseline, while staying inside the absolute 500 ms limit.
    assert next(check for check in current["checks"] if check["id"] == "first_output")["status"] == "pass"
    html = (output / "report.html").read_text()
    assert current["run_id"] in html and "baseline" in html.lower()
    assert "at most 100 requests" in result.stderr


def test_cli_detects_semantic_regression_with_valid_http_completions(tmp_path):
    with FakeEndpoint() as endpoint:
        config = run_config(endpoint, checks=["serving", "correctness"])
        _, baseline, directory = cli_run(tmp_path, config, "baseline")
        assert baseline["overall"] == "pass"
        endpoint.mode = "wrong"
        result, report, _ = cli_run(tmp_path, config, "wrong", baseline=directory / "report.json")

    assert result.returncode == 1 and report["overall"] == "fail"
    assert all(request["http_status"] == 200 and request["valid"] for request in report["requests"])
    assert any(request["score"] is False for request in report["requests"])
    assert next(check for check in report["checks"] if check["id"] == "correctness")["status"] == "fail"
    assert any(
        comparison["metric"] == "correctness_rate" and comparison["status"] == "fail"
        for target in report["baseline"]["targets"] for comparison in target["comparisons"]
    )


@pytest.mark.parametrize("mode", ["empty", "truncated"])
def test_cli_rejects_seeded_completion_failures(tmp_path, mode):
    with FakeEndpoint(mode=mode) as endpoint:
        result, report, _ = cli_run(
            tmp_path, run_config(endpoint, checks=["serving"]), mode,
        )
    assert result.returncode == 1 and report["overall"] == "fail"
    assert report["requests"]
    assert all(request["http_status"] == 200 and not request["valid"] for request in report["requests"])
    assert next(check for check in report["checks"] if check["id"] == "serving")["status"] == "fail"


def test_cli_honors_request_budget_and_never_runs_restart_hook_by_default(tmp_path):
    marker = tmp_path / "restart-was-run"
    with FakeEndpoint() as endpoint:
        config = run_config(endpoint, max_requests=3)
        config.targets[0].restart_command = [
            sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('run')",
        ]
        result, report, _ = cli_run(tmp_path, config, "bounded")
        observed_requests = len(endpoint.requests)
    assert observed_requests == len(report["requests"]) <= 3
    assert report["overall"] == "inconclusive" and result.returncode == 2
    assert report["abort_reason"]
    assert not marker.exists()
