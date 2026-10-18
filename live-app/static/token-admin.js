'use strict';
(() => {
  // Admin password stays in memory for this tab. Dhan token is never persisted here.
  let credential = '';
  const field = () => document.querySelector('#dhan-token');
  const message = text => { const node = document.querySelector('#admin-login-status'); if(node) node.textContent = text; };
  const enable = value => ['dhan-token','toggle-token','save-token'].forEach(id => { const node=document.getElementById(id); if(node) node.disabled=!value; });
  document.addEventListener('click', async event => {
    const id=event.target.closest('button')?.id;
    if(id==='admin-signout'){ credential=''; if(field())field().value=''; enable(false); message('Admin signed out.'); return; }
    if(id==='toggle-token') { const input=field(); if(input){ input.type=input.type==='password'?'text':'password'; event.target.textContent=input.type==='password'?'Show token':'Hide token'; } }
    if(id!=='admin-signin') return;
    try {
      const input=document.getElementById('token-admin-password');
      credential=input.value; input.value='';
      const response=await fetch('/api/admin/check',{method:'POST',headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:'{}'});
      const result=await response.json();
      if(!response.ok){credential='';enable(false);throw Error(result.detail||'Admin password check failed.');}
      enable(true);message('Token update unlocked for this tab.');
    } catch(error){ message(error.message); }
  });
  window.thetaSaveDhanToken=async () => {
    if(!credential) throw Error('Unlock token update with the admin password first.');
    const input=field(), token=input.value; input.value=''; input.type='password'; enable(false);
    try {
      const response=await fetch('/api/dhan/token', {method:'POST',headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:JSON.stringify({token})});
      const result=await response.json();
      if(!response.ok){ if(response.status===403) credential=''; throw Error(typeof result.detail==='string'?result.detail:'Token could not be saved.'); }
      await load();
      message('Token authenticated and saved. Client ID unchanged. Market feed check is separate.');
      toast('Token checked and saved.');
    } finally { enable(Boolean(credential)); }
  };
})();
