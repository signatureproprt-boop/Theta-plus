const {test}=require('node:test');
const assert=require('node:assert/strict');
const {build}=require('../static/chart-data.js');
const start=Date.parse('2026-10-22T03:45:00Z')/1000;
const row=(i,p=100)=>({timestamp:start+i*60,open:p,high:p+2,low:p-1,close:p+1});
test('session anchored aggregation preserves holes and excludes forming, invalid and conflicting minutes',()=>{
  const input=Array.from({length:15},(_,i)=>row(i));input.splice(7,1);
  input.push(row(2),row(3,200),{...row(9),timestamp:start+9*60+1},row(15));
  const r=build(input,5,start+15*60);
  assert.deepEqual(r.bars,[{time:start},{time:start+300},{time:start+600,open:100,high:102,low:99,close:101}]);
  assert.equal(r.conflicts,1);assert.equal(r.excluded,2);
  assert.equal(r.levels.some(r=>r.title.startsWith('ORB')),false);
});
test('regular session has 375 one-minute, 75 five-minute, 25 fifteen-minute and six full hourly bars',()=>{
  const rows=Array.from({length:375},(_,i)=>row(i));
  for(const [n,count] of [[1,375],[5,75],[15,25],[60,6]]){
    const r=build(rows,n,start+375*60);assert.equal(r.bars.length,count);assert.ok(r.bars.every(r=>r.close===101));assert.equal(r.bars[0].time,start);
  }
  assert.equal(build(rows,60,start+375*60).last.time,start+300*60);
});
test('opening range requires all 15 source minutes and confirmed close; extremes come from loaded candles',()=>{
  const rows=Array.from({length:20},(_,i)=>row(i,100+i));
  assert.equal(build(rows,1,start+14*60+59).levels.some(r=>r.title==='ORB 15m high'),false);
  const r=build(rows,5,start+20*60);
  assert.equal(r.levels.find(r=>r.title==='ORB 15m high').price,116);assert.equal(r.levels.find(r=>r.title==='Today high').price,121);
});
test('EMA uses full warm-up, resets after a missing minute and never mutates source candles',()=>{
  const rows=Array.from({length:80},(_,i)=>row(i));rows.splice(50,1);
  const before=JSON.stringify(rows),r=build(rows,1,start+80*60);
  assert.equal(r.ema20[18].value,undefined);assert.equal(r.ema20[19].value,101);assert.equal(r.ema50[49].value,101);assert.equal(r.ema20[50].value,undefined);
  assert.equal(r.ema20[69].value,undefined);assert.equal(r.ema20[70].value,101);assert.equal(r.ema50[79].value,undefined);assert.equal(JSON.stringify(rows),before);
});
test('no synthetic candles are created from empty or off-session source data',()=>{
  assert.deepEqual(build([],5,start+900).bars,[]);
  const r=build([row(-1),row(375),{...row(0),close:NaN}],1,start+400*60);
  assert.equal(r.bars.length,0);assert.equal(r.excluded,3);assert.equal(r.last,undefined);
});
