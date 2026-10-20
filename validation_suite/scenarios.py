"""Named, bounded experiments. Goldens and criteria precede candidate execution."""

from __future__ import annotations

import hashlib
import json
from typing import Any

LOADS = (1, 10, 50, 100, 200, 256)
RATES = (1, 10, 50, 100, 200)
CRITERIA_VERSION = "provisional-engineering-1"

EXECUTION_POLICY = {
    "maximum_active_seconds": 300,
    "excluded_experiments": ["E7"],
    "conditional_user_authorization_ceiling_seconds": 3600,
    "long_run_selection": "A necessary longer case requires a separately reviewed frozen selection; none is selected here.",
    "reason": "Current reviewed selection permits active durations up to 300 seconds and excludes E7.",
}

# Engineering tolerances, not an endpoint SLA. Fine stream timing has a separate
# precision budget and cannot inherit the much coarser dispatch health budget.
THRESHOLDS = {
    "launch_spread_ms": 100,
    "dispatch_lateness_p95_ms": 20,
    "dispatch_lateness_max_ms": 100,
    "rate_window_seconds": 1,
    "rate_window_relative_error": 0.10,
    "rate_window_count_allowance": 1,
    "receiver_spacing_relative_tolerance": 0.5,
    "receiver_spacing_coverage_fraction": 0.95,
    "occupancy_relative_tolerance": 0.10,
    "occupancy_time_coverage": 0.95,
    "ramp_exclusion_seconds": 2,
    "replacement_gap_p95_ms": 20,
    "replacement_gap_max_ms": 100,
    "active_duration_error_seconds": 0.25,
    "timing_error_ms": 1,
    "timing_relative_error": 0.10,
    "timing_coverage_fraction": 0.99,
    "reference_uncertainty_fraction_of_interval": 0.10,
    "queue_bound_records": 8192,
    "shutdown_deadline_seconds": 10,
    "memory_growth_bytes_per_hour": 64 * 1024**2,
    "missing_identities": 0,
    "duplicate_identities": 0,
    "corrupted_contents": 0,
    "false_successes": 0,
    "unaccounted_attempts": 0,
    "recording_losses": 0,
}

RESOURCE_POLICY = {
    "same_host": True,
    "physical_ram_bytes": 8 * 1024**3,
    "aggregate_owned_rss_stop_bytes": 3 * 1024**3,
    "candidate_rss_stop_bytes": 1536 * 1024**2,
    "minimum_free_disk_bytes": 10 * 1024**3,
    "study_storage_stop_bytes": 44 * 1024**3,
    "case_storage_stop_bytes": 6 * 1024**3,
    "max_case_requests": 250_000,
    "resource_sample_seconds": 1,
    "descriptor_sample_seconds": 10,
    "max_missing_resource_samples": 3,
    "endpoint_recorder": {"queue_rows": 2048, "queue_bytes": 16 * 1024**2,
                          "max_record_bytes": 256 * 1024},
    "product_report_decoder": {"request_value_bytes": 2 * 1024**2,
                               "metadata_value_bytes": 16 * 1024**2,
                               "read_chunk_bytes": 64 * 1024},
    "storage_projection_multiplier": 1.25,
    "storage_check_seconds": 1,
    "storage_accounting": "Apparent bytes of owned regular files and symlink entries; read-only symlink targets are not counted twice.",
    "shutdown_grace_seconds": 10,
    "sequential_candidates": True,
    "stop_scope": "Only subprocesses started by this driver; retain all partial evidence.",
}


def resource_policy() -> dict:
    """Capture the verifier's actual bounds, without a second copied definition."""
    from .verifier import verification_limits

    return {**RESOURCE_POLICY, "offline_verifier": verification_limits()}

CONCERNS = {
    "request_counts": ["E1", "E2", "E3", "E4", "E6"],
    "traffic_patterns": ["E2", "E3", "E4"],
    "duration": ["E3", "E4", "E7"],
    "response_completeness": ["E1", "E2", "E3", "E4", "E6"],
    "outcome_classification": ["E1", "E6"],
    "timing": ["E1", "E2", "E3", "E4", "E5", "E7"],
    "token_counts": ["E1", "E2", "E3", "E4"],
    "scoring": ["E1", "E2", "E3", "E4", "E5"],
    "interference": ["E5"],
    "resources": ["E2", "E3", "E4", "E5", "E6", "E7"],
    "failure_handling": ["E6"],
    "endurance": ["E7"],
    "reliable_capacity": ["E2", "E3", "E4", "E7"],
}


def baseline_fixture_goldens() -> dict:
    """Explicit baseline canaries, independent of its scorer and result export.

    Prompt/answer literals follow the pinned Apache-2.0 test fixtures. This
    catalogue is frozen before execution; observed output never populates it.
    """
    passage = (
        "The small boat crossed the lake early in the morning. The water was calm, "
        "and the trees along the shore were reflected on its surface. A family watched "
        "from the wooden pier as the boat approached. They carried a basket of bread, "
        "apples, and fresh water for their picnic. After tying the boat to the pier, "
        "the captain helped everyone aboard and checked that each person had a life jacket."
    )
    colors = "blue red green yellow orange silver violet brown white black".split()
    long_output = " ".join(colors[index % 10] for index in range(32))
    prefix, suffix = "Notes:\nThe box label is L8N2.\n", "\nReply with the box label only."
    filler_size = 4096 - len(prefix + suffix)
    filler = ("The box is gray. " * (filler_size // 17 + 1))[:filler_size]
    values = {
        "short.code.v2": ("The box label is K7P4. Reply with the box label only.", "K7P4"),
        "sustained.copy.v3": ("Copy exactly, with no extra text:\n" + passage, passage),
        "traffic.long_input.v1": (prefix + filler + suffix, "L8N2"),
        "traffic.long_output.v1": ("Copy exactly, with no extra text:\n" + long_output, long_output),
    }
    return {identifier: {"prompt": prompt, "content": answer, "outcome": "completed", "score": True,
                         "prompt_tokens": len(prompt.split()) + 8,
                         "completion_tokens": len(answer.split()), "prompt_bytes": len(prompt.encode()),
                         "response_bytes": len(answer.encode()),
                         "provenance": "Explicit pinned baseline fixture canary, independently specified; synthetic whitespace usage."}
            for identifier, (prompt, answer) in values.items()}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def poisson_offsets(scenario: dict):
    """Independent expected offsets for an explicitly selected scheduling contract.

    The original exponential-gap contract remains the default. PR #27 conditions
    arrivals on a fixed count; declare that difference before running either arm.
    """
    import random

    generator = random.Random(scenario["seed"])
    duration, rate = scenario["active_seconds"], scenario["rate_rps"]
    contract = scenario.get("poisson_schedule", "exponential_gaps")
    if contract == "conditioned_uniform":
        count = round(rate * duration)
        if count > 100_000:
            raise ValueError("Conditioned schedule exceeds the supported product plan limit")
        yield from sorted(generator.random() * duration for _ in range(count))
    elif contract == "exponential_gaps":
        offset = generator.expovariate(rate)
        while offset < duration:
            yield offset
            offset += generator.expovariate(rate)
    else:
        raise ValueError(f"Unknown Poisson scheduling contract: {contract}")


def _case(identifier: str, experiment: str, mode: str, **overrides: Any) -> dict:
    result = {
        "id": identifier, "experiment_id": experiment, "mode": mode,
        "purpose": identifier.replace("-", " "), "tier": "smoke", "repetitions": 1,
        "seed": 20261018, "active_seconds": 2, "request_count": 1,
        "concurrency": 1, "stream": True, "fixture": "normal", "response": "42",
        "event_delay_ms": 0, "first_delay_ms": 0,
        "prompt": "Return only the integer forty-two.", "max_output_tokens": 128,
        "request_timeout_seconds": 5, "healthy_capacity": True,
        "expected": {"content": "42", "outcome": "completed", "score": True,
                     "prompt_tokens": 14, "completion_tokens": 1, "event_contents": ["42"]},
    }
    result.update(overrides)
    if experiment == "E6" and mode == "serial":
        result["request_count"] = 1
    if mode == "fixed_rate" and result["healthy_capacity"] and result["fixture"] == "normal" and result["response"] != "auto":
        result["expected"] = {"content": result["response"], "score": True,
                              "prompt_tokens": len(result["prompt"].split()) + 8,
                              "completion_tokens": len(result["response"].split()), **result["expected"]}
    if result["expected"].get("prompt_tokens") is not None:
        result["expected"]["prompt_tokens"] = len(result["prompt"].split()) + 8
    if mode == "burst_pack":
        result["identity_policy"] = "Unique frozen request-pack fixture and Validation-ID marker per planned slot."
        result["expected"]["prompt_tokens"] = len(result["prompt"].split()) + 10
        result["request_prompt_bytes"] = len((result["prompt"] + "\nValidation-ID: 000000").encode())
    result["prompt_bytes"] = len(result["prompt"].encode())
    result["response_bytes"] = len(result["response"].encode())
    result["token_count_provenance"] = "Synthetic whitespace counts, not a model tokenizer."
    result["planned_semantics"] = "duration_admission_intent" if mode == "sustained_concurrency" else "finite"
    if result["response"] == "auto":
        result["response_bytes"] = None
        result["payload_size_provenance"] = "Exact per-fixture prompt/response byte lengths are in expected_by_fixture; actual wire bodies retained independently."
        result["expected_by_fixture"] = baseline_fixture_goldens()
        result["expected"].setdefault("score", True)
        if mode == "burst":
            if result["request_count"] > 1:
                result["prompt"] = baseline_fixture_goldens()["short.code.v2"]["prompt"]
                result["prompt_bytes"] = len(result["prompt"].encode())
                result["response_bytes"] = 4
            result["expected"] = {key: value for key, value in baseline_fixture_goldens()["short.code.v2"].items()
                                  if key in {"content", "outcome", "score", "prompt_tokens", "completion_tokens"}}
            if result["request_count"] == 1:
                result["expected"] = {"content": "42", "outcome": "completed", "score": True,
                                      "prompt_tokens": len(result["prompt"].split()) + 8, "completion_tokens": 1}
                result["scorer_expected"] = "42"
    return result


def scenario_matrix() -> list[dict]:
    cases = []
    fixtures = ("normal", "fragmented_utf8", "bundled", "reasoning", "tool_calls",
                "usage_trailer", "no_usage", "malformed_usage", "http_error",
                "stream_error_before", "stream_error_after", "malformed_stream",
                "missing_termination", "truncated", "incorrect")
    for fixture in fixtures:
        expected = {"content": "42", "outcome": "completed", "score": True,
                    "prompt_tokens": 14, "completion_tokens": 1}
        if fixture in {"http_error", "stream_error_before", "stream_error_after",
                       "malformed_stream", "missing_termination", "truncated"}:
            expected = {"outcome": "failed", "score": None}
        elif fixture == "incorrect":
            expected.update(content="INCORRECT", score=False)
        elif fixture == "fragmented_utf8":
            expected.update(content="café 🦒", completion_tokens=2)
        elif fixture == "tool_calls":
            expected = {"content": "", "outcome": "completed", "score": True}
        elif fixture in {"no_usage", "malformed_usage"}:
            expected.update(prompt_tokens=None, completion_tokens=None)
        cases.append(_case(f"E1-{fixture}", "E1", "serial", fixture=fixture,
                           response="café 🦒" if fixture == "fragmented_utf8" else "42",
                           expected=expected, healthy_capacity=fixture not in {
                               "http_error", "stream_error_before", "stream_error_after",
                               "malformed_stream", "missing_termination", "truncated", "incorrect"}))
    cases.append(_case("E1-nonstream", "E1", "serial", stream=False))
    for pace in (1, 5, 20):
        parts = ["one ", "two ", "three ", "four"]
        cases.append(_case(f"E1-paced-{pace}ms", "E1", "serial", response="".join(parts),
                           events=parts, event_delay_ms=pace, first_delay_ms=50,
                           expected={"content": "".join(parts), "outcome": "completed",
                                     "score": True, "completion_tokens": 4}))
    for count in LOADS:
        cases.append(_case(f"E2-burst-{count}", "E2", "burst", request_count=count,
                           concurrency=count, response="auto", expected={"outcome": "completed"}))
    cases.append(_case("E2-large-burst-10", "E2", "burst_pack", request_count=10,
                       concurrency=10, prompt="Synthetic padding. " * 3000,
                       response="word " * 2048, events=["word " * 128] * 16,
                       max_output_tokens=4096,
                       expected={"content": "word " * 2048, "outcome": "completed",
                                 "score": True, "completion_tokens": 2048}))
    # One independent main sweep, plus named size/pace/straggler cases: no product.
    for tier, duration, repeats in (("smoke", 2, 1), ("exploration", 60, 1),
                                     ("qualification", 300, 3)):
        for count in LOADS:
            cases.append(_case(f"E3-{tier}-c{count}", "E3", "sustained_concurrency",
                               tier=tier, active_seconds=duration, repetitions=repeats,
                               concurrency=count, request_count=None, response="auto",
                               event_count=4, event_delay_ms=320,
                               expected={"outcome": "completed"}))
        for rate in RATES:
            cases.append(_case(f"E4-{tier}-r{rate}", "E4", "fixed_rate", tier=tier,
                               active_seconds=duration, repetitions=repeats, rate_rps=rate,
                               request_count=int(rate * duration), concurrency=256))
    cases.extend([
        _case("E3-straggler-refill", "E3", "sustained_concurrency", concurrency=10,
              request_count=None, active_seconds=60, tier="exploration", response="auto",
              first_delay_ms=50, straggler_every=10, straggler_delay_ms=500,
              expected={"outcome": "completed"}),
        _case("E3-fast-zero-delay", "E3", "sustained_concurrency", concurrency=10,
              request_count=None, active_seconds=60, tier="exploration", response="auto",
              expected={"outcome": "completed"}),
        _case("E4-large-r10", "E4", "fixed_rate", rate_rps=10, concurrency=50,
              request_count=600, active_seconds=60, tier="exploration",
              prompt="Synthetic padding. " * 3000, response="word " * 2048,
              events=["word " * 128] * 16, max_output_tokens=4096,
              expected={"content": "word " * 2048, "outcome": "completed", "score": True,
                        "completion_tokens": 2048}),
        _case("E4-poisson-r10", "E4", "poisson", rate_rps=10, concurrency=50,
              request_count=None, active_seconds=60, tier="exploration"),
        _case("E4-periodic-batch-10", "E4", "periodic_batch", request_count=100,
              concurrency=50, batch_size=10, active_seconds=10, response="auto",
              expected={"outcome": "completed"}),
    ])
    for mode in ("sustained_concurrency", "fixed_rate"):
        for injector in ("control", "evaluation", "telemetry", "progress", "result_consumer",
                         "report_disk", "combined"):
            cases.append(_case(f"E5-{mode}-{injector}", "E5", mode, tier="exploration",
                               active_seconds=60, request_count=600 if mode == "fixed_rate" else None,
                               concurrency=10, rate_rps=10, interference=injector,
                               response="42" if mode == "fixed_rate" else "auto",
                               first_delay_ms=100,
                               expected={"outcome": "completed"}))
    for fault in ("http_error", "stream_error_before", "stream_error_after", "malformed_stream",
                  "hang", "disconnect", "connection_refused", "user_cancel", "bounded_storage",
                  "slow_storage", "evaluator_crash", "worker_crash", "controller_crash",
                  "terminate_before_persistence", "terminate_during_persistence", "global_budget"):
        cases.append(_case(f"E6-{fault}", "E6", "serial" if fault in {
            "http_error", "stream_error_before", "stream_error_after", "malformed_stream", "hang",
            "disconnect", "connection_refused"} else "fixed_rate", fault=fault,
            fixture=fault if fault in {"http_error", "stream_error_before", "stream_error_after",
                                      "malformed_stream", "hang", "disconnect"} else "normal",
            rate_rps=10, request_count=100, concurrency=10, active_seconds=10,
            first_delay_ms=200 if fault in {"user_cancel", "controller_crash"} else 0,
            healthy_capacity=False, expected={"outcome": "timeout" if fault == "hang" else "failed"}
            if fault in {"http_error", "stream_error_before", "stream_error_after", "malformed_stream",
                         "hang", "disconnect", "connection_refused"} else {},
            fault_expectations={"mechanism": fault, "require_injection_evidence": True,
                                "cleanup_deadline_seconds": 10,
                                "healthy_prior_completions_allowed": True}))
    for mode, load in (("fixed_rate", 1), ("fixed_rate", 10)):
        cases.append(_case(f"E7-{mode}-{load}", "E7", mode, tier="endurance",
                           active_seconds=3600, request_count=3600 * load,
                           concurrency=256, rate_rps=load, response="42",
                           expected={"outcome": "completed"},
                           selection_rule="Run a supported profile after matching 300-second repeats; "
                           "select the largest feasible frozen endurance profile, retain unrun alternative."))
    return cases


def validate_matrix(cases: list[dict], *, require_all_experiments: bool = True) -> None:
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Scenario identifiers must be unique")
    if require_all_experiments and {case["experiment_id"] for case in cases} != {f"E{i}" for i in range(1, 8)}:
        raise ValueError("All E1–E7 experiments are required")
    for case in cases:
        if type(case["repetitions"]) is not int or case["repetitions"] < 1:
            raise ValueError("Scenario repetitions must be positive integers")
        if type(case["active_seconds"]) not in (int, float) or not 0 < case["active_seconds"] <= 3600:
            raise ValueError("Scenario active duration must be positive and at most one hour")
        if type(case["concurrency"]) is not int or not 1 <= case["concurrency"] <= 256:
            raise ValueError("Scenario concurrency is outside baseline support")
        if case["tier"] == "qualification" and (case["repetitions"] < 3 or case["active_seconds"] != 300):
            raise ValueError("Qualification requires at least three 300-second repetitions")


def observer_calibration_cases(cases: list[dict]) -> list[dict]:
    """Matched short controls at the missing profile loads; never capacity tests."""
    selected = [case for case in cases if case["tier"] == "qualification" and
                ((case["experiment_id"] == "E3" and case["concurrency"] >= 50) or
                 (case["experiment_id"] == "E4" and case["rate_rps"] >= 10))]
    controls = []
    for case in selected:
        dimension = "c" if case["experiment_id"] == "E3" else "r"
        load = case["concurrency"] if dimension == "c" else case["rate_rps"]
        profile = f"{case['experiment_id']}-{dimension}{load}"
        for observed in (False, True):
            control = {**case, "id": f"{case['experiment_id']}-observer-{dimension}{load}-{'observed' if observed else 'unobserved'}",
                       "tier": "observer_calibration", "repetitions": 3, "active_seconds": 10,
                       "observer_enabled": observed, "timing_profile": profile,
                       "calibration_for": case["id"], "healthy_capacity": False,
                       "purpose": "Matched 10-second observation-overhead diagnostic; no duration/capacity transfer."}
            if control["mode"] == "fixed_rate":
                control["request_count"] = control["rate_rps"] * 10
            controls.append(control)
    return controls
