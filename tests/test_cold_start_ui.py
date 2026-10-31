"""Cold-start UI behavior without a browser server or socket binding."""

from pathlib import Path

from tests.test_ux_setup import run_browser_logic


APP = Path(__file__).parents[1] / "src/giraffe/static/app.js"


def test_reasoning_output_and_failed_warm_measurements_are_labelled():
    run_browser_logic(r'''
const html=renderColdStart({observations:{targets:{local:{restart:{readiness_ms:25,
first_request:{first_output_ms:5,elapsed_ms:20,first_output_kind:'reasoning'},
warm_requests:[{first_output_ms:2,elapsed_ms:12,first_output_kind:'reasoning'},
{first_output_ms:null,elapsed_ms:null,reason:'HTTP 503 <bad>'}]}}}}});
assert.equal(html.split('First output was reasoning; no answer within the output cap.').length-1,2);
assert.match(html,/Not measured/);assert.match(html,/HTTP 503 &lt;bad&gt;/);
assert.doesNotMatch(html,/<bad>/);
''')


def test_script_row_reuses_connection_pattern_and_hides_when_disallowed():
    run_browser_logic(r"""
const config={restart_script_timeout_seconds:600},target={restart_script:'/tmp/restart.sh'};
state.bootstrap.restart_allowed=true;
let html=renderRestartScriptRow(target,config);
assert.match(html,/<details class="connection-auth" id="restart-script-row">/);
assert.match(html,/<summary class="api-key-row">/);
assert.match(html,/<span class="api-key-label">Restart script \(optional\)<\/span>/);
assert.match(html,/when-closed">Edit/);assert.match(html,/when-open">Close/);
assert.match(html,/name="restart_script_timeout_seconds"[^>]*value="600"/);
assert.match(html,/class="field-info"/);
assert.match(html,/name="restart_script"/);
assert.match(html,/>Script path<\/label>/);
assert.match(html,/placeholder="\/path\/to\/restart.sh"/);
assert.equal(html.split('Restart script (optional)').length-1,1);
state.bootstrap.restart_allowed=false;
html=renderRestartScriptRow(target,config);
assert.doesNotMatch(html,/name="restart_script"/);
assert.match(html,/--allow-restart-script/);assert.match(html,/class="field-info"/);
assert.equal(renderRestartConfirmation({targets:[target]}),'');
state.bootstrap.restart_allowed=true;
assert.equal(renderRestartConfirmation({targets:[{}]}),'');
""")


def test_connection_rows_share_wider_desktop_label_column_and_original_phone_rule():
    css = APP.with_name("styles.css").read_text()
    assert ".connection-auth > .api-key-row { display: grid; grid-template-columns: 176px minmax(0, 1fr) auto;" in css
    assert "@media (max-width: 599px) {\n  .connection-auth > .api-key-row { grid-template-columns: auto minmax(0, 1fr) auto; gap: 12px; }" in css


def test_confirmation_is_explicit_ephemeral_and_invalidated_by_config_or_path_changes():
    run_browser_logic(r"""
state.bootstrap.restart_allowed=true;
state.config={targets:[{name:'local',restart_script:'/tmp/restart.sh'}]};
const input=element({value:'/tmp/restart.sh'}),checkbox=element({checked:false});
nodes['#new-run-form']={elements:{restart_script:input}};
nodes['#confirm-restart-script']=checkbox;
renderRequestPreview=updateBounds=()=>{};
assert.equal(restartConfirmed(),false);
assert.match(renderRestartConfirmation(state.config),/disabled/);
state.restartPreview={status:'current',route:state.routeVersion,requestedPath:'/tmp/restart.sh',path:'/tmp/restart.sh',machine:'mac',sha256:'abc',contents:'#!/bin/sh\ntrue'};
assert.equal(restartConfirmed(),false);
let html=renderRestartConfirmation(state.config);
assert.doesNotMatch(html,/checked/);assert.match(html,/Restart the server with this script before testing/);
assert.match(html,/Full path/);assert.match(html,/Machine/);assert.match(html,/SHA-256/);
listeners.change({target:{name:'confirm_restart_script',checked:true}});
assert.equal(state.restartConfirmedHash,'abc');assert.equal(restartConfirmed(),true);
assert.match(renderRestartConfirmation(state.config),/checked/);
configChanged();assert.equal(state.restartConfirmedHash,null);assert.equal(checkbox.checked,false);
listeners.change({target:{name:'confirm_restart_script',checked:true}});
input.value='/tmp/other.sh';assert.equal(restartConfirmed(),false);
input.value='/tmp/restart.sh';state.routeVersion++;assert.equal(restartConfirmed(),false);
state.restartPreview.route=state.routeVersion;state.restartConfirmedHash='abc';
invalidateRestartPreview();assert.equal(state.restartPreview,null);assert.equal(restartConfirmed(),false);
""")


def test_preview_is_escaped_and_discards_inflight_stale_response():
    run_browser_logic(r"""
state.bootstrap.restart_allowed=true;
const input=element({value:'/tmp/restart.sh'});
nodes['#new-run-form']={elements:{restart_script:input}};
nodes['#restart-script-preview']=element();
nodes['#field-restart_script-error']=element();
nodes['#field-restart_script']=input;
updateBounds=()=>{};
const malicious={path:'/tmp/<script>.sh',machine:'<img src=x>',sha256:'"hash',contents:'#!/bin/sh\n</pre><script>alert(1)</script>&'};
let resolve;api=()=>new Promise(done=>resolve=done);
let pending=previewRestartScript();
input.value='/tmp/new.sh';invalidateRestartPreview();resolve(malicious);await pending;
assert.equal(state.restartPreview,null);assert.equal(nodes['#restart-script-preview'].innerHTML,'');
api=async()=>malicious;await previewRestartScript();
const html=nodes['#restart-script-preview'].innerHTML+renderRestartConfirmation({targets:[{restart_script:'/tmp/new.sh'}]});
assert.doesNotMatch(html,/<script>|<img src=x>|onclick=/);
assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img src=x&gt;/);
assert.match(html,/&lt;\/pre&gt;/);assert.match(html,/&amp;/);
api=async()=>{throw new Error('Restart script must be executable.');};await previewRestartScript();
assert.equal(state.restartConfirmedHash,null);
assert.equal(nodes['#field-restart_script-error'].textContent,'Restart script must be executable.');
assert.equal(nodes['#field-restart_script-error'].hidden,false);
assert.equal(input.attributes['aria-invalid'],'true');
""")


def test_run_payload_only_carries_hash_after_current_confirmation():
    run_browser_logic(r"""
state.bootstrap.restart_allowed=true;
state.config={targets:[{name:'local',model:'m',url:'http://server/v1',restart_script:'/tmp/restart.sh'}],checks:[]};
state.bootstrap.config=state.config;
const values={name:'local',model:'m',url:'http://server/v1',restart_script:'/tmp/restart.sh',restart_script_timeout_seconds:'23'};
const elements=Object.fromEntries(Object.entries(values).map(([name,value])=>[name,{value}]));
nodes['#new-run-form']={elements,querySelectorAll:()=>[]};
FormData=class {constructor(){this.values={...values};}has(name){return Object.hasOwn(this.values,name);}get(name){return this.values[name]??null;}set(name,value){this.values[name]=value;}};
tokenizerChoice=()=>null;
configureRunLimits=configureTests=configureLoadTest=configureJudging=configureProfiles=configureSessions=config=>config;
loadFormDefaults=()=>({});
let payload=readForm(true);
assert.equal(payload.mode,'normal');assert.equal(payload.restart_target,null);assert.equal(payload.restart_script_sha256,undefined);
assert.equal(payload.config.restart_script_timeout_seconds,23);
state.restartPreview={status:'current',route:state.routeVersion,requestedPath:values.restart_script,sha256:'abc'};
state.restartConfirmedHash='abc';payload=readForm(true);
assert.equal(payload.mode,'cold-start');assert.equal(payload.restart_target,'local');assert.equal(payload.restart_script_sha256,'abc');
state.restartPreview.status='error';payload=readForm(true);
assert.equal(payload.mode,'normal');assert.equal(payload.restart_script_sha256,undefined);
values.restart_script='';elements.restart_script.value='';payload=readForm(true);
assert.equal(payload.mode,'normal');assert.equal(payload.config.targets[0].restart_script,null);
assert.equal(payload.restart_script_sha256,undefined);
""")


def test_slow_resident_request_never_becomes_load_evidence_and_results_escape_output():
    run_browser_logic(r"""
assert.equal(renderColdStart({observations:{targets:{local:{}}}}),'');
const restart={script_ms:5,service_answered_ms:30,readiness_ms:9000,first_request:{first_output_ms:8000,elapsed_ms:8500},warm_requests:[{first_output_ms:3,elapsed_ms:8},{first_output_ms:4,elapsed_ms:9}],model_load:'Model was already loaded',model_loaded_before_first_request:'Yes',measurement_api:"Measured through Ollama's native API",output_tail:'</pre><script>bad()</script>'};
const html=renderColdStart({observations:{targets:{local:{restart}}}});
assert.match(html,/<h2>Cold start<\/h2>/);assert.match(html,/Model was already loaded/);
assert.doesNotMatch(html,/cold load|reported by Ollama|<script>/);
assert.match(html,/Measured through Ollama&#39;s native API/);
assert.match(html,/Follow-up 1/);assert.match(html,/Follow-up 2/);
assert.match(html,/&lt;script&gt;/);assert.match(html,/class="field-info"/);
assert.match(html,/Some servers load weights at startup, in which case it&#39;s part of &quot;Service answered after&quot;\./);
""")


def test_restart_uses_existing_live_preparation_card_and_replaced_controls_are_removed():
    run_browser_logic(r"""
const detail={id:'run',state:'running',events:[{target:'local',scenario:'restart_script',preparation:{status:'running',step:'Running restart script',elapsed_seconds:2}}]};
const html=renderPreparation(detail);
assert.match(html,/class="run-preparation"/);assert.match(html,/<h2>Running restart script<\/h2>/);
assert.match(html,/class="preparation-elapsed"/);
""")
    source=APP.read_text()
    assert "selectField('mode','Server startup'" not in source
    assert "selectField('restart_target'" not in source
    assert "restart_command" not in source
    assert "updateRestart" not in source
    assert source.index("${renderRestartScriptRow(target,c)}") < source.index('id="setup-panel-2"')


def test_run_again_clears_confirmation_but_keeps_script_configuration():
    run_browser_logic(r"""
state.config={config_version:2,targets:[{name:'local',restart_script:'/tmp/restart.sh'}],checks:[]};
state.detail={report:{manifest:{config:state.config}}};
state.restartConfirmedHash='previous';
state.restartPreview={status:'current',requestedPath:'/tmp/restart.sh',route:state.routeVersion,sha256:'previous'};
invalidateModelDiscovery=renderRequestPreview=()=>{};
await rerun();
assert.equal(state.config.targets[0].restart_script,'/tmp/restart.sh');
assert.equal(state.config.restart_target,null);assert.equal(state.restartConfirmedHash,null);
assert.equal(state.restartPreview,null);assert.equal(restartConfirmed(),false);
""")


def test_saved_draft_never_persists_or_restores_confirmation():
    run_browser_logic(r"""
state.bootstrap.config={config_version:2,targets:[{name:'local',restart_script:'/tmp/restart.sh'}],checks:[]};
state.config=structuredClone(state.bootstrap.config);
state.restartConfirmedHash='must-not-save';
state.restartPreview={sha256:'must-not-save',contents:'must-not-save-script-contents'};
captureFormDraft=()=>{};
let saved;
sessionStorage={setItem:(key,value)=>saved=value,getItem:()=>saved,removeItem(){}};
saveDraft();assert(saved.includes('/tmp/restart.sh'));assert(!saved.includes('must-not-save'));
await restoreDraft();
assert.equal(state.config.targets[0].restart_script,'/tmp/restart.sh');
assert.equal(state.restartConfirmedHash,null);assert.equal(state.restartPreview,null);
""")
