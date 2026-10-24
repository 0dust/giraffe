"""The CLI keeps optional suite ceilings separate from check parallelism."""

import json
from pathlib import Path

import pytest
import yaml

from giraffe.cli import configuration, main, migrated_configuration, parser
from giraffe.models import RunConfig, RunReport


TARGET = {"name": "local", "url": "http://localhost", "model": "tiny"}
BASE = ["run", "--url", "http://localhost", "--model", "tiny", "--checks", "correctness"]


def test_flag_only_configs_are_v2_without_migration_notes(capsys):
    config = configuration(parser().parse_args(BASE))
    assert config.config_version == 2
    assert config.concurrency is config.max_requests is config.max_duration_seconds is None
    assert config.default_concurrency == 4
    assert config.effective_check("correctness")["concurrency"] == 4
    assert config._migration_notes == []
    assert not capsys.readouterr().err


def test_a24_legacy_concurrency_sets_ceiling_and_parallelism_with_note(capsys):
    config = configuration(parser().parse_args(BASE + ["--concurrency", "40"]))
    assert config.concurrency == config.default_concurrency == 40
    assert config.effective_check("correctness")["concurrency"] == 40
    assert "--concurrency 40 sets both" in capsys.readouterr().err
    assert config._migration_notes


def test_a24_simultaneous_limit_does_not_set_default_parallelism(capsys):
    config = configuration(parser().parse_args(BASE + ["--simultaneous-limit", "40"]))
    assert config.concurrency == 40 and config.default_concurrency == 4
    assert config.effective_check("correctness")["concurrency"] == 4
    assert not capsys.readouterr().err


def test_default_concurrency_can_override_legacy_flag_independently(capsys):
    config = configuration(parser().parse_args(BASE + [
        "--default-concurrency", "8", "--concurrency", "40", "--simultaneous-limit", "64",
    ]))
    assert config.concurrency == 64 and config.default_concurrency == 8
    assert config.effective_check("correctness")["concurrency"] == 8
    assert "--concurrency" in capsys.readouterr().err


@pytest.mark.parametrize("flag,field", [
    ("--no-max-requests", "max_requests"),
    ("--no-max-duration", "max_duration_seconds"),
    ("--no-simultaneous-limit", "concurrency"),
])
def test_a4_disabled_limit_wins_after_saved_values_and_flag_overrides(tmp_path, flag, field):
    values = {"config_version": 2, "targets": [TARGET], "checks": ["correctness"],
              "max_requests": 300, "max_duration_seconds": 300, "concurrency": 40}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    config = configuration(parser().parse_args([
        "run", "--config", str(path), flag, "--max-requests", "20", "--max-duration", "25",
        "--simultaneous-limit", "30",
    ]))
    expected = {"max_requests": 20, "max_duration_seconds": 25, "concurrency": 30}
    expected[field] = None
    assert {name: getattr(config, name) for name in expected} == expected
    assert config.model_dump(mode="json")[field] is None


def test_a19_omitted_legacy_limits_are_not_set_with_one_note_each(capsys):
    config = migrated_configuration({"targets": [TARGET], "checks": ["correctness"]})
    assert config.concurrency is config.max_requests is config.max_duration_seconds is None
    assert config.default_concurrency == 4
    assert len(config._migration_notes) == 3
    stderr = capsys.readouterr().err
    assert stderr.count("Migration:") == 3
    assert "300 requests" in stderr and "300" in stderr and "4" in stderr


def test_a19_explicit_null_is_not_set_without_notes(capsys):
    config = migrated_configuration({"targets": [TARGET], "checks": ["correctness"],
                                    "concurrency": None, "max_requests": None, "max_duration_seconds": None})
    assert config.concurrency is config.max_requests is config.max_duration_seconds is None
    assert config.default_concurrency == 4
    assert config._migration_notes == []
    assert not capsys.readouterr().err


def test_a19_legacy_concurrency_preserves_arrival_and_capacity_parallelism(capsys):
    from giraffe.load_test import admission_limit
    config = migrated_configuration({"targets": [TARGET], "checks": ["arrivals", "capacity"],
                                    "arrivals": {}, "concurrency": 40,
                                    "max_requests": 300, "max_duration_seconds": 300})
    assert config.concurrency == config.default_concurrency == 40
    assert config.checks == ['load']
    assert admission_limit(config, config.load_test) == 40
    assert len(config.load_test.migrated_tests) == 1
    capacity = config.load_test.migrated_tests[0]
    assert capacity.source == 'capacity'
    assert admission_limit(config, capacity.settings) == 40
    assert config.load_test.patterns.constant.enabled
    assert capacity.settings.patterns.rates == [1, 2, 4]
    assert any('retain legacy 40-request parallelism' in note for note in config._migration_notes)
    stderr = capsys.readouterr().err
    assert all('Migration: ' + note in stderr for note in config._migration_notes)


def test_a19_d2_runs_before_load_migration_without_imposing_300_second_limit():
    config = migrated_configuration({"targets": [TARGET], "checks": ["buckets"],
                                    "buckets": {"levels": list(range(1, 17))}})
    assert config.max_duration_seconds is None and config.concurrency is None
    assert config.load_test.schedule.duration_seconds == 15
    assert config.load_test.patterns.concurrent.levels == [1, 2, 3, 4]
    assert "load" in config.checks
    assert not any("duration reduced" in note or "cannot fit" in note for note in config._migration_notes)


def test_a4_save_config_and_manifest_roundtrip_null_limits(tmp_path, monkeypatch, capsys):
    saved = tmp_path / "config.json"

    async def run(config, **kwargs):
        return RunReport(run_id="unlimited", started_at="now", finished_at="now", overall="pass",
                         manifest={"config": config.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(BASE + ["--skip-preflight", "--save-config", str(saved), "--output", str(tmp_path / "run")]) == 0
    text = capsys.readouterr()
    assert "Maximum requests: Not set" in text.err
    assert "Simultaneous limit: Not set" in text.err
    assert "Maximum duration: Not set" in text.err
    values = json.loads(saved.read_text())
    assert values["config_version"] == 2
    assert values["max_requests"] is values["max_duration_seconds"] is values["concurrency"] is None
    loaded = configuration(parser().parse_args(["run", "--config", str(saved)]))
    manifest = configuration(parser().parse_args(["run", "--manifest", str(tmp_path / "run")]))
    assert loaded == manifest
    assert loaded.model_dump(mode="json") == values
    assert loaded._migration_notes == manifest._migration_notes == []
    assert not capsys.readouterr().err


@pytest.mark.parametrize("command", ["validate", "preview"])
def test_a4_null_limit_prints_not_set_and_serializes_null(command, tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_version": 2, "targets": [TARGET], "checks": ["correctness"],
                                "max_requests": None, "max_duration_seconds": None, "concurrency": None}))
    assert main([command, "--config", str(path), "--output-format", "text"]) == 0
    assert "Maximum requests: Not set" in capsys.readouterr().out
    assert main([command, "--config", str(path), "--output-format", "json"]) == 0
    values = json.loads(capsys.readouterr().out)["config"]
    assert values["config_version"] == 2
    assert values["max_requests"] is values["max_duration_seconds"] is values["concurrency"] is None


def test_versioned_examples_and_validation_configs_load_and_legacy_fixture_stays_legacy():
    for path in [*Path("examples").glob("*.yaml"), *Path("docs/validation").glob("*.json")]:
        values = yaml.safe_load(path.read_text())
        if "targets" not in values:
            continue
        assert values["config_version"] == 2, path
        assert migrated_configuration(values).config_version == 2, path
    legacy = yaml.safe_load(Path("tests/fixtures/legacy-concurrent-load.yaml").read_text())
    assert "config_version" not in legacy
    assert migrated_configuration(legacy).config_version == 2


def test_cli_concurrency_conflict_preserves_structured_fields_and_values(tmp_path, capsys):
    path = tmp_path / "conflict.json"
    path.write_text(json.dumps({"config_version": 2, "targets": [TARGET], "checks": ["correctness"],
                                "concurrency": 40, "test_options": {"correctness": {"concurrency": 64}}}))
    assert main(["validate", "--config", str(path), "--output-format", "json"]) == 4
    error = json.loads(capsys.readouterr().out)["errors"][0]
    assert error["type"] == "concurrency_conflict"
    assert error["fields"] == ["test_options.correctness.concurrency", "concurrency"]
    assert error["values"] == {"test_options.correctness.concurrency": 64, "concurrency": 40}


@pytest.mark.parametrize("identifier", [
    "E2-burst-10", "E3-qualification-c10", "E4-qualification-r10", "E4-poisson-r10",
    "E4-periodic-batch-10",
])
def test_validation_suite_keeps_legacy_compatible_configs_valid(identifier):
    from validation_suite.baseline import make_configuration
    from validation_suite.scenarios import scenario_matrix

    scenario = next(case for case in scenario_matrix() if case["id"] == identifier)
    values = make_configuration(scenario, "http://127.0.0.1:1/v1")
    assert "config_version" not in values
    assert all(values[name] is not None for name in ("concurrency", "max_requests", "max_duration_seconds"))
    config = migrated_configuration(values)
    assert config.default_concurrency == config.concurrency == scenario["concurrency"]
    assert config.max_requests == values["max_requests"]
    assert config.max_duration_seconds == values["max_duration_seconds"]
    assert config.checks == list(dict.fromkeys("load" if check in {"capacity", "arrivals", "concurrency"} else check for check in values["checks"]))
    assert config._migration_notes
    assert RunConfig.model_validate(config.model_dump()).model_dump() == config.model_dump()
