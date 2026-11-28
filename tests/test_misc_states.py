"""Contracts for the small provenance, observation, and estimate vocabularies."""
import ast
import asyncio
import inspect
import json

import pytest

from giraffe import deployment, judging, local_tokenizer, planning, reporting, runner, states
from tests.test_local_tokenizer import run_and_target
from tests.test_model_residency import config as residency_config, inventory
from tests.test_reporting import make_report
from tests.test_judging import configuration, fixture, record, response, transport
from tests.test_planning import config, load_config
from tests.test_run_limits_phase4_planning import observations


VOCABULARIES = {
    "tokenizer_sources": ("target_model", "remembered_mapping", "server_model_root", "hub_resolution",
                          "builtin_fallback", "configured", "builtin"),
    "tokenizer_calibration_statuses": ("calibrated", "unavailable"),
    "tokenizer_calibration_reasons": ("tokenizer_unavailable", "minimum_prompt_length",
                                      "input_sizing_unavailable", "context_window_exceeded"),
    "residency_states": ("unknown", "resident", "not_resident"),
    "residency_phases": ("before_preparation", "after_preparation"),
    "plan_confidence": ("low", "medium", "high"),
    "judging_stop_reasons": ("cancelled", "phase_deadline"),
    "judge_vote_error_reasons": ("cancelled", "phase_deadline", "not_evaluated", "timeout", "http_error",
                                "request_failed", "truncated_response", "invalid_completion",
                                "invalid_verdict", "client_initialization"),
}


def test_declarations_and_description():
    description = states.describe()
    assert json.loads(json.dumps(description)) == description
    for key, values in VOCABULARIES.items():
        assert getattr(states, key.upper()) == values
        assert description[key] == list(values)
    assert local_tokenizer.AUTO == "auto"
    assert "automatic" not in states.TOKENIZER_SOURCES


def _literals(expression):
    """Ignore keys and attribute names, but include any new bare value or typo."""
    parents = {child: parent for parent in ast.walk(expression) for child in ast.iter_child_nodes(parent)}
    for child in ast.walk(expression):
        if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
            continue
        parent = parents.get(child)
        ancestor = parent
        while ancestor is not None and not isinstance(ancestor, ast.Compare):
            ancestor = parents.get(ancestor)
        if ancestor is not None:
            continue
        if isinstance(parent, ast.Subscript) and parent.slice is child:
            continue
        if isinstance(parent, ast.Dict) and child in parent.keys:
            continue
        if isinstance(parent, ast.Call):
            if isinstance(parent.func, ast.Attribute) and parent.func.attr == "get" and parent.args[0] is child:
                continue
            if isinstance(parent.func, ast.Name) and parent.func.id == "getattr" and parent.args[1] is child:
                continue
        yield child.value


def _writes(tree, fields):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in fields:
                    yield value
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in fields:
                    yield keyword.value
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)):
                    for name, value in zip(target.elts, node.value.elts):
                        if isinstance(name, ast.Name) and name.id in fields:
                            yield value
                if ((isinstance(target, ast.Name) and target.id in fields)
                        or (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                            and target.slice.value in fields)):
                    yield node.value


@pytest.mark.parametrize("function,fields", [
    (local_tokenizer._resolve_tokenizer, {"source"}),
    (local_tokenizer.ensure_tokenizers, {"source"}),
    (local_tokenizer.prepare_messages, {"source", "status", "reason_code", "tokenizer_source"}),
    (deployment.unknown_residency, {"state"}),
    (deployment.model_residency, {"state"}),
    (planning._time_plan, {"confidence"}),
    (judging.judge_records, {"stop_reason"}),
    (judging._vote, {"category"}),
    (runner._stop_judging, {"stop_reason"}),
])
def test_writes_use_constants(function, fields):
    tree = ast.parse(inspect.getsource(function))
    expressions = list(_writes(tree, fields))
    if function is local_tokenizer._resolve_tokenizer:
        expressions += [node.value.elts[1] for node in ast.walk(tree)
                        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)]
    if function in (judging.judge_records, judging._vote):
        expressions += [node.args[1] for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "_error"]
    assert expressions
    for expression in expressions:
        assert not list(_literals(expression)), (function.__name__, ast.unparse(expression))


def test_phase_writers_and_html_defaults_use_constants():
    for module in (runner, reporting):
        tree = ast.parse(inspect.getsource(module))
        assert not [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and node.value in {"before_preparation", "after_preparation"}]
    tree = ast.parse(inspect.getsource(reporting))
    defaults = [node.args[1] for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "state"]
    assert defaults
    assert all(not list(_literals(value)) for value in defaults)


@pytest.mark.parametrize("guard,args", [
    (states.check_tokenizer_source, ("new",)),
    (states.check_tokenizer_calibration, ("new", None)),
    (states.check_tokenizer_calibration, ("calibrated", "new")),
    (states.check_residency_state, ("new",)),
    (states.check_plan_confidence, ("new",)),
    (states.check_judging_stop_reason, ("new",)),
    (states.check_judge_vote_error_reason, ("new",)),
])
def test_guards_raise_in_strict_mode_and_warn_in_production(guard, args, monkeypatch, caplog):
    assert states.STRICT_TRANSITIONS
    with pytest.raises(AssertionError, match="Undeclared"):
        guard(*args)
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    guard(*args)
    assert "Undeclared" in caplog.text


def test_guards_accept_declared_values(caplog):
    for value in VOCABULARIES["tokenizer_sources"]:
        states.check_tokenizer_source(value)
    for status in VOCABULARIES["tokenizer_calibration_statuses"]:
        for reason in (None, *VOCABULARIES["tokenizer_calibration_reasons"]):
            states.check_tokenizer_calibration(status, reason)
    for value in VOCABULARIES["residency_states"]:
        states.check_residency_state(value)
    for value in VOCABULARIES["plan_confidence"]:
        states.check_plan_confidence(value)
    for value in (None, *VOCABULARIES["judging_stop_reasons"]):
        states.check_judging_stop_reason(value)
    for value in VOCABULARIES["judge_vote_error_reasons"]:
        states.check_judge_vote_error_reason(value)
    assert not caplog.text


@pytest.mark.parametrize("state", ["unknown", "resident", "not_resident", '<script>alert("x")</script>'])
def test_residency_html_preserves_states_and_escapes_undeclared_values(state, tmp_path):
    report = make_report(count=0)
    report.observations = {"targets": {"local": {"deployment": {"model_residency": {
        phase: {"state": state, "source": "ollama /api/ps", "collected_at": "now", "scope": "one endpoint"}
        for phase in ("before_preparation", "after_preparation")}}}}}
    _, path = reporting.write_report(report, tmp_path, html=True)
    html = path.read_text()
    import html as escaping
    expected = escaping.escape(state, quote=True)
    assert f'<strong>Before preparation:</strong> {expected} · ollama /api/ps · now · one endpoint' in html
    assert f'<strong>After preparation:</strong> {expected} · ollama /api/ps · now · one endpoint' in html
    if state.startswith("<"):
        assert state not in html


async def test_tokenizer_setup_writer_checks_resolved_source(monkeypatch):
    cfg = residency_config()
    cfg.checks = ["context"]
    async def resolve(*args, **kwargs):
        return local_tokenizer.BUILTIN, "new_source"
    async def verify(*args, **kwargs):
        return {}
    monkeypatch.setattr(local_tokenizer, "_resolve_tokenizer", resolve)
    monkeypatch.setattr(local_tokenizer, "_verify_tokenizer", verify)
    with pytest.raises(AssertionError, match="Undeclared tokenizer source"):
        await local_tokenizer.ensure_tokenizers(cfg)
    assert not cfg.targets[0]._tokenizer_setup


async def test_tokenizer_reader_checks_saved_source():
    run, target, _ = run_and_target()
    target._tokenizer_setup = {"source": "new_source"}
    with pytest.raises(AssertionError, match="Undeclared tokenizer source"):
        await local_tokenizer.prepare_messages(run, target, "load", lambda n: [], 100)


@pytest.mark.parametrize("path", ["cached_failure", "target_budget", "final_failure"])
async def test_calibration_builder_guards_every_return(monkeypatch, path):
    run, target, _ = run_and_target()
    if path == "cached_failure":
        run._tokenizer_unavailable = {target.tokenizer: "cached failure"}
    elif path == "target_budget":
        run._tokenizer_preparation_seconds = {target.name: local_tokenizer.TARGET_PREPARATION_TIMEOUT_SECONDS}
    else:
        async def fail(*args, **kwargs):
            raise OSError("worker unavailable")
        monkeypatch.setattr(local_tokenizer, "_start_worker", fail)
    monkeypatch.setattr(states, "TOKENIZER_CALIBRATION_UNAVAILABLE", "new_status")
    with pytest.raises(AssertionError, match="Undeclared tokenizer calibration"):
        await local_tokenizer.prepare_messages(run, target, "load", lambda n: [], 100)


async def test_residency_writers_are_guarded(monkeypatch):
    cfg = residency_config()
    monkeypatch.setattr(states, "RESIDENCY_UNKNOWN", "new_state")
    with pytest.raises(AssertionError, match="Undeclared residency state"):
        deployment.unknown_residency(cfg.targets[0], "unobserved")
    monkeypatch.undo()
    inventory(monkeypatch, {"models": [{"model": "tiny"}]})
    monkeypatch.setattr(states, "RESIDENCY_RESIDENT", "new_state")
    with pytest.raises(AssertionError, match="Undeclared residency state"):
        await deployment.model_residency(cfg.targets[0], cfg)


@pytest.mark.parametrize("case,expected", [
    ("unknown", "low"), ("empty", "low"), ("uncertain", "low"),
    ("fixed", "high"), ("mixed", "medium"), ("wide", "low"),
])
def test_plan_confidence_emits_every_rule_branch(case, expected):
    if case in {"fixed", "mixed", "wide"}:
        configured = load_config(duration=10, finish={"fixed": 1, "mixed": 5, "wide": 30}[case])
        measured = None
    elif case == "empty":
        configured, measured = config(checks=["access"]), None
    else:
        configured = config(checks=["correctness"])
        measured = observations(configured) if case == "uncertain" else None
        if measured:
            for row in measured["local"]:
                row["n"] = 1
    result = planning.request_plan(configured, observations=measured)
    assert result["confidence"] == expected
    details = result["confidence_details"]
    if case == "unknown":
        assert result["unknown_time_components"]
    elif case == "empty":
        assert not result["time_components"]
    elif case == "uncertain":
        assert details["insufficient_observations"]
        assert not result["unknown_time_components"]
    elif case == "fixed":
        assert details["fixed_fraction"] >= .8
        assert details["drain_uncertainty_fraction"] <= .2
    elif case == "mixed":
        assert details["drain_uncertainty_fraction"] > .2
        assert result["estimated_high_seconds"] - result["estimated_low_seconds"] <= result["estimated_seconds"]
    else:
        assert details["insufficient_observations"] == [
            "The combined duration range is too wide for medium confidence."]
        assert result["estimated_high_seconds"] - result["estimated_low_seconds"] > result["estimated_seconds"]


def test_plan_emission_checks_confidence(monkeypatch):
    monkeypatch.setattr(states, "PLAN_CONFIDENCE_LOW", "undeclared")
    with pytest.raises(AssertionError, match="Undeclared plan confidence"):
        planning.request_plan(config(checks=["correctness"]))


@pytest.mark.parametrize("category", states.JUDGE_VOTE_ERROR_REASONS)
def test_vote_error_writer_accepts_declared_categories(category):
    vote = judging._error(configuration().judging.judges[0], category)
    assert vote.error_category == category


def test_vote_error_writer_guards_strict_and_production(monkeypatch, caplog):
    judge = configuration().judging.judges[0]
    with pytest.raises(AssertionError, match="Undeclared judge vote error reason"):
        judging._error(judge, "unexpected")
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    assert judging._error(judge, "unexpected").error_category == "unexpected"
    assert "Undeclared judge vote error reason" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_judging_emits_stop_and_vote_reasons(monkeypatch, cancel):
    stop = asyncio.Event()

    async def handler(request):
        if cancel:
            stop.set()
        await asyncio.sleep(10)
        return response('{"verdict":true,"reason":"Equivalent."}')

    clients = transport(monkeypatch, handler)
    item = record()
    events = []
    summary = await judging.judge_records(
        configuration(max_duration_seconds=60 if cancel else .02), [item], [fixture()],
        stop_event=stop, progress=events.append)
    expected = "cancelled" if cancel else "phase_deadline"
    assert summary["stop_reason"] == expected
    assert events[-1]["stop_reason"] == expected
    assert {vote.error_category for vote in item.judgement.votes} == {expected}
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_judging_summary_writer_checks_stop_reason(monkeypatch):
    stop = asyncio.Event()
    stop.set()
    # Error votes use the same reason; admit it there so the summary guard is
    # independently exercised at its persisted write.
    monkeypatch.setattr(states, "JUDGING_STOP_CANCELLED", "unexpected")
    monkeypatch.setattr(states, "JUDGE_VOTE_ERROR_REASONS", (*states.JUDGE_VOTE_ERROR_REASONS, "unexpected"))
    with pytest.raises(AssertionError, match="Undeclared judging stop reason"):
        await judging.judge_records(configuration(), [record()], [fixture()], stop_event=stop)


@pytest.mark.asyncio
async def test_runner_judging_cancellation_fallback_reports_cancelled():
    from types import SimpleNamespace

    evaluation = asyncio.create_task(asyncio.sleep(10))
    evaluation.cancel()
    await asyncio.gather(evaluation, return_exceptions=True)
    run = SimpleNamespace(stop=asyncio.Event(), pack_specs=[fixture()], records=[record()], config=configuration())
    assert (await runner._stop_judging(evaluation, run))["stop_reason"] == states.JUDGING_STOP_CANCELLED


async def test_judging_worker_does_not_swallow_strict_vote_guard(monkeypatch):
    async def handler(request):
        await asyncio.sleep(.1)
        return response('{"verdict":true,"reason":"Equivalent."}')

    transport(monkeypatch, handler)
    monkeypatch.setattr(states, "JUDGE_VOTE_ERROR_TIMEOUT", "unexpected")
    with pytest.raises(ExceptionGroup) as caught:
        await judging.judge_records(configuration(request_timeout_seconds=.01), [record()], [fixture()])
    assert caught.value.subgroup(AssertionError) is not None
    assert "Undeclared judge vote error reason" in str(caught.value.exceptions[0])


def test_runner_calibration_record_updates_use_constants():
    tree = ast.parse(inspect.getsource(runner._context_fixtures))
    expressions = [kw.value for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Name) and node.func.value.id == "result"
                   and node.func.attr == "update" for kw in node.keywords
                   if kw.arg in {"status", "reason_code"}]
    assert expressions
    for expression in expressions:
        assert not list(_literals(expression)), ast.unparse(expression)
