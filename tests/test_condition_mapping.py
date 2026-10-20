"""Configured test conditions group requests without losing their scenario detail."""

import pytest

from giraffe.conditions import condition_for


def test_matrix_cell_is_one_condition_with_plain_shapes_and_level():
    assert condition_for("bucket_c32_short_short", {}) == {
        "id": "bucket_c32_short_short", "label": "Short input / short output · 32 at once",
        "check": "buckets", "level": 32, "input": "short", "output": "short",
    }


def test_consistency_prompt_indices_are_details_within_level_and_shape():
    first = condition_for("consistency_c32_long_p0", {"prompt_index": 0})
    assert first == condition_for("consistency_c32_long_p19", {"prompt_index": 19})
    assert first == condition_for("consistency_c32_long", {})
    assert first == {"id": "consistency_c32_long", "label": "Long input consistency · 32 at once",
                     "check": "consistency", "level": 32, "input": "long"}
    assert first["id"] != condition_for("consistency_c1_long_p0", {})["id"]


@pytest.mark.parametrize("kind", ["long_input", "long_output"])
@pytest.mark.parametrize("phase", ["before_output", "active_stream"])
def test_mixed_short_and_heavy_share_one_condition(kind, phase):
    short = condition_for(f"mixed_short_{kind}_{phase}", {})
    heavy = condition_for(f"mixed_heavy_{kind}_{phase}", {"level": 2})
    assert short == heavy
    assert short["id"] == f"mixed_{kind}_{phase}"
    assert short["check"] == "mixed"
    assert "interference" in short["label"]
    assert short["id"] != condition_for("mixed_control", {})["id"]


def test_sessions_group_all_turns_and_sessions_by_mode():
    assert condition_for("session_live_turn0", {"session_id": "a"}) == condition_for(
        "session_live_turn8", {"session_id": "b"})
    assert condition_for("session_live_turn0", {}) == {
        "id": "session_live", "label": "Live sessions", "check": "sessions"}
    assert condition_for("session_fixed_turn0", {})["id"] == "session_fixed"


@pytest.mark.parametrize("mode", ["complete", "stream"])
def test_tool_conversation_turns_group_by_fixture_and_mode(mode):
    first = condition_for(f"tools_weather_result_{mode}_turn0", {})
    assert first == condition_for(f"tools_weather_result_{mode}_turn12", {"turn_id": 12})
    assert first == {"id": f"tools_weather_result_{mode}",
                     "label": f"Weather result conversation · {mode}", "check": "tools"}
    assert first["id"] != condition_for(f"tools_other_{mode}_turn0", {})["id"]


def test_arrivals_group_by_configured_rate():
    first = condition_for("arrival", {"requests_per_second": 2})
    assert first == condition_for("arrival", {"requests_per_second": 2.0, "arrival_index": 9})
    assert first == {"id": "arrival_r2", "label": "Arrivals · 2 per second", "check": "arrivals"}
    assert first["id"] != condition_for("arrival", {"requests_per_second": 4})["id"]
    assert first["id"] != condition_for("arrival", {"requests_per_second": 2.0000001})["id"]


def test_token_stress_keeps_each_case_and_level_separate():
    assert condition_for("stress_long_input_summary_c8", {}) == {
        "id": "stress_long_input_summary_c8", "label": "Long input summary · 8 at once",
        "check": "token_stress", "level": 8,
    }
    assert condition_for("stress_calibration_long_input_summary", {})["check"] == "token_stress"


@pytest.mark.parametrize("scenario,label,check", [
    ("initial", "First request", "access"), ("warm", "Warm-up", "serving"),
    ("correctness_c32", "Answer correctness · 32 at once", "correctness"),
    ("json_c4", "Structured JSON · 4 at once", "json"),
    ("context", "Long-context recall", "context"),
    ("client_deadline", "Client deadline", "cancellation"),
    ("fairness_short", "Short requests with competing load", "fairness"),
    ("prefix_shared", "Shared prefix", "prefix"),
    ("tools_custom_auto_stream", "Tools custom auto stream", "tools"),
    ("new_custom_scenario", "New custom scenario", "serving"),
])
def test_other_scenarios_keep_their_own_condition(scenario, label, check):
    condition = condition_for(scenario, None)
    assert condition["id"] == scenario
    assert condition["label"] == label
    assert condition["check"] == check


def test_traffic_stages_stay_distinct_and_use_plain_labels():
    assert condition_for("traffic_stage_0", {}) == {
        "id": "traffic_stage_0", "label": "Traffic stage 1", "check": "capacity"}


def test_mapping_does_not_mutate_workload_metadata():
    workload = {"level": 32, "input_bucket": "short", "output_bucket": "long"}
    original = workload.copy()
    condition_for("bucket_c32_short_long", workload)
    assert workload == original
