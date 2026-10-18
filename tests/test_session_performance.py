from collections import Counter

import pytest
from pydantic import ValidationError

from giraffe.models import RequestRecord, SessionWorkload, Sessions, session_workload_presets
from giraffe.runner import _stats, run_suite
from giraffe.reporting import write_report
from giraffe.session_metrics import performance, workload_plan
from tests.fake_endpoint import FakeEndpoint
from tests.test_workloads import config, check


def workloads():
    return [SessionWorkload(name=name, turns=[
        f"Copy exactly, with no extra text:\n{name} one two three four five six seven eight nine ten.",
        f"Copy exactly, with no extra text:\n{name} eleven twelve thirteen fourteen fifteen.",
    ]) for name in ("coding", "essay")]


def test_weighted_conversation_budget_is_exact_and_interleaved():
    selected = session_workload_presets()
    selected[0].weight = 2
    plan = workload_plan(selected, 8)
    assert Counter(item.name for item in plan) == {"coding": 4, "essay": 2, "chat": 2}
    assert len({item.name for item in plan[:3]}) == 3
    assert len(workload_plan(selected, 1)) == 1


@pytest.mark.parametrize("updates", [
    {"workloads": []},
    {"modes": ["fixed"]},
    {"workloads": [workloads()[0], workloads()[0]]},
])
def test_invalid_performance_profiles_are_rejected(updates):
    with pytest.raises(ValidationError):
        Sessions.model_validate({"profile": "performance", "modes": ["live"],
                                 "workloads": workloads(), **updates})


@pytest.mark.parametrize("retention", ["all", "none"])
async def test_mixed_live_conversations_overlap_and_keep_actual_history_isolated(retention, tmp_path):
    with FakeEndpoint(first_token_delay=.02) as endpoint:
        report = await run_suite(config(endpoint, checks=["sessions"], retention=retention,
            sessions={"profile": "performance", "modes": ["live"], "sessions": 4,
                      "concurrency": 2, "workloads": workloads(), "max_tokens": 64}))
        assert endpoint.peak_active == 2
        for payload in endpoint.requests:
            messages = payload["messages"]
            if len(messages) == 3:
                expected = messages[0]["content"].split("\n", 1)[1]
                assert messages[1] == {"role": "assistant", "content": expected}
                name = "coding" if "coding" in messages[0]["content"] else "essay"
                assert name in messages[-1]["content"]
                assert ("essay" if name == "coding" else "coding") not in str(messages)
    result = check(report, "sessions")
    assert result.status == "pass"
    measured = result.metrics["conversation_performance"]
    assert measured["completed_conversations"] == measured["planned_conversations"] == 4
    assert measured["actual_task_mix"] == {"coding": 2, "essay": 2}
    assert measured["mean_user_generation_tokens_per_second"] > 0
    assert measured["generation_rate_samples"] == 8
    assert len(measured["turns"]) == 4
    assert all(row["scored"] == 0 for row in measured["tasks"])
    if retention == "none":
        import json

        write_report(report, tmp_path)
        saved = json.loads((tmp_path / "report.json").read_text())
        assert all(not row["output"] for row in saved["requests"])
        assert next(row for row in saved["checks"] if row["id"] == "sessions")["metrics"]["conversation_performance"] == measured
        html = (tmp_path / "report.html").read_text()
        assert "Average generation speed per user:" in html
        assert "Conversation performance by task and turn" in html


@pytest.mark.parametrize("sessions,expected", [(1, "inconclusive"), (2, "pass")])
async def test_performance_caps_are_reported_without_stopping_remaining_turns(sessions, expected):
    with FakeEndpoint() as endpoint:
        report = await run_suite(config(endpoint, checks=["sessions"],
            sessions={"profile": "performance", "modes": ["live"], "sessions": sessions,
                      "concurrency": 1, "workloads": [workloads()[0]], "max_tokens": 5}))
    result = check(report, "sessions")
    assert result.status == expected
    assert result.metrics["conversation_performance"]["capped_responses"] == 2 * sessions
    assert result.metrics["conversation_performance"]["completed_conversations"] == sessions


async def test_failed_mixed_conversation_remains_visible_and_does_not_block_other_users():
    with FakeEndpoint(mode="http_error", fault_contains="essay") as endpoint:
        report = await run_suite(config(endpoint, checks=["sessions"],
            sessions={"profile": "performance", "modes": ["live"], "sessions": 2,
                      "concurrency": 2, "workloads": workloads(), "max_tokens": 64}))
    result = check(report, "sessions")
    assert result.status == "fail"
    measured = result.metrics["conversation_performance"]
    assert measured["planned_conversations"] == 2
    assert measured["completed_conversations"] == 1
    assert measured["failed"] == 1
    assert next(row for row in measured["users"] if row["task_type"] == "essay")["stop_reason"]


def record(user, turn, rate=None, *, failed=False):
    return RequestRecord(id=f"{user}-{turn}", target="local", fixture_id="test", scenario="sessions",
        check_ids=["sessions"], started_at="2026-10-14T00:00:00Z",
        status="failed" if failed else "completed", valid=not failed, stream=True,
        output_tokens=11, answer_chunks=2 if rate else 1, first_output_ms=100,
        last_output_ms=100 + 10000/rate if rate else 100, elapsed_ms=1200,
        dispatch_ms=turn*1500, completed_ms=turn*1500+1200,
        workload={"session_id": user, "task_type": "coding", "turn_id": turn, "mode": "live"})


def test_user_average_does_not_overweight_users_with_more_responses_or_invent_missing_rates():
    rows = [record("a", 0, 10), record("a", 1, 10), record("b", 0, 30), record("c", 0)]
    observation = {"planned_sessions": 3, "sessions": [
        {"id": name, "mode": "live", "turns": list(range(count)), "planned_turns": count,
         "task_type": "coding", "stop_reason": None} for name, count in [("a", 2), ("b", 1), ("c", 1)]]}
    measured = performance(observation, rows, _stats)
    assert measured["mean_user_generation_tokens_per_second"] == 20
    assert measured["generation_tokens_per_second_mean"] == pytest.approx(50/3)
    assert measured["users_with_measured_generation"] == 2
    assert measured["unmeasured_generation_requests"] == 1
    assert measured["users"][-1]["generation_tokens_per_second_mean"] is None


async def test_recovery_uses_long_streamed_known_answers_for_every_phase():
    with FakeEndpoint() as endpoint:
        report = await run_suite(config(endpoint, checks=["recovery"], sustained_seconds=.01,
            limits={"min_output_tokens_per_second": 1, "min_samples": 2}))
    result = check(report, "recovery")
    assert result.status == "pass"
    assert {row.scenario for row in report.requests} == {"sustained", "recovery_baseline", "recovery"}
    probes = [row for row in report.requests if row.scenario != "sustained"]
    assert all(row.answer_chunks > 10 and row.score is True for row in probes)
    assert len({row.fixture_id for row in probes}) == 1
    assert all(row.score is True for row in report.requests)
    assert not result.metrics.get("missing_timing_metrics")
