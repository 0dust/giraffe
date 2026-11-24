"""Pure request-timeout detection and shared message contracts."""
from types import SimpleNamespace

import pytest

from giraffe.timeout_warning import TimeoutWatch, format_timeout_warning


def record(**updates):
    return SimpleNamespace(**({"target": "local", "request_timeout_seconds": 30.,
        "deadline_source": "run", "timeout_check": None, "status": "timeout",
        "termination_reason": "request_timeout", "intentional": False, "scenario": "warm",
        "check_ids": ["serving"], "answer_chars": 0, "first_output_ms": None,
        "first_token_ms": None, "reasoning": "", "first_reasoning_ms": None,
        "reasoning_tokens": None} | updates))


def activate(watch, **updates):
    payload = None
    for elapsed in range(3):
        payload = watch.observe(record(**updates), elapsed)
    return payload


@pytest.mark.parametrize("changes,flags", [
    ({"intentional": True}, {}), ({"scenario": "deadline_probe"}, {}),
    ({"scenario": "client_deadline"}, {}), ({"scenario": "client_cancel"}, {}),
    ({"termination_reason": "run_duration_limit"}, {}),
    ({"termination_reason": "user_stop"}, {}),
    ({"termination_reason": "memory_limit"}, {}),
    ({"termination_reason": "error_threshold"}, {}),
    ({"termination_reason": "endpoint_unresponsive"}, {}),
    ({}, {"readiness": True}), ({}, {"evidence_probe": True}),
])
def test_exclusions(changes, flags):
    watch = TimeoutWatch()
    for elapsed in range(5):
        assert watch.observe(record(**changes), elapsed, **flags) is None
    assert watch.warnings == []


def test_threshold_and_live_updates():
    watch = TimeoutWatch()
    for i in range(7):
        assert watch.observe(record(status="completed", termination_reason=None), i) is None
    for i in range(2):
        assert watch.observe(record(), 7 + i) is None
    first = watch.observe(record(), 9)
    assert first["timed_out"] == 3 and first["finished"] == 10
    assert first["update"] == 1
    assert first["first_at_seconds"] == 9
    assert first["updated_at_seconds"] == 9
    second = watch.observe(record(), 10)
    assert second["update"] == 2 and second["timed_out"] == 4
    assert first["timed_out"] == 3  # emitted snapshots remain stable
    assert watch.warnings == [second]


def test_three_of_twenty_does_not_activate():
    watch = TimeoutWatch()
    for i in range(17):
        watch.observe(record(status="completed", termination_reason=None), i)
    for i in range(3):
        assert watch.observe(record(), 17 + i) is None
    assert watch.warnings == []


def test_breakdown_checks_and_never_retracts():
    watch = TimeoutWatch()
    watch.observe(record(answer_chars=2, check_ids=["generation"]), 1)
    watch.observe(record(reasoning="thinking", first_reasoning_ms=0), 2)
    payload = watch.observe(record(), 3)
    assert [payload[k] for k in ("answer_started", "reasoning_only", "no_output")] == [1, 1, 1]
    assert payload["checks"] == ["generation", "serving"]
    for i in range(30):
        assert watch.observe(record(status="completed", termination_reason=None), 4 + i)["update"] == i + 2
    payload = watch.observe(record(first_output_ms=0, reasoning="also reasoning"), 34)
    assert payload["timed_out"] == 4 and payload["finished"] == 34
    assert payload["answer_started"] == 2 and payload["update"] == 32
    assert payload["first_at_seconds"] == 3


@pytest.mark.parametrize("answer", [{"first_output_ms": 0}, {"answer_chars": 1}])
def test_answer_evidence(answer):
    assert activate(TimeoutWatch(), **answer)["answer_started"] == 3


@pytest.mark.parametrize("reasoning", [{"reasoning": "x"}, {"first_reasoning_ms": 0}, {"reasoning_tokens": 1}])
def test_reasoning_evidence(reasoning):
    assert activate(TimeoutWatch(), **reasoning)["reasoning_only"] == 3


def test_groups_split_by_target_deadline_and_source():
    watch = TimeoutWatch()
    payloads = [activate(watch), activate(watch, target="other"),
        activate(watch, request_timeout_seconds=60.),
        activate(watch, deadline_source="check", timeout_check="generation", check_ids=["generation"]),
        activate(watch, deadline_source="load", check_ids=["load"])]
    assert len({p["key"] for p in payloads}) == 5
    assert payloads[3]["setting"] == "test_options.generation.request_timeout_seconds"
    assert payloads[4]["setting"] == "load_test.request_timeout_seconds"
    assert all(p["timed_out"] == 3 for p in payloads)


@pytest.mark.parametrize("source,check,setting,flag", [
    ("run", None, "request_timeout_seconds", "--request-timeout"),
    ("check", "generation", "test_options.generation.request_timeout_seconds", None),
    ("load", None, "load_test.request_timeout_seconds", None),
])
def test_formatter(source, check, setting, flag):
    payload = activate(TimeoutWatch(), deadline_source=source, timeout_check=check,
                       answer_chars=1, check_ids=[check or "load"])
    assert payload["setting"] == setting and payload["cli_flag"] == flag
    ref = "--request-timeout (request_timeout_seconds)" if flag else setting
    assert format_timeout_warning(payload) == (
        "local: 3 of 3 requests hit the 30s request timeout (3 had started answering). "
        "Giraffe does not extend timeouts during a run. To allow more time, stop and rerun "
        f"with a higher {ref}; keep it if slower responses should count as failures.")


def test_formatter_breakdown_order_and_omits_zeroes():
    payload = activate(TimeoutWatch())
    payload.update(answer_started=2, reasoning_only=1, no_output=3, timed_out=6, finished=12)
    assert "(2 had started answering, 1 produced reasoning only, 3 received no output)" in format_timeout_warning(payload)


def test_twenty_percent_boundary_and_excluded_denominator():
    watch = TimeoutWatch()
    for i in range(12):
        watch.observe(record(status="completed", termination_reason=None), i)
    for i in range(50):
        watch.observe(record(status="completed", intentional=True), i)
    payload = activate(watch)
    assert payload["finished"] == 15 and payload["timed_out"] == 3


def test_report_retains_all_active_groups_and_snapshots_are_isolated():
    watch = TimeoutWatch()
    for deadline in range(1, 21):
        activate(watch, request_timeout_seconds=deadline)
    activate(watch, target="other")
    warnings = watch.warnings
    assert len(warnings) == 21
    assert [p["deadline_seconds"] for p in warnings if p["target"] == "local"] == list(range(1, 21))
    warnings[0]["checks"].clear()
    assert watch.warnings[0]["checks"] == ["serving"]


def test_reasoning_first_token_is_not_answer_evidence():
    payload = activate(TimeoutWatch(), first_token_ms=0, reasoning="thinking")
    assert payload["answer_started"] == 0 and payload["reasoning_only"] == 3


def test_equal_deadlines_keep_one_group_and_name_all_timeout_settings():
    watch = TimeoutWatch()
    watch.observe(record(deadline_source="check", timeout_check="generation"), 1)
    watch.observe(record(deadline_source="check", timeout_check="json"), 2)
    payload = watch.observe(record(deadline_source="check", timeout_check="generation"), 3)
    assert payload["timed_out"] == 3 and len(watch.warnings) == 1
    assert payload["settings"] == ["test_options.generation.request_timeout_seconds", "test_options.json.request_timeout_seconds"]
    assert "higher test_options.generation.request_timeout_seconds and test_options.json.request_timeout_seconds" in format_timeout_warning(payload)


def test_fixture_override_names_actual_file_field_without_global_cli_flag():
    payload = activate(TimeoutWatch(), timeout_setting="custom_fixtures (cases.json, fixture slow).request_timeout_seconds")
    assert payload["cli_flag"] is None and payload["setting_field"] == "custom_fixtures"
    message = format_timeout_warning(payload)
    assert "cases.json, fixture slow" in message and "--request-timeout" not in message


@pytest.mark.parametrize("reason", ["request_timeout", "request_deadline", "read_timeout",
                                    "write_timeout", "pool_timeout", "transport_timeout"])
def test_all_ordinary_client_timeout_reasons_are_counted(reason):
    payload = activate(TimeoutWatch(), termination_reason=reason)
    assert payload is not None and payload["timed_out"] == 3


@pytest.mark.parametrize("reason", ["user_stop", "run_duration_limit", "memory_limit",
    "error_threshold", "endpoint_unresponsive", "workload_cancelled", "intentional_deadline",
    "intentional_cancellation", "intentional_cancellation_deadline"])
def test_stop_terminations_do_not_dilute_finished_denominator(reason):
    watch = TimeoutWatch()
    for elapsed in range(20):
        assert watch.observe(record(status="cancelled", termination_reason=reason), elapsed) is None
    payload = activate(watch)
    assert payload is not None and payload["finished"] == payload["timed_out"] == 3


def test_active_warning_refreshes_after_twenty_successes():
    watch = TimeoutWatch()
    original = activate(watch)
    for index in range(20):
        payload = watch.observe(record(status="completed", termination_reason=None), 10 + index)
        assert payload["finished"] == 4 + index
        assert payload["update"] == 2 + index
    assert payload["timed_out"] == 3 and payload["finished"] == 23
    assert payload["updated_at_seconds"] == 29
    assert watch.warnings == [payload]
    assert original["finished"] == 3 and original["update"] == 1
