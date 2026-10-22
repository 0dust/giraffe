"""Minimal serialization changes required by backend run-limit semantics."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_legacy_session_storage_draft_uses_the_config_io_boundary_before_restore():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute draft restoration')
    path = Path('src/giraffe/static/app.js').resolve()
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
let stored=JSON.stringify({bootstrap:{concurrency:4},config:{targets:[{name:'one',model:'m'}],checks:['correctness'],concurrency:40}});
const context={assert,structuredClone,sessionStorage:{getItem(){return stored},setItem(key,value){stored=value},removeItem(){throw Error('Legacy draft was discarded')}},
 document:{querySelector(){return null},addEventListener(){}},window:{addEventListener(){}}};
const source=fs.readFileSync(PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
(async()=>{
state.bootstrap={config:{config_version:2,concurrency:null,max_requests:null,max_duration_seconds:null}};
const sent=[];
api=async(path,options)=>{
 sent.push({path,config:JSON.parse(options.body)});
 return {config:{...JSON.parse(options.body),config_version:2,default_concurrency:40,max_requests:null,max_duration_seconds:null},migration_notes:['Legacy concurrency preserves parallelism']};
};
await restoreDraft();
assert.equal(sent.length,1);assert.equal(sent[0].path,'/api/config/draft');
assert.equal(sent[0].config.config_version,undefined);
assert.equal(state.config.config_version,2);assert.equal(state.config.default_concurrency,40);
assert.equal(state.config.max_requests,null);assert.equal(state.config.max_duration_seconds,null);
assert.deepEqual(state.migrationNotes,['Legacy concurrency preserves parallelism']);
saveDraft();
await restoreDraft();assert.equal(sent.length,1);
assert.equal(state.config.concurrency,40);assert.equal(state.config.max_requests,null);
})()
`,context);
'''.replace('PATH', json.dumps(str(path)))
    result = subprocess.run([node, '--input-type=module', '-'], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
