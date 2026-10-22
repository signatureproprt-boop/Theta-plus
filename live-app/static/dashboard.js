'use strict';
(() => {
  const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=v=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString('en-IN',{maximumFractionDigits:2}):'—';
  const levels=[
    ['Market basics','OI PCR','Volume PCR','Futures VWAP','Option Greeks','India VIX'],
    ['Market structure','Max Pain','OI quadrant','IV skew','Support / resistance'],
    ['Options exposure','GEX','DEX','VEX / CEX','Gamma wall','Term structure'],
    ['Order flow','CVD','Volume profile','Expiry-day context','Probability model'],
    ['Microstructure','Order flow imbalance','Absorption','Order blocks','Dealer hedging'],
    ['Pattern alerts','Liquidity sweep','False breakout','Spoofing candidates','Flow regime'],
    ['Volatility research','IV mispricing','Skew normalization','Term structure spread','Dispersion'],
    ['Model validation','XGBoost','LSTM','Transformer','RL / Ensemble'],
  ];
  let active=0,month='',focus=false;
  const missing={state:'UNAVAILABLE',value:null,source:'Analysis module not connected',time:null};
  function observation(name,snapshot){
    const d=snapshot.data||{},c=d.chain||{},selected=snapshot.selected;
    if(name==='OI PCR'||name==='Volume PCR')return {value:name==='OI PCR'?c.pcr_oi:c.pcr_volume,state:c.status||'NOT_LOADED',source:'Dhan chain · current snapshot',time:c.exchange_at};
    if(name==='Futures VWAP'){const f=snapshot.context?.futures_vwap||d.futures_context;return f?{value:f.value,state:f.state,source:'Exact futures candles',time:f.exchange_at}:missing;}
    if(name==='Option Greeks')return {value:selected?.greeks?.delta,state:selected?.greeks?'PROVIDER_OBSERVATION':'UNAVAILABLE',source:'Selected contract · delta',time:selected?.exchange_at};
    return missing;
  }
  function metric(name,snapshot){
    const o=observation(name,snapshot);
    return `<article class="analysis-metric"><div class="analysis-metric-head"><h3>${esc(name)}</h3><span class="analysis-status">${esc(o.state)}</span></div><strong>${fmt(o.value)}</strong><small>${esc(o.source)}</small><small>Exchange time · ${esc(o.time||'Unavailable')}</small></article>`;
  }
  function pcr(snapshot){
    const c=snapshot.data?.chain||{};
    // A single chain snapshot is never promoted to an all-expiry/month average.
    const belongs=typeof c.expiry==='string'&&c.expiry.slice(0,7)===month;
    const has=belongs&&typeof c.pcr_oi==='number'&&Number.isFinite(c.pcr_oi);
    return `<section class="pcr-workspace" aria-label="Expiry and monthly PCR"><div class="analysis-heading"><div><h3>PCR · expiry & month</h3><p>OI PCR and Volume PCR · expiry coverage stays explicit</p></div><label>Month<input id="dashboard-pcr-month" type="month" value="${esc(month)}"></label></div><div class="pcr-summary"><div><span>SELECTED SNAPSHOT</span><b>${has?fmt(c.pcr_oi):'—'}</b><small>${has?esc(c.expiry):'No snapshot in selected month'}</small></div><div><span>MONTH COMBINED</span><b>—</b><small>All-expiry coverage unverified</small></div><div><span>WEEKLY SIMPLE AVERAGE</span><b>—</b><small>Weekly expiry set required</small></div><div><span>MONTH HISTORICAL AVERAGE</span><b>—</b><small>Time-series coverage required</small></div></div><div class="table-wrap"><table class="research-table pcr-table"><thead><tr><th>Expiry</th><th>Type</th><th>OI PCR</th><th>Volume PCR</th><th>Source status</th></tr></thead><tbody>${belongs?`<tr><td>${esc(c.expiry)}</td><td>Unclassified</td><td>${fmt(c.pcr_oi)}</td><td>${fmt(c.pcr_volume)}</td><td>${esc(c.status||'UNAVAILABLE')}</td></tr>`:'<tr><td colspan="5">No saved expiry snapshot connected for this month.</td></tr>'}</tbody></table></div><details><summary>Calculation & coverage rules</summary><p>Combined PCR = total Put OI / total Call OI across distinct expiries. Weekly average = arithmetic mean of covered weekly-expiry PCRs. A monthly expiry also listed weekly is counted once in the combined total. Historical averaging requires a declared sampling interval and missing-snapshot coverage. Current snapshot values do not establish these averages.</p></details><p class="research-help">Expiry discovery, weekly/monthly classification and monthly history are not connected to this panel yet. Missing values remain unavailable.</p></section>`;
  }
  function render(){
    const host=document.getElementById('dashboard-analysis');if(!host)return;
    const snapshot=window.thetaResearchSnapshot?.()||{};
    if(!month){const expiry=snapshot.data?.chain?.expiry;month=/^\d{4}-\d{2}-\d{2}$/.test(expiry||'')?expiry.slice(0,7):new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit'}).format(new Date());}
    host.innerHTML=`<div class="analysis-heading"><div><span class="decision-kicker">RESEARCH WORKSPACE</span><h2>Eight levels. One market view.</h2></div><span class="analysis-status">Modules show their own evidence</span></div><div class="analysis-tabs" role="tablist" aria-label="Analysis levels">${levels.map((l,i)=>`<button type="button" role="tab" id="analysis-tab-${i}" aria-controls="analysis-level-panel" aria-selected="${i===active}" tabindex="${i===active?0:-1}" data-analysis-level="${i}"><b>L${i+1}</b><span>${esc(l[0])}</span></button>`).join('')}</div><div id="analysis-level-panel" role="tabpanel" aria-labelledby="analysis-tab-${active}" tabindex="0"><div class="analysis-panel-title"><h3>Level ${active+1} · ${esc(levels[active][0])}</h3><span>${active===0?'Saved inputs · research context':'NOT IMPLEMENTED · design slots'}</span></div><div class="analysis-grid">${levels[active].slice(1).map(n=>metric(n,snapshot)).join('')}</div>${active===0?pcr(snapshot):'<p class="research-help">These cards are design slots. Values and alerts will appear only after their calculation modules and source checks are implemented. Exposure and dealer-flow models require explicit assumptions; pattern alerts do not prove intent.</p>'}</div>`;
    host.querySelectorAll('[data-analysis-level]').forEach(button=>{
      button.onclick=()=>{active=Number(button.dataset.analysisLevel);render();document.getElementById('analysis-tab-'+active)?.focus();};
      button.onkeydown=e=>{let next=active;if(e.key==='ArrowRight')next=(active+1)%8;else if(e.key==='ArrowLeft')next=(active+7)%8;else if(e.key==='Home')next=0;else if(e.key==='End')next=7;else return;e.preventDefault();active=next;render();document.getElementById('analysis-tab-'+active)?.focus();};
    });
    const input=document.getElementById('dashboard-pcr-month');if(input)input.onchange=()=>{if(/^\d{4}-\d{2}$/.test(input.value)){month=input.value;render();document.getElementById('dashboard-pcr-month')?.focus();}};
  }
  window.thetaDashboardMount=()=>{
    const snapshot=window.thetaResearchSnapshot?.()||{};
    if(snapshot.current!=='research'){focus=false;document.body.classList.remove('theta-chart-focus');return;}
    document.body.classList.toggle('theta-chart-focus',focus);
    const button=document.getElementById('dashboard-chart-focus');
    if(button){button.textContent=focus?'Exit chart focus':'Chart focus';button.setAttribute('aria-pressed',String(focus));button.onclick=()=>{focus=!focus;window.thetaDashboardMount();window.dispatchEvent(new Event('resize'));};}
    render();
  };
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&focus){focus=false;window.thetaDashboardMount();window.dispatchEvent(new Event('resize'));}});
  window.thetaDashboardMount();
})();
