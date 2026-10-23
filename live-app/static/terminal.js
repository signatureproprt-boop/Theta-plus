/* Compact shell. Reads retained observations; never starts collection or orders. */
(()=>{'use strict';
  const node=id=>document.getElementById(id),esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=v=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString('en-IN',{maximumFractionDigits:2}):'—';
  let markets={items:[]},jobs=null,search='',marketError='',jobError='',group='all',polling=false;
  const streamed=new Map();
  const groupOf=r=>r.key==='usd_inr'?'india':r.group==='USD Commodities'?'usd':r.segment==='MCX_COMM'||['Commodity','Commodities'].includes(r.group)?'inr':['India'].includes(r.group)?'india':'foreign';
  const signed=v=>Number.isFinite(v)?(v>0?'+':'')+fmt(v):'—';
  async function read(path,post=false){const response=await window.thetaApiFetch('/api/'+path,{method:post?'POST':'GET',signal:AbortSignal.timeout(60000),...(post?{headers:{'Content-Type':'application/json'},body:'{}'}:{})});const value=await window.thetaReadResponse(response);if(!response.ok)throw Error(value.detail||'Saved data unavailable');return value;}
  function time(v){const date=new Date(v);return v&&Number.isFinite(+date)?date.toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}):'Time unavailable';}
  function renderMarkets(){
    const items=markets.items||[];
    node('terminal-ticker').innerHTML=['nifty','banknifty','sensex','india_vix','usd_inr'].map(key=>{const r=items.find(i=>i.key===key);return `<span>${esc(r?.label||key.replaceAll('_',' '))} <b>${fmt(r?.price)}</b> <small>${esc(r?.status||'NOT_FETCHED')}</small></span>`;}).join('');
    const visible=items.filter(r=>(group==='all'||groupOf(r)===group||search)&&`${r.label} ${r.group} ${r.segment||''}`.toLowerCase().includes(search));
    node('terminal-watch-list').innerHTML=visible.map(saved=>{
      const tick=streamed.get(saved.key),recent=tick&&tick.security_id===saved.security_id&&tick.segment===saved.segment&&Date.now()>=Date.parse(tick.received_at)&&Date.now()>=Date.parse(tick.trade_at)&&window.thetaLiveIsConnected?.()&&Date.now()-Date.parse(tick.received_at)<=10000&&Date.now()-Date.parse(tick.trade_at)<=10000;
      const r=recent?{...saved,price:tick.ltp,quote_at:tick.trade_at,status:'STREAM_TRADE',change:saved.previous_close>0?tick.ltp-saved.previous_close:null,change_pct:saved.previous_close>0?100*(tick.ltp-saved.previous_close)/saved.previous_close:null}:saved;
      const change=typeof r.change==='number'&&Number.isFinite(r.change)&&typeof r.price==='number'?r.change:null;
      const unit=r.unit||(r.segment==='MCX_COMM'?'INR futures quote':r.instrument==='INDEX'?'index points':'Source / contract unavailable');
      return `<div class="watch-item ${recent&&Date.now()-Date.parse(tick.received_at)<1500?(tick.move>0?'tick-up':tick.move<0?'tick-down':''):''}" role="listitem"><button ${['nifty','banknifty'].includes(r.key)?`data-terminal-symbol="${r.key}"`:'data-terminal-detail="'+esc(r.key)+'"'}>${esc(r.label)}</button><b>${r.currency==='USD'?'$':r.currency==='INR'||r.segment==='MCX_COMM'?'₹':''}${fmt(r.price)}</b><em class="watch-change ${change<0?'negative':change>0?'positive':''}">${change==null?'Change unavailable':`${change>0?'▲ ':change<0?'▼ ':''}${signed(change)} ${Number.isFinite(r.change_pct)?'('+signed(r.change_pct)+'%)':'· % unavailable'}`}</em><small>${esc(unit)}${r.expiry?' · '+esc(r.expiry):''} · ${esc(r.status)}</small><time title="${esc(unit)}${r.expiry?' · '+esc(r.expiry):''}">${esc(r.status)} · ${esc(time(r.quote_at||r.fetched_at))}${!r.quote_at&&r.fetched_at?' · received':''}</time></div>`;
    }).join('')||'<p class="watch-empty">No matching instruments.</p>';
    node('terminal-watch-status').textContent=marketError||'Previous-close change · saved snapshots refresh every 15s. Stream ticks appear only while connected. Foreign / USD feeds need a separate source.';
    renderHome();
  }
  function renderHome(){
    const root=node('page-home');if(!root)return;
    const list=jobs?.jobs||[];const issues=jobs?.read_issues||[];
    root.innerHTML=`<div class="terminal-overview"><article class="card"><div class="card-head"><h2>Collection activity</h2><button class="button secondary" data-go="collect">Open collection</button></div><div class="table-wrap"><table><thead><tr><th>Scope</th><th>State</th><th>Returned rows / frames</th><th>Unresolved</th></tr></thead><tbody>${list.map(j=>`<tr><td>${esc((j.config?.families||[]).join(', ')||'Saved plan')}</td><td>${esc(j.state)}</td><td>${fmt(j.rows_saved)}</td><td>${fmt((j.counts?.failed||0)+(j.counts?.empty||0)+(j.counts?.permission_blocked||0)+(j.counts?.partial||0))}</td></tr>`).join('')||'<tr><td colspan="4">'+esc(jobError||'Loading saved collection outcomes…')+'</td></tr>'}</tbody></table></div><div class="card-body"><p>Rows and completed work items are not full historical coverage. Empty, failed and unsupported data remain separate.</p>${issues.length?'<p>'+esc(issues.length)+' job records unavailable on this page.</p>':''}</div></article><div><article class="card"><div class="card-head"><h2>Research workspace</h2></div><div class="card-body"><h3>Chart → CE/PE → setup → risk</h3><p>Exact contract identity, timestamps and data gaps stay visible. Setup confirmation remains blocked when required evidence is missing.</p><button class="button" data-go="research">Trading Room</button><button class="button secondary" data-go="quality">Data Quality</button><button class="button secondary" data-go="paper">Paper Journal</button></div></article><article class="card" style="margin-top:12px"><div class="card-head"><h2>Connection & account</h2></div><div class="card-body"><p id="terminal-overview-connection">${esc(node('connection')?.textContent||'Check Admin connection')}</p><p>Account balances and positions are not connected to this research view. Broker execution is disabled.</p><button class="button secondary" data-go="admin">Admin</button><button class="button secondary" data-go="sources">Dhan data</button></div></article></div></div>`;
  }
  async function load(){await Promise.allSettled([(async()=>{try{markets=await read('markets');marketError='';}catch(e){marketError=e.message;}renderMarkets();})(),(async()=>{try{jobs=await read('collection/jobs');jobError='';}catch(e){jobError=e.message;}renderHome();})()]);}
  node('terminal-watch-search').addEventListener('input',e=>{search=e.target.value.trim().toLowerCase();renderMarkets();});
  node('terminal-market-refresh').addEventListener('click',async e=>{e.target.disabled=true;try{markets=await read('markets/refresh',true);marketError='';}catch(error){marketError=error.message;}finally{e.target.disabled=false;renderMarkets();}});
  document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.id==='refresh')void load();if(b.dataset.marketGroup){group=b.dataset.marketGroup;document.querySelectorAll?.('[data-market-group]').forEach(tab=>tab.setAttribute('aria-pressed',String(tab.dataset.marketGroup===group)));renderMarkets();}if(b.dataset.terminalSymbol){location.hash='research';window.thetaResearchShow?.('research');window.thetaResearchSelectSymbol?.(b.dataset.terminalSymbol);}if(b.dataset.terminalDetail){const r=markets.items.find(r=>r.key===b.dataset.terminalDetail);if(r)node('terminal-watch-status').textContent=`${r.label} · ${r.status} · ${time(r.quote_at||r.fetched_at)} · ${r.note||'No research chart connected for this instrument.'}`;}});
  window.addEventListener('hashchange',()=>{document.querySelectorAll?.('.desk-tools[open]').forEach(menu=>{menu.open=false;});if(location.hash==='#home')renderHome();});
  window.thetaMarketLiveState=renderMarkets;
  window.thetaMarketLiveBatch=ticks=>{
    for(const tick of ticks){
      const old=markets.items.find(r=>r.key===tick.key);
      const received=Date.parse(tick.received_at),traded=Date.parse(tick.trade_at),last=streamed.get(tick.key);
      if(!old||tick.role==='option'||tick.security_id!==old.security_id||tick.segment!==old.segment||!(Number.isFinite(tick.ltp)&&tick.ltp>0)||tick.trade_state!=='RECENT_TRADE'||!Number.isFinite(received)||!Number.isFinite(traded)||received>Date.now()||traded>Date.now()||Date.now()-received>10000||Date.now()-traded>10000||last&&received<Date.parse(last.received_at))continue;
      streamed.set(tick.key,{...tick,move:tick.ltp-(last?.ltp??old.price??tick.ltp)});
    }
    renderMarkets();
  };
  setInterval(async()=>{if(document.hidden||polling)return;polling=true;try{markets=await read('markets');marketError='';}catch(error){marketError='Saved market refresh failed; retained observations may be stale.';}finally{polling=false;renderMarkets();}},15000);
  load();
})();
