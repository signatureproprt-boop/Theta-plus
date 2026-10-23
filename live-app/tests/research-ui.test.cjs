const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function environment(page='options',snapshot={}){
  const nodes=new Map(),calls=[],chartCalls=[];
  function element(id){if(!nodes.has(id)){let html='';const el={id,hidden:false,value:'',disabled:false,dataset:{},textContent:'',clientWidth:600,checked:true,
    setAttribute(){},addEventListener(t,fn){this['on'+t]=fn;},querySelector(){return element('submit');},reset(){},insertAdjacentHTML(_where,v){html+=v;}};
    Object.defineProperty(el,'innerHTML',{get:()=>html,set(v){html=v;}});nodes.set(id,el);}return nodes.get(id);}
  const data={server_time:'2026-10-19T07:00:00Z',history:{candles:[],invalid_record_ids:[],conflicting_minutes:0},chain:{status:'TIME_UNVERIFIED',contracts:[],pcr_oi:1.2,pcr_volume:1.4},setups:[],policies:{},availability:{},markets:{items:[]},...snapshot};
  const context={window:{addEventListener(){}},document:{getElementById:element,querySelectorAll(){return [];}},location:{hash:'#'+page},setInterval:()=>0,setTimeout:fn=>{fn();return 0;},Intl,Date,Number,String,Math,Set,Map,FormData:class{constructor(form){return Object.entries(form.fields||{});}},crypto:{randomUUID:()=> 'b'.repeat(32)},AbortSignal:{timeout:()=>undefined},console,fetch:async(url,o={})=>{calls.push({url,o});return {ok:true,status:200,text:async()=>JSON.stringify(url.includes('journal')?{entries:[],next_cursor:null}:data),json:async()=>url.includes('journal')?{entries:[],next_cursor:null}:data};}};
  context.window.thetaChartRender=(_element,model)=>chartCalls.push(model);context.window.thetaChartDispose=()=>{};context.window.thetaChartLive=()=>{};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/chart-data.js'),'utf8'),context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/api-response.js'),'utf8'),context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/research.js'),'utf8'),context);
  async function flush(){for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));}
  return {context,element,calls,flush,data,chartCalls};
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
  assert.equal(e.chartCalls.at(-1).last,undefined);
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
  assert.equal(e.chartCalls.at(-1).last,undefined);
  e.context.window.thetaResearchShow('options');
  e.context.window.thetaResearchShow('research');
  assert.match(e.element('page-research').innerHTML,/2\/3 closed minutes/);
});
test('complete provider bars render without deriving OHLC from live LTP',async()=>{
  const t=Date.parse('2026-10-19T03:45:00Z')/1000;
  const candles=Array.from({length:5},(_,i)=>({timestamp:t+i*60,open:100,high:102,low:99,close:101,volume:0}));
  const e=environment('research',{history:{candles}});await e.flush();
  assert.deepEqual(JSON.parse(JSON.stringify(e.chartCalls.at(-1).bars.filter(r=>r.close!==undefined).at(-1))),{time:t,open:100,high:102,low:99,close:101});
  assert.equal(e.chartCalls.at(-1).source,'Historical');
  e.context.window.thetaResearchTick({key:'nifty',ltp:999,trade_at:new Date().toISOString()});
  assert.deepEqual(JSON.parse(JSON.stringify(e.chartCalls.at(-1).bars.filter(r=>r.close!==undefined).at(-1))),{time:t,open:100,high:102,low:99,close:101});
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
test('missing spot never labels a median strike as ATM',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]}});await e.flush();
  const html=e.element('page-research').innerHTML;
  assert.match(html,/Spot unavailable/);assert.ok(!html.includes('<small>ATM</small>'));
});

test('session refresh binds selected security ID without a second workspace fetch',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]}});await e.flush();
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  e.context.window.thetaLiveIsActive=()=>true;
  let payload;e.context.window.thetaAdminRequest=async(_path,input)=>{payload=input;return {session_date:'2026-10-19',candles:[],expected_minutes:0,missing_minutes:0,freshness:{state:'MISSING'},received_at:'2026-10-19T07:00:00Z',workspace:{...e.data,setups:[{setup:'opening_range',state:'NO_TRADE',failed:['SPREAD_TOO_WIDE'],reason_codes:['SPREAD_TOO_WIDE']}]}};};
  const before=e.calls.length;await e.context.window.thetaSessionRefresh();await e.flush();
  assert.equal(payload.contract_id,'123');assert.equal(e.calls.length,before);
  assert.match(e.element('page-research').innerHTML,/Rule blocked/);
  assert.match(e.element('page-research').innerHTML,/spread to narrow/);
});
test('late session response cannot overwrite a newer contract or its setup checks',async()=>{
  const second={...contract,security_id:'456',side:'PE'};
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract,second]}});await e.flush();
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  e.context.window.thetaLiveIsActive=()=>true;
  let release;e.context.window.thetaAdminRequest=()=>new Promise(resolve=>{release=resolve;});
  const pending=e.context.window.thetaSessionRefresh();
  e.context.window.thetaResearchPickContract(second);await e.flush();
  release({session_date:'2026-10-19',candles:[],expected_minutes:0,missing_minutes:0,freshness:{state:'MISSING'},workspace:{chain:{contracts:[contract]},setups:[{setup:'opening_range',state:'CONFIRM',reason_codes:[]}]}});
  await pending;await e.flush();
  assert.equal(e.context.window.thetaResearchSnapshot().selected.security_id,'456');
  assert.ok(!e.element('page-research').innerHTML.includes('Trigger met'));
});

test('manual candle refresh works while socket is disconnected and does not start it',async()=>{
  const e=environment('research');await e.flush();let calls=0;
  e.context.window.thetaLiveIsActive=()=>false;
  e.context.window.thetaAdminRequest=async(path)=>{assert.equal(path,'research/session');calls++;return {session_date:'2026-10-19',candles:[],expected_minutes:0,missing_minutes:0,freshness:{state:'MISSING'}};};
  await e.context.window.thetaSessionRefresh();assert.equal(calls,0);
  await e.element('research-session-refresh').onclick();assert.equal(calls,1);
  assert.equal(e.element('research-session-refresh').disabled,false);
});
test('selected live observation updates separately, preserves risk assumptions and never promotes quote freshness',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]}});await e.flush();
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  const form=e.element('research-risk');form.fields={entry:'102',stop:'88'};form.oninput({currentTarget:form});
  e.context.window.thetaLiveIsActive=()=>true;
  const row={...contract,role:'option',underlying:'nifty',ltp:111,bid:110,ask:112,trade_at:new Date().toISOString(),received_at:new Date().toISOString(),trade_state:'RECENT_TRADE'};
  e.context.window.thetaResearchLiveBatch([row]);
  assert.match(e.element('selected-live-observation').innerHTML,/Recent trade · live socket/);
  assert.match(e.element('selected-live-observation').innerHTML,/111/);
  assert.match(e.element('selected-live-observation').innerHTML,/quote time unverified/);
  assert.equal(e.context.window.thetaResearchSnapshot().selected.freshness,'TIME_UNVERIFIED');
  assert.equal(form.fields.entry,'102');assert.equal(form.fields.stop,'88');
  e.context.window.thetaLiveIsActive=()=>false;e.context.window.thetaResearchLiveState();
  assert.match(e.element('selected-live-observation').innerHTML,/Retained \/ stale/);
  e.context.window.thetaResearchLiveReset();assert.match(e.element('selected-live-observation').innerHTML,/awaiting socket observation/);
});
test('mismatched option identity cannot populate the selected live observation',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]}});await e.flush();
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  e.context.window.thetaResearchLiveBatch([{...contract,role:'option',underlying:'nifty',side:'PE',ltp:999}]);
  assert.ok(!e.element('selected-live-observation').innerHTML.includes('999'));
});
test('failed contract refresh clears updating state and invalidates earlier confirmation',async()=>{
  const e=environment('research',{chain:{expiry:contract.expiry,contracts:[contract]},setups:[{setup:'opening_range',state:'CONFIRM',reason_codes:[]}]});await e.flush();
  e.context.window.thetaApiFetch=async()=>{throw Error('workspace unavailable');};
  e.context.window.thetaResearchPickContract(contract);await e.flush();
  const html=e.element('page-research').innerHTML;
  assert.ok(!html.includes('Updating contract checks'));assert.ok(!html.includes('Trigger met'));
  assert.match(html,/Workspace refresh failed/);assert.equal(e.element('research-message').textContent,'workspace unavailable');
});

test('transient workspace GET retries once, without replaying writes',async()=>{
  const e=environment('research');await e.flush();let calls=0;
  e.context.window.thetaApiFetch=async()=>{calls++;if(calls===1){const error=Error('temporary');error.status=500;throw error;}return {ok:true,text:async()=>JSON.stringify(e.data)};};
  await e.element('research-refresh').onclick();await e.flush();
  assert.equal(calls,2);assert.equal(e.element('research-message').textContent,'');
});
test('price trigger progress and strict missing evidence render separately beside explicit target planning',async()=>{
 const e=environment('research',{setups:[{setup:'opening_range',state:'DATA_UNAVAILABLE',missing:['EVENT_CONTEXT_UNAVAILABLE'],reason_codes:['EVENT_CONTEXT_UNAVAILABLE'],price_pattern:{state:'TRIGGER_OBSERVED',side:'CE',basis:'Closed underlying candles only',reason_codes:['FRESH_TRIGGER_OK']}}]});await e.flush();
 const html=e.element('page-research').innerHTML;
 assert.match(html,/Price trigger observed · research only/);assert.match(html,/Evidence eligibility/);assert.match(html,/WAIT · Data incomplete/);
 assert.match(html,/name="target"/);assert.match(html,/event-calendar source is not connected/);
});

test('live chart ticks reuse timezone formatters over a large retained history and refresh new candles',async()=>{
  const start=Date.parse('2026-10-01T03:45:00Z')/1000;
  const candles=Array.from({length:7500},(_,i)=>({timestamp:start+Math.floor(i/375)*86400+(i%375)*60,open:100,high:102,low:99,close:101}));
  const e=environment('research',{history:{candles}});await e.flush();
  let constructors=0;
  e.context.Intl={DateTimeFormat:class extends Intl.DateTimeFormat{constructor(...args){constructors++;super(...args);}}};
  e.context.window.thetaLiveIsActive=()=>true;
  for(let i=0;i<20;i++)e.context.window.thetaResearchTick({key:'nifty',ltp:101+i/100,trade_at:new Date().toISOString()});
  assert.equal(constructors,0,'live ticks must not build formatters for each historical minute');
  assert.equal(e.chartCalls.at(-1).last.close,101);
  const latest=start+19*86400;
  e.context.window.thetaAdminRequest=async()=>({session_date:'2026-10-20',candles:Array.from({length:5},(_,i)=>({timestamp:latest+i*60,open:110,high:112,low:109,close:111})),expected_minutes:5,missing_minutes:0,freshness:{state:'STALE'},received_at:new Date().toISOString()});
  await e.context.window.thetaSessionRefresh();
  assert.equal(e.chartCalls.at(-1).last.close,111);
});

 test('chart and indicator cards consume backend series rather than browser EMA or a retained session',async()=>{
  const time=Date.parse('2026-10-22T03:45:00Z')/1000;
  const bars=[{time,open:100,high:102,low:99,close:101}],ema20=[{time,value:88.123}],ema50=[{time,value:87.321}];
  const frame={bars,ema20,ema50,rsi14:[{time,value:62}],adx14:[{time,value:31}],snapshot:{ema20:88.123,ema50:87.321,rsi14:62,adx14:31,consecutive_bars:60,state:'CALCULATED'}};
  const e=environment('research',{indicator_chart:{version:'closed-candle-1.0',clock:'2026-10-22T04:00:00Z',frames:{'5':frame},levels:[],orb:{state:'COMPLETE',date:'2026-10-22',high:102,low:99},loadedDay:'2026-10-22',excluded:0,conflicts:0},history:{candles:[]},candle_freshness:{state:'STALE'}});
  await e.flush();const chart=e.chartCalls.at(-1);
  assert.deepEqual(JSON.parse(JSON.stringify(chart.ema20)),ema20);assert.deepEqual(JSON.parse(JSON.stringify(chart.rsi14)),frame.rsi14);assert.deepEqual(JSON.parse(JSON.stringify(chart.bars)),bars);
  assert.match(e.element('research-indicator-values').innerHTML,/HISTORICAL \/ STALE/);
  assert.match(e.element('research-indicator-values').innerHTML,/88.123|88.12/);
  assert.match(e.element('research-indicator-values').innerHTML,/RSI14/);
 });

test('live validator renders blocked checks with escaped evidence without provider writes',async()=>{
  const e=environment('quality',{data_validator:{state:'BLOCKED',checked_at:'2026-10-23T04:00:00Z',blocked_checks:1,checks:[{check:'nifty candles',state:'BLOCKED',detail:'<bad-source>',expected_minutes:45,observed_minutes:44,missing_minutes:1,conflicting_minutes:0,duplicate_rows:0}]}});
  await e.flush();const html=e.element('page-quality').innerHTML;
  assert.match(html,/Live data validator · BLOCKED/);assert.match(html,/44\/45 minutes/);
  assert.match(html,/&lt;bad-source&gt;/);assert.doesNotMatch(html,/<bad-source>/);
  assert.ok(e.calls.every(c=>!c.o.method||c.o.method==='GET'));
});
