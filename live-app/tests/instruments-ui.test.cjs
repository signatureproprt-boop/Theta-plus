const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function environment({fail=false,initialHash}={}){
  const nodes=new Map(),calls=[];
  function element(id=''){
    if(id&&nodes.has(id))return nodes.get(id);
    const node={id,value:'',checked:true,disabled:false,hidden:false,textContent:'',dataset:{},children:[],events:{},
      addEventListener(name,fn){this.events[name]=fn;},
      append(child){this.children.push(child);},replaceChildren(...children){this.children=children;},
      querySelector(){return element('submit');},querySelectorAll(){return[];},click(){},
      closest(){return this.dataset.instrumentSelect!==undefined?this:null;}};
    if(id)nodes.set(id,node);return node;
  }
  const context={document:{getElementById:element,createElement:()=>element()},window:{},URL,URLSearchParams,Blob,Intl,Date,AbortSignal,
    setTimeout:fn=>setImmediate(fn),clearTimeout:clearImmediate,
    Option:function(text,value){return {textContent:text,value};}};
  const windowEvents={};context.window.addEventListener=(name,fn)=>{windowEvents[name]=fn;};if(initialHash)context.window.location={hash:initialHash};
  context.window.thetaApiFetch=async(url)=>{calls.push(['get',url]);if(fail)throw Error('Catalogue unavailable');return {url}};
  context.window.thetaReadResponse=async(response)=>response.url?.includes('/saved-history?')?{rows:0,candles:[],state:'NO_SAVED_ROWS',from:'2026-10-01',to_exclusive:'2026-10-23'}:response.url?.includes('/recorded?')?{state:'NOT_SUBSCRIBED'}:({page:0,next_page:1,catalogue_sha256:'a'.repeat(64),catalogue_rows:200,matches:55,
    segments:['BSE_EQ','NSE_EQ'],catalogue_at:'2026-10-23',coverage_note:'Not proof of coverage',
    items:[{key:'exact',symbol:'<img src=x onerror=alert(1)>',segment:'BSE_EQ',security_id:'13',instrument:'EQUITY'}]});
  context.window.thetaAdminRequest=async(url,body)=>{calls.push(['post',url,body]);return {status:'TIME_UNAVAILABLE',fetched_at:'2026-10-23'}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/instruments.js'),'utf8'),context);
  const get=id=>element('instrument-'+id);
  return {get,calls,context,windowEvents,async flush(){for(let i=0;i<8;i++)await new Promise(setImmediate);},async search(){await get('search').events.submit({preventDefault(){}});await new Promise(setImmediate);},
    async select(){get('rows').events.click({target:{closest:()=>({dataset:{instrumentSelect:'0'}})}});}};
}
test('whole-master search renders untrusted names as text and uses exact selected identity',async()=>{
  const e=environment();e.get('query').value='RELIANCE';e.get('segment').value='BSE_EQ';
  await e.search();assert.match(e.calls[0][1],/segment=BSE_EQ/);
  assert.equal(e.get('rows').children[0].children[0].textContent,'<img src=x onerror=alert(1)>');
  await e.select();e.get('quote').events.click();await new Promise(setImmediate);
  const posted=e.calls.find(c=>c[0]==='post');assert.equal(posted[1],'instruments/quote');
  assert.deepEqual(JSON.parse(JSON.stringify(posted[2])),{key:'exact',catalogue_sha256:'a'.repeat(64)});
  assert.match(e.get('result-status').textContent,/TIME_UNAVAILABLE/);
});
test('a failed catalogue search disables fetches and displays the error',async()=>{
  const e=environment({fail:true});await e.search();
  assert.equal(e.get('quote').disabled,true);assert.equal(e.get('next').disabled,true);
  assert.match(e.get('status').textContent,/Catalogue unavailable/);
});
test('paging retains the submitted filter when unsent inputs are edited',async()=>{
  const e=environment();e.get('query').value='RELIANCE';await e.search();
  e.get('query').value='unsent';e.get('next').events.click();await new Promise(setImmediate);
  assert.match(e.calls[1][1],/q=RELIANCE/);assert.match(e.calls[1][1],/page=1/);
});


test('opening All instruments loads the first catalogue page without a submit or provider call',async()=>{
 const e=environment({initialHash:'#instruments'});await e.flush();
 assert.equal(e.calls.length,1);assert.equal(e.calls[0][0],'get');
 assert.match(e.calls[0][1],/category=all/);assert.match(e.calls[0][1],/include_quotes=true/);
 assert.equal(e.get('rows').children.length,1);
});

test('opening an exact instrument reads stored candles and quote without Admin or provider POST',async()=>{
 const e=environment();await e.search();await e.select();await e.flush();
 assert.ok(e.calls.every(c=>c[0]==='get'));
 const history=e.calls.find(c=>c[1].includes('/saved-history?'));
 assert.match(history[1],/key=exact&catalogue_sha256=aaaaaaaa/);
 assert.match(e.get('chart-status').textContent,/NO_SAVED_ROWS/);
 assert.match(e.get('price').textContent,/NOT_SUBSCRIBED/);
});

test('Home category and exchange shortcuts reach the matching full-catalogue query',async()=>{
 const e=environment();e.context.window.thetaInstrumentBrowse('options','NIFTY','NSE_FNO');await e.flush();
 assert.match(e.calls[0][1],/q=NIFTY/);assert.match(e.calls[0][1],/segment=NSE_FNO/);assert.match(e.calls[0][1],/category=options/);
});
