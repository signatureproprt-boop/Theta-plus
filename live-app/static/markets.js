'use strict';
let liveController=null, liveState='OFFLINE', liveRetry=null, liveRender=null, liveBackoff=5000;
let marketBoard=null, marketBusy=false, marketFetched=0;
const marketLabels={RECENT_TRADE:'Recent trade',STALE:'Stale / last session',TIME_UNAVAILABLE:'Quote time unavailable',NO_QUOTE:'No quote',FETCH_FAILED:'Feed error',NOT_IN_CATALOG:'No active instrument',NOT_FETCHED:'Not fetched',SEPARATE_FEED_REQUIRED:'Separate feed required',EXPIRED:'Expired contract'};
const compactLabels={RECENT_TRADE:'recent',STALE:'stale',TIME_UNAVAILABLE:'time N/A',NO_QUOTE:'no quote',FETCH_FAILED:'feed error',NOT_IN_CATALOG:'unavailable',NOT_FETCHED:'not fetched',SEPARATE_FEED_REQUIRED:'not connected',EXPIRED:'expired'};
function renderMarkets(){
 const positions=[...document.querySelectorAll('.market-line-scroll')].map(el=>el.scrollLeft);
 const rows=[['INDICES',r=>r.group==='India'||r.group==='Asia'],['COMMODITIES',r=>r.group==='Commodities'],['US MARKETS',r=>r.group==='US'||r.key==='dollar_index']];
 const items=marketBoard?.items||[];
 $('#market-strip').innerHTML=rows.map(([label,filter],i)=>`<div class="market-line"><strong class="market-line-label">${label}</strong><div class="market-line-scroll" tabindex="0" aria-label="${label}: swipe sideways">${items.filter(filter).map(r=>`<button class="market-inline" data-market-key="${esc(r.key)}" aria-label="${esc(r.label+'; '+(r.price==null?'unavailable':num(r.price))+'; '+(marketLabels[r.status]||r.status))}"><b>${esc(r.label)}</b><span class="market-inline-price">${num(r.price)}</span><span class="${r.change_pct>0?'positive':r.change_pct<0?'negative':'muted'}">${r.change_pct==null?'':(r.change_pct>0?'+':'')+num(r.change_pct)+'%'}</span>${r.expiry?`<small>${esc(r.expiry)}</small>`:''}<small class="${r.status==='RECENT_TRADE'?'positive':'amber'}">${esc(compactLabels[r.status]||r.status)}</small></button>`).join('')||'<span class="market-line-empty">Unlock workspace to load quotes</span>'}</div>${i===0?'<span id="live-feed-state" class="live-feed-state">'+liveState+'</span><button id="markets-refresh" class="market-reload" aria-label="Refresh markets" title="Refresh markets">↻</button>':''}</div>`).join('');
 const problem=[marketBoard?.error,marketBoard?.catalog_error].filter(Boolean).join(' · ');
 $('#markets-note').textContent=problem;$('#markets-note').hidden=!problem;
 $('#markets-refresh').disabled=marketBusy;
 document.querySelectorAll('.market-line-scroll').forEach((el,i)=>el.scrollLeft=positions[i]||0);
}
async function loadMarkets(refresh=false){
 if(marketBusy||!status||!$('#auth').hidden)return;
 marketBusy=true;$('#markets-refresh').disabled=true;
 try{
   marketBoard=await api(refresh&&status.dhan_configured?'markets/refresh':'markets',refresh&&status.dhan_configured?{}:undefined);
   marketFetched=Date.now();renderMarkets();startLive();
 }catch(ex){$('#markets-note').hidden=false;$('#markets-note').textContent=ex.message||'Market refresh failed';}
 finally{marketBusy=false;$('#markets-refresh').disabled=false;}
}
$('#market-toolbar').addEventListener('click',e=>{
 if(e.target.closest('#markets-refresh')){loadMarkets(true);return;}
 const button=e.target.closest('[data-market-key]');
 if(!button)return;
 const r=marketBoard?.items.find(x=>x.key===button.dataset.marketKey);if(!r)return;
 const detail=$('#market-quote-detail');
 detail.textContent=[r.label,marketLabels[r.status]||r.status,r.source||'Separate feed required',r.unit,r.expiry?'Contract expiry '+r.expiry:null,r.quote_at?'Trade time '+when(r.quote_at):'Exchange quote time unavailable',r.fetched_at?'Fetched '+when(r.fetched_at):null,r.note,'Latest market reference; not historical backtest data.'].filter(Boolean).join(' · ');
 detail.hidden=false;
});
document.addEventListener('theta:loaded',()=>{if(!marketBoard)loadMarkets(true);});
// Independent of the NIFTY session: MCX and global reference markets differ.
setInterval(()=>{if(!liveController&&!document.hidden&&status?.dhan_configured&&$('#auth').hidden&&Date.now()-marketFetched>=55000)loadMarkets(true);},60000);
renderMarkets();

function liveStateSet(value){liveState=value;const el=$('#live-feed-state');if(el)el.textContent=value;}
function scheduleTickRender(){if(liveRender)return;liveRender=setTimeout(()=>{liveRender=null;renderMarkets();},150);}
async function startLive(){
 if(liveController||liveRetry||document.hidden||!status?.dhan_configured||!$('#auth').hidden||!marketBoard)return;
 const controller=new AbortController();liveController=controller;liveStateSet('CONNECTING');
 try{
  const response=await fetch('/api/markets/live',{headers:key?{Authorization:'Bearer '+key}:{},signal:controller.signal});
  if(!response.ok){liveStateSet(response.status===401?'UNLOCK REQUIRED':'FEED ERROR '+response.status);throw Error('Feed connection failed');}
  const reader=response.body.getReader(),decoder=new TextDecoder();let pending='';
  while(true){
   const {value,done}=await reader.read();if(done)break;
   pending+=decoder.decode(value,{stream:true});if(pending.length>1048576)throw Error('Feed buffer exceeded');
   let boundary;
   while((boundary=pending.indexOf('\n'))>=0){
    const raw=pending.slice(0,boundary);pending=pending.slice(boundary+1);if(!raw)continue;
    const event=JSON.parse(raw);
    if(event.type==='tick'){
     const row=marketBoard.items.find(r=>r.key===event.key);if(!row)continue;
     Object.assign(row,event);document.dispatchEvent(new CustomEvent('theta:tick',{detail:event}));liveBackoff=5000;liveStateSet('TICKS RECEIVED');scheduleTickRender();
    }else if(event.type==='state'){
     liveStateSet(event.state==='CONNECTED_WAITING_FOR_TICKS'?'CONNECTED · WAITING':event.state+(event.code?' '+event.code:''));
    }
   }
  }
 }catch(ex){if(!controller.signal.aborted)liveStateSet('DISCONNECTED · RETRY');}
 finally{
  if(liveController===controller)liveController=null;
  if(!document.hidden&&status?.dhan_configured&&$('#auth').hidden){
   if(liveState==='TICKS RECEIVED')liveStateSet('DISCONNECTED · RETRY');
   liveRetry=setTimeout(()=>{liveRetry=null;startLive();},liveBackoff);liveBackoff=Math.min(liveBackoff*2,60000);
  }
 }
}
document.addEventListener('visibilitychange',()=>{
 if(document.hidden){if(liveRetry){clearTimeout(liveRetry);liveRetry=null;}liveController?.abort();liveStateSet('PAUSED');}
 else startLive();
});
// Quote age is computed from provider trade time, even if no further ticks arrive.
setInterval(()=>{
 let changed=false;
 for(const row of marketBoard?.items||[]){if(row.status==='RECENT_TRADE'&&row.quote_at&&Date.now()-Date.parse(row.quote_at)>60000){row.status='STALE';changed=true;}}
 if(changed)scheduleTickRender();
},5000);
