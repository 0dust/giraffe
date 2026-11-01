"""Restored setup DOM fixture and the backend baseline migration contract."""

import httpx
from giraffe.web import create_app


# Give the existing renderer DOM fixture normal form-control behavior. Application
# rendering, configuration serialization, profile selection and discovery stay real.
SETUP = r'''
const proto=Object.getPrototypeOf(document.body);
Object.defineProperties(proto,{
 name:{get(){return this.getAttribute('name')||'';}},
 type:{get(){return this.getAttribute('type')||'';}},
 checked:{get(){return this.hasAttribute('checked');},set(v){if(v)this.setAttribute('checked','');else this.removeAttribute('checked');}},
 value:{get(){if(this._value!==undefined)return this._value;if(this.tagName==='SELECT'){const option=this.querySelector('option[selected]')||this.querySelector('option');return option?.getAttribute('value')||'';}return this.getAttribute('value')??(this.tagName==='TEXTAREA'?this.textContent:'');},set(v){this._value=String(v);}},
 elements:{get(){return Object.fromEntries(this.querySelectorAll('[name]').map(node=>[node.name,node]));}},
 previousElementSibling:{get(){return this.parentNode?.childNodes.filter(n=>n.nodeType===1)[this.parentNode.childNodes.filter(n=>n.nodeType===1).indexOf(this)-1]||null;}}
});
proto.setCustomValidity=function(value){this.validationMessage=value;};
proto.checkValidity=function(){return !this.validationMessage;};proto.reportValidity=proto.checkValidity;
proto.focus=function(){document.activeElement=this;};
FormData=class {
 constructor(form){this.rows=[];for(const input of form.querySelectorAll('[name]'))if(!['checkbox','radio'].includes(input.type)||input.checked)this.rows.push([input.name,input.value||(input.type==='checkbox'?'on':'')]);}
 get(name){return this.rows.find(row=>row[0]===name)?.[1]??null;}
 has(name){return this.rows.some(row=>row[0]===name);}
 getAll(name){return this.rows.filter(row=>row[0]===name).map(row=>row[1]);}
 set(name,value){this.rows=this.rows.filter(row=>row[0]!==name);this.rows.push([name,value]);}
};
state.config=structuredClone(bootstrap.defaults);state.config.targets=[{name:'local',url:'http://current/v1',model:'current-model'}];
bootstrap.config=structuredClone(state.config);state.jsonDraft=state.jsonBase='synced';
state.baselines=[{id:'base',name:'Baseline'}];state.prefillBaseline='base';
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=watchPlanViewport=scheduleTokenizerSelection=renderTokenizerSelection=()=>{};
renderNew();
'''


async def test_matching_legacy_baseline_preserves_load_at_launch(tmp_path):
    archived = {"targets": [{"name": "old", "url": "http://old/v1", "model": "old-model"}],
                "checks": ["access", "capacity"], "request_timeout_seconds": 15}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                 base_url="http://localhost") as client:
        response = await client.post("/api/config/draft", json=archived)
    assert response.status_code == 200
    normalized = response.json()
    assert "load" in normalized["config"]["checks"]
