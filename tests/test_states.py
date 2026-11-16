from collections import Counter
from pathlib import Path
import typing


from giraffe import models, states


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


def test_scale_down_phase_vocabulary():
    assert set(states.SCALE_DOWN_PHASES[-2:]) == states.SCALE_DOWN_CLOSING_PHASES
    # scale_down.py is source-hashed, so its own copy of the phase set stays inline.
    source = (ROOT / "src" / "giraffe" / "scale_down.py").read_text()
    for phase in states.SCALE_DOWN_PHASES:
        assert f'"{phase}"' in source, phase


def test_documented_vocabulary():
    documentation = (ROOT / "docs" / "states.md").read_text()
    for name, vocabulary in vars(states).items():
        values = vocabulary if isinstance(vocabulary, (tuple, frozenset)) else (
            (vocabulary,) if name.startswith("STOP_") else ())
        for value in values:
            assert f"`{value}`" in documentation, (name, value)


# Baseline comparability hashes the bytes of these modules (reporting.py and
# runner.py workload identity), so any edit to them, even a literal-for-constant
# swap, makes saved baselines for that workload not comparable. They keep
# their inline literals on purpose.
SOURCE_HASHED = {"load_test.py", "concurrent_load.py", "traffic.py", "multi_user.py",
                 "multi_user_metrics.py", "session_sampling.py", "workloads.py",
                 "gateway_access.py", "gateway_json.py", "gateway_policy.py",
                 "gateway_response.py", "scale_down.py", "logprobs.py",
                 "logprob_workload.py", "tool_conversations.py"}


def test_stop_reasons_not_scattered():
    guarded = (states.STOP_REQUEST_BUDGET, states.STOP_MAX_DURATION,
               states.STOP_ERROR_THRESHOLD, states.STOP_CANCELLED_BY_USER)
    for path in (ROOT / "src" / "giraffe").glob("*.py"):
        if path.name == "states.py" or path.name in SOURCE_HASHED:
            continue
        occurrences = Counter(line.strip() for line in path.read_text().splitlines()
                              if any(f'"{value}"' in line for value in guarded))
        assert not occurrences, path
