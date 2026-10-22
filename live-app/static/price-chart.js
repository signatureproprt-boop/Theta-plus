'use strict';
(() => {
  const date=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false});
  const time=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false});
  const calendar=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short'});
  const number=v=>Number(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
  let view=null,expanded=false;const ranges=new Map();
  function dispose(){
    if(!view)return;
    const range=view.chart.timeScale().getVisibleLogicalRange(),timeRange=view.chart.timeScale().getVisibleRange?.();
    if(range&&view.hadData)ranges.set(view.key,{logical:range,time:timeRange});
    if(ranges.size>8)ranges.delete(ranges.keys().next().value);
    view.observer?.disconnect();document.removeEventListener('fullscreenchange',view.fullscreenChanged);
    window.removeEventListener('resize',view.resize);view.root?.classList.remove('chart-expanded');
    view.chart.remove();view=null;
  }
  function render(element,model){
    const L=window.LightweightCharts,readout=document.getElementById('research-chart-readout');
    if(!L){if(readout)readout.textContent='Chart library unavailable. Reload to retry; saved data is retained.';return;}
    if(view&&(view.element!==element||view.key!==model.key))dispose();
    if(!view){
      const chart=L.createChart(element,{autoSize:true,layout:{background:{type:L.ColorType.Solid,color:'#ffffff'},textColor:'#63748a',fontSize:11,attributionLogo:true},
        grid:{vertLines:{color:'#f0f3f7'},horzLines:{color:'#edf1f6'}},crosshair:{mode:L.CrosshairMode.Normal},
        rightPriceScale:{borderColor:'#dce4ed',scaleMargins:{top:.12,bottom:.12}},
        timeScale:{borderColor:'#dce4ed',timeVisible:true,secondsVisible:false,rightOffset:6,barSpacing:8,
          tickMarkFormatter:(t,type)=>[L.TickMarkType.Time,L.TickMarkType.TimeWithSeconds].includes(type)?time.format(new Date(t*1000)):calendar.format(new Date(t*1000))},
        localization:{locale:'en-IN',timeFormatter:t=>date.format(new Date(t*1000))+' IST'},
        handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:false},handleScale:{mouseWheel:true,pinch:true,axisPressedMouseMove:true}});
      const candles=chart.addSeries(L.CandlestickSeries,{upColor:'#148570',downColor:'#ce5969',borderVisible:false,wickUpColor:'#148570',wickDownColor:'#ce5969',priceLineVisible:false});
      const ema20=chart.addSeries(L.LineSeries,{color:'#3d81d4',lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
      const ema50=chart.addSeries(L.LineSeries,{color:'#cf942e',lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
      const rsi=chart.addSeries(L.LineSeries,{title:'RSI14',color:'#7755b5',lineWidth:1,priceLineVisible:false,priceFormat:{type:'price',precision:2,minMove:.01},autoscaleInfoProvider:()=>({priceRange:{minValue:0,maxValue:100}})},1);
      const adx=chart.addSeries(L.LineSeries,{title:'ADX14',color:'#087f85',lineWidth:1,priceLineVisible:false,priceFormat:{type:'price',precision:2,minMove:.01},autoscaleInfoProvider:()=>({priceRange:{minValue:0,maxValue:100}})},2);
      for(const value of [30,70])rsi.createPriceLine({price:value,title:String(value),color:'#b9accf',lineWidth:1,lineStyle:L.LineStyle.Dashed,axisLabelVisible:false});
      chart.panes?.().forEach((pane,index)=>pane.setStretchFactor(index===0?3:1));
      const root=element.closest('.room-chart'),resize=()=>{if(!view||view.element!==element)return;chart.applyOptions({autoSize:true});};
      const applyExpanded=()=>{root?.classList.toggle('chart-expanded',expanded);const button=document.getElementById('chart-fullscreen');if(button){button.textContent=expanded?'Exit fullscreen':'Fullscreen';button.setAttribute('aria-pressed',String(expanded));}};
      const fullscreenChanged=()=>{if(!document.fullscreenElement)expanded=false;applyExpanded();resize();};
      const observer=typeof ResizeObserver==='undefined'?null:new ResizeObserver(resize);observer?.observe(element);
      view={element,key:model.key,chart,candles,ema20,ema50,rsi,adx,root,resize,fullscreenChanged,observer,model:null,levels:[],ltp:null,ltpPrice:null,hadData:false};
      applyExpanded();
      window.addEventListener('resize',resize);document.addEventListener('fullscreenchange',fullscreenChanged);
      chart.subscribeCrosshairMove(param=>{
        if(!view||view.element!==element)return;const row=param.seriesData?.get(candles);
        if(readout)readout.textContent=row&&row.open!==undefined?describe(row,view.model)+` · EMA20 ${indicator(param,ema20)} · EMA50 ${indicator(param,ema50)} · RSI14 ${indicator(param,rsi)} · ADX14 ${indicator(param,adx)}`:view.latest;
      });
      const zoom=factor=>{const scale=chart.timeScale(),r=scale.getVisibleLogicalRange();if(!r)return;const center=(r.from+r.to)/2,half=Math.max(5,(r.to-r.from)*factor/2);scale.setVisibleLogicalRange({from:center-half,to:center+half});};
      const bind=(id,fn)=>{const button=document.getElementById(id);if(button)button.onclick=fn;};
      bind('chart-zoom-in',()=>zoom(.75));bind('chart-zoom-out',()=>zoom(1.35));
      bind('chart-reset',()=>{const end=(view?.model?.bars.length||1)-1;chart.timeScale().setVisibleLogicalRange({from:Math.max(0,end-85),to:end+6});});
      bind('chart-fullscreen',async()=>{
        expanded=!expanded;applyExpanded();
        if(!expanded&&document.fullscreenElement)await document.exitFullscreen?.();
        else if(expanded&&document.documentElement?.requestFullscreen){try{await document.documentElement.requestFullscreen();}catch(_){/* Expanded layout remains available. */}}
        resize();
      });
      element.onkeydown=e=>{
        if(['+','=','-','ArrowLeft','ArrowRight','Home'].includes(e.key))e.preventDefault();
        if(e.key==='+'||e.key==='=')zoom(.75);if(e.key==='-')zoom(1.35);
        if(e.key==='ArrowLeft'||e.key==='ArrowRight'){const scale=chart.timeScale(),r=scale.getVisibleLogicalRange();if(r){const shift=(r.to-r.from)*.2*(e.key==='ArrowLeft'?-1:1);scale.setVisibleLogicalRange({from:r.from+shift,to:r.to+shift});}}
        if(e.key==='Home')document.getElementById('chart-reset')?.click();
        if(e.key==='Escape'){expanded=false;applyExpanded();}
      };
    }
    const changed=view.model?.bars!==model.bars;
    const prepended=view.hadData&&view.model?.bars[0]?.time!==model.bars[0]?.time,oldRange=prepended?view.chart.timeScale().getVisibleRange?.():null;
    view.model=model;
    if(changed){
      view.candles.setData(model.bars);view.ema20.setData(model.ema20);view.ema50.setData(model.ema50);view.rsi.setData(model.rsi14||[]);view.adx.setData(model.adx14||[]);
      if(model.last&&!view.hadData){const saved=ranges.get(model.key),end=model.bars.length-1;if(saved?.time)view.chart.timeScale().setVisibleRange(saved.time);else view.chart.timeScale().setVisibleLogicalRange(saved?.logical||{from:Math.max(0,end-85),to:Math.max(0,end)+6});view.hadData=true;}
      else if(oldRange)view.chart.timeScale().setVisibleRange(oldRange);
    }
    view.ema20.applyOptions({visible:model.showEma20});view.ema50.applyOptions({visible:model.showEma50});
    const levelKey=JSON.stringify(model.levels);
    if(view.levelKey!==levelKey){for(const line of view.levels)view.candles.removePriceLine(line);view.levels=model.levels.map(level=>view.candles.createPriceLine({...level,lineWidth:1,lineStyle:L.LineStyle.Dashed,axisLabelVisible:true}));view.levelKey=levelKey;}
    view.latest=model.last?describe(model.last,model)+` · EMA20 ${value(model.ema20?.at(-1)?.value)} · EMA50 ${value(model.ema50?.at(-1)?.value)} · RSI14 ${value(model.rsi14?.at(-1)?.value)} · ADX14 ${value(model.adx14?.at(-1)?.value)}`:'No complete bars in the bounded saved-history view. Fetch source data in Dhan data; missing candles are not filled.';
    if(readout)readout.textContent=view.latest;
    live(model.live);
  }
  const value=v=>typeof v==='number'&&Number.isFinite(v)?number(v):'—';
  const indicator=(param,series)=>value(param.seriesData?.get(series)?.value);
  function describe(row,model){return `${model.source} · ${date.format(new Date(row.time*1000))} IST · O ${number(row.open)} H ${number(row.high)} L ${number(row.low)} C ${number(row.close)} · ${model.minutes}m closed candle · index points`;}
  function live(row){
    if(!view)return;
    const state=document.getElementById('research-chart-live');
    const tradeAge=Date.now()-Date.parse(row?.trade_at),receiptAge=Date.now()-Date.parse(row?.received_at);
    const valid=window.thetaLiveIsConnected?.()&&row?.trade_state==='RECENT_TRADE'&&tradeAge>=0&&tradeAge<=10000&&receiptAge>=0&&receiptAge<=10000&&typeof row.ltp==='number'&&Number.isFinite(row.ltp)&&row.ltp>0;
    if(!valid){if(view.ltp)view.candles.removePriceLine(view.ltp);view.ltp=null;view.ltpPrice=null;if(state)state.textContent='Closed provider candles · live LTP unavailable / not current';return;}
    if(!view.ltp)view.ltp=view.candles.createPriceLine({price:row.ltp,title:'Observed LTP',color:'#087f6e',lineWidth:1,lineStyle:window.LightweightCharts.LineStyle.Dashed,axisLabelVisible:true});
    else if(view.ltpPrice!==row.ltp)view.ltp.applyOptions({price:row.ltp});
    view.ltpPrice=row.ltp;if(state)state.textContent=`Observed LTP ${number(row.ltp)} · trade ${time.format(new Date(row.trade_at))} IST · not a forming candle`;
  }
  window.thetaChartRender=render;window.thetaChartDispose=dispose;window.thetaChartLive=live;
})();
