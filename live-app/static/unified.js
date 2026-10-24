'use strict';
(() => {
  async function refresh(){
    try{
      const response=await fetch('/api/v2/system',{signal:AbortSignal.timeout(5000)});
      if(!response.ok)return;
      const value=await response.json();if(value.system!=='Theta Plus')return;
      const header=document.querySelector('main header');if(!header)return;
      let label=document.getElementById('unified-backend-state');
      if(!label){label=document.createElement('span');label.id='unified-backend-state';label.style.fontSize='12px';label.setAttribute('role','status');header.append(label);}
      label.textContent=value.owner?.state==='NEW_BACKEND'?'Unified backend · PostgreSQL':'Unified backend · Migration pending';
      label.title='Old data import: '+(value.migration?.state||'Pending')+'. Live and historical coverage require separate checks.';
    }catch{}
  }
  refresh();setInterval(refresh,30000);
})();
