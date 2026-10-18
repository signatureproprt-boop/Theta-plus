'use strict';
let roomChart=null,roomCandles=null,roomPriceLine=null,roomSource=null,roomFit=false;
const roomSelection={CE:null,PE:null};
let roomLatestTick=null;
function buildRoom(){
 if($('#room-chart'))return;
 $('#page-room').innerHTML=`<div class="room-top"><div><b>NIFTY 50</b><span id="room-mode" class="badge">NO DATA</span></div><span>5m closed candles · Dhan data</span><button class="button secondary" id="room-fit">Fit chart</button></div><div class="room-grid"><article class="room-side room-call"><div class="room-side-title">CALL / CE</div><div id="room-ce"></div></article><article class="room-chart-card"><div class="room-chart-head"><strong id="room-price">—</strong><span id="room-tick-status">Waiting for NIFTY tick</span></div><div id="room-chart" aria-label="NIFTY 5-minute candlestick chart"></div><div id="room-chart-empty" hidden>No completed candles received. Connect data or choose imported history.</div><p class="room-chart-note">Candles: completed provider bars. Dashed line: received NIFTY ticker price, not a completed candle.</p><div class="room-attribution"><a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">Chart by TradingView Lightweight Charts™</a> · <a href="/static/vendor/NOTICE" target="_blank">Notice</a> · <a href="/static/vendor/LICENSE" target="_blank">License</a></div></article><article class="room-side room-put"><div class="room-side-title">PUT / PE</div><div id="room-pe"></div></article></div><div class="room-bottom"><article class="card"><div class="card-head"><h2>Signal evidence</h2><span id="room-decision" class="badge">WAIT</span></div><div id="room-evidence" class="card-body"></div></article><article class="card"><div class="card-head"><h2>Plan & journal</h2></div><div class="card-body"><p>Choose an exact Call or Put contract, then calculate quantity, premium and planned risk.</p><button class="full-button" data-go="decision">Open risk planner</button><button class="button secondary" data-go="journal">View saved plans</button><p class="form-help">Research workflow. Broker execution is disabled.</p></div></article></div>`;
 if(!window.LightweightCharts){$('#room-chart-empty').hidden=false;$('#room-chart-empty').textContent='Chart library could not load.';return;}
 roomChart=LightweightCharts.createChart($('#room-chart'),{autoSize:true,layout:{background:{type:'solid',color:'#0d151c'},textColor:'#9fb1bf',attributionLogo:true},grid:{vertLines:{color:'#1e2b34'},horzLines:{color:'#1e2b34'}},rightPriceScale:{borderColor:'#30404c'},timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#30404c',tickMarkFormatter:t=>new Date(Number(t)*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false})},localization:{timeFormatter:t=>new Date(Number(t)*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',hour12:false})}});
 roomCandles=roomChart.addSeries(LightweightCharts.CandlestickSeries,{upColor:'#85dfbb',downColor:'#eb929c',borderVisible:false,wickUpColor:'#85dfbb',wickDownColor:'#eb929c'});
}
function roomSide(side){
 const rows=(data?.contracts||[]).filter(c=>c.side===side).sort((a,b)=>Math.abs(a.strike-data.spot)-Math.abs(b.strike-data.spot));
 let c=rows.find(r=>r.security_id===roomSelection[side]);if(!c)c=rows[0];roomSelection[side]=c?.security_id||null;
 const target=side==='CE'?'#room-ce':'#room-pe';
 if(!c){$(target).innerHTML='<p class="room-missing">No supplied option quotes.</p>';return;}
 const old=!c.quote_at||Date.now()-Date.parse(c.quote_at)>60000;
 const spread=c.ask>=c.bid?c.ask-c.bid:null;
 $(target).innerHTML=`<label class="field">Contract<select data-room-side="${side}">${rows.map(r=>`<option value="${esc(r.security_id)}" ${r.security_id===c.security_id?'selected':''}>${num(r.strike)} ${side} · ${esc(r.expiry)}</option>`).join('')}</select></label><div class="room-premium"><small>ASK / BID</small><strong>${num(c.ask)} <span>/ ${num(c.bid)}</span></strong></div><dl>${[['Spread',num(spread)],['Open interest',num(c.oi)],['Volume',num(c.volume)],['IV',num(c.iv)],['Delta',num(c.delta)],['Lot size',num(c.lot_size)]].map(([k,v])=>`<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl><p class="room-quote-state ${old?'amber':'muted'}">${selected?'Historical snapshot':old?'Old or missing trade time':'Recent trade time'} · option REST snapshot</p><p class="room-quote-time">${esc(when(c.quote_at))}</p><button class="full-button" data-room-plan="${side}" ${!c.lot_size?'disabled':''}>Plan ${side}</button><p class="form-help">Selection is for inspection, not a buy recommendation.</p>`;
}
function updateRoom(){
 if(view!=='room')return;buildRoom();
 $('#room-mode').textContent=data?.mode||'NO_DATA';$('#room-decision').textContent=data?.decision||'WAIT';
 $('#room-price').textContent=num(data?.spot);$('#room-tick-status').textContent=selected?'Historical dataset · live ticks excluded':'Waiting for fresh NIFTY tick';
 const intraday=data?.trend?.intraday_15m,daily=data?.trend?.prior_daily;
 const trendRow=(label,item)=>`<div class="evidence-row"><span>${label}</span>${badge(item?.direction||'UNKNOWN')}</div><p class="form-help">${esc(item?.reason||'No timeframe candles')} · last complete: ${esc(item?.as_of?when(item.as_of):'none')} · invalidation: ${item?.invalidation==null?'—':num(item.invalidation)}</p>`;
 $('#room-evidence').innerHTML=evidence()+trendRow('15-minute structure',intraday)+trendRow('Prior daily structure',daily)+`<div class="evidence-row"><span>Full trend + options research gate</span>${badge(data?.trend_decision||'WAIT')}</div><p class="reason">${esc(data?.chart?.reason||'Chart evidence unavailable')}</p><p class="reason">${esc(data?.options?.reason||'Option evidence unavailable')}</p><p class="form-help">The default combined hypothesis above has not changed. Full trend research requires aligned 5m, 15m, four prior complete daily sessions and fixed-strike option evidence; unavailable inputs yield WAIT. Backtest this gate in Strategy lab. Unvalidated; no buy recommendation.</p>`;
 roomSide('CE');roomSide('PE');
 if(!roomCandles)return;
 const current=selected||'provider';if(current!==roomSource){roomSource=current;roomFit=false;if(roomPriceLine){roomCandles.removePriceLine(roomPriceLine);roomPriceLine=null;}}
 const bars=(data?.bars||[]).map(b=>({time:Math.floor(Date.parse(b.start)/1000),open:b.open,high:b.high,low:b.low,close:b.close})).sort((a,b)=>a.time-b.time);
 roomCandles.setData(bars);$('#room-chart-empty').hidden=!!bars.length;
 if(bars.length&&!roomFit){roomChart.timeScale().fitContent();roomFit=true;}
}
document.addEventListener('theta:loaded',updateRoom);
document.addEventListener('theta:view',updateRoom);
document.addEventListener('theta:tick',e=>{
 const t=e.detail;if(t.key==='nifty')roomLatestTick=t;
 if(view!=='room'||selected||t.key!=='nifty'||!roomCandles)return;
 $('#room-price').textContent=num(t.price);$('#room-tick-status').textContent=(t.status==='RECENT_TRADE'?'Recent NIFTY tick':'NIFTY tick; trade time stale/unknown')+' · '+when(t.quote_at);
 if(roomPriceLine)roomCandles.removePriceLine(roomPriceLine);
 roomPriceLine=roomCandles.createPriceLine({price:t.price,color:'#d5bf86',lineWidth:1,lineStyle:2,axisLabelVisible:true,title:'Received tick'});
});
$('#page-room').addEventListener('change',e=>{const side=e.target.dataset.roomSide;if(side){roomSelection[side]=e.target.value;roomSide(side);}});
$('#page-room').addEventListener('click',e=>{
 if(e.target.closest('#room-fit'))roomChart?.timeScale().fitContent();
 const side=e.target.closest('[data-room-plan]')?.dataset.roomPlan;
 if(side){go('decision');if($('#contract'))$('#contract').value=roomSelection[side];}
});
updateRoom();

setInterval(()=>{if(view==='room'&&!selected&&roomLatestTick&&$('#room-tick-status')){const t=roomLatestTick;const age=t.quote_at?Date.now()-Date.parse(t.quote_at):Infinity;$('#room-tick-status').textContent=(age>=0&&age<=60000?'Recent NIFTY trade':'Last NIFTY trade: stale / unknown')+' · '+when(t.quote_at);}},5000);
