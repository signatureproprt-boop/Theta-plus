const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function setup(){let now=10000,calls=0;const context={window:{},Date:class extends Date{static now(){return now;}},fetch:async()=>{calls++;return new Response('Rate exceeded.',{status:429,headers:{'Retry-After':'20'}});}};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/api-response.js'),'utf8'),context);return {context,advance:ms=>now+=ms,calls:()=>calls};}
test('plain-text 429 respects Retry-After, blocks repeated calls, never replays POST',async()=>{
 const e=setup();await assert.rejects(e.context.window.thetaApiFetch('/api/admin/check',{method:'POST'}),/Wait 20 seconds/);
 await assert.rejects(e.context.window.thetaApiFetch('/api/admin/check',{method:'POST'}),/Wait 20 seconds/);assert.equal(e.calls(),1);
 e.advance(21000);await assert.rejects(e.context.window.thetaApiFetch('/api/status'),/Wait 20 seconds/);assert.equal(e.calls(),2);
});
test('HTML and empty failures stay safe and never expose raw proxy bodies',async()=>{
 const e=setup();for(const body of ['<html>private proxy diagnostic</html>',''])await assert.rejects(e.context.window.thetaReadResponse(new Response(body,{status:503})),error=>/HTTP 503/.test(error.message)&&!error.message.includes('private'));
});
test('valid API errors survive and malformed successes are rejected',async()=>{
 const e=setup();await assert.rejects(e.context.window.thetaReadResponse(new Response('{"detail":"Invalid dates"}',{status:422})),/Invalid dates/);
 for(const body of ['null','[]','"hello"','not-json'])await assert.rejects(e.context.window.thetaReadResponse(new Response(body)));
 assert.equal((await e.context.window.thetaReadResponse(new Response('{"saved":true}'))).saved,true);
});
