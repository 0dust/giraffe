"""Deterministic arithmetic/contract tests
    no endpoint or socket."""
import json
import re

import pytest

from giraffe.models import RequestRecord, RunReport
from giraffe.performance import build_performance, completion_buckets, concurrency_series
from tests.performance_fixture import synthetic_report_1000


def record(identifier="a", start=0, end=1000, **values):
    return RequestRecord(id=identifier, target="t", fixture_id="test", scenario="generation", check_ids=["generation"],
        started_at="2026-01-01T00:00:00Z", status="completed", valid=True,
        dispatch_ms=start, completed_ms=end, elapsed_ms=end-start, **values)


def report(records, *, config=None, **extra):
    return RunReport(run_id="test", started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:05Z",
        overall="inconclusive", manifest={"config": config or {"targets": [{"name": "t"}]}}, requests=records, **extra)


def summary(value, target=0, window=None):
    name = value.performance["available_targets"][target]["target"]
    data = build_performance(value, target=name, window=window)["targets"][0] if window else value.performance["targets"][target]
    return data["summaries"][window or data["default_window"]]


def test_new_and_legacy_reports_expose_versioned_contract():
    value = report([record()])
    assert value.performance["performance_schema"] == 1
    old = value.model_dump()
    old.pop("performance")
    hydrated = RunReport.model_validate(old)
    metrics = summary(hydrated)["metrics"]
    assert metrics["client_ttft"]["value"] is None
    assert metrics["client_ttft"]["state"] == "not_collected"
    assert metrics["tpot"]["value"] is None
    for row in metrics.values():
        assert set(("value", "unit", "source", "state", "reason", "action", "counts", "window", "target")) <= row.keys()
    json.dumps(hydrated.performance, allow_nan=False)


def test_distributions_timing_origins_dispatch_order_and_no_decimation():
    values = [record(str(i), start=i*100, end=i*100+latency,
        timing_start_ms=i*100+25, first_output_ms=100, last_output_ms=200,
        stream_end_ms=latency-25, timing_schema=1, first_token_ms=50, first_token_reason=None)
        for i, latency in enumerate((1000,2000,3000,4000))]
    value = report(list(reversed(values)))
    result = summary(value)
    dist = result["distributions"]["response_latency"]["value"]
    assert dist == pytest.approx({"n":4,"mean":2500,"min":1000,"median":2500,"p90":3700,"p95":3850,"p99":3970,"max":4000})
    series = value.performance["targets"][0]["request_series"]
    assert result["request_row_indices"] == list(range(4))
    assert [row["request_id"] for row in series] == ["0","1","2","3"]
    assert [row["number"] for row in series] == [1,2,3,4]
    assert series[0]["values"]["before_first_useful_output_ms"] == 125
    assert result["metrics"]["first_useful_output"]["value"] == 100
    assert series[0]["values"]["answer_streaming_ms"] == 100
    assert series[0]["values"]["completion_tail_ms"] == 775


def test_concurrency_clips_carry_in_and_uses_completion_before_dispatch_ties():
    records = [record("carry", -100, 500), record("next", 500, 1500), record("zero",500,500), record("partial",750,3000)]
    result = concurrency_series(records, 0, 1000)
    assert result["peak"] == 2
    assert result["average"] == 1.25
    assert [p["in_flight"] for p in result["points"] if p["elapsed_s"] == .5] == [0,1]
    assert concurrency_series(records,0,0)["average"] is None


def test_buckets_boundary_final_partial_and_failed_completions():
    records = [record("a",0,1000), record("b",0,2000), record("c",0,2500)]
    records[-1].status = "timeout"
    records[-1].valid = False
    result = completion_buckets(records,0,2500)
    assert [b["count"] for b in result["buckets"]] == [0,1,2]
    assert sum(b["valid_completions"] for b in result["buckets"]) == 2
    assert result["final_partial_bucket"]
    assert result["buckets"][-1]["duration_s"] == .5


def test_tokens_do_not_double_count_reasoning_cache_or_missing_usage():
    a = record("a", input_tokens=100, output_tokens=20, completion_tokens=30, reasoning_tokens=10, cached_prompt_tokens=50)
    b = record("b",start=100,end=2000,input_tokens=None,output_tokens=None,completion_tokens=None)
    result = summary(report([a,b]))["metrics"]
    assert result["total_tokens"]["value"] == 130
    assert result["total_tokens"]["partial"]
    assert result["cached_share"]["value"] == .5
    assert result["missing_usage"]["value"]["answer"] == 1
    assert result["output_throughput"]["value"] == 10
    assert not result["completion_throughput"]["semantically_identical_to_answer"]


def test_equal_completion_answer_without_reasoning_usage_is_not_same_semantics():
    metrics = summary(report([record(input_tokens=100,output_tokens=20,completion_tokens=20)]))["metrics"]
    assert not metrics["completion_throughput"]["semantically_identical_to_answer"]
    assert metrics["reasoning_tokens"]["value"] is None


def test_outcomes_use_existing_reasoning_and_cancellation_accounting():
    a = record("a", score=True)
    b = record("b")
    b.status="timeout"
    b.valid=False
    c = record("c")
    c.status="cancelled"
    c.valid=False
    d = record("d", first_reasoning_ms=30, reasoning="thinking",finish_reason="length",completion_tokens=10,reasoning_tokens=10,output_tokens=0,
        error="reasoning exhausted the output budget before an answer")
    d.valid=False
    e = record("e",first_output_ms=40)
    e.status="failed"
    e.valid=False
    metrics = summary(report([a,b,c,d,e]))["metrics"]
    assert metrics["reasoning_budget_exhausted"]["value"] == 1
    assert metrics["error_rate"]["value"] == pytest.approx(2/3)
    assert metrics["correctness"]["value"] == 1
    assert metrics["unscored"]["value"] == 4
    assert metrics["partial_failed"]["value"] == 1


def test_no_intended_history_invented_from_ceiling_and_zero_window():
    a=record(start=10,end=10)
    value=report([a])
    value.manifest["config"]["concurrency"]=1000
    result=build_performance(value)["targets"][0]["summaries"]["full:generation"]
    assert result["intended_concurrency"] == []
    assert result["metrics"]["configured_ceiling"]["value"] == 1000
    assert result["metrics"]["request_throughput"]["value"] is None
    assert result["metrics"]["average_concurrency"]["value"] is None


def test_scope_separates_target_warmup_control_probe_and_stages():
    records = [record("warm", start=-1000,end=-500),record("a"),record("probe",start=1000,end=2000),record("other")]
    records[0].scenario="warm"
    records[2].scenario="probe"
    records[3].target="other"
    value=report(records,observations={"targets":{"t":{"schedule_transitions":[dict(stage_id="send",label="C1",kind="concurrency",start_ms=0,end_ms=500,intended_concurrency=1),dict(stage_id="drain",kind="drain",start_ms=500,end_ms=1000,intended_concurrency=0)]}}})
    target=value.performance["targets"][0]
    assert target["default_cohort"] == "generation"
    assert len(value.performance["targets"]) == 1
    assert [t["target"] for t in value.performance["available_targets"]] == ["t", "other"]
    assert build_performance(value, target="other")["targets"][0]["request_series"][0]["request_id"] == "other"
    assert len(target["request_series"]) == 3
    result=summary(value)
    assert result["metrics"]["attempted"]["value"] == 1
    assert [s["intended_concurrency"] for s in result["intended_concurrency"]] == [1,0]
    assert any(w["includes_drain"] and w["kind"]=="stage" for w in target["windows"])


def test_synthetic_thousand_preserves_full_dataset_and_reconciles():
    value = synthetic_report_1000()
    assert value.manifest["synthetic"]
    assert len(value.requests)==1000
    target=value.performance["targets"][0]
    assert len(target["request_series"])==1000
    # The six level changes are windows of the same comparable workload.
    assert len(target["cohorts"])==1
    result=summary(value)
    assert result["metrics"]["attempted"]["value"]==1000
    assert sum(b["count"] for b in result["completion_buckets"]["buckets"])==1000
    stages=[window for window in target["windows"] if window["kind"]=="stage" and not window["includes_drain"]]
    assert [window["request_count"] for window in stages]==[80,140,200,300,180,100]
    assert sum(window["request_count"] for window in stages)==1000
    assert sum(b["count"] for window in target["windows"] if window["kind"]=="stage"
               for b in summary(value,window=window["id"])["completion_buckets"]["buckets"])==1000
    assert len(value.observations["targets"]["SYNTHETIC"]["schedule_transitions"])==7
    assert max(row["values"]["response_latency_ms"] for row in target["request_series"]) > 7000
    assert summary(value)["metrics"]["knee"]["value"] is None


def test_all_sections_have_contract_rows_and_no_answer_text_leaks(tmp_path):
    from giraffe.reporting import _render, write_report
    from giraffe.cli import load_report
    from giraffe.performance import cli_summary
    from giraffe.report_stream import expanded_report_chunks

    value = report([record(output="private answer", reasoning="private thought", input_tokens=10, completion_tokens=2, output_tokens=2)])
    value.manifest["config"]["retention"] = "none"
    value.requests[0].first_output_ms = 100
    path, html = write_report(value, tmp_path, html=True)
    saved = load_report(path)
    # AM5 stores the default summary while avoiding a duplicate raw series.
    assert "performance" in json.loads(path.read_text())
    assert all("request_series" not in target for target in json.loads(path.read_text())["performance"]["targets"])
    assert saved.performance == json.loads(''.join(expanded_report_chunks(path)))["performance"]
    assert "private answer" not in json.dumps(saved.performance)
    assert "private thought" not in json.dumps(saved.performance)
    assert all(row["value"] is None or row["state"] in {"measured", "estimated"} for row in summary(saved)["metrics"].values())
    lines = cli_summary(saved)
    assert "Request throughput: 1 req/s" in '\n'.join(lines)
    assert "Availability:" in '\n'.join(lines)
    # The standalone report renders the same KPI as a card, with its unit in
    # separate markup, rather than the Phase A placeholder's CLI-style line.
    rendered = _render(saved)
    throughput_card = re.search(r'<article class="html-kpi"><h3>Request throughput.*?</article>', rendered).group()
    assert '<div class="html-value">1 <small>req/s</small></div>' in throughput_card
    assert 'Performance overview</h2>' in html.read_text()
    assert not saved.requests[0].output and not saved.requests[0].reasoning
    assert summary(saved)["metrics"]["baseline_timing"]["state"] == "no_baseline"
    assert summary(saved)["metrics"]["websocket_rtt"]["state"] == "not_applicable"
    assert len(summary(saved)["sections"]) == 7


def test_old_json_download_derives_performance_without_retaining_text(tmp_path):
    from giraffe.report_stream import expanded_report_chunks
    from giraffe.cli import load_report

    value = report([record(first_output_ms=100, output="retained answer", input_tokens=100)])
    raw = value.model_dump()
    raw.pop("performance")
    raw.pop("conditions")
    path = tmp_path / "old.json"
    path.write_text(json.dumps(raw))
    expanded = json.loads(''.join(expanded_report_chunks(path)))
    current = load_report(path)
    assert expanded["performance"] == current.performance
    assert expanded["requests"][0]["output"] == "retained answer"
    assert expanded["performance"]["targets"][0]["summaries"]["full:generation"]["metrics"]["client_ttft"]["value"] is None


def test_partial_stage_counts_boundary_completion_once_and_carry_in_occupancy():
    value = report([record("a",start=0,end=500), record("b",start=250,end=1000)], observations={"targets":{"t":{"schedule_transitions":[
        {"stage_id":"send","kind":"concurrency","start_ms":0,"end_ms":500,"intended_concurrency":2},
        {"stage_id":"drain","kind":"drain","start_ms":500,"end_ms":1000,"intended_concurrency":0}]}}})
    target=value.performance["targets"][0]
    sending=summary(value,window="stage:0:generation")
    drain=summary(value,window="stage:1:generation")
    assert sending["metrics"]["request_throughput"]["value"] == 0
    assert drain["metrics"]["request_throughput"]["value"] == 4
    assert drain["metrics"]["average_concurrency"]["value"] == 1
    assert sum(b["count"] for w in target["windows"] if w["kind"]=="stage"
               for b in summary(value, window=w["id"])["completion_buckets"]["buckets"]) == 2


def test_direct_tpot_and_read_gaps_do_not_imply_token_itl_for_multitoken_events():
    r=record(timing_schema=1, input_tokens=10,output_tokens=4,completion_tokens=4,
        first_output_ms=100,last_output_ms=400,answer_reads=2,answer_chars=16,first_read_answer_chars=4,
        answer_read_timestamps_ms=[100,400],output_events=[
            {"kind":"answer","at_ms":100,"read_id":1,"token_count":1},
            {"kind":"answer","at_ms":400,"read_id":2,"token_count":3}])
    data=summary(report([r]))
    assert data["metrics"]["tpot_direct"]["value"] == 100
    assert data["metrics"]["tpot"]["state"] == "measured"
    assert data["metrics"]["itl"]["value"] is None
    assert data["distributions"]["answer_read_gap"]["value"]["median"] == 300
    assert data["metrics"]["itl"]["exclusion_reasons"]


def test_sessions_and_qualified_load_evidence_are_bound_instead_of_placeholder_rows():
    from giraffe.models import CheckResult
    r=record()
    r.scenario="session_live"
    r.condition_id="session_live"
    session=report([r],checks=[CheckResult(id="sessions",target="t",title="Sessions",summary="Measured",status="inconclusive",
        metrics={"conversation_performance":{"planned_conversations":2,"started_conversations":2,"completed_conversations":1,
            "users_with_measured_generation":1,"users":[{"session_id":"a","generation_tokens_per_second_mean":10}],
            "turns":[{"turn":1,"p50_ms":100,"first_output_p50_ms":50,"generation_tokens_per_second_mean":10}]}})])
    metrics=summary(session)["metrics"]
    assert metrics["conversation_completion"]["value"] == .5
    assert metrics["turn_latency"]["value"][0]["p50_ms"] == 100
    assert metrics["user_fairness"]["state"] == "estimated"
    r.scenario="load_primary_combo_short_short_c1"
    r.condition_id=r.scenario
    r.load_point={"point_id":r.scenario,"role":"request","shape":"short_short"}
    loaded=report([r], config={"targets": [{"name": "t"}], "load_test": {"pass_criteria": {"max_completion_ms": 1000}}}, checks=[CheckResult(id="load",target="t",title="Load test",summary="Qualified",status="pass",
        metrics={"points":[{"id":r.scenario,"status":"pass","pattern":"constant","rate":1,"observation_seconds":1,
            "groups":[{"shape":"short_short","status":"pass","correct_work_meeting_limits":1,
                       "timing_quality":{"status":"qualified"},"client_mean_inflight":1}]}]})])
    metrics=summary(loaded)["metrics"]
    assert metrics["goodput"]["value"] == 1
    assert metrics["highest_acceptable_rate"]["value"] == 1
    assert metrics["workload_occupancy"]["value"][0]["shape"] == "short_short"
    assert metrics["latency_attainment"]["value"]


def test_partial_failed_and_reasoning_only_event_evidence_remains_observed():
    a=record("failed",timing_schema=1,first_token_ms=25,first_reasoning_ms=25,reasoning_read_timestamps_ms=[25,50],
        first_output_ms=100,answer_read_timestamps_ms=[100,150])
    a.status="failed"
    a.valid=False
    b=record("truncated",timing_schema=1,first_token_ms=50,read_timestamps_truncated=True,output_events_truncated=True)
    result=summary(report([a,b]))
    assert result["metrics"]["client_ttft"]["value"] == 37.5
    assert result["metrics"]["first_reasoning"]["value"] == 25
    assert result["distributions"]["answer_read_gap"]["value"]["median"] == 50
    assert result["event_evidence"]["truncated_requests"] == 1
    assert result["metrics"]["tpot_direct"]["value"] is None


def test_completion_interference_is_not_relabelled_decode_slowdown():
    from giraffe.models import CheckResult
    r=record()
    r.load_point={"point_id":"point","role":"short_stream","phase":"probe"}
    value=report([r],checks=[CheckResult(id="load",target="t",title="Load",summary="Compared",status="pass",
        metrics={"points":[{"id":"point","interference":{"comparison_metric":"short-answer completion time","slowdown":1.25}}]})])
    metrics=summary(value)["metrics"]
    assert metrics["decode_interference"]["value"] is None
    assert metrics["response_interference"]["value"] == [1.25]


def test_empty_and_larger_than_thousand_runs_keep_missing_distinct_from_zero():
    empty=summary(report([]))
    assert empty["metrics"]["attempted"]["value"] == 0
    assert empty["metrics"]["request_throughput"]["value"] is None
    assert empty["metrics"]["response_latency"]["value"] is None
    large=report([record(str(i),start=i,end=i+1000) for i in range(2001)])
    data=summary(large)
    series = large.performance["targets"][0]["request_series"]
    assert len(series) == 2001
    assert len(data["request_row_indices"]) == 2001
    assert series[-1]["number"] == 2001
    assert data["metrics"]["peak_concurrency"]["value"] == 1000
    assert sum(b["count"] for b in data["completion_buckets"]["buckets"]) == 2001


def test_baseline_rows_reuse_comparable_results_and_preserve_no_baseline_states():
    from giraffe.reporting import compare_baseline
    from tests.test_reporting import make_report
    from tests.test_traffic_reporting import traffic_report
    compared=compare_baseline(make_report(latency=200),make_report("baseline"))
    rows=summary(compared)["metrics"]
    assert rows["baseline_timing"]["state"] == "measured"
    assert rows["baseline_quality"]["state"] == "measured"
    assert rows["baseline_capacity"]["value"] is None
    traffic=compare_baseline(traffic_report(),traffic_report("baseline"))
    rows=summary(traffic)["metrics"]
    assert rows["highest_acceptable_rate"]["value"] == 4
    assert rows["goodput"]["value"] is None  # Distinct qualified stages are not pooled into one rate.
    assert rows["goodput"]["sample_count"] == 2
    assert rows["baseline_capacity"]["state"] == "measured"
    # A subset still binds only its own existing stage qualification. The full
    # request window now spans the sweep, rather than defaulting to rate 1.
    low=traffic.model_copy(update={"requests":[r for r in traffic.requests if r.traffic_stage == 0]})
    target=build_performance(low)["targets"][0]
    rows=target["summaries"][target["default_window"]]["metrics"]
    assert rows["highest_acceptable_rate"]["value"] == 1
    assert rows["goodput"]["value"] == 1
    assert rows["baseline_capacity"]["state"] == "measured"


@pytest.mark.parametrize("stored_without_point_id", [False, True])
def test_load_window_baseline_rows_match_point_shape_and_role(stored_without_point_id):
    from giraffe.reporting import compare_baseline
    from tests.test_load_test_baseline import arrival_report

    current = arrival_report(("short_short", "short_long"), concurrent=True, count=16)
    compared = compare_baseline(current, current.model_copy(deep=True))
    entry = compared.baseline["targets"][0]
    for row in entry["comparisons"]:
        if stored_without_point_id:
            row["load_point"].pop("point_id", None)
        else:
            assert row["load_point"]["point_id"] == row["scenario"].rsplit(".", 1)[0]
    measured = next(row for row in entry["comparisons"] if row["metric"] == "latency_ms")
    entry["comparisons"].append({**measured, "load_point": {**measured["load_point"], "phase": "probe"}})
    entry["comparisons"].append({**measured, "load_point": {**measured["load_point"], "role": "probe"}})
    entry["comparisons"].append({**measured, "load_point": {**measured["load_point"], "shape": "unrelated"}})
    data = build_performance(compared)["targets"][0]
    rows = data["summaries"][data["default_window"]]["metrics"]

    for metric, expected in (("baseline_timing", 4), ("baseline_throughput", 2), ("baseline_quality", 6)):
        assert rows[metric]["state"] == "measured"
        assert len(rows[metric]["value"]) == expected
        assert {row["load_point"]["shape"] for row in rows[metric]["value"]} == {"short_long"}
        assert not any(row["load_point"].get("phase") or row["load_point"].get("role") == "probe"
                       for row in rows[metric]["value"])


def test_load_window_capacity_uses_established_changes_even_when_neutral():
    from giraffe.reporting import compare_baseline
    from tests.test_load_test_baseline import arrival_report

    current = arrival_report(concurrent=True, count=16)
    compared = compare_baseline(current, current.model_copy(deep=True))
    points = compared.baseline["targets"][0]["load_test"]["points"]
    points[1].update(acceptance_change="unchanged_not_accepted", status="inconclusive")
    points.append({**points[0], "acceptance_change": "accepted_baseline_not_established", "status": "pass"})
    data = build_performance(compared)["targets"][0]
    rows = data["summaries"][data["default_window"]]["metrics"]

    assert rows["baseline_capacity"]["state"] == "measured"
    assert [row["acceptance_change"] for row in rows["baseline_capacity"]["value"]] == [
        "unchanged_accepted", "unchanged_not_accepted"]


@pytest.mark.parametrize('status', ['pass', 'fail', 'inconclusive'])
def test_historical_load_capacity_preserves_recorded_status_after_reload(status):
    from giraffe.reporting import compare_baseline
    from tests.test_load_test_baseline import concurrent_report

    current = concurrent_report()
    compared = compare_baseline(current, current.model_copy(deep=True))
    points = compared.baseline['targets'][0]['load_test']['points']
    for point in points:
        point.pop('acceptance_change')
        point['status'] = status
    # Historical reports predate persisted derived performance and conditions.
    raw = compared.model_dump(mode='json', exclude={'performance', 'conditions'})
    reloaded = RunReport.model_validate(raw)
    recorded = json.dumps(reloaded.baseline, sort_keys=True)

    data = build_performance(reloaded)['targets'][0]
    capacity = data['summaries'][data['default_window']]['metrics']['baseline_capacity']

    established = status in {'pass', 'fail'}
    assert capacity['state'] == ('measured' if established else 'insufficient_evidence')
    assert capacity['sample_count'] == (2 if established else 0)
    assert capacity['value'] == (points if established else None)
    assert json.dumps(reloaded.baseline, sort_keys=True) == recorded


@pytest.mark.parametrize('change', [None, 'unknown', 'work_changed', 'not_established'])
@pytest.mark.parametrize('status', ['pass', 'fail'])
def test_current_load_capacity_does_not_fall_back_to_status_for_unqualified_change(change, status):
    from giraffe.reporting import compare_baseline
    from tests.test_load_test_baseline import concurrent_report

    current = concurrent_report()
    compared = compare_baseline(current, current.model_copy(deep=True))
    for point in compared.baseline['targets'][0]['load_test']['points']:
        point.update(acceptance_change=change, status=status)

    data = build_performance(compared)['targets'][0]
    capacity = data['summaries'][data['default_window']]['metrics']['baseline_capacity']

    assert capacity['state'] == 'insufficient_evidence'
    assert capacity['sample_count'] == 0 and capacity['value'] is None


def test_missing_completion_clock_does_not_inflate_token_rate():
    a=record("clocked",output_tokens=10,input_tokens=10,completion_tokens=10)
    b=record("unclocked",output_tokens=100,input_tokens=100,completion_tokens=100)
    b.completed_ms=None
    data=summary(report([a,b]))["metrics"]
    assert data["answer_tokens"]["value"] == 110
    assert data["output_throughput"]["value"] == 10
    assert data["output_throughput"]["partial"]
    assert data["total_throughput"]["value"] == 20


@pytest.mark.parametrize("scored,limit,correct,expected", [
    (0, None, 0, None), (0, 1000, 0, None), (16, None, 16, None),
    (16, 1000, 0, 0), (16, 1000, 16, 8), (8, 1000, 8, 4), (16, "missing", 16, None),
])
def test_goodput_requires_scoring_and_latency_limits_without_losing_measured_zero(scored, limit, correct, expected):
    from giraffe.models import CheckResult

    records = [record(str(i), score=(i < correct) if i < scored else None,
                      load_point={"point_id": "load", "shape": "short_short"}) for i in range(16)]
    group = {"shape": "short_short", "valid": 16, "scored": scored, "unscored": 16-scored,
             "correct_work_meeting_limits": correct, "timing_quality": {"status": "qualified"}}
    value = report(records, checks=[CheckResult(id="load", target="t", title="Load", status="inconclusive",
        summary="Measured", metrics={"points": [{"id": "load", "source": "primary", "observation_seconds": 2,
            "groups": [group]}]})])
    if limit != "missing":
        value.manifest["config"]["load_test"] = {"pass_criteria": {
            "max_first_answer_ms": None, "max_completion_ms": limit}}
    data = build_performance(value)["targets"][0]
    metric = data["summaries"][data["default_window"]]["metrics"]["goodput"]
    assert metric["value"] == expected
    assert metric["state"] == ("measured" if expected is not None else "insufficient_evidence")
    assert metric["sample_count"] == (0 if expected is None else 1)
    assert metric["eligible_count"] == 1
    assert metric["sample_unit"] == "load points"
    assert metric["request_coverage"] == {"sample": scored, "eligible": 16, "missing": 16-scored}
    assert f"{scored} / 16 scored valid requests" in metric["display_note"]
    if not scored:
        assert "unscored" in metric["reason"].lower()
    if limit in (None, "missing"):
        assert "limit" in metric["reason"].lower()
    if 0 < scored < 16:
        assert metric["label"] == "Scored-subset correct goodput"
        assert metric["scored_subset"]
    # Raw workload accounting remains a count of demonstrated successes.
    assert group["correct_work_meeting_limits"] == correct


@pytest.mark.parametrize("scored,latency_limit,expected", [(True, None, None), (False, 150, None), (True, 150, 0)])
def test_historical_capacity_goodput_requires_scored_answers_and_saved_latency_limit(scored, latency_limit, expected):
    from tests.test_traffic_reporting import traffic_report

    value = traffic_report(high_failure="correctness")
    value.requests = [r for r in value.requests if r.traffic_stage == 1]
    if not scored:
        for item in value.requests:
            item.score = None
    for config in (value.manifest["config"], value.manifest["effective_checks"]["capacity"]):
        config["limits"].update(first_output_ms=None, latency_ms=latency_limit)
    data = build_performance(value)['targets'][0]
    metric = data['summaries'][data['default_window']]['metrics']['goodput']
    assert metric['value'] == expected
    assert metric['state'] == ('measured' if expected is not None else 'insufficient_evidence')
    assert value.checks[0].metrics['traffic_stages'][1]['goodput_rps'] == 0
