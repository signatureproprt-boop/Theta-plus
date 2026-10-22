'use strict';
(() => {
  const node=id=>document.getElementById(id);
  const escape=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=v=>v==null?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:2});
  let active=false,controller=null,generation=0,symbol='nifty',state='Disconnected',rows=new Map(),points=[],lastFrame=0,retries=0,connected=false,stalled=false,retryDelay=2500,busySince=0;
  function text(message){state=message;if(node('research-live-state'))node('research-live-state').textContent=message;}
  function currentTrade(r){const at=Date.parse(r.trade_at);const age=Date.now()-at;const receiptAge=Date.now()-Date.parse(r.received_at);return active&&connected&&Number.isFinite(at)&&age>=0&&age<=10000&&receiptAge>=0&&receiptAge<=10000&&r.trade_state==='RECENT_TRADE';}
  let paintTimer=null,pendingTicks=new Map(),pendingUnderlying=null;
  // Keep every observation in the stream state, but paint at most ten times a second.
  // Coalesce by exact identity so a busy contract cannot hide another contract's update.
  function cancelPaint(){if(paintTimer!==null)clearTimeout(paintTimer);paintTimer=null;}
  function schedulePaint(){if(paintTimer===null)paintTimer=setTimeout(()=>{paintTimer=null;render();},100);}
  function render(){
    cancelPaint();
    const ticks=[...pendingTicks.values()],underlying=pendingUnderlying;
    pendingTicks.clear();pendingUnderlying=null;
    if(active&&ticks.length){
      if(underlying&&currentTrade(underlying))window.thetaResearchTick?.(underlying);
      window.thetaResearchLiveBatch?.(ticks);window.thetaMarketLiveBatch?.(ticks);
    }
    window.thetaResearchLiveState?.();window.thetaMarketLiveState?.();if(!node('research-live-prices'))return;
    node('research-live-state').textContent=state;
    node('research-live-start').disabled=active;node('research-live-stop').disabled=!active;
    const main=[...rows.values()].filter(r=>r.role!=='option');
    node('research-live-prices').innerHTML=main.map(r=>`<span><b>${escape(r.label)} ${fmt(r.ltp)}</b><br><small>${currentTrade(r)?'Recent trade':'STALE / unverified trade'} · ${escape(r.trade_at)}</small></span>`).join('');
    const contracts=[...rows.values()].filter(r=>r.role==='option');
    node('research-live-contracts').innerHTML=contracts.length?`<table class="research-table"><thead><tr><th>Fixed contract</th><th>LTP ₹</th><th>Bid / ask</th><th>Spread %</th><th>Volume / OI</th><th>Trade time</th></tr></thead><tbody>${contracts.sort((a,b)=>a.strike-b.strike||a.side.localeCompare(b.side)).map(r=>`<tr><td><button class="button secondary" data-live-contract="${escape(r.security_id)}">${escape(r.label)}</button><br><small>${escape(r.expiry)} · ID ${escape(r.security_id)} · lot ${r.lot_size}</small></td><td>${fmt(r.ltp)}</td><td>${fmt(r.bid)} / ${fmt(r.ask)}</td><td>${fmt(r.spread_pct)}</td><td>${fmt(r.volume)} / ${fmt(r.oi)}</td><td>${currentTrade(r)?'Recent trade':'STALE / unverified'}<br><small>${escape(r.trade_at)}</small></td></tr>`).join('')}</tbody></table><p class="research-help">Depth received over the live socket. No separate exchange quote time is provided; bid/ask is not certified fresh by the last-trade timestamp. These observations do not unlock setup confirmation or historical PCR trend.</p>`:'';
    document.querySelectorAll?.('[data-live-contract]').forEach(b=>b.onclick=()=>{const row=[...rows.values()].find(r=>r.security_id===b.dataset.liveContract);if(row)window.thetaResearchPickContract?.(row);});
    draw();
  }
  function draw(){const svg=node('research-live-chart');if(!svg)return;svg.hidden=points.length<2;if(svg.hidden)return;
    const width=Math.max(260,svg.clientWidth),height=230,pad=66;svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.style.height='230px';
    let lo=Math.min(...points.map(p=>p.price)),hi=Math.max(...points.map(p=>p.price));const extra=(hi-lo)*.1||1;lo-=extra;hi+=extra;
    const first=points[0].at,last=points.at(-1).at,x=t=>12+(t-first)/(last-first||1)*(width-pad-12),y=p=>15+(hi-p)/(hi-lo)*180;
    let html='';for(let i=0;i<3;i++){const v=lo+(hi-lo)*i/2;html+=`<text x="${width-pad+4}" y="${y(v)+4}">${fmt(v)}</text><line x1="12" x2="${width-pad}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/>`;}
    // Gaps over 10s start a new segment; reconnects do not fabricate continuity.
    let path='';points.forEach((p,i)=>{path+=(i&&p.at-points[i-1].at<=10000?'L':'M')+x(p.at)+','+y(p.price);});
    html+=`<path d="${path}" fill="none" stroke="var(--mint)" stroke-width="1.7"/><text x="12" y="222">${new Date(first).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})} IST</text><text x="${width-pad}" y="222" text-anchor="end">Observed LTP · ${escape(symbol.toUpperCase())}</text>`;svg.innerHTML=html;
  }
  function receive(data){lastFrame=Date.now();if(data.type==='status'){
      if(data.state==='STREAM_BUSY'){
        busySince=busySince||Date.now();
        if(Date.now()-busySince>=330000){active=false;text('Another live tab is still active. Stop that tab, then Start live here.');}
        else {retryDelay=5000;text('Another stream is closing or active · retrying in 5s. Stop cancels this retry.');}
        render();return;
      }
      if(data.state==='UNAVAILABLE'||data.state==='PROVIDER_DISCONNECT'){active=false;text(data.state+' · '+(data.message||('Provider code '+data.code)));render();return;}
      if(data.state==='CONNECTED_AWAITING_TICKS'){connected=true;busySince=0;retryDelay=2500;}
      text(data.state==='RENEW_CONNECTION'?'Renewing connection…':data.state);return;
    }
    if(data.type!=='batch')return;
    for(const row of data.ticks||[]){const identity=row.segment+':'+row.security_id;rows.set(identity,row);pendingTicks.set(identity,row);
      if(row.role==='underlying'&&row.key===symbol&&currentTrade(row)){
        const at=Date.parse(row.trade_at),old=points.at(-1);
        if(!old||at>old.at)points.push({at,price:row.ltp});else if(at===old.at)old.price=row.ltp;
        points=points.slice(-300);pendingUnderlying=row;
      }
    }
    if((data.ticks||[]).length){retries=0;text('Connected · received '+new Date(data.received_at).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+' IST');}
    else if(!rows.size)text('Connected · awaiting market ticks');
    schedulePaint();
  }
  async function connect(id){
    while(active&&generation===id){controller=new AbortController();connected=false;lastFrame=Date.now();try{
      const response=await window.thetaLiveStreamRequest(symbol,controller.signal);
      if(generation!==id||!active){await response.body?.cancel?.().catch(()=>{});break;}
      if(!response.ok){if(response.status===409){receive({type:'status',state:'STREAM_BUSY'});if(active&&generation===id)await new Promise(resolve=>setTimeout(resolve,retryDelay));continue;}await window.thetaReadResponse(response);}
      if(!response.body)throw Error('Streaming is unavailable in this browser');
      const reader=response.body.getReader(),decoder=new TextDecoder();let pending='';
      while(active&&generation===id){const {done,value}=await reader.read();if(done)break;pending+=decoder.decode(value,{stream:true});if(pending.length>2000000)throw Error('Invalid stream size');let end;while((end=pending.indexOf('\n\n'))>=0){const block=pending.slice(0,end);pending=pending.slice(end+2);if(block.startsWith('data: ')&&active&&generation===id)receive(JSON.parse(block.slice(6)));}}
      await reader.cancel().catch(()=>{});
      connected=false;render();if(active&&generation===id){if(!busySince)text('Renewing live connection…');await new Promise(resolve=>setTimeout(resolve,retryDelay));}
    }catch(error){if(generation!==id||!active)break;if(error.name==='AbortError'&&!stalled)break;stalled=false;connected=false;retries++;
      // Authentication/configuration failures need a deliberate user action.
      if([401,403,422,429].includes(error.status)||/Admin|password|credentials|token|rejected/i.test(error.message)||retries>=5){active=false;text(error.message);render();break;}
      text('Disconnected · reconnect attempt '+retries+'/5');render();await new Promise(resolve=>setTimeout(resolve,Math.min(30000,1000*2**retries)));
    }}
  }
  window.thetaLiveIsActive=()=>active;
  window.thetaLiveIsConnected=()=>active&&connected;
  window.thetaLiveStop=()=>{active=false;connected=false;generation++;controller?.abort();text('Disconnected · last observations retained, not live');render();};
  window.thetaLiveMount=()=>{
    if(!node('research-live-start'))return;
    node('research-live-start').onclick=()=>{if(active)return;if(typeof window.thetaLiveStreamRequest!=='function'){text('Open Admin and Unlock Admin first.');render();return;}symbol=node('research-symbol')?.value||'nifty';cancelPaint();pendingTicks.clear();pendingUnderlying=null;rows=new Map();points=[];window.thetaResearchLiveReset?.();retries=0;busySince=0;retryDelay=2500;active=true;window.thetaSessionRefresh?.();lastFrame=Date.now();text('Connecting…');render();connect(++generation);};
    node('research-live-stop').onclick=window.thetaLiveStop;render();
  };
  setInterval(()=>{if(active&&lastFrame&&Date.now()-lastFrame>(connected?15000:120000)){text('Feed stalled · reconnecting');stalled=true;lastFrame=Date.now();controller?.abort();}render();},2000);
  window.addEventListener('beforeunload',()=>{cancelPaint();controller?.abort();});
  window.addEventListener('resize',draw);window.thetaLiveMount();
})();
