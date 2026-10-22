const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const path=require('node:path');
function env(){
  const nodes=new Map(),charts=[],listeners=new Map();let observerStops=0;
  const root={classList:{toggle(){},remove(){}}};
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,textContent:'',setAttribute(){},closest:()=>root,click(){this.onclick?.();}});return nodes.get(id);};
  const L={ColorType:{Solid:'solid'},CrosshairMode:{Normal:0},TickMarkType:{Time:3,TimeWithSeconds:4},LineStyle:{Dashed:2},CandlestickSeries:'candles',LineSeries:'line',createChart:(element,options)=>{
    let range=null;const panes=[{},{},{}].map(p=>({setStretchFactor(n){this.weight=n;}}));const c={element,options,panes:()=>panes,series:[],removed:false,rangeCalls:0,applyOptions(){},remove(){this.removed=true;},subscribeCrosshairMove(fn){this.crosshair=fn;},timeScale:()=>({getVisibleLogicalRange:()=>range,setVisibleLogicalRange:r=>{range=r;c.rangeCalls++;}}),addSeries(type,initial,pane=0){const s={type,pane,initial,sets:[],lines:[],options:[],setData(rows){this.sets.push(rows);},applyOptions(o){this.options.push(o);},createPriceLine(o){const l={options:o,applyOptions(v){this.options={...this.options,...v};}};this.lines.push(l);return l;},removePriceLine(l){this.lines=this.lines.filter(x=>x!==l);}};this.series.push(s);return s;}};charts.push(c);return c;
  }};
  const document={getElementById:node,addEventListener:(n,fn)=>listeners.set(n,fn),removeEventListener:n=>listeners.delete(n)};
  const window={LightweightCharts:L,addEventListener(){},removeEventListener(){},thetaLiveIsConnected:()=>true};
  const context={document,window,Intl,Date,Map,Number,ResizeObserver:class{observe(){}disconnect(){observerStops++;}}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/price-chart.js'),'utf8'),context);
  const time=Date.parse('2026-10-22T03:45:00Z')/1000;
  const model={key:'nifty:5',minutes:5,source:'Historical',bars:[{time,open:100,high:102,low:99,close:101},{time:time+300}],ema20:[],ema50:[],levels:[],last:{time,open:100,high:102,low:99,close:101},showEma20:true,showEma50:true,live:null};
  return {window,node,charts,model,root,stops:()=>observerStops};
}
test('v5 canvas receives real candles and whitespace; live LTP never calls candle setData',()=>{
  const e=env();e.window.thetaChartRender(e.node('research-chart'),e.model);
  const c=e.charts[0],candles=c.series[0];assert.equal(candles.type,'candles');assert.equal(candles.sets[0],e.model.bars);
  assert.equal(c.options.handleScale.pinch,true);assert.equal(c.options.handleScroll.vertTouchDrag,false);
  assert.match(e.node('research-chart-readout').textContent,/O 100.00 H 102.00 L 99.00 C 101.00/);
  const at=new Date().toISOString();e.window.thetaChartLive({ltp:105,trade_at:at,received_at:at,trade_state:'RECENT_TRADE'});
  assert.equal(candles.sets.length,1);assert.equal(candles.lines.at(-1).options.price,105);
  e.window.thetaLiveIsConnected=()=>false;e.window.thetaChartLive({ltp:105,trade_at:at,received_at:at,trade_state:'RECENT_TRADE'});
  assert.equal(candles.lines.length,0);assert.match(e.node('research-chart-live').textContent,/not current/);
});
test('data refresh and EMA toggles preserve viewport, while dispose removes canvas and observers',()=>{
  const e=env();e.window.thetaChartRender(e.node('research-chart'),e.model);const c=e.charts[0];
  c.timeScale().setVisibleLogicalRange({from:2,to:12});const calls=c.rangeCalls;
  e.window.thetaChartRender(e.node('research-chart'),{...e.model,showEma20:false});
  assert.equal(c.rangeCalls,calls);assert.equal(c.series[0].sets.length,1);assert.equal(c.series[1].options.at(-1).visible,false);
  e.window.thetaChartRender(e.node('research-chart'),{...e.model,bars:[...e.model.bars]});assert.equal(c.series[0].sets.length,2);assert.equal(c.rangeCalls,calls);
  e.window.thetaChartDispose();assert.equal(c.removed,true);assert.equal(e.stops(),1);
  e.window.thetaChartRender(e.node('research-chart'),e.model);assert.equal(e.charts[1].timeScale().getVisibleLogicalRange().from,2);
});
test('crosshair OHLC and zoom controls act on the selected candle and chart range',()=>{
  const e=env();e.window.thetaChartRender(e.node('research-chart'),e.model);const c=e.charts[0];
  const hovered={time:e.model.last.time,open:110,high:112,low:109,close:111};c.crosshair({seriesData:new Map([[c.series[0],hovered]])});
  assert.match(e.node('research-chart-readout').textContent,/O 110.00 H 112.00 L 109.00 C 111.00/);
  c.timeScale().setVisibleLogicalRange({from:0,to:100});const before=c.timeScale().getVisibleLogicalRange();e.node('chart-zoom-in').click();const after=c.timeScale().getVisibleLogicalRange();assert.ok(after.to-after.from<before.to-before.from);
});
test('unsupported native fullscreen has an expanded layout and does not break the chart',async()=>{
  const e=env();e.window.thetaChartRender(e.node('research-chart'),e.model);
  await e.node('chart-fullscreen').onclick();assert.equal(e.node('chart-fullscreen').textContent,'Exit fullscreen');
  await e.node('chart-fullscreen').onclick();assert.equal(e.node('chart-fullscreen').textContent,'Fullscreen');
});
test('an initial empty chart does not pin subsequently loaded history to its oldest bars',()=>{
  const e=env();e.window.thetaChartRender(e.node('research-chart'),{...e.model,bars:[],last:undefined});
  assert.equal(e.charts[0].rangeCalls,0);e.window.thetaChartDispose();
  const bars=Array.from({length:200},(_,i)=>({...e.model.last,time:e.model.last.time+i*300}));
  e.window.thetaChartRender(e.node('research-chart'),{...e.model,bars,last:bars.at(-1)});
  assert.equal(e.charts[1].timeScale().getVisibleLogicalRange().from,114);
});

 test('RSI and ADX have independent panes fed only by the authoritative backend arrays',()=>{
  const e=env(),time=e.model.last.time;
  const rsi14=[{time,value:60}],adx14=[{time,value:28}];
  e.window.thetaChartRender(e.node('research-chart'),{...e.model,rsi14,adx14});
  const c=e.charts[0];assert.deepEqual(c.panes().map(p=>p.weight),[3,1,1]);assert.equal(c.series[3].pane,1);assert.equal(c.series[4].pane,2);
  assert.equal(c.series[3].sets[0],rsi14);assert.equal(c.series[4].sets[0],adx14);
  c.crosshair({seriesData:new Map([[c.series[0],e.model.last],[c.series[3],{value:60}],[c.series[4],{value:28}]])});
  assert.match(e.node('research-chart-readout').textContent,/RSI14 60.00 · ADX14 28.00/);
 });
