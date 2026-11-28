from collections import Counter
import ast
import json
from pathlib import Path
import typing

import pytest

from giraffe import models, states
from giraffe.capabilities import capabilities
from tests.test_ui_navigation import run_browser_script


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
    (states.TOKEN_EVIDENCE_STATUSES, states.TOKEN_EVIDENCE_TRANSITIONS),
    (states.RESTART_STATUSES, states.RESTART_TRANSITIONS),
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
        "interrupted": {"completed", "error"}, "completed": {"error"}, "error": {"interrupted"},
    }
    # Terminal runs can still change state through recovery or an unreadable saved report.
    assert all(states.RUN_TRANSITIONS.values())


@pytest.mark.parametrize("current,new,allowed", [
    (None, "running", True), ("unknown", "running", True), (None, "unknown", True),
    ("running", "running", True), ("running", "stopping", True),
    ("interrupted", "completed", True), ("interrupted", "error", True),
    ("completed", "error", True), ("error", "error", True), ("error", "interrupted", True),
    ("completed", "running", False), ("stopping", "running", False),
    ("error", "completed", False), ("running", "unknown", False),
])
def test_run_transition_allowed(current, new, allowed):
    assert states.run_transition_allowed(current, new) is allowed


def test_condition_transitions():
    assert states.CONDITION_TRANSITIONS == {"waiting": {"running", "finished"},
        "running": {"running", "finished"}, "finished": {"running"}}
    assert states.CONDITION_RECOVERED == "interrupted"


def test_token_evidence_transition_guard(monkeypatch, caplog):
    for current, successors in states.TOKEN_EVIDENCE_TRANSITIONS.items():
        for new in successors:
            states.check_token_evidence_transition(current, new)
    with pytest.raises(AssertionError, match="Undeclared token evidence transition"):
        states.check_token_evidence_transition(states.TOKEN_EVIDENCE_OBSERVED, states.TOKEN_EVIDENCE_PROBING)
    with monkeypatch.context() as patch:
        patch.setattr(states, "STRICT_TRANSITIONS", False)
        states.check_token_evidence_transition(states.TOKEN_EVIDENCE_UNAVAILABLE, states.TOKEN_EVIDENCE_PROBING)
    assert "Undeclared token evidence transition" in caplog.text


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
        "metric_states": list(states.METRIC_STATES), "unavailable_causes": list(states.UNAVAILABLE_CAUSES),
        "request_statuses": list(states.REQUEST_STATUSES), "run_states": list(states.RUN_STATES),
        "run_in_progress_states": sorted(states.RUN_IN_PROGRESS_STATES),
        "run_terminal_states": sorted(states.RUN_TERMINAL_STATES),
        "run_transitions": {state: sorted(successors) for state, successors in states.RUN_TRANSITIONS.items()},
        "stop_reasons": list(states.STOP_REASONS), "stop_reasons_open": True,
        "condition_lifecycle": list(states.CONDITION_LIFECYCLE),
        "condition_transitions": {state: sorted(successors) for state, successors in states.CONDITION_TRANSITIONS.items()},
        "connection_test_statuses": list(states.CONNECTION_TEST_STATUSES),
        "connection_context_window_reasons": list(states.CONNECTION_CONTEXT_WINDOW_REASONS),
        "token_evidence_statuses": list(states.TOKEN_EVIDENCE_STATUSES),
        "token_evidence_field_states": list(states.TOKEN_EVIDENCE_FIELD_STATES),
        "restart_statuses": list(states.RESTART_STATUSES),
        "telemetry_snapshot_statuses": list(states.TELEMETRY_SNAPSHOT_STATUSES),
        "gateway_attribution_states": list(states.GATEWAY_ATTRIBUTION_STATES),
        "condition_aggregate_statuses": list(states.CONDITION_AGGREGATE_STATUSES),
        "connection_usable_statuses": sorted(states.CONNECTION_USABLE_STATUSES),
        "telemetry_discovery_statuses": list(states.TELEMETRY_DISCOVERY_STATUSES),
        "telemetry_usable_statuses": sorted(states.TELEMETRY_USABLE_STATUSES),
        "telemetry_discovery_reusable_statuses": sorted(states.TELEMETRY_DISCOVERY_REUSABLE_STATUSES),
        "condition_stop_reasons": list(states.CONDITION_STOP_REASONS),
        "condition_stop_reason_markers": list(states.CONDITION_STOP_REASON_MARKERS),
        "token_evidence_transitions": {state: sorted(successors) for state, successors in states.TOKEN_EVIDENCE_TRANSITIONS.items()},
        "restart_transitions": {state: sorted(successors) for state, successors in states.RESTART_TRANSITIONS.items()},
        "gateway_attribution_check_status": dict(states.GATEWAY_ATTRIBUTION_CHECK_STATUS),
        "condition_verdict_status": dict(states.CONDITION_VERDICT_STATUS),
        "scale_down_phases": list(states.SCALE_DOWN_PHASES),
        "scale_down_transitions": {phase: sorted(successors) for phase, successors in states.SCALE_DOWN_TRANSITIONS.items()},
    }
    assert states.EXIT_CODE_BLOCKED == states.EXIT_CODES["blocked"] == 3
    assert states.STOP_REASONS == (states.STOP_CANCELLED_BY_USER, states.STOP_RUN_CANCELLED,
        states.STOP_MAX_DURATION, states.STOP_REQUEST_BUDGET, states.STOP_ERROR_THRESHOLD)
    assert capabilities()["states"] == description


@pytest.mark.parametrize("name", ["RUN_IN_PROGRESS_STATES", "RUN_TERMINAL_STATES"])
def test_javascript_run_state_vocabulary(name):
    expected = json.dumps(sorted(getattr(states, name)))
    assertions = f"assert.deepEqual([...{name}].sort(),{expected});"
    run_browser_script(f"const page=browser();page.run({json.dumps(assertions)});")


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


def test_metric_availability_vocabulary_is_shared_and_described():
    from giraffe.availability import CAUSES
    from giraffe.performance_html import STATES

    description = states.describe()
    assert states.UNAVAILABLE_CAUSES == CAUSES
    assert description["unavailable_causes"] == list(CAUSES)
    assert description["metric_states"] == list(states.METRIC_STATES)
    assert set(STATES) == set(states.METRIC_STATES) - {"measured", "estimated"}
    json.dumps(capabilities(), allow_nan=False)


def test_observation_transition_tables():
    assert states.TOKEN_EVIDENCE_TRANSITIONS == {
        "not_probed": {"probing", "observed", "unavailable"},
        "probing": {"probing", "observed", "unavailable"},
        "observed": {"observed"}, "unavailable": {"unavailable"},
    }
    assert states.RESTART_TRANSITIONS == {
        "blocked": {"hook_completed"}, "hook_completed": {"ready", "blocked"}, "ready": set(),
    }


def test_verdict_mappings_cover_declared_vocabularies():
    assert set(states.GATEWAY_ATTRIBUTION_CHECK_STATUS) == set(states.GATEWAY_ATTRIBUTION_STATES)
    assert states.GATEWAY_ATTRIBUTION_CHECK_STATUS == {
        "not_selected": "skipped", "verified": "pass", "mismatch": "fail",
        "unavailable": "inconclusive", "unsupported": "inconclusive", "unverified": "inconclusive",
    }
    assert set(states.CONDITION_VERDICT_STATUS) == set(states.CHECK_STATUSES)
    assert set(states.CONDITION_VERDICT_STATUS.values()) <= set(states.CONDITION_AGGREGATE_STATUSES)
    assert states.CONDITION_VERDICT_STATUS == {
        "pass": "passed", "fail": "failed", "blocked": "inconclusive",
        "inconclusive": "inconclusive", "skipped": "skipped",
    }


def test_telemetry_reason_map_and_status_subsets():
    from giraffe.telemetry import TELEMETRY_SNAPSHOT_REASONS

    assert set(TELEMETRY_SNAPSHOT_REASONS) <= set(states.TELEMETRY_SNAPSHOT_STATUSES)
    assert states.TELEMETRY_USABLE_STATUSES <= set(states.TELEMETRY_SNAPSHOT_STATUSES)
    assert states.TELEMETRY_DISCOVERY_REUSABLE_STATUSES < states.TELEMETRY_USABLE_STATUSES
    assert set(states.TELEMETRY_DISCOVERY_STATUSES) == set(states.TELEMETRY_SNAPSHOT_STATUSES) - {"run_stopped"}
    assert states.CONNECTION_USABLE_STATUSES < set(states.CONNECTION_TEST_STATUSES)


@pytest.mark.parametrize("previous,new", [("completed", "running"), ("running", "undeclared")])
def test_storage_rejects_undeclared_run_state_write(tmp_path, previous, new):
    from giraffe.storage import update_metadata

    metadata = tmp_path / ".web.json"
    metadata.write_text(json.dumps({"state": previous, "id": "run"}))
    before = metadata.read_bytes()
    with pytest.raises(AssertionError, match="Undeclared run state transition"):
        update_metadata(tmp_path, {"state": new})
    assert metadata.read_bytes() == before
    assert update_metadata(tmp_path, {"label": "updated"}) == {
        "state": previous, "id": "run", "label": "updated"}


@pytest.mark.parametrize("filename", ["web.py", "cli.py", "storage.py"])
def test_run_state_writes_use_declared_constants(filename):
    tree = ast.parse((ROOT / "src" / "giraffe" / filename).read_text())
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            writes.extend(value for key, value in zip(node.keys, node.values)
                          if isinstance(key, ast.Constant) and key.value == "state")
        elif isinstance(node, ast.Call):
            writes.extend(keyword.value for keyword in node.keywords if keyword.arg == "state")
            if isinstance(node.func, ast.Name) and node.func.id == "save_state":
                writes.extend(node.args[:1])
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                   and target.slice.value == "state" for target in node.targets):
                writes.append(node.value)
    for value in writes:
        assert not any(isinstance(part, ast.Constant) and part.value in states.RUN_STATES
                       for part in ast.walk(value)), (filename, value.lineno)
