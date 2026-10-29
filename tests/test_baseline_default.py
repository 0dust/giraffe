"""The default workload has enough evidence for an unchanged baseline verdict."""
import pytest

from giraffe import runner
from giraffe.models import DEFAULT_CHECKS, Limits
from giraffe.reporting import compare_baseline
from tests.test_runner import client as client, config
from tests.test_reporting import baseline_check


@pytest.mark.asyncio
async def test_identical_default_five_sample_fake_runs_pass_baseline(client):
    settings = config(checks=list(DEFAULT_CHECKS), samples=5, max_requests=200,
                      limits=Limits(min_samples=5))
    previous = await runner.run_suite(settings)
    current = await runner.run_suite(settings)
    assert current.overall == previous.overall == "pass"
    check = baseline_check(compare_baseline(current, previous))
    assert check.status == "pass", check.summary


@pytest.mark.asyncio
async def test_identical_default_runs_with_optional_gpu_pass_baseline(client, monkeypatch):
    async def no_metrics(*args, **kwargs):
        return {"status": "unavailable", "reason": "Fake endpoint has no exporter", "samples": [],
                "elapsed_seconds": 0, "unavailable": []}
    monkeypatch.setattr("giraffe.telemetry.scrape", no_metrics)
    settings = config(checks=[*DEFAULT_CHECKS, "gpu"], samples=5, max_requests=200,
                      gpu_min_observation_seconds=0, limits=Limits(min_samples=5))
    previous = await runner.run_suite(settings)
    current = await runner.run_suite(settings)
    assert current.overall == previous.overall == "pass"
    assert all(not check.required for check in current.checks if check.id == "gpu")
    check = baseline_check(compare_baseline(current, previous))
    assert check.status == "pass", check.summary
    assert "gpu" not in check.summary
