'use strict';
(() => {
  let active = null, running = false, pumping = false, background = false, lastJob = null;
  const families = ['daily','intraday','rolling','chain','quotes','live','depth20','depth200'];
  const labels = {daily:'Daily history',intraday:'Intraday history',rolling:'Expired options',chain:'All active expiries / chains',quotes:'Quote snapshots',live:'Live feed samples',depth20:'20-level depth samples',depth200:'200-level depth samples'};
  const get = async path => {
    const response = await window.thetaApiFetch('/api/collection/'+path,{signal:AbortSignal.timeout(30000)});
    const result = await window.thetaReadResponse(response);
    if (!response.ok) throw Error(result.detail || 'Could not read collection status');
    return result;
  };
  const write = (path,body={}) => {
    if (!window.thetaAdminRequest) throw Error('Open Admin and unlock collection first.');
    return window.thetaAdminRequest('collection/'+path,body);
  };
  function panel() {
    const end = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    const startDate=new Date(end+'T00:00:00Z');
    startDate.setUTCFullYear(startDate.getUTCFullYear()-5);
    const start=startDate.toISOString().slice(0,10);
    $('#page-collect').innerHTML = `<article class="card"><div class="card-head"><h2>Automatic data system</h2></div><div class="card-body">
      <button class="button" id="data-system-start">Start automatic data system</button>
      <button class="button secondary" id="data-system-pause">Pause recording / schedules</button>
      <button class="button secondary" id="data-system-refresh">Refresh data status</button>
      <div id="data-system-feedback" role="status"></div><div id="data-system-status">Checking unattended recorder and daily updates…</div>
      </div></article><article class="card"><div class="card-head"><h2>Complete Dhan collection</h2></div><div class="card-body">
      <p>Collect the available instrument catalogue, history and market snapshots. Every selected item gets an outcome; unavailable data is listed with its reason.</p>
      <p>Open <button class="button secondary" data-go="admin">Admin</button> and unlock with your existing password to manage collection.</p>
      <button class="button" id="catalogue-refresh">Refresh full instrument catalogue</button>
      <button class="button secondary" id="collection-profile">Check subscription / segments</button><button class="button secondary" id="collection-refresh">Refresh progress</button>
      <div id="collection-engine" role="status">Checking data engine…</div>
      <div id="catalogue-status" role="status">Catalogue not loaded.</div>
      <form id="collection-plan"><div class="form-grid">
        <label class="field">History from (IST)<input type="date" id="collect-start" value="${start}" required></label>
        <label class="field">To (exclusive, IST)<input type="date" id="collect-end" value="${end}" required></label>
        <label class="field">Symbols (blank = all)<input id="collect-symbols" placeholder="NIFTY, BANKNIFTY"></label>
        <label class="field">API segments (blank = all)<input id="collect-segments" placeholder="NSE_EQ, NSE_FNO, MCX_COMM"></label>
      </div><fieldset><legend>Data to collect</legend><div class="collection-options">${families.map(f=>`<label><input type="checkbox" name="collect-family" value="${f}" checked> ${labels[f]}</label>`).join('')}</div></fieldset>
      <fieldset><legend>Intraday / expired-option intervals</legend><div class="collection-options">${[1,5,15,25,60].map(i=>`<label><input type="checkbox" name="collect-interval" value="${i}" checked> ${i} min</label>`).join('')}</div></fieldset>
      <p>The default range is the last five years; it does not establish full historical coverage. Daily history is requested from your chosen start date. First verify a short range for one symbol before creating a full plan. Minute and rolling history have a documented five-year horizon. Full-universe collection can take a long time and use substantial storage.</p>
      <p>Live and depth selections save one ten-second binary sample per eligible instrument. They do not provide continuous all-market recording or historical depth. Foreign-market feeds need separate verification. Use Automatic data system above for unattended recording and daily catch-up. Subscription scope and remaining gaps are shown separately.</p>
      <label><input type="checkbox" id="collect-continue-rejected"> Continue other data when Dhan rejects a request; record it as failed / not collected. Token and permission errors still stop.</label><button class="button" type="submit">Create collection plan</button></form>
      <div id="collection-feedback" role="status"></div></div></article>
      <article class="card collection-card"><div class="card-head"><h2>Collection progress</h2></div><div class="card-body">
      <div id="collection-jobs"></div><button class="button secondary" id="collection-more" hidden>More jobs</button>
      <div id="collection-current" aria-live="polite">Choose a job to see progress.</div>
      <p id="collection-runner-help">Checking background worker. Saved checkpoints survive a restart or token update.</p>
      <button class="button" id="collection-resume">Resume selected job</button>
      <button class="button secondary" id="collection-pause">Pause</button><button class="button secondary" id="collection-stop-on-rejected">Stop on future rejections</button>
      <button class="button secondary" id="collection-skip">Record blocked item and continue</button><button class="button secondary" id="collection-results">View results</button>
      <div id="collection-result-list"></div><button class="button secondary" id="collection-results-more" hidden>More results</button>
      </div></article>`;
  }
  let systemBusy=false;
  async function refreshDataSystem(){
    if(systemBusy)return;systemBusy=true;
    try{
      const response=await window.thetaApiFetch('/api/data-system',{signal:AbortSignal.timeout(30000)});
      const data=await window.thetaReadResponse(response);
      const streams=Object.entries(data.streams||{}).map(([name,r])=>`<tr><td>${esc(name)}</td><td>${esc(r.state)}</td><td>${esc(r.subscribed??0)}</td><td>${esc(r.last_durable_received_at||'No durable frames yet')}</td><td>${esc(r.missing_since_connection??'—')}</td></tr>`).join('');
      const history=Object.entries(data.maintenance?.history||{}).map(([name,r])=>`<tr><td>${esc(name)}${r.scope?'<br>'+esc(r.scope):''}</td><td>${esc(r.state)}</td><td>${esc(r.start)} → ${esc(r.end)} exclusive</td><td>${esc(JSON.stringify(r.counts||{}))}</td></tr>`).join('');
      const outcomes=Object.entries(data.audit?.job_groups||{}).map(([name,r])=>`<tr><td>${esc(name)}</td><td>${esc(JSON.stringify(r.states))}</td><td>${esc(JSON.stringify(r.attempt_outcomes))}</td></tr>`).join('');
      const additions=Object.entries(data.backfill?.catalogue_updates?.families||{}).map(([name,r])=>`<tr><td>${esc(name)}</td><td>${esc(r.added_identities)}</td><td>${r.job_id?`<button type="button" class="button secondary" data-collection-job="${esc(r.job_id)}">Open history job</button>`:'No additions in this update'}</td></tr>`).join('');
      $('#data-system-status').innerHTML=`<p><b>${data.enabled?'Enabled':'Paused / not enabled'}</b> · Worker: ${data.worker_active?'Active':'Not active'} · Maintenance: ${esc(data.maintenance?.state||'Not started')} · NSE session: ${esc(data.calendar?.state||'Unverified')}</p>
        <p role="status">Collector release: ${data.worker_build_matches?'Matches serving app':'Not verified / older execution; release activation required'}</p>
        <p>System scan: ${esc(data.doctor_progress?.state||'Not started')} · Phase: ${esc(data.doctor_progress?.phase||'—')} · Records inspected: ${esc(Object.values(data.doctor_progress?.inspected||{}).reduce((sum,n)=>sum+(Number(n)||0),0))} · Scan errors: ${esc(data.doctor_progress?.scan_errors??0)} · Updated: ${esc(data.doctor_progress?.updated_at||'—')}. Detailed findings require Admin; scan progress does not certify full data coverage.</p>
        ${data.scope_warning?'<p role="status">'+esc(data.scope_warning)+'</p>':''}
        <p>Recent internal diagnostics: ${esc(data.diagnostics?.total??0)} · ${esc((data.diagnostics?.items||[]).slice(0,3).map(r=>r.stage+': '+r.exception_type+' ['+r.id+']').join(' · '))}</p>
        <p>${esc(data.scope||'Subscription scope has not been prepared.')} · Live capacity excluded: ${esc(data.live_capacity_excluded??'Not assessed')}.</p>
        <div class="table-wrap"><table><thead><tr><th>Feed</th><th>State</th><th>Subscribed</th><th>Last durable receipt</th><th>Not observed since connection</th></tr></thead><tbody>${streams}</tbody></table></div>
        <p>Live recorder capacity: ${esc(data.live_capacity??10000)} · Eligible current identities: ${esc(data.live_candidate_count??'Not assessed')} · Outside recorder capacity: ${esc(data.live_capacity_excluded??'Not assessed')}. Other instruments use quote/history sweeps; simultaneous all-market coverage is not claimed.</p>
        <p>Depth20: ${esc(data.depth20_scope||'Scope not resolved')} · Other contracts not subscribed: ${esc(data.depth20_unsubscribed??'Not assessed')}. Depth200 records one exact NIFTY futures contract.</p>
        <div class="table-wrap"><table><thead><tr><th>New catalogue history</th><th>Added / changed identities</th><th>Backfill progress</th></tr></thead><tbody>${additions||'<tr><td colspan="3">Catalogue additions check pending.</td></tr>'}</tbody></table></div>
        <p>New identities use the historical ranges of the existing backfill plans. Queued work does not establish completed coverage; inspect each job for failed, empty and partial outcomes.</p>
        <div class="table-wrap"><table><thead><tr><th>Daily catch-up</th><th>Job state</th><th>Requested dates</th><th>Outcomes</th></tr></thead><tbody>${history||'<tr><td colspan="4">Daily catch-up not scheduled yet.</td></tr>'}</tbody></table></div>
        <div class="table-wrap"><table><thead><tr><th>Saved job families</th><th>States</th><th>Attempt outcomes (includes retries)</th></tr></thead><tbody>${outcomes||'<tr><td colspan="3">Audit pending.</td></tr>'}</tbody></table></div>
        <p>Unresolved catalogue mappings: ${esc(JSON.stringify(data.audit?.unresolved_catalogue_reasons||{}))}. Outcome counts are attempts, not a coverage percentage.</p>
        <p>Watchdog: ${data.watchdog?.reported_configured?'Configured by deployment; restart acceptance test pending':'Not configured'} · ${esc(data.completion||'Coverage not certified')}</p>
        ${data.maintenance?.error?'<p role="status">'+esc(data.maintenance.error)+'</p>':''}`;
    }catch(error){$('#data-system-status').textContent='Data system status unavailable: '+error.message;}
    finally{systemBusy=false;}
  }
  let jobCursor='', resultCursor='';
  function feedback(text) { $('#collection-feedback').textContent=text; }
  function syncPolicyButtons(){const job=lastJob;if(!job)return;
    $('#collection-stop-on-rejected').disabled=!job.config?.continue_rejected_requests||job.state==='running';
    $('#collection-skip').disabled=!['request_review','permission_blocked'].includes(job.state);
    $('#collection-skip').textContent=job.state==='request_review'?'Record rejection as failed and continue other data':'Record blocked item and continue';
    $('#collection-skip').dataset.action=job.state==='request_review'?'continue-rejected':'skip-blocked';
  }
  function showJob(job) {
    active=job.id;
    const counts=job.counts;
    lastJob=job;syncPolicyButtons();
    $('#collection-current').innerHTML=`<h3>${esc(job.state.replaceAll('_',' '))}</h3>
    <p>Job ${esc(job.id)} · ${esc(job.items_assessed)} work items assessed · ${esc(job.rows_saved)} rows / frames returned across batches</p>
    <div class="collection-options">${Object.entries(counts).map(([k,v])=>`<span>${esc(k)}: <b>${esc(v)}</b></span>`).join('')}</div>
    <p>${esc(job.message)}</p><p>Scope: ${esc((job.config?.families||[]).join(', ')||'Not reported')} · Symbols: ${esc(job.config?.symbols?.join(', ')||'All')} · Segments: ${esc(job.config?.segments?.join(', ')||'All mapped')} · ${esc(job.config?.start||'—')} → ${esc(job.config?.end||'—')} (exclusive)</p>
    ${job.chain_discovery?`<p>Chain discovery: ${esc(job.chain_discovery.underlyings)} option-bearing underlyings · unresolved option rows: ${esc(JSON.stringify(job.chain_discovery.unresolved_option_rows))}. Discovery is not verified coverage.</p>`:''}
    <p>Current item: ${esc(job.current_item ? job.current_item.family+' / '+job.current_item.symbol+' / '+job.current_item.segment+' / '+job.current_item.from+' → '+job.current_item.to : '—')}</p><p>Updated: ${esc(job.updated_at)}</p>`;
  }
  async function refresh(append=false) {
    void refreshDataSystem();
    const [cat,list,engine]=await Promise.all([get('catalogue'),get('jobs'+(append && jobCursor?'?after='+encodeURIComponent(jobCursor):'')),get('engine')]);
    background=!!engine.worker?.configured;
    $('#collection-engine').textContent='Official DhanHQ-py '+(engine.sdk?.version || 'unavailable')+' · '+(background?'Cloud worker configured':'Browser runner');
    $('#collection-runner-help').textContent=background?'Resume starts collection on the server. You may close this page; reopen and refresh to see progress. Token and request errors stop at the saved checkpoint.':'Keep this page open to run batches. Closing it stops further requests; checkpoints remain saved.';
    $('#catalogue-status').textContent=cat.rows ? `${cat.rows.toLocaleString()} catalogue entries · ${cat.fetched_at} · Review reasons: ${JSON.stringify(cat.review_reasons)}` : 'Refresh the instrument catalogue before creating a plan.';
    const links=list.jobs.map(j=>`<button type="button" class="button secondary" data-collection-job="${esc(j.id)}">${esc(j.created_at.slice(0,16))} · ${esc(j.state)} · ${esc(j.items_assessed)} items</button>`).join('');
    const issues=list.read_issues || [];
    const warning=issues.length?`<p role="status">${esc(issues.length)} job records unavailable on this page; saved data was not changed. Review: ${esc(issues.map(i=>i.document).join(', '))}</p>`:'';
    if (append) $('#collection-jobs').insertAdjacentHTML('beforeend',links+warning); else $('#collection-jobs').innerHTML=(links || (issues.length?'':'No collection jobs yet.'))+warning;
    jobCursor=list.next_cursor || '';
    $('#collection-more').hidden=!jobCursor;
    if (active) showJob(await get('jobs/'+active));
  }
  async function pump() {
    if (pumping) return;
    pumping=true;
    try {
      while (running && active && location.hash==='#collect') {
        const id=active;
        const job=await write('jobs/'+id+'/run');
        if (id!==active) break;
        showJob(job);
        if (job.state!=='running') { running=false; break; }
        await new Promise(resolve=>setTimeout(resolve,3500));
      }
    } catch (e) {running=false;feedback(e.message);}
    finally {pumping=false;}
  }
  async function results(append=false) {
    if (!active) throw Error('Select a collection job first.');
    const data=await get('jobs/'+active+'/results'+(append && resultCursor?'?after='+encodeURIComponent(resultCursor):''));
    const rows=data.items.map(e=>`<tr><td>${esc(e.task.item.symbol)}<br>${esc(e.task.item.segment || e.task.item.exchange+'_'+e.task.item.raw_segment)}</td>
    <td>${esc(e.task.family)}<br>${esc(['daily','intraday','rolling'].includes(e.task.family) ? e.task.from+' → '+e.task.to : 'Current sample / snapshot')}</td><td>${esc(e.result.status)}<br>${esc(e.result.reason || e.result.quality || '')}${e.result.quarantined_rows?'<br>'+esc(e.result.quarantined_rows)+' invalid candles excluded; coverage incomplete':''}${e.result.missing_security_ids?.length?'<br>'+esc(e.result.missing_security_ids.length)+' missing instruments':''}${e.result.instruments_assessed?'<br>'+esc(e.result.instruments_assessed)+' identities assessed':''}${e.result.isolated_probes?'<br>'+esc(e.result.isolated_probes)+' isolated probes':''}<br>${esc(e.result.coverage||'Coverage not verified')}</td>
    <td>${esc(e.result.rows)}${e.result.duplicate_timestamps ? ' · duplicates: '+esc(e.result.duplicate_timestamps):''}${e.result.missing_sessions?'<br>Missing sessions: '+esc(e.result.missing_sessions):''}${e.result.missing_minutes?'<br>Missing minutes: '+esc(e.result.missing_minutes):''}</td><td>
    ${e.result.raw_key?`<button class="button secondary" data-collection-export="${esc(e.result.raw_key)}">Raw JSON</button>`:''}
    ${e.result.quarantine_key?`<button class="button secondary" data-collection-export="${esc(e.result.quarantine_key)}">Excluded candles</button>`:''}
    ${['failed','partial','empty','permission_blocked'].includes(e.result.status)?`<button class="button secondary" data-collection-retry="${esc(e.key)}">Retry missing / failed</button><button class="button secondary" data-collection-diagnose="${esc(e.key)}">Diagnose</button>`:''}</td></tr>`).join('');
    if (append) $('#collection-result-body').insertAdjacentHTML('beforeend',rows);
    else $('#collection-result-list').innerHTML=`<div class="table-wrap"><table><thead><tr><th>Instrument</th><th>Data / dates</th><th>Outcome</th><th>Rows</th><th>Actions</th></tr></thead><tbody id="collection-result-body">${rows || '<tr><td colspan="5">No outcomes yet.</td></tr>'}</tbody></table></div>`;
    resultCursor=data.next_cursor || '';
    $('#collection-results-more').hidden=!resultCursor;
  }
  document.addEventListener('click',async e=>{
    const b=e.target.closest('button'); if (!b) return;
    if (!b.id.startsWith('data-system-') && !b.id.startsWith('collection-') && b.id!=='catalogue-refresh' && !Object.keys(b.dataset).some(k=>k.startsWith('collection'))) return;
    b.disabled=true;
    try {
      if (b.id==='data-system-start'||b.id==='data-system-pause') {
        if(!window.thetaAdminRequest)throw Error('Open Admin and unlock first.');
        await window.thetaAdminRequest('data-system/'+(b.id==='data-system-start'?'start':'pause'),{});
        $('#data-system-feedback').textContent=b.id==='data-system-start'?'Automatic data system enabled. Check actual recorder, daily jobs and watchdog status below.':'Recording and automatic schedules paused. Historical jobs retain their separate controls.';
        await refreshDataSystem();
      }
      if (b.id==='data-system-refresh')await refreshDataSystem();
      if (b.id==='catalogue-refresh') {feedback('Fetching full catalogue. Please keep this page open.'); await write('catalogue/refresh'); await refresh(); feedback('Catalogue saved.');}
      if (b.id==='collection-profile') {feedback('Checking subscription and bounded NSE commodity support…');const profile=await write('profile/check');const probe=profile.unmapped_commodity_check;feedback('Data plan: '+profile.data_plan+' · Active segments: '+(profile.active_segments || 'Not reported')+' · Valid until: '+profile.data_validity_reported+(probe?' · NSE commodity sample: '+probe.state+' · Catalogue mapping unchanged.':''));}
      if (b.id==='collection-skip') {running=false;if(active){const job=await write('jobs/'+active+'/'+(b.dataset.action||'skip-blocked'));showJob(job);feedback(job.message+' Press Resume selected job to start the worker.');}}
      if (b.id==='collection-refresh') await refresh();
      if (b.id==='collection-more') await refresh(true);
      if (b.dataset.collectionJob) {running=false;active=b.dataset.collectionJob;showJob(await get('jobs/'+active));$('#collection-result-list').textContent='';$('#collection-results-more').hidden=true;}
      if (b.id==='collection-resume') {if (!active) throw Error('Select a job first.');const job=await write('jobs/'+active+'/resume');showJob(job);if(job.runner?.mode==='cloud_run_job'){background=true;running=false;feedback('Server collection started. You may close this page.');}else{running=true;void pump();}}
      if (b.id==='collection-stop-on-rejected') {if(active)showJob(await write('jobs/'+active+'/stop-on-rejected'));}
      if (b.id==='collection-pause') {running=false;if(active)showJob(await write('jobs/'+active+'/pause'));}
      if (b.id==='collection-results') await results();
      if (b.id==='collection-results-more') await results(true);
      if (b.dataset.collectionExport) download('dhan-raw.json',await get('export/'+b.dataset.collectionExport));
      if (b.dataset.collectionRetry) {running=false;showJob(await write('retry',{job_id:active,key:b.dataset.collectionRetry}));await refresh();feedback('Retry job created. Resume when ready.');}
      if (b.dataset.collectionRepair) {running=false;showJob(await write('jobs/'+active+'/repair',{result_key:b.dataset.collectionRepair}));await refresh();feedback('Verified-range retry prepared. Resume to save that range; original historical gap remains visible.');}
      if (b.dataset.collectionDiagnose) {const id=active;feedback('Checking exact master identity and a bounded provider request. The saved checkpoint is retained.');const report=await write('jobs/'+id+'/diagnose',{result_key:b.dataset.collectionDiagnose});feedback('Diagnosis: '+report.conclusion+' · '+report.probes.map(p=>p.endpoint+': '+p.state+(p.rows!==undefined?' ('+p.rows+' rows)':'')).join(' · '));if(report.can_repair)$('#collection-feedback').insertAdjacentHTML('beforeend',` <button class="button secondary" data-collection-repair="${esc(b.dataset.collectionDiagnose)}">Prepare verified-range retry</button>`);}
    } catch(ex) {feedback(ex.message);}
    finally {b.disabled=false;syncPolicyButtons();}
  });
  document.addEventListener('submit',async e=>{
    if(e.target.id!=='collection-plan') return;
    e.preventDefault();
    const b=e.target.querySelector('[type="submit"]');b.disabled=true;
    try {
      const split=id=>$(id).value.split(',').map(s=>s.trim()).filter(Boolean);
      const plan={continue_rejected_requests:!!$('#collect-continue-rejected').checked,start:$('#collect-start').value,end:$('#collect-end').value,
        families:[...document.querySelectorAll('[name="collect-family"]:checked')].map(n=>n.value),
        intervals:[...document.querySelectorAll('[name="collect-interval"]:checked')].map(n=>Number(n.value)),
        symbols:split('#collect-symbols'),segments:split('#collect-segments')};
      running=false;showJob(await write('jobs',plan));await refresh();feedback('Plan created. Resume selected job to begin.');
    } catch(ex) {feedback(ex.message);}
    finally {b.disabled=false;syncPolicyButtons();}
  });
  window.addEventListener('hashchange',()=>{if(location.hash!=='#collect')running=false;else refresh().catch(e=>feedback(e.message));});
  setInterval(()=>{if(location.hash==='#collect')void refreshDataSystem();},15000);
  setInterval(()=>{if(background && active && location.hash==='#collect')get('jobs/'+active).then(showJob).catch(e=>feedback(e.message));},15000);
  panel(); if(location.hash==='#collect')refresh().catch(e=>feedback(e.message));
})();
