'use strict';
(() => {
  const day=t=>new Date((t+19800)*1000).toISOString().slice(0,10);
  const startOfDay=t=>Math.floor((t+19800)/86400)*86400-19800+555*60;
  function build(input,minutes,clock=Date.now()/1000){
    if(![1,5,15,60].includes(minutes))throw Error('Unsupported chart timeframe');
    const rows=new Map(),conflicts=new Set();let excluded=0;
    for(const r of input||[]){
      const t=r.timestamp,offset=(t-startOfDay(t))/60;
      if(!Number.isInteger(t)||t%60||offset<0||offset>=375||t+60>clock||
        !['open','high','low','close'].every(k=>typeof r[k]==='number'&&Number.isFinite(r[k])&&r[k]>0)||
        r.low>Math.min(r.open,r.close)||r.high<Math.max(r.open,r.close)||r.high<r.low){excluded++;continue;}
      const previous=rows.get(t);
      if(previous&&['open','high','low','close'].some(k=>previous[k]!==r[k]))conflicts.add(t);
      else rows.set(t,r);
    }
    for(const t of conflicts)rows.delete(t);
    const days=[...new Set([...rows.keys()].map(startOfDay))].sort((a,b)=>a-b),bars=[];
    for(const start of days){
      const count=Math.min(375,Math.max(0,Math.floor((clock-start)/60)));
      for(let offset=0;offset+minutes<=count;offset+=minutes){
        const time=start+offset*60,group=Array.from({length:minutes},(_,i)=>rows.get(time+i*60));
        if(group.some(r=>!r)){bars.push({time});continue;}
        bars.push({time,open:group[0].open,high:Math.max(...group.map(r=>r.high)),low:Math.min(...group.map(r=>r.low)),close:group.at(-1).close});
      }
    }
    const latestStart=days.at(-1),latest=latestStart===undefined?[]:[...rows.values()].filter(r=>startOfDay(r.timestamp)===latestStart);
    const levels=[];
    if(latest.length){
      const label=day(latestStart)===day(clock)?'Today':'Loaded session';
      levels.push({title:label+' high',price:Math.max(...latest.map(r=>r.high)),color:'#8b6b37'},
        {title:label+' low',price:Math.min(...latest.map(r=>r.low)),color:'#8b6b37'});
      const opening=Array.from({length:15},(_,i)=>rows.get(latestStart+i*60));
      if(opening.every(Boolean)&&latestStart+900<=clock)levels.push(
        {title:'ORB 15m high',price:Math.max(...opening.map(r=>r.high)),color:'#7755b5'},
        {title:'ORB 15m low',price:Math.min(...opening.map(r=>r.low)),color:'#7755b5'});
    }
    return {bars,ema20:[],ema50:[],levels,excluded,conflicts:conflicts.size,
      last:bars.findLast(r=>r.close!==undefined),loadedDay:latestStart===undefined?null:day(latestStart),
      loadedHigh:latest.length?Math.max(...latest.map(r=>r.high)):null,loadedLow:latest.length?Math.min(...latest.map(r=>r.low)):null};
  }
  const api={build,day};
  if(typeof window!=='undefined')window.thetaChartData=api;
  if(typeof module!=='undefined')module.exports=api;
})();
