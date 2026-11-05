"""Cold-start UI behavior without a browser server or socket binding."""

from pathlib import Path

from tests.test_ux_setup import run_browser_logic


APP = Path(__file__).parents[1] / "src/giraffe/static/app.js"


def test_reasoning_output_and_failed_warm_measurements_are_labelled():
    run_browser_logic(r'''
const html=renderColdStart({observations:{targets:{local:{restart:{readiness_ms:25,
first_request:{first_output_ms:5,elapsed_ms:20,first_output_kind:'reasoning',answer_present:false,output_cap_exhausted:true},
warm_requests:[{first_output_ms:2,elapsed_ms:12,first_output_kind:'reasoning',answer_present:false,output_cap_exhausted:true},
{first_output_ms:null,elapsed_ms:null,reason:'HTTP 503 <bad>'}]}}}}});
assert.equal(html.split('First output was reasoning; no answer within the output cap.').length-1,2);
assert.match(html,/Not measured/);assert.match(html,/HTTP 503 &lt;bad&gt;/);
assert.doesNotMatch(html,/<bad>/);
''')


def test_reasoning_before_an_answer_or_without_cap_evidence_is_not_labelled_missing():
    run_browser_logic(r'''
for(const evidence of [{answer_present:true,output_cap_exhausted:false},
 {answer_present:true,output_cap_exhausted:true},
 {answer_present:false,output_cap_exhausted:false},{}]){
 const item={first_output_ms:5,elapsed_ms:20,first_output_kind:'reasoning',...evidence};
 const html=renderColdStart({observations:{targets:{local:{restart:{
  first_request:item,warm_requests:[item]}}}}});
 assert.doesNotMatch(html,/no answer within the output cap/);
 assert.equal(html.split('First output was reasoning.').length-1,2);
}
''')


def test_model_load_values_omit_repeated_label_without_changing_json_evidence():
    run_browser_logic(r'''
for(const value of ['Model load: 1.38 s, reported by Ollama','Not reported by this server','Model was already loaded']){
 const report={observations:{targets:{local:{restart:{model_load:value}}}}},original=JSON.stringify(report);
 const html=renderColdStart(report);
 assert.match(html,/<dt>Model load<button/);
 assert(html.includes('<dd>'+value.replace(/^Model load: /,'')+'</dd>'));
 assert.doesNotMatch(html,/<dd>Model load: /);
 assert.equal(JSON.stringify(report),original);
}
''')


def test_retried_first_request_explains_why_it_is_not_cold_and_old_runs_are_unchanged():
    run_browser_logic(r'''
const report={observations:{targets:{local:{restart:{first_request:{first_output_ms:5,elapsed_ms:20},warm_requests:[{first_output_ms:2,elapsed_ms:12}]}}}}};
const first=report.observations.targets.local.restart.first_request,original=renderColdStart(report);
assert.match(original,/<th scope="row">First request<\/th>/);
assert.doesNotMatch(original,/cold_not_cold|retry, not cold/);
first.cold=true;first.attempt=1;assert.equal(renderColdStart(report),original);
first.cold=false;first.attempt=2;first.not_cold_reason='Retried after HTTP 503 <busy>.';
const help=infoHelp('cold_not_cold_0','First request',first.not_cold_reason),html=renderColdStart(report);
assert(html.includes('<th scope="row">First request (retry, not cold)'+help+'</th>'));
assert.match(html,/Retried after HTTP 503 &lt;busy&gt;\./);assert.doesNotMatch(html,/<busy>/);
assert.equal(html.replace('First request (retry, not cold)'+help,'First request'),original);
''')


def test_restart_target_selection_controls_preview_confirmation_and_payload():
    run_browser_logic(r'''
state.bootstrap.restart_allowed=true;
state.config={targets:[{name:'first',model:'m',url:'http://first/v1',restart_script:'/tmp/first.sh'},
 {name:'second <server>',model:'m',url:'http://second/v1',restart_script:'/tmp/second.sh'},
 {name:'no-script',model:'m',url:'http://third/v1'}],checks:[]};
state.bootstrap.config=state.config;
const values={name:'first',model:'m',url:'http://first/v1',restart_script:'/tmp/first.sh'};
const elements=Object.fromEntries(Object.entries(values).map(([name,value])=>[name,element({value})]));
nodes['#new-run-form']={elements,querySelectorAll:()=>[]};
nodes['#confirm-restart-script']=element();
FormData=class {constructor(){this.values={...values};}has(name){return Object.hasOwn(this.values,name);}get(name){return this.values[name]??null;}set(name,value){this.values[name]=value;}};
tokenizerChoice=()=>null;
configureRunLimits=configureTests=configureLoadTest=configureJudging=configureProfiles=configureSessions=config=>config;
loadFormDefaults=()=>({});renderRequestPreview=()=>{};
let rendered;updateBounds=()=>{rendered=renderRestartConfirmation(readForm(true).config);};
const paths=[];api=async(url,options)=>{const path=JSON.parse(options.body).path;paths.push(path);return {path,machine:'local',sha256:path,contents:'script '+path};};
let html=renderRestartConfirmation(state.config);
assert.match(html,/<label[^>]*>Endpoint to restart/);assert.match(html,/<select[^>]*name="restart_target"/);
assert.match(html,/<option value="first" selected>first<\/option>/);
assert.match(html,/<option value="second &lt;server&gt;" >second &lt;server&gt;<\/option>/);
assert.doesNotMatch(html,/no-script/);
await previewRestartScript();listeners.change({target:{name:'confirm_restart_script',checked:true}});
assert.equal(readForm(true).restart_target,'first');assert.equal(restartConfirmed(),true);
const selection={name:'restart_target',value:'second <server>',closest:()=>true,matches:()=>false};
listeners.input({target:selection});
listeners.change({target:selection});
assert.equal(state.restartConfirmedHash,null);assert.equal(nodes['#confirm-restart-script'].checked,false);
assert.equal(readForm(true).mode,'normal');
await Promise.resolve();
assert.equal(state.restartTarget,'second <server>');assert.equal(paths.at(-1),'/tmp/second.sh');
assert.equal(currentRestartPreview().path,'/tmp/second.sh');assert.match(rendered,/script \/tmp\/second.sh/);
assert.equal(restartScriptPath(),'/tmp/first.sh');assert.equal(elements.restart_script.value,'/tmp/first.sh');
listeners.change({target:{name:'confirm_restart_script',checked:true}});
const payload=readForm(true);
assert.equal(payload.mode,'cold-start');assert.equal(payload.restart_target,'second <server>');
assert.equal(payload.restart_script_sha256,'/tmp/second.sh');
assert.match(renderRestartConfirmation(structuredClone(state.config)),/<option value="second &lt;server&gt;" selected>/);
listeners.change({target:{name:'restart_script'}});await Promise.resolve();
assert.equal(paths.at(-1),'/tmp/first.sh');assert.equal(currentRestartPreview(),null);
assert.equal(state.restartTarget,'second <server>');assert.equal(state.restartConfirmedHash,null);
let resolveFirst,resolveSecond;
api=(url,options)=>new Promise(done=>{if(JSON.parse(options.body).path==='/tmp/first.sh')resolveFirst=done;else resolveSecond=done;});
listeners.change({target:{name:'restart_target',value:'first'}});
listeners.change({target:selection});
resolveSecond({path:'/tmp/second.sh',sha256:'second',contents:'second script'});await Promise.resolve();
resolveFirst({path:'/tmp/first.sh',sha256:'first',contents:'first script'});await Promise.resolve();
assert.equal(currentRestartPreview().sha256,'second');assert.match(rendered,/second script/);
listeners.change({target:{name:'confirm_restart_script',checked:true}});
state.config.targets[1].restart_script=null;
assert.doesNotMatch(renderRestartConfirmation(state.config),/<select/);assert.equal(state.restartTarget,'first');
assert.equal(restartConfirmed(),false);
''')


def test_only_restart_candidate_on_second_endpoint_needs_no_selector():
    run_browser_logic(r'''
state.bootstrap.restart_allowed=true;
state.config={targets:[{name:'first',model:'m',url:'http://first/v1'},
 {name:'second',model:'m',url:'http://second/v1',restart_script:'/tmp/second.sh'}],checks:[]};
state.bootstrap.config=state.config;
const values={name:'first',model:'m',url:'http://first/v1',restart_script:''};
const elements=Object.fromEntries(Object.entries(values).map(([name,value])=>[name,element({value})]));
nodes['#new-run-form']={elements,querySelectorAll:()=>[]};
FormData=class {constructor(){this.values={...values};}has(name){return Object.hasOwn(this.values,name);}get(name){return this.values[name]??null;}set(name,value){this.values[name]=value;}};
tokenizerChoice=()=>null;
configureRunLimits=configureTests=configureLoadTest=configureJudging=configureProfiles=configureSessions=config=>config;
loadFormDefaults=()=>({});updateBounds=()=>{};
const html=renderRestartConfirmation(state.config);
assert.match(html,/id="review-restart"/);assert.doesNotMatch(html,/<select|Endpoint to restart/);
api=async(url,options)=>{assert.equal(JSON.parse(options.body).path,'/tmp/second.sh');return {sha256:'second',contents:'second script'};};
await previewRestartScript();
assert.equal(currentRestartPreview().sha256,'second');
listeners.change({target:{name:'confirm_restart_script',checked:true}});
const payload=readForm(true);
assert.equal(payload.mode,'cold-start');assert.equal(payload.restart_target,'second');
assert.equal(payload.restart_script_sha256,'second');assert.equal(payload.config.targets[0].restart_script,null);
''')


def test_applying_or_importing_config_resets_restart_target_and_confirmation():
    run_browser_logic(r'''
state.bootstrap.restart_allowed=true;
const config={targets:[{name:'first',restart_script:'/tmp/first.sh'},
 {name:'second',restart_script:'/tmp/second.sh'}],checks:[]};
state.config=structuredClone(config);
captureFormDraft=invalidateModelDiscovery=renderRequestPreview=toast=()=>{};
configError=error=>{if(error)throw error;};
let rendered;renderNew=()=>{assert.equal(state.restartTarget,null);rendered=renderRestartConfirmation(state.config);};
api=async()=>({config:structuredClone(config)});
for(const importing of [false,true]){
 state.restartTarget='second';state.restartConfirmedHash='previous';
 state.restartPreview={status:'current',requestedPath:'/tmp/second.sh',route:state.routeVersion,sha256:'previous'};
 await applyConfig(JSON.stringify(config),{importing});
 assert.equal(state.restartTarget,'first');assert.equal(state.restartConfirmedHash,null);
 assert.equal(state.restartPreview,null);assert.equal(state.config.restart_target,null);
 assert.match(rendered,/<option value="first" selected>/);assert.doesNotMatch(rendered,/ checked/);
}
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
assert.equal(html.split('id="field-restart_script-info"').length-1,1);
assert.equal(html.split('Giraffe runs this script').length-1,1);
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


def test_restart_help_and_docs_explain_review_output_and_inherited_server_handles():
    run_browser_logic(r'''
state.bootstrap.restart_allowed=true;
const html=renderRestartScriptRow({restart_script:'/tmp/restart.sh'},{restart_script_timeout_seconds:600});
assert.match(html,/Only this file is reviewed; anything it calls is not\./);
assert.match(html,/Don&#39;t print secrets: output is saved in the report\./);
assert.match(html,/class="field-info"/);
const result=renderColdStart({observations:{targets:{local:{restart:{output_tail:'[redacted]'}}}}});
assert.match(result,/\[redacted\]/);
''')
    root = APP.parents[3]
    for name in ("README.md", "docs/running.md"):
        docs = (root / name).read_text()
        assert "Only this file is reviewed; anything it calls is not. Don't print secrets: output is saved in the report." in docs
        assert "output handles" in docs and "restart.log" in docs and "0600" in docs


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
    assert "restart_command" not in source
    assert "updateRestart" not in source
    assert source.index("${renderRestartScriptRow(target,c)}") < source.index('id="setup-panel-2"')


def test_run_again_clears_confirmation_but_keeps_script_configuration():
    run_browser_logic(r"""
state.config={config_version:2,targets:[{name:'local',restart_script:'/tmp/restart.sh'}],checks:[]};
state.detail={report:{manifest:{config:state.config}}};
state.restartConfirmedHash='previous';
state.restartTarget='local';
state.restartPreview={status:'current',requestedPath:'/tmp/restart.sh',route:state.routeVersion,sha256:'previous'};
invalidateModelDiscovery=renderRequestPreview=()=>{};
await rerun();
assert.equal(state.config.targets[0].restart_script,'/tmp/restart.sh');
assert.equal(state.config.restart_target,null);assert.equal(state.restartConfirmedHash,null);
assert.equal(state.restartTarget,null);
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
