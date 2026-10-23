"""Nice drawing domains do not change observations or request-number ticks."""
from tests.test_ui_performance import performance as performance, render_assertions
from tests.test_ux_results import run_render_checks


def test_time_value_and_count_axes_have_uniform_nice_steps_and_cover_the_data():
    run_render_checks(r"""
function checkNice(ticks,integer=false){
 assert(ticks.length>=2&&ticks.length<=9);
 const step=ticks[1]-ticks[0],scale=10**Math.floor(Math.log10(step));
 assert([1,2,2.5,5,10].some(n=>Math.abs(step/scale-n)<1e-8),'nice decimal step');
 for(let i=1;i<ticks.length;i++)assert(Math.abs(ticks[i]-ticks[i-1]-step)<step*1e-8,'uniform ticks');
 if(integer)assert(ticks.every(Number.isInteger));
}
for(const [max,integer] of [[11.26,false],[156,true],[.037,false],[3,true]]){
 const axis=performanceNiceTicks(0,max,4,integer);checkNice(axis.ticks,integer);
 assert(axis.max>=max);assert.equal(axis.ticks.at(-1),axis.max);
}
const time=performanceNiceTicks(0,217.48);checkNice(time.ticks);assert(time.max>=217.48);
const zoom=performanceNiceTicks(62.97,114.16);checkNice(zoom.ticks);assert(zoom.max>=114.16);
const axes=performanceAxes(1,1000,11.26,{integerX:true});
assert.deepEqual(axes.xticks,[1,251,501,751,1000]);
assert(axes.ymax>=11.26);assert.equal(axes.sy(axes.ymax),34);assert(axes.sy(11.26)>=34);
const counts=performanceAxes(0,217.48,156,{integerY:true});
checkNice(counts.xticks);checkNice(counts.yticks,true);
assert(counts.xmax>=217.48);assert(counts.ymax>=156);
assert(counts.sx(217.48)<=counts.right);assert(counts.sy(156)>=34);
""")


def test_extended_chart_domains_preserve_metrics_raw_rows_and_pointer_inspection(performance):
    render_assertions(performance, r"""
const before=JSON.stringify(summary),last=summary.actual_concurrency.points.at(-1).elapsed_s;
draw();
const concurrency=performanceCharts.get('concurrency');assert(concurrency.xmax>=last);
const svg=$('[data-performance-chart="concurrency"]');
svg.getBoundingClientRect=()=>({left:0,width:concurrency.width});
handlePerformancePointer({target:svg,clientX:concurrency.right,clientY:100});
assert.match($('#performance-inspection-concurrency').textContent,/Elapsed/);
const completions=performanceCharts.get('completions');
assert(completions.xmax>=summary.completion_buckets.buckets.at(-1).end_s);
assert.equal(JSON.stringify(summary),before);
""")
