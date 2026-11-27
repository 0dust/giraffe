"""Request construction stays lazy and matches the executor's outgoing histories."""

import time

import pytest

from giraffe import runner, session_sampling, workloads
from giraffe.models import RequestRecord
from tests.test_workload_versions import CaptureRun, _config, _requests


class SessionCaptureRun(CaptureRun):
    def __init__(self, config):
        super().__init__(config)
        self.start = time.monotonic()
        self.deadline = self.start + 60
        self.reason = None

    async def request(self, target, client, request, *, finalize_record=None):
        self.requests.append(request.model_copy(deep=True))
        record = RequestRecord(
            id=str(len(self.requests)), target=target.name,
            fixture_id=request.fixture_id, scenario=request.scenario,
            check_ids=request.check_ids, started_at="", status="completed",
            valid=True, output=request.expected or "A deterministic response.",
            score=True, finish_reason="stop", dispatch_ms=0, completed_ms=0,
        )
        if finalize_record:
            finalize_record(record)
        self.records.append(record)
        return record


async def test_prefix_rejection_constructs_only_first_request(monkeypatch):
    config = _config(checks=["prefix"], context_limit=512,
                     prefix={"prefix_chars": 65536, "repeats": 100, "history_turns": 32})
    constructed = []
    original = workloads.spec

    def count(*args, **kwargs):
        constructed.append(None)
        return original(*args, **kwargs)

    monkeypatch.setattr(workloads, "spec", count)
    run = CaptureRun(config)
    await workloads.prefixes(run, config.targets[0], None)
    assert len(constructed) == 1
    assert run.requests == []
    assert "prefix" in run.unfinished[config.targets[0].name]


def test_first_consistency_request_constructs_only_one_spec(monkeypatch):
    config = _config(checks=["consistency"], context_limit=524288,
                     consistency={"shapes": ["long"], "prompts_per_shape": 16})
    constructed = []
    original = workloads.spec

    def count(*args, **kwargs):
        constructed.append(None)
        return original(*args, **kwargs)

    monkeypatch.setattr(workloads, "spec", count)
    requests = workloads.consistency_requests(config)
    assert constructed == []
    assert next(requests)[0] == "long"
    assert len(constructed) == 1


async def test_fixed_session_builder_matches_sent_requests():
    config = _config(checks=["sessions"], sessions={
        "modes": ["fixed", "live"], "sessions": 2, "concurrency": 1,
        "turns": ["Remember this bakery", "Recall the bakery", "Recall it again"],
    })
    run = SessionCaptureRun(config)
    await workloads.sessions(run, config.targets[0], None)
    effective = runner._check_config(config, "sessions")
    expected = [request for mode in effective.sessions.modes
                for index in range(effective.sessions.sessions)
                for request in workloads.fixed_session_requests(effective, effective.sessions, mode, index)]
    assert len(run.requests) == 12
    assert _requests(run.requests) == _requests(expected)


def sampled_guard_config():
    return _config(checks=["sessions"], sessions={
        "profile": "performance", "modes": ["live"], "sessions": 2, "concurrency": 1,
        "workloads": [{"name": "chat", "turns": ["First", "Next", "Summarize"]}],
        "sampling": {"input_tokens": {"values": [64, 96]},
                     "output_tokens": {"values": [16, 32]},
                     "think_time_seconds": {"values": [0]}},
    })


async def sampled_guard_requests():
    config = sampled_guard_config()
    run = SessionCaptureRun(config)
    target = config.targets[0]
    effective = runner._check_config(config, "sessions")
    expected = []
    plans = session_sampling.session_plan(effective.sessions)
    for index, plan in enumerate(plans):
        history = []
        for turn, (prompt, sampled) in enumerate(zip(plan["workload"].turns, plan["turns"])):
            prepared = await session_sampling.prepare_session_input(
                run, target, prompt, sampled["input_tokens"])
            assert prepared["status"] == "calibrated"
            request = workloads.session_turn_request(
                effective, effective.sessions, "live", index, turn, prompt, history,
                sampled=sampled, preparation=prepared, workload=plan["workload"],
                task_type=plan["workload"].name,
            )
            expected.append(request)
            history = workloads.session_history(
                request, "live", f"Session_{index:04d}_live", "A deterministic response.")
    return [dict(plan, workload=plan["workload"].model_dump(mode="json")) for plan in plans], expected


@pytest.mark.usefixtures("local_tokenizer")
async def test_sampled_session_builder_matches_sent_requests():
    config = sampled_guard_config()
    run = SessionCaptureRun(config)
    await workloads.sessions(run, config.targets[0], None)
    _, expected = await sampled_guard_requests()
    assert len(run.requests) == 6
    assert _requests(run.requests) == _requests(expected)


@pytest.mark.usefixtures("local_tokenizer")
async def test_fixed_sampled_guard_schedule_matches_executor_pauses():
    from tests.test_workload_versions import _session_plan

    config = _config(checks=["sessions"], sessions={
        "sampling": {"think_time_seconds": {"values": [0, .25], "weights": [1, 1]}},
    })
    run = SessionCaptureRun(config)
    effective = runner._check_config(config, "sessions")
    schedules = []
    for plan in session_sampling.session_plan(effective.sessions):
        delays = []

        async def pause(delay):
            delays.append(delay)
            return True

        await workloads.execute_conversation(
            run, config.targets[0], None, effective, effective.sessions, plan["mode"], plan["index"],
            items=[], prepared_inputs={}, plan=plan, pause=pause,
        )
        assert delays == [turn["think_time_seconds"] for turn in plan["turns"]]
        schedules.append(plan["turns"])
    assert schedules == (await _session_plan())["fixed_schedule"]
    assert len(run.requests) == 20
