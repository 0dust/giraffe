"""Saved reports must not prevent a completed current run from being persisted."""

import json

import pytest
from pydantic import ValidationError

from giraffe.cli import main
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import (
    _run_limits_workload_identity, _settings_reasons, compare_baseline, write_report,
)
from tests.test_reporting import baseline_check, make_report
from tests.test_web import browser, finished


VERSION_REASON = (
    "Baseline recorded configuration has an unsupported config_version; comparison requires "
    "version 2 or an unversioned legacy configuration. Rerun the baseline run with a supported configuration."
)
INVALID_REASON = (
    "Baseline recorded configuration is invalid; comparability is unknown. "
    "Rerun the baseline run with a valid configuration."
)


def historical_report(version):
    """Versions 1 and 3 were accepted and retained by main 34e149e's integer field."""
    report = make_report("saved-baseline")
    if version == "unversioned":
        report.manifest["config"].pop("config_version")
    else:
        report.manifest["config"]["config_version"] = version
    return RunReport.model_validate_json(report.model_dump_json())


def assert_ineligible(result, reason):
    assert result.baseline["status"] == "inconclusive"
    assert result.baseline["targets"][0]["reasons"] == [reason]
    assert result.baseline["targets"][0]["comparisons"] == []
    assert baseline_check(result).status == "inconclusive"
    assert reason in baseline_check(result).summary
    assert result.overall == "inconclusive"
    assert result.checks[0].id == "correctness"
    assert result.checks[0].status == "pass"
    assert len(result.requests) == 8


@pytest.mark.parametrize("version", [1, 3, -1, True, 2.0, "2", None])
def test_unsupported_historical_version_is_ineligible_without_mutating_evidence(version):
    current, baseline = make_report(), historical_report(version)
    before = current.model_dump_json(), baseline.model_dump_json()
    assert_ineligible(compare_baseline(current, baseline), VERSION_REASON)
    assert (current.model_dump_json(), baseline.model_dump_json()) == before


@pytest.mark.parametrize("checks", [None, 1, [{}], [["serving"]]])
def test_malformed_saved_selection_is_ineligible_without_mutating_evidence(checks):
    current, baseline = make_report(), historical_report(2)
    baseline.manifest["config"]["checks"] = checks
    before = current.model_dump_json(), baseline.model_dump_json()
    assert_ineligible(compare_baseline(current, baseline), INVALID_REASON)
    assert (current.model_dump_json(), baseline.model_dump_json()) == before


@pytest.mark.parametrize("version", [2, "unversioned"])
def test_supported_historical_configuration_remains_comparable(version):
    current, baseline = make_report(), historical_report(version)
    before = current.model_dump_json(), baseline.model_dump_json()
    result = compare_baseline(current, baseline)
    assert _settings_reasons(current, baseline) == []
    assert baseline_check(result).status == "pass"
    assert result.baseline["targets"][0]["comparisons"]
    assert (current.model_dump_json(), baseline.model_dump_json()) == before


@pytest.mark.parametrize("change", ["unsupported_version", "malformed_checks"])
def test_invalid_saved_configuration_cannot_attest_a_workload_identity(change):
    baseline = historical_report(1 if change == "unsupported_version" else 2)
    if change == "malformed_checks":
        baseline.manifest["config"]["checks"] = [{}]
    before = baseline.model_dump_json()
    assert _run_limits_workload_identity(baseline.manifest, revision="phase3") is None
    assert baseline.model_dump_json() == before


@pytest.mark.parametrize("version", [1, 3])
def test_new_configuration_still_rejects_historical_unsupported_versions(version):
    with pytest.raises(ValidationError) as raised:
        RunConfig.model_validate(historical_report(version).manifest["config"])
    assert any(error["loc"] == ("config_version",) for error in raised.value.errors())


@pytest.mark.parametrize("version", [1, 3, 2, "unversioned"])
def test_cli_baseline_comparison_persists_completed_run(version, tmp_path, monkeypatch, capsys, local_tokenizer):
    baseline = historical_report(version)
    source, saved = tmp_path / "previous.json", tmp_path / "baseline.json"
    source.write_text(baseline.model_dump_json())
    assert main(["baseline", "save", str(source), str(saved)]) == 0
    capsys.readouterr()
    saved_before = saved.read_bytes()
    current = make_report()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current.manifest["config"]))
    calls = []

    async def run(configuration, **kwargs):
        calls.append(configuration.model_dump(mode="json"))
        return current.model_copy(deep=True)

    monkeypatch.setattr("giraffe.cli._run", run)
    output = tmp_path / "current"
    expected = 2 if version in (1, 3) else 0
    assert main(["run", "--config", str(config_path), "--baseline", str(saved),
                 "--output", str(output), "--skip-preflight", "--output-format", "json"]) == expected
    captured = capsys.readouterr()
    outcome = json.loads(captured.out)
    assert len(calls) == 1
    assert outcome["exit_code"] == expected
    assert outcome["request_count"] == 8
    assert outcome["report_paths"]["json"] == str((output / "report.json").resolve())
    persisted = RunReport.model_validate_json((output / "report.json").read_text())
    if expected:
        assert_ineligible(persisted, VERSION_REASON)
        assert outcome["outcome"] == "inconclusive"
    else:
        assert baseline_check(persisted).status == "pass"
        assert outcome["outcome"] == "pass"
    state = json.loads((output / ".web.json").read_text())
    assert state["state"] == "completed"
    assert not state.get("error")
    assert saved.read_bytes() == saved_before


@pytest.mark.parametrize("version", [1, 3, 2, "unversioned"])
async def test_web_saved_baseline_comparison_persists_completed_run(version, tmp_path, monkeypatch, local_tokenizer):
    baseline = historical_report(version)
    previous = tmp_path / "previous"
    write_report(baseline, previous)
    current = make_report()
    calls = []

    async def run(configuration, **kwargs):
        calls.append(configuration.model_dump(mode="json"))
        return current.model_copy(deep=True)

    monkeypatch.setattr("giraffe.web.run_suite", run)
    async with browser(tmp_path) as client:
        saved = await client.post("/api/baselines", json={"run_id": "previous", "name": "Before upgrade"})
        assert saved.status_code == 201, saved.text
        baseline_id = saved.json()["id"]
        baseline_path = tmp_path / ".baselines" / baseline_id / "report.json"
        saved_before = baseline_path.read_bytes()
        started = await client.post("/api/runs", json={
            "config": current.manifest["config"], "baseline_id": baseline_id,
        })
        assert started.status_code == 202, started.text
        identifier = started.json()["id"]
        detail = await finished(client, identifier)
        assert detail["state"] == "completed"
        assert not detail.get("error")
        assert len(calls) == 1
        persisted = RunReport.model_validate_json((tmp_path / identifier / "report.json").read_text())
        if version in (1, 3):
            assert_ineligible(persisted, VERSION_REASON)
        else:
            assert baseline_check(persisted).status == "pass"
        assert detail["report"]["baseline"] == persisted.baseline
        assert (await client.get(f"/api/runs/{identifier}/report.json")).status_code == 200
        row = next(item for item in (await client.get("/api/runs")).json()["runs"]
                   if item["id"] == identifier)
        assert row["state"] == "completed"
        state = json.loads((tmp_path / identifier / ".web.json").read_text())
        assert state["state"] == "completed"
        assert not state.get("error")
        assert baseline_path.read_bytes() == saved_before
