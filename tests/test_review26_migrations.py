"""Saved legacy request settings retain the values sent by pre-v2 Giraffe."""

import pytest
from pydantic import ValidationError

from giraffe.models import (RunConfig, is_legacy_config, migrate_concurrent_load,
                            migrate_run_limits, required_concurrency)

TARGET = {"name": "one", "url": "http://local", "model": "m"}


def saved(**values):
    value, notes = migrate_run_limits({"targets": [TARGET], **values})
    value, load_notes = migrate_concurrent_load(value)
    return RunConfig.model_validate(value), notes + load_notes


@pytest.mark.parametrize("ceiling,targets,levels", [(40, 1, [1, 4]), (8, 1, [1, 4]),
    (2, 1, [1]), (4, 2, [1]), (16, 2, [1, 4]), (None, 2, [1])])
def test_legacy_concurrent_load_inherited_levels_match_old_main(ceiling, targets, levels):
    values = {"checks": ["concurrency"], "targets": [
        {**TARGET, "name": str(n)} for n in range(targets)], "overlap_models": targets > 1}
    if ceiling is not None:
        values["concurrency"] = ceiling
    config, notes = saved(**values)
    assert config.load_test.patterns.concurrent.levels == levels
    assert any(f"legacy schedule.levels={levels!r}" in note for note in notes)
    assert RunConfig.model_validate(config.model_dump()).load_test.patterns.concurrent.levels == levels


@pytest.mark.parametrize("default,ceiling,levels", [(40, None, [1, 4]), (40, 2, [1]),
    (1, None, [1, 4]), (64, 40, [1, 4])])
def test_v2_default_load_levels_do_not_follow_default_parallelism(default, ceiling, levels):
    config = RunConfig(targets=[TARGET], checks=["concurrency"], default_concurrency=default,
                       concurrency=ceiling)
    assert config.load_test.patterns.concurrent.levels == levels


@pytest.mark.parametrize("ceiling,targets,levels", [(2, 1, [2]), (None, 1, [3, 4]),
    (4, 2, [2]), (1, 1, [1])])
def test_old_load_migration_bounds_explicit_levels(ceiling, targets, levels):
    values = {"checks": ["buckets"], "targets": [{**TARGET, "name": str(n)} for n in range(targets)],
              "overlap_models": targets > 1, "concurrent_workloads": {"levels": [3, 8]}}
    if ceiling is not None:
        values["concurrency"] = ceiling
    config, notes = saved(**values)
    assert config.load_test.patterns.concurrent.levels == levels
    assert any("bounded by Limit simultaneous requests" in note for note in notes)


@pytest.mark.parametrize("cap,outputs", [(8, [8, 8, 8]), (64, [32, 64, 64])])
def test_legacy_inherited_profile_output_budgets_retain_old_cap(cap, outputs):
    config, notes = saved(checks=["concurrency"], max_output_tokens=cap)
    assert [getattr(config.load_test.profile, size).output_tokens
            for size in ("short", "medium", "long")] == outputs
    assert any("clipped to legacy max_output_tokens" in note for note in notes)
    assert RunConfig.model_validate(config.model_dump()).load_test.profile == config.load_test.profile
    fresh = RunConfig(targets=[TARGET], checks=["concurrency"], max_output_tokens=cap)
    assert [getattr(fresh.load_test.profile, size).output_tokens
            for size in ("short", "medium", "long")] == [32, 64, 128]


@pytest.mark.parametrize("check", ["buckets", "token_stress"])
def test_legacy_migrated_profile_explicit_output_budgets_retain_old_cap(check):
    config, notes = saved(checks=[check], max_output_tokens=64,
                         concurrent_workloads={"output_tokens": {"short": 16, "medium": 48, "long": 256}})
    assert [getattr(config.load_test.profile, size).output_tokens
            for size in ("short", "medium", "long")] == [16, 48, 64]
    assert any("output_tokens.long=256 copied to profile.long.output_tokens=64; clipped" in note for note in notes)


@pytest.mark.parametrize("marker", [{"default_concurrency": 7}, {"arrivals": {"max_in_flight": 7}},
                                    {"config_version": 2}])
def test_any_v2_only_key_identifies_new_inputs(marker):
    values = {"targets": [TARGET], "concurrency": 40, **marker}
    assert not is_legacy_config(values)
    result, notes = migrate_run_limits(values)
    assert result == values and notes == []


@pytest.mark.parametrize("settings", [{}, {"sessions": {"sessions": 3}, "consistency": {"repetitions": 3}}])
def test_legacy_overlap_default_sessions_and_consistency_keep_old_values(settings):
    config, notes = saved(targets=[TARGET, {**TARGET, "name": "two"}, {**TARGET, "name": "three"}],
        checks=["sessions", "consistency"], overlap_models=True, concurrency=4, **settings)
    assert config.sessions.concurrency == 2 and config.consistency.levels == [1, 2]
    assert required_concurrency(config)["by_check"] == {"sessions": 2, "consistency": 2}
    assert any("sessions legacy defaults" in note for note in notes)
    assert any("consistency legacy defaults" in note for note in notes)


def test_sessions_conflict_uses_requests_actually_possible():
    config = RunConfig(targets=[TARGET], checks=["sessions"], concurrency=2,
                       sessions={"concurrency": 8, "sessions": 2})
    assert config.sessions.concurrency == 8
    assert required_concurrency(config)["by_check"]["sessions"] == 2


def test_inherited_load_overlap_error_reports_effective_level_and_levels_field():
    with pytest.raises(ValidationError) as error:
        RunConfig(targets=[TARGET, {**TARGET, "name": "two"}], checks=["concurrency"],
                  overlap_models=True, concurrency=1)
    detail = error.value.errors()[0]
    assert detail["ctx"]["fields"] == ["load_test.patterns.concurrent.levels", "concurrency"]
    assert detail["msg"] == "Concurrency levels exceed the run ceiling: 1 × 2 endpoints > 1. Update Run limits or lower the levels."
    assert detail["ctx"]["values"] == {"load_test.patterns.concurrent.levels": [1], "concurrency": 1}


@pytest.mark.parametrize("options", [{"samples": 3}, {"concurrency": 4},
                                    {"max_output_tokens": 8}, {"request_timeout_seconds": 5}])
def test_request_pack_correctness_overrides_fail_during_validation(options):
    with pytest.raises(ValidationError) as error:
        RunConfig(targets=[TARGET], checks=["correctness"], request_source="request_pack",
                  request_pack={"version": 1, "cases": [{"fixture_id": "case",
                      "messages": [{"role": "user", "content": "hi"}], "scorer": "none"}]},
                  test_options={"correctness": options})
    assert error.value.errors()[0]["ctx"]["fields"] == [f"test_options.correctness.{next(iter(options))}"]


def test_access_timeout_owns_initial_but_not_warm_requests():
    config = RunConfig(targets=[TARGET], checks=["access", "serving"],
        test_options={"access": {"request_timeout_seconds": 4},
                      "serving": {"request_timeout_seconds": 9}})
    assert config.effective_initial()["request_timeout_seconds"] == 4
    assert config.effective_response()["request_timeout_seconds"] == 9


@pytest.mark.parametrize("settings", [None, {}, {"max_tokens": 64},
    {"max_tokens": 64, "workloads": [{"name": "chat", "turns": ["Hi", "Again"], "max_tokens": 96}]}])
def test_legacy_session_output_budgets_retain_old_runtime_cap(settings):
    config, notes = saved(checks=["sessions"], max_output_tokens=16, sessions=settings)
    assert config.sessions.max_tokens == 16
    assert all(workload.max_tokens == 16 for workload in config.sessions.workloads)
    assert any("sessions.max_tokens=" in note and "legacy max_output_tokens=16" in note for note in notes)
    fresh = RunConfig(targets=[TARGET], checks=["sessions"], max_output_tokens=16,
                      sessions={"max_tokens": 64})
    assert fresh.sessions.max_tokens == 64


def test_legacy_sparse_profile_inherits_neighbor_constrained_output_values():
    config, notes = saved(checks=["concurrency"],
                         concurrent_load={"profile": {"medium": {"output_tokens": 24}}})
    assert [getattr(config.load_test.profile, size).output_tokens
            for size in ("short", "medium", "long")] == [24, 24, 128]
    assert any("legacy neighboring profile values" in note for note in notes)


def test_legacy_note_names_only_checks_actually_inheriting_parallelism():
    _, notes = saved(checks=["correctness", "sessions", "consistency", "capacity"], concurrency=40,
                     test_options={"correctness": {"concurrency": 2}})
    note = next(note for note in notes if note.startswith("concurrency: 40"))
    assert "Capacity retain legacy 40-request parallelism" in note
    assert "Known-answer correctness" not in note and "Multi-turn sessions" not in note


def test_legacy_pack_case_budgets_are_materialized_before_runtime_clamp_removal():
    values = {"targets": [TARGET], "checks": ["correctness"], "request_source": "request_pack",
              "max_output_tokens": 16, "request_pack": {"version": 1, "cases": [
                  {"fixture_id": "large", "messages": [{"role": "user", "content": "hi"}],
                   "scorer": "none", "max_tokens": 96},
                  {"fixture_id": "small", "messages": [{"role": "user", "content": "hi"}],
                   "scorer": "none", "max_tokens": 8}]}}
    migrated, notes = migrate_run_limits(values)
    config = RunConfig.model_validate(migrated)
    assert [case.max_tokens for case in config.request_pack.cases] == [16, 8]
    assert values["request_pack"]["cases"][0]["max_tokens"] == 96
    assert any("case 'large' max_tokens=96 preserved as 16" in note for note in notes)
    fresh = RunConfig.model_validate({**values, "config_version": 2})
    assert [case.max_tokens for case in fresh.request_pack.cases] == [96, 8]


def test_legacy_custom_fixture_cap_survives_export_without_reading_the_fixture():
    config, notes = saved(checks=["correctness"], max_output_tokens=16,
                         custom_fixtures="/a/path/that/does/not/exist.json")
    assert config.legacy_custom_fixture_max_output_tokens == 16
    assert any("Custom-fixture output budgets retain legacy max_output_tokens=16" in note for note in notes)
    restored = RunConfig.model_validate(config.model_dump())
    assert restored.legacy_custom_fixture_max_output_tokens == 16
    fresh = RunConfig(targets=[TARGET], checks=["correctness"], max_output_tokens=16,
                      custom_fixtures=config.custom_fixtures)
    assert fresh.legacy_custom_fixture_max_output_tokens is None
