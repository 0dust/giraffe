"""Planned skin cells follow the executor's first-use order, not catalogue order."""

import pytest

from giraffe.conditions import planned_conditions
from giraffe import runner
from tests.test_runner import FakeClient, config


@pytest.mark.asyncio
async def test_mixed_plan_follows_executed_conditions_without_reordering_the_run(monkeypatch):
    monkeypatch.setattr(runner, 'LLMClient', FakeClient)
    monkeypatch.setattr(FakeClient, 'delay', .001)
    monkeypatch.setattr(FakeClient, 'scenarios', [])
    monkeypatch.setattr(FakeClient, 'closed', [])
    settings = config(checks=['generation', 'cancellation', 'recovery', 'prefix', 'sessions', 'consistency'],
        max_requests=500, context_limit=2048, sustained_seconds=.01,
        prefix={'prefix_chars':32, 'repeats':2, 'history_turns':2},
        sessions={'sessions':1, 'concurrency':1, 'modes':['fixed'], 'delay_seconds':0},
        consistency={'levels':[1,2], 'shapes':['short'], 'prompts_per_shape':2, 'repetitions':2},
        test_options={'cancellation':{'cleanup_seconds':.001, 'deadline_probe_ms':1}})
    plan = [row['condition_id'] for row in planned_conditions(settings)]
    result = await runner.run_suite(settings)
    observed = list(dict.fromkeys(record.condition_id for record in result.requests if record.condition_id in plan))
    assert len(observed) >= 12  # The fixture must actually exercise the mixed phases.
    assert observed == [identifier for identifier in plan if identifier in observed]


def test_load_sessions_and_consistency_plans_follow_workload_execution_order():
    settings = config(checks=['prefix', 'load', 'sessions', 'consistency'],
        prefix={'prefix_chars':32, 'repeats':2, 'history_turns':2},
        sessions={'sessions':1, 'concurrency':1, 'modes':['fixed']},
        consistency={'levels':[1], 'shapes':['short'], 'prompts_per_shape':2, 'repetitions':2})
    checks = list(dict.fromkeys(row['check_id'] for row in planned_conditions(settings)))
    assert checks == ['prefix', 'load', 'sessions', 'consistency']


def test_tool_plan_puts_conversations_then_all_contracts_before_forced_diagnostics():
    from tests.test_tool_contract import fixture
    from tests.test_tool_conversations import fixture as conversation

    settings = config(checks=['tools'], forced_tool_diagnostic=True,
        tool_checks={'fixtures':[fixture(fixture_id='first'), fixture(fixture_id='second')],
                     'conversations':[conversation()], 'modes':['complete','stream']})
    assert [row['condition_id'] for row in planned_conditions(settings)] == [
        'tools_weather-result_complete', 'tools_weather-result_stream',
        'tools_first_auto_complete', 'tools_first_auto_stream',
        'tools_second_auto_complete', 'tools_second_auto_stream',
        'tools_first_forced_complete', 'tools_first_forced_stream',
        'tools_second_forced_complete', 'tools_second_forced_stream',
    ]
