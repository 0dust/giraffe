"""Current workload fingerprints preserve settings and request identity."""

import pytest

from giraffe import runner
from giraffe.deployment import fixture_hash
from giraffe.models import CheckResult, RunConfig
from giraffe.reporting import _settings_reasons, compare_baseline
from tests.test_runner import client as client
from tests.test_reporting import make_report


def reports(checks=None, **updates):
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost:8000", "model": "test"}],
                       checks=checks or ["load"], **updates)
    report = make_report()
    report.manifest["config"] = config.model_dump(mode="json")
    full, shared = runner.build_workload_identity(config)
    report.manifest["fixture_pack"] = {
        "builtin_sha256": "builtins", "workload_fixture_sha256": fixture_hash(full),
        **shared,
    }
    if "multi_user" in config.checks:
        report.checks.append(CheckResult(id="multi_user", target="local", title="Multi-user",
            status="pass", summary="Completed", metrics={"multi_user": {
                "version": 1, "window_policy": "completed-output-v1"}}))
    return report


@pytest.mark.parametrize("checks", [["load"], ["sessions"], ["multi_user"], ["load", "prefix"], ["tools"]])
def test_current_workload_identity_compares_cleanly(checks):
    current = reports(checks)
    previous = current.model_copy(deep=True)
    assert _settings_reasons(current, previous) == []
    assert _settings_reasons(previous, current) == []


@pytest.mark.parametrize("change", ["unknown_fingerprint", "settings", "builtin"])
def test_changed_workload_identity_requires_new_baseline(change):
    current = reports(["load", "prefix"])
    previous = current.model_copy(deep=True)
    if change == "unknown_fingerprint":
        previous.manifest["fixture_pack"]["workload_fixture_sha256"] = "unknown"
    elif change == "settings":
        config = RunConfig.model_validate(previous.manifest["config"])
        config.prefix.repeats += 1
        previous.manifest["config"] = config.model_dump(mode="json")
        full, _ = runner.build_workload_identity(config)
        previous.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(full)
    else:
        previous.manifest["fixture_pack"]["builtin_sha256"] = "changed"
    assert "Fixture pack identity differs." in _settings_reasons(current, previous)
    assert "Fixture pack identity differs." in _settings_reasons(previous, current)


@pytest.mark.asyncio
@pytest.mark.parametrize("load", [False, True], ids=["quick-check", "load"])
async def test_browser_safety_limits_preserve_real_baseline_comparability(load):
    from tests.test_concurrent_load import configuration

    config = (configuration(max_requests=None) if load else RunConfig(
        targets=[{"name": "local", "url": "http://localhost:8000", "model": "test"}]))
    assert config.max_requests is config.max_duration_seconds is config.concurrency is None
    previous = await runner.run_suite(config)
    limited = config.model_copy(update={
        "max_requests": 50_000, "max_duration_seconds": 7200, "concurrency": 128})
    current = await runner.run_suite(limited)

    compared = compare_baseline(current, previous)

    assert not previous.limits_reached and not current.limits_reached
    assert current.manifest["effective_checks"] == previous.manifest["effective_checks"]
    assert current.manifest["fixture_pack"] == previous.manifest["fixture_pack"]
    target = compared.baseline["targets"][0]
    assert target["reasons"] == []
    assert target["comparisons"]
