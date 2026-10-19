'use strict';
(() => {
  // Admin password is tab memory only. Saved Dhan tokens stay on the server.
  let credential='', settings=null, editing=false, saving=false;
  const node=id=>document.getElementById(id);
  const field=()=>node('dhan-token');
  const message=text=>{if(node('admin-login-status'))node('admin-login-status').textContent=text;};
  function clearCandidate(){if(field()){field().value='';field().type='password';}if(node('toggle-token'))node('toggle-token').textContent='Show token';}
  function sync(){
    const unlocked=!!credential;
    if(!unlocked){editing=false;clearCandidate();}
    const form=node('dhan-token-form');if(form)form.hidden=!editing;
    for(const id of ['dhan-token','toggle-token','save-token','cancel-token-edit'])if(node(id))node(id).disabled=!(unlocked && editing && !saving);
    if(node('edit-dhan-token'))node('edit-dhan-token').disabled=!(unlocked && settings?.editable && !saving);
    if(node('test-dhan-connection'))node('test-dhan-connection').disabled=!(unlocked && !saving);
    const client=node('dhan-client-id');
    if(client){if(settings?.client_id)client.value=settings.client_id;client.readOnly=!!settings?.client_id;client.disabled=!(unlocked && editing && !saving) || !!settings?.client_id;}
  }
  window.thetaRestoreDhanAdmin=()=>{sync();if(credential)message('Admin unlocked. Collection can use the saved token; edit only when needed.');};
  window.thetaLiveStreamRequest=async(symbol,signal)=>{
    if(!credential)throw Error('Open Admin and Unlock Admin first, then return and Start live.');
    return fetch('/api/research/live',{method:'POST',signal,headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:JSON.stringify({symbol})});
  };
  document.addEventListener('click',async event=>{
    const id=event.target.closest('button')?.id;
    if(id==='admin-signout'){window.thetaLiveStop?.();credential='';clearCandidate();sync();message('Admin locked. Saved Dhan connection is retained.');return;}
    if(id==='edit-dhan-token'){
      if(!credential){message('Unlock Admin first.');return;}
      editing=true;clearCandidate();sync();field()?.focus?.();return;
    }
    if(id==='cancel-token-edit'){editing=false;clearCandidate();sync();message('Edit cancelled. Existing saved token is unchanged.');return;}
    if(id==='toggle-token'){const input=field();if(input){input.type=input.type==='password'?'text':'password';event.target.textContent=input.type==='password'?'Show token':'Hide token';}return;}
    if(id==='test-dhan-connection'){try{showResult(await adminCall('test',{}));}catch(error){message(error.message);}return;}
    if(id!=='admin-signin')return;
    try{
      const input=node('token-admin-password');credential=input.value;input.value='';
      const response=await fetch('/api/admin/check',{method:'POST',headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:'{}'});
      const result=await response.json();
      if(!response.ok){credential='';sync();throw Error(result.detail||'Admin password check failed.');}
      settings=await adminCall('settings',{});
      editing=!settings.token_saved;sync();
      message(settings.editable?'Admin unlocked. Saved token is used automatically. Choose Edit token only to replace it.':'Connection storage setup is pending.');
    }catch(error){message(error.message);}
  });
  function candidate(){return {client_id:settings?.client_id||node('dhan-client-id').value.trim()||null,token:field().value||null};}
  function showResult(result){
    if(node('admin-dhan-result'))node('admin-dhan-result').textContent=(result.saved?'Saved. ':'Test passed. ')+result.status+' · Token validity (Dhan): '+(result.token_validity_reported||'Not reported')+' · Data plan: '+(result.data_plan||'Not reported')+' · Live prices not verified by this test.';
  }
  async function adminCall(action,body){
    if(!credential)throw Error('Unlock Admin first.');
    const response=await fetch('/api/admin/dhan/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok){if(response.status===403){credential='';sync();}throw Error(typeof result.detail==='string'?result.detail:'Connection check failed.');}
    return result;
  }
  window.thetaAdminRequest=async(path,body={})=>{
    if(!credential)throw Error('Open Admin and unlock with your password first.');
    const response=await fetch('/api/'+path,{method:'POST',signal:AbortSignal.timeout(240000),headers:{'Content-Type':'application/json','X-Theta-Admin-Password':credential},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok){if(response.status===403){credential='';sync();}throw Error(typeof result.detail==='string'?result.detail:'Collection request failed. Check inputs and status.');}
    return result;
  };
  window.thetaSaveDhanToken=async()=>{
    if(!editing)throw Error('Choose Edit token before replacing the saved connection.');
    const body=candidate();clearCandidate();saving=true;sync();
    try{
      const result=await adminCall('save',body);
      settings={...settings,client_id:body.client_id,token_saved:true};editing=false;
      await load();showResult(result);message('Token saved securely. Reuse it until Dhan expiry; Edit token when needed.');toast('Dhan token checked and saved.');
    }finally{saving=false;sync();}
  };
  sync();
})();
