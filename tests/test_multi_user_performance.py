import pytest

from giraffe.models import CheckResult
from giraffe.multi_user_metrics import summarize
from giraffe.performance import build_performance
from tests.test_multi_user_reporting import user_report


@pytest.mark.parametrize("scenarios", [
    ["multi_user_2_arrivals", "multi_user_3_arrivals"],
    ["multi_user_2_recovery_control", "multi_user_2_recovery_lighter"],
    ["multi_user_2_neighbors_long_short_before_output_probe",
     "multi_user_2_neighbors_short_long_during_stream_probe"],
])
def test_scenario_measurement_labels_preserve_condition_identity(scenarios):
    from giraffe.conditions import condition_for
    from giraffe.performance import _cohort_label
    from tests.test_multi_user_metrics import record

    labels = []
    for scenario in scenarios:
        request = record("user-01", scenario=scenario)
        request.traffic_stage = 0
        request.workload.update(role=scenario.rsplit("_", 1)[-1],
                                scenario="noisy_neighbors" if "neighbors" in scenario else
                                         "recovery" if "recovery" in scenario else "arrivals",
                                population=int(scenario.split("_")[2]))
        label = _cohort_label([request], {})[1]
        assert label == condition_for(scenario, request.workload)["label"]
        labels.append(label)
    assert len(set(labels)) == len(scenarios)


def test_saved_multi_user_stage_numbers_do_not_join_capacity_baselines():
    from giraffe.reporting import compare_baseline
    from tests.test_traffic_reporting import traffic_report

    current, baseline = traffic_report("current"), traffic_report("baseline")
    user_window = "multi_user_5_arrivals"
    for report, tokens in ((current, 64), (baseline, 10)):
        for row in report.requests:
            row.output_tokens = 10
        arrivals = user_report().requests
        for row in arrivals:
            row.scenario = user_window
            row.traffic_stage = 0
            row.workload.update(window_id=user_window, scenario="arrivals", role="arrivals")
            row.output_tokens = tokens
        report.requests.extend(arrivals)

    compared = compare_baseline(current, baseline)
    stage = compared.baseline["targets"][0]["traffic_capacity"]["stages"][0]
    assert stage["evidence_ids"] == [r.id for r in current.requests if "capacity" in r.check_ids and r.traffic_stage == 0]
    assert stage["timing_comparable"]
    assert stage["output_work"]["current"] == stage["output_work"]["baseline"]

    scope = f"full:{user_window}"
    data = build_performance(compared, window=scope)["targets"][0]["summaries"][scope]
    assert data["metrics"]["baseline_capacity"]["value"] is None
    assert data["metrics"]["baseline_throughput"]["value"] is None


@pytest.mark.parametrize(("scenario", "role"), [("arrivals", "arrivals"), ("recovery", "control")])
@pytest.mark.parametrize("legacy_timing", [False, True])
def test_full_multi_user_window_binds_only_matching_baseline_cohorts(scenario, role, legacy_timing):
    from giraffe.reporting import compare_baseline

    report = user_report()
    user_window = report.checks[0].metrics["multi_user"]["windows"][0]
    window_id = f"multi_user_5_{scenario}" + (f"_{role}" if scenario == "recovery" else "")
    user_window.update(id=window_id, scenario=scenario, role=role)
    for user in user_window["users"]:
        user["cohort"] = ("shared" if scenario == "recovery" else
                          "newcomer" if user["user_id"] == "user-05" else "incumbent")
    cohorts = {user["user_id"]: user["cohort"] for user in user_window["users"]}
    for request in report.requests:
        request.scenario = window_id
        request.workload.update(window_id=window_id, scenario=scenario, role=role,
                                population=user_window["population"], cohort=cohorts[request.workload["user_id"]])
    report.observations["targets"]["local"]["schedule_transitions"] = [{
        "stage_id": window_id, "check_id": "multi_user", "kind": "arrival_rate",
        "start_ms": 0, "end_ms": 4000, "intended_rate_rps": 5,
    }]

    compared = compare_baseline(report, report)
    comparisons = compared.baseline["targets"][0]["comparisons"]
    timing = [row for row in comparisons if row["metric"] in {"latency_ms", "first_output_ms"}]
    delivery = [row for row in comparisons if row["metric"] == "shared_window_output_tokens_per_second"]
    assert len(timing) == 10 and len(delivery) == 5
    comparisons.extend([
        {**timing[0], "cohort": "different", "status": "fail"},
        {**timing[0], "window_id": "another-window", "status": "fail"},
        {**timing[0], "check": "serving", "scenario": window_id, "status": "fail"},
        {**delivery[0], "cohort": "different", "status": "fail"},
    ])
    if legacy_timing:
        for row in timing:
            row.pop("cohort", None)
    else:
        assert all(row.get("cohort") == cohorts[row["user_id"]] for row in timing)

    full = f"full:{window_id}"
    target = build_performance(compared, window=full)["targets"][0]
    metrics = target["summaries"][full]["metrics"]
    assert metrics["baseline_timing"]["value"] == timing
    assert metrics["baseline_throughput"]["value"] == delivery
    assert metrics["baseline_capacity"]["value"] is None

    stage = f"stage:0:{window_id}"
    target = build_performance(compared, window=stage)["targets"][0]
    for metric in ("baseline_timing", "baseline_throughput"):
        assert target["summaries"][stage]["metrics"][metric]["value"] is None


@pytest.mark.parametrize(("generated", "scope", "expected"), [
    (2, "all", "fail"), (14, "all", "inconclusive"),
    (2, "lighter", "fail"), (14, "lighter", "inconclusive"),
    (14, "lighter-work", "inconclusive"),
])
def test_scheduled_user_timing_requires_comparable_total_generation(monkeypatch, local_tokenizer, generated, scope, expected):
    import asyncio

    from giraffe import runner
    from giraffe.reporting import compare_baseline
    from tests.test_multi_user import settings
    from tests.test_multi_user_scenarios import PROFILE
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient

    lighter_only = scope != "all"
    cfg = settings(levels=[2], scenarios=["arrivals", "recovery"], duration_seconds=1, profile=PROFILE,
        arrivals={"requests_per_second": 5, "activations": {"user-02": .4}},
        recovery={"control_seconds": 1, "lighter_seconds": 1, "sustained_seconds": 1,
                  "lighter_requests_per_second": 4, "sustained_requests_per_second": 8, "max_slowdown": 2},
        pass_criteria={} if lighter_only else {"max_completion_ms": 40})

    class Observed(FakeClient):
        delay = .02
        completion_tokens = 2
        slower = False

        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            if "multi_user" in spec.check_ids:
                lighter = spec.workload.get("role") == "lighter"
                record.completion_tokens = self.completion_tokens if scope != "lighter-work" or lighter else 2
                record.reasoning_tokens = record.completion_tokens - 2
                if self.slower and lighter_only and lighter:
                    await asyncio.sleep(.04)
                    record.elapsed_ms += 40
            return record

    async def replay():
        baseline = await runner.run_suite(cfg)
        if not lighter_only:
            Observed.delay = .06
        Observed.slower = True
        Observed.completion_tokens = generated
        return await runner.run_suite(cfg), baseline

    monkeypatch.setattr(runner, "LLMClient", Observed)
    current, baseline = run_virtual(monkeypatch, replay())
    current_acceptance = "inconclusive" if scope == "lighter-work" else "fail"
    for report in (current, baseline):
        check = next(check for check in report.checks if check.id == "multi_user")
        status = current_acceptance if lighter_only and report is current else "pass"
        assert check.metrics["multi_user"]["windows"][-1]["recovery"]["status"] == status
    compared = compare_baseline(current, baseline)
    assert compared.baseline["acceptance"] == {"current": current_acceptance, "baseline": "pass"}
    assert compared.baseline["status"] == expected
    if lighter_only:
        comparison = compared.baseline["targets"][0]
        rows = comparison["comparisons"]
        recovery = [row for row in rows if row["metric"] == "scenario_acceptance"]
        if scope == "lighter-work":
            assert not recovery
            assert any("Recovery evidence is missing or unqualified" in reason for reason in comparison["reasons"])
        else:
            assert len(recovery) == 2 and {row["status"] for row in recovery} == {expected}
        assert all(row["metric"] != "user_acceptance" for row in rows)
    for role in ("arrivals", "recovery_control", "recovery_sustained", "recovery_lighter"):
        window = f"full:multi_user_2_{role}"
        target, = build_performance(compared, target="service", window=window)["targets"]
        metrics = target["summaries"][window]["metrics"]
        completion = [row for row in metrics["baseline_timing"]["evidence"] if row["metric"] == "latency_ms"]
        slower = not lighter_only or role == "recovery_lighter"
        work_changed = generated == 14 and (scope != "lighter-work" or role == "recovery_lighter")
        status = expected if slower or work_changed else "pass"
        assert len(completion) == 2 and {row["status"] for row in completion} == {status}
        assert [row["baseline"] for row in completion] == pytest.approx([20, 20])
        assert [row["current"] for row in completion] == pytest.approx([60, 60] if slower else [20, 20])
        delivery = metrics["baseline_throughput"]["value"]
        assert len(delivery) == 2 and all(row["current"] == row["baseline"] for row in delivery)


@pytest.mark.parametrize(("swap_generation", "expected"), [(False, "fail"), (True, "inconclusive")])
def test_scheduled_stream_gap_qualifies_its_own_generation_samples(monkeypatch, local_tokenizer, swap_generation, expected):
    from giraffe import runner
    from giraffe.reporting import compare_baseline
    from tests.test_multi_user import settings
    from tests.test_multi_user_scenarios import PROFILE
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient

    cfg = settings(levels=[2], scenarios=["arrivals"], duration_seconds=1, profile=PROFILE,
                   arrivals={"requests_per_second": 8})

    class Observed(FakeClient):
        changed = False
        delay = .04

        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            if "multi_user" in spec.check_ids:
                has_gap = bool(spec.workload["request_sequence"] % 2)
                with_reasoning = has_gap if self.changed and swap_generation else not has_gap
                record.completion_tokens = 14 if with_reasoning else 2
                record.reasoning_tokens = record.completion_tokens - 2
                if has_gap:
                    record.max_stream_gap_ms = 18 if self.changed else 2
                    record.last_output_ms = record.first_output_ms + record.max_stream_gap_ms
                else:
                    record.max_stream_gap_ms = None
                    record.last_output_ms = record.first_output_ms
                    record.answer_chunks = record.answer_reads = 1
            return record

    async def replay():
        baseline = await runner.run_suite(cfg)
        Observed.changed = True
        return await runner.run_suite(cfg), baseline

    monkeypatch.setattr(runner, "LLMClient", Observed)
    current, baseline = run_virtual(monkeypatch, replay())
    result = compare_baseline(current, baseline)
    comparisons = result.baseline["targets"][0]["comparisons"]
    gaps = [row for row in comparisons if row["metric"] == "max_stream_gap_ms"]
    assert len(gaps) == 2
    for row in gaps:
        assert (row["current"], row["baseline"], row["status"]) == (18, 2, expected)
        assert row["current_samples"] == 4
        assert row["evidence_ids"] == [record.id for record in current.requests
            if record.workload.get("user_id") == row["user_id"] and record.max_stream_gap_ms is not None]
    assert {row["status"] for row in comparisons if row["metric"] == "latency_ms"} == {"pass"}


def test_scheduled_user_and_capacity_windows_keep_separate_offer_ledgers():
    from tests.test_multi_user_metrics import record

    report = user_report()
    user_window = "multi_user_5_arrivals"
    for request in report.requests:
        request.scenario = user_window
        request.traffic_stage = 0
        request.workload.update(window_id=user_window, scenario="arrivals", role="arrivals")
    summary_window = report.checks[0].metrics["multi_user"]["windows"][0]
    summary_window.update(id=user_window, scenario="arrivals", offer_accounting={
        "planned": 48, "offered": 47, "dispatched": 40, "dropped_local": 3,
        "dropped_late": 2, "unissued": 2, "not_offered": 1})
    capacity_request = record("user-01", identity="capacity", scenario="traffic_stage_0",
                              target="local", check_ids=["capacity"], traffic_stage=0, workload={})
    report.requests.append(capacity_request)
    report.manifest["config"]["checks"].append("capacity")
    report.checks.append(CheckResult(id="capacity", target="local", title="Capacity", status="pass",
        summary="Capacity completed.", metrics={"traffic_stages": [{
            "stage_index": 0, "rate_rps": 50, "accepted": True, "dropped_local": 25,
        }]}))

    def metrics(window):
        target = build_performance(report, window=window)["targets"][0]
        return target["summaries"][window]["metrics"]

    arrivals = metrics(f"full:{user_window}")
    drops, = arrivals["drops"]["value"]
    assert drops["dropped_local"] == 3
    assert drops["dropped_late"] == 2
    assert drops["unissued"] == 2
    assert drops["not_offered"] == 1
    assert arrivals["highest_acceptable_rate"]["value"] is None
    capacity = metrics("full:traffic:check=capacity")
    assert capacity["drops"]["value"][0]["dropped_local"] == 25
    assert capacity["highest_acceptable_rate"]["value"] == 50


@pytest.mark.parametrize("missing_users", [0, 1, 5])
def test_delivery_envelope_counts_known_zero_rates_and_missing_usage(missing_users):
    report = user_report()
    missing = {f"user-{index:02}" for index in range(1, missing_users + 1)}
    for record in report.requests:
        record.output_tokens = None if record.workload["user_id"] in missing else 0
    source = report.observations["targets"]["local"]["multi_user"]
    report.checks[0].metrics["multi_user"] = summarize(source, report.requests)

    target = build_performance(report)["targets"][0]
    delivery = target["summaries"][target["default_window"]]["metrics"]["persistent_user_delivery"]

    assert len(delivery["value"]) == 5
    assert delivery["counts"] == {"sample": 5 - missing_users, "eligible": 5, "missing": missing_users}
    assert delivery["sample_count"] == 5 - missing_users
    assert delivery["eligible_count"] == 5
    assert delivery["state"] == ("insufficient_evidence" if missing_users else "measured")
    assert bool(delivery["reason"]) == bool(missing_users)
    assert {row["user_id"] for row in delivery["value"] if row["output_tokens_per_second"] is None} == missing
