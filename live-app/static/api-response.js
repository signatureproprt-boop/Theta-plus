'use strict';
(() => {
  let cooldownUntil=0;
  function limited(response){
    const raw=response.headers?.get?.('Retry-After');
    const seconds=raw&&/^\d+$/.test(raw)?Number(raw):raw?(Date.parse(raw)-Date.now())/1000:30;
    const wait=Number.isFinite(seconds)?Math.min(300,Math.max(1,Math.ceil(seconds))):30;
    cooldownUntil=Math.max(cooldownUntil,Date.now()+wait*1000);
    return rateError();
  }
  function rateError(){const wait=Math.max(1,Math.ceil((cooldownUntil-Date.now())/1000));const error=Error(`Too many requests. Wait ${wait} seconds, then retry. Saved token and records have not been changed.`);error.status=429;error.retryAfterSeconds=wait;return error;}
  window.thetaApiFetch=async(...args)=>{
    if(Date.now()<cooldownUntil)throw rateError();
    const response=await fetch(...args);
    if(response.status===429)throw limited(response);
    return response;
  };
  window.thetaReadResponse=async response=>{
    if(response.status===429)throw limited(response);
    const body=await response.text();let value;
    try{value=JSON.parse(body);}catch{
      // Proxy errors may be plain text/HTML. Never show their raw body or turn
      // a failed status read into evidence that credentials are missing.
      const error=Error(`Server response unavailable (HTTP ${response.status}). Retry later; saved connection has not been changed.`);error.status=response.status;throw error;
    }
    if(!response.ok){const error=Error(typeof value?.detail==='string'?value.detail:`Request failed (HTTP ${response.status}).`);error.status=response.status;throw error;}
    if(!value||typeof value!=='object'||Array.isArray(value))throw Error('Server returned an invalid data response. Retry later.');
    return value;
  };
})();
