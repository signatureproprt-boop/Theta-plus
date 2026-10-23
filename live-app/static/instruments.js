'use strict';
(() => {
  const root=document.getElementById('page-instruments');
  if(!root)return;
  root.innerHTML=`<article class="card"><div class="card-head"><h2>All Dhan instruments</h2></div><div class="card-body">
    <p>Search the full retained Dhan master. Select an exact exchange segment and security ID to fetch quotes or historical candles.</p>
    <form id="instrument-search"><div class="form-grid"><label class="field">Symbol / name / security ID<input id="instrument-query" maxlength="80" type="search" placeholder="RELIANCE, SENSEX, GOLD…"></label>
    <label class="field">Exchange segment<select id="instrument-segment"><option value="">All segments</option></select></label>
    <label class="field"><span>Contract filter</span><span><input id="instrument-active" type="checkbox" checked> Current contracts only</span></label></div><button class="button">Search catalogue</button></form>
    <p id="instrument-status" role="status">Search to load the catalogue. A catalogue entry does not establish live coverage.</p>
    <div class="table-wrap"><table><thead><tr><th>Instrument</th><th>Segment / ID</th><th>Type / expiry</th><th>Availability</th><th>Select</th></tr></thead><tbody id="instrument-rows"></tbody></table></div>
    <button class="button secondary" id="instrument-prev" disabled>Previous</button> <button class="button secondary" id="instrument-next" disabled>Next</button>
    </div></article><article class="card" style="margin-top:20px"><div class="card-body"><h2 id="instrument-selected">Select an instrument</h2>
    <p>Fetches use the saved Dhan connection. Unlock Admin first. Quotes are snapshots; last-trade time does not establish exchange quote freshness.</p>
    <button class="button secondary" id="instrument-recorded" disabled>Read recorded live data</button>
    <button class="button" id="instrument-quote" disabled>Fetch and save quote</button>
    <form id="instrument-history"><div class="form-grid"><label class="field">Data<select id="instrument-family"><option value="intraday">Intraday candles</option><option value="daily">Daily candles</option></select></label>
    <label class="field">Interval (minutes)<select id="instrument-interval">${[1,5,15,25,60].map(n=>`<option>${n}</option>`).join('')}</select></label>
    <label class="field">From (IST)<input id="instrument-from" type="date" required></label><label class="field">To (exclusive, IST)<input id="instrument-to" type="date" required></label></div>
    <p>Completed dates only. Up to 31 days intraday or 366 days daily per request. Provider permissions, retention and contract availability apply.</p>
    <button class="button" id="instrument-fetch-history" disabled>Fetch and save history</button></form>
    <p id="instrument-result-status" role="status"></p><button class="button secondary" id="instrument-download" hidden>Download response JSON</button>
    <pre id="instrument-result" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:480px;overflow:auto"></pre>
    </div></article>`;
  const el=id=>document.getElementById('instrument-'+id);
  const state={page:0,next:null,items:[],sha:null,selected:null,result:null,busy:false,filters:{q:'',segment:'',active_only:true}};
  const istDay=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  el('to').value=istDay;
  el('from').value=new Date(Date.parse(istDay+'T00:00:00Z')-86400000).toISOString().slice(0,10);
  function selection(){return {key:state.selected.key,catalogue_sha256:state.sha};}
  function buttons(){
    el('prev').disabled=state.busy||state.page===0;el('next').disabled=state.busy||state.next===null;
    el('recorded').disabled=el('quote').disabled=el('fetch-history').disabled=state.busy||!state.selected||Boolean(state.selected.reason)||!state.selected.segment;
    root.querySelectorAll('[data-instrument-select]').forEach(b=>{b.disabled=state.busy;});
    el('search').querySelector('button').disabled=state.busy;
  }
  async function search(page){
    if(state.busy)return;
    state.busy=true;state.selected=null;el('selected').textContent='Select an instrument';buttons();el('status').textContent='Loading exact instrument catalogue…';
    try{
      const params=new URLSearchParams({...state.filters,page});
      const response=await window.thetaApiFetch('/api/instruments?'+params,{signal:AbortSignal.timeout(120000)});
      const data=await window.thetaReadResponse(response);
      state.page=data.page;state.next=data.next_page;state.items=data.items;state.sha=data.catalogue_sha256;
      const segment=el('segment'),selected=segment.value;
      segment.replaceChildren(new Option('All segments',''),...data.segments.map(s=>new Option(s,s)),new Option('Unmapped / needs review','UNMAPPED'));segment.value=selected;
      el('status').textContent=`${data.matches.toLocaleString()} matches · master ${data.catalogue_rows.toLocaleString()} rows · page ${data.page+1} · source ${data.catalogue_at}. ${data.coverage_note}`;
      el('rows').replaceChildren();
      data.items.forEach((item,index)=>{
        const row=document.createElement('tr');
        for(const text of [item.display||item.symbol,`${item.segment||'UNMAPPED'} / ${item.security_id}`,`${item.instrument} / ${item.expiry||'—'}`,item.reason||'Identity mapped; data requires provider response']){
          const td=document.createElement('td');td.textContent=text;row.append(td);
        }
        const td=document.createElement('td'),button=document.createElement('button');button.className='button secondary';button.textContent='Select';button.dataset.instrumentSelect=String(index);td.append(button);row.append(td);el('rows').append(row);
      });
    }catch(error){state.items=[];state.next=null;el('rows').replaceChildren();el('status').textContent=error.message;}
    finally{state.busy=false;buttons();}
  }
  async function fetchData(kind,body){
    if(state.busy||!state.selected)return;
    state.busy=true;state.result=null;el('result').textContent='';el('download').hidden=true;buttons();el('result-status').textContent='Fetching selected instrument from Dhan…';
    try{
      if(kind!=='recorded'&&!window.thetaAdminRequest)throw Error('Open Admin and unlock first.');
      const data=kind==='recorded'
        ?await window.thetaReadResponse(await window.thetaApiFetch('/api/instruments/recorded?'+new URLSearchParams(body),{signal:AbortSignal.timeout(60000)}))
        :await window.thetaAdminRequest('instruments/'+kind,body);
      state.result=data;el('download').hidden=false;
      el('result-status').textContent=kind==='recorded'?`${data.state} · recorder ${data.recorder||'outside continuous scope'} · receipt age ${data.receipt_age_seconds==null?'unknown':Math.round(data.receipt_age_seconds)+'s'}. ${data.coverage_note}`:kind==='quote'?`Quote saved · ${data.status} · received ${data.fetched_at}`:`Source saved · ${data.quality.rows} validated rows · ${data.quality.coverage}. Missing data is not filled.`;
      el('result').textContent=JSON.stringify(data,null,2);
    }catch(error){el('result-status').textContent=error.message;}
    finally{state.busy=false;buttons();}
  }
  el('search').addEventListener('submit',event=>{event.preventDefault();if(state.busy)return;state.filters={q:el('query').value,segment:el('segment').value,active_only:el('active').checked};search(0);});
  el('prev').addEventListener('click',()=>search(state.page-1));el('next').addEventListener('click',()=>search(state.next));
  el('rows').addEventListener('click',event=>{const b=event.target.closest('[data-instrument-select]');if(!b||state.busy)return;state.selected=state.items[Number(b.dataset.instrumentSelect)];const i=state.selected;el('selected').textContent=`${i.display||i.symbol} · ${i.segment||'UNMAPPED'} / ${i.security_id} · ${i.expiry||i.instrument}`;el('result').textContent='';el('result-status').textContent=i.reason||'Exact catalogue identity selected';state.result=null;el('download').hidden=true;buttons();});
  el('recorded').addEventListener('click',()=>{if(state.selected)fetchData('recorded',selection());});
  el('quote').addEventListener('click',()=>{if(state.selected)fetchData('quote',selection());});
  el('history').addEventListener('submit',event=>{event.preventDefault();if(state.selected)fetchData('history',{...selection(),start:el('from').value,end:el('to').value,family:el('family').value,interval:Number(el('interval').value)});});
  el('download').addEventListener('click',()=>{if(!state.result)return;const url=URL.createObjectURL(new Blob([JSON.stringify(state.result,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='theta-dhan-instrument.json';a.click();URL.revokeObjectURL(url);});
})();
