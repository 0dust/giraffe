"""Launch notes and traffic headlines preserve warning and count semantics."""

from tests.test_ux_catalogue import browser_logic


def test_migration_notes_use_neutral_status_and_keep_warning_styles():
    browser_logic(r'''
state.migrationNotes=['Earlier Giraffe versions <example>'];
const markup=renderMigrationNotes();
assert.match(markup,/<div class="notice migration-notes" role="status">/);
assert.match(markup,/<strong>Imported from an earlier Giraffe version<\/strong>/);
assert.match(markup,/&lt;example&gt;/);
assert.doesNotMatch(markup,/notice warning/);
assert.match(renderSettingIssues([{msg:'Real warning',fields:['concurrency']}],'warning'),/notice warning setting-issue/);
state.dismissedMigrationNotes=[...state.migrationNotes];
assert.equal(renderMigrationNotes(),'');
''')


def test_unknown_request_headline_and_other_count_labels():
    browser_logic(r'''
for(const count_kind of ['bounded','exact','estimate']){
 assert.equal(setupPlanCount({request_count_label:'Unknown',count_kind}),'Requests depend on response time');
}
for(const [plan,expected] of [
 [{request_count_label:'1',count_kind:'exact',planned_total:1},'1 request planned'],
 [{request_count_label:'20',count_kind:'exact',planned_total:20},'20 requests planned'],
 [{request_count_label:'≈20',count_kind:'estimate'},'≈20 requests'],
 [{request_count_label:'Unknown + 20',count_kind:'bounded'},'20 known requests + adaptive checks'],
 [{request_count_label:'Unknown + ≈20',count_kind:'bounded'},'≈20 expected requests + adaptive checks'],
 [{count_kind:'exact',planned_total:1},'1 request planned'],
 [{count_kind:'estimate',planned_total:20},'About 20 requests'],
 [{count_kind:'bounded',known_total:20},'20 known requests + adaptive checks'],
])assert.equal(setupPlanCount(plan),expected);
''')
