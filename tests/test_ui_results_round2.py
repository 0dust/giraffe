"""Reason formatting preserves sentences and upcoming backend copy."""
from tests.test_ux_results import run_render_checks


def test_removed_prefix_and_split_sentences_start_with_capitals():
    run_render_checks(r"""
const reason="Timing observed; no acceptance limit configured. set limits.first_output_ms to your service's acceptance target and rerun.";
const expected="Timing observed; no acceptance limit configured. Set limits.first_output_ms to your service's acceptance target and rerun.";
for(const input of [reason,reason.replace('. set ','. To conclude, set ')]){
 assert.equal(shortReason(input).summary,expected);
 const html=detail(report([check('first_output','inconclusive',{summary:input})],{overall:'inconclusive',observations:{summary:input}}));
 assert(html.includes('<p class="result-guidance">'+escape(expected)+'</p>'));
 assert(html.includes('<p class="finding-check-reason">'+escape(expected)+'</p>'));
 assert.doesNotMatch(html,/\. set limits|To conclude,/);
}
assert.equal(shortReason('To conclude, set a target. Keep this sentence. collect more samples.').summary,'Set a target. Keep this sentence.');
assert.equal(shortReason('First. Second. To conclude, collect more samples. then rerun.').rest,'Collect more samples. Then rerun.');
assert.equal(shortReason('First. Second. “collect more samples.”').rest,'“Collect more samples.”');
""")


def test_backend_limit_remedy_preserves_option_name_and_capitalization():
    run_render_checks(r"""
const reason='Timing observed; no acceptance limit configured. Set limits.first_output_ms (--first-output-ms) to your target and rerun.';
assert.equal(shortReason(reason).summary,reason);
const html=detail(report([check('first_output','inconclusive',{summary:reason})],{overall:'inconclusive',observations:{summary:reason}}));
assert(html.includes('<p class="result-guidance">'+reason+'</p>'));
assert(html.includes('<p class="finding-check-reason">'+reason+'</p>'));
assert.doesNotMatch(html,/\. set limits|--First-output-ms/);
""")


def test_backend_user_stop_sentence_is_not_prefixed_twice_or_lowercased():
    run_render_checks(r"""
const reason='Stopped by you after 16 of 20 planned requests.';
assert.equal(shortReason(reason).summary,reason);
for(const abort_reason of [null,'cancelled by user',reason]){
 const value=report([check('generation','inconclusive',{summary:'Partial timing evidence recorded.'})],{overall:'inconclusive',abort_reason,observations:{summary:reason}});
 const html=detail(value,{progress:{completed:16},request_plan:{planned_total:20}}),hero=html.split('report-toolbar')[0];
 assert.equal((hero.match(/Stopped by you/g)||[]).length,1);
 assert.doesNotMatch(hero,/Stopped by you.*Stopped by you|stopped by you/);
 if(abort_reason==='cancelled by user'){
  assert.match(hero,/<h1>Stopped by you<\/h1>/);
  assert.match(hero,/16 of 20 planned requests finished\. Partial results below\./);
 }else assert(hero.includes('<p class="result-guidance">'+reason+'</p>'));
}
""")
