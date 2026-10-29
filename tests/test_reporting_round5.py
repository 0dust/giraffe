"""Transport evidence must not describe a missing response as an empty answer."""
import pytest

from giraffe.models import EvaluationEvidence
from giraffe.reporting import _render
from tests.test_reporting import make_report


@pytest.mark.parametrize('status', ['failed', 'timeout', 'completed'])
def test_received_answer_distinguishes_transport_errors_from_empty_responses(status):
    report = make_report(count=1)
    report.overall = report.checks[0].status = 'fail'
    report.manifest['config']['retention'] = 'none'
    record = report.requests[0]
    record.output, record.output_chars, record.valid, record.score = '', 0, False, False
    record.status = status
    record.error = None if status == 'completed' else 'Endpoint <unreachable>'
    record.evaluation = EvaluationEvidence(messages=[dict(role='user', content='What is the answer?')],
                                           scorer='exact', expected='42', expected_provided=True)
    html = _render(report)
    if status == 'completed':
        assert 'Empty response' in html and 'No response received' not in html
    else:
        assert 'No response received: Endpoint &lt;unreachable&gt;' in html
        assert 'Empty response' not in html
        assert 'Endpoint <unreachable>' not in html


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('status', ['fail', 'pass'])
def test_capacity_request_order_reads_stage_ids_and_old_aggregate_ids(legacy, status):
    from tests.test_traffic_reporting import traffic_report

    report = traffic_report(high_failure='latency')
    check = report.checks[0]
    check.status = report.overall = status
    check.evidence_ids = [record.id for record in report.requests]
    over, accepted = report.requests[-1], report.requests[0]
    if legacy:
        check.metrics['violations'] = [over.id]
    else:
        check.metrics['traffic_stages'][-1]['violations'] = [over.id]
    html = _render(report)
    first, second = (over, accepted) if status == 'fail' else (accepted, over)
    assert html.index(f'<p>Request {first.id} ') < html.index(f'<p>Request {second.id} ')
