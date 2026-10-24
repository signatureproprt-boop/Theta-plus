const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function environment({fail=false,initialHash='#home'}={}){
 const nodes=new Map(),events={},timers=[],calls=[],browsed=[],opened=[],storage=new Map();
 function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',events:{},querySelector(){return null;},addEventListener(name,fn){this.events[name]=fn;}});return nodes.get(id);}
 const data={catalogue_sha256:'a'.repeat(64),catalogue_rows:215256,catalogue_at:'2026-10-24T00:00:00Z',counts:{all:180000,cash:9000,indices:100,options:150000,futures:2000,currency:8000,commodities:10000},unresolved:22026,
 watchlist:[{key:'exact',symbol:'RELIANCE',display:'Reliance <img src=x onerror=alert(1)>',segment:'NSE_EQ',security_id:'2885',saved_quote:{price:200,volume:12,received_at:'2026-10-24T00:00:00Z'}},{key:'waiting',symbol:'UNKNOWN',segment:'BSE_EQ',security_id:'999',saved_quote:null}],scope:'Retained observations only'};
 const context={document:{getElementById:node,hidden:false},location:{hash:initialHash},URLSearchParams,Intl,Date,AbortSignal,console,
 setInterval(fn,ms){timers.push({fn,ms});},setTimeout:fn=>setImmediate(fn),window:{addEventListener(name,fn){events[name]=fn;},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
 thetaApiFetch:async(url,options)=>{calls.push({url,options});if(fail)throw Error('Store unavailable');return {payload:data};},thetaReadResponse:async r=>r.payload,
 thetaInstrumentBrowse(...args){browsed.push(args);},thetaInstrumentOpen(...args){opened.push(args);}}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/market-home.js'),'utf8'),context);
 return {node,calls,timers,context,browsed,opened,storage,async flush(){for(let i=0;i<8;i++)await new Promise(setImmediate);},click(dataset={},attribute=''){node('page-home').events.click({target:{closest:()=>({dataset,hasAttribute:key=>key===attribute})}});}};
}

test('Home shows real catalogue counts and received watchlist prices without invented account balances',async()=>{
 const e=environment();await e.flush();const html=e.node('page-home').innerHTML;
 assert.match(html,/2,15,256/);assert.match(html,/22,026/);assert.match(html,/200/);assert.match(html,/Not recorded yet/);
 assert.match(html,/Reliance &lt;img/);assert.doesNotMatch(html,/<img/);
 assert.doesNotMatch(html,/My Portfolio|Top Up|Margin Available/);
 assert.equal(e.calls.length,1);assert.match(e.calls[0].url,/\/api\/instruments\/overview/);assert.equal(e.calls[0].options.method,undefined);
});

test('Home category/exchange and watchlist actions preserve full-catalogue scope and exact identity',async()=>{
 const e=environment();await e.flush();e.click({homeBrowse:'commodities'});e.click({homeSegment:'BSE_EQ'});e.click({homeInstrument:'exact'});
 assert.deepEqual(e.browsed[0],['commodities']);assert.deepEqual(e.browsed[1],['all','','BSE_EQ']);
 assert.equal(e.opened[0][0].security_id,'2885');assert.equal(e.opened[0][1],'a'.repeat(64));
 e.click({homeInstrument:'not-in-master'});assert.equal(e.opened.length,1);
});

test('watchlist storage is bounded and malformed input never becomes a catalogue query',async()=>{
 const e=environment();await e.flush();e.storage.set('theta-watchlist-v1','bad');assert.equal(e.context.window.thetaWatchKeys().length,0);
 e.storage.set('theta-watchlist-v1',JSON.stringify(['../secret','good',...Array.from({length:35},(_,i)=>'id'+i)]));
 const keys=e.context.window.thetaWatchKeys();assert.equal(keys.length,30);assert.equal(keys[0],'good');
});

test('Home refresh reads stored prices only and suspends polling when hidden',async()=>{
 const e=environment();await e.flush();assert.equal(e.timers[0].ms,15000);
 e.context.document.hidden=true;e.timers[0].fn();await e.flush();assert.equal(e.calls.length,1);
 e.context.document.hidden=false;e.timers[0].fn();await e.flush();assert.equal(e.calls.length,2);
 assert.ok(e.calls.every(c=>!c.options.method));
});

test('failed overview reads show unknown counts and a retry message rather than fake prices',async()=>{
 const e=environment({fail:true});await e.flush();const html=e.node('page-home').innerHTML;
 assert.match(html,/Store unavailable/);assert.match(html,/Watchlist unavailable/);assert.doesNotMatch(html,/2,15,256|200/);
});


test('shared watchlist loads and refreshes on research routes using saved reads only',async()=>{
 const e=environment({initialHash:'#research'});await e.flush();
 assert.equal(e.calls.length,1);assert.match(e.node('page-home').innerHTML,/Reliance &lt;img/);
 e.node('desk-market-watch').events.click({target:{closest:()=>({dataset:{homeInstrument:'exact'},hasAttribute:()=>false})}});
 assert.equal(e.opened[0][0].security_id,'2885');
 e.timers[0].fn();await e.flush();assert.equal(e.calls.length,2);
 assert.ok(e.calls.every(c=>!c.options.method));
});
