// Minimal DOM for node-vm renderer tests. No browser, socket or npm dependency.
const encode=value=>String(value).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
const decode=value=>value.replace(/&(amp|lt|gt|quot|#39);/g,(_,name)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[name]));
export function browserDOM(listeners) {
  let document;
  class Node {
    constructor(tag=null,text='') {this.nodeType=tag?1:3;this.tagName=tag?.toUpperCase();this.nodeValue=text;this.childNodes=[];this.attrs=new Map();this.parentNode=null;this.scrollTop=0;this.scrollLeft=0;this.writes=0;this.moves=0;this.stateMoves=0;this.equalChecks=0;this.animationCalls=[];this.animations=[];this.style={};this.visible=true;}
    get isConnected(){return this===document?.body || Boolean(this.parentNode?.isConnected);}
    get hidden(){return this.hasAttribute('hidden');}
    set hidden(value){if(value)this.setAttribute('hidden','');else this.removeAttribute('hidden');}
    contains(node){for(;node;node=node.parentNode)if(node===this)return true;return false;}
    getBoundingClientRect(){return {left:10,top:10,width:100,height:40};}
    getClientRects(){return this.isConnected&&this.visible&&!this.closest('[hidden]')&&!this.closest('details:not([open])')?[this.getBoundingClientRect()]:[];}
    isEqualNode(other){this.equalChecks++;return this.nodeType===other.nodeType&&this.tagName===other.tagName&&this.nodeValue===other.nodeValue&&this.attrs.size===other.attrs.size&&this.attributes.every(({name,value})=>other.getAttribute(name)===value)&&this.childNodes.length===other.childNodes.length&&this.childNodes.every((child,i)=>child.isEqualNode(other.childNodes[i]));}
    animate(keyframes,options){
      let finish,reject;
      const animation={effect:{target:this},keyframes,options,playState:'running',finished:new Promise((resolve,fail)=>{finish=resolve;reject=fail;}),finish(){this.playState='finished';finish(this);},cancel(){this.playState='idle';reject(Error('Animation cancelled'));}};
      this.animationCalls.push(animation);this.animations.push(animation);return animation;
    }
    getAnimations({subtree=false}={}){return [...this.animations.filter(a=>a.playState==='running'),...(subtree?this.childNodes.flatMap(child=>child.getAnimations({subtree:true})):[])];}
    get opacity(){const animation=this.getAnimations().at(-1);return animation?animation.keyframes[0].opacity:1;}
    get firstChild(){return this.childNodes[0] || null;}
    get nextSibling(){return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this)+1] || null;}
    get attributes(){return [...this.attrs].map(([name,value])=>({name,value}));}
    get id(){return this.getAttribute('id') || '';}
    get dataset(){return Object.fromEntries([...this.attrs].filter(([name])=>name.startsWith('data-')).map(([name,value])=>[name.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()),value]));}
    get classList(){const self=this;return {contains:name=>(self.getAttribute('class') || '').split(' ').includes(name),add(name){if(!this.contains(name))self.setAttribute('class',`${self.getAttribute('class') || ''} ${name}`.trim());},remove(name){self.setAttribute('class',(self.getAttribute('class') || '').split(' ').filter(item=>item!==name).join(' '));},toggle(name,on){if(on)this.add(name);else this.remove(name);}};}
    get open(){return this.hasAttribute('open');}
    set open(value){if(value)this.setAttribute('open','');else this.removeAttribute('open');}
    get innerHTML(){return this.childNodes.map(node=>node.outerHTML).join('');}
    set innerHTML(html){this.writes++;const root=this.tagName==='TEMPLATE'?this.content:this;for(const child of [...root.childNodes])root.removeChild(child);parse(html,root);}
    get outerHTML(){return this.nodeType===3?encode(this.nodeValue):`<${this.tagName.toLowerCase()}${this.attributes.map(({name,value})=>` ${name}="${encode(value)}"`).join('')}>${this.innerHTML}</${this.tagName.toLowerCase()}>`;}
    get textContent(){return this.nodeType===3?this.nodeValue:this.childNodes.map(node=>node.textContent).join('');}
    set textContent(value){for(const child of [...this.childNodes])this.removeChild(child);this.insertBefore(new Node(null,value),null);}
    getAttribute(name){return this.attrs.get(name) ?? null;}
    setAttribute(name,value){this.attrs.set(name,String(value));}
    hasAttribute(name){return this.attrs.has(name);}
    removeAttribute(name){this.attrs.delete(name);}
    insertBefore(child,before){if(child.parentNode){child.moves++;child.parentNode.removeChild(child);}const index=before?this.childNodes.indexOf(before):this.childNodes.length;if(index<0)throw Error('Not a child');this.childNodes.splice(index,0,child);child.parentNode=this;return child;}
    moveBefore(child,before){if(child.parentNode!==this)throw Error('Only sibling moves supported by this fixture');child.moves++;child.stateMoves++;this.childNodes.splice(this.childNodes.indexOf(child),1);const index=before?this.childNodes.indexOf(before):this.childNodes.length;this.childNodes.splice(index,0,child);}
    removeChild(child){if(child.isConnected&&child.contains(document.activeElement))document.activeElement=document.body;const index=this.childNodes.indexOf(child);if(index<0)throw Error('Not a child');this.childNodes.splice(index,1);child.parentNode=null;return child;}
    matches(selector){
      if(this.nodeType!==1)return false;
      if(selector.includes(','))return selector.split(',').some(part=>this.matches(part.trim()));
      const excluded=[...selector.matchAll(/:not\(([^)]+)\)/g)];if(excluded.some(([,part])=>this.matches(part)))return false;selector=selector.replace(/:not\([^)]+\)/g,'');
      const attrs=[...selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)];
      if(attrs.some(([,name,value])=>!this.hasAttribute(name)||value!==undefined&&this.getAttribute(name)!==value))return false;
      const base=selector.replace(/\[[^\]]*\]/g,''),id=base.match(/#([\w-]+)/)?.[1],tag=base.match(/^[\w-]+/)?.[0];
      return (!id||this.id===id)&&(!tag||this.tagName===tag.toUpperCase())&&[...base.matchAll(/\.([\w-]+)/g)].every(([,name])=>this.classList.contains(name));
    }
    closest(selector){for(let node=this;node;node=node.parentNode)if(node.matches(selector))return node;return null;}
    querySelectorAll(selector){
      const parts=selector.trim().split(/\s+/),last=parts.pop(),found=[];
      const visit=node=>{for(const child of node.childNodes){if(child.matches(last)){let ancestor=child.parentNode,ok=true;for(const part of [...parts].reverse()){if(part==='>')continue;while(ancestor&&!ancestor.matches(part))ancestor=ancestor.parentNode;if(!ancestor){ok=false;break;}ancestor=ancestor.parentNode;}if(ok)found.push(child);}visit(child);}};visit(this);return found;
    }
    querySelector(selector){return this.querySelectorAll(selector)[0] || null;}
    focus(options){if(options?.preventScroll!==true)throw Error('Focus must preserve scroll');document.activeElement=this;}
    showModal(){this.open=true;}
    close(){this.open=false;}
  }
  function parse(html,parent){
    const stack=[parent];
    for(const token of html.match(/<[^>]*>|[^<]+/g) || []){
      if(token.startsWith('</')){stack.pop();continue;}
      if(token.startsWith('<')){
        const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;
        const element=new Node(tag);
        for(const [,name,a,b,c] of token.slice(tag.length+1,-1).matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g))element.setAttribute(name,decode(a ?? b ?? c ?? ''));
        stack.at(-1).insertBefore(element,null);
        if(!token.endsWith('/>')&&!['hr','input','br','img','meta','link'].includes(tag))stack.push(element);
      }else stack.at(-1).insertBefore(new Node(null,decode(token)),null);
    }
  }
  const body=new Node('body');
  document={body,activeElement:null,createElement(tag){const node=new Node(tag);if(tag==='template')node.content=new Node('fragment');return node;},querySelector:selector=>body.querySelector(selector),querySelectorAll:selector=>body.querySelectorAll(selector),addEventListener:(kind,fn)=>listeners[kind]=fn};
  body.innerHTML='<main id="main"></main><a id="active-run-link"></a><ol id="rail-runs"></ol><section id="rail-recent"></section><div id="connection-error"></div><section id="report-sections" hidden></section><dialog id="test-dialog"></dialog><dialog id="request-dialog"></dialog><div id="spot-tip"></div>';
  return document;
}
