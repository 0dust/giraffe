"""Reason formatting preserves sentences and upcoming backend copy."""
from tests.test_ux_results import run_render_checks


def test_removed_prefix_and_split_sentences_start_with_capitals():
    run_render_checks(r"""
const reason="Timing observed; no acceptance limit configured. set Time to first answer in Advanced and run again.";
const expected="Timing observed; no acceptance limit configured. Set Time to first answer in Advanced and run again.";
for(const input of [reason,reason.replace('. set ','. To conclude, set ')]){
 assert.equal(shortReason(input).summary,expected);
 const html=detail(report([check('first_output','inconclusive',{summary:input})],{overall:'inconclusive',observations:{summary:input}}));
 assert(!html.includes('<p class="result-guidance">'+escape(expected)+'</p>'),'hero does not repeat the finding');
 assert(html.includes('<p class="finding-check-reason">'+escape(expected)+'</p>'));
 assert.doesNotMatch(html,/\. set Time|To conclude,/);
}
assert.equal(shortReason('To conclude, set a target. Keep this sentence. collect more samples.').summary,'Set a target. Keep this sentence.');
assert.equal(shortReason('First. Second. To conclude, collect more samples. then rerun.').rest,'Collect more samples. Then rerun.');
assert.equal(shortReason('First. Second. “collect more samples.”').rest,'“Collect more samples.”');
""")


def test_backend_limit_remedy_uses_ui_control_names_without_cli_or_config_keys():
    run_render_checks(r"""
const reason='Timing observed; no acceptance limit configured. Set limits.first_output_ms (--first-output-ms) to your target and rerun.';
assert.equal(shortReason(reason).summary,reason);
const html=detail(report([check('first_output','inconclusive',{summary:reason,finding_summary:'Timing observed; no acceptance limit configured.',remediation:{action:'set_acceptance',check:'first_output'}})],{overall:'inconclusive',observations:{summary:reason}}));
assert.match(html,/finding-check-reason[^<]*.*Timing observed; no acceptance limit configured/s);
assert.match(html,/Set acceptance limits for Time to first answer in Advanced/);
assert.match(html,/data-action="edit-result-check" data-check-id="first_output">Open check settings/);
assert.equal(measuredCheckReason(check('first_output','inconclusive',{summary:reason}),report([])),reason,'legacy backend prose is preserved verbatim, not regex rewritten');
assert.doesNotMatch(html,/limits\.first_output_ms|--first-output-ms|--First-output-ms|<summary>Details<\/summary>/);
""")


def test_backend_user_stop_sentence_is_not_prefixed_twice_or_lowercased():
    run_render_checks(r"""
const reason='Stopped by you after 16 of 20 planned requests.';
assert.equal(shortReason(reason).summary,reason);
for(const abort_reason of [null,'cancelled by user',reason]){
 const value=report([check('generation','inconclusive',{summary:'Partial timing evidence recorded.'})],{overall:'inconclusive',abort_reason,observations:{summary:reason}});
 const html=detail(value,{progress:{completed:16},request_plan:{planned_total:20}}),hero=html.split('report-toolbar')[0];
 assert.equal((hero.match(/Stopped by you/g)||[]).length,abort_reason?1:0);
 assert.doesNotMatch(hero,/Stopped by you.*Stopped by you|stopped by you/);
 if(abort_reason==='cancelled by user'){
  assert.match(hero,/<h1>Stopped by you<\/h1>/);
  assert.match(hero,/16 of 20 planned requests finished\. Partial results below\./);
 }else if(abort_reason)assert(hero.includes('<p class="result-guidance">'+reason+'</p>'));
 else {assert.match(hero,/<h1>Generation speed is inconclusive<\/h1>/);assert.doesNotMatch(hero,/Stopped by you/);}
}
""")
