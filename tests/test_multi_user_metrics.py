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


def fairness_records(counts=(2, 2)):
    return [record(f'user-{index + 1:02d}', identity=f'user-{index + 1:02d}-{turn}',
                   dispatch=turn * 2000, end=turn * 2000 + 1000,
                   stream=True, answer_chunks=3, answer_reads=3, last_output_ms=900)
            for index, count in enumerate(counts) for turn in range(count)]


def fairness_observation(records, *, users=('user-01', 'user-02'), limit=1.2):
    source = observation(records, users=users, criteria={'max_error_percent': None})
    source['settings']['pass_criteria']['max_throughput_ratio'] = limit
    source['settings']['fairness_tolerance_percent'] = 10
    source['settings']['think_time_seconds'] = {'values': [0], 'weights': None}
    for user in source['windows'][0]['users']:
        user['think_time_plan'] = {'values': [0], 'weights': None, 'seed_key': None}
    return source


def fairness(result):
    return result['windows'][0]['fairness']


@pytest.mark.parametrize('counts,expected,ratio', [((2, 2), 'pass', 1), ((4, 1), 'fail', 2.5)])
def test_equal_work_fairness_counts_the_slowest_user(counts, expected, ratio):
    records = fairness_records(counts)
    source = fairness_observation(records)
    before = deepcopy(source)
    result = summarize(source, records)
    measured = fairness(result)
    assert measured['status'] == result['status'] == expected
    assert measured['qualified']
    assert measured['ratio'] == ratio
    assert measured['eligible_user_ids'] == ['user-01', 'user-02']
    assert measured['compared_user_ids'] == ['user-01', 'user-02']
    assert measured['overlap_user_ids'] == ['user-01', 'user-02']
    assert source == before


@pytest.mark.parametrize(("first", "second", "reason"), [
    ({"cached_prompt_tokens": 100}, {"cached_prompt_tokens": 0}, "uncached input"),
    ({"cached_prompt_tokens": 100}, {}, "Cached input usage is missing"),
    ({"cached_prompt_tokens": 101}, {"cached_prompt_tokens": 0}, "cached input usage is invalid"),
    ({"completion_tokens": 100, "reasoning_tokens": 0},
     {"completion_tokens": 200, "reasoning_tokens": 100}, "total generation"),
])
def test_fairness_keeps_visible_output_rate_but_qualifies_effective_work(first, second, reason):
    records = [r.model_copy(update=first if r.workload["user_id"] == "user-01" else second)
               for r in fairness_records((4, 2))]
    result = summarize(fairness_observation(records), records)
    measured = fairness(result)
    assert measured["status"] == result["status"] == "inconclusive"
    assert measured["ratio"] == 1.5
    assert [u["output_tokens_per_second"] for u in result["windows"][0]["users"]] == [40, 20]
    assert any(reason in text for text in measured["reasons"])


def test_equal_cache_and_reasoning_usage_can_still_qualify():
    records = [r.model_copy(update={"cached_prompt_tokens": 50, "completion_tokens": 200,
                                    "reasoning_tokens": 100}) for r in fairness_records()]
    measured = fairness(summarize(fairness_observation(records), records))
    assert measured["status"] == "pass"
    assert [user["uncached_input_median_tokens"] for user in measured["users"]] == [50, 50]
    assert [user["total_generated_median_tokens"] for user in measured["users"]] == [200, 200]


def test_normally_finished_conversations_can_qualify_below_the_output_cap():
    records = fairness_records()
    for request in records:
        request.workload["session_id"] = "conversation-" + request.workload["user_id"]
        request.finish_reason = "stop"
        request.requested_output_tokens = None
        request.requested_max_tokens = 32
        request.output_tokens = request.completion_tokens = 4
        request.score = True
    source = fairness_observation(records)
    for user in source["windows"][0]["users"]:
        user["profile"] = "conversation:exact_repeat"
    measured = fairness(summarize(source, records))
    assert measured["status"] == "pass"
    assert measured["ratio"] == 1


def test_equal_medians_do_not_hide_visible_work_replaced_by_reasoning():
    records = fairness_records((5, 5))
    for request, output in zip(records, [100]*8 + [10, 10]):
        request.output_tokens = output
        request.completion_tokens = 100
        request.reasoning_tokens = 100 - output
    measured = fairness(summarize(fairness_observation(records), records))
    assert measured["status"] == "inconclusive"
    assert measured["ratio"] == pytest.approx(1.28125)
    assert [user["output_median_tokens"] for user in measured["users"]] == [100, 100]
    assert any("output lengths" in reason and "mix" in reason for reason in measured["reasons"])


def test_single_user_baseline_contributes_per_user_acceptance_but_not_fairness():
    records = fairness_records()
    source = fairness_observation(records)
    source["settings"]["pass_criteria"]["max_completion_ms"] = 1500
    solo_records = [r.model_copy(update={"id": "solo", "workload": {
        **r.workload, "window_id": "multi_user_1_measurement", "population": 1}}) for r in records[:1]]
    solo = observation(solo_records, users=("user-01",))
    solo_window = solo["windows"][0]
    solo_window["id"] = "multi_user_1_measurement"
    solo["settings"] = source["settings"]
    solo_result = summarize(solo, solo_records)
    assert solo_result["status"] == "inconclusive"
    assert solo_result["windows"][0]["status"] == "pass"
    assert fairness(solo_result)["status"] == "not_applicable"
    assert fairness(solo_result)["ratio"] is None

    source["windows"].insert(0, solo_window)
    mixed = summarize(source, solo_records + records)
    assert mixed["status"] == "pass"
    assert [w["fairness"]["status"] for w in mixed["windows"]] == ["not_applicable", "pass"]

    solo_records[0].completed_ms = 1600
    assert summarize(source, solo_records + records)["status"] == "fail"


@pytest.mark.parametrize('change,reason', [
    ('think_plan', 'think'), ('actual_input', 'input'), ('actual_output', 'output'),
    ('early_eos', 'ended'), ('missing_input', 'usage'), ('missing_output', 'usage'),
    ('unserved', 'samples'), ('missing_overlap', 'overlap'), ('missing_stream', 'overlap'),
    ('buffered', 'overlap'), ('late_activation', 'exposure'), ('profile', 'profile'),
    ('short_window', 'window'), ('short_samples', 'samples'), ('client_lag', 'scheduling'),
])
def test_incomparable_work_cannot_pass_or_fail_fairness(change, reason):
    records = fairness_records((4, 1))
    source = fairness_observation(records)
    window = source['windows'][0]
    second = records[-1]
    health = None
    if change == 'think_plan':
        window['users'][1]['think_time_plan']['values'] = [1]
    elif change == 'actual_input':
        second.input_tokens = 200
    elif change == 'actual_output':
        second.output_tokens = 50
    elif change == 'early_eos':
        second.finish_reason, second.output_tokens = 'stop', 95
    elif change == 'missing_input':
        second.input_tokens = None
    elif change == 'missing_output':
        second.output_tokens = None
    elif change == 'unserved':
        records.pop()
        window['users'][1]['dispatched_count'] = 0
    elif change == 'missing_overlap':
        second.dispatch_ms = second.timing_start_ms = 8500
        second.completed_ms = 9500
    elif change == 'missing_stream':
        second.stream = False
    elif change == 'buffered':
        second.answer_reads = second.answer_chunks = 1
    elif change == 'late_activation':
        window['users'][1]['activated_ms'] = 5000
    elif change == 'profile':
        window['users'][1]['profile'] = 'medium_short'
    elif change == 'short_window':
        window['cut_short'] = True
    elif change == 'short_samples':
        source['settings']['pass_criteria']['min_samples'] = 2
    elif change == 'client_lag':
        health = {'schema_version': 1, 'windows': [{'start_ms': 100, 'end_ms': 700, 'lag_ms': 550}]}
    measured = fairness(summarize(source, records, timing_health=health))
    assert measured['status'] == 'inconclusive'
    assert not measured['qualified']
    assert any(reason in message.lower() for message in measured['reasons'])


def test_each_user_needs_a_peer_overlap_not_a_population_wide_overlap():
    users = tuple(f'user-{index + 1:02d}' for index in range(20))
    records = [record(user, dispatch=(index // 5) * 2000, end=(index // 5) * 2000 + 1000,
                      stream=True, answer_chunks=3, answer_reads=3, last_output_ms=900)
               for index, user in enumerate(users)]
    result = summarize(fairness_observation(records, users=users), records)
    assert fairness(result)['status'] == 'pass'
    assert fairness(result)['overlap_user_ids'] == list(users)


def test_unserved_third_user_cannot_be_dropped_from_fairness_population():
    records = fairness_records()
    users = ('user-01', 'user-02', 'user-03')
    result = summarize(fairness_observation(records, users=users), records)
    assert fairness(result)['status'] == 'inconclusive'
    assert fairness(result)['compared_user_ids'] == list(users)
    assert fairness(result)['eligible_user_ids'] == list(users[:2])
    assert fairness(result)['ratio'] is None


def test_unselected_fairness_is_measurement_only_even_with_unequal_delivery():
    records = fairness_records((4, 1))
    result = summarize(fairness_observation(records, limit=None), records)
    assert fairness(result)['status'] == result['status'] == 'measured'
    assert fairness(result)['ratio'] == 2.5


@pytest.mark.parametrize('tokens', [0, None])
def test_missing_or_zero_worst_throughput_never_invents_a_ratio(tokens):
    records = fairness_records()
    for item in records:
        if item.workload['user_id'] == 'user-02':
            item.output_tokens = tokens
    measured = fairness(summarize(fairness_observation(records), records))
    assert measured['ratio'] is None
    assert measured['status'] == 'inconclusive'


def test_fairness_inconclusive_does_not_erase_an_independent_error_failure():
    records = fairness_records()
    records[-1].status, records[-1].valid = 'failed', False
    source = fairness_observation(records)
    source['settings']['pass_criteria']['max_error_percent'] = 0
    result = summarize(source, records)
    assert result['status'] == 'fail'
    assert fairness(result)['status'] == 'inconclusive'
    assert row(result, 'user-02')['status'] == 'fail'


def test_actual_length_tolerance_is_configurable_and_input_shape_still_required():
    records = fairness_records()
    for item in records:
        if item.workload['user_id'] == 'user-02':
            item.input_tokens = 115
    source = fairness_observation(records)
    assert fairness(summarize(source, records))['status'] == 'inconclusive'
    source['settings']['fairness_tolerance_percent'] = 20
    assert fairness(summarize(source, records))['status'] == 'pass'
    for item in records:
        item.input_tokens = 512
    assert fairness(summarize(source, records))['status'] == 'inconclusive'


def test_variable_independent_pause_and_conversation_plans_are_not_equal_work():
    records = fairness_records()
    source = fairness_observation(records)
    for user in source['windows'][0]['users']:
        user['think_time_plan'] = {'values': [0, 1], 'weights': None, 'seed_key': user['user_id']}
    assert fairness(summarize(source, records))['status'] == 'inconclusive'
    for user in source['windows'][0]['users']:
        user['think_time_plan'] = {'values': [0], 'weights': None, 'seed_key': None}
    source['settings']['conversations'] = {'sampling': {'turn_count': {'values': [2, 3]}}}
    assert fairness(summarize(source, records))['status'] == 'inconclusive'


def test_missing_usage_on_late_work_keeps_delivery_known_but_fairness_unqualified():
    records = fairness_records()
    records.append(record('user-02', identity='late', dispatch=9500, end=11000,
                          input_tokens=None, tokens=None, stream=True, answer_reads=3,
                          answer_chunks=3, last_output_ms=1400))
    source = fairness_observation(records)
    result = summarize(source, records)
    assert row(result, 'user-02')['output_tokens_per_second'] == 20
    assert fairness(result)['status'] == 'inconclusive'
    assert any('usage' in reason for reason in fairness(result)['reasons'])


@pytest.mark.parametrize('input_tokens,expected', [
    ([100, 100, 200, 100, 200, 100], 'inconclusive'),
    ([100, 200, 100, 100, 200, 200], 'pass'),
])
def test_fairness_compares_actual_lengths_of_counted_completions(input_tokens, expected):
    records = fairness_records((3, 3))
    for request, tokens in zip(records, input_tokens):
        request.input_tokens = tokens
    source = fairness_observation(records)
    source['windows'][0].update(end_ms=4500, planned_end_ms=4500,
                                duration_seconds=4.5, planned_duration_seconds=4.5)
    result = summarize(source, records)
    assert row(result)['late_completed'] == row(result, 'user-02')['late_completed'] == 1
    measured = fairness(result)
    assert measured['ratio'] == 1
    assert measured['status'] == expected


def test_overlap_of_a_users_own_requests_does_not_prove_peer_overlap():
    records = fairness_records((2, 1))
    records[1].dispatch_ms = records[1].timing_start_ms = 0
    records[1].completed_ms = 1000
    records[-1].dispatch_ms = records[-1].timing_start_ms = 5000
    records[-1].completed_ms = 6000
    result = summarize(fairness_observation(records), records)
    assert fairness(result)['status'] == 'inconclusive'
    assert fairness(result)['overlap_user_ids'] == []


def test_lag_while_users_are_thinking_still_qualifies_the_shared_duration():
    records = fairness_records((1, 1))
    health = {'schema_version': 1, 'windows': [{'start_ms': 2000, 'end_ms': 2900, 'lag_ms': 850}]}
    result = summarize(fairness_observation(records), records, timing_health=health)
    assert all(user['timing_quality']['status'] == 'qualified' for user in result['windows'][0]['users'])
    assert fairness(result)['status'] == 'inconclusive'
    assert fairness(result)['timing_quality']['phase_affected']


def test_saved_scheduling_windows_cannot_be_ignored_when_recomputing_fairness():
    records = fairness_records((1, 1))
    source = fairness_observation(records)
    source['scheduling_delay_windows'] = [{'start_ms': 2000, 'end_ms': 2900}]
    result = summarize(source, records)
    assert fairness(result)['status'] == 'inconclusive'
    assert any('scheduling' in reason for reason in fairness(result)['reasons'])


@pytest.mark.parametrize('conversation',[False,True])
def test_truncated_conversation_is_partial_but_stateless_cap_is_valid(conversation):
    item = record('user-01',end=50)
    if conversation:
        item.workload.update(session_id='chat-1',turn_id=0)
    result = summarize(observation([item],users=('user-01',),criteria={
        'min_samples':1,'max_error_percent':0,'max_completion_ms':100,
        'min_output_tokens_per_second':1}),[item])
    user = row(result)
    if conversation:
        assert user['outcomes']['errors'] == 1
        assert user['outcomes']['valid_completed'] == 0
        assert user['partial_output_tokens'] == 100
        assert user['known_output_tokens'] == 0
        assert user['status'] == 'fail'
    else:
        assert user['outcomes']['errors'] == 0
        assert user['outcomes']['valid_completed'] == 1
        assert user['partial_output_tokens'] == 0
        assert user['known_output_tokens'] == 100
        assert user['status'] == 'pass'
