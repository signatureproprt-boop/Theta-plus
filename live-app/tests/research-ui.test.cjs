const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function environment(page='options',snapshot={}){
  const nodes=new Map(),calls=[];
  function element(id){if(!nodes.has(id)){let html='';const el={id,hidden:false,value:'',disabled:false,dataset:{},textContent:'',clientWidth:600,checked:true,
    setAttribute(){},addEventListener(t,fn){this['on'+t]=fn;},querySelector(){return element('submit');},reset(){},insertAdjacentHTML(_where,v){html+=v;}};
    Object.defineProperty(el,'innerHTML',{get:()=>html,set(v){html=v;}});nodes.set(id,el);}return nodes.get(id);}
  const data={server_time:'2026-10-19T07:00:00Z',history:{candles:[],invalid_record_ids:[],conflicting_minutes:0},chain:{status:'TIME_UNVERIFIED',contracts:[],pcr_oi:1.2,pcr_volume:1.4},setups:[],policies:{},availability:{},markets:{items:[]},...snapshot};
  const context={window:{addEventListener(){}},document:{getElementById:element,querySelectorAll(){return [];}},location:{hash:'#'+page},setInterval:()=>0,Intl,Date,Number,String,Math,Set,Map,FormData:class{constructor(form){return Object.entries(form.fields||{});}},crypto:{randomUUID:()=> 'b'.repeat(32)},AbortSignal:{timeout:()=>undefined},console,fetch:async(url,o={})=>{calls.push({url,o});return {ok:true,status:200,text:async()=>JSON.stringify(url.includes('journal')?{entries:[],next_cursor:null}:data),json:async()=>url.includes('journal')?{entries:[],next_cursor:null}:data};}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/api-response.js'),'utf8'),context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/research.js'),'utf8'),context);
  async function flush(){for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));}
  return {context,element,calls,flush,data};
}

test('options screen labels snapshot PCR and unverified history without polling provider',async()=>{
  const e=environment();await e.flush();const html=e.element('page-options').innerHTML;
  assert.match(html,/OI PCR/);assert.match(html,/Continuous cadence unverified/);
  assert.match(html,/TIME_UNVERIFIED/);assert.match(html,/does not establish continuous/i);
  assert.equal(e.calls.length,1);assert.equal(e.calls[0].url,'/api/research/workspace?symbol=nifty&contract_id=');
  assert.ok(e.calls.every(c=>!c.url.includes('/api/collection/')&&!c.o.method?.includes('POST')));
});
test('missing data still renders research chart and unavailable message',async()=>{
  const e=environment('research');await e.flush();
  assert.match(e.element('research-chart-readout').textContent,/No complete bars/);
  assert.match(e.element('page-research').innerHTML,/Saved-data freshness above/);
});
test('session refresh replaces today bars, excludes older today snapshots, and retains gap labels',async()=>{
  const t=Date.parse('2026-10-19T03:45:00Z')/1000;
  const bar=(timestamp,close)=>({timestamp,open:close,high:close,low:close,close,volume:0});
  const e=environment('research',{history:{candles:[bar(t,999)]}});await e.flush();
  e.context.window.thetaLiveIsActive=()=>true;
  e.context.window.thetaAdminRequest=async()=>({session_date:'2026-10-19',candles:[bar(t,100),bar(t+120,102)],expected_minutes:3,missing_minutes:1,freshness:{state:'FRESH'},received_at:'2026-10-19T03:48:00Z'});
  await e.context.window.thetaSessionRefresh();
  assert.match(e.element('research-session-state').textContent,/2\/3 closed minutes · 1 gaps/);
  e.context.document.querySelectorAll=()=>[];
  // Five-minute bucket is intentionally incomplete, so it must remain absent.
  assert.match(e.element('research-chart-readout').textContent,/No complete bars/);
  e.context.window.thetaResearchShow('options');
  e.context.window.thetaResearchShow('research');
  assert.match(e.element('page-research').innerHTML,/2\/3 closed minutes/);
});
test('complete provider bars render without deriving OHLC from live LTP',async()=>{
  const t=Date.parse('2026-10-19T03:45:00Z')/1000;
  const candles=Array.from({length:5},(_,i)=>({timestamp:t+i*60,open:100,high:102,low:99,close:101,volume:0}));
  const e=environment('research',{history:{candles}});await e.flush();
  assert.match(e.element('research-chart').innerHTML,/O 100 H 102 L 99 C 101/);
  assert.match(e.element('research-chart-readout').textContent,/Historical/);
  e.context.window.thetaResearchTick({key:'nifty',ltp:999,trade_at:new Date().toISOString()});
  assert.match(e.element('research-chart').innerHTML,/O 100 H 102 L 99 C 101/);
});
test('backend chain strings cannot inject markup',async()=>{
  const e=environment('options',{chain:{status:'<script>bad</script>',contracts:[]}});await e.flush();
  const html=e.element('page-options').innerHTML;
  assert.ok(!html.includes('<script>bad</script>'));assert.match(html,/&lt;script&gt;/);
});
test('risk and replay gates remain visible without fabricated metrics',async()=>{
  const e=environment('risk');await e.flush();
  assert.match(e.element('page-risk').innerHTML,/daily_loss_cap/);
  assert.match(e.element('page-risk').innerHTML,/costs_source/);
  e.context.window.thetaResearchShow('replay');
  assert.match(e.element('page-replay').innerHTML,/Underlying replay is not an option P&L backtest/);
  assert.match(e.element('page-replay').innerHTML,/Not available until execution data passes/);
});
test('changing research pages removes duplicate control IDs from the old page',async()=>{
  const e=environment('options');await e.flush();assert.match(e.element('page-options').innerHTML,/research-symbol/);
  e.context.window.thetaResearchShow('quality');
  assert.equal(e.element('page-options').innerHTML,'');
  assert.match(e.element('page-quality').innerHTML,/research-symbol/);
});
test('notes are explicit observations and Admin auth is required for writes',async()=>{
  const e=environment('paper');await e.flush();
  assert.match(e.element('page-paper').innerHTML,/do not count toward the 50-trade gate/);
  const form=e.element('research-note');form.fields={note:'observation'};
  await form.onsubmit({preventDefault(){},target:form});
  assert.ok(!e.calls.some(c=>c.o.method==='POST'));
  assert.ok(e.element('research-message').textContent);
});

const contract={security_id:'123',expiry:'2026-10-27',strike:23000,side:'CE',lot_size:65,identity_verified:true,freshness:'TIME_UNVERIFIED',last_price:101,ask:102,bid:100,oi:300,delta_oi:50};
test('Trading Room contains chart, paired snapshot chain, reasons and shared risk form',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,status:'TIME_UNVERIFIED',spot:23000,contracts:[contract,{...contract,side:'PE',security_id:'124'},{...contract,expiry:'2026-11-03',security_id:'999'}]},setups:[{setup:'opening_range',state:'DATA_UNAVAILABLE',rule_version:'test',reason_codes:['RANGE_BREAK_OK','OPTION_TIME_UNVERIFIED']}]});await e.flush();
  const html=e.element('page-research').innerHTML;
  assert.match(html,/id="research-chart"/);assert.match(html,/CALL · CE/);assert.match(html,/PUT · PE/);assert.match(html,/id="research-risk"/);
  assert.match(html,/Closed 5m candle ne range/);assert.match(html,/quote time verified/);
  assert.ok(!html.includes('data-contract="999"'));
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  assert.equal(e.context.location.hash,'#research');assert.equal(e.context.window.thetaResearchSnapshot().selected.security_id,'123');
  assert.match(e.element('page-research').innerHTML,/name="entry"[^>]*value="102"/);
  assert.ok(e.calls.some(c=>c.url.endsWith('contract_id=123')));
});
test('contract falling outside snapshot stays pinned but loses quote eligibility',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]}});await e.flush();
  e.context.window.thetaResearchPickContract(contract);await e.flush();e.data.chain.contracts=[];
  await e.element('research-refresh').onclick();await e.flush();
  const selected=e.context.window.thetaResearchSnapshot().selected;
  assert.equal(selected.security_id,'123');assert.equal(selected.ask,null);assert.equal(selected.freshness,'UNAVAILABLE');
  assert.match(e.element('page-research').innerHTML,/absent from this snapshot/);
});
test('risk assumptions survive a saved refresh and new contract clears them',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]}});await e.flush();
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  let cleared=0;e.context.window.thetaPaperPlanClear=()=>cleared++;
  const form=e.element('research-risk');form.fields={entry:'99',stop:'89',risk_rupees:'1000'};
  form.oninput({currentTarget:form});await e.element('research-refresh').onclick();await e.flush();
  assert.match(e.element('page-research').innerHTML,/name="stop"[^>]*value="89"/);
  e.context.window.thetaResearchPickContract({...contract,security_id:'456'});await e.flush();
  assert.ok(!e.element('page-research').innerHTML.includes('value="89"'));assert.ok(cleared>=2);
});
test('late risk response cannot restore a paper plan after assumptions change',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]}});await e.flush();
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  let release,plans=0;e.context.window.thetaPaperPlanReady=()=>plans++;
  e.context.window.thetaApiFetch=()=>new Promise(resolve=>{release=resolve;});
  e.context.window.thetaReadResponse=async()=>({state:'SIZED',lots:1,quantity:65,planned_stop_loss:650,estimated_costs:0,premium_paid:6500,reason_codes:[],warning:'estimate'});
  const form=e.element('research-risk');form.fields={entry:'100',stop:'90',risk_rupees:'1000'};
  const pending=form.onsubmit({preventDefault(){},target:form});
  form.fields.stop='80';form.oninput({currentTarget:form});release({ok:true});await pending;
  assert.equal(plans,0);assert.match(e.element('research-risk-result').textContent,/Assumptions changed/);
});
test('switching setups preserves the room form and uses the selected explanation',async()=>{
  const e=environment('research',{setups:[{setup:'opening_range',state:'WAIT',reason_codes:['RETEST_PENDING']},{setup:'trend_pullback',state:'WAIT',reason_codes:['PULLBACK_PENDING']}]});await e.flush();
  const setup=e.element('room-setup');setup.value='trend_pullback';setup.onchange({target:setup});
  const html=e.element('page-research').innerHTML;assert.match(html,/15m EMA20 par pullback/);
  assert.equal((html.match(/id="research-risk"/g)||[]).length,1);
  assert.ok(!html.includes('Range edge par retest ka wait'));
});
