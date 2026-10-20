from validation_suite.scenarios import (CONCERNS, LOADS, RATES, baseline_fixture_goldens,
                                        observer_calibration_cases, scenario_matrix, validate_matrix)
import pytest


def test_matrix_contains_required_sweeps_durations_and_concerns():
    cases = scenario_matrix()
    validate_matrix(cases)
    assert len(CONCERNS) == 13
    assert {case["request_count"] for case in cases if case["experiment_id"] == "E2"
            and case["mode"] == "burst"} == set(LOADS)
    assert {case["concurrency"] for case in cases if case["experiment_id"] == "E3"
            and case["tier"] == "exploration"} == set(LOADS)
    assert {case["rate_rps"] for case in cases if case["experiment_id"] == "E4"
            and case["mode"] == "fixed_rate" and case["tier"] == "exploration"} == set(RATES)
    assert {case["active_seconds"] for case in cases if case["tier"] == "qualification"} == {300}
    assert {case["repetitions"] for case in cases if case["tier"] == "qualification"} == {3}
    assert {case["active_seconds"] for case in cases if case["experiment_id"] == "E7"} == {3600}
    assert {case["event_delay_ms"] for case in cases if case["id"].startswith("E1-paced-")} == {1, 5, 20}


def test_manifest_uses_independent_fixture_goldens_and_preserves_auto_output():
    lookup = {case["id"]: case for case in scenario_matrix()}
    assert lookup["E1-incorrect"]["expected"]["content"] == "INCORRECT"
    assert lookup["E1-incorrect"]["expected"]["score"] is False
    assert lookup["E1-fragmented_utf8"]["response"] == "café 🦒"
    assert lookup["E1-normal"]["expected"]["prompt_tokens"] == 13
    for case in lookup.values():
        if case["response"] == "auto":
            assert "events" not in case
    assert lookup["E3-smoke-c10"]["event_count"] == 4
    assert lookup["E3-smoke-c10"]["event_delay_ms"] == 320


def test_fixed_rate_and_periodic_batch_have_distinct_names():
    lookup = {case["id"]: case for case in scenario_matrix()}
    assert lookup["E4-periodic-batch-10"]["mode"] == "periodic_batch"
    assert lookup["E4-poisson-r10"]["mode"] == "poisson"
    assert lookup["E7-fixed_rate-1"]["rate_rps"] == 1


def test_load_goldens_cover_builtin_fixture_families_without_runtime_results():
    goldens = baseline_fixture_goldens()
    assert goldens["short.code.v2"]["content"] == "K7P4"
    assert goldens["short.code.v2"]["prompt_tokens"] == 19
    assert goldens["traffic.long_input.v1"]["prompt_bytes"] == 4096
    assert goldens["traffic.long_input.v1"]["prompt_tokens"] == 970
    assert goldens["traffic.long_output.v1"]["completion_tokens"] == 32
    assert goldens["sustained.copy.v3"]["completion_tokens"] == 70
    assert all(value["score"] is True for value in goldens.values())


def test_observer_controls_preserve_load_geometry_and_have_explicit_short_scope():
    cases = scenario_matrix()
    lookup = {case["id"]: case for case in cases}
    controls = observer_calibration_cases(cases)
    assert len(controls) == 16
    assert sum(case["repetitions"] for case in controls) == 48
    for control in controls:
        original = lookup[control["calibration_for"]]
        assert control["active_seconds"] == 10
        for key in ["mode", "concurrency", "stream", "fixture", "event_count", "event_delay_ms", "expected", "expected_by_fixture"]:
            assert control.get(key) == original.get(key)
        assert control["healthy_capacity"] is False


def test_explicit_subset_does_not_claim_complete_experiment_coverage():
    subset = [scenario_matrix()[0]]
    validate_matrix(subset, require_all_experiments=False)
    with pytest.raises(ValueError, match="All E1–E7"):
        validate_matrix(subset)


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), 3601])
def test_subset_rejects_invalid_duration(value):
    subset = [{**scenario_matrix()[0], "active_seconds": value}]
    with pytest.raises(ValueError, match="active duration"):
        validate_matrix(subset, require_all_experiments=False)
