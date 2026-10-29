"""Portable HTML and the UI use the same human condition labels."""

import json
import re
from html import unescape

import pytest

from giraffe.conditions import condition_for
from giraffe.reporting import _baseline_table_html, _render
from tests.test_reporting import make_report
from tests.test_ux_results import run_render_checks


def assert_rendered_comparison_label(target, expected, *, conditions=()):
    run_render_checks(
        "const target=" + json.dumps(target) + ";const expected=" + json.dumps(expected)
        + ";const conditions=" + json.dumps(conditions) + ";" + r"""
const value=report([],{conditions,baseline:{targets:[target]}});
$('#main').innerHTML=renderComparison(value);
const heading=$('.comparison-group-heading');
assert(heading);
assert(heading.textContent.endsWith(' · '+expected),heading.textContent);
if(expected.includes('<')){
 assert(heading.innerHTML.includes('&lt;'));
 assert.equal(heading.querySelector('condition'),null);
}
""", dom=True,
    )


@pytest.mark.parametrize(
    "scenario,label",
    [
        ("correctness_c2", "Answer correctness"),
        ("initial", "First request"),
        ("warm", "Warm-up"),
    ],
)
def test_html_comparison_group_header_matches_ui_condition_label(scenario, label):
    target = {
        "target": "local",
        "comparisons": [
            {
                "scenario": scenario,
                "metric": "latency_ms",
                "status": "pass",
                "baseline": 10,
                "current": 10,
            }
        ],
    }
    html = _baseline_table_html({"targets": [target]})
    header = unescape(
        re.search(r'colspan="5" scope="rowgroup">(.*?)</th>', html).group(1)
    ).removeprefix("local · ")
    assert header == label
    condition = condition_for(scenario)
    assert_rendered_comparison_label(
        target, header, conditions=[{"target": "local", "condition": condition}],
    )


def test_html_measurements_findings_and_request_summaries_do_not_use_raw_condition_ids():
    report = make_report(count=1)
    record = report.requests[0]
    record.scenario = "correctness_c2"
    record.condition_id = "correctness_c2"
    record.score = False
    report.checks[0].status = "fail"
    report.checks[0].evidence_ids = [record.id]
    from giraffe.models import EvaluationEvidence

    record.evaluation = EvaluationEvidence(
        messages=[], scorer="exact", expected="43", expected_provided=True
    )
    html = _render(report)
    for section in ["findings", "measurements", "requests"]:
        content = re.search(
            r'<section id="' + section + r'"[^>]*>(.*?)</section>', html, re.S
        ).group(1)
        visible = unescape(
            re.sub(r"<[^>]+>", "", re.sub(r"<pre>.*?</pre>", "", content, flags=re.S))
        )
        assert "correctness_c2" not in visible
        assert "Answer correctness" in visible
        assert "Answer correctness · Answer correctness" not in visible


def test_python_catalogue_change_is_shared_with_ui_without_a_js_mapping(monkeypatch):
    from giraffe.conditions import _SCENARIOS

    monkeypatch.setitem(_SCENARIOS, "initial", ("Shared Python label", "access"))
    html = _baseline_table_html(
        {
            "targets": [
                {
                    "target": "local",
                    "comparisons": [{"scenario": "initial", "metric": "latency_ms"}],
                }
            ]
        }
    )
    assert "local · Shared Python label" in html
    run_render_checks("assert.equal(phaseName('initial'),'Shared Python label');")


@pytest.mark.parametrize(
    "scenario,label",
    [
        ("correctness_c2", "Answer correctness"),
        ("load_primary_combinations_short_long_concurrent_c4", "Short input / long output"),
        ("load_primary_interference_c4.baseline", "Short answers alone"),
        (
            "mixed_heavy_long_input_before_output",
            "Long input interference · before output · heavy partner",
        ),
        ("session_live_turn1", "Live sessions · turn 2"),
        ("consistency_c4_short_p2", "Short input consistency · prompt 3"),
    ],
)
def test_html_comparison_fallback_matches_ui_without_saved_condition_metadata(scenario, label):
    target = {"target": "local", "comparisons": [{"scenario": scenario, "metric": "latency_ms"}]}
    html = _baseline_table_html({"targets": [target]})
    assert f"local · {label}</th>" in html
    assert_rendered_comparison_label(target, label)


def test_html_comparison_preserves_custom_condition_label_and_escapes_it():
    report = make_report(count=1)
    report.conditions = [
        {"target": "other", "condition": {"id": "custom_c2", "label": "Another target"}},
        {
            "target": "local",
            "condition": {"id": "custom_c2", "label": "Custom <condition> · 2 at once"},
        }
    ]
    target = {"target": "local", "comparisons": [{"scenario": "custom_c2", "metric": "latency_ms"}]}
    html = _baseline_table_html({"targets": [target]}, report)
    assert "local · Custom &lt;condition&gt;</th>" in html
    assert_rendered_comparison_label(target, "Custom <condition>", conditions=report.conditions)
