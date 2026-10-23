const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function environment({fail=false}={}){
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
    Option:function(text,value){return {textContent:text,value};}};
  context.window.thetaApiFetch=async(url)=>{calls.push(['get',url]);if(fail)throw Error('Catalogue unavailable');return {}};
  context.window.thetaReadResponse=async()=>({page:0,next_page:1,catalogue_sha256:'a'.repeat(64),catalogue_rows:200,matches:55,
    segments:['BSE_EQ','NSE_EQ'],catalogue_at:'2026-10-23',coverage_note:'Not proof of coverage',
    items:[{key:'exact',symbol:'<img src=x onerror=alert(1)>',segment:'BSE_EQ',security_id:'13',instrument:'EQUITY'}]});
  context.window.thetaAdminRequest=async(url,body)=>{calls.push(['post',url,body]);return {status:'TIME_UNAVAILABLE',fetched_at:'2026-10-23'}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/instruments.js'),'utf8'),context);
  const get=id=>element('instrument-'+id);
  return {get,calls,context,async search(){await get('search').events.submit({preventDefault(){}});await new Promise(setImmediate);},
    async select(){get('rows').events.click({target:{closest:()=>({dataset:{instrumentSelect:'0'}})}});}};
}
test('whole-master search renders untrusted names as text and uses exact selected identity',async()=>{
  const e=environment();e.get('query').value='RELIANCE';e.get('segment').value='BSE_EQ';
  await e.search();assert.match(e.calls[0][1],/segment=BSE_EQ/);
  assert.equal(e.get('rows').children[0].children[0].textContent,'<img src=x onerror=alert(1)>');
  await e.select();e.get('quote').events.click();await new Promise(setImmediate);
  assert.equal(e.calls[1][1],'instruments/quote');
  assert.deepEqual(JSON.parse(JSON.stringify(e.calls[1][2])),{key:'exact',catalogue_sha256:'a'.repeat(64)});
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
