const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function environment(current='risk',selected=null){
  const nodes=new Map(),calls=[];let state={current,selected,symbol:'nifty',data:{indicators:{'5':{rsi14:55,adx14:20}},collection:{jobs:[]}}};
  function create(id){if(!nodes.has(id)){let html='';const e={id,dataset:{},addEventListener(k,fn){this['on'+k]=fn;},insertAdjacentHTML(_,v){this.innerHTML+=v;}};Object.defineProperty(e,'innerHTML',{get:()=>html,set(v){html=v;for(const m of v.matchAll(/id="([^"]+)"/g))create(m[1]);}});nodes.set(id,e);}return nodes.get(id);}
  create('workflow-aside');if(current==='risk')create('paper-plan-result');if(current==='paper')create('paper-trade-panel');
  const context={document:{getElementById:id=>nodes.get(id)||null,querySelectorAll:()=>[]},window:{thetaResearchSnapshot:()=>state,thetaResearchShow:name=>{state.current=name;calls.push({show:name});},thetaAdminRequest:async(path,body)=>{calls.push({path,body});return {trades:[],recorded_closed_trades:0,validated_trades:0,hypothetical_net_pnl:0,qualification:'Manual hypothetical only'};}},location:{hash:''},Number,String,Date,console};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/research-workflow.js'),'utf8'),context);
  return {context,nodes,calls,flush:async()=>{for(let i=0;i<5;i++)await new Promise(r=>setImmediate(r));}};
}
test('only a sized risk plan offers the paper-journal transition',()=>{
  const e=environment();e.context.window.thetaPaperPlanReady({plan:{state:'NO_TRADE'}});
  assert.match(e.nodes.get('paper-plan-result').innerHTML,/blocked/);
  e.context.window.thetaPaperPlanReady({plan:{state:'SIZED',lots:2},risk:{},contract:{identity_verified:true}});
  e.nodes.get('paper-use-plan').onclick();assert.equal(e.context.location.hash,'#paper');
  assert.ok(e.calls.some(c=>c.show==='paper'));assert.ok(!e.calls.some(c=>c.path==='research/paper'));
});
test('paper ledger reads use the Admin bridge and do not create entries or fills',async()=>{
  const e=environment('paper');await e.flush();
  assert.equal(e.calls[0].path,'research/paper/list');
  assert.match(e.nodes.get('paper-trade-panel').innerHTML,/Manual hypothetical/);
  assert.match(e.nodes.get('paper-trade-panel').innerHTML,/validated: 0/);
  assert.equal(e.calls.length,1);
});
test('contract labels cannot inject markup and momentum is context only',()=>{
  const e=environment('research',{strike:100,side:'<script>unsafe</script>',expiry:'2026-10-19',lot_size:75});
  const html=e.nodes.get('workflow-aside').innerHTML;
  assert.ok(!html.includes('<script>unsafe</script>'));assert.match(html,/&lt;script&gt;/);
  assert.match(html,/RSI\(14\) 55/);assert.match(html,/not an accuracy score/);
});
