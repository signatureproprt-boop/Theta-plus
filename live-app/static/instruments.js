'use strict';
(() => {
  const root=document.getElementById('page-instruments');
  if(!root)return;
  root.innerHTML=`<article class="card"><div class="card-head"><h2>All Dhan instruments</h2></div><div class="card-body">
    <p>Browse the full Dhan catalogue. Open any instrument to read its recorded price and saved chart.</p><div id="instrument-categories" class="instrument-categories"><button type="button" class="button secondary" data-instrument-category="all" aria-pressed="true">All</button><button type="button" class="button secondary" data-instrument-category="indices" aria-pressed="false">Indices</button><button type="button" class="button secondary" data-instrument-category="cash" aria-pressed="false">Stocks / cash</button><button type="button" class="button secondary" data-instrument-category="futures" aria-pressed="false">Futures</button><button type="button" class="button secondary" data-instrument-category="options" aria-pressed="false">Options</button><button type="button" class="button secondary" data-instrument-category="currency" aria-pressed="false">Currency</button><button type="button" class="button secondary" data-instrument-category="commodities" aria-pressed="false">Commodities</button><button type="button" class="button secondary" data-instrument-category="unmapped" aria-pressed="false">Needs mapping</button></div>
    <form id="instrument-search"><div class="form-grid"><label class="field">Symbol / name / security ID<input id="instrument-query" maxlength="80" type="search" placeholder="RELIANCE, SENSEX, GOLD…"></label>
    <label class="field">Exchange segment<select id="instrument-segment"><option value="">All segments</option></select></label>
    <label class="field"><span>Contract filter</span><span><input id="instrument-active" type="checkbox" checked> Current contracts only</span></label></div><button class="button">Search catalogue</button></form>
    <p id="instrument-status" role="status">Search to load the catalogue. A catalogue entry does not establish live coverage.</p>
    <div class="table-wrap instrument-table-wrap"><table><thead><tr><th>Instrument</th><th>Segment / ID</th><th>Type / expiry</th><th>Recorded price / availability</th><th>Select</th></tr></thead><tbody id="instrument-rows"></tbody></table></div>
    <button class="button secondary" id="instrument-prev" disabled>Previous</button> <button class="button secondary" id="instrument-next" disabled>Next</button>
    </div></article><article class="card" style="margin-top:20px"><div class="card-body"><h2 id="instrument-selected">Select an instrument</h2>
    <div class="instrument-selected-actions"><button class="button secondary" id="instrument-pin" disabled>Add to watchlist</button><button class="button secondary" id="instrument-read-chart" disabled>Read saved chart</button></div><p id="instrument-price" class="instrument-price" role="status">No instrument selected.</p><p id="instrument-chart-status" role="status">Choose any instrument above to read its stored candles.</p><details class="instrument-coverage"><summary>Date-wise saved coverage</summary><div id="instrument-coverage" class="table-wrap"></div></details><div id="instrument-chart" class="instrument-candle-chart" role="img" aria-label="Selected instrument saved candlestick chart"></div><small>Charts by <a href="https://www.tradingview.com/" target="_blank" rel="noopener">TradingView</a> · Historical source rows; missing candles are never filled.</small><p>Reading saved data needs no Admin unlock. Fetching new source data uses the saved Dhan connection and requires Admin.</p>
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
  const state={page:0,next:null,items:[],sha:null,selected:null,result:null,busy:false,loaded:false,selectionVersion:0,readVersion:0,chart:null,chartData:null,filters:{q:'',segment:'',active_only:true,category:'all'}};
  const istDay=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  el('to').value=istDay;
  el('from').value=new Date(Date.parse(istDay+'T00:00:00Z')-30*86400000).toISOString().slice(0,10);
  const displayPrice=v=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString('en-IN',{maximumFractionDigits:4}):'—';
  async function read(path){for(let attempt=0;attempt<3;attempt++){try{return await window.thetaReadResponse(await window.thetaApiFetch('/api/instruments'+path,{signal:AbortSignal.timeout(120000)}));}catch(error){if(error.status!==503||attempt===2)throw error;await new Promise(resolve=>setTimeout(resolve,1500));}}}
  function selection(){return {key:state.selected.key,catalogue_sha256:state.sha};}
  function buttons(){
    el('pin').disabled=el('read-chart').disabled=state.busy||!state.selected||Boolean(state.selected.reason)||!state.selected.segment;
    el('prev').disabled=state.busy||state.page===0;el('next').disabled=state.busy||state.next===null;
    el('recorded').disabled=el('quote').disabled=el('fetch-history').disabled=state.busy||!state.selected||Boolean(state.selected.reason)||!state.selected.segment;
    root.querySelectorAll('[data-instrument-select]').forEach(b=>{b.disabled=state.busy;});
    el('search').querySelector('button').disabled=state.busy;
  }
  async function search(page){
    if(state.busy)return;
    state.busy=true;state.selected=null;state.selectionVersion++;disposeChart();el('selected').textContent='Select an instrument';el('coverage').replaceChildren();buttons();el('status').textContent='Loading exact instrument catalogue…';
    try{
      const params=new URLSearchParams({...state.filters,page,include_quotes:true});
      const data=await read('?'+params);state.loaded=true;
      state.page=data.page;state.next=data.next_page;state.items=data.items;state.sha=data.catalogue_sha256;
      const segment=el('segment'),selected=state.filters.segment;
      segment.replaceChildren(new Option('All segments',''),...data.segments.map(s=>new Option(s,s)),new Option('Unmapped / needs review','UNMAPPED'));segment.value=selected;
      el('status').textContent=`${data.matches.toLocaleString()} matches · master ${data.catalogue_rows.toLocaleString()} rows · page ${data.page+1} · source ${data.catalogue_at}. ${data.coverage_note}`;
      if(state.filters.category==='unmapped'&&data.mapping_review)el('status').textContent+=` Whole-master mapping review: ${data.mapping_review.total} total · ${data.mapping_review.current} current · ${data.mapping_review.expired} expired. API segment support remains unverified.`;
      el('rows').replaceChildren();
      data.items.forEach((item,index)=>{
        const row=document.createElement('tr');
        for(const text of [item.display||item.symbol,`${item.segment||'UNMAPPED'} / ${item.security_id}`,`${item.instrument} / ${item.expiry||'—'}`,item.reason||(item.saved_quote?displayPrice(item.saved_quote.price)+' · saved '+(item.saved_quote.trade_at||item.saved_quote.received_at):'No recorded quote; provider response needed')]){
          const td=document.createElement('td');td.textContent=text;row.append(td);
        }
        const td=document.createElement('td'),button=document.createElement('button');button.className='button secondary';button.textContent='Open';button.dataset.instrumentSelect=String(index);td.append(button);row.append(td);el('rows').append(row);
      });
    }catch(error){state.items=[];state.next=null;el('rows').replaceChildren();el('status').textContent=error.message;}
    finally{state.busy=false;buttons();if(state.pendingSearch){state.pendingSearch=false;search(0);}}
  }
  function disposeChart(){state.chart?.remove();state.chart=null;el('chart').textContent='';}
  function drawChart(data){disposeChart();state.chartData=data;const L=window.LightweightCharts;if(!L||!data?.candles?.length)return;const date=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false});state.chart=L.createChart(el('chart'),{autoSize:true,height:360,layout:{background:{type:L.ColorType.Solid,color:'#fff'},textColor:'#657580',fontSize:11},grid:{vertLines:{color:'#f1f3f5'},horzLines:{color:'#f1f3f5'}},timeScale:{timeVisible:data.family==='intraday',secondsVisible:false,tickMarkFormatter:t=>date.format(new Date(t*1000))},localization:{timeFormatter:t=>date.format(new Date(t*1000))+' IST'}});const series=state.chart.addSeries(L.CandlestickSeries,{upColor:'#008f75',downColor:'#cf5965',wickUpColor:'#008f75',wickDownColor:'#cf5965',borderVisible:false});series.setData(data.candles);const end=data.candles.length-1;state.chart.timeScale().setVisibleLogicalRange({from:Math.max(0,end-120),to:end+5});}
  function renderCoverage(data){
    const host=el('coverage');host.replaceChildren();
    const note=document.createElement('p');note.textContent='Received source candles by IST date. Missing minutes can include no-trade periods; unavailable history and lost ticks are not inferred. Other exchanges/intervals keep calendar verification explicit.';host.append(note);
    const table=document.createElement('table'),head=document.createElement('thead'),body=document.createElement('tbody'),tr=document.createElement('tr');
    for(const title of ['Date (IST)','Received rows','Missing minutes','Source session state']){const th=document.createElement('th');th.textContent=title;tr.append(th);}head.append(tr);table.append(head);
    for(const day of data.session_coverage||[]){const row=document.createElement('tr');for(const value of [day.date,day.received_rows,day.missing_minutes??'Unverified / not applicable',day.state]){const td=document.createElement('td');td.textContent=String(value??'—');row.append(td);}body.append(row);}table.append(body);host.append(table);
  }
  async function loadSaved(){if(!state.selected||state.selected.reason)return;const version=state.selectionVersion,readVersion=++state.readVersion;const selected=selection();el('coverage').replaceChildren();el('chart-status').textContent='Reading exact instrument from saved history…';const params={...selected,start:el('from').value,end:el('to').value,family:el('family').value||'intraday',interval:el('interval').value||'1'};try{const data=await read('/saved-history?'+new URLSearchParams(params));if(version!==state.selectionVersion||readVersion!==state.readVersion)return;drawChart(data);renderCoverage(data);el('chart-status').textContent=`${data.rows} saved candles · ${data.from} → ${data.to_exclusive} · ${data.state}. ${data.rows?'Historical chart; coverage not certified.':'No saved candles in this date range; try another interval/date or fetch source data.'}`;}catch(error){if(version===state.selectionVersion&&readVersion===state.readVersion)el('chart-status').textContent=error.message;}}
  async function selectedPrice(){const version=state.selectionVersion;try{const data=await read('/recorded?'+new URLSearchParams(selection()));if(version!==state.selectionVersion)return;const row=data.observation;el('price').textContent=row?`${displayPrice(row.ltp)} · stored trade ${row.trade_at||'time unavailable'} · ${data.state}`:`${data.state} · No recorded quote for this exact instrument yet.`;}catch(error){if(version===state.selectionVersion)el('price').textContent=error.message;}}
  function selectItem(item){el('coverage').replaceChildren();state.selected=item;state.selectionVersion++;const i=item;el('selected').textContent=`${i.display||i.symbol} · ${i.segment||'UNMAPPED'} / ${i.security_id} · ${i.expiry||i.instrument}`;el('result').textContent='';el('result-status').textContent=i.reason||'Exact catalogue identity selected';el('price').textContent=i.saved_quote?displayPrice(i.saved_quote.price)+' · stored observation':'Reading saved quote…';state.result=null;state.chartData=null;disposeChart();el('download').hidden=true;buttons();if(!i.reason&&i.segment){void loadSaved();void selectedPrice();}else el('chart-status').textContent=i.reason||'Segment mapping unverified; no provider query made.';el('selected').scrollIntoView?.({block:'start'});}
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
      el('result').textContent=JSON.stringify(data,null,2);if(kind==='history')void loadSaved();
    }catch(error){el('result-status').textContent=error.message;}
    finally{state.busy=false;buttons();}
  }
  el('search').addEventListener('submit',event=>{event.preventDefault();if(state.busy)return;state.filters={q:el('query').value,segment:el('segment').value,active_only:el('active').checked,category:state.filters.category};search(0);});
  el('prev').addEventListener('click',()=>search(state.page-1));el('next').addEventListener('click',()=>search(state.next));
  el('rows').addEventListener('click',event=>{const b=event.target.closest('[data-instrument-select]');if(!b||state.busy)return;selectItem(state.items[Number(b.dataset.instrumentSelect)]);});
  el('read-chart').addEventListener('click',loadSaved);
  el('pin').addEventListener('click',()=>{if(!state.selected)return;try{const keys=window.thetaWatchKeys?.()||[];if(!keys.length)keys.push(...(window.thetaWatchDefaultKeys?.()||[]));if(!keys.includes(state.selected.key))keys.push(state.selected.key);window.localStorage?.setItem('theta-watchlist-v1',JSON.stringify(keys.slice(-30)));window.dispatchEvent?.(new Event('theta-watchlist'));el('result-status').textContent='Added to Home watchlist on this browser.';}catch(error){el('result-status').textContent='Watchlist could not be saved in this browser.';}});
  el('categories').addEventListener('click',event=>{const b=event.target.closest('[data-instrument-category]');if(b)window.thetaInstrumentBrowse(b.dataset.instrumentCategory,el('query').value,el('segment').value);});
  let inputTimer;el('query').addEventListener('input',()=>{clearTimeout(inputTimer);inputTimer=setTimeout(()=>window.thetaInstrumentBrowse(state.filters.category,el('query').value,el('segment').value),400);});
  for(const id of ['segment','active'])el(id).addEventListener('change',()=>window.thetaInstrumentBrowse(state.filters.category,el('query').value,el('segment').value));
  window.thetaInstrumentBrowse=(category='all',query='',segment='')=>{state.filters={q:query,segment,active_only:el('active').checked,category};el('query').value=query;el('segment').value=segment;root.querySelectorAll('[data-instrument-category]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.instrumentCategory===category)));if(window.location)window.location.hash='instruments';if(state.busy){state.pendingSearch=true;return;}search(0);};
  window.thetaInstrumentOpen=(item,sha)=>{state.sha=sha;if(window.location)window.location.hash='instruments';selectItem(item);};
  window.addEventListener?.('hashchange',()=>{if(window.location?.hash==='#instruments'){if(!state.loaded&&!state.selected&&!state.busy)search(0);else if(state.chartData)drawChart(state.chartData);if(state.selected)el('selected').scrollIntoView?.({block:'start'});}else{state.chart?.remove();state.chart=null;}});
  if(window.location?.hash==='#instruments')search(0);
  el('recorded').addEventListener('click',()=>{if(state.selected)fetchData('recorded',selection());});
  el('quote').addEventListener('click',()=>{if(state.selected)fetchData('quote',selection());});
  el('history').addEventListener('submit',event=>{event.preventDefault();if(state.selected)fetchData('history',{...selection(),start:el('from').value,end:el('to').value,family:el('family').value,interval:Number(el('interval').value)});});
  el('download').addEventListener('click',()=>{if(!state.result)return;const url=URL.createObjectURL(new Blob([JSON.stringify(state.result,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='theta-dhan-instrument.json';a.click();URL.revokeObjectURL(url);});
})();
