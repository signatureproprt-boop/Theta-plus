'use strict';
(() => {
  // Google ID token stays in memory for this tab. Dhan token is never persisted here.
  let credential = '', loading;
  const field = () => document.querySelector('#dhan-token');
  const message = text => { const node = document.querySelector('#admin-login-status'); if(node) node.textContent = text; };
  const enable = value => ['dhan-token','toggle-token','save-token'].forEach(id => { const node=document.getElementById(id); if(node) node.disabled=!value; });
  function library() {
    if(window.google?.accounts?.id) return Promise.resolve();
    if(!loading) loading=new Promise((resolve,reject) => {
      const script=document.createElement('script'); script.src='https://accounts.google.com/gsi/client'; script.async=true;
      script.onload=resolve; script.onerror=() => { loading=null; reject(Error('Google sign-in could not load. Try again.')); }; document.head.append(script);
    });
    return loading;
  }
  document.addEventListener('click', async event => {
    const id=event.target.closest('button')?.id;
    if(id==='admin-signout'){ credential=''; if(field())field().value=''; enable(false); message('Admin signed out.'); return; }
    if(id==='toggle-token') { const input=field(); if(input){ input.type=input.type==='password'?'text':'password'; event.target.textContent=input.type==='password'?'Show token':'Hide token'; } }
    if(id!=='admin-signin') return;
    try {
      const response=await fetch('/api/admin/config'); const config=await response.json();
      if(!response.ok||!config.enabled) throw Error('One-time Google admin setup is pending.');
      await library();
      window.google.accounts.id.initialize({client_id:config.client_id, callback: result => {
        credential=result.credential||''; enable(Boolean(credential)); message('Google sign-in received. Admin permission is verified when saving.');
      }});
      window.google.accounts.id.renderButton(document.getElementById('google-admin-button'), {theme:'outline',size:'large'});
    } catch(error){ message(error.message); }
  });
  window.thetaSaveDhanToken=async () => {
    if(!credential) throw Error('Sign in with your Google admin account first.');
    const input=field(), token=input.value; input.value=''; input.type='password'; enable(false);
    try {
      const response=await fetch('/api/dhan/token', {method:'POST',headers:{'Content-Type':'application/json','X-Theta-Admin-ID-Token':credential},body:JSON.stringify({token})});
      const result=await response.json();
      if(!response.ok){ if(response.status===403) credential=''; throw Error(typeof result.detail==='string'?result.detail:'Token could not be saved.'); }
      await load();
      message('Token authenticated and saved. Client ID unchanged. Market feed check is separate.');
      toast('Token checked and saved.');
    } finally { enable(Boolean(credential)); }
  };
})();
