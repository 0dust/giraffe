"""Availability is derived from recorded window evidence, without network I/O."""
from dataclasses import replace
import json
from types import SimpleNamespace

from giraffe import states
from giraffe.fixtures import REASONING_WITHOUT_ANSWER, TRUNCATED_ANSWER
from giraffe.models import EvaluationEvidence, JudgeVote, RequestJudgement
from giraffe.scoring_facts import scoring_facts, scoring_gap
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
    ("first_useful_output", {"first_output_ms": None, "first_reasoning_ms": 5, "finish_reason": "length"}, "data_missing", "Output cap"),
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
    facts = replace(facts, population=[r], good=[r], cohort=[r], dispatched=[r])
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


IDENTITY_OWNERS = {
    'goodput': 'load', 'latency_attainment': 'load', 'offered_rate': 'load',
    'drops': 'load', 'highest_acceptable_rate': 'load', 'correctness': 'correctness',
    'structured_correctness': 'json', 'tool_correctness': 'tools',
    'turn_latency': 'sessions', 'turn_generation': 'sessions',
    'conversation_completion': 'sessions', 'user_fairness': 'sessions',
}


def identity_report(check='load', pattern='concurrent', completion='late'):
    end = 1500 if completion == 'late' else 500
    r = record(check_ids=[check], scenario=f'{check}_c1', stream=True, timing_schema=1,
               end=end, first_output_ms=10, scheduled_ms=0 if pattern == 'constant' else None,
               load_point=({'point_id': 'load_c1', 'source': 'primary', 'pattern': pattern}
                           if check == 'load' else None),
               workload={'pattern': pattern} if check == 'load' else {})
    transitions = [dict(stage_id='send', kind='arrival_rate' if pattern == 'constant' else 'concurrency',
                        start_ms=0, end_ms=1000, check_id=check,
                        **({'intended_rate_rps': 1} if pattern == 'constant' else {'intended_concurrency': 1})),
                   dict(stage_id='drain', kind='drain', start_ms=1000, end_ms=2000, check_id=check)]
    value = report([r], config={'targets': [{'name': 't'}], 'checks': list(CHECK_NAMES)},
                   observations={'targets': {'t': {'schedule_transitions': transitions}}})
    value.manifest['effective_checks'] = {name: {} for name in CHECK_NAMES}
    value.performance = build_performance(value)
    return value


def test_stage_dispatch_identity_survives_completion_in_drain():
    value = identity_report()
    windows = value.performance['targets'][0]['windows']
    assert all(w['check_ids'] == ['load'] and w['cohort_check'] == 'load' for w in windows)
    stage = next(w for w in windows if w['id'].startswith('stage:0:'))
    assert (stage['dispatch_count'], stage['completion_count']) == (1, 0)
    full = next(w for w in windows if w['kind'] == 'full')
    assert (full['dispatch_count'], full['completion_count']) == (1, 1)
    rows = summary(value, window=stage['id'])['metrics']
    for key in ('goodput', 'latency_attainment', 'offered_rate', 'drops', 'highest_acceptable_rate'):
        row = rows[key]
        assert row['cause'] != 'not_applicable' or 'fixed concurrency' in row['reason']
        assert 'not a' not in row['reason']
    row = rows['goodput']
    assert row['cause'] == 'too_few_samples'
    assert 'dispatched 1 request' in row['reason']
    assert 'finished after it ended (during drain)' in row['reason']
    assert 'load_test.schedule.duration_seconds' in row['action']
    assert len(row['reason']) <= 200 and len(row['action']) <= 170


@pytest.mark.parametrize('check,pattern', [('load', 'concurrent'), ('load', 'constant'),
                                         ('correctness', 'concurrent'), ('sessions', 'concurrent')])
@pytest.mark.parametrize('completion', ['early', 'late'])
def test_window_identity_is_cohort_scoped_and_never_vacuous(check, pattern, completion):
    value = identity_report(check, pattern, completion)
    for window in value.performance['targets'][0]['windows']:
        records = value.requests
        population = (records if window['kind'] == 'full' else
                      [r for r in records if window['start_ms'] <= r.completed_ms <= window['end_ms']
                       and (window.get('end_inclusive') or r.completed_ms < window['end_ms'])])
        dispatched = [r for r in records if window['kind'] == 'full'
                      or window['start_ms'] <= r.dispatch_ms < window['end_ms']]
        facts = WindowFacts(value, window, population, population, population, [], window['duration_s'],
                            population, cohort=records, dispatched=dispatched)
        assert facts.owned_checks == frozenset({check})
        assert facts.streamed and facts.timing_versioned
        assert len(facts.nonstreaming_by_design) == len(records)
        assert not any(facts.nonstreaming_by_design)
        assert facts.scheduled == (pattern == 'constant')
        rows = summary(value, window=window['id'])['metrics']
        for key, owner_check in IDENTITY_OWNERS.items():
            row = rows[key]
            if row['value'] is None and owner_check != check:
                assert row['cause'] == 'not_applicable'
                assert 'which is not part of' in row['reason']
            elif row['value'] is None:
                assert 'which is not part of' not in row['reason']


# Declarative contradiction predicates apply to every unavailable row, including
# future owner metrics. Identity evidence never comes from interval completions.
IDENTITY_CONTRADICTIONS = [
    (lambda owner, window, selected: owner in window.get('check_ids', []),
     r'which is not part of|not a .+ window'),
    (lambda owner, window, selected: owner in window.get('check_ids', []) or owner in selected,
     r'was not selected'),
    (lambda owner, window, selected: True, r'not a Load test window'),
]


@pytest.mark.parametrize('check,pattern', [('load', 'constant'), ('load', 'concurrent'),
                                         ('correctness', 'concurrent'), ('json', 'concurrent'),
                                         ('tools', 'concurrent'), ('sessions', 'concurrent')])
@pytest.mark.parametrize('completion', ['early', 'late'])
def test_g1_explanations_never_contradict_window_identity(check, pattern, completion):
    value = identity_report(check, pattern, completion)
    target = value.performance['targets'][0]
    windows = list(target['windows'])
    empty = dict(windows[0], id='send-empty', kind='stage', start_ms=3000, end_ms=4000,
                 includes_drain=False, dispatch_count=0, completion_count=0, request_count=0)
    windows.append(empty)
    selected = value.manifest['effective_checks']
    owner_explanations = {}
    for window in windows:
        if window['id'] == 'send-empty':
            from giraffe.performance import _cohort_label, _window_summary
            window['workload_label'] = _cohort_label(value.requests, value.observations['targets']['t'])[1]
            rows = _window_summary(value, 't', window, value.requests, target['request_series'],
                                   value.observations['targets']['t'])['metrics']
        else:
            rows = summary(value, window=window['id'])['metrics']
        for key, row in rows.items():
            if row['value'] is not None:
                continue
            owner_check = IDENTITY_OWNERS.get(key)
            if owner_check and owner_check != check:
                text = (row['cause'], row['reason'], row['action'])
                assert owner_explanations.setdefault(key, text) == text
            for applies, forbidden in IDENTITY_CONTRADICTIONS:
                if applies(owner_check, window, selected):
                    assert not re.search(forbidden, row['reason'] or ''), (window['id'], key, row)
    # Also cover a drain-only selected interval without any dispatch in it.
    if completion == 'late':
        drain = next(w for w in windows if w.get('includes_drain') and w['kind'] == 'stage')
        assert drain['dispatch_count'] == 0 and drain['completion_count'] == 1



def scoring_record(**updates):
    values = dict(check_ids=["correctness"], score=None,
                  evaluation=EvaluationEvidence(messages=[{"role": "user", "content": "SECRET-INPUT"}],
                                                scorer="judge", expected="SECRET-EXPECTED", expected_provided=True),
                  judgement=RequestJudgement(status="inconclusive", votes=[JudgeVote(
                      judge="j", url="http://SECRET-URL", model="m", status="error", error_category="timeout",
                      reason="SECRET-MODEL-OUTPUT")]), score_message="AI judge evaluation incomplete")
    values.update(updates)
    return record(**values)


def scoring_context(records, config=None, stop=None):
    return SimpleNamespace(good=records, config=config or {}, run_stop=stop,
                           scoring=[scoring_facts(r) for r in records])


def test_scoring_timeout_uses_recorded_outcome_before_absent_config():
    row = scoring_gap(scoring_context([scoring_record(), scoring_record()]), "correctness")
    assert row.cause == "data_missing"
    assert "2/2" in row.reason and "timed out" in row.reason
    assert "judging.request_timeout_seconds" in row.action
    assert "attach" not in (row.reason + row.action).lower()


@pytest.mark.parametrize("updates,config,cause,reason,action", [
    ({"evaluation": None}, {}, "not_configured", "no expected", "--request-pack"),
    ({"evaluation": EvaluationEvidence(messages=[], scorer="none")}, {}, "not_configured", "no expected", "--fixtures"),
    ({"score_message": "AI judge panel tied", "judgement": RequestJudgement(status="inconclusive", votes=[
        JudgeVote(judge="j", url="http://x", model="m", status="completed", verdict=True)])}, {}, "data_missing", "tied", "odd number"),
    ({"judgement": None, "score_message": "AI judgment pending."}, {}, "data_missing", "pending or cancelled", "judging.max_duration_seconds"),
    ({"score_message": TRUNCATED_ANSWER}, {}, "data_missing", "cut off", "--max-output-tokens"),
    ({"score_message": REASONING_WITHOUT_ANSWER}, {}, "data_missing", "reasoning", "--max-output-tokens"),
    ({"evaluation_error": "schema_recursion", "check_ids": ["json"]}, {}, "data_missing", "not a wrong answer", "JSON schema"),
    ({}, {"judging": {"judges": []}}, "not_configured", "no judge endpoint", "judging.judges"),
])
def test_scoring_cause_matrix(updates, config, cause, reason, action):
    records = [scoring_record(**updates), scoring_record(**updates)]
    row = scoring_gap(scoring_context(records, config), "json" if "json" in records[0].check_ids else "correctness")
    assert row.cause == cause
    assert reason in row.reason and action in row.action
    assert len(row.reason) <= 200 and len(row.action) <= 170


@pytest.mark.parametrize("category,needle", [
    ("timeout", "judging.request_timeout_seconds"), ("http_error", "judging.judges"),
    ("request_failed", "judging.judges"), ("client_initialization", "judging.judges"),
    ("truncated_response", "judging.max_output_tokens"), ("invalid_verdict", "verdict JSON"),
    ("invalid_completion", "verdict JSON"), ("phase_deadline", "judging.max_duration_seconds"),
    ("not_evaluated", "judging.max_duration_seconds"), ("cancelled", "judging.max_duration_seconds"),
    ("SECRET-UNKNOWN", "judging.judges"),
])
def test_judge_error_categories_are_private_and_actionable(category, needle):
    value = scoring_record(judgement=RequestJudgement(status="inconclusive", votes=[JudgeVote(
        judge="SECRET-JUDGE", url="https://SECRET-USER:SECRET-PASSWORD@judge.test/v1?SECRET-TOKEN=1#SECRET-FRAGMENT", model="SECRET-MODEL", status="error",
        error_category=category, reason="SECRET-MODEL-OUTPUT")]))
    row = scoring_gap(scoring_context([value, value]), "correctness")
    assert row.cause == "data_missing" and "2/2" in row.reason and needle in row.action
    assert "SECRET-" not in str(row)
    assert "SECRET-" not in json.dumps(scoring_facts(value))


def test_scoring_facts_are_bounded_and_survive_evaluator_drop():
    value = scoring_record()
    facts = scoring_facts(value)
    assert len(json.dumps(facts, separators=(",", ":"))) <= 300
    dropped = value.model_copy(update={"evaluation": None, "judgement": None, "workload": {"_scoring": facts}})
    direct = scoring_gap(scoring_context([value, value]), "correctness")
    streamed = scoring_gap(SimpleNamespace(good=[dropped, dropped], config={}, run_stop=None), "correctness")
    assert direct == streamed


def test_scoring_dominance_counts_all_relevant_valid_replies():
    records = [scoring_record(), scoring_record(), scoring_record(evaluation=None), scoring_record(score=True)]
    row = scoring_gap(scoring_context(records), "correctness")
    assert "2/4" in row.reason and "timed out" in row.reason


@pytest.mark.parametrize("check,key", [("correctness", "correctness"), ("json", "structured_correctness"),
                                       ("tools", "tool_correctness"), ("load", "goodput"),
                                       ("load", "highest_acceptable_rate")])
def test_scoring_sibling_rows_use_recorded_judge_errors(check, key):
    from tests.test_performance import report, summary
    values = dict(check_ids=[check], stream=True, timing_schema=1, output="answer", first_output_ms=10)
    if check == "load":
        values.update(load_point={"source": "primary", "pattern": "constant", "point_id": "p"}, scheduled_ms=0)
    records = [scoring_record(identifier=str(i), start=i*1000, end=i*1000+100, **values) for i in range(2)]
    config = {"targets": [{"name": "t"}], "checks": [check], "load_test": {"pass_criteria": {
        "max_completion_ms": 1000}, "patterns": {"constant": {"enabled": True}}}}
    row = summary(report(records, config=config))["metrics"][key]
    assert row["cause"] == "data_missing"
    assert "judge" in row["reason"] and "timed out" in row["reason"]
    assert "2/2" in row["reason"] and "judging.request_timeout_seconds" in row["action"]
    assert "expected answers" not in row["action"]


def test_scoring_streamed_saved_export_keeps_explanations_and_original_requests(tmp_path):
    from giraffe.cli import load_report
    from giraffe.report_stream import expanded_report_chunks
    from tests.test_performance import report
    value = report([scoring_record(identifier=str(i), start=i*1000, end=i*1000+100,
                                   stream=True, timing_schema=1, first_output_ms=10, output="SECRET-ANSWER")
                    for i in range(2)], config={"targets": [{"name": "t"}], "checks": ["correctness"]})
    raw = value.model_dump(mode="json")
    raw.pop("performance")
    raw.pop("conditions")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(raw))
    exported = json.loads("".join(expanded_report_chunks(path)))
    assert exported["performance"] == load_report(path).performance
    assert exported["requests"] == raw["requests"]
    assert all("_scoring" not in r["workload"] for r in exported["requests"])
    assert "SECRET-" not in json.dumps(exported["performance"])


def test_scoring_dominance_groups_judge_failure_class_before_error_category():
    records = [scoring_record(evaluation=None) for _ in range(3)]
    for i, category in enumerate(["timeout", "http_error", "timeout", "invalid_verdict"]):
        records.append(scoring_record(identifier=str(i), judgement=RequestJudgement(status="inconclusive", votes=[
            JudgeVote(judge="j", url="http://x", model="m", status="error", error_category=category)])))
    row = scoring_gap(scoring_context(records), "correctness")
    assert row.cause == "data_missing" and "4/7" in row.reason and "timed out" in row.reason
    assert "judging.request_timeout_seconds" in row.action


@pytest.mark.parametrize("check", ["correctness", "json", "tools"])
def test_recorded_scorer_limit_explains_relevant_check_without_model_text(check):
    value = scoring_record(check_ids=[check], evaluation_error="tool_evaluation_budget")
    row = scoring_gap(scoring_context([value, value]), check)
    assert row.cause == "data_missing" and "2/2" in row.reason
    assert "not a wrong answer" in row.reason and "tool expectation" in row.action
    assert "SECRET-" not in str(row)


def test_scoring_facts_reject_unbounded_unknown_strings():
    value = scoring_record(evaluation=EvaluationEvidence(messages=[], scorer="SECRET-SCORER"),
                           evaluation_error="SECRET-LIMIT", score_message="SECRET-OUTPUT")
    facts = scoring_facts(value)
    assert facts["scorer"] is None and facts["evaluation_error"] is None
    assert facts["score_message_code"] is None and "SECRET-" not in json.dumps(facts)



STOP_CASES = [
    (states.STOP_MAX_DURATION, ['max_duration_seconds'], {}, 'maximum duration', '--max-duration'),
    (states.STOP_CANCELLED_BY_USER, [], {}, 'stopped', 'Rerun'),
    (states.STOP_REQUEST_BUDGET, ['max_requests'], {}, 'request budget', '--max-requests'),
    (states.STOP_ERROR_THRESHOLD, [], {}, 'error threshold', '--stop-after-errors'),
    (None, [], {'target_abort_reasons': {'t': 'SECRET-URL'}}, 'stopped responding', 'reachable'),
    (None, [], {'resources': {'memory_limit_reached': True}}, 'memory limit', 'memory limit'),
]


@pytest.mark.parametrize('abort,limits,observations,reason,action', STOP_CASES)
@pytest.mark.parametrize('empty', [True, False])
def test_recorded_run_stop_explains_empty_and_cancelled_windows(abort, limits, observations, reason, action, empty):
    records = [] if empty else [record(status='cancelled', valid=False, termination_reason='run_duration_limit')]
    value = report(records, abort_reason=abort, limits_reached=limits, observations=observations)
    rows = summary(value)['metrics']
    for key in ('request_throughput',) if empty else ('response_latency', 'first_useful_output'):
        row = rows[key]
        assert reason in row['reason']
        assert action in row['action']
        assert 'Fix the failing requests first' not in row['action']
        assert 'more requests' not in row['action']
        assert len(row['reason']) <= 200 and len(row['action']) <= 170
        assert 'SECRET-' not in str(row['reason'])


# This table describes forbidden contradictions from recorded facts, not rule
# implementations: every unavailable row participates as new rules are added.
EVIDENCE_CONTRADICTIONS = [
    ('expected answer recorded', lambda facts: facts['expected'], r'(?is)(?:attach.*expected answer|expected answer.*attach)'),
    ('stop requires own-window evidence', lambda facts: not facts.get('stop_affected', False),
     r'The run (?:was stopped|reached its (?:maximum duration|memory limit)|used its request budget|stopped after reaching its error threshold)|The endpoint stopped responding'),
    ('unfinished stopped run', lambda facts: facts['stopped'], r'(?i)(?:rerun with more requests|Fix the failing requests first)'),
    ('usage rejected', lambda facts: facts['rejected'], r'Check that the endpoint returns usage'),
    ('response retention', lambda facts: facts['retained_none'], r'The model returned no reasoning content'),
    ('private evidence', lambda facts: True, r'SECRET-'),
    ('private URL components', lambda facts: True, r'https?://[^\s/]*@|https?://[^\s]*[?#]'),
    *[(f'generic text {text}', lambda facts: True, re.escape(text)) for text in FORBIDDEN_TEXT],
]


@pytest.mark.parametrize('stop_case', [None, *STOP_CASES[:4]])
@pytest.mark.parametrize('usage', ['not_recorded', 'rejected'])
@pytest.mark.parametrize('scorer', ['none', 'exact', 'judge'])
def test_recorded_evidence_contradiction_table(stop_case, usage, scorer):
    from giraffe.models import EvaluationEvidence, JudgeVote, RequestJudgement
    from giraffe.fixtures import TRUNCATED_ANSWER, REASONING_WITHOUT_ANSWER
    variants = [(None, 'AI judgment pending.'), ('inconclusive', 'AI judge panel tied'),
                ('inconclusive', TRUNCATED_ANSWER), ('inconclusive', REASONING_WITHOUT_ANSWER), ('pass', None)]
    variants += [(category, 'AI judge evaluation incomplete') for category in (
        'timeout', 'cancelled', 'http_error', 'request_failed', 'truncated_response', 'invalid_completion',
        'invalid_verdict', 'phase_deadline', 'not_evaluated', 'client_initialization')]
    for judgement, message in variants:
        evaluation = EvaluationEvidence(messages=[], scorer=scorer, expected='SECRET-ANSWER', expected_provided=scorer != 'none')
        judge = None if judgement is None else RequestJudgement(status='pass' if judgement == 'pass' else 'inconclusive', votes=[
            JudgeVote(judge='j', url='https://SECRET-URL', model='m', status='error', error_category=judgement, reason='SECRET-OUTPUT')])
        stopped = stop_case is not None
        abort, limits, observations = stop_case[:3] if stopped else (None, [], {})
        rows = [record(str(i), check_ids=['correctness'], timing_schema=1, stream=True,
                       status='cancelled' if stopped else 'completed', valid=not stopped,
                       termination_reason='run_duration_limit' if stopped else None, evaluation=evaluation,
                       judgement=judge, score_message=message, output_chars=20,
                       workload={'token_evidence': {'usage': usage}}) for i in range(2)]
        value = report(rows, config={'targets': [{'name': 't'}], 'retention': 'none',
                                    'checks': ['correctness'], 'judging': {'judges': [{'name': 'j'}]}},
                       abort_reason=abort, limits_reached=limits, observations=observations)
        facts = dict(expected=scorer != 'none', stopped=stopped, stop_affected=stopped,
                     rejected=usage == 'rejected', retained_none=True)
        for key, row in summary(value)['metrics'].items():
            if row['value'] is not None:
                continue
            text = ' '.join(str(row.get(field) or '') for field in ('reason', 'action', 'detail'))
            for label, predicate, forbidden in EVIDENCE_CONTRADICTIONS:
                assert not predicate(facts) or not re.search(forbidden, text), (label, key, text)
            assert len(row.get('reason') or '') <= 200
            assert len(re.sub(r'--checks [a-z_,]+\.?', '', row.get('action') or '').strip()) <= 170


@pytest.mark.parametrize('finish,cap', [('stop', False), ('length', True)])
def test_output_cap_guidance_requires_recorded_length_finish(finish, cap):
    value = report([record(first_reasoning_ms=1, first_output_ms=None, finish_reason=finish, stream=True, timing_schema=1)])
    row = summary(value)['metrics']['first_useful_output']
    assert ('--max-output-tokens' in row['action']) is cap


def test_specific_timing_and_baseline_evidence_never_becomes_a_guessed_failure():
    from giraffe.availability import WindowFacts, response_latency
    r = record()
    value = report([r], baseline={'status': 'inconclusive', 'targets': [
        {'target': 't', 'reasons': ['Input workload differs.']}]})
    f = WindowFacts(value, {'kind': 'full', 'target': 't'}, [r], [r], [r], [], 1, [r])
    row = response_latency('response_latency', {}, f)
    assert row.cause == 'unknown' and 'timestamps were recorded' in row.reason
    baseline = summary(value)['metrics']['baseline_timing']
    assert 'Input workload differs' in baseline['reason']
    assert 'recorded' in baseline['action']


def test_first_token_reason_is_bounded_to_recorded_vocabulary():
    value = report([record(timing_schema=1, stream=True, first_token_reason='SECRET-MODEL https://credentials@host')])
    row = summary(value)['metrics']['client_ttft']
    assert 'SECRET-' not in row['reason'] and 'https://' not in row['reason']
    assert 'failed' not in row['reason']


def test_exporter_fallback_preserves_safe_route_and_uses_recorded_status():
    from giraffe.availability import WindowFacts, explain_window
    from giraffe.performance import metric
    r = record()
    value = report([r])
    f = WindowFacts(value, {'kind': 'full', 'target': 't'}, [r], [r], [r], [], 1, [r])
    for status, needle in [('authentication_denied', 'credentials'), ('endpoint_absent', 'metrics_url'),
                           ('response_limit', 'response size')]:
        rows = {'server_probe': metric(label='Probe', unit='', source='', target='t', window='full',
                                      state='not_collected', details={'collection_status': status},
                                      reason='Collection failed at https://host/metrics.')}
        explain_window(rows, {}, f)
        assert needle in rows['server_probe']['action']
        assert rows['server_probe']['reason'] == 'Collection failed at https://host/metrics.'
        assert 'secret' not in rows['server_probe']['reason']



def test_scoring_aggregation_cache_cannot_reintroduce_private_text():
    cached = dict(scorer="SECRET-SCORER", expected=True, judgement="SECRET-VERDICT",
                  judge_errors=["SECRET-ERROR"], judge_votes=999999,
                  score_message_code="SECRET-MESSAGE", evaluation_error="SECRET-OUTPUT")
    r = record(workload={"_scoring": cached})
    facts = scoring_facts(r)
    assert "SECRET-" not in str(facts)
    assert facts["judge_votes"] == 8 and facts["judge_errors"] == ["other"]
    assert "SECRET-" not in str(summary(report([r]))["metrics"])


@pytest.mark.parametrize('module', ['performance', 'availability', 'run_evidence', 'scoring_facts',
                                    'telemetry', 'report_stream', 'states'])
def test_availability_modules_support_cold_import_order(module):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", f"import giraffe.{module}; import giraffe.performance; import giraffe.availability; import giraffe.scoring_facts"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr



def test_missing_saved_window_identity_keys_fall_back_to_cohort():
    value = identity_report()
    for window in value.performance['targets'][0]['windows']:
        old_window = {key: item for key, item in window.items() if key not in {'check_ids', 'cohort_check', 'target'}}
        f = WindowFacts(value, old_window, [], [], [], [], 1, [], cohort=value.requests, dispatched=[])
        assert f.owns('load') and f.streamed and f.timing_versioned


def test_stage_schedule_pattern_wins_over_other_patterns_in_the_cohort():
    value = identity_report()
    value.requests = [record('held', start=0, end=500, check_ids=['load'],
                             load_point={'point_id': 'held', 'source': 'primary', 'pattern': 'concurrent'}),
                      record('arrival', start=2000, end=2500, check_ids=['load'], scheduled_ms=2000,
                             load_point={'point_id': 'arrival', 'source': 'primary', 'pattern': 'constant'})]
    value.observations['targets']['t']['schedule_transitions'] = [
        dict(stage_id='held', kind='concurrency', start_ms=0, end_ms=1000, check_id='load', intended_concurrency=1),
        dict(stage_id='arrival', kind='arrival_rate', start_ms=2000, end_ms=3000, check_id='load', intended_rate_rps=1)]
    value.performance = build_performance(value)
    sending = next(w for w in value.performance['targets'][0]['windows'] if w['id'].startswith('stage:0:'))
    rows = summary(value, window=sending['id'])['metrics']
    for key in ('offered_rate', 'arrival_first_output', 'arrival_completion', 'drops'):
        assert rows[key]['cause'] == 'not_applicable', rows[key]
        assert 'fixed concurrency' in rows[key]['reason']


@pytest.mark.parametrize('url,safe', [
    ('http://localhost:11434/metrics', 'http://localhost:11434/metrics'),
    ('https://user:pw@host:9400/metrics?token=abc#x', 'https://host:9400/metrics[redacted]'),
])
def test_exporter_route_survives_availability_and_privacy_guards(url, safe):
    from giraffe.deployment import redact_url
    expected = f'Metrics endpoint {redact_url(url)} returned HTTP 404; no runtime exporter was available at that route.'
    value = report([record()], observations={'targets': {'t': {'serving_telemetry': {'snapshots': [
        {'status': 'endpoint_absent', 'endpoint': url, 'reason': expected}]}}}})
    row = summary(value)['metrics']['server_ttft']
    assert row['reason'] == f'Metrics endpoint {safe} returned HTTP 404; no runtime exporter was available at that route.'
    for label, _, forbidden in EVIDENCE_CONTRADICTIONS:
        if label in {'private evidence', 'private URL components'}:
            assert not re.search(forbidden, str((row['reason'], row['action'], row['detail'])))


def stopped_stage_report():
    rows = [record('clean', start=0, end=500, check_ids=['load'], scenario='load_c1',
                   stream=True, timing_schema=1, first_output_ms=10, answer_reads=1,
                   load_point={'point_id': 'load_c1', 'source': 'primary', 'pattern': 'concurrent'},
                   workload={'pattern': 'concurrent'}),
            record('stopped', start=1100, end=1500, check_ids=['load'], scenario='load_c1',
                   status='cancelled', valid=False, termination_reason='run_duration_limit',
                   load_point={'point_id': 'load_c1', 'source': 'primary', 'pattern': 'concurrent'})]
    transitions = [dict(stage_id=f's{i}', label=f'C{i+1}', kind='concurrency',
                        start_ms=i*1000, end_ms=(i+1)*1000, intended_concurrency=i+1) for i in range(2)]
    return report(rows, config={'targets': [{'name': 't'}], 'checks': ['load']},
                  observations={'targets': {'t': {'schedule_transitions': transitions}}},
                  abort_reason=states.STOP_MAX_DURATION, limits_reached=['max_duration_seconds'])


def test_stop_attribution_uses_selected_stage_requests_only():
    value = stopped_stage_report()
    clean = value.model_copy(update={'abort_reason': None, 'limits_reached': []})
    for window in value.performance['targets'][0]['windows']:
        if window['kind'] != 'stage':
            continue
        rows = summary(value, window=window['id'])['metrics']
        for label, predicate, forbidden in EVIDENCE_CONTRADICTIONS:
            if label == 'stop requires own-window evidence' and predicate({'stop_affected': window['stage_id'] != 's0'}):
                for row in rows.values():
                    assert not re.search(forbidden, str((row.get('reason'), row.get('action'))))
        if window['stage_id'] == 's0':
            expected = summary(clean, window=window['id'])['metrics']
            for key, row in rows.items():
                assert (row.get('cause'), row.get('reason'), row.get('action')) == (
                    expected[key].get('cause'), expected[key].get('reason'), expected[key].get('action')), key
        else:
            assert 'maximum duration' in rows['response_latency']['reason']
            assert '--max-duration' in rows['response_latency']['action']


@pytest.mark.parametrize('termination', [None, 'external_cancellation', 'intentional_cancellation'])
def test_intentional_cancellation_never_attributes_run_stop(termination):
    from giraffe.run_evidence import cancelled_by_stop
    r = record(check_ids=['cancellation'], scenario='client_cancel', status='cancelled', valid=False,
               intentional=True, termination_reason=termination)
    assert not cancelled_by_stop(r)
    value = report([r], config={'targets': [{'name': 't'}], 'checks': ['cancellation']},
                   abort_reason=states.STOP_REQUEST_BUDGET, limits_reached=['max_requests'])
    for row in summary(value)['metrics'].values():
        assert 'request budget' not in (row.get('reason') or '')


@pytest.mark.parametrize('stop,limits,expected', [
    (states.STOP_MAX_DURATION, ['max_duration_seconds'], False),
    (states.STOP_REQUEST_BUDGET, ['max_requests'], False),
    (states.STOP_ERROR_THRESHOLD, [], False),
    (states.STOP_CANCELLED_BY_USER, [], True),
])
@pytest.mark.parametrize('mixed', [False, True])
def test_judging_stop_attribution_respects_phase(stop, limits, expected, mixed):
    r = scoring_record(judgement=None, score_message='AI judgment pending.')
    rows = [r]
    if mixed:
        rows.append(record('cancelled', status='cancelled', valid=False, termination_reason='run_duration_limit',
                           check_ids=['correctness']))
    value = report(rows, config={'targets': [{'name': 't'}], 'checks': ['correctness']},
                   abort_reason=stop, limits_reached=limits)
    row = summary(value)['metrics']['correctness']
    assert ('The run was stopped' in row['reason']) is expected
    assert '--max-duration' not in row['action'] and '--max-requests' not in row['action']
    if not expected:
        assert 'judging.max_duration_seconds' in row['action']
        assert 'judging was pending or cancelled' in row['reason']


def test_target_abort_does_not_affect_other_target_windows():
    value = report([record('a', target='A', status='cancelled', valid=False,
                           termination_reason='endpoint_unresponsive'), record('b', target='B')],
                   config={'targets': [{'name': 'A'}, {'name': 'B'}]},
                   observations={'target_abort_reasons': {'A': 'private endpoint error'}})
    for index, target in enumerate(value.performance['targets']):
        rows = summary(value, target=index)['metrics']
        if target['target'] == 'A':
            assert 'stopped responding' in rows['response_latency']['reason']
        else:
            assert all('stopped responding' not in (row.get('reason') or '') for row in rows.values())


@pytest.mark.parametrize('start,affected', [(0, False), (500, False), (2000, True), (None, True)])
def test_empty_window_stop_requires_unreached_schedule(start, affected):
    value = report([], abort_reason=states.STOP_MAX_DURATION, limits_reached=['max_duration_seconds'],
                   observations={'inference_duration_seconds': 1})
    f = WindowFacts(value, {'kind': 'stage', 'target': 't', 'start_ms': start, 'end_ms': 3000},
                    [], [], [], [], 1, [], cohort=[], dispatched=[])
    assert f.stop_affected is affected


def test_recorded_judge_deadline_outweighs_inference_budget():
    value = report([scoring_record(judgement=None, score_message='AI judgment pending.')],
                   config={'targets': [{'name': 't'}], 'checks': ['correctness']},
                   abort_reason=states.STOP_MAX_DURATION, limits_reached=['max_duration_seconds'],
                   observations={'judging': {'stop_reason': 'phase_deadline'}})
    row = summary(value)['metrics']['correctness']
    assert 'judge phase reached its duration limit' in row['reason']
    assert row['action'] == 'Raise judging.max_duration_seconds, then rerun.'


def test_memory_stop_ends_judging_despite_completed_request_budget():
    value = report([scoring_record(judgement=None, score_message='AI judgment pending.')],
                   config={'targets': [{'name': 't'}], 'checks': ['correctness']},
                   abort_reason=states.STOP_REQUEST_BUDGET, limits_reached=['max_requests'],
                   observations={'resources': {'memory_limit_reached': True}})
    row = summary(value)['metrics']['correctness']
    assert 'memory limit' in row['reason'] and 'memory limit' in row['action']
    assert '--max-requests' not in row['action']


def test_completed_reply_without_completion_timestamp_is_not_stop_evidence():
    r = record(completed_ms=None)
    value = report([r], abort_reason=states.STOP_MAX_DURATION, limits_reached=['max_duration_seconds'])
    f = WindowFacts(value, {'target': 't', 'kind': 'full'}, [r], [r], [], [], None, [], dispatched=[r])
    assert not f.stop_affected
    assert 'maximum duration' not in summary(value)['metrics']['client_ttft']['reason']


def test_unreached_target_stage_uses_target_stop_time_not_other_targets_duration():
    a = record('a', target='A', start=0, end=1000, status='cancelled', valid=False,
               termination_reason='endpoint_unresponsive')
    b = record('b', target='B', start=0, end=4000)
    value = report([a, b], config={'targets': [{'name': 'A'}, {'name': 'B'}]},
                   observations={'target_abort_reasons': {'A': 'private failure'}, 'inference_duration_seconds': 4})
    f = WindowFacts(value, {'target': 'A', 'kind': 'stage', 'start_ms': 2000, 'end_ms': 3000},
                    [], [], [], [], 1, [], cohort=[a], dispatched=[])
    assert f.stop_affected
    # A completed target is unaffected even when the run also has a duration flag.
    value.limits_reached = ['max_duration_seconds']
    f = WindowFacts(value, {'target': 'B', 'kind': 'full', 'start_ms': 0, 'end_ms': 4000},
                    [b], [b], [b], [], 4, [b], cohort=[b], dispatched=[b])
    assert not f.stop_affected


@pytest.mark.parametrize('termination', [None, 'external_cancellation', 'workload_cancelled', 'unknown'])
def test_unattributed_cancellation_does_not_prove_run_stop(termination):
    from giraffe.run_evidence import cancelled_by_stop
    r = record(status='cancelled', valid=False, termination_reason=termination)
    assert not cancelled_by_stop(r)
    value = report([r], abort_reason=states.STOP_REQUEST_BUDGET, limits_reached=['max_requests'])
    row = summary(value)['metrics']['response_latency']
    assert 'request budget' not in row['reason']
