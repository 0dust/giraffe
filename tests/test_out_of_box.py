"""Default health checks and stop causes, without a listening endpoint."""

import asyncio
from pathlib import Path

import pytest

from giraffe import runner
from giraffe.models import DEFAULT_CHECKS, RunConfig, Target
from giraffe.web import _bootstrap_config
from tests.test_check_intent_round2_core import record


def configuration(**changes):
    return RunConfig(targets=[Target(name="t", model="m", url="http://local")], **changes)


def test_shared_health_defaults_and_bootstrap():
    assert DEFAULT_CHECKS == ("access", "serving", "correctness", "first_output")
    assert configuration().checks == list(DEFAULT_CHECKS)
    assert _bootstrap_config(None)["checks"] == list(DEFAULT_CHECKS)
    assert configuration().limits.first_output_ms is None
    assert configuration().effective_check("first_output")["limits"]["first_output_ms"] == 10000
    app = (Path(__file__).parents[1] / "src/giraffe/static/app.js").read_text()
    assert "state.config.checks=clone(state.bootstrap.config.checks)" in app


def test_generation_stall_default_yields_to_explicit_limits():
    assert configuration().effective_check("generation")["limits"]["stream_gap_ms"] == 5000
    assert configuration(limits={"latency_ms": 9000}).effective_check("generation")["limits"]["stream_gap_ms"] is None
    assert configuration(test_options={"generation": {"limits": {"stream_gap_ms": 7000}}}).effective_check("generation")["limits"]["stream_gap_ms"] == 7000


def test_user_stop_summary_has_counts_and_no_budget_advice():
    config = configuration(checks=["serving"], samples=5, max_requests=200)
    run = runner._Run(config, None, asyncio.Event())
    run.reason = "cancelled by user"
    run.unfinished["t"].add("serving")
    run.records = [record(str(i), check_ids=["serving"]) for i in range(2)]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "serving")
    assert result.status == "inconclusive"
    assert result.summary == "Stopped by you after 2 of 6 planned requests."
    assert result.summary.endswith(" planned requests.")
    assert "budget" not in result.summary.lower()
    assert "To conclude" not in result.summary


def test_explicitly_unset_first_output_limit_names_flag_and_key():
    config = configuration(checks=["first_output"], limits={"first_output_ms": None})
    run = runner._Run(config, None, None)
    run.records = [record(str(i), check_ids=["first_output"]) for i in range(5)]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "first_output")
    assert result.status == "inconclusive"
    assert "--first-output-ms" in result.summary
    assert "limits.first_output_ms" in result.summary


def test_first_output_default_uses_p95_not_a_single_outlier():
    config = configuration(checks=["first_output"])
    run = runner._Run(config, None, None)
    run.records = [record(str(i), check_ids=["first_output"], first_output_ms=1 if i < 20 else 20000)
                   for i in range(21)]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "first_output")
    assert result.metrics["first_output_p95_ms"] == 1
    assert result.status == "pass"
    run.records[-2].first_output_ms = 20000
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "first_output")
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_healthy_endpoint_passes_shared_defaults(monkeypatch):
    config = configuration()
    assert config.effective_check("correctness")["concurrency"] == 4
    # Correctness answers depend on the prompt; use the fixture-aware fake client.
    from tests.test_runner import FakeClient
    for name, value in {"delay": .004, "active": 0, "peak": 0, "closed": [], "scenarios": [],
                        "bad_target": None, "initial_slow": False, "bad_sustained": False,
                        "useful_event": True, "input_usage": True, "bad_mixed_gap": False}.items():
        monkeypatch.setattr(FakeClient, name, value)
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    report = await runner.run_suite(config)
    assert report.overall == "pass"
    assert {c.id for c in report.checks if c.status == "pass"} == set(DEFAULT_CHECKS)
    assert report.abort_reason is None


@pytest.mark.asyncio
async def test_generation_default_passes_and_explicit_gap_overrides(monkeypatch):
    from tests.test_check_intent_core import install, streamed

    install(monkeypatch, lambda payload: streamed(8, .005))
    report = await runner.run_suite(configuration(checks=["generation"]))
    assert next(c for c in report.checks if c.id == "generation").status == "pass"
    strict = await runner.run_suite(configuration(checks=["generation"], limits={"stream_gap_ms": 1}))
    assert next(c for c in strict.checks if c.id == "generation").status == "fail"


@pytest.mark.parametrize("reason,flag", [
    ("maximum request budget reached", "--max-requests"),
    ("maximum run duration reached", "--max-duration"),
])
def test_budget_and_deadline_keep_specific_remedies(reason, flag):
    config = configuration(checks=["serving"], samples=5, max_requests=2, max_duration_seconds=10)
    run = runner._Run(config, None, None)
    run.reason = reason
    run.unfinished["t"].add("serving")
    run.records = [record(check_ids=["serving"])]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "serving")
    assert result.status == "inconclusive"
    assert flag in result.summary
    assert "Stopped by you" not in result.summary
    assert "To conclude, Raise" not in result.summary
    if flag == "--max-requests":
        assert "to at least 6" in result.summary


@pytest.mark.parametrize("budget", [200, None])
def test_user_stop_of_duration_workload_uses_request_budget_when_set(budget):
    config = configuration(checks=["recovery"], max_requests=budget)
    run = runner._Run(config, None, None)
    run.reason = "cancelled by user"
    run.unfinished["t"].add("recovery")
    run.records = [record(str(i), check_ids=["recovery"]) for i in range(12)]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == "recovery")
    assert result.summary == ("Stopped by you after 12 of 200 planned requests." if budget else
        "Stopped by you after 12 requests. This duration-based workload has no fixed request count.")
    assert "--max-requests" not in result.summary and "--max-duration" not in result.summary


@pytest.mark.parametrize('options', [{}, {'limits': {'stream_gap_ms': None}},
    {'test_options': {'generation': {'limits': {'stream_gap_ms': None}}}}])
def test_generation_gap_default_preserves_explicit_null(options):
    config = configuration(**options)
    expected = 5000 if not options else None
    assert config.effective_check('generation')['limits']['stream_gap_ms'] == expected
    restored = RunConfig.model_validate(config.model_dump(mode='json'))
    assert restored.effective_check('generation')['limits']['stream_gap_ms'] == expected


def test_first_output_default_does_not_change_other_checks():
    config = configuration()
    assert config.limits.first_output_ms is None
    assert config.effective_check('first_output')['limits']['first_output_ms'] == 10000
    for check_id in ('recovery', 'prefix', 'sessions', 'consistency', 'tools', 'arrivals', 'capacity'):
        assert config.effective_check(check_id)['limits']['first_output_ms'] is None


@pytest.mark.parametrize('check_id', ['correctness', 'json'])
@pytest.mark.parametrize('explicit', [None, 'global', 'per_check'])
@pytest.mark.parametrize('all_correct', [False, True])
def test_fully_scored_serial_answers_conclude_only_when_parallelism_inherited(check_id, explicit, all_correct):
    values = {'checks': [check_id], 'stream': False}
    if explicit == 'global':
        values['default_concurrency'] = 4
    elif explicit == 'per_check':
        values['test_options'] = {check_id: {'concurrency': 4}}
    config = configuration(**values)
    run = runner._Run(config, None, None)
    run.records = [record(str(i), scenario=check_id + '_c4', check_ids=[check_id], stream=False)
                   for i in range(6)]
    if not all_correct:
        run.records[0].score = False
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == check_id)
    assert result.status == ('fail' if not all_correct else 'inconclusive' if explicit else 'pass')
    if not explicit:
        assert result.metrics['concurrency_evidence'].startswith('not observed')
        assert f'{6 if all_correct else 5}/6 correct; server answered one at a time' in result.summary


@pytest.mark.parametrize('reason', ['cancelled by user', 'maximum request budget reached', 'maximum run duration reached'])
def test_stop_does_not_replace_endpoint_failures_or_unrelated_inconclusive_reasons(reason):
    config = configuration(checks=['serving', 'first_output'], samples=5, max_requests=10,
                           limits={'first_output_ms': None})
    run = runner._Run(config, None, None)
    run.reason = reason
    run.unfinished['t'].update(config.checks)
    run.records = [record('bad', check_ids=['serving'], status='failed', valid=False, score=None),
                   record('few', check_ids=['first_output'])]
    checks = {c.id: c for c in runner._checks(run, config.targets[0])}
    assert checks['serving'].status == 'fail'
    assert 'Invalid completions' in checks['serving'].summary
    assert checks['first_output'].metrics['reason_code'] == 'missing_acceptance_limit'
    assert 'Stopped by you' not in checks['first_output'].summary
    assert 'Request budget reached before' not in checks['first_output'].summary
    assert 'Run deadline reached before' not in checks['first_output'].summary


@pytest.mark.asyncio
async def test_final_planned_request_can_consume_budget_without_aborting(monkeypatch):
    from tests.test_runner import FakeClient
    from giraffe.planning import request_plan
    config = configuration(checks=['serving'])
    config.max_requests = request_plan(config)['planned_total']
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    report = await runner.run_suite(config)
    assert len(report.requests) == config.max_requests
    assert report.abort_reason is None
    assert report.overall == 'pass'
    assert 'max_requests' not in report.limits_reached
    assert not any('budget exhausted' in warning.lower() for warning in report.warnings)


@pytest.mark.parametrize('check_id', ['first_output', 'generation'])
@pytest.mark.parametrize('unfinished', [False, True])
def test_user_stop_replaces_sample_advice_only_for_unfinished_timing(check_id, unfinished):
    config = configuration(checks=[check_id], max_requests=200)
    run = runner._Run(config, None, None)
    run.reason = 'cancelled by user'
    if unfinished:
        run.unfinished['t'].add(check_id)
    run.records = [record(str(i), check_ids=[check_id],
                          scenario='generation' if check_id == 'generation' else 'warm') for i in range(2)]
    result = next(c for c in runner._checks(run, config.targets[0]) if c.id == check_id)
    assert result.status == 'inconclusive'
    if unfinished:
        from giraffe.planning import request_plan
        assert result.summary == f"Stopped by you after 2 of {request_plan(config)['planned_total']} planned requests."
        assert 'samples' not in result.summary and '--max-' not in result.summary
    else:
        assert result.metrics['reason_code'] == 'insufficient_timing_samples'
        assert 'samples' in result.summary and 'Stopped by you' not in result.summary
