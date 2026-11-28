"""Availability is derived from recorded window evidence, without network I/O."""
from dataclasses import replace
import re

import pytest

from giraffe.availability import CAUSES, FORBIDDEN_TEXT, LIMITS, NO_ACTION_OK, WindowFacts, explain_window
from giraffe.models import CHECK_NAMES, RequestRecord
from giraffe.performance import KPI_IDS, build_performance, cli_summary, metric
from giraffe.states import METRIC_STATES
from tests.performance_fixture import synthetic_report_1000
from tests.test_performance import report, summary
from tests.test_performance_telemetry import counters, snapshot
from tests.test_session_performance import record as session_record

DEFAULT_CHECKS = ("access", "serving", "correctness", "first_output")


def record(identifier="a", start=0, end=1000, **updates):
    values = dict(id=identifier, target="t", fixture_id="test", scenario="generation", check_ids=["generation"],
                  started_at="2026-01-01T00:00:00Z", status="completed", valid=True,
                  dispatch_ms=start, completed_ms=end, elapsed_ms=end-start)
    values.update(updates)
    return RequestRecord(**values)


def issue_report(*, selected=DEFAULT_CHECKS, **updates):
    values = dict(stream=True, timing_schema=1, first_output_ms=10, answer_reads=1,
                  completion_tokens=None, output_tokens=None, input_tokens=None)
    values.update(updates)
    records = [record(str(i), start=i * 1000, end=i * 1000 + 100, scenario="correctness_c1",
                      check_ids=["correctness", "first_output"], **values) for i in range(5)]
    value = report(records, config={"targets": [{"name": "t"}], "checks": list(selected), "stream": values.get("stream", True)})
    value.manifest["effective_checks"] = {check: {} for check in selected}
    # Mutating a manifest does not rebuild a persisted summary automatically.
    value.performance = build_performance(value)
    return value


def test_issue_default_checks_no_usage_streamed():
    value = issue_report()
    rows = summary(value)["metrics"]
    output = rows["output_throughput"]
    assert output["cause"] == "data_missing"
    assert "no token counts" in output["reason"] and "5/5" in output["reason"]
    assert "--checks access,serving,correctness,first_output,load" in output["action"]
    for key in ("tpot", "generation_pace"):
        assert rows[key]["cause"] == "data_missing"
        assert "no token counts" in rows[key]["reason"]
    assert rows["request_throughput"]["value"] is not None
    text = "\n".join(cli_summary(value))
    assert "Output token throughput: not measured - " + output["reason"] in text
    assert "Next:" in text and "--checks access,serving,correctness,first_output,load" in text
    assert "Availability:" not in text
    assert "  Not available:" in text


PACE_BASE = dict(stream=True, timing_schema=1, answer_reads=2, first_output_ms=10,
                 last_output_ms=100, output_tokens=10, completion_tokens=10,
                 first_read_output_tokens=1, last_read_output_tokens=10)


@pytest.mark.parametrize("key,updates,cause,needle", [
    *((key, {"stream": False}, "not_applicable", "--no-stream")
      for key in ("tpot", "itl", "client_ttft", "generation_pace")),
    ("tpot", {"output_tokens": None, "completion_tokens": None,
              "workload": {"token_evidence": {"usage": "rejected"}}}, "endpoint_unsupported", "token counts"),
    ("output_throughput", {"output_tokens": None, "completion_tokens": None,
                          "workload": {"token_evidence": {"usage": "rejected"}}}, "endpoint_unsupported", "rejected"),
    ("tpot", {"first_read_output_tokens": None, "last_read_output_tokens": None}, "endpoint_unsupported", "final token total"),
    ("tpot", {"output_tokens": 1, "completion_tokens": 1}, "too_few_samples", "Generation"),
    ("tpot", {"answer_reads": 1}, "endpoint_unsupported", "single network read"),
    ("tpot", {"first_output_ms": None}, "data_missing", "First/last"),
    ("tpot", {"reasoning": "private thought", "output_tokens": None}, "data_missing", "reasoning_tokens"),
    ("tpot", {"reasoning": "private thought", "reasoning_tokens": None}, "data_missing", "reasoning_tokens"),
    ("tpot", {"tool_calls": [{"id": "call_1", "function": {"name": "test", "arguments": "{}"}}]}, "not_applicable", "Tool-call"),
    ("first_reasoning", {"output": "answer"}, "not_applicable", "no reasoning content"),
    ("first_reasoning", {"reasoning": "private thought"}, "data_missing", "no timestamp"),
    ("first_useful_output", {"first_output_ms": None, "first_reasoning_ms": 5}, "data_missing", "Output cap"),
    ("response_latency", {"valid": False, "status": "failed"}, "data_missing", "failed"),
    ("first_useful_output", {"first_output_ms": None, "valid": False, "status": "failed"}, "data_missing", "Fix the failing requests"),
    ("itl", {"workload": {"token_evidence": {"logprobs": "supported"}}}, "data_missing", "batching"),
    ("itl", {"workload": {"token_evidence": {"logprobs": "rejected"}}}, "endpoint_unsupported", "rejected"),
    ("itl", {"workload": {"token_evidence": {"logprobs": "not_returned"}}}, "endpoint_unsupported", "no token boundaries"),
    ("itl", {"workload": {"token_evidence": {"logprobs": "shape_unverified"}}}, "not_applicable", "plain-text"),
    ("itl", {"workload": {"token_evidence": {"logprobs": "disabled_by_request"}}}, "not_configured", "request_options"),
    ("itl", {}, "data_missing", "probe"),
])
def test_every_cause_branch_has_specific_copy(key, updates, cause, needle):
    values = {**PACE_BASE, **updates}
    row = summary(issue_report(**values))["metrics"][key]
    assert row["value"] is None
    assert row["cause"] == cause
    assert needle in (row["reason"] + " " + (row["action"] or ""))
    assert "private thought" not in str(row)
    if "rejected" in updates.get("workload", {}).get("token_evidence", {}).values():
        assert row["action"] is None
    if key == "first_reasoning" and not updates:
        assert row["action"] is None


@pytest.mark.parametrize("load", [False, True])
def test_throughput_load_hint_matches_selection(load):
    selected = DEFAULT_CHECKS + (("load",) if load else ())
    row = summary(issue_report(selected=selected))["metrics"]["output_throughput"]
    assert ("Separately, to compare rates under traffic" in row["action"]) is (not load)


@pytest.mark.parametrize("selected", [DEFAULT_CHECKS, DEFAULT_CHECKS + ("generation",)])
def test_short_answers_suggest_generation_only_when_missing(selected):
    row = summary(issue_report(selected=selected, **{**PACE_BASE, "output_tokens": 1}))["metrics"]["tpot"]
    assert ("--checks" in row["action"]) is ("generation" not in selected)
    if "generation" in selected:
        assert "--max-output-tokens" in row["action"]


def test_zero_duration_and_nonstreamed_first_answer():
    value = report([record(start=0, end=0)], config={"targets": [{"name": "t"}], "checks": list(DEFAULT_CHECKS)})
    row = summary(value)["metrics"]["request_throughput"]
    assert row["cause"] == "too_few_samples"
    assert "spanning 0 ms" in row["reason"] and "--max-requests" in row["action"]
    nonstreamed = summary(issue_report(stream=False))["metrics"]["first_useful_output"]
    assert nonstreamed["value"] == 10 and nonstreamed["state"] == "measured"
    assert nonstreamed["cause"] is None


def _all_rows(performance):
    for target in performance["targets"]:
        for window in target["summaries"].values():
            yield from window["metrics"].values()
            yield from window["distributions"].values()
            for rows in window["sections"].values():
                yield from rows
            assert all(key in window["metrics"] for key in KPI_IDS)


def _coverage_reports():
    default = issue_report()
    yield default
    yield issue_report(selected=tuple(check for check in CHECK_NAMES if check != "load"))
    yield synthetic_report_1000()
    yield report([])
    yield report([record(target="other"), record("t")], config={"targets": [{"name": "t"}, {"name": "other"}]})
    # A saved session fixture includes both the request cohort and its lifecycle evidence.
    session_rows = [session_record("a", 0, 10), session_record("a", 1, 10),
                    session_record("b", 0, 30), session_record("c", 0)]
    yield report(session_rows,
                 config={"targets": [{"name": "local"}], "checks": ["sessions"]},
                 observations={"targets": {"local": {"sessions": {"planned_sessions": 3}}}})
    for samples in ([], [snapshot(counters("a", 0, 0), 0)],
                    [snapshot(counters("a", 0, 0), 0), snapshot(counters("a", 1, 2), 1)]):
        yield report([record()], observations={"targets": {"t": {"serving_telemetry": {"snapshots": samples}}}})
    yield build_performance(default, window="full:correctness_c1")


def test_no_generic_text_anywhere():
    for fixture in _coverage_reports():
        performance = fixture if isinstance(fixture, dict) else fixture.performance
        for row in _all_rows(performance):
            if row["value"] is not None:
                assert row["cause"] is None and row["detail"] is None
                continue
            assert row["cause"] in CAUSES and row["cause"] != "unknown", row
            assert row["reason"], row
            assert not any(text in row["reason"] or text in (row["action"] or "") for text in FORBIDDEN_TEXT), row
            assert len(row["reason"]) <= LIMITS["reason"], row
            assert len(re.sub(r"--checks [a-z_,]+\.?", "", row["action"] or "").strip()) <= LIMITS["action"], row
            assert row["action"] or row["cause"] in NO_ACTION_OK, row
            assert row["state"] in METRIC_STATES, row


def _old_pace(r):
    first, last = r.first_read_output_tokens, r.last_read_output_tokens
    if (not r.valid or r.status != "completed" or not r.stream
            or r.output_tokens is None or r.output_tokens <= 1
            or r.answer_reads < 2 or r.first_output_ms is None
            or r.last_output_ms is None or r.last_output_ms <= r.first_output_ms
            or first is None or last is None or not 0 < first < last <= r.output_tokens
            or r.tool_calls or ((r.reasoning_stripped or r.reasoning.strip()) and not r.reasoning_tokens)):
        return None
    return (last - first) * 1000 / (r.last_output_ms - r.first_output_ms)


@pytest.mark.parametrize("updates,code", [
    ({}, None), ({"valid": False}, "not_complete"), ({"status": "failed"}, "not_complete"),
    ({"stream": False}, "not_streamed"), ({"completion_tokens": None, "output_tokens": None}, "no_usage"),
    ({"output_tokens": None}, "reasoning_unseparated"), ({"output_tokens": 1}, "short_answer"),
    ({"answer_reads": 1}, "single_read"), ({"first_output_ms": None}, "no_timing"),
    ({"last_output_ms": None}, "no_timing"), ({"last_output_ms": 10}, "no_timing"),
    ({"first_read_output_tokens": None}, "final_only_usage"), ({"last_read_output_tokens": None}, "final_only_usage"),
    ({"last_read_output_tokens": 11}, "final_only_usage"),
    ({"tool_calls": [{"function": {"name": "test"}}]}, "tool_call"),
    ({"reasoning": "private thought"}, "reasoning_unseparated"),
])
def test_generation_pace_reason_matches_value(updates, code):
    r = record(**{**PACE_BASE, **updates})
    reason = r.generation_pace_reason
    assert (reason[0] if reason else None) == code
    assert (r.generation_tokens_per_second is None) == (reason is not None)
    assert r.generation_tokens_per_second == _old_pace(r)


def test_generation_pace_frozen_formula_on_synthetic_fixture():
    for r in synthetic_report_1000().requests:
        assert r.generation_tokens_per_second == _old_pace(r)


def test_checks_flag_is_authoritative_list():
    value = report([], config={"targets": [{"name": "t"}], "checks": ["access", "serving"]})
    facts = WindowFacts(value, {}, [], [], [], [], None, [])
    assert facts.checks_flag("load") == "--checks access,serving,load"
    assert facts.checks_flag("load").removeprefix("--checks ").split(",") == ["access", "serving", "load"]
    assert all(check in CHECK_NAMES for check in facts.checks_flag("load").split(" ")[1].split(","))
    empty = replace(facts, report=report([]))
    assert empty.checks_flag("load") == "--checks load"
    assert "added to your selected checks" in empty.add_check("load")


def test_unknown_measure_degrades_with_observation_and_next_step():
    facts = WindowFacts(report([]), {}, [], [], [], [], None, [])
    rows = {"future_measure": metric(label="Future measure", unit="", source="test", target="t", window="full")}
    explain_window(rows, {}, facts)
    assert rows["future_measure"]["cause"] == "unknown"
    assert "0 of 0 eligible" in rows["future_measure"]["reason"]
    assert rows["future_measure"]["action"]


REPLY_ROWS = ('response_latency', 'first_useful_output', 'tpot', 'generation_pace',
              'client_ttft', 'itl', 'cached_tokens', 'answer_read_gap', 'reasoning_read_gap',
              'first_reasoning', 'input_tokens', 'output_throughput', 'arrival_first_output', 'arrival_completion')


def test_legacy_timing_precedes_buffering_diagnosis():
    rows = summary(issue_report(timing_schema=0, answer_reads=0, completion_tokens=10, output_tokens=10))['metrics']
    for key in ('tpot', 'generation_pace', 'client_ttft', 'itl', 'answer_read_gap', 'first_reasoning'):
        assert rows[key]['cause'] == 'data_missing'
        assert 'older Giraffe' in rows[key]['reason']
        assert rows[key]['action'] == 'Rerun with the current Giraffe.'
        assert 'proxy' not in rows[key]['reason']


@pytest.mark.parametrize('empty', [False, True])
def test_reply_preconditions_override_every_endpoint_diagnosis(empty):
    value = report([]) if empty else issue_report(valid=False, status='failed', first_output_ms=None)
    for key in REPLY_ROWS:
        row = summary(value)['metrics'][key]
        assert row['cause'] == ('too_few_samples' if empty else 'data_missing'), (key, row)
        assert ('No requests fell' if empty else 'No valid completed replies') in row['reason']
        assert ('Measurement menu' if empty else 'Fix the failing requests') in row['action']
    assert '0 of 0' not in str(list(_all_rows(value.performance)))


def test_by_design_nonstreamed_gateway_never_blames_stream_setting():
    value = report([record(check_ids=['gateway_access'], scenario='gateway_access', stream=False, timing_schema=1)],
                   config={'targets': [{'name': 't'}], 'checks': ['gateway_access'], 'stream': True})
    for key in ('tpot', 'generation_pace', 'client_ttft', 'itl'):
        row = summary(value)['metrics'][key]
        assert row['cause'] == 'not_applicable'
        assert 'Gateway access policy sends non-streamed requests' in row['reason']
        assert 'Measurement menu' in row['action']
        assert '--no-stream' not in row['action']


def test_retained_reasoning_signals_and_missing_text_are_consistent():
    from giraffe.reporting import _without_response
    for signals in ({'reasoning_read_timestamps_ms': [1, 2]}, {}, {'reasoning_stripped': True}):
        raw = record(**{**PACE_BASE, 'reasoning': 'private thought', 'output': 'answer', 'output_chars': 6, **signals})
        trimmed = _without_response(raw)
        value = report([trimmed], config={'targets': [{'name': 't'}], 'checks': ['generation'], 'retention': 'none'})
        rows = summary(value)['metrics']
        for key in ('first_reasoning', 'reasoning_tokens', 'reasoning_read_gap'):
            if rows[key]['value'] is None:
                assert rows[key]['cause'] != 'not_applicable', rows[key]
                assert 'no reasoning content' not in rows[key]['reason']
                assert 'do not emit reasoning' not in rows[key]['reason']
    # Older redacted evidence with no surviving positive signal is uncertain.
    value = report([record(output_chars=10, timing_schema=1)], config={'targets': [{'name': 't'}], 'retention': 'none'})
    row = summary(value)['metrics']['first_reasoning']
    assert row['cause'] == 'data_missing' and 'not kept' in row['reason']
    assert '--retention all' in row['action']


def test_recorded_load_and_legacy_aliases_are_authoritative():
    value = synthetic_report_1000()
    rows = summary(value)['metrics']
    assert rows['drops']['cause'] != 'check_not_selected'
    assert 'not selected' not in rows['drops']['reason']
    facts = WindowFacts(value, {}, value.requests, value.requests, value.requests, [], 1, value.requests)
    assert facts.ran('load')
    alias_report = issue_report(selected=('access', 'capacity', 'concurrency', 'arrivals'))
    facts = WindowFacts(alias_report, {}, alias_report.requests, alias_report.requests, [], [], 1, [])
    hint = facts.checks_flag('load').split()[1].split(',')
    assert hint.count('load') == 1
    assert not {'capacity', 'concurrency', 'arrivals'}.intersection(hint)


@pytest.mark.parametrize('key', ['input_tokens', 'input_throughput', 'input_length', 'total_tokens', 'total_throughput'])
def test_usage_copy_names_the_actual_missing_field_everywhere(key):
    row = summary(issue_report(**{**PACE_BASE, 'input_tokens': None}))['metrics'][key]
    assert row['cause'] == 'data_missing'
    for text in (row['reason'], row['action'], row['detail'] or row['action']):
        assert 'usage.prompt_tokens' in text
        assert 'usage.completion_tokens' not in text


def test_tool_call_without_boundary_counts_is_not_an_endpoint_limitation():
    r = record(**{**PACE_BASE, 'tool_calls': [{'function': {'name': 'test'}}],
                   'first_read_output_tokens': None, 'last_read_output_tokens': None})
    assert r.generation_pace_reason[0] == 'tool_call'
    row = summary(report([r]))['metrics']['generation_pace']
    assert row['cause'] == 'not_applicable' and 'Tool-call' in row['reason']


def test_stale_profile_support_and_counter_reset_classification():
    from giraffe.telemetry import derive_server_metrics
    rows = derive_server_metrics({'snapshots': [{'status': 'stale_samples', 'profile': 'mtplx', 'samples': []}]})
    assert rows['server_ttft_mean_ms']['cause'] == 'endpoint_unsupported'
    assert 'MTPLX' in rows['server_ttft_mean_ms']['reason']
    rows = derive_server_metrics({'snapshots': [snapshot(counters('a', 10, 5), 0), snapshot(counters('a', 1, 1), 1)]})
    row = rows['server_ttft_mean_ms']
    assert row['cause'] == 'data_missing'
    assert 'reset' in row['reason'] and 'two scrapes' not in row['reason']
    assert 'resets' in row['action']


def test_goodput_not_pooled_has_a_matching_next_step():
    facts = WindowFacts(issue_report(selected=('load',)), {}, [], [], [], [], 1, [])
    row = metric(label='Goodput', unit='req/s', source='test', target='t', window='full',
                 reason='Separate load-point rates are not pooled; select one load-point window.')
    # The pooling rule is about load-point scope, so supply a nonempty valid window.
    r = record(check_ids=['load'])
    facts = replace(facts, population=[r], good=[r])
    explain_window({'goodput': row}, {}, facts)
    assert row['cause'] == 'not_applicable'
    assert row['action'] == 'Choose a single load point in the Measurement menu.'


def test_held_concurrency_and_itl_probe_guidance_use_window_facts():
    value = report([record(**PACE_BASE, check_ids=['load'], load_point={'pattern': 'concurrent'})],
                   config={'targets': [{'name': 't'}], 'checks': ['load'], 'stream': True,
                           'load_test': {'patterns': {'concurrent': {'enabled': True}}}})
    rows = summary(value)['metrics']
    for key in ('client_wait', 'dispatch_delay', 'arrival_first_output', 'arrival_completion', 'offered_rate', 'highest_acceptable_rate'):
        row = rows[key]
        assert row['cause'] == 'not_applicable', (key, row)
        assert 'fixed concurrency' in row['reason']
        assert 'Constant arrivals' in row['action'] and 'patterns.constant.enabled' in row['action']
        assert '--checks' not in row['action']
    assert rows['itl']['cause'] == 'data_missing'
    assert 'probe result' in rows['itl']['reason']
    assert 'streamed token and usage support' in rows['itl']['action']
    assert '--no-stream' not in rows['itl']['action']


def test_mixed_pace_reason_counts_and_distribution_sync():
    records = [record(str(i), **{**PACE_BASE, 'first_read_output_tokens': None}) for i in range(3)]
    records += [record('short', **{**PACE_BASE, 'output_tokens': 1})]
    value = report(records)
    rows = summary(value)['metrics']
    assert rows['generation_pace']['cause'] == 'endpoint_unsupported'
    assert '3/4' in rows['generation_pace']['reason'] and 'final token total' in rows['generation_pace']['reason']
    for fixture in _coverage_reports():
        performance = fixture if isinstance(fixture, dict) else fixture.performance
        for target in performance['targets']:
            for result in target['summaries'].values():
                for key in result['metrics'].keys() & result['distributions'].keys():
                    for field in ('cause', 'reason', 'action'):
                        assert result['metrics'][key][field] == result['distributions'][key][field], (key, field)


def test_all_fixture_actions_respect_recorded_checks_and_stream_setting():
    import re
    for fixture in _coverage_reports():
        if isinstance(fixture, dict):
            continue
        for target in fixture.performance['targets']:
            for result in target['summaries'].values():
                records = [r for r in fixture.requests if r.target == target['target']]
                good = [r for r in records if r.valid and r.status == 'completed']
                facts = WindowFacts(fixture, result['window'], records, good, good, [], 1, [])
                for row in _all_rows({'targets': [{'summaries': {'s': result}}]}):
                    if row['value'] is not None:
                        continue
                    action = row['action'] or ''
                    if '--checks ' in action:
                        command = re.search(r'--checks ([a-z_,]+)', action).group(1).split(',')
                        # Hints include existing checks, but their newly added id must not have run.
                        assert not facts.ran(command[-1]), row
                    if fixture.manifest.get('config', {}).get('stream', True):
                        assert 'omit --no-stream' not in action, row


def test_missing_request_timestamps_are_not_missing_usage():
    row = summary(issue_report(dispatch_ms=None, completed_ms=None))['metrics']['request_throughput']
    assert row['cause'] == 'data_missing'
    assert 'timestamps' in row['reason']
    assert row['state'] == 'insufficient_evidence'
    assert 'current Giraffe' in row['action']
    output = summary(issue_report(dispatch_ms=None, completed_ms=None))['metrics']['output_throughput']
    assert output['cause'] == 'data_missing' and output['state'] == 'insufficient_evidence'
    assert 'timestamps' in output['reason']


@pytest.mark.parametrize('missing', ['usage.prompt_tokens', 'usage.completion_tokens'])
def test_total_usage_selects_whichever_field_is_missing(missing):
    updates = {'input_tokens': 10, 'completion_tokens': 10, 'output_tokens': 10}
    updates['input_tokens' if missing.endswith('prompt_tokens') else 'completion_tokens'] = None
    row = summary(issue_report(**updates))['metrics']['total_tokens']
    assert row['cause'] == 'data_missing'
    assert all(missing in row[key] for key in ('reason', 'action', 'detail'))


def test_load_missing_limits_and_interference_name_actual_configuration():
    value = report([record(**PACE_BASE, check_ids=['load'], load_point={'pattern': 'concurrent'})],
                   config={'targets': [{'name': 't'}], 'checks': ['load'],
                           'load_test': {'pass_criteria': {'max_first_answer_ms': None, 'max_completion_ms': None}}})
    rows = summary(value)['metrics']
    assert rows['latency_attainment']['cause'] == 'not_configured'
    assert 'load_test.pass_criteria.max_completion_ms' in rows['latency_attainment']['action']
    assert rows['decode_interference']['cause'] == 'not_configured'
    assert 'workloads.interference.enabled' in rows['decode_interference']['action']


def test_goodput_respects_load_selection_and_window_ownership():
    row = summary(issue_report())['metrics']['goodput']
    assert row['cause'] == 'check_not_selected'
    assert '--checks access,serving,correctness,first_output,load' in row['action']
    row = summary(issue_report(selected=DEFAULT_CHECKS + ('load',)))['metrics']['goodput']
    assert row['cause'] == 'not_applicable'
    assert 'Measurement menu' in row['action'] and '--checks' not in row['action']


@pytest.mark.parametrize('dominant', ['tool_call', 'reasoning_unseparated', 'no_timing'])
def test_mixed_pace_reasons_always_keep_dominant_counts(dominant):
    updates = {'tool_call': {'tool_calls': [{'function': {'name': 'test'}}]},
               'reasoning_unseparated': {'reasoning': 'thought', 'output_tokens': None},
               'no_timing': {'first_output_ms': None}}[dominant]
    records = [record(str(i), **{**PACE_BASE, **updates}) for i in range(3)]
    records.append(record('short', **{**PACE_BASE, 'output_tokens': 1}))
    row = summary(report(records))['metrics']['generation_pace']
    assert row['cause'] == ('not_applicable' if dominant == 'tool_call' else 'data_missing')
    assert '3/4' in row['reason']


@pytest.mark.parametrize('reasoning_text,tool_reply', [('', False), ('private thought', False), ('', True)])
def test_streamed_export_preserves_availability_when_dropping_text_in_memory(tmp_path, reasoning_text, tool_reply):
    import json
    from giraffe.cli import load_report
    from giraffe.report_stream import expanded_report_chunks
    values = {**PACE_BASE, 'output': 'answer', 'reasoning': reasoning_text}
    if tool_reply:
        values.update(tool_calls=[{'function': {'name': 'test'}}], first_read_output_tokens=None, last_read_output_tokens=None)
    value = report([record(**values)],
                   config={'targets': [{'name': 't'}], 'checks': ['generation'], 'retention': 'all'})
    raw = value.model_dump(mode='json')
    raw.pop('performance')
    raw.pop('conditions')
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(raw))
    exported = json.loads(''.join(expanded_report_chunks(path)))
    assert exported['performance'] == load_report(path).performance
    assert exported['requests'] == raw['requests']
    assert '_response_text_available' not in exported['requests'][0]['workload']


def test_burst_load_does_not_claim_missing_or_rejected_steady_rates():
    value = report([record(**PACE_BASE, check_ids=['load'], load_point={'pattern': 'bursts', 'bursts': {'size': 10}})],
                   config={'targets': [{'name': 't'}], 'checks': ['load']})
    rows = summary(value)['metrics']
    for key in ('offered_rate', 'highest_acceptable_rate'):
        assert rows[key]['cause'] == 'not_applicable'
        assert 'Bursts' in rows[key]['reason']
        assert 'Constant arrivals' in rows[key]['action']
        assert '--checks' not in rows[key]['action']


def test_interference_execution_metadata_overrides_missing_manifest_toggle():
    value = report([record(**PACE_BASE, check_ids=['load'],
                           load_point={'pattern': 'interference', 'type': 'interference', 'interference_mode': 'short_during_long'})],
                   config={'targets': [{'name': 't'}], 'checks': ['load']})
    row = summary(value)['metrics']['decode_interference']
    assert row['cause'] == 'not_applicable' and 'matched' in row['reason']
    assert 'Measurement menu' in row['action'] and 'Enable' not in row['action']


OWNER_ROWS = {
    'tools': ('tool_correctness',), 'json': ('structured_correctness',),
    'sessions': ('turn_latency', 'turn_generation', 'conversation_completion', 'user_fairness'),
    'cancellation': ('cancel_followup',), 'recovery': ('recovery_ratios',),
    'load': ('goodput', 'latency_attainment', 'highest_acceptable_rate', 'drops'),
}


@pytest.mark.parametrize('empty', [False, True])
def test_owner_applicability_precedes_missing_reply_data(empty):
    value = report([]) if empty else issue_report(valid=False, status='failed', first_output_ms=None)
    rows = summary(value)['metrics']
    for owner, keys in OWNER_ROWS.items():
        for key in keys:
            assert rows[key]['cause'] == 'check_not_selected', (key, rows[key])
            assert 'Add it:' in rows[key]['action']
    # Run-wide selection establishes the check ran, but does not make this its cohort.
    value.manifest['config']['checks'] = list(OWNER_ROWS)
    value.manifest['effective_checks'] = {check: {} for check in OWNER_ROWS}
    rows = summary(report(value.requests, config=value.manifest['config']))['metrics']
    for keys in OWNER_ROWS.values():
        for key in keys:
            assert rows[key]['cause'] == 'not_applicable', (key, rows[key])
            assert 'Measurement menu' in rows[key]['action']


def test_legacy_gateway_design_applicability_precedes_legacy_timing():
    value = report([record(check_ids=['gateway_access'], scenario='gateway_access', stream=False, timing_schema=0)],
                   config={'targets': [{'name': 't'}], 'checks': ['gateway_access'], 'stream': True})
    for key in ('tpot', 'generation_pace', 'itl', 'client_ttft', 'first_reasoning'):
        row = summary(value)['metrics'][key]
        assert row['cause'] == 'not_applicable'
        assert 'sends non-streamed requests' in row['reason']
        assert 'Measurement menu' in row['action']
        assert 'Rerun' not in row['action']


@pytest.mark.parametrize('invalid', [False, True])
def test_failed_reply_precondition_names_safe_dominant_observation(invalid):
    records = [record(str(i), valid=False, status='completed' if invalid else 'failed',
                      http_status=200 if invalid else (503 if i < 4 else 401),
                      error='secret https://user:password@example.test model output', timing_schema=1)
               for i in range(6)]
    value = report(records)
    row = summary(value)['metrics']['response_latency']
    assert row['cause'] == 'data_missing'
    assert ('Invalid completion, 6 of 6' if invalid else 'HTTP 503, 4 of 6') in row['reason']
    assert 'Fix the failing requests' in row['action']
    assert len(row['reason']) <= 200
    assert 'secret' not in row['reason'] and 'password' not in row['reason']


@pytest.mark.parametrize('tool_only', [False, True])
def test_genuinely_empty_or_tool_only_reply_is_not_retention_loss(tool_only):
    value = report([record(timing_schema=1, completion_tokens=0, output_tokens=0,
                          tool_calls=[{'id': 'call_1'}] if tool_only else [])],
                   config={'targets': [{'name': 't'}], 'retention': 'all'})
    row = summary(value)['metrics']['first_reasoning']
    assert row['cause'] == 'not_applicable'
    assert 'no reasoning content' in row['reason']
    assert 'not kept' not in row['reason']


@pytest.mark.parametrize('key', ['total_tokens', 'total_throughput'])
def test_total_usage_names_every_missing_field(key):
    value = issue_report(selected=DEFAULT_CHECKS + ('load',))
    row = summary(value)['metrics'][key]
    assert row['cause'] == 'data_missing'
    for field in ('usage.prompt_tokens', 'usage.completion_tokens'):
        assert field in row['reason'] and field in row['action'] and field in row['detail']
    assert len(row['reason']) <= 200 and len(row['action']) <= 170


def test_failed_reply_summary_reuses_safe_connection_class():
    value = report([record(str(i), valid=False, status='failed',
                           error='ConnectError: https://user:secret@example.test' if i < 2 else 'private response body')
                    for i in range(3)])
    row = summary(value)['metrics']['response_latency']
    assert row['cause'] == 'data_missing'
    assert 'ConnectError, 2 of 3' in row['reason']
    assert 'secret' not in row['reason'] and 'example.test' not in row['reason']


def test_total_usage_rejected_support_preserves_all_missing_fields():
    value = issue_report(workload={'token_evidence': {'usage': 'rejected'}})
    for key in ('total_tokens', 'total_throughput'):
        row = summary(value)['metrics'][key]
        assert row['cause'] == 'endpoint_unsupported'
        assert 'usage.prompt_tokens' in row['reason'] and 'usage.completion_tokens' in row['reason']
        assert len(row['reason']) <= 200
