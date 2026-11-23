"""Imported reverse interference keeps its meaning in forms and comparisons."""

import json
import pytest

from giraffe.models import RunConfig
from giraffe.reporting import _comparison_load_label, _settings_reasons, compare_baseline
from tests.test_concurrent_load_ui_review import FORM
from tests.test_load_test_baseline import concurrent_report
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def test_imported_short_during_long_survives_form_edits_with_correct_instructions():
    config = RunConfig(targets=[{"name": "local", "url": "http://unused.test/v1", "model": "test"}],
        checks=["load"], load_test={"workloads": {"combinations": {"enabled": False},
            "interference": {"enabled": True, "mode": "short_during_long"}},
            "patterns": {"concurrent": {"levels": [2]}}})
    browser_logic(FORM + r'''
const html=renderLoadEditor(config);
assert.match(html,/Short request during an active long answer/);
assert.match(html,/When to send the short request/);
assert.match(html,/After the long answers start streaming/);
assert.match(html,/After five streamed chunks from each long answer/);
assert.doesNotMatch(html,/Repeat the same short workload\. While the short answers are still streaming/);
form.elements.load_workloads_interference_trigger.value='after_five_chunks';
const read=readForm(false).config;
assert.equal(read.load_test.workloads.interference.mode,'short_during_long');
assert.equal(read.load_test.workloads.interference.trigger,'after_five_chunks');
assert.match(loadDisplay(loadSettings(read)).allocation,/long answers/);
assert.match(renderLoadPlan(read),/Short request during an active long answer/);
form.elements.load_patterns_concurrent_levels.value='1';
assert(currentSettingErrors().some(issue=>issue.msg==='The short-request / long-answer test needs a concurrency level of at least 2.'));
''', config)


def test_short_during_long_results_show_first_answer_comparison_and_actual_mode():
    run_render_checks(r'''
const point={id:'load_primary_interference_c2',type:'interference',pattern:'interference',level:2,
 interference_mode:'short_during_long',interference:{mode:'short_during_long',
 baseline:{matched_count:2,first_answer_median_ms:12,completion_median_ms:20},
 probe:{matched_count:2,first_answer_median_ms:36,completion_median_ms:40},
 first_answer_slowdown:3,slowdown:2,triggered:2}};
const html=renderLoadTestResults({points:[point]});
assert.match(html,/Short request during an active long answer/);
assert.match(html,/First-answer median/);
assert.match(html,/First-answer slowdown: 3×/);
assert.match(html,/During a long answer/);
assert.doesNotMatch(html,/Long input during an active short stream/);
''')


def test_historical_omitted_default_mode_keeps_load_settings_comparable():
    current = concurrent_report()
    previous = current.model_copy(deep=True)
    previous.manifest["config"]["load_test"]["workloads"]["interference"].pop("mode")
    previous.manifest["effective_checks"]["load"]["workloads"]["interference"].pop("mode")
    before = previous.model_dump_json()

    assert _settings_reasons(current, previous) == []
    assert previous.model_dump_json() == before


def test_recorded_interference_mode_difference_blocks_stale_manifest_comparison():
    previous = concurrent_report()
    previous.checks[0].metrics["highest_accepted"] = []
    for point in previous.checks[0].metrics["points"]:
        point.update(type="interference", workload="interference", pattern="interference")
    for record in previous.requests:
        record.load_point.update(type="interference", workload="interference", pattern="interference",
            phase="probe", role="short_stream")
    current = previous.model_copy(deep=True)
    for record in current.requests:
        record.load_point["interference_mode"] = "short_during_long"

    target = compare_baseline(current, previous).baseline["targets"][0]

    assert target["status"] == "inconclusive"
    assert target["load_exclusions"]
    assert all("interference modes differ" in row["reason"].lower()
               for row in target["load_exclusions"])
    assert all(row["load_point"]["interference_mode"] == "short_during_long"
               for row in target["load_exclusions"])


@pytest.mark.parametrize(("role", "phase", "label"), [
    ("short_stream", "probe", "During a long answer"),
    ("short_stream", "baseline", "Short-only control"),
    ("long_stream", "probe", "Long-answer background"),
])
def test_short_during_long_comparison_labels_follow_recorded_role(role, phase, label):
    point = {"type": "interference", "workload": "interference", "pattern": "interference",
        "interference_mode": "short_during_long", "role": role, "phase": phase, "level": 2}
    text = _comparison_load_label(point)
    assert "Short request during an active long answer" in text
    assert label in text
    run_render_checks("const point=" + json.dumps(point) + ",label=" + json.dumps(label) + r''';
const text=comparisonLoadLabel(point);
assert.match(text,/Short request during an active long answer/);
assert(text.includes(label),text);
assert.doesNotMatch(text,/Long-input probe|During long-input probe/);
''')
