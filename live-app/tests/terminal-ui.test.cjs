const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function env(){
 const nodes=new Map(),events={},windowEvents={},calls=[],selected=[];
 const node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',listeners:{},addEventListener(n,fn){this.listeners[n]=fn;}});return nodes.get(id);};
 const payload={items:[{key:'nifty',label:'NIFTY 50',group:'India',price:22888.95,status:'STALE',quote_at:'2026-10-19T09:59:00Z'},{key:'gold',label:'Gold <img src=x>',group:'Commodity',price:null,status:'NO_QUOTE'}],fetched_at:'2026-10-20T12:00:00Z'};
 const context={document:{getElementById:node,addEventListener(n,fn){events[n]=fn;}},window:{addEventListener(n,fn){windowEvents[n]=fn;},thetaApiFetch:async(url,options)=>{calls.push({url,options});return {ok:true,payload:url.includes('collection')?{jobs:[{state:'running',rows_saved:15,counts:{failed:2},config:{families:['daily']}}]}:payload};},thetaReadResponse:async r=>r.payload,thetaResearchShow(){},thetaResearchSelectSymbol(v){selected.push(v);}},location:{hash:'#home'},AbortSignal:{timeout:()=>undefined},Intl,Date,console};
 vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/terminal.js'),'utf8'),context);
 return {node,calls,events,windowEvents,selected,context,flush:async()=>{for(let i=0;i<6;i++)await new Promise(r=>setImmediate(r));}};
}
test('compact terminal reads saved observations without starting provider, collection or orders',async()=>{
 const e=env();await e.flush();assert.equal(e.calls.length,2);assert.ok(e.calls.every(c=>c.options.method==='GET'));
 assert.match(e.node('terminal-ticker').innerHTML,/STALE/);assert.match(e.node('terminal-watch-list').innerHTML,/&lt;img/);
 assert.doesNotMatch(e.node('terminal-watch-list').innerHTML,/<img/);assert.match(e.node('page-home').innerHTML,/full historical coverage/);
 assert.match(e.node('page-home').innerHTML,/Broker execution is disabled/);
});
test('watch search and instrument selection do not fabricate a chart for unsupported instruments',async()=>{
 const e=env();await e.flush();e.node('terminal-watch-search').listeners.input({target:{value:'gold'}});
 assert.match(e.node('terminal-watch-list').innerHTML,/Gold/);assert.doesNotMatch(e.node('terminal-watch-list').innerHTML,/NIFTY 50/);
 e.events.click({target:{closest:()=>({dataset:{terminalDetail:'gold'}})}});assert.match(e.node('terminal-watch-status').textContent,/NO_QUOTE/);assert.equal(e.selected.length,0);
 e.events.click({target:{closest:()=>({dataset:{terminalSymbol:'nifty'}})}});assert.equal(e.selected[0],'nifty');
});
test('only explicit market refresh sends one provider POST',async()=>{
 const e=env();await e.flush();const button={disabled:false};await e.node('terminal-market-refresh').listeners.click({target:button});
 const post=e.calls.filter(c=>c.options.method==='POST');assert.equal(post.length,1);assert.equal(post[0].url,'/api/markets/refresh');assert.equal(button.disabled,false);
});
