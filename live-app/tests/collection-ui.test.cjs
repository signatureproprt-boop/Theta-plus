const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function environment(runState='completed',options={}) {
  const nodes=new Map(), events={}, windowEvents={}, calls=[];
  function element(id) {
    if (!nodes.has(id)) {
      let html='';
      const n={id,value:'',hidden:false,disabled:false,dataset:{},textContent:'',
        classList:{add(){},remove(){},toggle(){}},
        querySelector(){return element('submit');},click(){},
        insertAdjacentHTML(_where,s){this.innerHTML+=s;}};
      Object.defineProperty(n,'innerHTML',{get(){return html;},set(s){
        html=s;
        for(const m of s.matchAll(/id="([^"]+)"[^>]*?(?:value="([^"]*)")?[^>]*>/g)) {
          if(m[1]!==id)element(m[1]);
        }
      }});
      nodes.set(id,n);
    }
    return nodes.get(id);
  }
  const job={id:'a'.repeat(32),state:'paused',counts:{completed:0,empty:0,unsupported:0,failed:0,permission_blocked:0},
    created_at:'2026-10-19T00:00:00Z',updated_at:'2026-10-19T00:00:00Z',items_assessed:0,rows_saved:0,message:'Ready'};
  const context={document:{
    querySelector:s=>element(s.slice(1)),getElementById:element,
    querySelectorAll:s=>s==='[name="collect-family"]:checked'?[{value:'intraday'}]:
      s==='[name="collect-interval"]:checked'?[{value:'1'}]:[],
    addEventListener:(name,fn)=>(events[name]??=[]).push(fn),createElement:()=>({click(){}})
  },window:{addEventListener:(name,fn)=>(windowEvents[name]??=[]).push(fn)},
    location:{hash:'#collect'},AbortSignal:{timeout:()=>undefined},Intl,Date,console,
    Blob,URL, setInterval:()=>0,setTimeout, clearTimeout,fetch:async(url,opts={})=>{
      calls.push([url,opts]);
      if(options.responses?.[url])return options.responses[url];
      let data={};
      if(url==='/api/status' && options.statusPromise)await options.statusPromise;
      if(url==='/api/dhan/collected?kind=dhan_chain' && options.chainPromise)await options.chainPromise;
      if(url==='/api/dhan/collected?kind=dhan_history')data={dhan_history:options.history||[]};
      if(url==='/api/dhan/collected?kind=dhan_chain')data={dhan_chain:[]};
      if(url==='/api/status')data={dhan_configured:options.savedToken!==false,token_screen_enabled:true};
      if(url==='/api/dhan/collected')data={};
      if(url==='/api/admin/check')data={authorised:true};
      if(url==='/api/admin/dhan/save')data={saved:true,status:'AUTHENTICATED',token_validity_reported:'20/10/2026 00:49',data_plan:'Active'};
      if(url==='/api/admin/dhan/test')data={saved:false,status:'AUTHENTICATED'};
      if(url==='/api/admin/dhan/settings')data={client_id:'fixture',editable:true,token_saved:options.savedToken!==false};
      if(url==='/api/collection/engine')data={sdk:{version:'2.3.0'},worker:{configured:!!options.background}};
      if(url==='/api/collection/catalogue')data={rows:2,fetched_at:'now',review_reasons:{}};
      if(url==='/api/collection/jobs' && !opts.method)data={jobs:[],next_cursor:null};
      if(url==='/api/collection/jobs' && opts.method)data={...job};
      if(url.endsWith('/resume'))data={...job,state:'running',...(options.background?{runner:{mode:'cloud_run_job',state:'starting'}}:{})};
      if(url.endsWith('/continue-rejected'))data={...job,config:{continue_rejected_requests:true},message:'Rejected item recorded as failed / NOT_COLLECTED'};
      if(url.endsWith('/run'))data={...job,state:runState,items_assessed:1,rows_saved:1};
      if(url==='/api/collection/jobs/'+job.id)data={...job};
      return {ok:true,status:200,text:async()=>JSON.stringify(data),json:async()=>data};
    }};
  vm.createContext(context);
  for(const file of ['api-response.js','app.js','token-admin.js','collection.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../static',file),'utf8'),context,{filename:file});
  }
  async function flush(){for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));}
  async function click(id){const b=element(id);await Promise.all(events.click.map(fn=>fn({target:{closest:()=>b}})));await flush();}
  return {context,nodes,element,events,calls,flush,click,job};
}

test('Admin and collection forms render before network status resolves',()=>{
  const e=environment();
  assert.match(e.element('page-admin').innerHTML,/Token admin password/);
  assert.match(e.element('page-collect').innerHTML,/Create collection plan/);
  assert.match(e.element('page-collect').innerHTML,/continuous all-market recording/);
});

test('Admin unlock, create plan and bounded run use protected collection API',async()=>{
  const e=environment();await e.flush();
  e.element('token-admin-password').value='fixture-admin';
  await e.click('admin-signin');
  e.element('collect-start').value='2026-10-16';
  e.element('collect-end').value='2026-10-17';
  const target={id:'collection-plan',querySelector:()=>e.element('submit')};
  await Promise.all(e.events.submit.map(fn=>fn({target,preventDefault(){}})));
  await e.flush();
  const create=e.calls.find(([url,o])=>url==='/api/collection/jobs' && o.method==='POST');
  assert.ok(create);
  assert.equal(create[1].headers['X-Theta-Admin-Password'],'fixture-admin');
  assert.equal(JSON.parse(create[1].body).intervals[0],1);
  await e.click('collection-resume');
  assert.equal(e.calls.filter(([url])=>url.endsWith('/run')).length,1);
  assert.match(e.element('collection-current').innerHTML,/completed/);
  assert.ok(!JSON.stringify(create[1].body).includes('fixture-admin'));
});

test('collection writes without Admin unlock show actionable feedback',async()=>{
  const e=environment();await e.flush();
  await e.click('catalogue-refresh');
  assert.match(e.element('collection-feedback').textContent,/Admin and unlock/);
  assert.equal(e.calls.filter(([url])=>url.endsWith('/catalogue/refresh')).length,0);
});


test('rejected request stops browser pump and retains review status',async()=>{
  const e=environment('request_review');await e.flush();
  e.element('token-admin-password').value='fixture-admin';
  await e.click('admin-signin');
  // Select an existing job before resuming.
  const b=e.element('job-button');b.dataset.collectionJob=e.job.id;
  await e.click('job-button');
  await e.click('collection-resume');
  assert.equal(e.calls.filter(([url])=>url.endsWith('/run')).length,1);
  assert.match(e.element('collection-current').innerHTML,/request review/);
});


test('saved history renders while status and chains are still pending; export keeps storage kind',async()=>{
  const pending=new Promise(()=>{});
  const e=environment('completed',{statusPromise:pending,chainPromise:pending,history:[{id:123,kind:'expired_option',index:'nifty',side:'PUT',expiry_flag:'MONTH',expiry_code:1,rows:385,from:'2026-10-12',to_exclusive:'2026-10-13'}]});
  await e.flush();
  const html=e.element('page-saved').innerHTML;
  assert.match(html,/nifty/);assert.match(html,/PUT/);assert.match(html,/MONTH/);
  assert.match(html,/data-export="dhan_history\/123"/);
  assert.doesNotMatch(html,/No saved Dhan records found/);
});

test('new saved page shows loading rather than claiming no records',()=>{
  const e=environment('completed',{statusPromise:new Promise(()=>{}),chainPromise:new Promise(()=>{})});
  assert.match(e.element('page-saved').innerHTML,/Loading saved records/);
  assert.doesNotMatch(e.element('page-saved').innerHTML,/No saved Dhan records found/);
});


test('server worker Resume does not start a browser pump',async()=>{
  const e=environment('running',{background:true});await e.flush();
  e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
  const b=e.element('job-button');b.dataset.collectionJob=e.job.id;await e.click('job-button');
  await e.click('collection-resume');
  assert.equal(e.calls.filter(([url])=>url.endsWith('/run')).length,0);
  assert.match(e.element('collection-feedback').textContent,/may close this page/);
  assert.match(e.element('collection-engine').textContent,/DhanHQ-py 2.3.0/);
});


test('saved token stays closed after unlock and saved connection test sends no candidate',async()=>{
  const e=environment();await e.flush();
  e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
  assert.equal(e.element('dhan-token-form').hidden,true);
  assert.equal(e.element('edit-dhan-token').disabled,false);
  assert.equal(e.element('dhan-token').disabled,true);
  await e.click('test-dhan-connection');
  const call=e.calls.find(([url])=>url==='/api/admin/dhan/test');
  assert.equal(call[1].body,'{}');
  assert.equal(e.element('dhan-token-form').hidden,true);
});

test('Edit token is explicit; cancel clears candidate and preserves the saved connection',async()=>{
  const e=environment();await e.flush();
  e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
  await e.click('edit-dhan-token');
  assert.equal(e.element('dhan-token-form').hidden,false);
  assert.equal(e.element('dhan-client-id').disabled,true);
  assert.equal(e.element('dhan-client-id').value,'fixture');
  e.element('dhan-token').value='candidate-not-saved';await e.click('cancel-token-edit');
  assert.equal(e.element('dhan-token-form').hidden,true);
  assert.equal(e.element('dhan-token').value,'');
  assert.equal(e.calls.some(([url])=>url==='/api/admin/dhan/save'),false);
});

test('save clears token entry, closes editor and keeps Admin unlocked after refresh',async()=>{
  const e=environment();await e.flush();
  e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
  await e.click('edit-dhan-token');
  e.element('dhan-token').value='candidate-token-at-least-twenty-chars';
  await e.context.window.thetaSaveDhanToken();await e.flush();
  const call=e.calls.find(([url])=>url==='/api/admin/dhan/save');
  assert.equal(JSON.parse(call[1].body).client_id,'fixture');
  assert.equal(e.element('dhan-token').value,'');
  assert.equal(e.element('dhan-token-form').hidden,true);
  assert.equal(e.element('edit-dhan-token').disabled,false);
  await e.click('refresh');
  assert.equal(e.element('dhan-token-form').hidden,true);
  assert.equal(e.element('edit-dhan-token').disabled,false);
});

test('plain-text rate limit does not claim token setup is missing or repeat requests',async()=>{
  const e=environment('completed',{responses:{'/api/status':{ok:false,status:429,text:async()=> 'Rate exceeded.',headers:{get:()=> '60'}}}});await e.flush();
  assert.match(e.element('error').textContent,/Wait 60 seconds/);
  assert.match(e.element('page-admin').innerHTML,/Connection status could not load/);
  assert.doesNotMatch(e.element('page-admin').innerHTML,/need one-time setup|No token check recorded/);
  const before=e.calls.length;await e.click('refresh');assert.equal(e.calls.length,before);
});
test('non-JSON admin rejection cannot leave Admin authenticated',async()=>{
  const e=environment('completed',{responses:{'/api/admin/check':{ok:false,status:502,text:async()=>'<html>proxy error</html>'}}});await e.flush();
  e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
  assert.match(e.element('admin-login-status').textContent,/HTTP 502/);
  await assert.rejects(e.context.window.thetaAdminRequest('research/paper/list'),/unlock/);
  assert.equal(e.element('token-admin-password').value,'');
});

test('unknown authentication payload fails closed',async()=>{
  const e=environment('completed',{responses:{'/api/admin/check':{ok:true,status:200,text:async()=> '{}'}}});await e.flush();
  e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
  await assert.rejects(e.context.window.thetaAdminRequest('research/paper/list'),/unlock/);
  assert.match(e.element('admin-login-status').textContent,/verification unavailable/);
});

test('request-review continuation uses explicit protected action and does not claim coverage',async()=>{
 const e=environment('request_review');await e.flush();
 e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
 const b=e.element('job-button');b.dataset.collectionJob=e.job.id;await e.click('job-button');await e.click('collection-resume');
 assert.equal(e.element('collection-skip').dataset.action,'continue-rejected');
 assert.match(e.element('collection-skip').textContent,/failed/);
 await e.click('collection-skip');
 const call=e.calls.find(([url])=>url.endsWith('/continue-rejected'));
 assert.equal(call[1].headers['X-Theta-Admin-Password'],'fixture-admin');
 assert.match(e.element('collection-feedback').textContent,/NOT_COLLECTED/);
 assert.equal(e.element('collection-skip').disabled,true);
});

test('partial results expose missing identities and protected diagnosis without resuming collection',async()=>{
 const jid='a'.repeat(32),key='results/'+jid+'/fixture';
 const response=data=>({ok:true,status:200,text:async()=>JSON.stringify(data)});
 const e=environment('completed',{responses:{
  ['/api/collection/jobs/'+jid+'/results']:response({items:[{key,task:{family:'quote',item:{symbol:'NIFTY',segment:'IDX_I'}},result:{status:'partial',rows:1,missing_security_ids:['25','51'],coverage:'PARTIAL',isolated_probes:3}}],next_cursor:null}),
  ['/api/collection/jobs/'+jid+'/diagnose']:response({conclusion:'EMPTY_UNRESOLVED',probes:[{endpoint:'marketfeed/quote',state:'empty',rows:0}]})
 }});
 await e.flush();e.element('token-admin-password').value='fixture-admin';await e.click('admin-signin');
 const b=e.element('job-button');b.dataset.collectionJob=jid;await e.click('job-button');await e.click('collection-results');
 const html=e.element('collection-result-list').innerHTML;
 assert.match(html,/2 missing instruments/);assert.match(html,/3 isolated probes/);assert.match(html,/data-collection-diagnose/);
 const d=e.element('diagnose-fixture');d.dataset.collectionDiagnose=key;await e.click('diagnose-fixture');
 const call=e.calls.find(([url])=>url.endsWith('/diagnose'));
 assert.equal(call[1].headers['X-Theta-Admin-Password'],'fixture-admin');
 assert.deepEqual(JSON.parse(call[1].body),{result_key:key});
 assert.match(e.element('collection-feedback').textContent,/EMPTY_UNRESOLVED/);
 assert.equal(e.calls.filter(([url])=>url.endsWith('/run')||url.endsWith('/resume')).length,0);
});
