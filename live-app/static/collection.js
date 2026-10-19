'use strict';
(() => {
  let active = null, running = false, pumping = false, background = false;
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
    $('#page-collect').innerHTML = `<article class="card"><div class="card-head"><h2>Complete Dhan collection</h2></div><div class="card-body">
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
      <p>Live and depth selections save one ten-second binary sample per eligible instrument. They do not provide continuous all-market recording or historical depth. Foreign-market feeds need separate verification. Complete historical session checks and continuous live recording are pending.</p>
      <button class="button" type="submit">Create collection plan</button></form>
      <div id="collection-feedback" role="status"></div></div></article>
      <article class="card collection-card"><div class="card-head"><h2>Collection progress</h2></div><div class="card-body">
      <div id="collection-jobs"></div><button class="button secondary" id="collection-more" hidden>More jobs</button>
      <div id="collection-current" aria-live="polite">Choose a job to see progress.</div>
      <p id="collection-runner-help">Checking background worker. Saved checkpoints survive a restart or token update.</p>
      <button class="button" id="collection-resume">Resume selected job</button>
      <button class="button secondary" id="collection-pause">Pause</button>
      <button class="button secondary" id="collection-skip">Record blocked item and continue</button><button class="button secondary" id="collection-results">View results</button>
      <div id="collection-result-list"></div><button class="button secondary" id="collection-results-more" hidden>More results</button>
      </div></article>`;
  }
  let jobCursor='', resultCursor='';
  function feedback(text) { $('#collection-feedback').textContent=text; }
  function showJob(job) {
    active=job.id;
    const counts=job.counts;
    $('#collection-current').innerHTML=`<h3>${esc(job.state.replaceAll('_',' '))}</h3>
    <p>Job ${esc(job.id)} · ${esc(job.items_assessed)} work items assessed · ${esc(job.rows_saved)} rows / frames returned across batches</p>
    <div class="collection-options">${Object.entries(counts).map(([k,v])=>`<span>${esc(k)}: <b>${esc(v)}</b></span>`).join('')}</div>
    <p>${esc(job.message)}</p><p>Current item: ${esc(job.current_item ? job.current_item.family+' / '+job.current_item.symbol+' / '+job.current_item.segment+' / '+job.current_item.from+' → '+job.current_item.to : '—')}</p><p>Updated: ${esc(job.updated_at)}</p>`;
  }
  async function refresh(append=false) {
    const [cat,list,engine]=await Promise.all([get('catalogue'),get('jobs'+(append && jobCursor?'?after='+encodeURIComponent(jobCursor):'')),get('engine')]);
    background=!!engine.worker?.configured;
    $('#collection-engine').textContent='Official DhanHQ-py '+(engine.sdk?.version || 'unavailable')+' · '+(background?'Cloud worker configured':'Browser runner');
    $('#collection-runner-help').textContent=background?'Resume starts collection on the server. You may close this page; reopen and refresh to see progress. Token and request errors stop at the saved checkpoint.':'Keep this page open to run batches. Closing it stops further requests; checkpoints remain saved.';
    $('#catalogue-status').textContent=cat.rows ? `${cat.rows.toLocaleString()} catalogue entries · ${cat.fetched_at} · Review reasons: ${JSON.stringify(cat.review_reasons)}` : 'Refresh the instrument catalogue before creating a plan.';
    const links=list.jobs.map(j=>`<button type="button" class="button secondary" data-collection-job="${esc(j.id)}">${esc(j.created_at.slice(0,16))} · ${esc(j.state)} · ${esc(j.items_assessed)} items</button>`).join('');
    if (append) $('#collection-jobs').insertAdjacentHTML('beforeend',links); else $('#collection-jobs').innerHTML=links || 'No collection jobs yet.';
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
    <td>${esc(e.task.family)}<br>${esc(['daily','intraday','rolling'].includes(e.task.family) ? e.task.from+' → '+e.task.to : 'Current sample / snapshot')}</td><td>${esc(e.result.status)}<br>${esc(e.result.reason || (e.result.missing_security_ids?.length ? e.result.missing_security_ids.length+' missing instruments' : e.result.quality) || '')}</td>
    <td>${esc(e.result.rows)}${e.result.duplicate_timestamps ? ' · duplicates: '+esc(e.result.duplicate_timestamps):''}</td><td>
    ${e.result.raw_key?`<button class="button secondary" data-collection-export="${esc(e.result.raw_key)}">Raw JSON</button>`:''}
    ${['failed','empty','permission_blocked'].includes(e.result.status)?`<button class="button secondary" data-collection-retry="${esc(e.key)}">Retry</button>`:''}</td></tr>`).join('');
    if (append) $('#collection-result-body').insertAdjacentHTML('beforeend',rows);
    else $('#collection-result-list').innerHTML=`<div class="table-wrap"><table><thead><tr><th>Instrument</th><th>Data / dates</th><th>Outcome</th><th>Rows</th><th>Actions</th></tr></thead><tbody id="collection-result-body">${rows || '<tr><td colspan="5">No outcomes yet.</td></tr>'}</tbody></table></div>`;
    resultCursor=data.next_cursor || '';
    $('#collection-results-more').hidden=!resultCursor;
  }
  document.addEventListener('click',async e=>{
    const b=e.target.closest('button'); if (!b) return;
    if (!b.id.startsWith('collection-') && b.id!=='catalogue-refresh' && !Object.keys(b.dataset).some(k=>k.startsWith('collection'))) return;
    b.disabled=true;
    try {
      if (b.id==='catalogue-refresh') {feedback('Fetching full catalogue. Please keep this page open.'); await write('catalogue/refresh'); await refresh(); feedback('Catalogue saved.');}
      if (b.id==='collection-profile') {const profile=await write('profile/check');feedback('Data plan: '+profile.data_plan+' · Active segments: '+(profile.active_segments || 'Not reported')+' · Valid until: '+profile.data_validity_reported);}
      if (b.id==='collection-skip') {running=false;if(active)showJob(await write('jobs/'+active+'/skip-blocked'));}
      if (b.id==='collection-refresh') await refresh();
      if (b.id==='collection-more') await refresh(true);
      if (b.dataset.collectionJob) {running=false;active=b.dataset.collectionJob;showJob(await get('jobs/'+active));$('#collection-result-list').textContent='';$('#collection-results-more').hidden=true;}
      if (b.id==='collection-resume') {if (!active) throw Error('Select a job first.');const job=await write('jobs/'+active+'/resume');showJob(job);if(job.runner?.mode==='cloud_run_job'){background=true;running=false;feedback('Server collection started. You may close this page.');}else{running=true;void pump();}}
      if (b.id==='collection-pause') {running=false;if(active)showJob(await write('jobs/'+active+'/pause'));}
      if (b.id==='collection-results') await results();
      if (b.id==='collection-results-more') await results(true);
      if (b.dataset.collectionExport) download('dhan-raw.json',await get('export/'+b.dataset.collectionExport));
      if (b.dataset.collectionRetry) {running=false;showJob(await write('retry',{job_id:active,key:b.dataset.collectionRetry}));await refresh();feedback('Retry job created. Resume when ready.');}
    } catch(ex) {feedback(ex.message);}
    finally {b.disabled=false;}
  });
  document.addEventListener('submit',async e=>{
    if(e.target.id!=='collection-plan') return;
    e.preventDefault();
    const b=e.target.querySelector('[type="submit"]');b.disabled=true;
    try {
      const split=id=>$(id).value.split(',').map(s=>s.trim()).filter(Boolean);
      const plan={start:$('#collect-start').value,end:$('#collect-end').value,
        families:[...document.querySelectorAll('[name="collect-family"]:checked')].map(n=>n.value),
        intervals:[...document.querySelectorAll('[name="collect-interval"]:checked')].map(n=>Number(n.value)),
        symbols:split('#collect-symbols'),segments:split('#collect-segments')};
      running=false;showJob(await write('jobs',plan));await refresh();feedback('Plan created. Resume selected job to begin.');
    } catch(ex) {feedback(ex.message);}
    finally {b.disabled=false;}
  });
  window.addEventListener('hashchange',()=>{if(location.hash!=='#collect')running=false;else refresh().catch(e=>feedback(e.message));});
  setInterval(()=>{if(background && active && location.hash==='#collect')get('jobs/'+active).then(showJob).catch(e=>feedback(e.message));},15000);
  panel(); if(location.hash==='#collect')refresh().catch(e=>feedback(e.message));
})();
