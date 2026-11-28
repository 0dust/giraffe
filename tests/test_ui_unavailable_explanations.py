"""Unavailable values expose the shared reason and next step in existing cards."""

import json

from giraffe.performance import build_performance
from tests.test_performance import record, report
from tests.test_ui_performance import render_assertions
from tests.test_ux_results import run_render_checks


def issue_report():
    checks = ['access', 'serving', 'correctness', 'first_output']
    records = [record(str(index), start=index * 1000, end=index * 1000 + 100,
                      stream=True, timing_schema=1, answer_reads=1, first_output_ms=25)
               .model_copy(update={'check_ids': ['correctness']}) for index in range(5)]
    return report(records, config={'targets': [{'name': 't'}], 'checks': checks})


def test_unavailable_kpi_shows_reason_and_next_step_with_detail_only_in_popover():
    value = build_performance(issue_report())
    target = value['targets'][0]
    summary = target['summaries'][target['default_window']]
    row = summary['metrics']['output_throughput']
    row['detail'] = 'Additional <endpoint> evidence belongs in the popover.'
    assert row['value'] is None
    assert row['reason'] and row['action']
    render_assertions(value, r'''
const unavailableMetric=summary.metrics.output_throughput;
const card=$('[data-live-key="kpi-output_throughput"]');assert(card);
const popover=card.querySelector('.performance-info-popover');assert(popover);
assert(card.querySelector('.performance-collect'));
assert.equal(card.querySelector('.performance-collect').textContent,unavailableMetric.action);
assert.match(popover.textContent,/Next step:/);
assert(popover.textContent.includes(unavailableMetric.detail));
const visible=card.childNodes.filter(node=>!node.classList?.contains('performance-card-label')).map(node=>node.textContent).join(' ');
assert(visible.includes(unavailableMetric.reason));assert(visible.includes(unavailableMetric.action));
assert(!visible.includes(unavailableMetric.detail));
assert.doesNotMatch($('#main').textContent,/No eligible observations|How to collect/);
''')


def test_distribution_without_action_never_prints_null():
    row = {'value': None, 'state': 'not_applicable', 'label': 'Token interval',
           'reason': 'The reply was not streamed.', 'action': None, 'unit': 'ms'}
    run_render_checks('const unavailableMetric=' + json.dumps(row) + r''';
$('#main').innerHTML=performanceDistributions({distributions:{itl:unavailableMetric}},['itl'],'Timing');
assert($('#main').textContent.includes(unavailableMetric.reason));
assert.doesNotMatch($('#main').textContent,/null|undefined/);
''', dom=True)


def test_window_precondition_is_once_per_table_and_shared_cards_show_state_only():
    run_render_checks(r'''
const availability={cause:'data_missing',reason:'WINDOW requests failed.',action:'Fix WINDOW requests first.'};
const row={...availability,value:null,state:'not_collected',label:'First answer',unit:'ms',source:'Recorded timing',sample_count:0,eligible_count:3,display_note:'A card-specific display note'};
const other={...row,label:'Other',cause:'check_not_selected',action:'Add another check.'};
for(const html of [performanceMetricTable([row,{...row,label:'Second'},other],'Timing',availability),performanceDistributions({availability,distributions:{first:row,second:{...row,label:'Second'},other}},['first','second','other'],'Timing')]){
  $('#main').innerHTML=html;
  const main=$('#main');
  assert.equal(main.textContent.split(availability.action).length-1,1);
  const table=main.querySelector('table');
  assert(!table.textContent.includes(availability.action));
  assert.equal(table.textContent.split(availability.reason).length-1,1);
  assert(table.textContent.includes('Add another check.'));
  assert([...main.querySelectorAll('.hint')].some(note=>note.textContent.includes(availability.reason)));
}
$('#main').innerHTML=performanceCard(row,'first',{availability});
assert(!$('#main').textContent.includes(availability.reason));
assert(!$('#main').textContent.includes(availability.action));
assert($('#main').querySelector('.metric-state'));
assert(!$('#main').querySelector('.performance-collect'));
assert($('#main').querySelector('.performance-info-popover').textContent.includes('Recorded timing'));
assert($('#main').querySelector('.performance-info-popover').textContent.includes('0 / 3 eligible'));
assert(!$('#main').textContent.includes('A card-specific display note'));
''', dom=True)


def test_all_failed_window_note_covers_cards_and_tables_once_per_section():
    failed = [record(str(index), timing_schema=1).model_copy(
        update={'valid': False, 'status': 'failed', 'http_status': 503}) for index in range(3)]
    value = build_performance(report(failed))
    target = value['targets'][0]
    summary = target['summaries'][target['default_window']]
    # Keep a distinct explanation in the same KPI grid.
    own = summary['metrics']['output_throughput']
    own.update(cause='endpoint_unsupported', reason='Distinct endpoint evidence.',
               action='Check distinct endpoint support.')
    render_assertions(value, r'''
const availability=summary.availability;assert(availability);
const before=JSON.stringify(summary);
for(const id of ['overview','timing','throughput','delivery','outcomes','tokens','effects','server']){
  const section=$('#performance-'+id);
  const notes=section.querySelectorAll('.hint').filter(note=>note.textContent.includes(availability.action));
  assert(notes.length<=1,id+' duplicates the shared note');
  const sharedCards=section.querySelectorAll('.performance-kpi').filter(card=>{
    const label=card.querySelector('h3').textContent;
    return Object.values(summary.metrics).some(row=>row.label===label&&performanceSharesAvailability(row,availability));
  });
  if(sharedCards.length){
    assert.equal(notes.length,1,id+' needs a shared note');
    assert(section.innerHTML.indexOf(availability.action)<section.innerHTML.indexOf('class="performance-kpi '),id+' note must precede cards');
    for(const card of sharedCards){
      assert(!card.textContent.includes(availability.reason));
      assert(!card.textContent.includes(availability.action));
      assert(!card.querySelector('.performance-collect'));
      assert(!card.querySelector('p'));
      assert(card.querySelector('.metric-state'));
      assert(card.querySelector('.performance-info-popover').textContent.includes('Window:'));
    }
  }
}
const other=$('[data-live-key="kpi-output_throughput"]');
assert(other.textContent.includes('Distinct endpoint evidence.'));
assert(other.textContent.includes('Check distinct endpoint support.'));
assert.equal(JSON.stringify(summary),before);
''')
