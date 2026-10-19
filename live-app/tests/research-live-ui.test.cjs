const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function environment(messages,authorized=true){
  const nodes=new Map(),calls=[],timers=[];
  function node(id){if(!nodes.has(id))nodes.set(id,{id,value:id==='research-symbol'?'nifty':'',textContent:'',innerHTML:'',hidden:false,disabled:false,clientWidth:600,style:{},setAttribute(){}});return nodes.get(id);}
  const text=messages.map(x=>'data: '+JSON.stringify(x)+'\n\n').join('');const encoded=new TextEncoder().encode(text);let read=0;
  const context={document:{getElementById:node},window:{addEventListener(){}},AbortController,TextDecoder,Date,Number,Math,String,Map,Set,console,setInterval:fn=>timers.push(fn),setTimeout:()=>0};
  if(authorized)context.window.thetaLiveStreamRequest=async(symbol,signal)=>{calls.push({symbol,signal});return {ok:true,body:{getReader:()=>({read:async()=>read++===0?{value:encoded.slice(0,17),done:false}:read===2?{value:encoded.slice(17),done:false}:{done:true},cancel:async()=>{}})}};};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/research-live.js'),'utf8'),context);
  return {context,node,calls,timers,flush:async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));}};
}

test('fragmented stream renders real tick fields and provider disconnect stops renewals',async()=>{
  const at=new Date().toISOString();const e=environment([{type:'batch',received_at:at,ticks:[{key:'nifty',segment:'IDX_I',security_id:'13',label:'NIFTY',role:'underlying',ltp:25000,trade_at:at,trade_state:'RECENT_TRADE'}]},{type:'status',state:'PROVIDER_DISCONNECT',code:805}]);
  e.node('research-live-start').onclick();await e.flush();
  assert.equal(e.calls.length,1);assert.equal(e.calls[0].symbol,'nifty');
  assert.match(e.node('research-live-prices').innerHTML,/25,000/);
  assert.match(e.node('research-live-state').textContent,/PROVIDER_DISCONNECT/);
  assert.equal(e.node('research-live-start').disabled,false);
});
test('Start live without Admin bridge fails closed and never opens a provider connection',async()=>{
  const e=environment([],false);e.node('research-live-start').onclick();await e.flush();
  assert.equal(e.calls.length,0);e.context.window.thetaLiveStop();
  assert.match(e.node('research-live-state').textContent,/Disconnected/);
});
test('Stop aborts request and retained observations remain labelled disconnected',async()=>{
  const e=environment([]);e.node('research-live-start').onclick();await e.flush();e.context.window.thetaLiveStop();
  assert.ok(e.calls[0].signal.aborted);assert.match(e.node('research-live-state').textContent,/not live/);
});
