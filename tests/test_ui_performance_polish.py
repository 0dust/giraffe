"""Phase D chart readability contracts, without browser sockets."""

from pathlib import Path

from tests.test_ui_performance import performance as performance, render_assertions
from tests.test_ux_results import run_render_checks, stylesheet_rules


def test_axes_use_css_pixel_width_and_integer_count_ticks():
    run_render_checks(r"""
const axes=performanceAxes(1,1000,3,{width:330,integerX:true,integerY:true});
$('#main').innerHTML='<svg>'+axes.html+'</svg>';
assert.equal(axes.width,330);
assert(axes.sx(1000)<=310);
for(const text of $$('text').filter(item=>item.dataset.axisTick))assert.match(text.textContent,/^[\d,]+$/);
assert.deepEqual(performanceIntegerTicks(1,1000,4),[1,251,501,751,1000]);
assert.deepEqual(performanceIntegerTicks(0,1,4),[0,1]);
""", dom=True)


def test_resize_observer_reflows_chart_geometry_without_changing_data(performance):
    render_assertions(performance, r"""
const before=JSON.stringify(summary.metrics);
performanceChartWidths.set('size',330);draw();
const svg=$('[data-performance-chart="size"]');assert.equal(svg.getAttribute('viewBox'),'0 0 330 292');
const chart=performanceCharts.get('size');assert.equal(chart.width,330);
svg.getBoundingClientRect=()=>({left:0,width:330});
handlePerformancePointer({target:svg,clientX:chart.right,clientY:150});
assert.match($('#performance-inspection-size').textContent,/SYNTHETIC-1000/);
assert.equal(JSON.stringify(summary.metrics),before);
let callback,observed=[];
ResizeObserver=class{constructor(fn){callback=fn;}observe(node){observed.push(node);}disconnect(){}};
state.reportRouteActive=true;
observePerformanceCharts();assert(observed.includes(svg));
let rendered=0;renderDetail=()=>{rendered++;};
callback([{target:svg,contentRect:{width:390}}]);assert.equal(performanceChartWidths.get('size'),390);assert.equal(rendered,1);
state.reportRouteActive=false;
callback([{target:svg,contentRect:{width:400}}]);assert.equal(rendered,1,'pending resize must not reopen a report after navigation');
""")


def test_unique_series_colors_above_plot_and_bounded_intended_transition_dots(performance):
    render_assertions(performance, r"""
const timing=$('[data-performance-chart="timing"]'),article=timing.parentNode,legend=article.querySelector('.performance-chart-legend');
assert(article.childNodes.indexOf(legend)<article.childNodes.indexOf(timing));
const colors=$$('[data-chart="timing"]').map(item=>item.getAttribute('style'));
assert.equal(new Set(colors).size,5);
const intended=$('[data-performance-chart="concurrency"] [data-series="intended"]');assert(intended);
const dots=$$('.performance-intended-transition');assert(dots.length>=6);assert(dots.length<=40);
assert.match(intended.getAttribute('d'),/H[\d.]+V/);
""")


def test_numeric_precision_and_units():
    run_render_checks(r"""
assert.match(performanceDisplay({value:5569.554,unit:'ms'}),/5,569\.55/);
assert.match(performanceDisplay({value:121.553,unit:'tok\/s'}),/121\.55/);
assert.match(performanceDisplay({value:0.0001234,unit:'s'}),/0\.00012/);
assert.match(performanceDisplay({value:0.015,unit:'s'}),/0\.015/);
assert.match(performanceDisplay({value:1.234e-12,unit:'s'}),/1\.2e-12/);
assert.match(performanceDisplay({value:481.45,unit:'tok\/s'}),/<small class="performance-unit">tok\/s<\/small>/);
assert.equal(performanceDisplay({value:null,unit:'ms'}),'—');
""")


def test_completion_bucket_caption_gaps_and_token_specific_captions(performance):
    render_assertions(performance, r"""
const article=$('[data-performance-chart="completions"]').parentNode,caption=article.querySelector('.performance-caption').textContent;
assert.match(caption,/second buckets;.*final bucket.*actual duration/);
assert.doesNotMatch(caption,/Aim|half.open|choose first/);
assert.match(article.querySelector('details').textContent,/bucket|boundary/i);
const bars=$$('.performance-completion-bar');
for(let i=1;i<bars.length;i++)assert(Number(bars[i-1].getAttribute('x'))+Number(bars[i-1].getAttribute('width'))<Number(bars[i].getAttribute('x')),'bars need visible gaps');
assert.doesNotMatch($('#performance-tokens').textContent,/Timing phases are not all additive/);
assert.match($('#performance-tokens').textContent,/input.*answer|cached.*input/i);
""")


def test_narrow_toolbar_is_in_normal_flow_and_chart_text_remains_readable():
    css=(Path(__file__).parents[1]/'src/giraffe/static/styles.css').read_text()
    rules=list(stylesheet_rules(css))
    assert any('.report-toolbar' in selector and row.get('position')=='static' and any('max-width:640px' in parent.replace(' ','') for parent in parents) for selector,row,parents in rules)
    assert any('.performance-chart text' in selector and '11px' in row.get('font','') for selector,row,parents in rules)
    assert 'ResizeObserver' in (Path(__file__).parents[1]/'src/giraffe/static/app.js').read_text()


def test_measurement_groups_readable_workload_names_and_describes_full_schedule():
    from giraffe.performance import build_performance
    from tests.performance_fixture import synthetic_report_1000

    value=build_performance(synthetic_report_1000())
    render_assertions(value, r"""
$('#main').innerHTML=renderReportToolbar()+renderPerformance();
const select=$('#performance-window'),groups=select.querySelectorAll('optgroup');
assert.equal(groups.length,1);assert.match(groups[0].getAttribute('label'),/Load test/);
assert.equal(select.querySelectorAll('option').length,8);
assert.match(select.textContent,/full run.*1,000 requests/);
assert.match(select.textContent,/Stage C4.*80 requests/);
assert.match(select.textContent,/Drain/);
assert.doesNotMatch(select.textContent,/synthetic_stage_|cohort|check=load/);
assert.match($('.performance-window-description').textContent,/Full request window.*1,000 requests.*six changing concurrency stages/);
assert.equal(performanceCharts.get('timing').rows.length,1000);
""")
