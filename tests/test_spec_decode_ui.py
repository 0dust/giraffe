"""The live browser renders the same server evidence as exported reports."""
import json

from tests.test_performance import summary
from tests.test_spec_decode_reporting import saved_report
from tests.test_ux_results import run_render_checks


def test_live_server_table_discloses_partial_coverage_and_position_denominator():
    rows = summary(saved_report(partial=True))["sections"]["server"]
    run_render_checks('const rows=' + json.dumps(rows) + r''';
const markup=performanceMetricTable(rows);
document.querySelector('#main').innerHTML=markup;
const text=$('#main').textContent;
assert.match(text,/Speculative decoding acceptance rate/);
assert.match(text,/84\.2.*%/);
assert.match(text,/1 \/ 2.*intervals/);
assert.match(text,/2–12 s/);
assert.match(text,/partial coverage/i);
assert.match(text,/selected request window/);
assert.match(text,/Accepted speculative tokens by position/);
assert.match(text,/all drafts/);
assert.match(text,/zero-based/);
assert.match(text,/96\.89.*%/);
''', dom=True)
