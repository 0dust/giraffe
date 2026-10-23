"""Condition skin reading order and controls match the shared report skin."""

from pathlib import Path
import re

import pytest

from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize("count", [2, 7, 24, 32])
def test_planned_cells_follow_centroid_reading_order_and_stay_put_on_updates(count):
    run_render_checks('const conditionCount='+str(count)+';'+r"""
state.detail={id:'ordered-skin',state:'running'};
const planned=Array.from({length:conditionCount},(_,i)=>({target:'local',condition:{id:'test-'+i,label:'Test '+i,check:'buckets'},aggregate_status:'waiting',planned:32,started:0}));
const draw=conditions=>patchHTML($('#main'),renderTests([],{live:true,conditions}));
draw(planned);
const before=$$('.condition-spot').map(spot=>({node:spot,id:spot.dataset.conditionId,points:$('polygon',spot).getAttribute('points'),x:Number($('text',spot).getAttribute('x')),y:Number($('text',spot).getAttribute('y'))}));
const svg=$('.condition-mosaic'),columns=Number(svg.dataset.columns),height=Number(svg.getAttribute('viewBox').split(' ')[3]),rowHeight=height/Math.ceil(planned.length/columns);
for(let i=1;i<before.length;i++){
 const previous=before[i-1],current=before[i],previousRow=Math.floor(previous.y/rowHeight),currentRow=Math.floor(current.y/rowHeight);
 assert(currentRow>previousRow || currentRow===previousRow&&current.x>previous.x,'Planned execution order must follow cell centroids left to right, then top to bottom');
}
// A keyed update may arrive in a different order; it must not reorder keyboard
// navigation, reassign cells, or restart a terminal cell's animation.
before[0].node.focus({preventScroll:true});
const updated=planned.map((row,i)=>({...row,aggregate_status:i<8?'passed':i===8?'running':'waiting'})).reverse();
draw(updated);
assert.deepEqual($$('.condition-spot').map(spot=>spot.dataset.conditionId),before.map(cell=>cell.id));
for(const cell of before){const spot=$$('.condition-spot').find(spot=>spot.dataset.conditionId===cell.id);assert.equal(spot,cell.node);assert.equal($('polygon',spot).getAttribute('points'),cell.points);}
assert.equal(document.activeElement,before[0].node);
const terminal=before[0].node,animations=terminal.animationCalls.length;
draw([...updated,{target:'local',condition:{id:'unexpected',label:'Extra condition',check:'buckets'},aggregate_status:'waiting'}]);
assert.equal(terminal.animationCalls.length,animations);assert.equal($$('.condition-spot').at(-1).dataset.conditionId,'unexpected');
for(const cell of before)assert.equal($('polygon',cell.node).getAttribute('points'),cell.points);
""", dom=True)


def test_summary_and_legend_use_mock_status_order_regardless_of_execution_order():
    run_render_checks(r"""
state.detail={id:'status-order',state:'running'};
const statuses=['waiting','running','skipped','inconclusive','failed','passed','stopped'];
$('#main').innerHTML=renderTests([],{live:true,conditions:statuses.map((status,i)=>({target:'local',condition:{id:'test-'+i,label:'Test '+i,check:'buckets'},aggregate_status:status}))});
assert.equal($('.condition-summary').textContent,'1 passed · 1 failed · 1 inconclusive · 1 running · 1 waiting · 1 not run · 1 stopped');
const legend=$('.mosaic-legend').textContent;
const expected=['Passed','Failed','Inconclusive','Running','Waiting','Not run','Stopped'];
for(let i=1;i<expected.length;i++)assert(legend.indexOf(expected[i-1])<legend.indexOf(expected[i]));
""", dom=True)


def test_live_mission_control_uses_condition_copy():
    run_render_checks(r"""
state.detail={id:'live-copy',state:'running',config:{targets:[{name:'local',model:'test',url:'http://localhost/v1'}],checks:[],limits:{}},live_requests:[],live_conditions:[],events:[]};
renderLive();const html=nodes['#main'].innerHTML;
assert.match(html,/>Mission control<\/h1>/);
assert.match(html,/One spot per test\. Select a spot for its requests and results\./);
assert.doesNotMatch(html,/Requests appear as they start/);
""")


def test_dialog_status_and_rows_have_chevron_space_and_app_focus_ring():
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    dialog_rules = re.findall(r'#test-dialog \.request-tools select\s*\{([^}]+)\}', css)
    assert dialog_rules
    # This selector outranks #test-dialog select; its shorthand used to reset
    # the chevron's 32px padding to 8px and draw text over the select affordance.
    assert 'padding:4px 32px 4px 8px' in dialog_rules[-1]
    global_select = re.search(r'^select\s*\{([^}]+)\}', css, re.M).group(1)
    assert 'appearance: none' in global_select
    assert 'background-image: url("data:image/svg+xml,' in global_select
    assert 'background-repeat: no-repeat' in global_select
    focus = re.search(r'^input:focus, select:focus, textarea:focus\s*\{([^}]+)\}', css, re.M).group(1)
    assert 'border-color: var(--accent-line)' in focus
    assert 'box-shadow: 0 0 0 4px var(--accent-soft)' in focus
