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
  const context={window:{addEventListener(){}},document:{getElementById:element,querySelectorAll(){return [];}},location:{hash:'#'+page},Intl,Date,Number,String,Math,Set,Map,FormData:class{constructor(form){return Object.entries(form.fields||{});}},crypto:{randomUUID:()=> 'b'.repeat(32)},AbortSignal:{timeout:()=>undefined},console,fetch:async(url,o={})=>{calls.push({url,o});return {ok:true,json:async()=>url.includes('journal')?{entries:[],next_cursor:null}:data};}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/research.js'),'utf8'),context);
  async function flush(){for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));}
  return {context,element,calls,flush};
}

test('options screen labels snapshot PCR and unverified history without polling provider',async()=>{
  const e=environment();await e.flush();const html=e.element('page-options').innerHTML;
  assert.match(html,/OI PCR/);assert.match(html,/Continuous cadence unverified/);
  assert.match(html,/TIME_UNVERIFIED/);assert.match(html,/does not establish continuous/i);
  assert.equal(e.calls.length,1);assert.equal(e.calls[0].url,'/api/research/workspace?symbol=nifty');
  assert.ok(e.calls.every(c=>!c.url.includes('/api/collection/')&&!c.o.method?.includes('POST')));
});
test('missing data still renders research chart and unavailable message',async()=>{
  const e=environment('research');await e.flush();
  assert.match(e.element('research-chart-readout').textContent,/No complete bars/);
  assert.match(e.element('page-research').innerHTML,/Saved-data freshness above/);
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
