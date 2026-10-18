'use strict';
(() => {
  // Admin password stays in memory for this tab. Dhan token is never persisted here.
  let credential = '';
  const field = () => document.querySelector('#dhan-token');
  const message = text => { const node = document.querySelector('#admin-login-status'); if(node) node.textContent = text; };
  const enable = value => ['dhan-token','dhan-client-id','toggle-token','save-token','test-dhan-connection'].forEach(id => { const node=document.getElementById(id); if(node) node.disabled=!value; });
  document.addEventListener('click', async event => {
    const id=event.target.closest('button')?.id;
    if(id==='admin-signout'){ credential=''; if(field())field().value=''; enable(false); message('Admin signed out.'); return; }
    if(id==='toggle-token') { const input=field(); if(input){ input.type=input.type==='password'?'text':'password'; event.target.textContent=input.type==='password'?'Show token':'Hide token'; } }
    if(id==='test-dhan-connection'){try{const result=await adminCall('test',candidate());showResult(result);}catch(error){message(error.message);}return;}
    if(id!=='admin-signin') return;
    try {
      const input=document.getElementById('token-admin-password');
      credential=input.value; input.value='';
      const response=await fetch('/api/admin/check',{method:'POST',headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:'{}'});
      const result=await response.json();
      if(!response.ok){credential='';enable(false);throw Error(result.detail||'Admin password check failed.');}
      const settings=await adminCall('settings',{});
      document.getElementById('dhan-client-id').value=settings.client_id||'';
      enable(true);message(settings.editable?'Admin unlocked. Client ID stays saved.':'Connection storage setup is pending.');
    } catch(error){ message(error.message); }
  });
  function candidate(){return {client_id:document.getElementById('dhan-client-id').value.trim()||null,token:field().value||null};}
  function showResult(result){
    const node=document.getElementById('admin-dhan-result');
    if(node)node.textContent=(result.saved?'Saved. ':'Test passed. ')+result.status+' · Token validity (Dhan): '+(result.token_validity_reported||'Not reported')+' · Data plan: '+(result.data_plan||'Not reported')+' · Live prices not verified by this test.';
  }
  async function adminCall(action,body){
    if(!credential)throw Error('Unlock Admin first.');
    const response=await fetch('/api/admin/dhan/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok){if(response.status===403){credential='';enable(false);}throw Error(typeof result.detail==='string'?result.detail:'Connection check failed.');}
    return result;
  }
  window.thetaSaveDhanToken=async () => {
    const body=candidate();field().value='';field().type='password';enable(false);
    try{const result=await adminCall('save',body);await load();document.getElementById('dhan-client-id').value=body.client_id;showResult(result);message('Connection saved. Client ID will remain here for future updates.');toast('Dhan connection checked and saved.');}
    finally{enable(Boolean(credential));}
  };
})();
