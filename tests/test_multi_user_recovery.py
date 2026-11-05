"""Recovery retains matched work and the slowest user's observation."""
from copy import deepcopy
import asyncio
import hashlib
import json

import pytest

from giraffe.multi_user import _planned_observation
from giraffe.multi_user_metrics import summarize
from tests.test_multi_user import settings
from tests.test_multi_user_metrics import record
from tests.test_multi_user_scenarios import PROFILE


def recovery_observation():
    cfg = settings(levels=[3], scenarios=["recovery"], profile=PROFILE, recovery={
        "control_seconds": 1, "sustained_seconds": 1, "lighter_seconds": 1,
        "sustained_requests_per_second": 4, "lighter_requests_per_second": 1, "max_slowdown": 2})
    source = _planned_observation(cfg.multi_user)
    records = []
    for index, window in enumerate(source["windows"]):
        window.update(start_ms=index*1000, end_ms=(index+1)*1000, planned_end_ms=(index+1)*1000,
                      duration_seconds=1, cut_short=False, reason=None)
        window["offer_accounting"] = {"planned": 3, "offered": 3, "dispatched": 3,
            "dropped_local": 0, "dropped_late": 0, "unissued": 0, "not_offered": 0}
        for user in window["users"]:
            uid = user["user_id"]
            delay = 300 if window["role"] == "lighter" and uid == "user-03" else 100
            r = record(uid, identity=f"{index}-{uid}", dispatch=index*1000, end=index*1000+delay,
                tokens=16, input_tokens=64, requested_input_tokens=64, requested_output_tokens=16,
                first_output_ms=20, last_output_ms=delay-10, stream_end_ms=delay, stream=True,
                answer_reads=3, answer_chunks=3, max_stream_gap_ms=20)
            r.workload.update(window_id=window["id"], scenario="recovery", role=window["role"],
                matched_work_sequence=1, matched_work_sha256="same", request_sequence=index+1)
            user.update(activated_ms=index*1000, ready_count=1, dispatched_count=1, request_ids=[r.id])
            user["offer_accounting"] = {"planned": 1, "offered": 1, "dispatched": 1,
                "dropped_local": 0, "dropped_late": 0, "unissued": 0, "not_offered": 0}
            records.append(r)
    return source, records


def test_recovery_exposes_one_slow_user_when_median_recovers():
    source, records = recovery_observation()
    result = summarize(source, records)
    evidence = result["windows"][-1]["recovery"]
    assert [w["role"] for w in result["windows"]] == ["control", "sustained", "lighter"]
    assert [u["status"] for u in evidence["users"]] == ["pass", "pass", "fail"]
    assert evidence["users"][-1]["slowdown"] == 3
    assert result["status"] == "fail"
    assert result["worst_user"]["user_id"] == "user-03"


@pytest.mark.parametrize("phase", [0, 1, 2])
def test_recovery_missing_phase_end_is_inconclusive(phase):
    source, records = recovery_observation()
    source["windows"][phase]["end_ms"] = None
    evidence = summarize(source, records)["windows"][-1]["recovery"]
    assert evidence["status"] == "inconclusive"
    assert not evidence["qualified"]


@pytest.mark.parametrize("fault", ["missing_control", "drift", "missing_usage", "generator", "gap", "partial"])
def test_unqualified_recovery_cannot_pass(fault):
    source, records = recovery_observation()
    if fault == "missing_control":
        records = [r for r in records if r.id != "0-user-01"]
    elif fault == "drift":
        records[-3].input_tokens = 1000
    elif fault == "missing_usage":
        records[-3].output_tokens = None
    elif fault == "generator":
        source["windows"][1]["offer_accounting"]["dropped_local"] = 1
    elif fault == "gap":
        records[-3].max_stream_gap_ms = None
    else:
        source["windows"][0]["cut_short"] = True
    before = deepcopy(source)
    evidence = summarize(source, records)["windows"][-1]["recovery"]
    assert evidence["users"][0]["status"] == "inconclusive"
    assert source == before


@pytest.mark.usefixtures("local_tokenizer")
def test_http_same_users_continue_work_and_one_recovers_slowly():
    from giraffe import runner
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from giraffe.multi_user import execute, result
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[3], scenarios=["recovery"], shapes=["short_long"], profile=PROFILE, recovery={
            "control_seconds": 1, "sustained_seconds": 1, "lighter_seconds": 1,
            "sustained_requests_per_second": 2, "lighter_requests_per_second": 1,
            "max_slowdown": 2}).model_copy(update={"concurrency": 3,
                "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        nonce = hashlib.sha256(b"42:3:user-03:1").hexdigest()[:32]

        def delay(payload):
            windows = state.observations["service"]["multi_user"]["windows"]
            return .3 if windows[-1]["start_ms"] is not None and nonce in json.dumps(payload) else 0
        endpoint.response_delay = delay
        async with LLMClient(cfg.targets[0], cfg, max_connections=3) as client:
            await execute(state, cfg.targets[0], client)
        summary = result(state, cfg.targets[0]).metrics["multi_user"]
        windows = summary["windows"]
        assert [w["offer_accounting"]["dispatched"] for w in windows] == [3, 6, 3]
        assert all(w["offer_accounting"]["not_offered"] == 0 for w in windows)
        assert endpoint.peak_active == 3
        for uid in ("user-01", "user-02", "user-03"):
            rows = sorted([r for r in state.records if r.workload["user_id"] == uid], key=lambda r:r.dispatch_ms)
            assert [r.workload["request_sequence"] for r in rows] == [1, 2, 3, 4]
            assert [r.workload["work_sequence"] for r in rows] == [1, 1, 2, 3]
            assert len({r.workload["matched_work_sha256"] for r in rows}) == 1
        evidence = windows[-1]["recovery"]
        assert evidence["users"][-1]["slowdown"] > 2, evidence
        assert evidence["users"][0]["slowdown"] < 2, evidence
        assert evidence["users"][-1]["status"] == "fail", evidence["users"][-1]["reasons"]
        assert len(endpoint.requests) == len(state.records) == 12

    with FakeEndpoint(first_token_delay=.01, chunk_delay=.005) as endpoint:
        asyncio.run(run(endpoint))


@pytest.mark.usefixtures("local_tokenizer")
def test_budget_between_recovery_phases_keeps_unexercised_ledgers(monkeypatch):
    from giraffe import runner
    from giraffe.multi_user import execute, result
    from tests.test_runner import FakeClient
    from tests.test_review26_phase4_accuracy import run_virtual

    async def run():
        cfg = settings(levels=[2], scenarios=["recovery"], profile=PROFILE, recovery={
            "control_seconds": .2, "sustained_seconds": .2, "lighter_seconds": .2,
            "sustained_requests_per_second": 10, "lighter_requests_per_second": 5}).model_copy(update={"max_requests": 2})
        state = runner._Run(cfg, None, None)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        summary = result(state, cfg.targets[0]).metrics["multi_user"]
        control, sustained, lighter = summary["windows"]
        assert control["offer_accounting"]["dispatched"] == 2
        assert not control["cut_short"]
        assert sustained["offer_accounting"]["not_offered"] == 4
        assert lighter["offer_accounting"]["not_offered"] == 2
        assert sustained["start_ms"] is lighter["start_ms"] is None
        assert lighter["recovery"]["status"] == "inconclusive"
    run_virtual(monkeypatch, run())
