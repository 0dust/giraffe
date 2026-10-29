"""The reordered report preserves main's deployment and telemetry evidence."""

import json

import pytest

from tests.test_reporting_merged_features import deployment_comparison, gpu_series_limited_report
from tests.test_spec_decode_reporting import saved_report
from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize("confounded", [False, True])
def test_baseline_explains_deployment_changes_outside_diagnostics(confounded):
    value = deployment_comparison(confounded=confounded).model_dump(mode='json')
    run_render_checks('const value=' + json.dumps(value) + ';const confounded=' + json.dumps(confounded) + r''';
detail(value);
$('#report-baseline-reference').open=true;
const section=$('#baseline-content .comparison-section');
const summary=section.querySelector('.deployment-change');
assert(summary);assert(!summary.closest('details.comparison-support'));
assert.match(summary.textContent,/Deployment change: extension.requested_speculative_config, serving.launch_arguments/);
if(confounded){
 assert.match(section.textContent,/Confounded: multiple unrelated changes/);
 assert.doesNotMatch(summary.textContent,/one intended change/);
}else{
 assert.match(summary.textContent,/\(one intended change\)/);
 assert.doesNotMatch(section.querySelector('.deployment-change').textContent,/Confounded/);
}
const caveat=section.querySelectorAll('p').find(node=>/not evidence of causality/.test(node.textContent));
assert(caveat,'Deployment interpretation needs a visible causality caveat');
assert(!caveat.closest('details.comparison-support'));
for(let node=caveat;node&&node!==section.parentNode;node=node.parentNode)assert(!node.hidden);
''', dom=True)


def test_speculative_metrics_remain_in_active_performance_panel_after_checks():
    value = saved_report(partial=True).model_dump(mode='json')
    run_render_checks('const value=' + json.dumps(value) + r''';
detail(value);
assert(!$('#report-results').hidden);assert($('#report-performance').hidden);
chooseReportTab('performance');
assert(!$('#report-performance').hidden);
const server=$('#performance-server');
assert.match(server.textContent,/Speculative decoding acceptance rate/);
assert.match(server.textContent,/84\.2.*%/);
assert.match(server.textContent,/Partial coverage/i);
assert.match(server.textContent,/Accepted speculative tokens by position/);
assert.match(server.textContent,/zero-based/);
''', dom=True)


def test_series_limited_gpu_failure_remains_in_findings_check_and_performance():
    value = gpu_series_limited_report().model_dump(mode='json')
    run_render_checks('const value=' + json.dumps(value) + r''';
detail(value);
assert.match($('#failing-requests').textContent,/GPU 7: XID changed during observation \(value 79.0\)/);
state.selectedCheck='test:gpu';renderDetail();
const gpuDetails=$('.check-detail');
assert.match(gpuDetails.textContent,/DCGM_FI_DEV_XID_ERRORS/);
assert.match(gpuDetails.textContent,/79/);
chooseReportTab('performance');
const server=$('#performance-server');
const errors=server.querySelectorAll('tr').find(row=>row.textContent.startsWith('GPU device errors'));
assert(errors);assert.match(errors.textContent,/79 code/);
assert.match(server.textContent,/Speculative decoding acceptance rate/);
assert(!$('#report-performance').hidden);
''', dom=True)
