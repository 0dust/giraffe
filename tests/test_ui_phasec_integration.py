"""Real control paths for Phase C condition evidence and telemetry settings."""

from tests.test_ui_conditions import DIALOG_SETUP
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks
from tests.test_ux_setup import run_browser_logic


def test_condition_row_keeps_later_page_number_and_existing_request_evidence():
    run_render_checks(DIALOG_SETUP+r"""
(async()=>{
 remaining=757;aggregate={...aggregate,planned:757,started:757,running:757};draw();
 openConditionDialog([...conditionViews.keys()][0]);await settle();
 $('#condition-page-number').value='76';listeners.keydown({target:$('#condition-page-number'),key:'Enter',preventDefault(){}});await settle();
 const record={id:'r751',target:'local',scenario:'short',status:'completed',valid:true,score:true,output:'Saved answer',output_chars:12,evaluation:{messages:[{role:'user',content:'Saved input'}],scorer:'contains',expected:'answer',expected_provided:true}};
 state.detail.report=report([check('access','pass')],{requests:[record]});
 await listeners.click({target:$('[data-condition-request="r751"]'),preventDefault(){}});
 const dialog=$('#request-dialog');assert(dialog.open);assert($('#test-dialog').open);
 assert.match(dialog.textContent,/Request #0751 of 757 · Short input \/ short output/);assert.doesNotMatch(dialog.textContent,/Request 1 of 1/);
 assert.match(dialog.textContent,/What was sent/);assert.match(dialog.textContent,/Saved input|Saved answer/);
 closeRequestEvidence();assert($('#test-dialog').open);assert.equal(conditionDialog.page,76);assert.equal(document.activeElement,$('[data-condition-request="r751"]'));
})()
""", dom=True)


def test_host_exporter_field_is_optional_labeled_and_explains_its_scope():
    browser_logic(r"""
config.checks=['gpu'];config.targets[0].host_metrics_url='http://node:9100/metrics';
const markup=renderTestCard(bootstrap.checks.find(check=>check.id==='gpu'),config);
assert.match(markup,/name="host_metrics_url" type="url" value="http:\/\/node:9100\/metrics"/);
assert.match(markup,/Host metrics exporter URL \(optional\)/);assert.match(markup,/server-wide CPU and host RAM/);assert.match(markup,/inference credentials are not forwarded/);
assert.doesNotMatch(markup,/name="host_metrics_url"[^>]*required/);
""")


def test_host_exporter_setting_survives_form_snapshot_and_can_be_cleared():
    run_browser_logic(r"""
state.bootstrap={...state.bootstrap,config:{traffic:{}},session_workloads:{defaults:{}},checks:[]};
state.config={targets:[{name:'local',model:'test',url:'http://localhost/v1',host_metrics_url:null}],checks:[],limits:{},concurrency:4,max_requests:10,max_duration_seconds:30};
const values=new Map([['url','http://localhost/v1'],['model','test'],['name','local']]);
FormData=class {get(key){return values.get(key) ?? null;} has(key){return values.has(key);} set(key,value){values.set(key,value);}};
const exporter={value:'  http://node:9100/metrics  '};
nodes['#new-run-form']={elements:{metrics_url:{value:''},host_metrics_url:exporter},querySelectorAll:()=>[]};
configureTests=configureLoadTest=configureJudging=configureProfiles=configureSessions=c=>c;
let payload=readForm();assert.equal(payload.config.targets[0].host_metrics_url,'http://node:9100/metrics');assert.equal(state.config.targets[0].host_metrics_url,null);
exporter.value='';payload=readForm();assert.equal(payload.config.targets[0].host_metrics_url,null);
""")


def test_chart_pointer_uses_one_floating_tooltip_and_native_svg_hidden_attribute():
    run_render_checks(r"""
state.detail={id:'pointer',state:'completed'};
const rows=[{number:1,request_id:'a',values:{response_latency_ms:1000}},{number:2,request_id:'b',values:{response_latency_ms:2500}}];
$('#main').innerHTML=renderPerformanceLineChart('timing','Timing','Recorded',rows,[{key:'response_latency_ms',label:'Response latency',color:'var(--accent)',scale:.001,unit:'s'}]);
const svg=$('[data-performance-chart="timing"]'),crosshair=$('.performance-crosshair',svg);assert(crosshair.hasAttribute('hidden'));
listeners.pointermove({target:svg,clientX:110,clientY:30});assert(!crosshair.hasAttribute('hidden'));
const tooltip=$('#performance-chart-tooltip');assert(!tooltip.hidden);assert.match(tooltip.textContent,/Request #0002 · b/);assert.match(tooltip.textContent,/2.5 s/);
listeners.pointerout({target:svg,relatedTarget:document.body});assert(tooltip.hidden);
""", dom=True)
