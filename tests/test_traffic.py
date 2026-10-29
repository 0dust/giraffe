"""Arrival scheduling, bounded admission, and honest qualification evidence."""

import asyncio
import time

import pytest

from giraffe import traffic
from giraffe.models import Limits, RequestRecord, RequestSpec, TrafficConfig


def profile(**overrides):
    return TrafficConfig(**({
        "rates": [50], "duration_seconds": .08, "max_in_flight": 4,
        "mix": {"short": 1, "long_input": 0, "long_output": 0},
        "drain_timeout_seconds": .3,
    } | overrides))


class Harness:
    def __init__(self, slots=4, budget=100, delay=.003):
        self.slots, self.available, self.budget = slots, slots, budget
        self.delay = delay
        self.starts, self.finishes, self.records = [], [], []
        self.stop = None
        self.transform = lambda record: record
        self.on_start = lambda: None

    def reserve(self):
        if self.budget <= 0:
            return False
        self.budget -= 1
        return True

    def acquire(self):
        if self.available == 0:
            return False
        self.available -= 1
        return True

    def release(self):
        self.available += 1
        assert self.available <= self.slots

    async def execute(self, spec, finalize_record):
        start = time.monotonic()
        self.starts.append(start)
        self.on_start()
        status = "completed"
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            status = "cancelled"
        self.finishes.append(time.monotonic())
        record = RequestRecord(
            id=str(len(self.records)), target="test", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now", status=status,
            elapsed_ms=(time.monotonic() - start) * 1000, valid=status == "completed",
            score=status == "completed", first_output_ms=1, last_output_ms=2,
            answer_chunks=2, output_tokens=2, max_stream_gap_ms=1,
        )
        self.records.append(record)
        record = self.transform(record)
        finalize_record(record)
        return record

    async def run(self, settings=None, limits=None, deadline=None, **pack):
        settings = settings or profile()
        specs = {kind: [RequestSpec(fixture_id=kind, scenario="traffic", check_ids=["capacity"],
                                  messages=[], scorer="exact", expected="ok")]
                 for kind in ("short", "long_input", "long_output")}
        result = await traffic.run_stage(
            traffic=settings, rate_rps=settings.rates[0], stage_index=0, fixtures=specs,
            execute=self.execute, reserve_offer=self.reserve, try_acquire=self.acquire,
            release=self.release, stop_reason=lambda: self.stop,
            deadline=deadline or time.monotonic() + 5,
            limits=limits or Limits(latency_ms=1000, min_samples=2), **pack,
        )
        assert result["scheduled"] == sum(result[key] for key in (
            "started", "dropped_local", "dropped_late", "not_offered"))
        assert self.available == self.slots
        return result


def test_seeded_poisson_and_fixture_mix_are_reproducible_and_uneven():
    settings = profile(arrival="poisson", duration_seconds=1,
                       mix={"short": .5, "long_input": .25, "long_output": .25})
    schedule, overflow = traffic.arrival_schedule(settings, 50)
    assert not overflow
    assert traffic.arrival_schedule(settings, 50) == (schedule, overflow)
    assert traffic.arrival_schedule(settings.model_copy(update={"seed": 43}), 50)[0] != schedule
    assert {kind for _, kind in schedule} == {"short", "long_input", "long_output"}
    intervals = [right[0] - left[0] for left, right in zip(schedule, schedule[1:])]
    assert max(intervals) > 3 * min(intervals)
    assert all(0 < offset < 1 for offset, _ in schedule)


async def test_arrivals_start_while_earlier_request_is_still_running():
    harness = Harness(delay=.11)
    result = await harness.run(profile(rates=[25], duration_seconds=.12))
    assert len(harness.starts) == 3
    assert harness.starts[1] < harness.finishes[0]
    assert result["peak_in_flight"] == 3
    assert result["accepted"] and result["goodput_rps"] == 25
    assert result["good_fraction"] == 1
    assert all(record.traffic_stage == 0 and record.traffic_class == "short"
               and record.arrival_elapsed_ms >= record.elapsed_ms for record in harness.records)


async def test_nearly_coincident_arrivals_within_tolerance_are_not_missed_ticks(monkeypatch):
    # A valid uneven cluster can be closer than the event loop's clock/timer
    # resolution. Dispatch lateness, not the next random gap, governs admission.
    schedule = [(0, "short"), (.000001, "short"), (.000002, "short"), (.04, "short")]
    monkeypatch.setattr(traffic, "arrival_schedule", lambda *_: (schedule, False))
    harness = Harness()
    result = await harness.run()
    assert result["started"] == result["scheduled"] == 4
    assert result["dropped_late"] == 0 and result["accepted"]


async def test_seeded_poisson_profile_does_not_drop_healthy_nearby_arrivals():
    harness = Harness(slots=8, delay=.005)
    result = await harness.run(profile(
        rates=[20], duration_seconds=1, arrival="poisson", seed=43, max_in_flight=8,
    ))
    assert result["scheduled"] == result["started"] == 20
    assert result["dropped_late"] == 0 and result["accepted"]


async def test_local_ceiling_drops_arrivals_without_waiting_or_exceeding_bound():
    harness = Harness(slots=2, delay=.15)
    result = await harness.run(profile(rates=[100], duration_seconds=.1, max_in_flight=2))
    assert result["started"] == result["peak_in_flight"] == 2
    assert result["dropped_local"] > 0
    assert result["generator_limited"] and not result["accepted"]
    assert result["status"] == "inconclusive"
    assert result["good_fraction"] == result["good"] / result["scheduled"] < 1
    assert harness.budget == 100 - result["scheduled"]


async def test_budget_exhaustion_counts_exact_remainder_and_drains_admitted_requests():
    harness = Harness(budget=2, delay=.07)
    drops = []
    result = await harness.run(profile(rates=[20], duration_seconds=.2),
                               on_drop=lambda: drops.append(harness.budget))
    assert drops == []
    assert result["scheduled"] == 4
    assert result["started"] == result["completed"] == result["not_offered"] == 2
    assert result["good_fraction"] == .5
    assert result["status"] == "inconclusive" and not result["fully_offered"]


async def test_stop_cancels_active_tasks_and_preserves_exact_plan():
    harness = Harness(delay=1)
    harness.on_start = lambda: setattr(harness, "stop", "Stopped by user.")
    start = time.monotonic()
    drops = []
    result = await harness.run(profile(duration_seconds=.2),
                               on_drop=lambda: drops.append(harness.budget))
    assert drops == []
    assert time.monotonic() - start < .2
    assert result["started"] == result["cancelled"] == 1
    assert result["not_offered"] == result["scheduled"] - 1
    assert result["status"] == "inconclusive"


async def test_global_deadline_cancels_drain_without_leaking_tasks():
    harness = Harness(delay=1)
    result = await harness.run(profile(rates=[10], duration_seconds=.2),
                               deadline=time.monotonic() + .025)
    assert result["started"] == result["cancelled"] == 1
    assert result["not_offered"] == 1 and not result["accepted"]


async def test_outer_cancellation_is_preserved_as_partial_stage_evidence():
    harness = Harness(delay=1)
    task = asyncio.create_task(harness.run(profile(duration_seconds=.2)))
    while not harness.starts:
        await asyncio.sleep(0)
    task.cancel()
    result = await task
    assert result["started"] == result["cancelled"] == 1
    assert not result["accepted"] and "Traffic stage cancelled." in result["reasons"]


async def test_cancellation_before_request_coroutine_starts_still_releases_slot(monkeypatch):
    create_task = asyncio.create_task

    def cancel_before_start(coroutine):
        task = create_task(coroutine)
        task.cancel()
        return task

    monkeypatch.setattr(traffic.asyncio, "create_task", cancel_before_start)
    harness = Harness()
    drops = []
    result = await harness.run(on_drop=lambda: drops.append(harness.budget))
    assert result["not_offered"] == result["scheduled"] and result["started"] == 0
    assert len(drops) == 100 - harness.budget == result["scheduled"]


async def test_monitor_cancellation_during_stop_cleanup_keeps_partial_stage():
    harness = Harness(delay=1)
    execute = harness.execute
    cleanup_started = asyncio.Event()

    async def slow_cleanup(spec, finalize_record):
        try:
            return await execute(spec, finalize_record)
        finally:
            cleanup_started.set()
            await asyncio.sleep(.02)

    harness.execute = slow_cleanup
    harness.on_start = lambda: setattr(harness, "stop", "Stopped by user.")
    task = asyncio.create_task(harness.run(profile(duration_seconds=.2)))
    await cleanup_started.wait()
    task.cancel()
    result = await task
    assert result["started"] == result["cancelled"] == 1
    assert result["not_offered"] == result["scheduled"] - 1
    assert result["status"] == "inconclusive"


async def test_drain_expiry_excludes_cancelled_answer_from_goodput():
    harness = Harness(delay=1)
    result = await harness.run(profile(duration_seconds=.04, drain_timeout_seconds=.02))
    assert result["drain_expired"] and result["cancelled"] == 2
    assert result["goodput_rps"] == 0 and not result["accepted"]


async def test_missed_ticks_are_dropped_instead_of_sent_in_a_catchup_burst():
    harness = Harness()
    block_once = True

    def block_event_loop_once():
        nonlocal block_once
        if block_once:
            block_once = False
            time.sleep(.045)

    harness.on_start = block_event_loop_once
    result = await harness.run(profile(rates=[100], duration_seconds=.1,
                                       scheduler_lag_tolerance_ms=10))
    assert result["dropped_late"] >= 4
    assert harness.records[1].scheduled_offset_ms >= 50
    assert not any(0 < record.scheduled_offset_ms < 50 for record in harness.records)
    assert not result["fully_offered"] and not result["accepted"]


@pytest.mark.parametrize("change,expected_status", [
    ({"score": False}, "fail"),
    ({"status": "failed", "valid": False}, "fail"),
    ({"status": "timeout", "valid": False}, "fail"),
    ({"elapsed_ms": 2000}, "fail"),
    ({"first_output_ms": None}, "inconclusive"),
    ({"score": None}, "inconclusive"),
])
async def test_only_correct_complete_measured_timely_answers_count_as_goodput(change, expected_status):
    harness = Harness()
    harness.transform = lambda record: record.model_copy(update=change)
    result = await harness.run(limits=Limits(latency_ms=1000, first_output_ms=100, min_samples=2))
    assert result["good"] == result["goodput_rps"] == 0
    assert result["status"] == expected_status


async def test_wrong_answers_remain_failures_when_generator_is_limited():
    harness = Harness(slots=1, delay=.1)
    harness.transform = lambda record: record.model_copy(update={"score": False})
    result = await harness.run(profile(max_in_flight=1))
    assert result["generator_limited"] and result["status"] == "fail"


async def test_observed_latency_failure_is_not_hidden_by_local_slot_limit():
    harness = Harness(slots=1, delay=.1)
    result = await harness.run(profile(max_in_flight=1), limits=Limits(latency_ms=10, min_samples=2))
    assert result["generator_limited"] and result["status"] == "fail"


async def test_capacity_requires_user_waiting_limit_even_with_generation_limit():
    result = await Harness().run(limits=Limits(min_output_tokens_per_second=1, min_samples=2))
    assert result["status"] == "inconclusive"
    assert any("first-output or total-latency" in reason for reason in result["reasons"])


async def test_windows_show_growing_response_latency_and_pending_requests():
    harness = Harness(delay=.035)
    harness.transform = lambda record: record.model_copy(update={"elapsed_ms": len(harness.records) * 10})
    result = await harness.run(profile(rates=[50], duration_seconds=.1))
    windows = result["windows"]
    assert windows[-1]["latency_p50_ms"] > windows[0]["latency_p50_ms"]
    assert any(window["pending_at_end"] > 0 for window in windows)
    assert sum(window["scheduled"] for window in windows) == result["scheduled"]


async def test_poisson_schedule_overflow_is_bounded_and_never_dispatched(monkeypatch):
    monkeypatch.setattr(traffic, "MAX_PLANNED_ARRIVALS", 2)
    harness = Harness()
    result = await harness.run(profile(arrival="poisson", rates=[100], duration_seconds=1))
    assert result["schedule_truncated"] and result["scheduled"] == result["not_offered"] == 2
    assert result["started"] == 0 and result["status"] == "inconclusive"


async def test_callback_that_does_not_start_request_releases_reserved_slot():
    harness = Harness()

    async def no_request(_, finalize_record):
        return None

    harness.execute = no_request
    drops = []
    result = await harness.run(on_drop=lambda: drops.append(harness.budget))
    assert result["not_offered"] == result["scheduled"] and result["started"] == 0
    assert len(drops) == 100 - harness.budget == result["scheduled"]


async def test_escaped_callback_failure_is_not_reported_as_success():
    harness = Harness()

    async def broken_callback(_, finalize_record):
        raise RuntimeError("private transport details")

    harness.execute = broken_callback
    result = await harness.run()
    assert result["failed"] == result["started"] > 0 and result["status"] == "fail"
    assert "private transport" not in str(result)


def pack_spec(key, scorer="exact"):
    return RequestSpec(fixture_id=f"pack:{key}", scenario="request_pack", check_ids=["capacity"], messages=[],
                       scorer=scorer, expected="ok" if scorer != "none" else None,
                       workload={"case_id": key})


async def test_pack_drops_keep_preassigned_case_and_zero_offer_case_blocks_claim():
    alpha, beta = pack_spec("alpha"), pack_spec("beta")
    result = await Harness(slots=1, delay=.1).run(
        profile(max_in_flight=1), case_specs=[alpha, beta, alpha, beta],
        active_case_specs=[alpha, beta])
    assert result["case_counts"]["beta"]["planned"] == 2
    assert result["case_counts"]["beta"]["dropped_local"] == 2
    assert result["case_counts"]["alpha"]["started"] == 1
    assert not result["coverage_complete"] and not result["accepted"]
    zero_offer = await Harness().run(case_specs=[alpha]*4, active_case_specs=[alpha, beta])
    assert zero_offer["case_counts"]["beta"]["offered"] == 0
    assert zero_offer["status"] == "inconclusive" and not zero_offer["accepted"]


async def test_active_unscored_case_blocks_capacity_even_without_offers():
    alpha, beta = pack_spec("alpha"), pack_spec("beta", "none")
    result = await Harness().run(case_specs=[alpha]*4, active_case_specs=[alpha, beta])
    assert not result["all_cases_scored"] and not result["accepted"]
    assert result["valid_throughput_rps"] == result["scored_subset_correct_goodput_rps"] == 50


@pytest.mark.parametrize("stop", [False, True])
async def test_pack_partial_stages_preserve_exact_case_offer_accounting(stop):
    alpha, beta = pack_spec("alpha"), pack_spec("beta")
    harness = Harness(budget=2, delay=.06)
    if stop:
        harness.on_start = lambda: setattr(harness, "stop", "Stopped by user.")
    result = await harness.run(case_specs=[alpha, beta, alpha, beta], active_case_specs=[alpha, beta])
    for row in result["case_counts"].values():
        assert row["planned"] == row["started"] + row["drops"] + row["not_offered"]
        assert row["offered"] == row["started"] + row["drops"]
    assert sum(row["started"] for row in result["case_counts"].values()) == result["started"]
    assert not result["accepted"]


@pytest.mark.parametrize("budget", [2, 100])
async def test_deferred_scores_refinalize_original_stage_evidence_without_extending_observation(budget):
    alpha, beta = pack_spec("alpha"), pack_spec("beta")
    harness = Harness(budget=budget)

    def pending_score(record):
        record.score = None
        return record

    harness.transform = pending_score
    captured = []
    limits = Limits(latency_ms=1000, min_samples=2)
    original = await harness.run(
        limits=limits, case_specs=[alpha, beta, alpha, beta],
        active_case_specs=[alpha, beta], capture_evidence=captured.append,
    )
    assert len(captured) == 1
    assert original["status"] == "inconclusive" and original["goodput_rps"] == 0
    assert not original["coverage_complete"]
    assert any("Too few scored" in reason for reason in original["reasons"])
    target_records = [record.model_dump() for record in harness.records]
    await asyncio.sleep(.02)  # Represents evaluation after inference has stopped.
    for record in harness.records:
        record.score = True
    result = traffic.finalize_stage(captured[0], limits)
    assert result["accepted"] is (budget == 100)
    assert result["good"] == result["scored"] == result["correct"] == len(harness.records)
    assert result["goodput_rps"] == len(harness.records) / result["duration_seconds"]
    assert result["coverage_complete"]
    assert result["unscored_completions"] == 0
    assert not any("Too few scored" in reason or "lack required" in reason for reason in result["reasons"])
    assert ("Request budget stopped the planned arrivals." in result["reasons"]) is (budget == 2)
    assert all(row["valid"] == row["scored"] == row["correct"] for row in result["case_counts"].values())
    assert sum(window["good"] for window in result["windows"]) == result["good"]
    score_fields = {
        "good", "scored", "correct", "correctness", "unscored_completions", "goodput_rps",
        "scored_subset_correct_goodput_rps", "good_fraction", "accepted", "status", "reasons",
        "windows", "case_counts", "coverage_complete",
    }
    assert {key: value for key, value in result.items() if key not in score_fields} == {
        key: value for key, value in original.items() if key not in score_fields
    }
    for before, after in zip(original["windows"], result["windows"], strict=True):
        assert {key: value for key, value in before.items() if key not in score_fields} == {
            key: value for key, value in after.items() if key not in score_fields
        }
    for before, record in zip(target_records, harness.records, strict=True):
        assert record.model_dump() == before | {"score": True}
    assert traffic.finalize_stage(captured[0], limits) == result
    assert original["goodput_rps"] == 0  # Earlier reports are not mutated.


async def test_one_missing_deferred_score_blocks_acceptance_despite_case_coverage():
    alpha = pack_spec("alpha")
    harness = Harness()
    captured = []
    limits = Limits(latency_ms=1000, min_samples=2)
    original = await harness.run(
        limits=limits, case_specs=[alpha] * 4, active_case_specs=[alpha],
        capture_evidence=captured.append,
    )
    assert original["accepted"]
    harness.records[-1].score = None
    result = traffic.finalize_stage(captured[0], limits)
    assert result["coverage_complete"] and result["all_cases_scored"]
    assert result["case_counts"]["alpha"]["scored"] == 3
    assert result["unscored_completions"] == 1
    assert result["status"] == "inconclusive" and not result["accepted"]
    harness.records[-1].score = False
    failed = traffic.finalize_stage(captured[0], limits)
    assert failed["status"] == "fail" and not failed["accepted"]
    assert failed["correctness"] == .75
    assert failed["goodput_rps"] == result["goodput_rps"]


@pytest.mark.parametrize("wrong,minimum,slow,status", [
    (1, .8, False, "inconclusive"),
    (1, .9, False, "inconclusive"),
    (1, 1, False, "fail"),
    (3, .8, False, "fail"),
    (1, .8, True, "fail"),
])
async def test_incomplete_judgements_fail_only_when_remaining_votes_cannot_meet_correctness(
    wrong, minimum, slow, status,
):
    alpha = pack_spec("alpha", "judge")
    harness = Harness()
    captured = []
    limits = Limits(latency_ms=1000, min_samples=2, min_correctness=minimum)
    if slow:
        harness.transform = lambda record: record.model_copy(update={"elapsed_ms": 2000})
    await harness.run(
        profile(duration_seconds=.2), limits=limits,
        case_specs=[alpha] * 10, active_case_specs=[alpha], capture_evidence=captured.append,
    )
    records = [record for _, record in captured[0].records]
    assert len(records) == 10
    for index, record in enumerate(records):
        record.score = True if index == 0 else False if index <= wrong else None
    result = traffic.finalize_stage(captured[0], limits)
    assert result["scored"] == wrong + 1
    assert result["correctness"] == 1 / (wrong + 1)
    assert result["unscored_completions"] == 9 - wrong
    assert result["status"] == status and not result["accepted"]
    assert ("Known-answer correctness was below the required fraction." in result["reasons"]) is (
        (10 - wrong) / 10 < minimum
    )


@pytest.mark.parametrize("kind", ["local", "late"])
async def test_reserved_offers_dropped_before_sending_notify_live_progress(monkeypatch, kind):
    schedule = [(0, "short"), (.01, "short"), (.02, "short")]
    monkeypatch.setattr(traffic, "arrival_schedule", lambda *_: (schedule, False))
    harness = Harness(slots=0 if kind == "local" else 1)
    drops = []
    if kind == "late":
        def late_reservation():
            time.sleep(.025)
            return Harness.reserve(harness)
        harness.reserve = late_reservation
    result = await harness.run(profile(scheduler_lag_tolerance_ms=1 if kind == "late" else 100),
                               on_drop=lambda: drops.append(harness.budget))
    expected = result["dropped_local"] + result["dropped_late"]
    assert len(drops) == expected > 0
    assert not harness.starts
