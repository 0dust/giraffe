"""Accepted tested loads remain comparable when optional metrics are sparse."""

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


def arrival_report(shapes=("short_short",), *, concurrent=False, count=None):
    config = configuration(limits={"min_samples": 5 if concurrent else 100}, load={
        "combinations": {"selected": list(shapes)},
        "patterns": {"concurrent": {"enabled": concurrent, "levels": [1, 4]}, "constant": {"enabled": not concurrent},
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
        sample_count = count if count is not None else int(point["rate"])
        records = [RequestRecord(id=f'{point["id"]}-{index}', target="local",
            fixture_id=f'load.{point["shape"]}', scenario=point["id"], check_ids=["load"],
            started_at="now", status="completed", valid=True, score=True,
            input_chars=256, input_tokens=64, output="K7P4", output_chars=4, output_tokens=1,
            elapsed_ms=10, first_output_ms=2, arrival_elapsed_ms=10, arrival_first_output_ms=2,
            load_point={**point, "point_id": point["id"], "role": "request"})
            for index in range(sample_count)]
        point.update(status="pass", dispatched=sample_count, scheduled=None if concurrent else sample_count,
            server_evidenced_concurrency=point['level'] if concurrent else None, not_sent=0, cancelled=0,
            request_ids=[row.id for row in records],
            groups=[{"shape": point["shape"], "status": "pass"}])
        report.requests.extend(records)
    report.checks = [CheckResult(id="load", title="Load test", target="local", status="pass",
        summary="All tested arrival rates accepted.", metrics={"points": points,
        "highest_accepted": [{"source": "primary", "type": "combinations", "bursts": False,
                              "pattern": "concurrent" if concurrent else "constant", "workload": shape,
                              "value": 4 if concurrent else 50,
                              "label": "Highest accepted concurrency" if concurrent else "Highest accepted request rate"}
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


@pytest.mark.parametrize("missing_side", ["current", "baseline"])
def test_missing_load_cohorts_preserve_only_previously_accepted_levels(missing_side):
    current = arrival_report()
    previous = current.model_copy(deep=True)
    missing = current if missing_side == "current" else previous
    missing.checks[0].metrics = {"points": []}

    compared = compare_baseline(current, previous)

    assert compared.baseline["status"] == 'inconclusive'  # Metric samples remain below their minimum.
    expected = 'inconclusive'
    comparison = compared.baseline["targets"][0]["load_test"]
    assert comparison["status"] == expected
    assert len(comparison['points']) == 2
    assert all(row['acceptance_change'] == f'not_tested_in_{missing_side}' for row in comparison['points'])


@pytest.mark.parametrize("change", ["inconclusive", "truncated", "missing_shape", "configuration"])
def test_incomplete_or_changed_load_cannot_claim_lost_accepted_rate(change):
    old = arrival_report()
    current = old.model_copy(deep=True)
    point = lose_rate(current)
    if change == "inconclusive":
        point["status"] = point["groups"][0]["status"] = "inconclusive"
    elif change == "truncated":
        point["status"] = point["groups"][0]["status"] = "pass"
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
    assert affected["reason"] == {"missing_metadata": "Dispatched request metadata is incomplete.",
                                  "input_chars": "Requested work differs: input identities.",
                                  "requested_max_tokens": "Requested work differs: requested output caps."}[change]
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
    assert target['load_test']['status'] == 'inconclusive'
    assert all(row['status'] == 'inconclusive' for row in target['load_test']['points'])
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


def concurrent_report(count=12):
    report = arrival_report(concurrent=True, count=count)
    for record in report.requests:
        record.workload['input_preparation'] = {
            'target_tokens': 128, 'method': 'tokenizer', 'tokenizer': 'test'}
    return report


def test_calibrated_concurrency_compares_counts_and_character_jitter():
    old, current = concurrent_report(10), concurrent_report(12)
    for record in current.requests:
        record.input_chars += 4
    target = compare_baseline(current, old).baseline['targets'][0]
    assert not target['reasons']
    assert target['load_test']['status'] == 'pass'
    timing = [row for row in target['comparisons'] if row['metric'] == 'latency_ms']
    assert len(timing) == 2 and all(row['status'] == 'pass' for row in timing)
    assert timing[1]['baseline_samples'] == 10 and timing[1]['current_samples'] == 12
    assert timing[1]['load_point']['level'] == 4
    assert all(row['load_point']['shape'] == 'short_short' for row in target['observed_outputs'])


@pytest.mark.parametrize('change', ['target_tokens', 'method', 'tokenizer', 'chat_template', 'count_scope', 'token_count_scope', 'encoding', 'uncalibrated', 'fixture_id', 'stream', 'requested_max_tokens'])
def test_different_load_work_still_blocks_metrics_and_acceptance(change):
    old, current = concurrent_report(), concurrent_report()
    if change == 'uncalibrated':
        current.requests[0].workload.clear()
        current.requests[0].input_chars += 4
    elif change in {'target_tokens', 'method', 'tokenizer', 'chat_template', 'count_scope', 'token_count_scope', 'encoding'}:
        current.requests[0].workload['input_preparation'][change] = 256 if change == 'target_tokens' else 'different'
    else:
        setattr(current.requests[0], change, {'fixture_id': 'different', 'stream': False, 'requested_max_tokens': 100}[change])
    target = compare_baseline(current, old).baseline['targets'][0]
    assert not target['reasons'] and target['load_exclusions']
    assert target['load_test']['status'] == 'inconclusive'
    assert not any(row['load_point']['level'] == 1 for row in target['comparisons'])


@pytest.mark.parametrize('baseline_status,current_status,change,status', [
    ('pass', 'pass', 'unchanged_accepted', 'pass'),
    ('fail', 'fail', 'unchanged_not_accepted', 'inconclusive'),
    ('pass', 'fail', 'lost', 'fail'),
    ('fail', 'pass', 'gained', 'pass'),
    ('inconclusive', 'pass', 'accepted_baseline_not_established', 'inconclusive'),
    ('inconclusive', 'fail', 'not_established_in_baseline', 'inconclusive'),
    ('pass', 'inconclusive', 'not_established_in_current', 'inconclusive'),
    ('inconclusive', 'inconclusive', 'not_established', 'inconclusive'),
])
def test_load_acceptance_changes(baseline_status, current_status, change, status):
    old, current = concurrent_report(), concurrent_report()
    for run, outcome in [(old, baseline_status), (current, current_status)]:
        point = run.checks[0].metrics['points'][1]
        point['status'] = point['groups'][0]['status'] = outcome
        if outcome == 'inconclusive':
            point.update(reason_code='load_not_delivered', server_evidenced_concurrency=1,
                         reason='Requested concurrency not delivered.')
    target = compare_baseline(current, old).baseline['targets'][0]
    comparison = target['load_test']
    row = comparison['points'][1]
    assert row['acceptance_change'] == change and row['status'] == status
    assert comparison['status'] == ('fail' if change == 'lost' else 'inconclusive' if change == 'not_established_in_current' else 'pass')
    assert comparison['summary_counts'][change] == (2 if change == 'unchanged_accepted' else 1)
    if baseline_status == 'inconclusive':
        assert row['baseline_acceptance'] == 'not_established'
    if change == 'gained':
        assert 'newly accepted' in target['summary']
    if baseline_status == 'inconclusive' and current_status == 'pass':
        assert 'not delivered' in row['reason']
        timing = next(r for r in target['comparisons'] if r['load_point']['level'] == 4 and r['metric'] == 'latency_ms')
        warning = next(w for w in timing['warnings'] if w['code'] == 'load_not_delivered')
        assert 'Baseline' in warning['message'] and 'Server queueing is not established' in warning['message']
        assert timing['status'] == 'inconclusive'


@pytest.mark.parametrize('share,changed', [(6, False), (7, True)])
def test_load_request_mix_warns_only_above_ten_percentage_points(share, changed):
    old, current = concurrent_report(10), concurrent_report(10)
    for run, split in [(old, 5), (current, share)]:
        for point in run.checks[0].metrics['points']:
            for index, record in enumerate(r for r in run.requests if r.id in point['request_ids']):
                record.fixture_id = 'a' if index < split else 'b'
    target = compare_baseline(current, old).baseline['targets'][0]
    assert not target['reasons'] and target['load_test']['status'] == 'pass'
    timing = next(r for r in target['comparisons'] if r['metric'] == 'latency_ms')
    assert ('request_mix_changed' in [w['code'] for w in timing['warnings']]) == changed
    assert timing['status'] == ('inconclusive' if changed else 'pass')


def test_load_delivery_warning_preserves_low_sample_reason_and_measurements():
    old, current = concurrent_report(2), concurrent_report(2)
    server_limited(old.checks[0].metrics['points'][1])
    target = compare_baseline(current, old).baseline['targets'][0]
    timing = next(r for r in target['comparisons'] if r['load_point']['level'] == 4 and r['metric'] == 'latency_ms')
    assert timing['status'] == 'inconclusive' and timing['current'] == timing['baseline'] == 10
    assert timing['warnings'][0] == {'code': 'sample_uncertainty', 'message': 'Needs at least 5 measured samples in each run.'}
    assert 'load_not_delivered' in [w['code'] for w in timing['warnings']]


@pytest.mark.parametrize('side', ['baseline', 'current'])
@pytest.mark.parametrize('incomplete', ['cut_short', 'not_run', 'not_sent', 'insufficient_responses'])
def test_incomplete_nonfailing_load_cannot_establish_a_loss_or_gain(side, incomplete):
    old, current = concurrent_report(), concurrent_report()
    run = old if side == 'baseline' else current
    point = run.checks[0].metrics['points'][1]
    point['status'] = point['groups'][0]['status'] = 'pass'
    if incomplete == 'insufficient_responses':
        point['status'] = point['groups'][0]['status'] = 'inconclusive'
        point.update(reason_code=incomplete, reason='Too few valid responses.')
    else:
        point[incomplete] = True if incomplete != 'not_sent' else 1
    comparison = compare_baseline(current, old).baseline['targets'][0]['load_test']
    row = comparison['points'][1]
    assert row['acceptance_change'] == ('accepted_baseline_not_established' if side == 'baseline' else 'not_established_in_current')
    assert row['status'] == 'inconclusive'
    assert comparison['status'] == ('pass' if side == 'baseline' else 'inconclusive')


@pytest.mark.parametrize('side', ['baseline', 'current'])
@pytest.mark.parametrize('coverage_flag', ['cut_short', 'not_sent'])
@pytest.mark.parametrize('reason_code', ['timing', 'error_rate'])
def test_recorded_load_failure_precedes_incomplete_coverage(side, coverage_flag, reason_code):
    old, current = concurrent_report(), concurrent_report()
    point = (old if side == 'baseline' else current).checks[0].metrics['points'][1]
    point['status'] = point['groups'][0]['status'] = 'fail'
    point.update(reason_code=reason_code, reason='A configured acceptance criterion failed.')
    point[coverage_flag] = True if coverage_flag == 'cut_short' else 1
    target = compare_baseline(current, old).baseline['targets'][0]
    comparison = target['load_test']
    row = comparison['points'][1]
    change, status = ('gained', 'pass') if side == 'baseline' else ('lost', 'fail')
    assert row['acceptance_change'] == change
    assert row['status'] == comparison['status'] == target['status'] == status
    assert reason_code in row['reason']


@pytest.mark.parametrize('interference_only', [True, False])
@pytest.mark.parametrize('change', ['work_changed', 'missing_group'])
def test_uncovered_interference_exclusion_makes_target_inconclusive(interference_only, change):
    old = concurrent_report()
    for point in old.checks[0].metrics['points'][:2 if interference_only else 1]:
        point.update(type='interference', workload='interference', pattern='interference')
        for index, record in enumerate(r for r in old.requests if r.id in point['request_ids']):
            phase = 'baseline' if index % 2 else 'probe'
            record.scenario = point['id'] + '.' + phase
            record.load_point.update(type='interference', workload='interference', pattern='interference',
                                     phase=phase, role='short_stream')
    current = old.model_copy(deep=True)
    original = compare_baseline(current, old).baseline['targets'][0]
    assert original['status'] == 'pass'
    record = current.requests[0]
    if change == 'work_changed':
        record.requested_max_tokens += 1
    else:
        current.requests = [r for r in current.requests if r.scenario != record.scenario]
    target = compare_baseline(current, old).baseline['targets'][0]
    assert target['status'] == 'inconclusive'
    assert target['load_exclusions']
    exclusion = target['load_exclusions'][0]
    assert exclusion['load_point']['pattern'] == 'interference'
    assert exclusion['reason'] in target['summary']
    assert any(exclusion['reason'] in reason for reason in target['reasons'])
    if interference_only:
        assert 'load_test' not in target
    else:
        assert target['load_test']['status'] == 'pass'


def test_load_exclusion_without_acceptance_rows_cannot_pass():
    old = concurrent_report()
    old.checks[0].metrics = {'points': []}
    current = old.model_copy(deep=True)
    current.requests[0].requested_max_tokens += 1
    target = compare_baseline(current, old).baseline['targets'][0]
    assert 'load_test' not in target and target['status'] == 'inconclusive'
    assert target['load_exclusions'][0]['reason'] in target['summary']


def test_gain_without_any_accepted_baseline_has_a_passing_gained_summary():
    old, current = concurrent_report(), concurrent_report()
    for point in old.checks[0].metrics['points']:
        point['status'] = point['groups'][0]['status'] = 'fail'
        point.update(reason_code='timing', reason='Response timing exceeded the limit.')
    old.checks[0].metrics['highest_accepted'] = []
    comparison = compare_baseline(current, old).baseline['targets'][0]['load_test']
    assert comparison['status'] == 'pass'
    assert comparison['summary_counts']['gained'] == 2
    assert 'newly accepted' in comparison['reason'] and 'reason_code' not in comparison


@pytest.mark.parametrize('outcome,change,status', [('fail', 'lost', 'fail'),
    ('inconclusive', 'not_established_in_current', 'inconclusive')])
def test_gained_level_cannot_hide_lost_or_unverified_baseline_acceptance(outcome, change, status):
    old, current = concurrent_report(), concurrent_report()
    old_point, current_point = old.checks[0].metrics['points'][1], current.checks[0].metrics['points'][0]
    old_point['status'] = old_point['groups'][0]['status'] = 'fail'
    current_point['status'] = current_point['groups'][0]['status'] = outcome
    target = compare_baseline(current, old).baseline['targets'][0]
    comparison = target['load_test']
    assert comparison['status'] == target['status'] == status
    assert comparison['summary_counts']['gained'] == comparison['summary_counts'][change] == 1
    assert '1 tested load(s) newly accepted' in target['summary']
    assert target['summary'].count(comparison['reason']) == 1


def server_limited(point):
    point['status'] = point['groups'][0]['status'] = 'inconclusive'
    point.update(reason_code='load_not_delivered', reason='Requested concurrency not delivered.',
                 client_mean_inflight=point['level'], server_evidenced_concurrency=1,
                 concurrency_reason=None)


@pytest.mark.parametrize('baseline_status,current_status,change,status', [
    ('limited', 'pass', 'gained', 'pass'), ('pass', 'limited', 'lost', 'fail'),
    ('fail', 'limited', 'unchanged_not_accepted', 'inconclusive'),
    ('limited', 'fail', 'unchanged_not_accepted', 'inconclusive'),
    ('pass', 'fail', 'lost', 'fail')])
def test_server_capacity_shortfall_and_failure_are_established_not_accepted(baseline_status, current_status, change, status):
    old, current = concurrent_report(), concurrent_report()
    for run, outcome in [(old, baseline_status), (current, current_status)]:
        point = run.checks[0].metrics['points'][1]
        if outcome == 'limited':
            server_limited(point)
        else:
            point['status'] = point['groups'][0]['status'] = outcome
            point.update(server_evidenced_concurrency=1, reason_code='timing' if outcome == 'fail' else 'criteria_met')
    comparison = compare_baseline(current, old).baseline['targets'][0]['load_test']
    row = comparison['points'][1]
    assert row['acceptance_change'] == change and row['status'] == status
    assert comparison['status'] == ('fail' if change == 'lost' else 'pass')
    assert row['baseline']['valid'] == row['baseline']['dispatched'] == 12
    assert row['baseline']['required'] == 2
    if 'limited' in (baseline_status, current_status) and change in {'gained', 'lost'}:
        assert 'server ran at most 1 of 4' in row['reason']


@pytest.mark.parametrize('reason_code', ['insufficient_responses', 'client_delay', 'context_exceeded', 'cached_input', 'load_not_delivered', 'cut_short'])
def test_unestablished_baseline_pass_is_accepted_now_without_a_gain(reason_code):
    old, current = concurrent_report(), concurrent_report()
    point = old.checks[0].metrics['points'][1]
    point.update(status='blocked' if reason_code == 'context_exceeded' else 'inconclusive',
                 reason_code=reason_code, reason='Original baseline reason.', valid=2,
                 server_evidenced_concurrency=0, client_mean_inflight=1)
    point['groups'][0]['status'] = point['status']
    if reason_code == 'cut_short':
        point.update(status='pass', reason_code='criteria_met', reason='Per-response criteria met.', cut_short=True)
    comparison = compare_baseline(current, old).baseline['targets'][0]['load_test']
    row = comparison['points'][1]
    assert row['acceptance_change'] == 'accepted_baseline_not_established'
    assert row['status'] == 'inconclusive' and comparison['summary_counts'].get('gained', 0) == 0
    assert 'requested concurrency not delivered' not in row['reason']
    if reason_code == 'cut_short':
        assert 'cut short' in row['reason'] and 'criteria met' not in row['reason']
    else:
        assert 'Original baseline reason' in row['reason'] and reason_code in row['reason']


def test_nonstream_level_one_pass_is_established_without_stream_concurrency_evidence():
    old, current = concurrent_report(), concurrent_report()
    for run in (old, current):
        run.checks[0].metrics['points'][0]['server_evidenced_concurrency'] = 0
        for record in run.requests:
            record.stream = False
    comparison = compare_baseline(current, old).baseline['targets'][0]['load_test']
    assert comparison['points'][0]['acceptance_change'] == 'unchanged_accepted'
    assert comparison['status'] == 'pass'
    assert not any(w['code'] == 'load_not_delivered' for row in compare_baseline(current, old).baseline['targets'][0]['comparisons'] for w in row['warnings'])


@pytest.mark.parametrize('side,latency,expected', [('baseline', 10, 'inconclusive'),
    ('baseline', 100, 'inconclusive'), ('current', 10, 'inconclusive'), ('current', 100, 'fail')])
def test_not_delivered_metric_qualification_depends_on_side(side, latency, expected):
    old, current = concurrent_report(), concurrent_report()
    server_limited((old if side == 'baseline' else current).checks[0].metrics['points'][1])
    for record in current.requests:
        if record.load_point['level'] == 4:
            record.elapsed_ms = latency
    compared = compare_baseline(current, old)
    target = compared.baseline['targets'][0]
    row = next(row for row in target['comparisons'] if row['metric'] == 'latency_ms' and row['load_point']['level'] == 4)
    assert row['status'] == expected
    assert any(w['code'] == 'load_not_delivered' and 'server ran at most 1 of 4' in w['message'] for w in row['warnings'])
    if expected != 'fail':
        assert not any(r['status'] == 'fail' for r in target['comparisons'])


@pytest.mark.parametrize('change', ['gained', 'lost'])
@pytest.mark.parametrize('mixed_shapes', [False, True])
def test_shifted_work_or_shape_mix_prevents_attributing_acceptance_change(change, mixed_shapes):
    old, current = concurrent_report(10), concurrent_report(10)
    failing = (old if change == 'gained' else current).checks[0].metrics['points'][1]
    failing['status'] = failing['groups'][0]['status'] = 'fail'
    for run, share in [(old, 5), (current, 8)]:
        point = run.checks[0].metrics['points'][1]
        for index, record in enumerate(r for r in run.requests if r.id in point['request_ids']):
            record.fixture_id = 'a' if index < share else 'b'
            if mixed_shapes:
                record.load_point['shape'] = 'short_short' if index < share else 'long_short'
        if mixed_shapes:
            point['groups'] = [{'shape': shape, 'status': point['status']} for shape in ('short_short', 'long_short')]
    comparison = compare_baseline(current, old).baseline['targets'][0]['load_test']
    row = comparison['points'][1]
    assert row['acceptance_change'] == 'work_changed' and row['status'] == 'inconclusive'
    assert row['reason'] == 'Request mix shifted by more than 10 percentage points; acceptance change is not attributable.'
    assert comparison['summary_counts'].get(change, 0) == 0


def test_work_exclusion_for_unaccepted_baseline_does_not_taint_target():
    old, current = concurrent_report(), concurrent_report()
    for run in (old, current):
        point = run.checks[0].metrics['points'][1]
        point['status'] = point['groups'][0]['status'] = 'fail'
    next(r for r in current.requests if r.load_point['level'] == 4).fixture_id = 'different'
    target = compare_baseline(current, old).baseline['targets'][0]
    assert target['status'] == target['load_test']['status'] == 'pass'
    assert not target['reasons'] and target['load_exclusions']
    assert target['load_exclusions'][0]['load_point']['level'] == 4
    assert 'fixtures' in target['load_exclusions'][0]['reason']


@pytest.mark.parametrize('field,value,change', [('client_mean_inflight', 3.6, 'gained'),
    ('client_mean_inflight', 3.59, 'accepted_baseline_not_established'),
    ('server_evidenced_concurrency', None, 'accepted_baseline_not_established'),
    ('concurrency_reason', 'Non-streaming responses cannot establish overlap.', 'accepted_baseline_not_established')])
def test_server_shortfall_requires_client_occupancy_and_stream_evidence(field, value, change):
    old, current = concurrent_report(), concurrent_report()
    point = old.checks[0].metrics['points'][1]
    server_limited(point)
    point[field] = value
    row = compare_baseline(current, old).baseline['targets'][0]['load_test']['points'][1]
    assert row['acceptance_change'] == change


@pytest.mark.parametrize('field', ['chat_template', 'count_scope', 'token_count_scope', 'encoding'])
def test_missing_calibration_metadata_differs_from_present_null(field):
    old, current = concurrent_report(), concurrent_report()
    for record in current.requests:
        record.workload['input_preparation'][field] = None
    target = compare_baseline(current, old).baseline['targets'][0]
    assert len(target['load_exclusions']) == 2 and not target['comparisons']
    assert target['load_test']['status'] == 'inconclusive'
