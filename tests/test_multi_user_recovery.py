"""Recovery retains matched work and the slowest user's observation."""
from copy import deepcopy
import asyncio

import pytest

from giraffe.multi_user import _planned_observation
from giraffe.multi_user_metrics import summarize
from tests.test_multi_user import settings
from tests.test_multi_user_metrics import record
from tests.test_multi_user_scenarios import PROFILE


def recovery_observation(*, duration=1, sustained_duration=None, lighter_rate=1, sustained_rate=4):
    cfg = settings(levels=[3], scenarios=["recovery"], profile=PROFILE, recovery={
        "control_seconds": duration, "sustained_seconds": sustained_duration or duration, "lighter_seconds": duration,
        "sustained_requests_per_second": sustained_rate, "lighter_requests_per_second": lighter_rate,
        "max_slowdown": 2})
    source = _planned_observation(cfg.multi_user)
    records = []
    for index, window in enumerate(source["windows"]):
        start = index*1000
        planned_duration = window["planned_duration_seconds"]
        end = start+planned_duration*1000
        window.update(start_ms=start, end_ms=end, planned_end_ms=end,
                      duration_seconds=planned_duration, cut_short=False, reason=None)
        counts = {"dropped_local": 0, "dropped_late": 0, "unissued": 0, "not_offered": 0}
        window["offer_accounting"] = dict.fromkeys(("planned", "offered", "dispatched"), len(window["offers"])) | counts
        for user in window["users"]:
            uid = user["user_id"]
            offsets = [o["offset_seconds"] for o in window["offers"] if o["user_id"] == uid]
            delay = 300 if window["role"] == "lighter" and uid == "user-03" else 100
            ids = []
            for sequence, offset in enumerate(offsets, 1):
                dispatch = start+offset*1000
                match = (sequence-1) % window["control_work_count"] + 1
                r = record(uid, identity=f"{index}-{uid}" + (f"-{sequence}" if sequence > 1 else ""),
                    dispatch=dispatch, end=dispatch+delay, tokens=16, input_tokens=64,
                    requested_input_tokens=64, requested_output_tokens=16, first_output_ms=20,
                    last_output_ms=delay-10, stream_end_ms=delay, stream=True,
                    answer_reads=3, answer_chunks=3, max_stream_gap_ms=20)
                r.workload.update(window_id=window["id"], scenario="recovery", role=window["role"],
                    matched_work_sequence=match, matched_work_sha256=f"work-{match}", request_sequence=sequence)
                ids.append(r.id)
                records.append(r)
            user.update(activated_ms=start, ready_count=len(ids), dispatched_count=len(ids), request_ids=ids)
            user["offer_accounting"] = dict.fromkeys(("planned", "offered", "dispatched"), len(ids)) | counts
    return source, records


def test_recovery_offer_counts_and_replay_use_half_open_windows(monkeypatch, local_tokenizer):
    from giraffe import runner
    from giraffe.multi_user import execute
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient

    async def run():
        cfg = settings(levels=[2], scenarios=["recovery"], profile=PROFILE, recovery={
            "control_seconds": 100, "sustained_seconds": 100, "lighter_seconds": 100,
            "lighter_requests_per_second": .07, "sustained_requests_per_second": .14,
        }).model_copy(update={"max_duration_seconds": 310})
        expected = [14, 28, 14]
        planned = _planned_observation(cfg.multi_user, include_offers=False)["windows"]
        assert [window["scheduled_offers"] for window in planned] == expected

        target = cfg.targets[0]
        state = runner._Run(cfg, None, None)
        await execute(state, target, FakeClient(target, cfg))
        windows = state.observations[target.name]["multi_user"]["windows"]
        assert [len(window["offers"]) for window in windows] == expected
        assert [window["offer_accounting"]["dispatched"] for window in windows] == expected
        assert {window["control_work_count"] for window in windows} == {7}
        lighter = [record for record in state.records if record.workload["role"] == "lighter"
                   and record.workload["user_id"] == "user-01"]
        assert [record.workload["work_sequence"] for record in lighter] == list(range(15, 22))
        assert [record.workload["matched_work_sequence"] for record in lighter] == list(range(1, 8))

    run_virtual(monkeypatch, run())


def test_recovery_exposes_one_slow_user_when_median_recovers():
    source, records = recovery_observation()
    result = summarize(source, records)
    evidence = result["windows"][-1]["recovery"]
    assert [w["role"] for w in result["windows"]] == ["control", "sustained", "lighter"]
    assert [u["status"] for u in evidence["users"]] == ["pass", "pass", "fail"]
    assert evidence["users"][-1]["slowdown"] == 3
    assert result["status"] == "fail"
    assert result["worst_user"]["user_id"] == "user-03"


@pytest.mark.parametrize("drift_phase", [None, "lighter", "sustained"])
def test_recovery_checks_actual_lengths_of_each_replay(drift_phase):
    source, records = recovery_observation(lighter_rate=2)
    source["settings"]["pass_criteria"]["min_samples"] = 2
    for r in records:
        token_counts = (16, 4) if r.workload["role"] == drift_phase else (4, 16)
        r.output_tokens = token_counts[r.workload["matched_work_sequence"]-1]

    user = summarize(source, records)["windows"][-1]["recovery"]["users"][0]
    assert all(phase["output_tokens"] == 10 for phase in user["phases"].values())
    assert user["status"] == ("inconclusive" if drift_phase else "pass")
    if drift_phase:
        assert f"Actual token lengths drifted between matched control and {drift_phase} requests." in user["reasons"]


def test_recovery_allows_extra_replays_of_matched_sustained_work():
    source, records = recovery_observation(lighter_rate=2, sustained_rate=5)
    source["settings"]["pass_criteria"]["min_samples"] = 2
    for r in records:
        r.output_tokens = (4, 16)[r.workload["matched_work_sequence"]-1]

    user = summarize(source, records)["windows"][-1]["recovery"]["users"][0]
    assert len(user["control_request_ids"]) == 2
    assert len(user["sustained_request_ids"]) == 5
    assert user["phases"]["control"]["output_tokens"] == 10
    assert user["phases"]["sustained"]["output_tokens"] == 4
    assert user["status"] == "pass"


@pytest.mark.parametrize("swap_work", [False, True])
@pytest.mark.parametrize(("slower", "limit"), [("one_fixture", 2), ("all", 2), (None, 2), (None, None)])
def test_recovery_baseline_qualifies_tokens_by_matched_work(monkeypatch, local_tokenizer, tmp_path, swap_work, slower, limit):
    import json

    from giraffe import runner
    from giraffe.reporting import compare_baseline
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient

    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps([
        {"fixture_id": f"fixture-{letter}", "scorer": "none", "max_tokens": 32,
         "messages": [{"role": "user", "content": letter * 128}]}
        for letter in "ab"
    ]))
    cfg = settings(levels=[2], scenarios=["recovery"], profile=PROFILE, recovery={
        "control_seconds": 1, "sustained_seconds": 1, "lighter_seconds": 1,
        "lighter_requests_per_second": 4, "sustained_requests_per_second": 8, "max_slowdown": limit,
    }).model_copy(update={"custom_fixtures": str(fixtures)})

    class Observed(FakeClient):
        delay = .02
        changed = False

        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            if "multi_user" in spec.check_ids:
                first_fixture = spec.fixture_id == "fixture-a"
                record.output_tokens = 2 if first_fixture != (swap_work and self.changed) else 14
                record.completion_tokens = record.output_tokens
                if spec.workload.get("role") == "lighter" and (
                    (slower == "all" and self.changed) or (slower == "one_fixture" and first_fixture
                        and (record.output_tokens == 14 if swap_work else self.changed))
                ):
                    await asyncio.sleep(.06)
                    record.elapsed_ms += 60
            return record

    async def replay():
        baseline = await runner.run_suite(cfg)
        Observed.changed = True
        return await runner.run_suite(cfg), baseline

    monkeypatch.setattr(runner, "LLMClient", Observed)
    current, baseline = run_virtual(monkeypatch, replay())
    baseline_status = "measured" if limit is None else "pass"
    current_status = "fail" if slower else baseline_status
    current_slowdown = {"one_fixture": 2.5, "all": 4, None: 1}[slower]
    for report, slowdown, status in ((baseline, 1, baseline_status), (current, current_slowdown, current_status)):
        check = next(check for check in report.checks if check.id == "multi_user")
        users = check.metrics["multi_user"]["windows"][-1]["recovery"]["users"]
        assert len(users) == 2
        assert {user["status"] for user in users} == {status}
        assert [user["slowdown"] for user in users] == pytest.approx([slowdown, slowdown])
        assert all(phase["output_tokens"] == 8 for user in users for phase in user["phases"].values())

    comparison = compare_baseline(current, baseline).baseline
    rows = comparison["targets"][0]["comparisons"]
    acceptance = [row for row in rows if row["metric"] == "scenario_acceptance"]
    delivery = [row for row in rows if row["metric"] == "shared_window_output_tokens_per_second"]
    expected = "inconclusive" if swap_work else "fail" if slower else "pass"
    assert comparison["status"] == expected
    if slower:
        assert len(acceptance) == 2 and {row["status"] for row in acceptance} == {expected}
        if slower == "all":
            latency = [row for row in rows if row["metric"] == "latency_ms" and row.get("role") == "lighter"]
            assert len(latency) == 2 and {row["status"] for row in latency} == {expected}
    else:
        assert not acceptance
        observed = [row for row in rows if row["metric"] == "matched_recovery_slowdown"]
        assert len(observed) == (2 if swap_work else 0)
        for row in observed:
            assert row["status"] == "inconclusive" and row["threshold"] is None
            assert row["current"] == pytest.approx(row["baseline"])
            assert row["current_samples"] == row["baseline_samples"] == 4
            evidence = [r for r in current.requests if r.id in row["evidence_ids"]]
            assert len(evidence) == 16 and {r.workload["user_id"] for r in evidence} == {row["user_id"]}
            assert {r.workload["role"] for r in evidence} == {"control", "sustained", "lighter"}
    assert len(delivery) == 6 and all(row["status"] == "pass" and row["current"] == row["baseline"] for row in delivery)


@pytest.mark.parametrize("drift_phase", ["lighter", "sustained"])
def test_recovery_qualifies_total_generation_separately_from_visible_output(drift_phase):
    source, records = recovery_observation()
    for request in records:
        has_extra_reasoning = request.workload["role"] == drift_phase and request.workload["user_id"] == "user-03"
        request.output_tokens = 4
        request.reasoning_tokens = 12 if has_extra_reasoning else 0
        request.completion_tokens = request.output_tokens + request.reasoning_tokens

    users = summarize(source, records)["windows"][-1]["recovery"]["users"]
    assert [user["status"] for user in users] == ["pass", "pass", "inconclusive"]
    assert users[-1]["slowdown"] == 3
    assert all(phase["output_tokens"] == 4 for phase in users[-1]["phases"].values())
    assert f"Actual token lengths drifted between matched control and {drift_phase} requests." in users[-1]["reasons"]


@pytest.mark.parametrize("sustained_duration", [.5, .25])
def test_recovery_requires_a_higher_delivered_schedule(sustained_duration):
    source, records = recovery_observation(duration=.5, sustained_duration=sustained_duration,
                                           lighter_rate=.5, sustained_rate=1.5)
    assert all(window["offer_accounting"]["dispatched"] == 3 for window in source["windows"])
    evidence = summarize(source, records)["windows"][-1]["recovery"]
    assert evidence["status"] == "inconclusive"
    assert any("did not exercise more load" in reason for reason in evidence["reasons"])


@pytest.mark.parametrize("phase", ["sustained", "lighter"])
def test_recovery_checks_reported_uncached_input_in_each_replay(phase):
    source, records = recovery_observation()
    for request in records:
        request.cached_prompt_tokens = 64 if request.workload["role"] == phase else 0
    evidence = summarize(source, records)["windows"][-1]["recovery"]
    for user in evidence["users"]:
        assert user["status"] == "inconclusive"
        assert f"Actual uncached input drifted between matched control and {phase} requests." in user["reasons"]


@pytest.mark.parametrize("phase", [0, 1, 2])
def test_recovery_missing_phase_end_is_inconclusive(phase):
    source, records = recovery_observation()
    source["windows"][phase]["end_ms"] = None
    evidence = summarize(source, records)["windows"][-1]["recovery"]
    assert evidence["status"] == "inconclusive"
    assert not evidence["qualified"]


@pytest.mark.parametrize("fault", [None, "single_read", "missing_gap"])
def test_recovery_requires_qualified_peer_streams(fault):
    source, records = recovery_observation()
    for request in records:
        if request.workload["role"] != "sustained":
            continue
        uid = request.workload["user_id"]
        shift = {"user-01": 0, "user-02": 120, "user-03": 60}[uid]
        request.dispatch_ms += shift
        request.timing_start_ms += shift
        request.completed_ms += shift
        if uid == "user-03":
            if fault == "single_read":
                request.answer_reads = request.answer_chunks = 1
            elif fault == "missing_gap":
                request.max_stream_gap_ms = None
    evidence = summarize(source, records)["windows"][-1]["recovery"]
    for user in evidence["users"][:2]:
        assert user["status"] == ("inconclusive" if fault else "pass")
        assert any("qualified peer streaming overlap" in reason for reason in user["reasons"]) == bool(fault)


@pytest.mark.usefixtures("local_tokenizer")
def test_fractional_phase_boundaries_replay_only_scheduled_control_work(monkeypatch):
    from giraffe import runner
    from giraffe.multi_user import execute
    from tests.test_runner import FakeClient
    from tests.test_review26_phase4_accuracy import run_virtual

    async def run():
        cfg = settings(levels=[2], scenarios=["recovery"], profile=PROFILE, recovery={
            "control_seconds": .14, "sustained_seconds": .14, "lighter_seconds": .14,
            "sustained_requests_per_second": 100, "lighter_requests_per_second": 50})
        state = runner._Run(cfg, None, None)
        await execute(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg))
        windows = state.observations["service"]["multi_user"]["windows"]
        assert [w["offer_accounting"]["dispatched"] for w in windows] == [14, 28, 14]
        for uid in ("user-01", "user-02"):
            own = [r for r in state.records if r.workload["user_id"] == uid]
            control = {r.workload["matched_work_sequence"]: r.workload["matched_work_sha256"]
                       for r in own if r.workload["role"] == "control"}
            assert set(control) == set(range(1, 8))
            later = [r for r in own if r.workload["role"] != "control"]
            assert all(r.workload["matched_work_sequence"] in control for r in later)
            assert all(r.workload["matched_work_sha256"] == control[r.workload["matched_work_sequence"]]
                       for r in later)
            lighter = [r for r in own if r.workload["role"] == "lighter"]
            assert [r.workload["work_sequence"] for r in lighter] == list(range(15, 22))
    run_virtual(monkeypatch, run())


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
        slow_prompts = set()

        class RecoveryClient(LLMClient):
            async def execute(self, spec, **kwargs):
                if spec.workload.get("role") == "lighter" and spec.workload.get("user_id") == "user-03":
                    slow_prompts.add(spec.messages[0]["content"])
                return await super().execute(spec, **kwargs)

        endpoint.response_delay = lambda payload: .3 if payload["messages"][0]["content"] in slow_prompts else 0
        async with RecoveryClient(cfg.targets[0], cfg, max_connections=3) as client:
            await execute(state, cfg.targets[0], client)
        summary = result(state, cfg.targets[0]).metrics["multi_user"]
        windows = summary["windows"]
        assert [w["offer_accounting"]["dispatched"] for w in windows] == [3, 6, 3]
        assert all(w["offer_accounting"]["not_offered"] == 0 for w in windows)
        assert endpoint.peak_active == 3
        assert all(r.workload["ready_ms"] == r.workload["scheduled_ms"] for r in state.records)
        for window in windows:
            assert all(r.workload["ready_ms"] >= window["start_ms"] for r in state.records
                       if r.workload["window_id"] == window["id"])
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
        assert len({payload["messages"][0]["content"] for payload in endpoint.requests}) == 12

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
