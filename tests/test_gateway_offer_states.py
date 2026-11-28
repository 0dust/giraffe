"""Gateway conclusion contracts and the two independent offer ledgers."""
import ast
import asyncio
import inspect
import json
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from giraffe import gateway_access, gateway_policy, multi_user, states, traffic
from giraffe.models import GatewayAccessCase, Limits
from tests.test_gateway_policy import configuration, exercise
from tests.test_traffic import Harness, profile
from tests.virtual_clock import run_virtual


POLICY_PAIRS = {
    "unverified": {"inconclusive"}, "unavailable": {"inconclusive"}, "unexercised": {"inconclusive"},
    "observed_failure": {"fail"}, "observed": {"pass"},
}
ACCESS_REASONS = {
    "not_run", "request_cancelled", "identity_changed", "identity_unavailable", "fallback_reported",
    "model_group_mismatch", "valid_completion", "assistant_refusal", "metered_reasoning_completion",
    "gateway_authentication", "gateway_model_access", "incomplete_or_unsupported_response",
    "request_not_dispatched", "auth_header_unverified", "identity_conflict", "unsupported_profile",
    "run_deadline", "run_stopped", "request_budget_exhausted",
}
TRAFFIC_OUTCOMES = {"not_offered", "dispatching", "started", "cancelled", "dropped_local", "dropped_late"}
MULTI_USER_OUTCOMES = {"dispatched", "dropped_local", "dropped_late", "unissued", "not_offered"}
TRANSITIONS = {
    "not_offered": {"not_offered", "dispatching", "dropped_local", "dropped_late"},
    "dispatching": {"started", "dropped_late", "not_offered"},
    "started": {"cancelled", "dropped_late", "not_offered"},
    "cancelled": {"not_offered"}, "dropped_local": set(), "dropped_late": set(),
}


def test_declarations_and_description():
    assert states.GATEWAY_POLICY_STATUSES == POLICY_PAIRS
    assert set(states.GATEWAY_POLICY_QUALIFICATIONS) == set(POLICY_PAIRS)
    assert set(states.GATEWAY_ACCESS_EXPECTED) == {"allow", "deny"}
    assert set(get_args(get_type_hints(GatewayAccessCase)["expected"])) == set(states.GATEWAY_ACCESS_EXPECTED)
    assert set(states.GATEWAY_ACCESS_OBSERVATIONS) == {"allow", "deny", "unverified"}
    assert set(states.GATEWAY_ACCESS_REASONS) == ACCESS_REASONS
    assert set(states.GATEWAY_ACCESS_DENIAL_KINDS) == {"unknown", "authentication", "model_access"}
    assert set(states.TRAFFIC_OFFER_OUTCOMES) == TRAFFIC_OUTCOMES
    assert set(states.MULTI_USER_OFFER_OUTCOMES) == MULTI_USER_OUTCOMES
    assert set(states.OFFER_OUTCOMES) == TRAFFIC_OUTCOMES | MULTI_USER_OUTCOMES
    assert states.OFFER_TRANSITIONS == TRANSITIONS
    assert states.OFFER_TRANSIENT_OUTCOMES == {"dispatching"}
    assert states.OFFER_TERMINAL_OUTCOMES == set(states.OFFER_OUTCOMES) - {"dispatching"}
    description = states.describe()
    assert json.loads(json.dumps(description)) == description
    for name in ("GATEWAY_POLICY_QUALIFICATIONS", "GATEWAY_ACCESS_EXPECTED", "GATEWAY_ACCESS_OBSERVATIONS",
                 "GATEWAY_ACCESS_REASONS", "GATEWAY_ACCESS_DENIAL_KINDS", "OFFER_OUTCOMES",
                 "TRAFFIC_OFFER_OUTCOMES", "MULTI_USER_OFFER_OUTCOMES", "OFFER_TERMINAL_OUTCOMES",
                 "OFFER_TRANSIENT_OUTCOMES"):
        assert description[name.lower()] == sorted(getattr(states, name))
    assert description["gateway_policy_statuses"] == {key: sorted(value) for key, value in POLICY_PAIRS.items()}
    assert description["offer_transitions"] == {key: sorted(value) for key, value in TRANSITIONS.items()}
    assert description["gateway_access_case_statuses"] == {
        "unverified": {"allow": "inconclusive", "deny": "inconclusive"},
        "allow": {"allow": "pass", "deny": "fail"}, "deny": {"allow": "fail", "deny": "pass"},
    }


def _string_literals(node):
    parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    return [child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
            and not (isinstance(parents.get(child), ast.Subscript) and parents[child].slice is child)
            and not (isinstance(parents.get(child), ast.Dict) and child in parents[child].keys)]


def _vocabulary_writes(module):
    """Scan write expressions, not just known spellings, so a new typo also fails."""
    tree = ast.parse(inspect.getsource(module))
    expressions = []
    fields = {gateway_policy: {"qualification"}, gateway_access: {"observed", "reason", "expected"},
              multi_user: {"outcome"}, traffic: {"outcomes"}}[module]
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            expressions.extend(value for key, value in zip(node.keys, node.values)
                               if isinstance(key, ast.Constant) and key.value in fields)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)):
                    expressions.extend(value for name, value in zip(target.elts, node.value.elts)
                                       if isinstance(name, ast.Name) and name.id in fields)
                if ((isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                     and target.slice.value in fields)
                        or (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                            and target.value.id == "outcomes")
                        or (isinstance(target, ast.Name) and target.id in fields)
                        or (module is gateway_access and isinstance(target, ast.Name) and target.id == "reason")):
                    expressions.append(node.value)
        if isinstance(node, ast.Call):
            expressions.extend(kw.value for kw in node.keywords if kw.arg in fields)
            if isinstance(node.func, ast.Name) and node.func.id in {"outcome", "drop"}:
                expressions.append(node.args[0 if node.func.id == "outcome" else 1])
    if module is gateway_access:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"observation", "denial_kind", "routing_issue"}:
                expressions.extend(child.value for child in ast.walk(node) if isinstance(child, ast.Return)
                                   and child.value is not None)
        identity_map = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == "_IDENTITY_FAILURE_REASONS" for t in node.targets))
        expressions.extend(identity_map.values)
    if module is traffic:
        blocked = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "blocked_offer")
        expressions.extend(node.value for node in ast.walk(blocked) if isinstance(node, ast.Return))
    return tree, [expression for expression in expressions if expression is not None]


@pytest.mark.parametrize("module", [gateway_policy, gateway_access, traffic, multi_user])
def test_vocabulary_writes_use_declared_constants(module):
    tree, expressions = _vocabulary_writes(module)
    for expression in expressions:
        assert not _string_literals(expression), (module.__name__, ast.unparse(expression))
    prefixes, declared = {
        gateway_policy: (("GATEWAY_POLICY_",), set(POLICY_PAIRS)),
        gateway_access: (("GATEWAY_ACCESS_",), ACCESS_REASONS | {"allow", "deny", "unverified", "unknown", "authentication", "model_access"}),
        traffic: (("OFFER_",), TRAFFIC_OUTCOMES), multi_user: (("OFFER_",), MULTI_USER_OUTCOMES),
    }[module]
    referenced = {getattr(states, node.attr) for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id == "states" and node.attr.startswith(prefixes)}
    assert referenced == declared


@pytest.mark.parametrize("guard,args", [
    (states.check_gateway_policy_outcome, ("new", "pass")),
    (states.check_gateway_policy_outcome, ("observed", "fail")),
    (states.check_gateway_policy_outcome, ("unverified", "new")),
    (states.check_gateway_access_observation, ("new", "not_run")),
    (states.check_gateway_access_observation, ("allow", "new")),
    (states.gateway_access_case_status, ("new", "allow")),
    (states.gateway_access_case_status, ("allow", "new")),
    (states.check_offer_transition, ("new", "started")),
    (states.check_offer_transition, ("started", "new")),
    (states.check_offer_transition, ("dropped_local", "started")),
    (states.check_offer_transition, ("started", "started")),
    (states.check_multi_user_offer_outcome, ("started",)),
    (states.check_multi_user_offer_outcome, ("new",)),
])
def test_guards_raise_in_tests_and_warn_in_production(guard, args, monkeypatch, caplog):
    assert states.STRICT_TRANSITIONS
    with pytest.raises(AssertionError, match="Undeclared"):
        guard(*args)
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    guard(*args)
    assert "Undeclared" in caplog.text


@pytest.mark.parametrize("expected,observed,status", [
    ("allow", "allow", "pass"), ("allow", "deny", "fail"), ("allow", "unverified", "inconclusive"),
    ("deny", "allow", "fail"), ("deny", "deny", "pass"), ("deny", "unverified", "inconclusive"),
])
def test_access_status_rule_and_row_builder(expected, observed, status, monkeypatch):
    from giraffe.models import RequestRecord

    assert states.gateway_access_case_status(expected, observed) == status
    row = gateway_access._row(GatewayAccessCase(name="case", identity="user", expected=expected))
    assert (row["observed"], row["reason"], row["status"]) == ("unverified", "not_run", "inconclusive")
    record = RequestRecord(id="r", target="t", fixture_id="f", scenario="access", started_at="now",
                           status="completed", valid=True, check_ids=["gateway_access"])
    monkeypatch.setattr(gateway_access, "observation", lambda _: (observed, "valid_completion"))
    gateway_access._record_case(row, record)
    assert row["status"] == status


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
@pytest.mark.parametrize("behavior,qualification,status", [
    ("enforce", "observed", "pass"), ("overadmit", "observed_failure", "fail"),
    ("unknown", "unverified", "inconclusive"), ("ignore", "unexercised", "inconclusive"),
])
async def test_policy_pairs_match_existing_scenarios(monkeypatch, mode, behavior, qualification, status):
    check, _, _ = await exercise(monkeypatch, mode, behavior=behavior)
    assert (check.metrics["gateway_policy"]["qualification"], check.status) == (qualification, status)
    states.check_gateway_policy_outcome(qualification, status)


@pytest.mark.parametrize("settings", [None, configuration(gateway="unsupported").gateway_policy])
def test_unavailable_policy_pair(settings):
    summary = gateway_policy.summarize(settings, {}, [])
    assert (summary["qualification"], summary["status"]) == ("unavailable", "inconclusive")


def test_policy_finalization_checks_the_pair(monkeypatch):
    monkeypatch.setattr(states, "GATEWAY_POLICY_UNAVAILABLE", "new_qualification")
    with pytest.raises(AssertionError, match="Undeclared gateway policy outcome"):
        gateway_policy.summarize(None, {}, [])


@pytest.mark.parametrize("field,value", [("observed", "new_observation"), ("reason", "new_reason")])
def test_access_finalization_checks_unrun_rows(field, value):
    from giraffe.runner import _Run
    from tests.test_gateway_access import config

    cfg = config()
    run = _Run(cfg, None, None)
    row = gateway_access._row(cfg.gateway_access.cases[0])
    row[field] = value
    run.observations["proxy"]["gateway_access"] = {"cases": [row]}
    with pytest.raises(AssertionError, match="Undeclared gateway access observation"):
        gateway_access.result(run, cfg.targets[0])


def test_production_access_finalization_accepts_rows_without_reason(monkeypatch):
    from giraffe.runner import _Run
    from tests.test_gateway_access import config

    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    cfg = config()
    run = _Run(cfg, None, None)
    rows = [{**case.model_dump(), "observed": case.expected, "status": "pass", "request_id": None}
            for case in cfg.gateway_access.cases]
    run.observations["proxy"]["gateway_access"] = {"cases": rows}
    check = gateway_access.result(run, cfg.targets[0])
    assert check.status == "pass"
    assert check.metrics["gateway_access"]["coverage_complete"]
    assert check.metrics["gateway_access"]["cases"] == rows
    assert all("reason" not in row for row in rows)


@pytest.mark.parametrize("excluded", ["not_dispatched", "budget_exhausted"])
async def test_policy_downgrade_keeps_declared_pair(monkeypatch, excluded):
    _, run, _ = await exercise(monkeypatch)
    original = next(record for record in run.records if record.identity == "peer" and record.valid)
    changes = (dict(not_dispatched=True, dispatch_ms=None) if excluded == "not_dispatched" else
               dict(finish_reason="length", output="", reasoning="thinking", error="response contained no useful answer output"))
    extra = original.model_copy(update=dict(id="excluded", status="failed", valid=False, **changes))
    summary = gateway_policy.summarize(run.config.gateway_policy, run.observations["proxy"]["gateway_policy"],
                                       [*run.records, extra])
    assert (summary["qualification"], summary["status"]) == ("unverified", "inconclusive")


def test_traffic_real_writes_and_summary_counts(monkeypatch):
    seen = set()
    original = states.check_offer_transition

    def checked(old, new):
        original(old, new)
        seen.add((old, new))

    monkeypatch.setattr(states, "check_offer_transition", checked)

    async def scenario():
        monkeypatch.setattr("tests.test_traffic.time", traffic.time)
        harness = Harness()
        normal = await harness.run(profile(), schedule_override=[(0, "short")])
        assert (normal["started"], normal["not_offered"]) == (1, 0)
        local = await Harness(slots=0).run(schedule_override=[(0, "short")])
        assert local["dropped_local"] == 1
        late = await Harness().run(schedule_override=[(-1, "short")])
        assert late["dropped_late"] == 1
        explicit = traffic.ExplicitTrafficStage(duration_seconds=.08, max_in_flight=4, scheduler_lag_tolerance_ms=10)

        async def refused(*_):
            return None

        async def explicit_run(execute, reserve=None, **options):
            from giraffe.models import RequestSpec

            return await traffic.run_stage(
                traffic=explicit, rate_rps=1, stage_index=0,
                fixtures={"short": [RequestSpec(fixture_id="short", scenario="traffic", check_ids=["traffic"], messages=[])]},
                execute=execute, reserve_offer=reserve or harness.reserve, try_acquire=harness.acquire,
                release=harness.release, stop_reason=lambda: harness.stop,
                deadline=asyncio.get_running_loop().time() + 5, limits=Limits(min_samples=2),
                schedule_override=[(0, "short")], **options)

        refused_result = await explicit_run(refused)
        assert refused_result["not_offered"] == 1

        async def cancelled(*_):
            raise asyncio.CancelledError

        cancelled_result = await explicit_run(cancelled)
        assert cancelled_result["not_offered"] == 1

        def stopped_reservation():
            harness.stop = "stopped after reservation"
            return harness.reserve()

        stopped = await explicit_run(refused, reserve=stopped_reservation)
        assert stopped["not_offered"] == 1
        harness.stop = None

        create_task = asyncio.create_task

        def stopped_task(coroutine):
            task = create_task(coroutine)
            harness.stop = "stopped before coroutine entry"
            return task

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", stopped_task)
            stopped = await explicit_run(refused)
        assert stopped["not_offered"] == 1
        harness.stop = None

        def delayed_task(coroutine):
            task = create_task(coroutine)
            asyncio.get_running_loop().now += .02
            return task

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", delayed_task)
            late_task = await explicit_run(refused)
        assert late_task["dropped_late"] == 1

        def delayed_spec(_):
            from giraffe.models import RequestSpec

            asyncio.get_running_loop().now += .02
            return RequestSpec(fixture_id="short", scenario="traffic", check_ids=["traffic"], messages=[])

        late_spec = await explicit_run(refused, spec_factory=delayed_spec)
        assert late_spec["dropped_late"] == 1

    run_virtual(monkeypatch, scenario())
    assert seen == {(old, new) for old, successors in TRANSITIONS.items() for new in successors}


def test_traffic_folding_is_separate_from_multi_user_accounting(monkeypatch):
    from dataclasses import replace

    captured = []

    async def scenario():
        monkeypatch.setattr("tests.test_traffic.time", traffic.time)
        await Harness().run(profile(), schedule_override=[(0, "short")], capture_evidence=captured.append)

    run_virtual(monkeypatch, scenario())
    evidence = replace(captured[0], schedule=((0, "short"),) * 6,
                       outcomes=("not_offered", "dispatching", "started", "cancelled", "dropped_local", "dropped_late"))
    summary = traffic.finalize_stage(evidence, Limits(min_samples=2))
    assert {key: summary[key] for key in ("scheduled", "started", "not_offered", "dropped_local", "dropped_late")} == {
        "scheduled": 6, "started": 2, "not_offered": 2, "dropped_local": 1, "dropped_late": 1,
    }
    offers = [{"outcome": outcome, "offered": outcome != "not_offered"} for outcome in MULTI_USER_OUTCOMES]
    assert multi_user._offer_counts(offers) == {
        "planned": 5, "offered": 4, "dispatched": 1, "dropped_local": 1, "dropped_late": 1,
        "unissued": 1, "not_offered": 1,
    }


@pytest.mark.parametrize("old,new", [("dispatching", "started"), ("started", "not_offered"),
                                    ("started", "cancelled"), ("cancelled", "not_offered")])
def test_strict_traffic_guard_errors_escape_dispatch_tasks(monkeypatch, old, new):
    from giraffe.models import RequestSpec

    transitions = {key: value - {new} if key == old else value for key, value in states.OFFER_TRANSITIONS.items()}
    monkeypatch.setattr(states, "OFFER_TRANSITIONS", transitions)

    async def scenario():
        async def execute(*_):
            if new == "cancelled" or old == "cancelled":
                raise asyncio.CancelledError
            return None

        with pytest.raises(AssertionError, match="Undeclared offer transition"):
            await traffic.run_stage(
                traffic=traffic.ExplicitTrafficStage(duration_seconds=.08, max_in_flight=1,
                                                    scheduler_lag_tolerance_ms=10),
                rate_rps=1, stage_index=0,
                fixtures={"short": [RequestSpec(fixture_id="short", scenario="traffic", check_ids=["traffic"], messages=[])]},
                execute=execute, reserve_offer=lambda: True, try_acquire=lambda: True,
                release=lambda: None, stop_reason=lambda: None,
                deadline=asyncio.get_running_loop().time() + 5, limits=Limits(min_samples=2),
                schedule_override=[(0, "short")])

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("error", [AssertionError, RuntimeError])
def test_production_traffic_preserves_callback_failure_counts(monkeypatch, error):
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)

    async def scenario():
        monkeypatch.setattr("tests.test_traffic.time", traffic.time)
        harness = Harness()

        async def broken_callback(*_):
            raise error("private callback details")

        harness.execute = broken_callback
        result = await harness.run(schedule_override=[(0, "short")])
        assert result["failed"] == result["started"] == result["errors"] == result["evaluated"] == 1
        assert result["status"] == "fail" and result["error_rate"] == 1
        assert "private callback details" not in str(result)

    run_virtual(monkeypatch, scenario())


def test_production_traffic_does_not_retrieve_escaped_dispatch_exception(monkeypatch):
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)

    async def scenario():
        monkeypatch.setattr("tests.test_traffic.time", traffic.time)
        harness = Harness()
        created = []
        create_task = asyncio.create_task
        error = RuntimeError("drop callback failed")

        def delayed_task(coroutine):
            task = create_task(coroutine)
            created.append(task)
            # Make dispatch drop the offer before entering its exception handler.
            asyncio.get_running_loop().now += .02
            return task

        def broken_drop():
            raise error

        monkeypatch.setattr(asyncio, "create_task", delayed_task)
        try:
            result = await harness.run(profile(scheduler_lag_tolerance_ms=10),
                                       schedule_override=[(0, "short")], on_drop=broken_drop)
            assert result["dropped_late"] == 1
            assert harness.starts == []
            task, = created
            assert task.done() and not task.cancelled()
            # CPython clears this flag when exception() or result() retrieves it.
            assert task._log_traceback
        finally:
            # Retrieve after checking so the regression test itself stays quiet.
            for task in created:
                if task.done() and not task.cancelled():
                    assert task.exception() is error

    run_virtual(monkeypatch, scenario())


def test_docs_cover_all_new_values():
    docs = (Path(__file__).resolve().parents[1] / "docs" / "states.md").read_text()
    for value in set(POLICY_PAIRS) | ACCESS_REASONS | TRAFFIC_OUTCOMES | MULTI_USER_OUTCOMES:
        assert f"`{value}`" in docs


def test_declared_policy_pairs_are_the_only_accepted_pairs():
    for qualification, allowed in POLICY_PAIRS.items():
        for status in states.CHECK_STATUSES:
            if status in allowed:
                states.check_gateway_policy_outcome(qualification, status)
            else:
                with pytest.raises(AssertionError, match="Undeclared gateway policy outcome"):
                    states.check_gateway_policy_outcome(qualification, status)


@pytest.mark.usefixtures("local_tokenizer")
def test_multi_user_finalization_checks_its_own_subset(monkeypatch):
    from giraffe.runner import _Run
    from tests.test_multi_user_arrivals import arrival_settings
    from tests.test_runner import FakeClient

    monkeypatch.setattr(states, "MULTI_USER_OFFER_OUTCOMES",
                        tuple(value for value in states.MULTI_USER_OFFER_OUTCOMES if value != "dispatched"))

    async def scenario():
        cfg = arrival_settings()
        run = _Run(cfg, None, None)
        target = cfg.targets[0]
        with pytest.raises(AssertionError, match="Undeclared multi-user offer outcome: 'dispatched'"):
            await multi_user.execute(run, target, FakeClient(target, cfg))
        assert run.slots.qsize() == cfg.concurrency

    run_virtual(monkeypatch, scenario())


def test_strict_traffic_preserves_callback_assertion_accounting(monkeypatch):
    assert states.STRICT_TRANSITIONS

    async def scenario():
        monkeypatch.setattr("tests.test_traffic.time", traffic.time)
        harness = Harness()

        async def broken_callback(*_):
            raise AssertionError("private callback details")

        harness.execute = broken_callback
        result = await harness.run(schedule_override=[(0, "short")])
        assert result["failed"] == result["started"] == result["errors"] == result["evaluated"] == 1
        assert result["status"] == "fail" and result["error_rate"] == 1
        assert "private callback details" not in str(result)

    run_virtual(monkeypatch, scenario())


def test_strict_offer_guard_before_task_creation_releases_slot(monkeypatch):
    transitions = {key: value - {"dispatching"} if key == "not_offered" else value
                   for key, value in states.OFFER_TRANSITIONS.items()}
    monkeypatch.setattr(states, "OFFER_TRANSITIONS", transitions)

    async def scenario():
        monkeypatch.setattr("tests.test_traffic.time", traffic.time)
        harness = Harness()
        with pytest.raises(AssertionError, match="Undeclared offer transition"):
            await harness.run(schedule_override=[(0, "short")])
        assert harness.available == harness.slots
        assert harness.starts == []

    run_virtual(monkeypatch, scenario())
