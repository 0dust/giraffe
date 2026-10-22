"""Issue #45 backend contract. No socket servers; tiny windows and fake clients."""

import asyncio
import itertools
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from giraffe.configuration_errors import configuration_errors
from giraffe.load_test import (
    admission_limit,
    burst_times,
    execute,
    load_time_plan,
    measurements,
    planned_arrivals,
    result,
    score_group,
    test_points as load_points,
)
from giraffe.models import (
    CHECK_NAMES,
    RequestRecord,
    RunConfig,
    migrate_load_test,
    required_concurrency,
)
from giraffe.planning import request_plan
from giraffe.runner import _Run, _checks
from giraffe.reporting import _load_test_html

TARGET = {"name": "local", "url": "http://unused.test/v1", "model": "test"}


def values():
    return {
        "targets": [TARGET],
        "checks": ["load"],
        "max_memory_mb": 0,
        "load_test": {
            "workloads": {"combinations": {"selected": ["short_short"]}},
            "patterns": {
                "concurrent": {"enabled": True, "levels": [1]},
                "constant": {"enabled": False},
            },
            "schedule": {"duration_seconds": 1, "finish_seconds": 0.02},
            "pass_criteria": {"min_valid_responses": 1, "max_completion_ms": 1000},
        },
    }


def config():
    return RunConfig.model_validate(values())


@pytest.mark.parametrize(
    "concurrent,constant,poisson,bursts", list(itertools.product([False, True], repeat=4))
)
def test_all_pattern_combinations(concurrent, constant, poisson, bursts):
    v = values()
    p = v["load_test"]["patterns"]
    p.update(
        concurrent={"enabled": concurrent, "levels": [1, 4]},
        constant={"enabled": constant},
        poisson={"enabled": poisson, "seed": 12},
        bursts={"enabled": bursts, "size": 2, "first_seconds": 0, "gap_seconds": 0.5},
        rates=[1, 2],
    )
    if not any((concurrent, constant, poisson, bursts)):
        with pytest.raises(ValidationError):
            RunConfig.model_validate(v)
        return
    c = RunConfig.model_validate(v)
    points = load_points(c.load_test)
    assert len(points) == 2 * concurrent + 2 * constant + 2 * poisson + (
        bursts and not (constant or poisson)
    )
    assert sum(p["pattern"] == "bursts" for p in points) == (bursts and not (constant or poisson))
    assert all(not p["bursts"] for p in points if p["pattern"] == "concurrent")
    assert all(
        bool(p["bursts"]) == bursts for p in points if p["pattern"] in {"constant", "poisson"}
    )


def burst_config(constant=False, poisson=False):
    v = values()
    v["load_test"]["patterns"] = {
        "concurrent": {"enabled": False},
        "constant": {"enabled": constant},
        "poisson": {"enabled": poisson, "seed": 17},
        "rates": [10],
        "bursts": {"enabled": True, "size": 32, "first_seconds": 10, "gap_seconds": 10},
    }
    v["load_test"]["schedule"]["duration_seconds"] = 60
    return RunConfig.model_validate(v)


@pytest.mark.parametrize(
    "constant,poisson,expected,tests",
    [(False, False, 160, 1), (False, True, 760, 1), (True, True, 1520, 2)],
)
def test_worked_burst_plans(constant, poisson, expected, tests):
    c = burst_config(constant, poisson)
    assert burst_times(c.load_test) == [10, 20, 30, 40, 50]
    plan = request_plan(c)["load_test"]
    assert plan["expected_scheduled_requests"] == expected
    assert plan["scheduled_tests"] == tests
    assert plan["time_with_allowances_seconds"] == tests * 60.02
    assert ("160 additional requests" if constant or poisson else "160 scheduled requests") in plan["burst_previews"][0]["text"]
    for p in load_points(c.load_test):
        offers, _ = planned_arrivals(c.load_test, p)
        assert sum(o["origin"] == "burst" for o in offers) == 160
        assert all(o["offset_seconds"] < 60 for o in offers)
        assert offers == planned_arrivals(c.load_test, p)[0]
        if p["pattern"] == "poisson":
            assert 650 < len(offers) < 870


def test_true_poisson_counts_and_seed_reproducibility():
    c = burst_config(poisson=True)
    p = load_points(c.load_test)[0]
    a, _ = planned_arrivals(c.load_test, p)
    c.load_test.patterns.poisson.seed += 1
    b, _ = planned_arrivals(c.load_test, p)
    assert a != b
    assert len(a) != len(b)  # Exponential intervals, not conditioned on a fixed count.


def test_mixed_shares_and_bursts_use_one_seeded_random_mix():
    c = burst_config()
    w = c.load_test.workloads
    w.combinations.enabled = False
    w.mixed.enabled = True
    w.mixed.selected = ["short_short", "long_long"]
    point = load_points(c.load_test)[0]
    offers, _ = planned_arrivals(c.load_test, point)
    assert offers == planned_arrivals(c.load_test, point)[0]
    assert {o["shape"] for o in offers} == set(w.mixed.selected)
    assert 50 < sum(o["shape"] == "long_long" for o in offers) < 110
    w.mixed.share_mode = "custom"
    w.mixed.shares = {"short_short": 90, "long_long": 10}
    offers, _ = planned_arrivals(c.load_test, point)
    assert 3 < sum(o["shape"] == "long_long" for o in offers) < 35


@pytest.mark.parametrize(
    "path,value,field",
    [
        ("profile.short.input_tokens", 0, "load_test.profile.short.input_tokens"),
        ("profile.medium.input_tokens", 128, "load_test.profile.medium.input_tokens"),
        ("profile.long.output_tokens", 64, "load_test.profile.long.output_tokens"),
        ("workloads.combinations.selected", [], "load_test.workloads.combinations.selected"),
        ("workloads.combinations.enabled", False, "load_test.workloads.combinations.enabled"),
        ("patterns.concurrent.levels", [2, 1], "load_test.patterns.concurrent.levels"),
        ("patterns.concurrent.levels", [0], "load_test.patterns.concurrent.levels.0"),
        ("patterns.rates", [2, 1], "load_test.patterns.rates"),
        ("patterns.rates", [0], "load_test.patterns.rates.0"),
        ("patterns.poisson.seed", -1, "load_test.patterns.poisson.seed"),
        ("patterns.poisson.seed", 1.2, "load_test.patterns.poisson.seed"),
        ("patterns.bursts.size", 0, "load_test.patterns.bursts.size"),
        ("patterns.bursts.first_seconds", -1, "load_test.patterns.bursts.first_seconds"),
        ("patterns.bursts.gap_seconds", 0, "load_test.patterns.bursts.gap_seconds"),
        ("schedule.duration_seconds", 0, "load_test.schedule.duration_seconds"),
        ("schedule.finish_seconds", -1, "load_test.schedule.finish_seconds"),
        ("request_timeout_seconds", 0, "load_test.request_timeout_seconds"),
        ("pass_criteria.max_error_percent", 101, "load_test.pass_criteria.max_error_percent"),
        ("pass_criteria.min_correct_percent", -1, "load_test.pass_criteria.min_correct_percent"),
        ("pass_criteria.max_first_answer_ms", 0, "load_test.pass_criteria.max_first_answer_ms"),
        ("pass_criteria.max_completion_ms", -1, "load_test.pass_criteria.max_completion_ms"),
        ("pass_criteria.max_stream_pause_ms", 0, "load_test.pass_criteria.max_stream_pause_ms"),
        (
            "pass_criteria.min_generation_tokens_per_second",
            0,
            "load_test.pass_criteria.min_generation_tokens_per_second",
        ),
        ("pass_criteria.min_valid_responses", 0, "load_test.pass_criteria.min_valid_responses"),
        ("workloads.interference.max_slowdown", 1, "load_test.workloads.interference.max_slowdown"),
        (
            "workloads.stress.min_output_percent",
            101,
            "load_test.workloads.stress.min_output_percent",
        ),
    ],
)
def test_validation_fields_and_actions(path, value, field):
    v = values()
    # Full defaults allow editing any dedicated field.
    v["load_test"] = config().load_test.model_dump()
    node = v["load_test"]
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(v)
    error = configuration_errors(exc.value.errors(), v)[0]
    assert field in error["fields"]
    assert error["actions"] and all(
        a.get("field") in error["fields"] or a.get("fields") for a in error["actions"]
    )


@pytest.mark.parametrize(
    "workload,updates,field",
    [
        ("mixed", {"selected": ["short_short"]}, "selected"),
        (
            "mixed",
            {
                "share_mode": "custom",
                "selected": ["short_short", "long_long"],
                "shares": {"short_short": 99, "long_long": 0},
            },
            "shares",
        ),
        (
            "mixed",
            {
                "share_mode": "custom",
                "selected": ["short_short", "long_long"],
                "shares": {"short_short": 99, "long_long": 2},
            },
            "shares",
        ),
        ("stress", {"cases": []}, "cases"),
    ],
)
def test_workload_validation(workload, updates, field):
    v = values()
    v["load_test"]["workloads"][workload] = {"enabled": True, **updates}
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(v)
    assert (
        f"load_test.workloads.{workload}.{field}"
        in configuration_errors(exc.value.errors(), v)[0]["fields"]
    )


def test_interference_level_one_not_applicable_and_two_full_windows():
    v = values()
    v["load_test"]["workloads"]["interference"] = {"enabled": True}
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(v)
    assert (
        "load_test.patterns.concurrent.levels"
        in configuration_errors(exc.value.errors(), v)[0]["fields"]
    )
    v["load_test"]["patterns"]["concurrent"]["levels"] = [1, 2, 4]
    c = RunConfig.model_validate(v)
    points = load_points(c.load_test)
    assert [p["level"] for p in points if p["type"] == "interference"] == [2, 4]
    assert load_time_plan(c.load_test)["windows"] == 7


def test_run_limits_planning_and_context():
    v = values()
    v["concurrency"] = 1
    v["load_test"]["patterns"]["concurrent"]["levels"] = [2]
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(v)
    assert "concurrency" in configuration_errors(exc.value.errors(), v)[0]["fields"]
    v = values()
    v["max_requests"] = 1
    v["load_test"]["patterns"]["concurrent"]["levels"] = [2]
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(v)
    assert "max_requests" in configuration_errors(exc.value.errors(), v)[0]["fields"]
    c = burst_config(poisson=True)
    c.max_requests, c.max_duration_seconds = 200, 20
    plan = request_plan(c)
    assert any("max_requests" in w["fields"] for w in plan["load_warnings"])
    assert any("max_duration_seconds" in w["fields"] for w in plan["time_warnings"])
    assert required_concurrency(c)["by_check"]["load"] == admission_limit(c, c.load_test)
    c.concurrency = None
    assert admission_limit(c, c.load_test) == 512  # min(512, ceil(10*60)+32)
    assert "load" in CHECK_NAMES and not {"capacity", "arrivals", "concurrency"}.intersection(
        CHECK_NAMES
    )


class Client:
    def __init__(self, delays=(0.005,), trigger=True):
        self.delays, self.trigger = delays, trigger
        self.specs, self.started, self.finished = [], [], []

    async def execute(self, spec, first_output_event=None):
        self.specs.append(spec)
        calibration = spec.scenario.startswith("load_calibration")
        r = RequestRecord(
            id=f"r{len(self.specs)}",
            target="local",
            fixture_id=spec.fixture_id,
            scenario=spec.scenario,
            check_ids=spec.check_ids,
            started_at="now",
            status="completed",
            valid=True,
            output="K7P4",
            score=True,
            input_chars=spec.input_chars,
            input_tokens=round(spec.input_chars / 4),
            output_tokens=spec.max_tokens,
            completion_tokens=spec.max_tokens,
            requested_max_tokens=spec.max_tokens,
            response_body_bytes=10,
            finish_reason="stop",
            stream=True,
        )
        if calibration:
            return r
        begin = time.monotonic()
        self.started.append(begin)
        delay = self.delays[(len(self.started) - 1) % len(self.delays)]
        await asyncio.sleep(0.001)
        if first_output_event and self.trigger:
            first_output_event.set()
            for _ in range(5):
                first_output_event.on_answer_chunk()
        await asyncio.sleep(delay)
        r.elapsed_ms = (time.monotonic() - begin) * 1000
        r.first_output_ms = 1
        r.last_output_ms = r.elapsed_ms - 1
        r.stream_end_ms = r.elapsed_ms
        r.answer_chunks = r.answer_reads = 6
        r.answer_chars = r.output_chars = 20
        r.first_read_answer_chars = 2
        r.max_stream_gap_ms = 2
        self.finished.append(time.monotonic())
        return r


async def test_sustained_individual_replenishment_and_not_sample_count():
    c = config()
    c.load_test.patterns.concurrent.levels = [2]
    c.load_test.schedule.duration_seconds = 0.06
    run, client = _Run(c, None, None), Client((0.003, 0.035))
    obs = await execute(run, c.targets[0], client)
    assert len(client.started) > 3
    assert (
        client.started[2] < client.started[1] + 0.035
    )  # One completion refills without slow peer.
    assert obs["points"][0]["delivered_window_seconds"] >= 0.059
    assert not obs["points"][0]["cut_short"]
    sending, drain = run.observations["local"]["schedule_transitions"]
    assert sending["intended_concurrency"] == 2
    assert sending["start_ms"] == obs["points"][0]["sending_start_ms"]
    assert sending["end_ms"] == obs["points"][0]["sending_end_ms"]
    assert drain["start_ms"] == sending["end_ms"]
    assert drain["intended_concurrency"] == 0
    assert drain["end_ms"] >= drain["start_ms"]
    assert run.slots.qsize() == run.slots.maxsize
    assert (
        _checks(run, c.targets[0])[
            list(
                k for k in __import__("giraffe.models", fromlist=["CORE_CHECKS"]).CORE_CHECKS
            ).index("load")
        ].id
        == "load"
    )


async def test_finish_allowance_then_cancellation_is_accounted_and_sequential():
    c = config()
    c.load_test.schedule.duration_seconds = 0.015
    c.load_test.schedule.finish_seconds = 0.01
    c.load_test.patterns.concurrent.levels = [1, 2]
    run, client = _Run(c, None, None), Client((0.2,))
    obs = await execute(run, c.targets[0], client)
    assert len(client.started) == 3
    for p in obs["points"]:
        assert sum(g["cancelled"] for g in p["groups"]) == p["level"]
        assert p["status"] == "inconclusive"
        assert p["drain_seconds"] >= 0.009
    a, b = obs["points"]
    assert (
        max(r.completed_ms for r in run.records if r.load_point.get("point_id") == a["id"])
        <= b["sending_start_ms"]
    )
    assert run.slots.qsize() == run.slots.maxsize


async def test_arrival_local_drops_not_sent_not_endpoint_errors_and_group_mix():
    c = config()
    p = c.load_test.patterns
    p.concurrent.enabled = False
    p.bursts.enabled = True
    p.bursts.size = 8
    p.bursts.first_seconds = 0
    p.bursts.gap_seconds = 1
    c.concurrency = 2
    c.load_test.schedule.duration_seconds = 0.02
    c.load_test.workloads.combinations.enabled = False
    w = c.load_test.workloads.mixed
    w.enabled = True
    w.selected = ["short_short", "long_long"]
    run, client = _Run(c, None, None), Client((0.05,))
    obs = await execute(run, c.targets[0], client)
    point = obs["points"][0]
    assert len(point["groups"]) == 2
    assert sum(g["scheduled"] for g in point["groups"]) == 8
    assert sum(g["local_drops"] for g in point["groups"]) == 6
    assert sum(g["failed"] for g in point["groups"]) == 0
    assert all(g["error_percent"] in {0, None} for g in point["groups"])
    assert all(
        r.dispatch_lag_ms is not None and r.arrival_elapsed_ms >= r.elapsed_ms
        for r in run.records
        if r.load_point.get("point_id")
    )
    assert all(r.check_ids == ["load"] for r in run.records)
    assert run.slots.qsize() == run.slots.maxsize


async def test_interference_trigger_missing_is_inconclusive_and_two_full_windows():
    c = config()
    w = c.load_test.workloads
    w.combinations.enabled = False
    w.interference.enabled = True
    c.load_test.patterns.concurrent.levels = [2]
    c.load_test.schedule.duration_seconds = 0.03
    run, client = _Run(c, None, None), Client(trigger=False)
    obs = await execute(run, c.targets[0], client)
    point = obs["points"][0]
    assert [w["phase"] for w in point["measurement_windows"]] == ["baseline", "probe"]
    assert all(w["delivered_window_seconds"] >= 0.029 for w in point["measurement_windows"])
    assert point["interference"]["triggered"] == 0
    assert point["status"] == "inconclusive"


def record(**updates):
    return RequestRecord(
        id="r",
        target="local",
        fixture_id="f",
        scenario="load_test",
        check_ids=["load"],
        started_at="now",
        status="completed",
        valid=True,
        score=True,
        elapsed_ms=20,
        first_output_ms=2,
        completion_tokens=10,
        output_tokens=10,
        input_tokens=128,
        load_point={"shape": "short_short"},
        dispatch_ms=0,
        completed_ms=20,
        **updates,
    )


def group_fixture(c, records, arrival=False):
    p = load_points(c.load_test)[0]
    p.update(
        pattern="constant" if arrival else "concurrent",
        rate=1 if arrival else None,
        sending_start_ms=0,
        sending_end_ms=1000,
        delivered_window_seconds=1,
        observation_seconds=1,
        cut_short=False,
        load_qualified=True,
    )
    run = SimpleNamespace(records=records, max_lag_ms=0)
    g = measurements(run, c.targets[0], c.load_test, p, records, "short_short")
    return p, g


def test_per_response_not_p95_and_arrival_clock():
    c = config()
    c.load_test.pass_criteria.max_completion_ms = 100
    records = [record().model_copy(update={"id": str(i)}) for i in range(100)]
    records[-1].elapsed_ms = 200
    p, g = group_fixture(c, records)
    assert g["completion_p95_ms"] == 20
    assert score_group(c.load_test, p, g, records)["status"] == "fail"
    r = record()
    r.arrival_elapsed_ms = 200
    r.arrival_first_output_ms = 182
    r.dispatch_lag_ms = 180
    p, g = group_fixture(c, [r], True)
    assert score_group(c.load_test, p, g, [r])["status"] == "fail"
    assert "planned arrival" in g["timing_clock"]
    p, g = group_fixture(c, [r], False)
    assert score_group(c.load_test, p, g, [r])["status"] == "pass"


def test_client_delay_qualifies_violation_to_inconclusive():
    c = config()
    c.load_test.pass_criteria.max_completion_ms = 10
    rows = [record()]
    p, g = group_fixture(c, rows)
    g["unattributed_timing_violations"], g["timing_violations"] = ["r"], []
    assert score_group(c.load_test, p, g, rows)["reason_code"] == "client_delay"


def test_unknown_judgments_and_measure_only():
    c = config()
    rows = [record()]
    rows[0].score = None
    p, g = group_fixture(c, rows)
    assert score_group(c.load_test, p, g, rows)["reason_code"] == "missing_judgments"
    rows[0].score = True
    c.load_test.pass_criteria.max_first_answer_ms = None
    c.load_test.pass_criteria.max_completion_ms = None
    p, g = group_fixture(c, rows)
    assert score_group(c.load_test, p, g, rows)["reason_code"] == "measure_only"


def test_stress_long_long_and_ceil_coverage():
    c = config()
    w = c.load_test.workloads
    w.combinations.enabled = False
    w.stress.enabled = True
    w.stress.cases = ["long_long"]
    c.load_test.profile.long.output_tokens = 512
    r = record()
    r.load_point["shape"] = "long_long"
    r.input_tokens = 2048
    r.requested_max_tokens = r.requested_output_tokens = 512
    r.output_tokens = 409
    run = SimpleNamespace(records=[r], max_lag_ms=0)
    p = load_points(c.load_test)[0]
    p.update(
        load_qualified=True, cut_short=False, delivered_window_seconds=1, observation_seconds=1
    )
    g = measurements(run, c.targets[0], c.load_test, p, [r], "long_long")
    assert g["coverage"]["threshold_tokens"] == 410
    assert score_group(c.load_test, p, g, [r])["reason_code"] == "insufficient_coverage"
    r.output_tokens = 410
    g = measurements(run, c.targets[0], c.load_test, p, [r], "long_long")
    assert score_group(c.load_test, p, g, [r])["status"] == "pass"


def test_legacy_migration_no_blending_notes_and_roundtrip():
    v = {
        "targets": [TARGET],
        "config_version": 2,
        "checks": ["concurrency", "capacity", "arrivals"],
        "concurrent_load": {
            "mixed": {"enabled": False},
            "schedule": {"levels": [1], "duration_seconds": 2},
            "pass_criteria": {"first_output_p95_ms": 100},
        },
        "traffic": {"arrival": "poisson", "rates": [10], "duration_seconds": 60},
        "arrivals": {"mode": "burst", "requests": 8, "burst_size": 2, "interval_seconds": 3},
    }
    converted, notes = migrate_load_test(v)
    assert converted["checks"] == ["load"]
    c = RunConfig.model_validate(v)
    assert c._migration_notes
    assert c.load_test.schedule.duration_seconds == 2
    assert c.load_test.pass_criteria.max_first_answer_ms == 2000
    assert [x.source for x in c.load_test.migrated_tests] == ["capacity", "arrivals"]
    patterns = {p["source"]: p["pattern"] for p in load_points(c.load_test)}
    assert patterns["legacy_0_capacity"] == "poisson"
    assert patterns["legacy_1_arrivals"] == "bursts"
    assert "p95 limits were not converted" in " ".join(notes)
    assert "not blended" in " ".join(notes)
    assert RunConfig.model_validate(c.model_dump()).load_test == c.load_test


async def test_html_report_data_and_measure_only_has_no_accepted_capacity():
    c = config()
    c.load_test.schedule.duration_seconds = 0.02
    c.load_test.pass_criteria.max_first_answer_ms = None
    c.load_test.pass_criteria.max_completion_ms = None
    run, client = _Run(c, None, None), Client()
    await execute(run, c.targets[0], client)
    check = result(run, c.targets[0])
    assert check.id == "load" and not check.metrics["highest_accepted"]
    html = _load_test_html(check.metrics)
    assert (
        "local drops" in html
        and "aggregate output tokens/sec" in html
        and "endpoint-reported" in html
    )


def test_virtual_clock_run_suite_uses_unified_load_and_preserves_accounting(monkeypatch):
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient
    from giraffe import runner
    from giraffe.latency import latency_summary

    async def scenario():
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        c = config()
        report = await runner.run_suite(c)
        load = next(check for check in report.checks if check.id == "load")
        assert load.metrics["points"][0]["delivered_window_seconds"] == pytest.approx(1)
        assert load.metrics["points"][0]["dispatched"] > 100
        assert all("load" in r.check_ids for r in report.requests)
        assert report.manifest["config"]["load_test"]["schedule"]["finish_seconds"] == 0.02
        measured = latency_summary(report)
        entries = {t["name"]: t["measurements"] for t in measured["targets"]}
        row = next(
            r
            for r in request_plan(c, observations=entries)["rows"]
            if r["id"] == "load_combinations"
        )
        assert row["estimated_requests_per_target"] > 100
        assert row["estimate_source"] == "from your last run"
        assert report.manifest["fixture_pack"]["workload_fixture_sha256"]

    run_virtual(monkeypatch, scenario())


def test_virtual_clock_budgets_truncate_and_cancel_without_endpoint_failure(monkeypatch):
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient
    from giraffe import runner

    async def scenario():
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        c = config()
        c.max_requests = 6
        report = await runner.run_suite(c)
        point = next(ch for ch in report.checks if ch.id == "load").metrics["points"][0]
        assert point["cut_short"] and point["status"] == "inconclusive"
        assert point["reason_code"] == "budget_limited"
        assert "max_requests" in report.limits_reached
        c = config()
        c.max_duration_seconds = 0.05
        report = await runner.run_suite(c)
        point = next(ch for ch in report.checks if ch.id == "load").metrics["points"][0]
        assert point["cut_short"] and point["status"] == "inconclusive"
        assert "max_duration_seconds" in report.limits_reached

    run_virtual(monkeypatch, scenario())


async def test_stream_interference_trigger_and_matching_compare_completion_times():
    c = config()
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.interference.enabled = True
    c.load_test.patterns.concurrent.levels = [2]
    c.load_test.schedule.duration_seconds = 0.08
    run, client = _Run(c, None, None), Client((0.008,))
    observation = await execute(run, c.targets[0], client)
    evidence = observation["points"][0]["interference"]
    assert evidence["triggered"] > 0
    assert evidence["probe"]["matched_count"] > 0
    assert evidence["matched_measurement_windows"]
    if evidence["baseline"]["matched_count"]:
        assert evidence["slowdown"] == pytest.approx(
            evidence["probe"]["completion_median_ms"] / evidence["baseline"]["completion_median_ms"]
        )


def test_interference_slowdown_fails_and_missing_overlap_cannot_pass():
    from giraffe.load_test import interference_measurements

    a, b, probe = record(), record(), record()
    for r, phase in ((a, "baseline"), (b, "probe")):
        r.load_point.update(role="short_stream", phase=phase)
        r.answer_chunks = 5
        r.first_output_ms = 1
        r.last_output_ms = r.stream_end_ms = 10
    a.id = "baseline"
    b.id = "short"
    probe.id = "probe"
    a.elapsed_ms = 10
    b.elapsed_ms = 30
    a.timing_start_ms = 0
    b.timing_start_ms = 100
    probe.timing_start_ms = 102
    probe.first_output_ms = 3
    probe.input_tokens = 2048
    probe.load_point.update(role="probe", phase="probe", shape="long_short")
    evidence = interference_measurements(
        [a, b, probe],
        [
            {"phase": "baseline", "sending_start_ms": 0},
            {"phase": "probe", "sending_start_ms": 100, "triggered": 1},
        ],
    )
    assert evidence["slowdown"] == 3
    assert evidence["baseline"]["matched_count"] == evidence["probe"]["matched_count"] == 1
    assert evidence["actual_overlap_request_ids"] == ["short"]

    from giraffe.load_test import assess_point

    c = config()
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.interference.enabled = True
    c.load_test.patterns.concurrent.levels = [2]
    run = _Run(c, None, None)
    run.records = [a, b, probe]
    a.dispatch_ms, a.completed_ms = 0, 10
    b.dispatch_ms, b.completed_ms = 100, 130
    probe.dispatch_ms, probe.completed_ms = 102, 122
    point = load_points(c.load_test)[0]
    point.update(
        cut_short=False,
        sending_start_ms=0,
        sending_end_ms=110,
        delivered_window_seconds=0.02,
        observation_seconds=0.04,
        measurement_windows=[
            {"phase": "baseline", "sending_start_ms": 0, "sending_end_ms": 10},
            {"phase": "probe", "sending_start_ms": 100, "sending_end_ms": 110, "triggered": 1},
        ],
    )
    assess_point(run, c.targets[0], c.load_test, point, run.records)
    assert point["status"] == "fail" and "slowdown" in point["reason"]
    point["measurement_windows"][1]["triggered"] = 0
    assess_point(run, c.targets[0], c.load_test, point, run.records)
    assert point["status"] == "inconclusive"


def test_client_delay_qualification_uses_real_timing_health():

    c = config()
    c.load_test.pass_criteria.max_completion_ms = 10
    r = record()
    r.elapsed_ms = r.completed_ms = 200
    run = SimpleNamespace(
        records=[r],
        max_lag_ms=0,
        timing_health=SimpleNamespace(
            snapshot=lambda: {
                "schema_version": 1,
                "windows": [{"start_ms": 0, "end_ms": 200, "lag_ms": 150}],
                "overflow_window": None,
            }
        ),
    )
    p = load_points(c.load_test)[0]
    p.update(
        load_qualified=True, cut_short=False, delivered_window_seconds=1, observation_seconds=1
    )
    g = measurements(run, c.targets[0], c.load_test, p, [r], "short_short")
    assert score_group(c.load_test, p, g, [r])["reason_code"] == "client_delay"


async def test_saved_questions_and_scoring_are_used_without_silent_prompt_replacement():
    v = values()
    v.update(
        request_source="request_pack",
        request_pack={
            "version": 1,
            "cases": [
                {
                    "fixture_id": "mine",
                    "messages": [{"role": "user", "content": "My original saved question"}],
                    "scorer": "exact",
                    "expected": "K7P4",
                    "max_tokens": 24,
                }
            ],
        },
    )
    c = RunConfig.model_validate(v)
    c.load_test.schedule.duration_seconds = 0.02
    run, client = _Run(c, None, None), Client()
    observation = await execute(run, c.targets[0], client)
    assert client.specs and all(
        s.messages[0]["content"] == "My original saved question" for s in client.specs
    )
    assert all(s.max_tokens == 24 and s.scorer == "exact" for s in client.specs)
    assert observation["workload_source"] == "request_pack" and not observation["calibrations"]
    assert "load_calibration" not in [r["id"] for r in request_plan(c)["rows"]]


def test_all_shapes_mixed_and_stress_expand_as_separate_workloads():
    from giraffe.models import SHAPES

    c = config()
    c.load_test.workloads.combinations.selected = list(SHAPES)
    c.load_test.workloads.mixed.enabled = True
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["long_short", "short_long", "long_long"]
    c.load_test.patterns.constant.enabled = c.load_test.patterns.poisson.enabled = True
    c.load_test.patterns.bursts.enabled = True
    c.load_test.patterns.bursts.first_seconds = 0
    c.load_test.patterns.rates = [1]
    points = load_points(c.load_test)
    assert len(points) == (9 + 1 + 3) * 3
    assert [p["type"] for p in points[:27]] == ["combinations"] * 27
    assert sum(p["type"] == "mixed" for p in points) == 3


def test_legacy_steady_arrivals_notes_and_known_context_do_not_size_from_client():
    v = {
        "targets": [TARGET],
        "checks": ["arrivals"],
        "arrivals": {"requests": 10, "requests_per_second": 2, "max_pending": 20},
    }
    c = RunConfig.model_validate(v)
    assert c.load_test.schedule.duration_seconds == 5
    assert c.load_test.patterns.constant.enabled
    assert not c.load_test.patterns.bursts.enabled
    assert "no pending queue" in " ".join(c._migration_notes)
    v = values()
    v["context_limit"] = 128
    assert RunConfig.model_validate(v).load_test.profile.long.input_tokens == 2048
    v["targets"] = [{**TARGET, "deployment": {"serving": {"context_limit": 100}}}]
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(v)
    assert (
        "load_test.profile.short.input_tokens"
        in configuration_errors(exc.value.errors(), v)[0]["fields"]
    )


def test_old_report_renderers_remain_readable():
    from giraffe.models import CheckResult
    from giraffe.reporting import _render
    from tests.test_web import report

    c = config()
    saved = report(c)
    saved.checks = [
        CheckResult(
            id="concurrency",
            title="Concurrent load",
            target="local",
            status="inconclusive",
            summary="Legacy concurrent report",
            metrics={
                "load_types": {"combinations": {"title": "Old combinations", "points": []}},
                "configured_load_seconds": 15,
            },
        ),
        CheckResult(
            id="capacity",
            title="Capacity",
            target="local",
            status="inconclusive",
            summary="Legacy capacity report",
            metrics={"traffic_stages": []},
        ),
        CheckResult(
            id="arrivals",
            title="Arrivals",
            target="local",
            status="inconclusive",
            summary="Legacy arrivals report",
            metrics={"planned": 8, "issued": 4},
        ),
    ]
    html = _render(saved)
    assert all(
        text in html
        for text in (
            "Old combinations",
            "Legacy capacity report",
            "Legacy arrivals report",
            "How these are measured",
        )
    )


def test_accepted_timeout_can_exceed_response_limit_even_when_errors_allowed():
    c = config()
    c.load_test.pass_criteria.max_error_percent = 100
    c.load_test.pass_criteria.max_completion_ms = 10
    r = record()
    r.status, r.valid, r.http_status, r.elapsed_ms = "timeout", False, 200, 200
    p, g = group_fixture(c, [r])
    assert score_group(c.load_test, p, g, [r])["reason_code"] == "timing"


async def test_active_request_finishes_inside_allowance_without_cancellation():
    c = config()
    c.load_test.schedule.duration_seconds = 0.01
    c.load_test.schedule.finish_seconds = 0.03
    run, client = _Run(c, None, None), Client((0.015,))
    obs = await execute(run, c.targets[0], client)
    group = obs["points"][0]["groups"][0]
    assert group["completed_during_finish"] == 1
    assert group["cancelled"] == 0
    assert group["throughput_window_seconds"] > 0.01
    assert group["completed_requests_per_second"] == pytest.approx(
        1 / group["throughput_window_seconds"]
    )


def test_defaults_follow_mock_except_the_smaller_default_schedule():
    c = RunConfig(targets=[TARGET], checks=["load"])
    cfg = c.load_test
    assert [
        cfg.profile.short.output_tokens,
        cfg.profile.medium.output_tokens,
        cfg.profile.long.output_tokens,
    ] == [64, 256, 512]
    assert cfg.request_timeout_seconds == 60
    assert cfg.pass_criteria.max_first_answer_ms == 2000
    assert cfg.pass_criteria.max_completion_ms == 30000
    assert cfg.patterns.concurrent.levels == [1, 4]
    assert cfg.patterns.rates == [1, 2]
    assert cfg.schedule.duration_seconds == cfg.schedule.finish_seconds == 15
    assert load_time_plan(cfg)["scheduled_tests"] == 12
    assert load_time_plan(cfg)["time_with_allowances_seconds"] == 360


def test_explicit_parallel_endpoint_concurrency_must_fit_shared_run_ceiling():
    v = values()
    v.update(targets=[TARGET, {**TARGET, "name": "second"}], overlap_models=True, concurrency=4)
    v["load_test"]["patterns"]["concurrent"]["levels"] = [4]
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(v)
    assert "concurrency" in configuration_errors(exc.value.errors(), v)[0]["fields"]


async def test_repeated_cancellation_preserves_partial_window_records_and_slots():
    class SlowCleanup(Client):
        async def execute(self, spec, first_output_event=None):
            try:
                return await super().execute(spec, first_output_event)
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)
                raise

    c = config()
    c.load_test.schedule.duration_seconds = 0.1
    run, client = _Run(c, None, None), SlowCleanup((0.2,))
    task = asyncio.create_task(execute(run, c.targets[0], client))
    await asyncio.sleep(0.006)
    task.cancel()
    await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    check = result(run, c.targets[0])
    point = check.metrics["points"][0]
    assert point["cancelled"] == 1
    assert point["delivered_window_seconds"] > 0
    assert point["cut_short"] and point["status"] == "inconclusive"
    sending, drain = run.observations["local"]["schedule_transitions"]
    assert sending["end_ms"] == point["sending_end_ms"]
    assert drain["start_ms"] == sending["end_ms"]
    assert drain["end_ms"] >= drain["start_ms"]
    assert drain["intended_concurrency"] == 0
    assert run.slots.qsize() == run.slots.maxsize
