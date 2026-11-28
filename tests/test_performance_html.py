"""Standalone report shares aggregation semantics without external assets."""
import re

from giraffe.models import RunReport
from giraffe.performance import build_performance
from giraffe.reporting import _render
from tests.performance_fixture import synthetic_report_1000


def test_standalone_findings_first_order_and_complete_sections():
    report = synthetic_report_1000()
    before = report.model_dump_json()
    html = _render(report)
    assert 'id="html-tab-performance"' not in html
    assert 'id="html-results"' in html
    sections = ['id="findings"', 'id="checks"', 'id="baseline"',
                'id="html-performance"', 'id="requests"', 'id="reproduction"']
    assert [html.index(section) for section in sections] == sorted(html.index(section) for section in sections)
    assert html.count('>Baseline comparison</h2>') == 1
    assert 'Entry 1' not in html
    assert '@media(prefers-color-scheme:light)' in html
    assert '@media print' in html
    assert 'details>*:not(summary){display:block!important}' in html
    assert 'details::details-content{content-visibility:visible!important;height:auto!important}' in html
    headings = ['Performance overview', '02 Request timing', '03 Throughput &amp; capacity',
                '04 Load delivery', '05 Outcomes &amp; quality', '06 Workload &amp; tokens',
                '07 Conversations &amp; workload effects', '08 Server observations']
    positions = [html.index(f'>{heading}</h2>') for heading in headings]
    assert positions == sorted(positions)
    assert 'All timing percentiles &amp; definitions' in html
    assert 'Token accounting · selected window' in html
    assert 'Linear interpolation between closest ranks' in html
    assert 'partial bucket' in html.lower()
    assert 'SYNTHETIC DATA' in html
    assert '<script' not in html and '<link' not in html
    assert report.model_dump_json() == before


def test_standalone_chart_decimation_preserves_extrema_and_bounds_geometry():
    from giraffe.performance_html import decimate

    points = [(n, 10000 if n == 333 else -100 if n == 777 else n % 11) for n in range(1000)]
    drawn = decimate(points, buckets=60)
    assert points[0] == drawn[0] and points[-1] == drawn[-1]
    assert points[333] in drawn and points[777] in drawn
    assert len(drawn) <= 122
    html = _render(synthetic_report_1000())
    # Request series have no dense markers; only bounded intended-stage changes
    # are marked, so a 1,000-request report never has 1,000 circles.
    assert html.count('<circle') == html.count('class="html-transition"') <= 40
    assert html.count('<svg') <= 5
    assert html.count('<path') <= 14
    assert html.count('<rect') < 100
    assert 'every request included in metrics' in html
    assert 'class="html-throughput"' in html
    assert '.html-throughput{display:grid;grid-template-columns:' in html
    assert '.html-throughput{grid-template-columns:1fr}' in html


def test_standalone_old_empty_reports_show_availability_without_empty_charts():
    report = RunReport(run_id='old', started_at='2026-01-01', finished_at='2026-01-01',
                       overall='inconclusive', manifest={'config': {'targets': [{'name': 'old'}]}})
    html = _render(report)
    assert 'No request timing observations' in html
    assert 'No completed-request bucket observations' in html
    assert 'No request lifetime observations' in html
    assert 'Not collected' in html and 'Not applicable' in html
    assert 'no runtime telemetry collection evidence' in html
    assert '<svg' not in html
    assert 'Request evidence' in html and 'Reproduce this run' in html


def test_standalone_values_come_from_selected_backend_summary():
    from giraffe.performance_html import render_performance

    performance = build_performance(synthetic_report_1000())
    target = performance['targets'][0]
    summary = target['summaries'][target['selected_window']]
    summary['metrics']['request_throughput']['value'] = 9876.5
    summary['metrics']['first_useful_output'].update(value=None, state='not_collected',
                                                    reason='CANARY unavailable', action='Collect CANARY')
    html = render_performance(performance)
    assert '9,876.5' in html
    assert 'CANARY unavailable' in html and 'Collect CANARY' in html
    assert 'aria-label=' in html and '<desc>' in html
    assert 'Median · P95' in html


def test_standalone_all_thousand_requests_remain_bounded_and_reported():
    report = synthetic_report_1000()
    for record in report.requests:
        record.condition_id = 'one-cohort'
    html = _render(report)
    assert '1,000 requests' in html
    assert 'Response latency: 1000 observations' in html
    paths = re.findall(r'<path d="([^"]+)"', html)
    assert len(paths) <= 14
    assert max(map(len, paths)) < 5000
    assert 'Measurement windows · static report' in html
    assert 'Other windows are selectable in the app' in html


def test_standalone_zero_completions_disclose_recorded_zero_without_chart():
    from giraffe.performance_html import render_performance

    performance = build_performance(synthetic_report_1000())
    target = performance['targets'][0]
    summary = target['summaries'][target['selected_window']]
    for bucket in summary['completion_buckets']['buckets']:
        bucket['count'] = 0
    summary['actual_concurrency'] = {'points': [{'elapsed_s': 0, 'in_flight': 0},
                                               {'elapsed_s': 1, 'in_flight': 0}], 'sample_count': 0}
    summary['intended_concurrency'] = []
    html = render_performance(performance)
    assert 'No completed requests in this selected window; recorded bucket counts are zero.' in html
    assert 'No request lifetime observations' in html
    assert 'request lifetimes were not collected' not in html
    assert html.count('<svg') == 3


def test_standalone_targets_are_separate_and_content_is_escaped():
    from tests.test_reporting import make_report

    report = make_report(count=1)
    report.manifest['config']['targets'].append({'name': 'other', 'model': '<model>',
                                                'url': 'https://other.invalid/?x=<script>'})
    report.requests.extend(report.requests[0].model_copy(update={'target': 'other', 'id': f'other-{i}'})
                           for i in range(2))
    html = _render(report)
    assert html.count('>Performance overview</h2>') == 2
    assert '<strong>local</strong> · Full request window · — s · 1 requests' in html
    assert '<strong>other</strong> · Full request window · — s · 2 requests' in html
    assert '&lt;model&gt;' in html and '&lt;script&gt;' in html
    assert '<script' not in html


def test_standalone_repeated_input_preview_links_to_preserved_evidence():
    from giraffe.models import EvaluationEvidence
    from tests.test_reporting import make_report

    report = make_report(count=2)
    for record in report.requests:
        record.evaluation = EvaluationEvidence(messages=[{'role': 'user', 'content': '<shared-input>'}],
                                               scorer='exact', expected='42', expected_provided=True)
    html = _render(report)
    # The full input is rendered once; each request summary shows its prompt label.
    assert html.count('<h3>Input messages</h3><pre>') == 1
    assert html.count('<summary>&lt;shared-input&gt; · local · Warm-up') == 2
    assert html.count('&lt;shared-input&gt;') == 3
    assert 'Same recorded input as <a href="#request-0">earlier request evidence</a>' in html
    assert '<details id="request-1">' in html


def test_standalone_without_target_is_honest_and_not_blank():
    report = RunReport(run_id='empty', started_at='2026-01-01', finished_at='2026-01-01', manifest={}, overall='fail')
    html = _render(report)
    assert 'No target or request measurements were recorded' in html
    assert '<svg' not in html
    assert 'id="html-results"' in html


def test_standalone_measurement_catalog_preview_is_bounded():
    from giraffe.performance_html import render_performance

    performance = build_performance(synthetic_report_1000())
    target = performance['targets'][0]
    target['windows'] = [dict(target['windows'][0], label=f'window-canary-{n}', selection_label=f'window-canary-{n}') for n in range(1000)]
    html = render_performance(performance)
    assert 'Showing 50 of 1000 windows; complete catalog in report.json.' in html
    assert 'window-canary-49' in html and 'window-canary-50' not in html


def test_unavailable_card_next_step_is_visible_and_detail_stays_in_info():
    from giraffe.performance_html import _card

    row = {'value': None, 'label': 'Output token throughput', 'state': 'insufficient_evidence',
           'reason': 'The endpoint returned no token counts.', 'action': 'Check usage.completion_tokens.',
           'detail': 'Long <evidence> belongs in the info popover.', 'source': 'Recorded reply usage'}
    html = _card(row)
    info = re.search(r'<details class="html-info">.*?</details>', html).group()
    visible = html.replace(info, '')
    assert row['reason'] in visible
    assert '<span class="html-next html-note"><br>' + row['action'] + '</span>' in visible
    assert row['action'] not in info
    assert 'Long &lt;evidence&gt;' in info
    assert 'Long &lt;evidence&gt;' not in visible


def test_standalone_unavailable_evidence_note_has_no_generic_instruction():
    html = _render(synthetic_report_1000())
    assert 'Collect eligible observations' not in html
    assert 'Absent evidence stays unavailable; each unavailable value states why and what to change.' in html


def test_window_precondition_is_once_per_table_but_retained_on_cards():
    from giraffe.performance_html import _card, _distributions, _table

    availability = {'cause': 'data_missing', 'reason': 'WINDOW requests failed.',
                    'action': 'Fix WINDOW requests first.'}
    row = dict(availability, value=None, state='not_collected', label='First answer',
               unit='ms', source='Recorded timing', sample_count=0)
    other = dict(row, label='Other', cause='check_not_selected', action='Add another check.')
    for html in (_table([row, dict(row, label='Second'), other], availability),
                 _distributions([row, dict(row, label='Second'), other], 'Ranks', availability)):
        assert html.count(availability['action']) == 1
        assert html.count(availability['reason']) == 2  # The different explanation is not hidden.
        assert html.count('class="html-note"') >= 1
        table = html[html.index('<table'):]
        assert availability['action'] not in table
        assert 'Add another check.' in table
        assert html.index(availability['action']) < html.index('<table')
    card = _card(row)
    assert availability['reason'] in card and availability['action'] in card


def test_all_failed_window_note_covers_cards_and_tables_once_per_section():
    from html import escape
    from giraffe.performance import refresh_performance
    from giraffe.performance_html import render_performance
    from tests.test_reporting import dead_endpoint_report

    report = refresh_performance(dead_endpoint_report())
    performance = report.performance
    before = report.model_dump_json()
    target = performance['targets'][0]
    summary = target['summaries'][target['default_window']]
    common = summary['availability']
    html = render_performance(performance)
    overview = re.search(r'<section class="html-section" id="[^"]+-overview">.*?</section>', html).group()
    assert overview.count(escape(common['reason'])) == 1
    assert overview.count(escape(common['action'])) == 1
    assert overview.index(escape(common['reason'])) < overview.index('class="html-cards')
    for section in re.findall(r'<section class="html-section".*?</section>', html):
        assert section.count(escape(common['reason'])) <= 1
        assert section.count(escape(common['action'])) <= 1
    matched = 0
    for card in re.findall(r'<article class="html-kpi">.*?</article>', html):
        label = re.search(r'<h3>(.*?)<details', card).group(1)
        row = next(row for row in summary['metrics'].values() if escape(row['label']) == label)
        if row.get('value') is None and all(row.get(field) == common[field] for field in common):
            matched += 1
            assert escape(common['reason']) not in card and escape(common['action']) not in card
            assert '<strong>Insufficient evidence</strong>' in card
            assert escape(row['source']) in card
            assert f"{row['sample_count']}/{row['eligible_count']}" in card
        elif row['id'] == 'goodput':
            assert escape(row['reason']) in card and escape(row['action']) in card
    assert matched > 1
    assert report.model_dump_json() == before
