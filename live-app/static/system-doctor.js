'use strict';
(() => {
  const node=id=>document.getElementById(id);
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let latest=null,busy=false,polling=false,findingCursor='';
  const active=()=>['QUEUED','SCANNING','REPAIRING','VERIFYING'].includes(latest?.state);
  const panel=node('page-doctor');if(!panel)return;
  panel.innerHTML=`<article class="card"><div class="card-head"><h2>System Doctor</h2></div><div class="card-body">
    <p>Scan application, security, Dhan connection, instruments, feeds, history, workers, storage, daily collection and consistency. Unlock Admin first.</p>
    <button class="button" id="doctor-scan">Full system scan</button>
    <button class="button" id="doctor-repair">Full scan &amp; safe repair</button>
    <button class="button secondary" id="doctor-refresh">Refresh report</button>
    <button class="button secondary" id="doctor-stop">Stop scan</button>
    <button class="button secondary" id="doctor-export">Download full report</button><button class="button secondary" id="doctor-findings">View detailed findings</button>
    <p>Safe repair restores retained subscriptions and requests eligible gap recovery when collection is enabled. It preserves data, leases, credentials and manually paused jobs. It does not enable paused collection or reset cloud settings.</p>
    <div id="doctor-feedback" role="status"></div><pre id="doctor-findings-list"></pre><div id="doctor-report">No report loaded. A scan continues on the server when this page closes.</div>
    </div></article>`;
  function render(data){
    if(latest?.id!==data.id){findingCursor='';node('doctor-findings-list').textContent='';}
    latest=data;
    const checks=(data.checks||[]).map(c=>`<tr><td>${escape(c.area)}<br>${escape(c.id)}</td><td>${escape(c.state)}</td><td>${escape(JSON.stringify(c.evidence))}<br>${escape(c.next_action)}</td><td>${escape(c.checked_at)}</td></tr>`).join('');
    const repairs=(data.repairs||[]).map(r=>`<li>${escape(r.action)}: ${escape(r.state)} · ${escape(r.reason||'Requested actions are not verified repairs.')}</li>`).join('');
    node('doctor-report').innerHTML=`<p><b>${escape(data.state)}</b> · Phase: ${escape(data.phase||'—')} · Started: ${escape(data.started_at||'—')} · Updated: ${escape(data.updated_at||'—')}</p>
      <p>Check results: ${escape(JSON.stringify(data.summary||{}))} · Unchecked areas: ${escape((data.unchecked_areas||data.areas||[]).join(', ')||'None outside listed checks')}</p>
      <p>Records inspected: ${escape(JSON.stringify(data.inspected||{}))} · Unreadable / scan errors: ${escape(data.scan_errors??0)} · Cursor: ${escape(data.cursor||'—')}</p>
      <p>${escape(data.scope||'No scan started.')}</p>
      <div class="table-wrap"><table><thead><tr><th>Area / check</th><th>Result</th><th>Evidence / next action</th><th>Checked</th></tr></thead><tbody>${checks||'<tr><td colspan="4">Checks are pending; nothing has passed yet.</td></tr>'}</tbody></table></div>
      <p>Archive findings: ${escape(JSON.stringify(data.findings||{}))}</p>
      ${repairs?'<p>Repair actions</p><ul>'+repairs+'</ul>':''}
      ${data.last_error?'<p>Scan retry pending: '+escape(JSON.stringify(data.last_error))+'</p>':''}
      <details><summary>Saved examples and API inventory</summary><pre>${escape(JSON.stringify({examples:data.examples||[],routes:data.api_inventory||[],before_checks:data.before_checks||[]},null,2))}</pre></details>
      <p>${escape(data.completion||'Full-system coverage is not certified.')}</p>
      <ul>${(data.limitations||[]).map(s=>'<li>'+escape(s)+'</li>').join('')}</ul>`;
    node('doctor-scan').disabled=active()||busy;node('doctor-repair').disabled=active()||busy;
    node('doctor-stop').disabled=!active()||busy;node('doctor-export').disabled=!data.id;
  }
  async function call(action,body={}){
    if(!window.thetaAdminRequest)throw Error('Open Admin and unlock first.');
    return window.thetaAdminRequest('system-doctor/'+action,body);
  }
  async function refresh(){
    if(busy||polling)return;
    polling=true;
    try{render(await call('status'));node('doctor-feedback').textContent='Report refreshed. Check NOT_VERIFIED items and limitations.';}
    catch(error){node('doctor-feedback').textContent=error.message;}
    finally{polling=false;}
  }
  document.addEventListener('click',async event=>{
    const id=event.target.closest('button')?.id;
    if(!id?.startsWith('doctor-'))return;
    if(id==='doctor-export'){
      if(!latest?.id||busy||polling)return;
      busy=true;
      try{
        const snapshot=latest,pages=[];let cursor='';
        do{
          const result=await call('findings',{after:cursor});
          if(result.scan_id!==snapshot.id)throw Error('Scan changed; refresh the report before exporting.');
          pages.push(...(result.pages||[]));cursor=result.next_cursor||'';
        }while(cursor);
        const url=URL.createObjectURL(new Blob([JSON.stringify({report:snapshot,finding_pages:pages},null,2)],{type:'application/json'}));
        const a=document.createElement('a');a.href=url;a.download='theta-system-doctor-'+snapshot.id+'.json';a.click();URL.revokeObjectURL(url);
        node('doctor-feedback').textContent='Report and all currently saved finding pages downloaded. A running scan may add more findings.';
      }catch(error){node('doctor-feedback').textContent=error.message;}
      finally{busy=false;}
      return;
    }
    if(id==='doctor-findings'){
      if(busy||polling)return;
      polling=true;
      try{
        const result=await call('findings',{after:findingCursor});
        node('doctor-findings-list').textContent+=(findingCursor?'\n':'')+JSON.stringify(result.pages||[],null,2);
        findingCursor=result.next_cursor||'';
        node('doctor-feedback').textContent=findingCursor?'More findings available. Press View detailed findings again.':'End of currently saved findings. Full report downloads every saved page.';
      }catch(error){node('doctor-feedback').textContent=error.message;}
      finally{polling=false;}
      return;
    }
    if(id==='doctor-refresh'){await refresh();return;}
    if(busy||polling)return;
    busy=true;node('doctor-scan').disabled=true;node('doctor-repair').disabled=true;
    try{
      node('doctor-feedback').textContent='Checking access and preparing the durable scan…';
      render(await call(id==='doctor-stop'?'stop':'start',id==='doctor-stop'?{}:{repair:id==='doctor-repair'}));
      node('doctor-feedback').textContent=id==='doctor-stop'?'Scan stopped at the next checkpoint. An in-flight step may finish; collection and saved data are retained.':'Scan queued on the server. You may close this page; refresh the report after unlocking Admin.';
    }catch(error){node('doctor-feedback').textContent=error.message;}
    finally{busy=false;if(latest)render(latest);else{node('doctor-scan').disabled=false;node('doctor-repair').disabled=false;}}
  });
  window.addEventListener('hashchange',()=>{if(location.hash==='#doctor')void refresh();});
  setInterval(()=>{if(location.hash==='#doctor'&&active())void refresh();},10000);
  if(location.hash==='#doctor')void refresh();
})();
