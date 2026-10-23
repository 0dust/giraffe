"""Performance presentation contracts from recorded aggregation data, without sockets."""

import copy
import json
from pathlib import Path

import pytest

from giraffe.models import RequestRecord, RunReport
from giraffe.performance import build_performance
from tests.performance_fixture import synthetic_report_1000
from tests.test_ux_results import run_render_checks, stylesheet_rules


@pytest.fixture(scope="module")
def performance():
    # Schedule stages belong to one comparable workload; use the real fixture
    # unchanged so its default scope must retain all 1,000 observations.
    return build_performance(synthetic_report_1000())


def render_assertions(performance, assertions):
    setup = "const fixture=" + json.dumps(performance) + ";\n" + r"""
state.detail={id:'SYNTHETIC-1000-NOT-A-BENCHMARK',state:'completed',report:report([],{performance:fixture})};
syncReportView();
reportView.tab='performance';
const draw=()=>{document.querySelector('#main').innerHTML=renderPerformance();};
draw();
const selected=fixture.targets[0],summary=selected.summaries[selected.selected_window||selected.default_window];
const kpiHeadline=card=>card.childNodes.find(node=>node.tagName==='STRONG');
"""
    run_render_checks(setup + assertions, dom=True)


def test_synthetic_thousand_sections_kpis_and_bounded_charts(performance):
    render_assertions(performance, r"""
const sectionIds=['overview','timing','throughput','delivery','outcomes','tokens','effects','server','baseline'];
const markup=$('#main').innerHTML;
let previous=-1;
for(const id of sectionIds){const position=markup.indexOf('id="performance-'+id+'"');assert(position>previous,id+' is missing or out of order');previous=position;}
assert.equal($$('.performance-kpis .performance-kpi').length,10);
assert.equal(summary.request_row_indices.length,1000);
assert.match($('#performance-overview').textContent,/1,000/);
for(const id of ['timing','completions','concurrency','size','cached']){
 const chart=$('[data-performance-chart="'+id+'"]');assert(chart,id+' chart');
 const svg=chart.tagName==='SVG'?chart:chart.querySelector('svg');assert(svg,id+' SVG');
 assert.equal(svg.getAttribute('role'),'img');assert(svg.getAttribute('aria-label')||svg.querySelector('desc'));
 assert(svg.querySelectorAll('path').length<=8,'lines should be paths, not a node per request');
 assert.equal(svg.querySelectorAll('circle:not(.performance-intended-transition)').length,0,'dense lines have no visible request markers');
 assert(svg.querySelectorAll('.performance-intended-transition').length<=40,'schedule transition markers remain bounded');
 assert(svg.querySelectorAll('*').length<500,'SVG node count must stay bounded');
}
assert.match($('#performance-timing').textContent,/1,000/);
assert.match($('#performance-timing').textContent,/every request|all requests|metrics/i);
assert.match(markup,/All timing percentiles &amp; definitions/);
const distributions=$$('details').find(item=>/All timing percentiles & definitions/.test(item.querySelector('summary')?.textContent||''));
assert(distributions);for(const label of ['Mean','Min','Median','P90','P95','P99','Max'])assert.match(distributions.textContent,new RegExp(label,'i'));
assert.match(distributions.textContent,/linear interpolation/i);
assert.match($('#performance-throughput').textContent,/partial/i);
assert.match($('#performance-throughput').textContent,/count|completed requests/i);
assert.match($('#performance-outcomes').textContent,/Requests started/);
assert.match($('#performance-tokens').textContent,/Token accounting/);
assert.match($('#performance-server').textContent,/This saved run has no runtime telemetry collection evidence/);
assert.match($('#performance-baseline').textContent,/baseline/i);
""")


def test_decimation_keeps_endpoints_and_outliers():
    run_render_checks(r"""
const points=Array.from({length:10003},(_,x)=>({x,y:Math.sin(x/9),request_id:'r'+x}));
points[125].y=10000;points[126].y=-9000;points[9199].y=8000;
const drawn=minMaxDecimate(points,80);
assert(drawn.length<=162);assert.equal(drawn[0],points[0]);assert.equal(drawn.at(-1),points.at(-1));
for(const index of [125,126,9199])assert(drawn.includes(points[index]),'lost outlier '+index);
for(let i=1;i<drawn.length;i++)assert(drawn[i].x>drawn[i-1].x);
assert.deepEqual(minMaxDecimate([],80),[]);
assert.deepEqual(minMaxDecimate(points.slice(0,1),80),points.slice(0,1));
assert.deepEqual(minMaxDecimate(points.slice(0,2),80),points.slice(0,2));
""")


def test_chart_inspection_reads_full_rows_and_legend_toggles(performance):
    render_assertions(performance, r"""
const chart=$('[data-performance-chart="timing"]');assert(chart);
assert(handlePerformancePointer({target:chart,clientX:110}));
assert.match($('#performance-inspection-timing').textContent,/SYNTHETIC-1000/);
assert.equal(chart.querySelector('.performance-crosshair').hidden,false);
let prevented=false;
assert(handlePerformanceKeydown({target:chart,key:'End',preventDefault(){prevented=true;}}));
assert(prevented);assert.match($('#performance-timing').textContent,/SYNTHETIC-1000/);
handlePerformanceKeydown({target:chart,key:'Home',preventDefault(){}});
assert.match($('#performance-timing').textContent,/SYNTHETIC-0001/);
const legend=$('[data-performance-series][data-chart="timing"]');assert(legend,'interactive series legend');
const old=legend.getAttribute('aria-pressed');
handlePerformanceClick({target:legend,preventDefault(){}});
const updated=$('[data-performance-series="'+legend.dataset.performanceSeries+'"][data-chart="timing"]');
assert.notEqual(updated.getAttribute('aria-pressed'),old);
handlePerformanceKeydown({target:$('[data-performance-chart="timing"]'),key:'Home',preventDefault(){}});
assert.match($('#performance-inspection-timing').textContent,/SYNTHETIC-0001/);
""")


def test_stage_zoom_changes_drawing_without_fetching_or_mutating_backend_metrics(performance):
    render_assertions(performance, r"""
const selector=$('#performance-concurrency-stage');assert(selector,'stage zoom control');
const stage=selector.querySelectorAll('option').find(option=>/C4/.test(option.textContent));assert(stage);
const before=$('[data-performance-chart="concurrency"]').innerHTML;
const recorded=JSON.stringify(summary.metrics);
api=()=>{throw Error('Stage zoom must not request a different measurement window');};
selector.value=stage.getAttribute('value');
assert(handlePerformanceChange({target:selector}));
const after=$('[data-performance-chart="concurrency"]').innerHTML;
assert.notEqual(after,before);
assert.equal(JSON.stringify(summary.metrics),recorded,'zoom is a chart viewport, not a new aggregation');
""")


def test_rendering_uses_backend_values_without_scanning_request_records(performance):
    render_assertions(performance, r"""
Object.defineProperty(state.detail.report,'requests',{get(){throw Error('Performance must consume aggregation data');}});
const changedMetric=summary.metrics.request_throughput;
changedMetric.value=12345.67;
draw();
const throughput=$$('.performance-kpis .performance-kpi').find(card=>/Request throughput/.test(card.textContent));
assert.match(throughput.textContent,/12,345\.67/);
assert.match(throughput.textContent,/req\/s/);
""")


def test_kpi_estimates_and_missing_data_retain_backend_explanations(performance):
    value = copy.deepcopy(performance)
    target = value["targets"][0]
    summary = target["summaries"][target["default_window"]]
    summary["metrics"]["tpot"].update(value=17.5, state="estimated", reason="SYNTHETIC rate estimate", source="1000 / recorded generation pace")
    summary["metrics"]["itl"].update(value=None, state="not_supported", reason="Token boundaries were not observed", action="Use a stream with confirmed token boundaries")
    render_assertions(value, r"""
const cards=$$('.performance-kpis .performance-kpi');
const estimate=cards.find(card=>/Time per output token|TPOT/.test(card.textContent));assert(estimate);
assert.match(estimate.textContent,/17\.5/);assert.match(estimate.textContent,/estimate/i);
const unavailable=cards.find(card=>/Inter.token|ITL/.test(card.textContent));assert(unavailable);
assert.match(unavailable.textContent,/—/);assert.match(unavailable.textContent,/not supported/i);
assert.match(unavailable.textContent,/Token boundaries were not observed/);
assert.match(unavailable.textContent,/Use a stream with confirmed token boundaries/);
assert.doesNotMatch(kpiHeadline(unavailable).textContent,/\b0\b/);
const info=estimate.querySelector('[data-performance-info]');assert(info,'definition reachable by touch and keyboard');
assert.equal(info.tagName,'BUTTON');
const definitionId=info.getAttribute('aria-controls')||info.getAttribute('aria-describedby');assert(definitionId);assert($('#'+definitionId));
handlePerformanceClick({target:info,preventDefault(){}});
assert.equal(info.getAttribute('aria-expanded'),'true');
""")


def test_window_cohort_is_not_silently_expanded_to_all_request_series():
    report = synthetic_report_1000()
    catalog = report.performance["targets"][0]["windows"]
    first_stage = next(window for window in catalog if window["kind"] == "stage")
    value = build_performance(report, window=first_stage["id"])
    render_assertions(value, r"""
assert.equal(selected.request_series.length,1000);
assert(summary.request_row_indices.length>0&&summary.request_row_indices.length<1000);
assert.equal(performanceCharts.get('timing').rows.length,summary.request_row_indices.length);
assert.match($('#performance-timing').textContent,new RegExp(summary.request_row_indices.length+' requests'));
const chart=$('[data-performance-chart="timing"]');
handlePerformanceKeydown({target:chart,key:'End',preventDefault(){}});
assert($('#performance-inspection-timing').textContent.includes(selected.request_series[summary.request_row_indices.at(-1)].request_id));
assert.doesNotMatch($('.performance-inspection').textContent,/SYNTHETIC-1000/);
""")


def test_unknown_intended_load_is_not_drawn_from_configured_ceiling(performance):
    value = copy.deepcopy(performance)
    target = value["targets"][0]
    summary = target["summaries"][target["default_window"]]
    summary["intended_concurrency"] = []
    summary["schedule_intervals"] = []
    summary["intended_state"] = "not_collected"
    summary["intended_reason"] = "No intended schedule transitions were recorded."
    render_assertions(value, r"""
assert.match($('#performance-delivery').textContent,/No intended schedule transitions were recorded/);
const chart=$('[data-performance-chart="concurrency"]');assert(chart,'actual lifetimes still produce a chart');
assert.equal(chart.querySelectorAll('[data-series="intended"]').length,0);
""")


def test_single_nonstreamed_request_keeps_observed_latency_and_missing_timing_honest():
    request = RequestRecord(id="legacy-nonstream", target="old", fixture_id="test",
                            scenario="generation", check_ids=["generation"],
                            started_at="2026-01-01T00:00:00Z", status="completed", valid=True,
                            dispatch_ms=0, completed_ms=1250, elapsed_ms=1250)
    value = RunReport(run_id="old-single", started_at="2026-01-01T00:00:00Z",
                      finished_at="2026-01-01T00:00:01.250Z", overall="inconclusive",
                      manifest={"config": {"targets": [{"name": "old"}]}}, requests=[request])
    render_assertions(value.performance, r"""
const cards=$$('.performance-kpis .performance-kpi');
const latency=cards.find(card=>/Response latency/.test(card.textContent));assert(latency);
assert.doesNotMatch(kpiHeadline(latency).textContent,/—/);
const ttft=cards.find(card=>/TTFT.*client|client.*TTFT/i.test(card.textContent));assert(ttft);
assert.match(kpiHeadline(ttft).textContent,/—/);
assert.match(ttft.textContent,/not collected|not measured|timing|schema/i);
assert.equal($('[data-performance-chart="size"]'),null);
assert.equal($('[data-performance-chart="cached"]'),null);
assert.match($('#performance-tokens').textContent,/no |not measured|unavailable/i);
for(const chart of $$('.performance-chart'))assert(chart.querySelector('path,rect'),'a rendered chart has actual observed marks');
""")


def test_old_report_without_performance_still_has_honest_empty_sections():
    run_render_checks(r"""
state.detail={id:'old',state:'completed',report:{requests:[],checks:[]}};
syncReportView();$('#main').innerHTML=renderPerformance();
assert.equal($$('.performance-chart').length,0);
assert.match($('#performance-overview').textContent,/no |not yet|available|loading/i);
assert.match($('#performance-timing').textContent,/no |not yet|available/i);
assert.match($('#main').textContent,/View test results/);
""", dom=True)


@pytest.mark.parametrize("state", ["completed", "cancelled", "error"])
def test_empty_legacy_and_partial_reports_do_not_draw_empty_charts(state):
    value = RunReport(run_id="legacy", started_at="2026-01-01T00:00:00Z",
                      finished_at="2026-01-01T00:00:01Z", overall="inconclusive",
                      manifest={"config": {"targets": [{"name": "legacy"}]}})
    payload = json.dumps(value.performance)
    run_render_checks("const fixture=" + payload + ";\n" + r"""
state.detail={id:'legacy',state:STATE,report:report([],{performance:fixture})};
syncReportView();$('#main').innerHTML=renderPerformance();
assert.equal($$('.performance-chart').length,0);
assert.match($('#performance-timing').textContent,/no |unavailable|insufficient|not measured/i);
assert.match($('#performance-tokens').textContent,/no |unavailable|not measured/i);
assert.equal($$('.performance-kpis .performance-kpi').length,10);
for(const card of $$('.performance-kpis .performance-kpi'))assert.match(card.textContent,/—/);
""".replace("STATE", json.dumps(state)), dom=True)


def test_desktop_and_narrow_chart_layout_css_and_toolbar_do_not_overlap():
    css = (Path(__file__).parents[1] / "src/giraffe/static/styles.css").read_text()
    rules = list(stylesheet_rules(css))
    toolbar = [(selector, declarations) for selector, declarations, parents in rules
               if not parents and ".report-toolbar" in selector and ">" in selector]
    assert any(row.get("position") == "sticky" for _, row in toolbar), "The main child rule must not override sticky"
    assert any("--app-header-height" in row.get("top", "") for _, row in toolbar)
    assert "safe-area-inset-top" in css
    popover_rules = [(name, row) for name, row, parents in rules
                     if not parents and ".performance-info-popover" in name]
    assert any(":hover" in name and row.get("display") == "block" for name, row in popover_rules)
    assert any(":focus" in name and row.get("display") == "block" for name, row in popover_rules)
    for selector in (".performance-throughput-layout", ".performance-token-charts"):
        desktop = [row for name, row, parents in rules if not parents and selector in name]
        narrow = [row for name, row, parents in rules if parents and selector in name and any("max-width" in p for p in parents)]
        assert any(row.get("display") == "grid" for row in desktop), selector
        assert any(" " in row.get("grid-template-columns", "") or "repeat(2" in row.get("grid-template-columns", "") for row in desktop), selector
        assert any(row.get("grid-template-columns") in ("1fr", "minmax(0,1fr)", "minmax(0, 1fr)") for row in narrow), selector


def test_condition_skin_scales_to_its_panel_content_width():
    css = (Path(__file__).parents[1] / "src/giraffe/static/styles.css").read_text()
    rules = [(selector, row) for selector, row, parents in stylesheet_rules(css)
             if not parents and ".condition-mosaic" in selector]
    assert any(row.get("width", "").replace(" ", "") == "var(--condition-display-width,100%)" for _, row in rules), "Large mosaics fill the panel; one or two conditions use their compact width"
    assert not any(row.get("max-width") not in (None, "none", "100%") for _, row in rules)


def test_uncollected_pause_health_is_distinct_from_measured_no_pauses(performance):
    render_assertions(performance, r"""
summary.client_pauses=[];summary.client_pause_threshold_ms=null;draw();
assert.match($('.performance-pauses').textContent,/recording was not collected/);
summary.client_pause_threshold_ms=100;summary.metrics.max_event_loop_delay.reason='No delays above recording threshold in this window.';draw();
assert.match($('.performance-pauses').textContent,/No delays above recording threshold/);
assert.doesNotMatch($('.performance-pauses').textContent,/recording was not collected/);
""")


def test_stage_bands_and_pause_rows_are_bounded_with_explicit_disclosure(performance):
    render_assertions(performance, r"""
summary.schedule_intervals=Array.from({length:100},(_,i)=>({elapsed_s:i,end_elapsed_s:i+1,label:'Stage '+i}));
summary.client_pauses=Array.from({length:100},(_,i)=>({elapsed_s:i,lag_ms:150}));draw();
assert.equal($$('.performance-stage-band').length,40);assert.match($('#performance-delivery').textContent,/first 40 of 100 stage bands/);
assert.equal($$('.performance-pause-list > div').length,51);assert.match($('.performance-pauses').textContent,/first 50 of 100 client pauses/);
assert.equal(summary.schedule_intervals.length,100);assert.equal(summary.client_pauses.length,100);
""")


def test_zoom_inspection_holds_recorded_step_values_at_window_boundaries():
    run_render_checks(r"""
state.detail={id:'zoom',state:'completed'};
const rows=[{x:0,values:{actual:8}},{x:3,values:{intended:4}},{x:20,values:{actual:0}},{x:20,values:{intended:0}}];
$('#main').innerHTML=renderPerformanceLineChart('concurrency','Actual and intended','Recorded steps',rows,[{key:'actual',label:'Actual',unit:'requests',color:'var(--accent)'},{key:'intended',label:'Intended',unit:'requests',color:'var(--text-2)'}],{step:true,bounds:[5,10]});
const chart=performanceCharts.get('concurrency');assert.equal(chart.ymax,8);assert.equal(chart.rows[0].x,5);assert.equal(chart.rows.at(-1).x,10);
handlePerformanceKeydown({target:$('[data-performance-chart="concurrency"]'),key:'End',preventDefault(){}});
assert.match($('#performance-inspection-concurrency').textContent,/Actual: 8 requests/);assert.match($('#performance-inspection-concurrency').textContent,/Intended: 4 requests/);
""", dom=True)


def test_observed_zero_completion_buckets_have_an_honest_empty_view():
    run_render_checks(r"""
const html=renderPerformanceCompletions({completion_buckets:{width_s:5,rule:'Recorded five-second buckets',buckets:[{start_s:0,end_s:5,duration_s:5,count:0,valid_completions:0}]}});
assert.doesNotMatch(html,/<svg/);assert.match(html,/recorded bucket counts are zero/);assert.doesNotMatch(html,/timestamps were not recorded|lifetimes were not collected/);
""")
