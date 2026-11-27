from collections import Counter
import ast
import json
from pathlib import Path
import re
import typing

import pytest

from giraffe import models, states
from giraffe.capabilities import capabilities


ROOT = Path(__file__).resolve().parents[1]


def test_model_status_vocabularies():
    assert set(typing.get_args(models.Status)) == set(states.CHECK_STATUSES)
    assert set(typing.get_args(models.RequestRecord.model_fields["status"].annotation)) == set(states.REQUEST_STATUSES)


def test_verdict_subsets():
    assert states.PROBLEM_STATUSES <= set(states.CHECK_STATUSES)
    assert set(states.EXIT_CODES) <= set(states.CHECK_STATUSES)


def test_run_state_partitions():
    assert states.RUN_OWNED_STATES < states.RUN_IN_PROGRESS_STATES
    assert states.RUN_TERMINAL_STATES.isdisjoint(states.RUN_IN_PROGRESS_STATES)
    assert states.RUN_TERMINAL_STATES | states.RUN_IN_PROGRESS_STATES == set(states.RUN_STATES)


@pytest.mark.parametrize("vocabulary,transitions", [
    (states.RUN_STATES, states.RUN_TRANSITIONS),
    (states.CONDITION_LIFECYCLE, states.CONDITION_TRANSITIONS),
    (states.SCALE_DOWN_PHASES, states.SCALE_DOWN_TRANSITIONS),
])
def test_transition_vocabulary_and_reachability(vocabulary, transitions):
    assert set(transitions) == set(vocabulary)
    assert all(isinstance(successors, frozenset) and successors <= set(vocabulary)
               for successors in transitions.values())
    reached = {vocabulary[0]}
    while successors := set().union(*(transitions[state] for state in reached)) - reached:
        reached.update(successors)
    assert reached == set(vocabulary)


def test_run_transitions():
    assert states.RUN_TRANSITIONS == {
        "queued": {"running", "stopping", "completed", "interrupted", "error"},
        "running": {"stopping", "completed", "interrupted", "error"},
        "stopping": {"completed", "interrupted", "error"},
        "interrupted": {"completed", "error"}, "completed": {"error"}, "error": set(),
    }
    # Interrupted runs recover; completed runs can expose an unreadable report as error.
    assert {state for state, successors in states.RUN_TRANSITIONS.items() if not successors} == (
        states.RUN_TERMINAL_STATES - {"interrupted", "completed"})


@pytest.mark.parametrize("current,new,allowed", [
    (None, "running", True), ("unknown", "running", True), (None, "unknown", True),
    ("running", "running", True), ("running", "stopping", True),
    ("interrupted", "completed", True), ("interrupted", "error", True),
    ("completed", "error", True), ("error", "error", True),
    ("completed", "running", False), ("stopping", "running", False),
    ("error", "completed", False), ("running", "unknown", False),
])
def test_run_transition_allowed(current, new, allowed):
    assert states.run_transition_allowed(current, new) is allowed


def test_condition_transitions():
    assert states.CONDITION_TRANSITIONS == {"waiting": {"running", "finished"},
        "running": {"running", "finished"}, "finished": {"running"}}
    assert states.CONDITION_RECOVERED == "interrupted"


def test_finished_condition_can_restart():
    from giraffe.runner import _Run

    config = models.RunConfig(targets=[{"name": "t", "url": "http://localhost:1", "model": "m"}])
    run = _Run(config, None, None)
    row = run.plan_condition("t", "warm")
    run.finish_condition("t", "warm")
    assert row["status"] == "finished"
    assert run.start_condition("t", "warm") is row
    assert row["status"] == "running"


def test_scale_down_transitions():
    assert states.SCALE_DOWN_TRANSITIONS == {
        "baseline": {"waiting_for_output", "finalizing", "finished"},
        "waiting_for_output": {"armed", "finalizing", "finished"},
        "armed": {"observing", "finalizing", "finished"},
        "observing": {"post_removal", "finalizing", "finished"},
        "post_removal": {"finalizing", "finished"}, "finalizing": {"finished"}, "finished": set(),
    }


def test_describe():
    description = states.describe()
    assert json.loads(json.dumps(description)) == description
    assert description == {
        "check_statuses": list(states.CHECK_STATUSES), "problem_statuses": sorted(states.PROBLEM_STATUSES),
        "exit_codes": {**states.EXIT_CODES, "cancelled": states.EXIT_CODE_CANCELLED,
                       "usage_error": states.EXIT_CODE_USAGE, "default": states.EXIT_CODE_DEFAULT},
        "request_statuses": list(states.REQUEST_STATUSES), "run_states": list(states.RUN_STATES),
        "run_in_progress_states": sorted(states.RUN_IN_PROGRESS_STATES),
        "run_terminal_states": sorted(states.RUN_TERMINAL_STATES),
        "run_transitions": {state: sorted(successors) for state, successors in states.RUN_TRANSITIONS.items()},
        "stop_reasons": list(states.STOP_REASONS), "stop_reasons_open": True,
        "condition_lifecycle": list(states.CONDITION_LIFECYCLE),
        "condition_transitions": {state: sorted(successors) for state, successors in states.CONDITION_TRANSITIONS.items()},
        "scale_down_phases": list(states.SCALE_DOWN_PHASES),
        "scale_down_transitions": {phase: sorted(successors) for phase, successors in states.SCALE_DOWN_TRANSITIONS.items()},
    }
    assert states.EXIT_CODE_BLOCKED == states.EXIT_CODES["blocked"] == 3
    assert states.STOP_REASONS == (states.STOP_CANCELLED_BY_USER, states.STOP_RUN_CANCELLED,
        states.STOP_MAX_DURATION, states.STOP_REQUEST_BUDGET, states.STOP_ERROR_THRESHOLD)
    assert capabilities()["states"] == description


@pytest.mark.parametrize("name", ["RUN_IN_PROGRESS_STATES", "RUN_TERMINAL_STATES"])
def test_javascript_run_state_vocabulary(name):
    source = (ROOT / "src" / "giraffe" / "static" / "app.js").read_text()
    match = re.search(rf"const {name} = (\[[^\]]*\]);", source)
    assert match, name
    assert set(ast.literal_eval(match[1])) == getattr(states, name)


def test_scale_down_phase_vocabulary():
    assert set(states.SCALE_DOWN_PHASES[-2:]) == states.SCALE_DOWN_CLOSING_PHASES
    from giraffe.scale_down import ScaleDownController

    controller = ScaleDownController("run", "target", run_start=0, stop=None)
    with pytest.raises(ValueError, match="^Unknown scale-down phase$"):
        controller.set_phase("unknown")


def test_documented_vocabulary():
    documentation = (ROOT / "docs" / "states.md").read_text()
    for name, vocabulary in vars(states).items():
        values = vocabulary if isinstance(vocabulary, (tuple, frozenset)) else (
            (vocabulary,) if name.startswith("STOP_") else ())
        for value in values:
            assert f"`{value}`" in documentation, (name, value)


def test_stop_reasons_not_scattered():
    guarded = (states.STOP_REQUEST_BUDGET, states.STOP_MAX_DURATION,
               states.STOP_ERROR_THRESHOLD, states.STOP_CANCELLED_BY_USER)
    for path in (ROOT / "src" / "giraffe").glob("*.py"):
        if path.name == "states.py":
            continue
        occurrences = Counter(line.strip() for line in path.read_text().splitlines()
                              if any(f'"{value}"' in line for value in guarded))
        assert not occurrences, path


def test_undeclared_run_transition_fails_in_tests_and_warns_in_production(monkeypatch, caplog):
    assert states.STRICT_TRANSITIONS is True
    for current, new in [(None, "running"), ("running", "running"), ("running", "completed"),
                         ("interrupted", "completed"), ("completed", None), ("legacy", "running")]:
        states.check_run_transition(current, new)
    for current, new in [("completed", "running"), ("error", "completed"), ("running", "paused")]:
        with pytest.raises(AssertionError, match="Undeclared run state transition"):
            states.check_run_transition(current, new)
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    with caplog.at_level("WARNING", logger="giraffe.states"):
        states.check_run_transition("error", "completed")
    assert "Undeclared run state transition: 'error' -> 'completed'" in caplog.text


def test_undeclared_scale_down_transition_fails_in_tests_and_warns_in_production(monkeypatch, caplog):
    from giraffe.scale_down import ScaleDownController

    assert states.STRICT_TRANSITIONS is True
    controller = ScaleDownController("run", "target", run_start=0, stop=None)
    for current, successors in states.SCALE_DOWN_TRANSITIONS.items():
        for new in (current, *successors):
            controller.phase = current
            controller.set_phase(new)
            assert controller.phase == new
    for current, new in [("finished", "armed"), ("baseline", "observing")]:
        controller.phase = current
        with pytest.raises(AssertionError, match="Undeclared scale-down transition"):
            controller.set_phase(new)
        assert controller.phase == current
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    with caplog.at_level("WARNING", logger="giraffe.states"):
        for current, new in [("finished", "armed"), ("baseline", "observing")]:
            controller.phase = current
            controller.set_phase(new)
            assert controller.phase == new
            assert f"Undeclared scale-down transition: {current!r} -> {new!r}" in caplog.text


@pytest.mark.parametrize("state", states.RUN_STATES)
@pytest.mark.parametrize("report_available", [False, True])
def test_only_interrupted_runs_with_pending_evidence_are_recoverable(state, report_available):
    assert states.run_recoverable(state, False, report_available) is False
    assert states.run_recoverable(state, True, report_available) is (state == "interrupted" and not report_available)
