import json

import pytest

from giraffe.cli import configuration, parser
from giraffe.models import CATALOGUE_NAMES, CheckResult, RunConfig, RunReport
from giraffe.reporting import _settings_reasons
from tests.test_reporting import make_report


TARGETS = [{"name": name, "url": "http://localhost/v1", "model": "test"}
           for name in ("one", "two")]


@pytest.mark.parametrize("source", ["config", "manifest"])
def test_legacy_concurrency_flag_rebuilds_migrated_overlap_defaults(source, tmp_path, capsys):
    saved = {"targets": TARGETS, "overlap_models": True, "concurrency": 40,
             "checks": ["correctness", "json", "recovery", "arrivals", "capacity", "concurrency"],
             "arrivals": {}, "max_requests": 300, "max_duration_seconds": 300}
    path = tmp_path / "saved.json"
    if source == "manifest":
        path.write_text(RunReport(run_id="saved", started_at="now", finished_at="now", overall="pass",
                                  manifest={"config": saved}).model_dump_json())
    else:
        path.write_text(json.dumps(saved))
    config = configuration(parser().parse_args([
        "run", "--" + source, str(path), "--concurrency", "8",
    ]))
    assert config.concurrency == config.default_concurrency == 8
    assert {name: config.effective_check(name)["concurrency"]
            for name in ("correctness", "json", "recovery")} == {
                "correctness": 8, "json": 8, "recovery": 8}
    assert config.effective_arrivals().max_in_flight == 8
    assert config.effective_traffic().max_in_flight == 8
    assert config.concurrent_load.schedule.levels == [1, 4]
    notices = config.concurrency_notices()
    assert len(notices) == 5
    assert {notice["check"] for notice in notices} == {
        "correctness", "json", "recovery", "arrivals", "capacity"}
    assert all(notice["msg"].startswith(CATALOGUE_NAMES[notice["check"]] + ": ") for notice in notices)
    assert all(notice["msg"].count("Shared across") == 1 for notice in notices)
    assert not any("40" in note for note in config._migration_notes)
    assert capsys.readouterr().err.count("--concurrency 8 sets both") == 1


def test_legacy_concurrency_flag_preserves_explicit_check_settings(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({
        "targets": TARGETS, "overlap_models": True, "concurrency": 40,
        "checks": ["correctness", "json", "recovery", "arrivals", "capacity"],
        "test_options": {"correctness": {"concurrency": 9}, "json": {"concurrency": 3}},
        "arrivals": {}, "traffic": {"max_in_flight": 6},
    }))
    config = configuration(parser().parse_args(["run", "--config", str(path), "--concurrency", "8"]))
    assert config.effective_check("correctness")["concurrency"] == 9
    assert config.effective_check("json")["concurrency"] == 3
    assert config.effective_check("recovery")["concurrency"] == 8
    assert config.effective_arrivals().max_in_flight == 8
    assert config.effective_traffic().max_in_flight == 6


def test_simultaneous_limit_flag_only_changes_legacy_ceiling(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"targets": TARGETS, "overlap_models": True, "concurrency": 40,
                                "checks": ["correctness", "arrivals"], "arrivals": {}}))
    config = configuration(parser().parse_args([
        "run", "--config", str(path), "--simultaneous-limit", "8",
    ]))
    assert config.concurrency == 8 and config.default_concurrency == 40
    assert config.effective_check("correctness")["concurrency"] == 40
    assert config.effective_arrivals().max_in_flight == 40


def test_v2_input_concurrency_flag_does_not_replace_explicit_check_values(tmp_path):
    config = RunConfig(targets=TARGETS, checks=["correctness"], overlap_models=True,
                       test_options={"correctness": {"concurrency": 9}})
    path = tmp_path / "v2.json"
    path.write_text(config.model_dump_json())
    resolved = configuration(parser().parse_args([
        "run", "--config", str(path), "--concurrency", "8",
    ]))
    assert resolved.concurrency == resolved.default_concurrency == 8
    assert resolved.effective_check("correctness")["concurrency"] == 9
    assert len(resolved._migration_notes) == 1
    assert "--concurrency 8 sets both" in resolved._migration_notes[0]


@pytest.mark.parametrize("evidence", ["warning", "check", "observation"])
@pytest.mark.parametrize("limited", ["current", "baseline"])
def test_old_budget_evidence_preserves_limit_and_quota_baseline_differences(evidence, limited):
    current, old = make_report(), make_report("baseline")
    old.manifest["config"]["max_requests"] = 12
    for report, quota in ((current, {}), (old, {"local": 12})):
        report.manifest["load_shape"] = {"target_request_quotas": quota}
    report = current if limited == "current" else old
    metrics = {"load_types": {"combinations": {"points": [{"reason_code": "budget_limited"}]}}}
    if evidence == "warning":
        report.warnings = ["Global request budget exhausted; remaining checks could not finish."]
    elif evidence == "check":
        report.checks.append(CheckResult(id="concurrency", target="local", title="Concurrent load",
                                        status="inconclusive", summary="Budget-limited", metrics=metrics))
    else:
        report.observations = {"targets": {"local": {"concurrent_load": metrics}}}
    before = current.model_dump_json(), old.model_dump_json()
    reasons = _settings_reasons(current, old)
    assert "Run setting max_requests differs and its limit stopped work or constrained concurrency." in reasons
    assert "load_shape differs." in reasons
    assert (current.model_dump_json(), old.model_dump_json()) == before


def test_budget_limited_text_without_budget_evidence_does_not_invent_a_limit():
    current, old = make_report(), make_report("baseline")
    old.manifest["config"]["max_requests"] = 12
    old.warnings = ["The request budget may limit coverage."]
    old.checks[0].summary = "A request budget may limit coverage."
    assert _settings_reasons(current, old) == []
