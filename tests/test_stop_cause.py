"""Run stop causes preserve soft budgets and first-wins terminal stops."""

import asyncio
from types import SimpleNamespace

import pytest

from giraffe import runner
from tests.test_runner import client as client, config


KINDS = ("user", "cancelled", "duration", "request_budget", "error_threshold",
         "endpoints_unresponsive", "memory")
FREE_TEXT = {"endpoints_unresponsive": "Endpoint service unresponsive",
             "memory": "memory limit reached: RAM grew by 2 MiB"}


@pytest.mark.parametrize("current", [None, *KINDS])
@pytest.mark.parametrize("new", KINDS)
@pytest.mark.parametrize("replace_soft", [False, True])
def test_latch_table(current, new, replace_soft):
    run = runner._Run(config(), None, None)
    if current is not None:
        assert run._latch_stop(current, FREE_TEXT.get(current))
    previous = run.stop_cause
    changed = current is None or (current == "request_budget" and new != "request_budget" and replace_soft)
    assert run._latch_stop(new, FREE_TEXT.get(new), replace_soft=replace_soft) is changed
    assert run.stop_cause.kind == (new if changed else current)
    if not changed:
        assert run.stop_cause is previous


def test_stop_cause_is_frozen_and_reason_compatibility():
    from dataclasses import FrozenInstanceError
    from giraffe import states

    run = runner._Run(config(), None, None)
    assert run.reason is None and run.user_stop_scope is None
    for text, kind in states.STOP_REASON_KIND.items():
        run.reason = text
        assert run.stop_cause == runner.StopCause(kind, text)
    with pytest.raises(ValueError, match="_latch_stop"):
        run.reason = "memory limit reached"
    with pytest.raises(FrozenInstanceError):
        run.stop_cause.kind = "memory"
    run.reason = None
    assert run.stop_cause is None and run.reason is None


@pytest.mark.parametrize("user,duration,endpoints,errors,expected", [
    (True, True, True, True, "user"),
    (False, True, True, True, "duration"),
    (False, False, True, True, "endpoints_unresponsive"),
    (False, False, False, True, "error_threshold"),
])
def test_stop_reason_ladder_and_single_notification(user, duration, endpoints, errors, expected):
    events = []
    run = runner._Run(config(), events.append, None)
    if user:
        run.stop.set()
    if duration:
        run.deadline = 0
    if endpoints:
        run.endpoint_health["service"]["abort_reason"] = FREE_TEXT["endpoints_unresponsive"]
    if errors:
        run.errors = run.config.stop_after_errors
    reason = run.stop_reason()
    assert run.stop_cause.kind == expected
    assert run.reason == reason
    if duration:
        assert run.limits_reached == {"max_duration_seconds"}
        assert run.duration_limit_scope == "shared"
    assert run.stop_reason() == reason
    stopping = [e for e in events if e["event"] == "run_stopping"]
    assert len(stopping) == 1
    assert stopping[0]["abort_reason"] == reason
    assert stopping[0]["stop_kind"] == expected


def test_budget_then_duration_and_target_abort_poll():
    events = []
    run = runner._Run(config(), events.append, None)
    run._latch_stop("request_budget")
    assert run.stop_reason() == "maximum request budget reached"
    assert not events
    run.deadline = 0
    assert run.stop_reason() == "maximum run duration reached"
    assert run.stop_cause.kind == "duration"
    run.endpoint_health["service"]["abort_reason"] = FREE_TEXT["endpoints_unresponsive"]
    assert run.stop_reason("service") == FREE_TEXT["endpoints_unresponsive"]
    assert run.reason == "maximum run duration reached"
    assert len(events) == 1


@pytest.mark.parametrize("current", [None, "request_budget"])
@pytest.mark.parametrize("errors", [False, True])
def test_empty_endpoint_health_does_not_latch_unresponsive_stop(current, errors):
    events = []
    run = runner._Run(config(), events.append, None)
    run.endpoint_health.clear()
    if current is not None:
        run._latch_stop(current)
    if errors:
        run.errors = run.config.stop_after_errors

    expected = "error threshold reached" if errors else (
        "maximum request budget reached" if current else None)
    assert run.stop_reason() == expected
    assert run.stop_reason() == expected
    assert len(events) == int(errors)
    if errors:
        assert run.stop_cause.kind == "error_threshold"
    elif current is None:
        assert run.stop_cause is None
    else:
        assert run.stop_cause.kind == "request_budget"


@pytest.mark.parametrize("reason,expected", [
    (None, "check stopped early"),
    ("cancelled by user", "run stopped"),
    ("maximum run duration reached", "time limit reached"),
    ("maximum request budget reached", "request budget reached"),
    ("error threshold reached", "check stopped early"),
    ("memory limit reached", "check stopped early"),
])
def test_condition_reason_supports_reason_only_run(reason, expected):
    run = SimpleNamespace(reason=reason, stop=asyncio.Event(), deadline=float("inf"),
                          remaining=lambda target: None)
    assert runner._Run.condition_reason(run, "service") == expected


@pytest.mark.parametrize("stopped,deadline,remaining,expected", [
    (True, float("inf"), None, "run stopped"),
    (False, 0, None, "time limit reached"),
    (False, float("inf"), 0, "request budget reached"),
])
def test_condition_reason_supports_signal_only_run(stopped, deadline, remaining, expected):
    run = SimpleNamespace(stop=asyncio.Event(), deadline=deadline,
                          remaining=lambda target: remaining)
    if stopped:
        run.stop.set()
    assert runner._Run.condition_reason(run, "service") == expected


@pytest.mark.parametrize("current,expected", [("user", "user"), ("request_budget", "memory")])
def test_memory_sampling_respects_latched_cause(current, expected):
    events = []
    run = runner._Run(config(max_memory_mb=1), events.append, None)
    run._latch_stop(current)
    run.rss_at_start_bytes = 1
    run.memory_sample(2 * 1024**2)
    assert run.stop_cause.kind == expected
    assert run.memory_limit_reached
    assert len([e for e in events if e["event"] == "run_stopping"]) == 1


@pytest.mark.parametrize("watchdog_aborts", [False, True])
def test_user_scope_observes_watchdog_updates(watchdog_aborts):
    run = runner._Run(config(checks=["gateway_access"], unresponsive_after_failures=1,
                             unresponsive_for_seconds=1), None, None)
    run._gateway_only_phase = True
    run._latch_stop("request_budget")
    if watchdog_aborts:
        run.endpoint_health["service"].update(consecutive_failures=1, first_attempt_seconds=-10)
    run.stop.set()
    run.stop_reason()
    assert run.stop_cause.kind == "user"
    assert run.stop_cause.scope == (None if watchdog_aborts else "gateway")
    assert run.user_stop_scope == run.stop_cause.scope
    run._gateway_only_phase = False
    run.stop_reason()
    assert run.user_stop_scope == (None if watchdog_aborts else "gateway")


@pytest.mark.parametrize("kind", [None, "user", "duration", "request_budget", "memory"])
async def test_report_and_events_expose_raw_cause(monkeypatch, local_tokenizer, kind):
    from giraffe import resources

    events = []
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)

    async def target(run, target, fixtures):
        if kind == "user":
            run.stop.set()
        elif kind == "duration":
            run.deadline = 0
        elif kind == "request_budget":
            run._latch_stop(kind)
        elif kind == "memory":
            run._latch_stop("request_budget")
            run.memory_sample(2 * 1024**2)
        run.stop_reason()

    monkeypatch.setattr(runner, "_target", target)
    report = await runner.run_suite(config(max_memory_mb=1), progress=events.append)
    cause = report.model_dump(mode="json")["observations"]["stop_cause"]
    if kind is None:
        assert cause is None
    else:
        assert set(cause) == {"kind", "text", "scope"}
        assert cause["kind"] == kind and cause["scope"] is None
        if kind == "request_budget":
            assert cause["text"] == "maximum request budget reached"
            assert report.abort_reason is None
        else:
            assert cause["text"] == report.abort_reason
        if kind == "memory":
            assert report.overall == "inconclusive"
            assert report.abort_reason.startswith("memory limit reached:")
    stopping = [e for e in events if e["event"] == "run_stopping"]
    assert len(stopping) == (0 if kind in {None, "request_budget"} else 1)
    if stopping:
        assert stopping[0]["stop_kind"] == kind
    finished = [e for e in events if e["event"] == "run_finished"]
    assert len(finished) == 1
    assert finished[0]["stop_kind"] == kind
    assert finished[0]["abort_reason"] == report.abort_reason


@pytest.mark.parametrize("external,user,expected", [
    (False, False, "request_budget"),
    (True, False, "cancelled"),
    (True, True, "user"),
])
async def test_cancellation_budget_rules(monkeypatch, local_tokenizer, external, user, expected):
    started, stop = asyncio.Event(), asyncio.Event()

    async def target(run, target, fixtures):
        run._latch_stop("request_budget")
        started.set()
        if external:
            await asyncio.Event().wait()
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_target", target)
    task = asyncio.create_task(runner.run_suite(config(), stop_event=stop))
    await asyncio.wait_for(started.wait(), 1)
    if external:
        if user:
            stop.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, 1)
        report = cancelled.value.giraffe_report
    else:
        report = await asyncio.wait_for(task, 1)
    assert report.observations["stop_cause"]["kind"] == expected


async def test_late_judging_user_stop_preserves_soft_budget(client, monkeypatch):
    from giraffe import judging
    from tests.test_runner import judge_summary, judged_pack_config

    async def evaluate(config, records, specs, *, stop_event, **kwargs):
        assert len(records) == 1
        stop_event.set()
        return judge_summary(records, stopped="cancelled")

    monkeypatch.setattr(judging, "judge_records", evaluate)
    settings = judged_pack_config(checks=["correctness"], max_requests=1)
    settings.load_test.schedule.duration_seconds = 1
    report = await runner.run_suite(settings)
    assert report.observations["stop_cause"] == {
        "kind": "request_budget", "text": "maximum request budget reached", "scope": None}
    assert report.abort_reason == "maximum request budget reached"
    assert report.observations["judging"]["stop_reason"] == "cancelled"


async def test_cancelled_cli_run_recovers_stop_cause_from_kept_journal(client, monkeypatch, tmp_path):
    from giraffe import cli
    from giraffe.evidence import recover_evidence

    run_suite = runner.run_suite

    async def cancelled_run(config, *, progress, stop_event, **kwargs):
        def cancel_after_request(event):
            progress(event)
            if event["event"] == "request_completed":
                stop_event.set()

        return await run_suite(config, progress=cancel_after_request, stop_event=stop_event, **kwargs)

    monkeypatch.setattr(runner, "run_suite", cancelled_run)
    report = await cli._run(config(), quiet=True, directory=tmp_path)
    assert report.requests
    expected = {"kind": "user", "text": "cancelled by user", "scope": None}
    assert report.observations["stop_cause"] == expected
    assert not (tmp_path / "report.json").exists()
    recovered = recover_evidence(tmp_path, {})
    assert len(recovered.requests) == len(report.requests)
    assert recovered.observations["stop_cause"] == expected
