'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let status=null,statusState="loading",collected={},busy=false;
const savedStates={dhan_history:'loading',dhan_chain:'loading'};
const labels={instruments:['All instruments','Your complete Dhan catalogue.'],research:['Trading Room','Market research.'],options:['Options Analysis','Current expiry.'],quality:['Data Quality','Evidence before signals.'],risk:['Risk & Rules','Plan the risk.'],replay:['Replay / Backtest','Replay known observations.'],paper:['Paper Journal','Record the decision.'],home:['Home','Data first.'],sources:['Dhan data','Fetch source data.'],collect:['Collect all data','Your Dhan data library.'],doctor:['System Doctor','Check every area.'],saved:['Saved data','Your records are retained.'],admin:['Admin','Dhan connection.']};
async function api(path,body){const response=await window.thetaApiFetch('/api/'+path,{signal:AbortSignal.timeout(body===undefined?60000:240000),method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const payload=await window.thetaReadResponse(response);if(!response.ok)throw Error(typeof payload.detail==='string'?payload.detail:'Request failed');return payload;}
function error(e){$('#error').hidden=false;$('#error').textContent=e.message||String(e);}
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('show');setTimeout(()=>$('#toast').classList.remove('show'),4000);}
function show(name){name=labels[name]?name:'home';$$('.page').forEach(n=>n.hidden=n.id!=='page-'+name);$$('nav button').forEach(n=>n.classList.toggle('active',n.dataset.page===name));$('#page-label').textContent=labels[name][0];$('#page-title').textContent=labels[name][1];window.thetaResearchShow?.(name);}
function renderHome(){$('#page-home').innerHTML=`<article class="card"><div class="card-head"><h2>Option buying research workspace</h2></div><div class="card-body"><p>Admin, Dhan credentials and saved data are retained.</p><p>Use Trading Room, Options Analysis and Data Quality. Versioned research rules remain unvalidated; no live orders are placed.</p><p>Missing timestamps, context and exact contract data block setup confirmation.</p><button class="button" data-go="sources">Open Dhan data</button></div></article>`;}
function previousWeekday(){const day=new Date();day.setUTCDate(day.getUTCDate()-1);while([0,6].includes(day.getUTCDay()))day.setUTCDate(day.getUTCDate()-1);return day.toISOString().slice(0,10);}
function renderSources(){const start=previousWeekday(),end=new Date(Date.parse(start+'T00:00:00Z')+86400000).toISOString().slice(0,10);$('#page-sources').innerHTML=`<article class="card"><div class="card-head"><h2>Dhan data collection</h2></div><div class="card-body"><p>Raw data only. Empty responses and missing minutes remain visible.</p><form id="history-form"><div class="form-grid"><label class="field">Index<select id="data-index"><option value="nifty">NIFTY</option><option value="banknifty">BANKNIFTY</option><option value="india_vix">India VIX</option></select></label><label class="field">Data<select id="data-kind"><option value="index">Index 1-minute candles</option><option value="daily">Index daily candles</option><option value="future">Current futures candles + OI</option><option value="expired_option">Expired option ATM history</option></select></label><label class="field">From (IST)<input id="data-start" type="date" value="${start}" required></label><label class="field">To (exclusive)<input id="data-end" type="date" value="${end}" required></label><label class="field">Option side<select id="data-side"><option>CALL</option><option>PUT</option></select></label><label class="field">Expiry type<select id="data-expiry"><option>WEEK</option><option>MONTH</option></select></label></div><button class="button" type="submit">Fetch and save</button></form><button class="button secondary" id="fetch-chain">Fetch and save option chain</button><p>Chain receipt time is not proof of fresh live quotes. Current futures are not a continuous historical series. Choose the date range using the exchange calendar.</p><pre id="fetch-result" style="white-space:pre-wrap;overflow-wrap:anywhere" role="status"></pre></div></article>`;}
function renderSaved(){
  const records=Object.entries(collected).flatMap(([recordKind,items])=>items.map(r=>({...r,recordKind})));
  const loading=Object.values(savedStates).includes('loading');
  const failed=Object.values(savedStates).includes('error');
  const message=loading?'Loading saved records…':failed?'Some saved records could not load. Press Refresh status to retry.':'';
  $('#page-saved').innerHTML=`<article class="card"><div class="card-head"><h2>Saved Dhan records</h2></div><div class="card-body" role="status">${esc(message)}</div>
    <div class="table-wrap"><table><thead><tr><th>TYPE</th><th>INDEX</th><th>SIDE / EXPIRY</th><th>DATE RANGE</th><th>RAW / SESSION ROWS</th><th>ACTIONS</th></tr></thead><tbody>
    ${records.map(r=>`<tr><td>${esc(r.kind)}</td><td>${esc(r.index)}</td><td>${esc(r.side || '—')} ${esc(r.expiry_flag || '')} ${r.expiry_code==null?'':'code '+esc(r.expiry_code)}</td>
    <td>${esc(r.from||r.expiry||'—')}${r.to_exclusive?' → '+esc(r.to_exclusive)+' (exclusive)':''}</td>
    <td>${esc(r.rows??r.strikes??'—')}${r.regular_session_rows==null?'':' / '+esc(r.regular_session_rows)}</td><td>
    <button class="button secondary" data-export="${esc(r.recordKind)}/${esc(r.id)}">JSON</button>
    ${r.recordKind==='dhan_history'?`<button class="button secondary" data-verify="${esc(r.recordKind)}/${esc(r.id)}">Check quality</button>`:''}</td></tr>`).join('')||`<tr><td colspan="6">${loading?'Loading saved records…':failed?'Saved records are unavailable; retry refresh.':'No saved Dhan records found.'}</td></tr>`}
    </tbody></table></div><div class="card-body"><button class="button" id="saved-coverage">Check recent coverage</button><button class="button secondary" id="coverage-export">Export regular-session data</button><p id="coverage-result" role="status"></p></div><pre id="saved-quality" style="white-space:pre-wrap;overflow-wrap:anywhere" role="status"></pre></article>
    <article class="card" style="margin-top:20px"><div class="card-body"><h2>Retained research archive</h2><p>Existing records can be exported. The retired engine is not run.</p><select id="archive-kind">${['dataset','journal','context','contract_metadata','experiment','plan','live_snapshot','live_candles','data_audit','research_setup'].map(k=>`<option>${k}</option>`).join('')}</select><button class="button secondary" id="archive-export">Export archive JSON</button></div></article>`;
}
async function refreshSaved(){
  for(const k of Object.keys(savedStates))savedStates[k]='loading';
  renderSaved();
  await Promise.allSettled(Object.keys(savedStates).map(async kind=>{
    try{const result=await api('dhan/collected?kind='+kind);collected[kind]=result[kind]||[];savedStates[kind]='loaded';}
    catch(e){savedStates[kind]='error';error(e);}
    renderSaved();
  }));
}
async function load(){
  if(busy)return;busy=true;statusState='loading';$('#error').hidden=true;renderHome();renderSources();renderAdmin();
  await Promise.allSettled([
    refreshSaved(),
    (async()=>{try{status=await api('status');statusState='loaded';$('#connection').textContent=status.dhan_configured?'Dhan configured':'Dhan not configured';renderAdmin();}catch(e){statusState='error';$('#connection').textContent='Connection status unavailable';renderAdmin();error(e);}})()
  ]);
  busy=false;
}

function coverageRange(){const end=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());const start=new Date(end+'T00:00:00Z');let days=0;while(days<5){start.setUTCDate(start.getUTCDate()-1);if(![0,6].includes(start.getUTCDay()))days++;}return 'start='+start.toISOString().slice(0,10)+'&end='+end;}
function download(name,value){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{if(b.dataset.page)location.hash=b.dataset.page;if(b.dataset.go)location.hash=b.dataset.go;if(b.id==='refresh')await load();if(b.id==='archive-export'){const kind=$('#archive-kind').value;download('theta-archive-'+kind+'.json',await api('archive/'+kind));}if(b.id==='saved-coverage'||b.id==='coverage-export'){b.disabled=true;$('#coverage-result').textContent='Checking retained records…';try{const report=await api('dhan/coverage?'+coverageRange()+(b.id==='coverage-export'?'&include_rows=true':''));if(b.id==='coverage-export')download('theta-regular-session-data.json',report);$('#coverage-result').textContent=report.from+' → '+report.to_exclusive+' (exclusive). '+report.groups.map(g=>g.index+' '+(g.side||'index')+': '+g.rows+' rows, missing '+g.sessions.reduce((n,s)=>n+s.missing,0)+', conflicts '+g.conflicting_rows).join(' · ')+'. '+report.calendar_validation+' '+report.options;}finally{b.disabled=false;}}if(b.dataset.verify){const q=await api('dhan/collected/'+b.dataset.verify+'/verify');$('#saved-quality').textContent=JSON.stringify(q,null,2);}if(b.dataset.export)download('theta-'+b.dataset.export.replace('/','-')+'.json',await api('dhan/collected/'+b.dataset.export));if(b.id==='fetch-chain'){b.disabled=true;const result=await api('dhan/chain',{index:$('#data-index').value});$('#fetch-result').textContent=JSON.stringify(result,null,2);await refreshSaved();}}catch(ex){error(ex);}finally{if(b.id==='fetch-chain')b.disabled=false;}});
document.addEventListener('submit',async e=>{const form=e.target;if(!['history-form','dhan-token-form'].includes(form.id))return;e.preventDefault();const button=form.querySelector('[type="submit"]');try{if(form.id==='dhan-token-form'){await window.thetaSaveDhanToken();return;}if(form.id==='history-form'){button.disabled=true;const result=await api('dhan/history',{kind:$('#data-kind').value,index:$('#data-index').value,start:$('#data-start').value,end:$('#data-end').value,side:$('#data-side').value,expiry_flag:$('#data-expiry').value});$('#fetch-result').textContent=JSON.stringify(result,null,2);await refreshSaved();}}catch(ex){error(ex);}finally{if(button)button.disabled=false;}});
window.addEventListener('hashchange',()=>show(location.hash.slice(1)));
function renderAdmin(){
  const saved=!!status?.dhan_configured;
  $('#page-admin').innerHTML=`<article class="card"><div class="card-head"><h2>Admin · Dhan connection</h2></div><div class="card-body">
    <div id="token-admin-panel"><label class="field">Token admin password<input id="token-admin-password" type="password" autocomplete="current-password" maxlength="512"></label>
    <button class="button secondary" id="admin-signin" type="button">Unlock Admin</button><button class="button secondary" id="admin-signout" type="button">Lock Admin</button>
    <p id="admin-login-status" class="form-help">Unlock Admin to manage collection or edit the saved token.</p></div>
    ${statusState!=='loaded'?'<p class="form-help" role="status">'+(statusState==='error'?'Connection status could not load. Retry Refresh status after the wait. Saved token has not been removed.':'Loading connection status…')+'</p>':''}
    ${status?.token_screen_enabled?`<div id="saved-token-summary" role="status">${saved?'Dhan token is saved securely. It stays saved when you close or refresh this page.':'No Dhan token is configured yet.'}</div>
    <p class="form-help">Dhan Web tokens have a 24-hour validity from generation. Use the saved token until its Dhan-reported expiry. Choose Edit token to replace it when needed. Dhan Client ID stays saved.</p>
    <button class="button secondary" id="edit-dhan-token" type="button" disabled>${saved?'Edit token':'Add token'}</button>
    <button class="button secondary" type="button" id="test-dhan-connection" disabled>Test saved connection</button>
    <form id="dhan-token-form" autocomplete="off" hidden>
      <label class="field">Dhan Client ID — saved permanently<input id="dhan-client-id" type="text" autocomplete="off" maxlength="60" required disabled></label>
      <label class="field">New Dhan access token<input id="dhan-token" type="password" autocomplete="new-password" spellcheck="false" required minlength="20" maxlength="4096" aria-label="New Dhan access token" disabled></label>
      <button class="button secondary" id="toggle-token" type="button" disabled>Show token</button><button class="button" type="submit" id="save-token" disabled>Save & check</button>
      <button class="button secondary" id="cancel-token-edit" type="button" disabled>Cancel edit</button>
    </form>`:(status===null?'<p class="form-help">Token settings are unavailable until connection status loads.</p>':`<p class="form-help">Admin password and secure token storage need one-time setup.</p>`)}
    <div id="token-check-status" class="form-help">${status?.credential_check?esc('Last check: '+status.credential_check.status+' · Token validity (Dhan): '+(status.credential_check.token_validity_reported||'Not reported')+' · Data plan: '+(status.credential_check.data_plan||'Not reported')):(status===null?'Token check status is unavailable.':'No token check recorded. No expiry countdown is assumed.')}</div>
    <div id="admin-dhan-result" class="form-help" role="status"></div></div></article>`;
  window.thetaRestoreDhanAdmin?.();
}
show(location.hash.slice(1));load();
