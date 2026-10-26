"""Speculative-decoding observations use only matched fresh counter windows."""

import json

import pytest

from giraffe import telemetry
from giraffe.models import RunConfig, Target


NAMES = {
    "drafts": "vllm:spec_decode_num_drafts_total",
    "draft_tokens": "vllm:spec_decode_num_draft_tokens_total",
    "accepted_tokens": "vllm:spec_decode_num_accepted_tokens_total",
}
POSITION = "vllm:spec_decode_num_accepted_tokens_per_pos_total"


def body(drafts, proposed, accepted, *, engine="0", positions=()):
    labels = f'model_name="model",engine="{engine}"'
    return "\n".join(
        [f'{name}{{{labels}}} {value}' for name, value in zip(NAMES.values(), (drafts, proposed, accepted))]
        + [f'{POSITION}{{{labels},position="{position}"}} {value}' for position, value in enumerate(positions)]
    ) + "\n"


def snapshot(text, elapsed, profile="vllm-v1"):
    target = Target(name="test", url="http://inference/v1", model="test", metrics_profile=profile)
    return telemetry.parse(text, target, RunConfig(targets=[target], metrics=True)) | {
        "elapsed_seconds": elapsed, "phase": "load", "collected_at": str(elapsed)}


def derive(*snapshots):
    return telemetry.derive_server_metrics({"snapshots": list(snapshots)})


def test_h100_nonzero_initial_counters_produce_window_counts_and_ratios():
    first = snapshot(body(100, 300, 200, positions=(90, 70, 40)), 0)
    last = snapshot(body(2096, 6288, 5242, positions=(2024, 1674, 1544)), 10)
    rows = derive(first, last)
    assert len(first["samples"]) == 6
    for key, value in [("drafts", 1996), ("draft_tokens", 5988), ("accepted_tokens", 5042),
                       ("proposal_length", 3), ("acceptance_ratio", 5042 / 5988)]:
        row = rows[f"spec_decode_{key}"]
        assert row["value"] == pytest.approx(value)
        assert row["scope"] == "server-wide"
        assert row["details"]["activity"] == "observed"
        assert row["details"]["observed_intervals"] == 1
    positions = rows["spec_decode_accepted_tokens"]["details"]["accepted_tokens_by_position"]
    assert [p["accepted_tokens"] for p in positions] == [1934, 1604, 1504]
    assert positions[0]["acceptance_ratio"] == pytest.approx(1934 / 1996)


def test_optional_counters_do_not_add_serving_requirements_or_change_legacy():
    serving = ("vllm:kv_cache_usage_perc 0\nvllm:num_requests_running 0\n"
               "vllm:num_requests_waiting 0\nvllm:num_preemptions_total 0\n")
    parsed = snapshot(serving, 0)
    assert parsed["status"] == "available" and parsed["missing_families"] == []
    assert derive(parsed)["spec_decode_drafts"]["value"] is None
    legacy = snapshot(body(10, 30, 20), 0, "vllm-legacy")
    assert legacy["samples"] == []
    assert derive(legacy)["spec_decode_drafts"]["state"] == "not_supported"
    detected = snapshot(body(10, 30, 20), 0, "unknown")
    assert detected["profile"] == "vllm-v1"
    assert len(detected["samples"]) == 3


def test_zero_activity_differs_from_no_compatible_observation():
    first, last = snapshot(body(10, 30, 20), 0), snapshot(body(10, 30, 20), 1)
    rows = derive(first, last)
    assert rows["spec_decode_drafts"]["value"] == 0
    assert rows["spec_decode_drafts"]["details"]["activity"] == "not_observed"
    assert rows["spec_decode_acceptance_ratio"]["value"] is None
    assert rows["spec_decode_proposal_length"]["value"] is None
    missing = derive(first)["spec_decode_drafts"]
    assert missing["value"] is None and missing["details"]["activity"] == "unknown"


def test_multiple_engines_pool_counts_not_ratios_and_position_denominators_match():
    first = snapshot(body(100, 100, 50, engine="a", positions=(50,))
                     + body(100, 200, 100, engine="b", positions=(60, 40)), 0)
    last = snapshot(body(102, 102, 51, engine="a", positions=(51,))
                    + body(108, 216, 112, engine="b", positions=(68, 44)), 2)
    rows = derive(first, last)
    assert rows["spec_decode_acceptance_ratio"]["value"] == pytest.approx(13 / 18)
    assert rows["spec_decode_proposal_length"]["value"] == 1.8
    positions = rows["spec_decode_accepted_tokens"]["details"]["accepted_tokens_by_position"]
    assert positions == [
        {"position": 0, "accepted_tokens": 9, "drafts": 10, "acceptance_ratio": .9},
        {"position": 1, "accepted_tokens": 4, "drafts": 8, "acceptance_ratio": .5},
    ]


@pytest.mark.parametrize("problem", ["stale", "failed", "truncated", "missing", "label_change", "mismatched", "duplicate", "negative", "reset"])
def test_bad_middle_scrape_never_bridges_raw_or_derived_counters(problem):
    snapshots = [snapshot(body(10, 30, 20, positions=(10, 7, 3)), 0),
                 snapshot(body(20, 60, 40, positions=(20, 14, 6)), 1),
                 snapshot(body(30, 90, 60, positions=(30, 21, 9)), 2)]
    middle = snapshots[1]
    if problem == "stale":
        middle["samples"][0]["stale"] = True
    elif problem == "failed":
        middle["status"] = "connection_failure"
    elif problem == "truncated":
        middle["series_limit_reached"] = True
    elif problem == "missing":
        middle["samples"].pop(0)
    elif problem == "label_change":
        for sample in middle["samples"]:
            sample["labels"]["engine"] = "new"
    elif problem == "mismatched":
        middle["samples"][0]["labels"]["engine"] = "other"
    elif problem == "duplicate":
        middle["samples"].append(middle["samples"][0].copy())
    elif problem == "negative":
        middle["samples"][0]["value"] = -1
    else:
        # First interval resets; second interval resets a different member.
        middle["samples"][0]["value"] = 1
        middle["samples"][1]["value"] = 100
    result = telemetry.summarize({"snapshots": snapshots}, .9)
    row = result["derived"]["spec_decode_drafts"]
    assert row["value"] is None
    assert row["details"]["excluded_intervals"] == 2
    assert all(row["delta"] is None for row in result["series"])


def test_valid_windows_recover_after_reset_with_explicit_partial_coverage():
    snapshots = [snapshot(body(10, 30, 20, positions=(10, 7, 3)), 0),
                 snapshot(body(20, 60, 40, positions=(20, 14, 6)), 1),
                 snapshot(body(0, 0, 0, positions=(0, 0, 0)), 2),
                 snapshot(body(5, 15, 10, positions=(5, 3, 2)), 4)]
    result = telemetry.summarize({"snapshots": snapshots}, .9)
    row = result["derived"]["spec_decode_drafts"]
    assert row["value"] == 15
    assert row["details"]["observed_intervals"] == 2
    assert row["details"]["excluded_intervals"] == 1
    assert row["details"]["partial_coverage"] is True
    assert row["details"]["duration_seconds"] == 3
    assert row["details"]["first_elapsed_seconds"] == 0
    assert row["details"]["last_elapsed_seconds"] == 4
    assert next(r for r in result["series"] if r["family"] == "spec_decode_drafts")["delta"] == 15


@pytest.mark.parametrize("bad_counts", [(0, 1, 0), (1, 1, 2)])
def test_invalid_engine_cannot_be_hidden_by_other_engines(bad_counts):
    first = snapshot(body(0, 0, 0, engine="a") + body(0, 0, 0, engine="b"), 0)
    last = snapshot(body(*bad_counts, engine="a") + body(100, 300, 200, engine="b"), 1)
    assert derive(first, last)["spec_decode_drafts"]["value"] is None


@pytest.mark.parametrize("problem", ["missing_engine", "missing_position", "mismatched_sum", "nonmonotone", "exceeds_drafts", "reset", "stale", "invalid_position"])
def test_optional_position_failures_preserve_core_counts(problem):
    first = snapshot(body(10, 30, 20, positions=(10, 7, 3)), 0)
    last = snapshot(body(20, 60, 40, positions=(20, 14, 6)), 1)
    if problem == "missing_engine":
        last["samples"][3]["labels"]["engine"] = "other"
    elif problem == "missing_position":
        first["samples"].pop(4)
        last["samples"].pop(4)
    elif problem == "mismatched_sum":
        last["samples"][3]["value"] = 19
    elif problem == "nonmonotone":
        last["samples"][3]["value"] = 15
        last["samples"][4]["value"] = 17
        last["samples"][5]["value"] = 8
    elif problem == "exceeds_drafts":
        last["samples"][3]["value"] = 21
        last["samples"][4]["value"] = 13
    elif problem == "reset":
        last["samples"][5]["value"] = 2
    elif problem == "stale":
        last["samples"][3]["stale"] = True
    else:
        for snap in (first, last):
            snap["samples"][3]["labels"]["position"] = "-1"
    result = telemetry.summarize({"snapshots": [first, last]}, .9)
    row = result["derived"]["spec_decode_accepted_tokens"]
    assert row["value"] == 20
    assert row["details"]["accepted_tokens_by_position"] == []
    assert row["details"]["position_coverage"]["excluded_intervals"] == 1
    assert all(r["delta"] is None for r in result["series"] if r["name"] == POSITION)


def test_zero_denominator_position_rate_is_unavailable():
    rows = derive(snapshot(body(10, 30, 20, positions=(10, 7, 3)), 0),
                  snapshot(body(10, 30, 20, positions=(10, 7, 3)), 1))
    positions = rows["spec_decode_accepted_tokens"]["details"]["accepted_tokens_by_position"]
    assert len(positions) == 3
    assert all(p["accepted_tokens"] == 0 and p["acceptance_ratio"] is None for p in positions)


def test_overflowing_engine_pool_is_unavailable_instead_of_nan_or_infinity():
    first = snapshot(body(0, 0, 0, engine="a") + body(0, 0, 0, engine="b"), 0)
    last = snapshot(body(1e308, 1e308, 1e308, engine="a")
                    + body(1e308, 1e308, 1e308, engine="b"), 1)
    rows = derive(first, last)
    assert rows["spec_decode_drafts"]["value"] is None
    assert rows["spec_decode_acceptance_ratio"]["value"] is None


@pytest.mark.parametrize("counts", [(1e-308, 1e308, 1), (1, 1.5, 1), (1, 2, .5)])
def test_fractional_speculative_count_populations_are_unavailable(counts):
    first, last = snapshot(body(0, 0, 0), 0), snapshot(body(*counts), 1)
    rows = derive(first, last)
    assert rows["spec_decode_drafts"]["value"] is None
    assert rows["spec_decode_proposal_length"]["value"] is None
    json.dumps(rows, allow_nan=False)


def test_fractional_position_counts_do_not_invalidate_integral_core():
    first = snapshot(body(0, 0, 0, positions=(0, 0)), 0)
    last = snapshot(body(2, 4, 2, positions=(1.5, .5)), 1)
    row = derive(first, last)["spec_decode_accepted_tokens"]
    assert row["value"] == 2
    assert row["details"]["accepted_tokens_by_position"] == []
