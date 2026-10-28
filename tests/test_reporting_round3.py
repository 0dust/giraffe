"""Round-three report render regressions; execution and retention stay unchanged."""
import pytest

from giraffe.models import EvaluationEvidence
from giraffe.reporting import _render
from tests.test_reporting import make_report


@pytest.mark.parametrize('before,after,verdict,delta', [(1, 4, 'Better', '+300.0%'), (4, 1, 'Worse', '-75.0%')])
def test_capacity_comparison_direction_and_delta_in_html(before, after, verdict, delta):
    report = make_report(count=1)
    report.baseline = dict(run_id='old', targets=[dict(target='local', traffic_capacity=dict(
        status='pass' if after > before else 'fail',
        baseline_highest_acceptable_rate_rps=before, current_highest_acceptable_rate_rps=after,
        delta_rps=after-before, change_percent=(after-before)/before*100, reason='Matched complete stages.'))])
    html = _render(report)
    assert f'>{verdict}</strong>' in html
    assert delta in html
    assert f'1 {verdict.lower()}' in html


def test_html_distinguishes_unsaved_truncated_and_empty_responses():
    report = make_report(count=1)
    record = report.requests[0]
    report.overall = report.checks[0].status = 'fail'
    record.score, record.output, record.output_chars = False, '', 12
    record.evaluation = EvaluationEvidence(messages=[dict(role='user', content='What is correct?')],
                                           scorer='exact', expected='correct', expected_provided=True)
    report.manifest['config']['retention'] = 'none'
    html = _render(report)
    assert 'Response not saved (retention: none)' in html
    report.manifest['config']['retention'] = 'all'
    report.manifest['storage'] = dict(response_bodies_omitted=1)
    assert 'Response not saved (storage limit)' in _render(report)
    record.output = 'part'
    html = _render(report)
    assert 'Response truncated' in html and 'part' in html
    record.output, record.output_chars, record.valid = '', 0, False
    record.evaluation = EvaluationEvidence(messages=[dict(role='user', content='Check the weather.')], scorer='tool')
    assert 'Response evidence unavailable (storage limit)' in _render(report)
    report.manifest.pop('storage')
    record.output, record.output_chars = '', 0
    record.tool_calls = []
    html = _render(report)
    assert 'Empty response' in html
    assert 'Response not saved' not in html and 'No retained response body' not in html


@pytest.mark.parametrize('metric', ['throughput', 'output_tokens_per_second', 'generation_tokens_per_second'])
@pytest.mark.parametrize('before,after,verdict', [(1, 4, 'Better'), (4, 1, 'Worse')])
def test_sampled_higher_is_better_metrics_keep_backend_direction(metric, before, after, verdict):
    from giraffe.reporting import _baseline_table_html

    improves = after > before
    row = dict(metric=metric, baseline=before, current=after,
               status='pass' if improves else 'fail', worsening=-300 if improves else 75,
               worsening_interval_95=[-310, -290] if improves else [70, 80], threshold=20)
    html = _baseline_table_html(dict(targets=[dict(target='local', comparisons=[row])]))
    assert f'>{verdict}</strong>' in html
    assert ('+300.0%' if improves else '-75.0%') in html


def test_html_tool_answer_dropped_by_retention_does_not_become_empty(tmp_path):
    from giraffe.models import RunReport
    from giraffe.reporting import write_report

    report = make_report(count=1)
    record = report.requests[0]
    report.overall = report.checks[0].status = 'fail'
    report.manifest['config']['retention'] = 'none'
    record.score, record.output, record.output_chars = False, '', 0
    record.evaluation = EvaluationEvidence(messages=[dict(role='user', content='Check the weather.')], scorer='tool')
    record.tool_calls = [dict(type='function', function=dict(name='weather', arguments='{}'))]
    json_path, html_path = write_report(report, tmp_path, html=True)
    saved = RunReport.model_validate_json(json_path.read_text())
    assert saved.requests[0].tool_calls == []
    html = html_path.read_text()
    assert '<span class="muted">Response not saved (retention: none)</span>' in html
    assert 'Received: Empty response' not in html


def test_invalid_zero_character_response_stays_empty_with_none_retention():
    report = make_report(count=1)
    report.manifest['config']['retention'] = 'none'
    report.overall = report.checks[0].status = 'fail'
    record = report.requests[0]
    record.output, record.output_chars, record.valid, record.score = '', 0, False, False
    record.evaluation = EvaluationEvidence(messages=[dict(role='user', content='Answer the question.')],
                                           scorer='exact', expected='42', expected_provided=True)
    html = _render(report)
    assert '<span class="muted">Empty response</span>' in html
    assert 'Response not saved' not in html
