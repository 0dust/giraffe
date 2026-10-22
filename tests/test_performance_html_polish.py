"""Readable, bounded standalone charts retain the app's presentation semantics."""
from giraffe.performance_html import _buckets, _chart, _fmt


def test_metric_formatting_is_concise_and_preserves_small_observations():
    assert _fmt(5569.554) == '5,569.55'
    assert _fmt(121.553) == '121.55'
    assert _fmt(.0001234) == '0.00012'
    assert _fmt(1000) == '1,000'


def test_chart_labels_are_css_pixel_overlays_and_legend_precedes_plot():
    html = _chart('Tokens', [('Input', [(1, 2), (1000, 200)])], ylabel='tokens')
    assert 'class="html-chart-frame"' in html
    assert 'class="html-axis-label' in html
    assert html.index('class="html-legend"') < html.index('<svg')
    assert '<text' not in html


def test_bucket_caption_is_plain_language_with_rule_in_disclosure():
    html = _buckets({'width_s': 5, 'rule': 'Aim for 10–20 buckets',
                     'boundary_rule': 'half-open intervals',
                     'partial_bucket_disclosure': 'Final bucket uses its actual duration.',
                     'final_partial_bucket': True,
                     'buckets': [{'start_s': 0, 'end_s': 2.39, 'duration_s': 2.39,
                                  'count': 4, 'partial': True}]})
    assert '5-second buckets; the final bucket uses its actual duration (2.39 s).' in html
    assert '<details' in html
    assert html.index('Aim for 10–20 buckets') > html.index('<details')


def test_only_intended_transitions_have_bounded_markers():
    rows = [(i, i % 8) for i in range(1000)]
    html = _chart('Concurrency', [('Actual', rows), ('Intended', rows)], step=True)
    assert 0 < html.count('class="html-transition"') <= 40
    assert html.count('<circle') == html.count('class="html-transition"')
