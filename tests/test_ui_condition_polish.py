"""Condition rows and controls retain readable copy and native affordances."""

from pathlib import Path
import re

from tests.test_ux_results import run_render_checks


def test_live_running_view_uses_condition_copy():
    run_render_checks(r"""
state.detail={id:'live-copy',state:'running',config:{targets:[{name:'local',model:'test',url:'http://localhost/v1'}],checks:[],limits:{}},live_requests:[],live_conditions:[],events:[]};
renderLive();const html=nodes['#main'].innerHTML;
assert.match(html,/>Running<\/h1>/);
assert.doesNotMatch(html,/One spot per|Select a spot|condition-mosaic|request-mosaic/);assert.match(html,/Conditions/);
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
