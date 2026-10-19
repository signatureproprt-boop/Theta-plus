'use strict';
(() => {
  const node=id=>document.getElementById(id),snapshot=()=>window.thetaResearchSnapshot?.()||{};
  const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=v=>v==null?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:2});
  let draft=null,ledger=null,paperLoading=false,paperError='';
  const message=(id,value)=>{if(node(id))node(id).textContent=value;};
  function planButton(){
    if(!node('paper-plan-result')||!draft)return;
    node('paper-plan-result').innerHTML=draft.plan.state==='SIZED'?`<p>${draft.plan.lots} lots · manual hypothetical entry only.</p><button class="button secondary" id="paper-use-plan">Use this plan in Paper Journal</button>`:'Paper entry remains blocked by risk checks.';
    if(node('paper-use-plan'))node('paper-use-plan').onclick=()=>{location.hash='#paper';window.thetaResearchShow('paper');};
  }
  window.thetaPaperPlanReady=value=>{draft=value;planButton();};
  window.thetaPaperPlanClear=()=>{draft=null;if(node('paper-plan-result'))node('paper-plan-result').textContent='';};
  async function loadPaper(){
    if(paperLoading)return;paperLoading=true;
    try{ledger=await window.thetaAdminRequest('research/paper/list',{});paperError='';}
    catch(e){paperError=e.message;}
    finally{paperLoading=false;if(snapshot().current==='paper')renderPaper();}
  }
  function renderPaper(){
    const el=node('paper-trade-panel');if(!el)return;
    const ready=draft?.plan.state==='SIZED'&&draft.contract?.identity_verified;
    el.innerHTML=`<p class="research-help">Manual hypothetical option buys. Entry and exit prices are your explicit assumptions; no automatic fills or accuracy claims. Charges, spread and slippage use the saved risk plan.</p>${paperError?`<p role="status">${esc(paperError)}</p>`:''}<button class="button secondary" id="paper-refresh">Read protected paper ledger</button>${ready?`<div class="research-identity">${esc(draft.symbol.toUpperCase())} · ${esc(draft.contract.expiry)} · ${fmt(draft.contract.strike)} ${esc(draft.contract.side)} · ${draft.plan.quantity} quantity · assumed entry ₹${fmt(draft.risk.entry)}</div><form id="paper-open" class="research-form"><label>Setup<select name="setup"><option value="opening_range">Opening range + retest</option><option value="trend_pullback">Trend pullback</option></select></label><label class="wide">Entry reason<textarea name="reason" minlength="3" maxlength="2000" required></textarea></label><label class="wide">Rule deviations (required for an unconfirmed setup)<textarea name="deviations" maxlength="2000"></textarea></label><button class="button" type="submit">Record hypothetical buy</button></form>`:'<p class="research-help">Select a verified option in Options Analysis, then calculate a sized risk plan to prepare a paper entry.</p>'}<div id="paper-action-state" role="status"></div>${ledger?`<p class="research-help">Recorded closed: ${ledger.recorded_closed_trades} · validated: ${ledger.validated_trades} · hypothetical net ₹${fmt(ledger.hypothetical_net_pnl)}. ${esc(ledger.qualification)}</p><button class="button secondary" id="paper-export">Export paper ledger JSON</button>${ledger.trades.map(t=>`<article class="research-note"><b>${esc(t.input.symbol.toUpperCase())} ${fmt(t.contract.strike)} ${esc(t.contract.side)} · ${esc(t.contract.expiry)} · ${esc(t.state)}</b><div>${t.quantity} quantity · assumed buy ₹${fmt(t.entry_premium)} · ${esc(t.opened_at)}</div><div>Entry: ${esc(t.input.reason)} · deviation: ${esc(t.input.deviations||'None recorded')}</div><div>Rule: ${esc(t.setup_snapshot.state)} · ${esc(t.setup_snapshot.reason_codes.join(', '))}</div>${t.state==='OPEN'?`<form data-paper-close="${t.id}" class="research-form"><label>Assumed exit premium ₹<input name="exit_premium" type="number" step="any" min="0" required></label><label>Exit reason<input name="reason" minlength="3" maxlength="2000" required></label><label>Exit deviation<input name="deviations" maxlength="2000"></label><button class="button secondary" type="submit">Record hypothetical exit</button></form>`:`<p>Gross ₹${fmt(t.gross_hypothetical_pnl)} · costs ₹${fmt(t.estimated_costs)} · net ₹${fmt(t.hypothetical_net_pnl)}</p><div>Exit: ${esc(t.close_input.reason)} · deviation: ${esc(t.close_input.deviations||'None recorded')}</div>`}</article>`).join('')}`:''}`;
    node('paper-refresh').onclick=loadPaper;
    node('paper-open')?.addEventListener('submit',async e=>{
      e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
      const id=e.target.dataset.entryId||(e.target.dataset.entryId=crypto.randomUUID().replaceAll('-',''));
      try{await window.thetaAdminRequest('research/paper',{...Object.fromEntries(new FormData(e.target)),id,symbol:draft.symbol,security_id:draft.contract.security_id,risk:draft.risk});draft=null;await loadPaper();}
      catch(error){message('paper-action-state',error.message);button.disabled=false;}
    });
    document.querySelectorAll('[data-paper-close]').forEach(form=>form.onsubmit=async e=>{
      e.preventDefault();const button=form.querySelector('button');button.disabled=true;
      const fields=Object.fromEntries(new FormData(form));fields.exit_premium=Number(fields.exit_premium);
      try{await window.thetaAdminRequest('research/paper/'+form.dataset.paperClose+'/close',fields);await loadPaper();}
      catch(error){message('paper-action-state',error.message);button.disabled=false;}
    });
    if(node('paper-export'))node('paper-export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(ledger,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='theta-paper-ledger.json';a.click();URL.revokeObjectURL(url);};
  }
  window.thetaWorkflowMount=()=>{
    const {data,selected,current,context}=snapshot();
    const aside=node('workflow-aside');if(aside){
      const ind=data?.indicators?.['5'];const future=context?.futures_vwap||data?.futures_context;const breadth=context?.breadth||data?.breadth;
      aside.innerHTML=`<h2>Decision checklist</h2><p class="research-help">5m RSI(14) ${fmt(ind?.rsi14)} · ADX(14) ${fmt(ind?.adx14)} · +DI ${fmt(ind?.plus_di)} / −DI ${fmt(ind?.minus_di)}</p><p class="research-help">${esc(context?.warmup_error?'Warm-up refresh: '+context.warmup_error:'')}</p><p class="research-help">${future?`Futures candle VWAP ${fmt(future.value)} · ${esc(future.state)} · expiry ${esc(future.expiry)}. ${esc(future.basis||future.reason)}`:'Futures candle VWAP: Start live to request current exact contract.'}</p><p class="research-help">Breadth: ${esc(breadth?.state||'NOT_CONNECTED')}${breadth?.fraction_up==null?'':` · advances ${breadth.advances} / declines ${breadth.declines} / unchanged ${breadth.unchanged}`}. Equal-count participation, not index weights.</p><div class="research-identity">${selected?`${fmt(selected.strike)} ${esc(selected.side)} · ${esc(selected.expiry)} · lot ${esc(selected.lot_size)}`:'Select an exact option in Options Analysis.'}</div><button class="button secondary" id="workflow-risk">Plan risk</button><p class="research-help">Missing events, breadth or quote time keep confirmation blocked. RSI, ADX and PCR are context, not an accuracy score.</p>`;
      node('workflow-risk').onclick=()=>{if(current==='research'){node('research-risk')?.querySelector('input')?.focus();}else{location.hash='#risk';window.thetaResearchShow('risk');}};
      if(current==='quality'){
        const jobs=data?.collection?.jobs||[];
        aside.insertAdjacentHTML('beforeend',`<h2>Completion checks</h2><div class="table-wrap"><table class="research-table"><thead><tr><th>Check</th><th>Status / next requirement</th></tr></thead><tbody>${(data?.readiness||[]).map(r=>`<tr><td>${esc(r.item)}</td><td>${esc(r.state)}<br><small>${esc(r.next)}</small></td></tr>`).join('')}</tbody></table></div>`);
        aside.insertAdjacentHTML('beforeend',`<h2>Blocked request diagnosis</h2>${jobs.filter(j=>['request_review','permission_blocked','token_required'].includes(j.state)).map(j=>`<button class="button secondary" data-diagnose="${esc(j.id)}">Check ${esc(j.id.slice(0,8))}</button>`).join('')||'<p class="research-help">No blocked job in this bounded view.</p>'}<div id="workflow-diagnosis" class="research-result" role="status"></div>`);
        document.querySelectorAll('[data-diagnose]').forEach(b=>b.onclick=async()=>{b.disabled=true;message('workflow-diagnosis','Checking exact master identity and two bounded recent requests…');try{const r=await window.thetaAdminRequest('collection/jobs/'+b.dataset.diagnose+'/diagnose',{});message('workflow-diagnosis',JSON.stringify(r,null,2));}catch(e){message('workflow-diagnosis',e.message);}finally{b.disabled=false;}});
      }
    }
    planButton();if(current==='paper'){renderPaper();if(!ledger&&!paperLoading&&!paperError)loadPaper();}
  };
  window.thetaWorkflowMount();
})();
