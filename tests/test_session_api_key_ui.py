"""Exercise credential entry and async connection invalidation in the shipped JS."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_tab_key_scope_clearing_and_inflight_connection_changes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser behavior checks")
    script_path = Path(__file__).parents[1] / "src/giraffe/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes={}, listeners={};
const context={structuredClone,nodes,listeners,assert,
  document:{querySelector:q=>nodes[q],addEventListener:(event,fn)=>listeners[event]=fn},
  window:{addEventListener(){}}};
const source=fs.readFileSync(SCRIPT_PATH,'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
(async()=>{
  const form={elements:{}};
  for(const [name,value] of Object.entries({url:'http://server/v1',model:'test',name:'local',api_key:'',api_key_env:''})){
    form.elements[name]={name,value,id:'field-'+name,setCustomValidity(){},checkValidity(){return true;},
      matches(){return false;},closest(q){return q==='#new-run-form'?form:null;}};
  }
  nodes['#new-run-form']=form;nodes['#cli-fallback']={hidden:true};nodes['#model-options']={open:true};
  state.config={targets:[{name:'local',url:'http://server/v1',model:'test',api_key_env:null}]};
  state.jsonBase=state.jsonDraft='same';
  configChanged=()=>{};updateBounds=()=>{};renderModelDiscovery=()=>{};
  function input(name,value){form.elements[name].value=value;listeners.input({target:form.elements[name]});}
  assert.equal(apiKeyFor(undefined),'');
  input('api_key','tab-secret');
  assert.equal(apiKeyFor('http://server/v1'),'tab-secret');
  assert.equal(apiKeyFor('http://different/v1'),'');
  assert(!JSON.stringify(state.config).includes('tab-secret'));
  assert(!modelDiscoveryKey().includes('tab-secret'));
  const credentials=sessionAPIKeys(state.config);
  assert.equal(credentials[0].target,'local');assert.equal(credentials[0].url,'http://server/v1');
  assert.equal(credentials[0].api_key,'tab-secret');
  assert.equal(sessionAPIKeys({targets:[{name:'other',url:'http://different/v1'}]}).length,0);
  let sent,finish;
  api=async(path,options)=>{sent=JSON.parse(options.body);return new Promise(resolve=>finish=resolve);};
  const probe=testConnection();
  assert.equal(sent.api_key,'tab-secret');
  input('api_key','replacement');
  finish({status:'connected',model:'stale-response',latency_ms:1});await probe;
  assert.equal(form.elements.model.value,'test');assert.equal(state.modelDiscovery,null);
  input('api_key_env','OPENAI_API_KEY');
  assert.equal(apiKeyFor('http://server/v1'),'');assert.equal(form.elements.api_key.value,'');
  input('api_key','new-secret');assert.equal(form.elements.api_key_env.value,'');
  input('url','http://different/v1');
  assert.equal(apiKeyFor('http://server/v1'),'');assert.equal(form.elements.api_key.value,'');
  input('api_key','another-secret');clearAPIKey();
  assert.equal(sessionAPIKey,null);assert.equal(form.elements.api_key.value,'');
})();`,context);
""".replace("SCRIPT_PATH", json.dumps(str(script_path)))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
