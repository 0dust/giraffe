"""Accepted tested loads remain comparable when optional metrics are sparse."""

import json

import pytest

from giraffe.load_test import test_points as load_points
from giraffe.models import CheckResult, RequestRecord
from giraffe.reporting import _render, compare_baseline
from tests.test_concurrent_load import configuration
from tests.test_web import report as empty_report


pytestmark = pytest.mark.usefixtures("local_tokenizer")


@pytest.mark.parametrize("request_source", ["builtin", "request_pack"])
@pytest.mark.parametrize("load_result", ["absent", "skipped", "empty_points"])
def test_correctness_baseline_ignores_unselected_load_without_cohorts(request_source, load_result):
    from giraffe.models import RunConfig
    from giraffe.request_pack import pack_identity
    from tests.test_reporting import make_report

    current = make_report(count=16)
    values = {
        "targets": current.manifest["config"]["targets"], "checks": ["correctness"],
        "request_source": request_source, "limits": {"regression_percent": 1_000_000},
        "test_options": {"capacity": {"limits": {"latency_ms": None}}},
    }
    if request_source == "request_pack":
        values["request_pack"] = {"version": 1, "cases": [{
            "fixture_id": "answer", "messages": [{"role": "user", "content": "Return 42"}],
            "scorer": "exact", "expected": "42"}]}
    config = RunConfig.model_validate(values)
    current.manifest["config"] = config.model_dump(mode="json")
    if request_source == "request_pack":
        current.manifest["request_pack"] = pack_identity(config)
    assert current.manifest["config"]["checks"] == ["correctness"]
    for index, record in enumerate(current.requests):
        record.check_ids = ["correctness"]
        record.scenario = ("pack_control" if index < 8 else "pack_loaded") if request_source == "request_pack" else "correctness_c4"
        if request_source == "request_pack":
            record.fixture_id = "pack:answer"
    if load_result != "absent":
        current.checks.append(CheckResult(id="load", title="Load test", target="local",
            required=False, status="skipped", summary="Not selected.",
            metrics={} if load_result == "skipped" else {"points": []}))
    previous = current.model_copy(deep=True)
    previous.run_id = "baseline"
    before = current.model_dump_json(), previous.model_dump_json()

    compared = compare_baseline(current, previous)

    assert compared.baseline["status"] == compared.overall == "pass"
    target = compared.baseline["targets"][0]
    assert "load_test" not in target
    assert target["comparisons"] and all(row["status"] == "pass" for row in target["comparisons"])
    assert (current.model_dump_json(), previous.model_dump_json()) == before


def arrival_report(shapes=("short_short",)):
    config = configuration(limits={"min_samples": 100}, load={
        "combinations": {"selected": list(shapes)},
        "patterns": {"concurrent": {"enabled": False}, "constant": {"enabled": True},
                     "rates": [10, 50]},
    })
    report = empty_report(config)
    report.manifest.update(timing_measurement_version=2,
        fixture_pack={"version": "1", "builtin_sha256": "fixed", "workload_fixture_sha256": "fixed"},
        effective_checks={"load": config.load_test.model_dump()}, effective_traffic=None,
        load_shape={}, run_location={})
    report.requests = []
    points = load_points(config.load_test)
    for point in points:
        count = int(point["rate"])
        records = [RequestRecord(id=f'{point["id"]}-{index}', target="local",
            fixture_id=f'load.{point["shape"]}', scenario=point["id"], check_ids=["load"],
            started_at="now", status="completed", valid=True, score=True,
            input_chars=256, input_tokens=64, output="K7P4", output_chars=4, output_tokens=1,
            elapsed_ms=10, first_output_ms=2, arrival_elapsed_ms=10, arrival_first_output_ms=2,
            load_point={**point, "point_id": point["id"], "role": "request"})
            for index in range(count)]
        point.update(status="pass", dispatched=count, scheduled=count, not_sent=0, cancelled=0,
            request_ids=[row.id for row in records],
            groups=[{"shape": point["shape"], "status": "pass"}])
        report.requests.extend(records)
    report.checks = [CheckResult(id="load", title="Load test", target="local", status="pass",
        summary="All tested arrival rates accepted.", metrics={"points": points,
        "highest_accepted": [{"pattern": "constant", "workload": shape, "value": 50,
                              "label": "Highest accepted request rate"}
                             for shape in shapes]})]
    return report


def lose_rate(report, *, shape="short_short", status="fail"):
    point = next(point for point in report.checks[0].metrics["points"]
                 if point["shape"] == shape and point["rate"] == 50)
    point["status"] = point["groups"][0]["status"] = status
    report.checks[0].metrics["highest_accepted"][0]["value"] = 10
    return point


def test_losing_accepted_rate_fails_baseline_without_bootstrap_sample_minimum():
    old = arrival_report()
    current = old.model_copy(deep=True)
    point = lose_rate(current)
    compared = compare_baseline(current, old)
    assert compared.baseline["status"] == compared.overall == "fail"
    target = compared.baseline["targets"][0]
    assert target["comparisons"] and all(row["status"] == "inconclusive" for row in target["comparisons"])
    comparison = target["load_test"]
    assert comparison["status"] == "fail"
    assert comparison["baseline_highest_accepted"][0]["value"] == 50
    assert comparison["current_highest_accepted"][0]["value"] == 10
    assert [row["status"] for row in comparison["points"]] == ["pass", "fail"]
    assert next(check for check in compared.checks if check.id == "baseline").evidence_ids == point["request_ids"]
    assert "previously acceptable tested load now fails" in _render(compared)


@pytest.mark.parametrize("missing_current", [False, True])
def test_browser_load_acceptance_preserves_loss_or_missing_evidence_under_filters(missing_current):
    from tests.test_ux_results import run_render_checks

    old = arrival_report()
    old.manifest["config"]["limits"]["min_samples"] = 5
    current = old.model_copy(deep=True)
    if missing_current:
        current.checks[0].metrics = {"points": []}
    else:
        lose_rate(current)
    compared = compare_baseline(current, old)
    target = compared.baseline["targets"][0]
    assert all(row["status"] == "pass" for row in target["comparisons"])
    assert target["load_test"]["status"] == ("inconclusive" if missing_current else "fail")
    run_render_checks(r"""
const value=VALUE,missing=MISSING;
state.detail={report:value};
for(const filter of ['all','with_warnings','without_warnings']){
  state.comparisonFilter=filter;state.comparisonCheck='correctness';
  const html=renderComparison(value);
  assert.match(html,/id="results-load-acceptance"/);
  assert.match(html,/Tested load acceptance/);
  assert.match(html,/Baseline highest accepted \(tested\)/);
  assert.match(html,/50 requests\/second/);
  if(missing){
    assert.match(html,/One run lacks final Load test evidence/);
    assert.match(html,/Not established/);
  }else{
    assert.match(html,/10 requests\/second/);
    assert.match(html,/previously acceptable tested load now fails its configured criteria/);
    assert.match(html,/1 previously acceptable tested load\(s\) now fail/);
    assert.match(html,/Comparison evidence/);
  }
}
assert(resultsSections().some(section=>section[0]==='load-acceptance'));
""".replace("VALUE", json.dumps(compared.model_dump(mode="json")))
        .replace("MISSING", json.dumps(missing_current)))


@pytest.mark.parametrize("missing_side", ["current", "baseline"])
def test_load_cohorts_missing_from_only_one_run_remain_inconclusive(missing_side):
    current = arrival_report()
    previous = current.model_copy(deep=True)
    missing = current if missing_side == "current" else previous
    missing.checks[0].metrics = {"points": []}

    compared = compare_baseline(current, previous)

    assert compared.baseline["status"] == "inconclusive"
    comparison = compared.baseline["targets"][0]["load_test"]
    assert comparison["status"] == "inconclusive"
    assert comparison["reason"] == "One run lacks final Load test evidence."


@pytest.mark.parametrize("change", ["inconclusive", "truncated", "missing_shape", "configuration"])
def test_incomplete_or_changed_load_cannot_claim_lost_accepted_rate(change):
    old = arrival_report()
    current = old.model_copy(deep=True)
    point = lose_rate(current)
    if change == "inconclusive":
        point["status"] = point["groups"][0]["status"] = "inconclusive"
    elif change == "truncated":
        point["cut_short"] = True
    elif change == "missing_shape":
        point["groups"][0]["shape"] = "long_short"
    else:
        current.manifest["config"]["load_test"]["patterns"]["rates"] = [10, 100]
    compared = compare_baseline(current, old)
    assert compared.baseline["status"] == "inconclusive"
    assert compared.baseline["targets"][0]["load_test"]["status"] == "inconclusive"
    assert not next(check for check in compared.checks if check.id == "baseline").evidence_ids


def test_matching_shapes_are_not_collapsed_at_the_same_tested_arrival_rate():
    shapes = ("short_short", "long_short", "short_long")
    old = arrival_report(shapes)
    current = old.model_copy(deep=True)
    point = lose_rate(current, shape="long_short")
    compared = compare_baseline(current, old)
    rows = compared.baseline["targets"][0]["load_test"]["points"]
    assert len(rows) == 6
    assert {(row["current"]["shape"], row["current"]["rate"]) for row in rows} == {
        (shape, rate) for shape in shapes for rate in (10, 50)}
    failed = [row for row in rows if row["status"] == "fail"]
    assert len(failed) == 1 and failed[0]["current"]["id"] == point["id"]
    assert compared.baseline["status"] == "fail"


@pytest.mark.parametrize("change", ["input_chars", "requested_max_tokens", "missing_metadata"])
def test_same_config_with_changed_or_missing_actual_request_work_cannot_establish_acceptance_loss(change):
    old = arrival_report()
    current = old.model_copy(deep=True)
    point = lose_rate(current)
    record = next(row for row in current.requests if row.id in point["request_ids"])
    if change == "missing_metadata":
        current.requests.remove(record)
    else:
        setattr(record, change, getattr(record, change) + 1)
    compared = compare_baseline(current, old)
    assert compared.baseline["status"] == "inconclusive"
    rows = compared.baseline["targets"][0]["load_test"]["points"]
    affected = next(row for row in rows if row["current"]["id"] == point["id"])
    assert affected["status"] == "inconclusive"
    assert affected["reason"] == ("Dispatched request metadata is incomplete." if change == "missing_metadata"
                                   else "Requested work or request-shape proportions differ.")
    assert not next(check for check in compared.checks if check.id == "baseline").evidence_ids


def test_observed_mixed_share_variation_does_not_change_work_within_each_shape():
    old = arrival_report()
    for point in old.checks[0].metrics["points"]:
        point.update(type="mixed", workload="mixed", shape=None)
        point["groups"] = [{"shape": shape, "status": "pass"}
                           for shape in ("short_short", "long_short")]
        for index, record in enumerate(row for row in old.requests if row.id in point["request_ids"]):
            record.load_point["shape"] = "long_short" if index % 2 else "short_short"
            record.fixture_id = "load." + record.load_point["shape"]
            record.input_chars = 512 if index % 2 else 256
    current = old.model_copy(deep=True)
    point = current.checks[0].metrics["points"][1]
    point["status"] = point["groups"][0]["status"] = "fail"
    # A completion-driven random mix can observe different shape counts while
    # each shape's prompt size and output cap remain identical between runs.
    changed = next(row for row in current.requests if row.id in point["request_ids"]
                   and row.load_point["shape"] == "short_short")
    changed.load_point["shape"] = "long_short"
    changed.fixture_id, changed.input_chars = "load.long_short", 512
    compared = compare_baseline(current, old)
    assert compared.baseline["status"] == "fail"
    rows = compared.baseline["targets"][0]["load_test"]["points"]
    assert [row["status"] for row in rows] == ["pass", "fail"]


# Fake inference clients in this module use the matching local tokenizer.


def test_failed_baseline_does_not_block_observed_metric_comparison():
    old = arrival_report()
    old.manifest['config']['limits']['min_samples'] = 5
    for point in old.checks[0].metrics['points']:
        point['status'] = point['groups'][0]['status'] = 'fail'
    old.checks[0].status = 'fail'
    old.checks[0].metrics['highest_accepted'] = []
    current = old.model_copy(deep=True)
    compared = compare_baseline(current, old)
    assert compared.baseline['status'] == 'pass'
    assert compared.overall == 'fail'
    assert compared.baseline['acceptance'] == {'baseline': 'fail', 'current': 'fail'}
    target = compared.baseline['targets'][0]
    assert target['load_test']['reason_code'] == 'no_accepted_baseline'
    assert target['comparisons']
    assert all(row['status'] == 'pass' for row in target['comparisons'])


def test_failed_baseline_with_too_few_samples_still_has_no_regression_verdict():
    old = arrival_report()  # min_samples=100, greater than either tested cohort
    for point in old.checks[0].metrics['points']:
        point['status'] = point['groups'][0]['status'] = 'fail'
    old.checks[0].status = 'fail'
    old.checks[0].metrics['highest_accepted'] = []
    compared = compare_baseline(old.model_copy(deep=True), old)
    assert compared.baseline['status'] == 'inconclusive'
    assert compared.baseline['targets'][0]['comparisons'][0]['current'] is not None
