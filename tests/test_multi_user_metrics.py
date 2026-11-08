"""Shared user windows retain individual delivery and unfinished experience."""
from copy import deepcopy

import pytest

from giraffe.models import MultiUserCriteria, RequestRecord
from giraffe.multi_user_metrics import summarize


def record(user, *, identity=None, dispatch=0, end=1000, tokens=100, **changes):
    values = dict(id=identity or f"request-{user}", target="service", fixture_id="load.short_short",
                  scenario="multi_user_2_measurement", check_ids=["multi_user"],
                  started_at="2026-11-05T00:00:00Z", status="completed", valid=True,
                  dispatch_ms=dispatch, timing_start_ms=dispatch, completed_ms=end,
                  elapsed_ms=end-dispatch, first_output_ms=50, output_tokens=tokens,
                  input_tokens=100, requested_output_tokens=100, requested_max_tokens=100,
                  finish_reason="length", workload={"user_id": user,
                  "window_id": "multi_user_2_measurement", "phase": "measurement",
                  "population": 2, "profile": "short_short", "ready_ms": dispatch})
    values.update(changes)
    return RequestRecord(**values)


def observation(records=(), *, users=("user-01", "user-02"), duration=10, criteria=None):
    population = [{"user_id": user, "profile": "short_short"} for user in users]
    return {"version": 1, "window_policy": "completed-output-v1", "population": population,
            "settings": {"pass_criteria": MultiUserCriteria(**(criteria or {})).model_dump()},
            "windows": [{"id": "multi_user_2_measurement", "scenario": "repeated_requests",
              "population": len(users), "phase": "measurement", "start_ms": 0,
              "end_ms": duration*1000, "planned_end_ms": duration*1000,
              "duration_seconds": duration, "planned_duration_seconds": duration,
              "cut_short": False, "reason": None, "drain_reason": None,
              "users": [{**user, "activated_ms": 0,
                         "ready_count": sum(r.workload["user_id"] == user["user_id"] for r in records),
                         "dispatched_count": sum(r.workload["user_id"] == user["user_id"] for r in records),
                         "request_ids": [r.id for r in records if r.workload["user_id"] == user["user_id"]],
                         "pending_ready_ms": None, "pending_observed_ms": None,
                         "stop_reason": None} for user in population]}]}


def row(result, user="user-01"):
    return next(user_row for user_row in result["windows"][0]["users"] if user_row["user_id"] == user)


def test_equal_delivered_tokens_use_common_window_not_individual_activity():
    records = [record("user-01", end=1000), record("user-02", end=9000)]
    source = observation(records)
    before = deepcopy(source)
    result = summarize(source, records)
    assert row(result)["output_tokens_per_second"] == 10
    assert row(result, "user-02")["output_tokens_per_second"] == 10
    assert row(result)["latency"]["ready_completion_ms"]["max"] == 1000
    assert row(result, "user-02")["latency"]["ready_completion_ms"]["max"] == 9000
    assert result["status"] == "pass"
    assert source == before


def test_failure_during_drain_survives_healthy_peers_and_incomplete_window():
    records = [record("user-01", identity=f"ok-{i}", dispatch=i*80, end=i*80+50)
               for i in range(99)]
    records.append(record("user-02", dispatch=9900, end=10500, status="failed", valid=False))
    source = observation(records)
    source["windows"][0]["drain_reason"] = "cancelled by user"
    result = summarize(source, records)
    assert result["status"] == "fail"
    assert row(result, "user-02")["status"] == "fail"
    assert row(result, "user-02")["outcomes"]["errors"] == 1
    assert result["worst_user"]["user_id"] == "user-02"


def test_late_completion_is_excluded_from_tokens_but_kept_in_timing_attempts():
    records = [record("user-01", dispatch=9900, end=10500)]
    result = summarize(observation(records, users=("user-01",),
        criteria={"max_completion_ms": 500}), records)
    user = row(result)
    assert user["output_tokens_per_second"] == 0
    assert user["late_output_tokens"] == 100
    assert user["criteria"]["max_completion_ms"]["denominator"] == 1
    assert user["criteria"]["max_completion_ms"]["missed"] == 1
    assert user["status"] == "fail"


def test_pending_wait_can_fail_client_admission_without_endpoint_attempt():
    records = [record("user-01", end=50)]
    source = observation(records, users=("user-01",), criteria={"max_completion_ms": 100})
    source["windows"][0]["users"][0].update(ready_count=2, pending_ready_ms=100,
                                            pending_observed_ms=10000)
    user = row(summarize(source, records))
    assert user["outcomes"]["attempted"] == 1
    assert user["outcomes"]["errors"] == 0
    assert user["criteria"]["max_completion_ms"]["denominator"] == 1
    assert user["criteria"]["max_completion_ms"]["met"] == 1
    assert user["pending_wait"]["status"] == "breach"
    assert user["pending_wait"]["attribution"] == "client_admission"
    assert user["status"] == "fail"


def test_short_pending_wait_is_censored_without_claiming_eventual_success():
    records = [record("user-01", end=50)]
    source = observation(records, users=("user-01",), criteria={"max_completion_ms": 100})
    source["windows"][0]["users"][0].update(ready_count=2, pending_ready_ms=9950,
                                            pending_observed_ms=10000)
    user = row(summarize(source, records))
    assert user["pending_wait"]["status"] == "right_censored"
    assert user["status"] == "pass"
    assert user["criteria"]["max_completion_ms"]["denominator"] == 1


@pytest.mark.parametrize("late", [False, True])
def test_only_missing_eligible_usage_makes_exact_window_throughput_unknown(late):
    records = [record("user-01", tokens=None, end=11000 if late else 1000)]
    user = row(summarize(observation(records, users=("user-01",)), records))
    assert user["output_tokens_per_second"] == (0 if late else None)
    assert user["missing_usage_count"] == (0 if late else 1)
    assert user["known_output_tokens"] == 0


def test_unknown_eligible_usage_cannot_pass_selected_throughput():
    records = [record("user-01", tokens=None)]
    user = row(summarize(observation(records, users=("user-01",),
        criteria={"min_output_tokens_per_second": 1}), records))
    assert user["status"] == "inconclusive"
    assert user["criteria"]["min_output_tokens_per_second"]["status"] == "inconclusive"
    assert user["coverage"]["status"] == "partial"


def test_intentional_cancel_is_excluded_and_forced_drain_cancel_is_unknown():
    records = [record("user-01", identity="ok"),
               record("user-01", identity="intentional", status="cancelled", valid=False,
                      intentional=True, tokens=7),
               record("user-01", identity="forced", status="cancelled", valid=False,
                      tokens=None)]
    user = row(summarize(observation(records, users=("user-01",),
        criteria={"max_completion_ms": 2000}), records))
    criterion = user["criteria"]["max_completion_ms"]
    assert (criterion["denominator"], criterion["met"], criterion["unknown"], criterion["excluded"]) == (2, 1, 1, 1)
    assert criterion["missed"] == 0
    assert user["status"] == "inconclusive"
    assert user["outcomes"]["errors"] == 0
    assert user["cancelled_output_tokens"] == 7
    assert user["cancelled_missing_usage_count"] == 1
    assert user["partial_missing_usage_count"] == 1


def test_no_selected_criteria_are_measured_without_invented_pass():
    records = [record("user-01")]
    result = summarize(observation(records, users=("user-01",),
        criteria={"max_error_percent": None}), records)
    assert result["status"] == row(result)["status"] == "measured"
    assert row(result)["criteria"] == {}


def test_zero_dispatch_is_known_zero_delivery_with_unexercised_acceptance():
    result = summarize(observation(users=("user-01",),
        criteria={"min_output_tokens_per_second": 1}), [])
    user = row(result)
    assert user["known_output_tokens"] == user["output_tokens_per_second"] == 0
    assert user["coverage"]["status"] == "unexercised"
    assert user["status"] == "inconclusive"
    assert user["outcomes"]["attempted"] == 0


def test_zero_duration_stop_retains_failure_at_same_timestamp_as_admission():
    records = [record("user-01", dispatch=0, end=200, status="failed", valid=False)]
    source = observation(records, users=("user-01",), duration=0)
    source["windows"][0].update(cut_short=True, planned_end_ms=10000, planned_duration_seconds=10)
    result = summarize(source, records)
    user = row(result)
    assert user["output_tokens_per_second"] is None
    assert user["outcomes"]["attempted"] == 1
    assert user["outcomes"]["errors"] == 1
    assert result["status"] == user["status"] == "fail"


def test_ready_latency_exposes_slot_wait_while_dispatch_latency_stays_short():
    records = [record("user-01", dispatch=9000, end=9050,
        workload={"user_id": "user-01", "window_id": "multi_user_2_measurement", "ready_ms": 0})]
    user = row(summarize(observation(records, users=("user-01",),
        criteria={"max_completion_ms": 100}), records))
    assert user["latency"]["dispatch_completion_ms"]["max"] == 50
    assert user["latency"]["ready_completion_ms"]["max"] == 9050
    assert user["latency"]["client_wait_ms"]["max"] == 9000
    assert user["status"] == "fail"


def test_missing_first_answer_is_unknown_not_pass_or_endpoint_failure():
    records = [record("user-01", first_output_ms=None)]
    user = row(summarize(observation(records, users=("user-01",),
        criteria={"max_first_answer_ms": 100}), records))
    assert user["status"] == "inconclusive"
    assert user["criteria"]["max_first_answer_ms"]["unknown"] == 1
    assert user["outcomes"]["errors"] == 0
    assert user["coverage"]["missing_first_answer_count"] == 1


def test_missing_completion_timestamp_cannot_be_reported_as_zero_throughput():
    records = [record("user-01", completed_ms=None)]
    user = row(summarize(observation(records, users=("user-01",),
        criteria={"min_output_tokens_per_second": 1}), records))
    assert user["output_tokens_per_second"] is None
    assert user["unclassified_completed_count"] == 1
    assert user["status"] == "inconclusive"


@pytest.mark.parametrize("pending", [False, True])
def test_generator_lag_over_waiting_interval_prevents_unsupported_timing_failure(pending):
    records = [record("user-01", dispatch=9000 if not pending else 0,
                      end=9050 if not pending else 50,
        workload={"user_id": "user-01", "window_id": "multi_user_2_measurement", "ready_ms": 0})]
    source = observation(records, users=("user-01",), criteria={"max_completion_ms": 100})
    if pending:
        source["windows"][0]["users"][0].update(ready_count=2, pending_ready_ms=100,
                                                pending_observed_ms=10000)
    health = {"schema_version": 1, "windows": [{"start_ms": 500, "end_ms": 1500, "lag_ms": 950}]}
    user = row(summarize(source, records, timing_health=health))
    assert user["status"] == "inconclusive"
    assert user["outcomes"]["errors"] == 0
    if pending:
        assert user["pending_wait"]["status"] == "unqualified"
        assert user["criteria"]["max_completion_ms"]["pending_unknown"]
    else:
        assert user["criteria"]["max_completion_ms"]["unknown"] == 1


def test_warmup_never_enters_measurement_acceptance_or_delivered_tokens():
    records = [record("user-01"), record("user-01", identity="warmup", status="failed", valid=False,
        workload={"user_id": "user-01", "window_id": "warmup", "phase": "warmup"})]
    source = observation(records[:1], users=("user-01",))
    source["windows"].insert(0, {**deepcopy(source["windows"][0]), "id": "warmup", "phase": "warmup"})
    result = summarize(source, records)
    assert result["status"] == "pass"
    assert result["windows"][0]["status"] == "warmup"
    assert result["windows"][1]["users"][0]["outcomes"]["attempted"] == 1


def test_early_eos_keeps_real_tokens_and_explicit_length_coverage():
    records = [record("user-01", tokens=10, finish_reason="stop")]
    user = row(summarize(observation(records, users=("user-01",)), records))
    assert user["output_tokens_per_second"] == 1
    assert user["lengths"]["early_eos_count"] == 1
    assert user["lengths"]["output_tokens"]["median"] == 10
    assert user["finish_reasons"] == {"stop": 1}


def test_failed_requests_are_timing_misses_without_polluting_success_latency():
    records = [record("user-01", identity="ok", end=50),
               record("user-01", identity="timeout", status="timeout", valid=False, end=9000)]
    user = row(summarize(observation(records, users=("user-01",),
        criteria={"max_completion_ms": 100}), records))
    assert user["criteria"]["max_completion_ms"]["denominator"] == 2
    assert user["criteria"]["max_completion_ms"]["attainment_fraction"] == .5
    assert user["latency"]["ready_completion_ms"]["n"] == 1
    assert user["latency"]["ready_completion_ms"]["max"] == 50


@pytest.mark.parametrize("interruption", ["window", "drain", "missing_terminal"])
def test_interrupted_throughput_shortfall_is_not_an_established_full_window_failure(interruption):
    records = [record("user-01", tokens=0)]
    source = observation(records, users=("user-01",), criteria={"max_error_percent": None,
                                                                "min_output_tokens_per_second": 10})
    window = source["windows"][0]
    if interruption == "window":
        window.update(cut_short=True, planned_end_ms=20000, planned_duration_seconds=20)
    elif interruption == "drain":
        window["drain_reason"] = "cancelled by user"
        records.append(record("user-01", identity="cancel", status="cancelled", valid=False))
        window["users"][0]["dispatched_count"] += 1
    else:
        window["users"][0]["dispatched_count"] += 1
    user = row(summarize(source, records))
    assert user["output_tokens_per_second"] == (None if interruption == "missing_terminal" else 0)
    assert user["known_output_tokens_per_second"] == 0
    assert user["criteria"]["min_output_tokens_per_second"]["status"] == "inconclusive"
    assert user["status"] == "inconclusive"


def test_intentional_completed_request_is_not_labelled_cancelled():
    records = [record("user-01", intentional=True)]
    user = row(summarize(observation(records, users=("user-01",)), records))
    assert user["outcomes"]["intentional_cancelled"] == 0


def test_user_window_counts_do_not_claim_distinct_population_totals():
    records = [record("user-01", status="failed", valid=False)]
    source = observation(records, users=("user-01",))
    source["windows"].append({**deepcopy(source["windows"][0]), "id": "multi_user_5_measurement", "population": 5})
    records.append(record("user-01", identity="other-window", status="failed", valid=False,
        workload={"user_id": "user-01", "window_id": "multi_user_5_measurement", "ready_ms": 0}))
    result = summarize(source, records)
    assert result["failed_user_windows"] == 2
    assert "failed_users" not in result
