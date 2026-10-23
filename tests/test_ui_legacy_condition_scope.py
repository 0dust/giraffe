"""Old report condition cells retain their recorded concurrency context."""
from tests.test_ux_results import run_render_checks


def test_legacy_bucket_id_keeps_concurrency_in_tooltip_strip_and_dialog():
    run_render_checks(r"""
state.detail={id:'old-bucket',state:'completed'};
const condition={id:'bucket_c32_short_short',label:'Short input / short output',check:'buckets'};
const summary={condition,started:32,completed:32,planned:32,planned_kind:'exact',status:'finished'};
const html=renderTests([],{conditions:[summary]});
const view=[...conditionViews.values()][0];
assert.equal(view.description,'32 at once');
assert.match(html,/Short input \/ short output · 32 at once/);
assert.doesNotMatch(conditionSelection(view),/Concurrency not recorded/);
""")
