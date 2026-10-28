"""Final layout and setup regressions, exercised without sockets."""
from pathlib import Path
import re

from tests.test_ux_results import run_render_checks


def test_comparison_uses_condition_headers_and_five_cells_per_metric():
    run_render_checks(r'''
const rows=[{check:'access',scenario:'warm',metric:'latency_ms',status:'pass',baseline:10,current:12,baseline_samples:6,current_samples:6},
 {check:'generation',scenario:'warm',metric:'output_tokens_per_second',status:'fail',baseline:40,current:20,reason:'Output slowed down.'},
 {check:'first_output',scenario:'cold',metric:'first_output_ms',status:'inconclusive',reason:'Only one sample.',baseline:5,current:6}];
$('#main').innerHTML=renderComparison(report([],{baseline:{targets:[{target:'local',comparisons:rows}]}}));
for(const table of $$('.comparison-metrics')){
 assert.deepEqual(Array.from(table.querySelectorAll('thead th'),node=>node.textContent),['Metric','Baseline','Current','Δ','Verdict']);
 for(const row of table.querySelectorAll('.comparison-metric'))assert.equal(row.childNodes.filter(node=>node.nodeType===1).length,5);
}
assert.equal($$('.comparison-metric').length,3);assert.equal($$('.comparison-group').length,2);
for(const header of $$('.comparison-group th')){assert.equal(header.getAttribute('colspan'),'5');assert.equal(header.getAttribute('scope'),'rowgroup');}
assert.equal($('.comparison-reason').closest('tr').getAttribute('class'),'comparison-metric');
assert.equal($('.comparison-metric [data-label="Baseline"]').textContent,'10 msn=6');
assert.match($('#main').textContent,/Output slowed down|Only one sample/);
''',dom=True)


def test_wide_estimate_keeps_low_estimate_and_names_slow_response_bound():
    run_render_checks(r'''
assert.equal(renderPlanTime({estimated_low_seconds:180,estimated_high_seconds:3780}),'<p class="hint plan-time">~3 min (up to 63 min if responses slow down)</p>');
assert.match(renderPlanTime({estimated_low_seconds:1,estimated_high_seconds:6}),/~1 s \(up to 6 s if responses slow down\)/);
assert.match(renderPlanTime({estimated_low_seconds:1,estimated_high_seconds:5}),/~1 s–5 s/);
assert.match(renderPlanTime({estimated_low_seconds:0,estimated_high_seconds:2}),/~0 s \(up to 2 s if responses slow down\)/);
assert.equal(renderPlanTime(null),'<p class="hint plan-time">Duration unknown until first requests</p>');
''')

    css=(Path(__file__).parents[1]/'src/giraffe/static/styles.css').read_text()
    table=re.search(r'\.comparison-table \.comparison-metrics\s*\{([^}]+)\}',css).group(1)
    assert 'table-layout: fixed' in table and 'min-width: 0' in table and 'width: 100%' in table
    widths=[float(re.search(r'\.'+name+r'\s*\{\s*width:\s*([\d.]+)%',css).group(1))
            for name in ['comparison-name-col','comparison-value-col','comparison-value-col','comparison-delta-col','comparison-verdict-col']]
    assert sum(widths)==100
    assert re.search(r'\.comparison-table\s*\{[^}]*min-width: 0;[^}]*overflow: visible',css)
    numeric=re.search(r'\.comparison-table \.comparison-number\s*\{([^}]+)\}',css).group(1)
    assert 'text-align: right' in numeric and 'white-space: nowrap' in numeric
    assert 'font-variant-numeric: tabular-nums' in numeric
    reason=re.search(r'\.comparison-table \.comparison-reason\s*\{([^}]+)\}',css).group(1)
    assert 'text-transform: none' in reason
    assert re.search(r'@media\s*\(max-width:\s*599px\)',css)
    assert '.comparison-metric [data-label="Current"]::before' in css


def test_reset_restored_limits_includes_timeout_and_concurrency():
    run_render_checks(r'''
(async()=>{
bootstrap.config={...structuredClone(bootstrap.defaults),request_timeout_seconds:300,concurrency:4,targets:[{name:'local',model:'m'}]};
state.config={...structuredClone(bootstrap.config),max_requests:80,max_duration_seconds:60,request_timeout_seconds:15,concurrency:1,max_output_tokens:1024};
state.limitSource='restored';saveDraft=captureFormDraft=()=>{};readForm=()=>({config:state.config});
renderNew=()=>{$('#main').innerHTML='<form id="new-run-form">'+renderSharedRunSettings(state.config)+'</form>';};
renderNew();await listeners.click({target:$('[data-action="reset-restored-limits"]'),preventDefault(){}});
for(const key of ['max_requests','max_duration_seconds','concurrency','request_timeout_seconds'])assert.equal(state.config[key],bootstrap.config[key],key);
assert.equal(state.config.max_output_tokens,1024);assert.equal(state.config.targets[0].model,'m');
assert.match($('#run-ceilings-summary').textContent,/≤ 5 m per request · 4 at once/);
state.config.request_timeout_seconds=15;state.limitSource='restored';renderNew();assert.match($('#main').textContent,/Limits from your last run/);
})()
''',dom=True)
