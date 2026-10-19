const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function environment(messages,authorized=true){
  const nodes=new Map(),calls=[],timers=[],timeouts=[];
  function node(id){if(!nodes.has(id))nodes.set(id,{id,value:id==='research-symbol'?'nifty':'',textContent:'',innerHTML:'',hidden:false,disabled:false,clientWidth:600,style:{},setAttribute(){}});return nodes.get(id);}
  const text=messages.map(x=>'data: '+JSON.stringify(x)+'\n\n').join('');const encoded=new TextEncoder().encode(text);let read=0;
  const context={document:{getElementById:node},window:{addEventListener(){}},AbortController,TextDecoder,Date,Number,Math,String,Map,Set,console,setInterval:fn=>timers.push(fn),setTimeout:(fn,ms)=>{timeouts.push({fn,ms});return timeouts.length;}};
  if(authorized)context.window.thetaLiveStreamRequest=async(symbol,signal)=>{calls.push({symbol,signal});return {ok:true,body:{getReader:()=>({read:async()=>read++===0?{value:encoded.slice(0,17),done:false}:read===2?{value:encoded.slice(17),done:false}:{done:true},cancel:async()=>{}})}};};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/api-response.js'),'utf8'),context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/research-live.js'),'utf8'),context);
  return {context,node,calls,timers,timeouts,flush:async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));}};
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

test('busy lease waits and retries; Stop cancels pending retry without stealing stream',async()=>{
  const e=environment([{type:'status',state:'STREAM_BUSY',retry_after_seconds:5}]);
  e.node('research-live-start').onclick();await e.flush();
  assert.match(e.node('research-live-state').textContent,/retrying in 5s/);
  assert.equal(e.node('research-live-stop').disabled,false);
  assert.equal(e.timeouts[0].ms,5000);
  e.timeouts.shift().fn();await e.flush();
  assert.equal(e.calls.length,2);
  e.context.window.thetaLiveStop();
  for(const t of e.timeouts)t.fn();await e.flush();
  assert.equal(e.calls.length,2);assert.match(e.node('research-live-state').textContent,/Disconnected/);
});
test('late response from stopped generation cannot overwrite new connection',async()=>{
  const e=environment([]);let complete,cancelled=false;
  e.context.window.thetaLiveStreamRequest=()=>new Promise(resolve=>{complete=resolve;});
  e.node('research-live-start').onclick();e.context.window.thetaLiveStop();
  complete({ok:true,body:{cancel:async()=>{cancelled=true;},getReader:()=>{throw Error('stale reader used');}}});
  await e.flush();assert.ok(cancelled);
  assert.match(e.node('research-live-state').textContent,/Disconnected/);
});
test('busy retries expire instead of waiting forever on another active tab',async()=>{
  const e=environment([{type:'status',state:'STREAM_BUSY'}]);let now=1000;
  e.context.Date=class extends Date {static now(){return now;}};
  e.node('research-live-start').onclick();await e.flush();
  // A subsequent HTTP conflict after the bounded wait is still handled safely.
  now+=331000;e.context.window.thetaLiveStreamRequest=async()=>({ok:false,status:409,json:async()=>({detail:'busy'})});
  e.timeouts.shift().fn();await e.flush();
  assert.equal(e.node('research-live-start').disabled,false);
  assert.match(e.node('research-live-state').textContent,/Stop that tab/);
});

test('option batches reach the selected-option bridge and new connection clears old observations',async()=>{
  const tick={key:'123',underlying:'nifty',segment:'NSE_FNO',security_id:'123',role:'option',identity_verified:true,ltp:100};
  const e=environment([{type:'batch',ticks:[tick],received_at:new Date().toISOString()}]);
  let received=[],reset=0;e.context.window.thetaResearchLiveBatch=rows=>received.push(...rows);e.context.window.thetaResearchLiveReset=()=>reset++;
  e.node('research-live-start').onclick();await e.flush();
  assert.equal(reset,1);assert.equal(received[0].security_id,'123');
  e.context.window.thetaLiveStop();
});
