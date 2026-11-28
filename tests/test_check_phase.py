"""Per-check phases preserve pending work, unfinished flags and watchdog timing."""

from collections import defaultdict

import pytest

from giraffe import runner, states
from tests.test_runner import client as client, config


@pytest.mark.parametrize("judging_pending", [False, True])
@pytest.mark.parametrize("check", ["correctness", "capacity", "load", "serving", "gpu"])
def test_finish_checks_pending_view(monkeypatch, judging_pending, check):
    cfg = config(unresponsive_after_failures=0)
    # Exercise the internal legacy capacity name without config normalization to load.
    cfg.checks = [check]
    if check == "capacity":
        monkeypatch.setattr("giraffe.conditions.planned_conditions", lambda _: [])
        monkeypatch.setattr("giraffe.planning.request_plan", lambda _: {"rows": []})
    cfg.targets.append(cfg.targets[0].model_copy(update={"name": "second"}))
    run = runner._Run(cfg, None, None)
    run.judging_pending = judging_pending
    expected = {("service", check): "inference", ("second", check): "inference"}
    assert run.pending_checks == expected
    assert run.check_phases == expected

    run.finish_checks("missing", [check])
    run.finish_checks("service", ["missing"])
    assert run.pending_checks == expected

    for _ in range(2):
        run.finish_checks("service", [check])
        if judging_pending and check in {"correctness", "capacity", "load"}:
            expected["service", check] = "judging"
        else:
            expected.pop(("service", check), None)
        assert run.pending_checks == expected
        assert list(run.pending_checks.items()) == list(expected.items())
        assert tuple(run.pending_checks) == tuple(expected)
        assert (("service", check) in run.pending_checks) == (("service", check) in expected)

    run.judging_pending = False
    for target in cfg.targets:
        run.finish_checks(target.name, [check])
    assert run.pending_checks == {}
    # A late finish while judging is pending must not resurrect a completed check.
    run.judging_pending = True
    run.finish_checks("service", [check])
    assert run.pending_checks == {}
    assert run.check_phases == {("service", check): "done", ("second", check): "done"}


def test_pending_checks_is_an_independent_read_only_snapshot():
    run = runner._Run(config(checks=["serving", "correctness"]), None, None)
    before = run.pending_checks
    changed = run.pending_checks
    changed.clear()
    changed["unknown", "check"] = "judging"
    assert run.pending_checks == before
    with pytest.raises(AttributeError):
        run.pending_checks = {}
    run.judging_pending = True
    run.finish_checks("service", ["serving", "correctness"])
    assert before == {("service", "serving"): "inference", ("service", "correctness"): "inference"}
    assert run.pending_checks == {("service", "correctness"): "judging"}


def test_check_phase_writer_validates_and_ignores_untracked_keys():
    run = runner._Run(config(), None, None)
    before = run.check_phases.copy()
    run._set_check_phase("unknown", "serving", states.CHECK_PHASE_DONE)
    run._set_check_phase("service", "unknown", states.CHECK_PHASE_JUDGING)
    assert run.check_phases == before
    run._set_check_phase("service", "serving", states.CHECK_PHASE_JUDGING)
    with pytest.raises(AssertionError, match="Undeclared check phase transition"):
        run._set_check_phase("service", "serving", states.CHECK_PHASE_INFERENCE)
    assert run.pending_checks == {("service", "serving"): "judging"}
    run._set_check_phase("service", "serving", states.CHECK_PHASE_DONE)
    for phase in (states.CHECK_PHASE_INFERENCE, states.CHECK_PHASE_JUDGING):
        with pytest.raises(AssertionError, match="Undeclared check phase transition"):
            run._set_check_phase("service", "serving", phase)
    assert run.pending_checks == {}
    assert run.check_phases == {("service", "serving"): "done"}


def test_done_check_can_remain_unfinished():
    run = runner._Run(config(), None, None)
    assert isinstance(run.unfinished, defaultdict) and run.unfinished.default_factory is set
    run.unfinished["service"].add("serving")
    run.unfinished["service"].update({"correctness"})
    run.mark_unfinished("service", ["capacity"])
    run.finish_checks("service", ["serving"])
    assert run.pending_checks == {}
    assert run.unfinished["service"] == {"serving", "correctness", "capacity"}


@pytest.mark.parametrize("judging_pending", [False, True])
@pytest.mark.parametrize("threshold_reached", [False, True])
def test_watchdog_retires_when_last_ordinary_inference_finishes(
    monkeypatch, judging_pending, threshold_reached,
):
    cfg = config(checks=["serving", "correctness"], max_duration_seconds=None, unresponsive_after_failures=1,
                 unresponsive_for_seconds=1)
    # GPU sampling and gateway work do not keep the inference watchdog alive.
    cfg.checks.extend(["gpu", "gateway_access"])
    events = []
    run = runner._Run(cfg, events.append, None)
    run.judging_pending = judging_pending
    now = run.start + .5
    monkeypatch.setattr(runner.time, "monotonic", lambda: now)
    run.endpoint_health["service"].update(first_attempt_seconds=0, consecutive_failures=1)
    run.finish_checks("service", ["serving"])
    assert not run._retired_endpoint_watchdogs
    assert run.stop_reason() is None

    now = run.start + (1.1 if threshold_reached else .9)
    run.finish_checks("service", ["correctness"])
    assert run._retired_endpoint_watchdogs == {"service"}
    assert (run.endpoint_health["service"]["abort_reason"] is not None) is threshold_reached
    assert run.pending_checks == {
        **({("service", "correctness"): "judging"} if judging_pending else {}),
        ("service", "gpu"): "inference", ("service", "gateway_access"): "inference",
    }
    now = run.start + 10
    run.finish_checks("service", ["correctness"])
    assert (run.stop_reason() is not None) is threshold_reached
    assert len([event for event in events if event["event"] == "target_stopping"]) == int(threshold_reached)
