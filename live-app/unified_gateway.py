"""Keep the existing HTTPS URL while the new VM owns application/data processing.

Enabled only by the owner handover. Local fence/health routes stay on Cloud Run.
Backend port is on a private IP, restricted to a dedicated Cloud Run subnet.
"""
import asyncio
import ipaddress
import logging
from urllib.parse import urlsplit
import httpx
from websockets.asyncio.client import connect

LOCAL = {'/health-check', '/api/migration/status', '/api/collection/account-budget'}
HOP = {b'connection', b'keep-alive', b'proxy-authenticate', b'proxy-authorization',
       b'te', b'trailer', b'transfer-encoding', b'upgrade', b'content-length'}
WIRE = logging.Logger('theta.gateway.private', level=logging.CRITICAL+1)
WIRE.propagate=False;WIRE.addHandler(logging.NullHandler())


class UnifiedGateway:
    def __init__(self, local, upstream, client_factory=httpx.AsyncClient, connector=connect):
        parsed=urlsplit(upstream)
        try: address=ipaddress.ip_address(parsed.hostname or '')
        except ValueError: raise ValueError('GATEWAY_REQUIRES_PRIVATE_IPV4') from None
        if (parsed.scheme!='http' or address.version!=4 or not address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or
            parsed.port!=8000 or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment):
            raise ValueError('GATEWAY_REQUIRES_PRIVATE_IPV4_PORT_8000')
        self.local,self.upstream,self.client_factory,self.connect=local,upstream.rstrip('/'),client_factory,connector

    async def __call__(self, scope, receive, send):
        if scope['type']=='lifespan' or scope.get('path') in LOCAL:
            return await self.local(scope,receive,send)
        if scope['type']=='websocket':
            return await self.websocket(scope,receive,send)
        if scope['type']!='http': return await self.local(scope,receive,send)
        body=bytearray()
        while True:
            event=await receive()
            if event['type']=='http.disconnect': return
            body.extend(event.get('body',b''))
            if len(body)>12*1024*1024:
                await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'application/json')]})
                await send({'type':'http.response.body','body':b'{"detail":"Upload limit exceeded"}'})
                return
            if not event.get('more_body'): break
        path=scope.get('raw_path',scope['path'].encode()).decode('ascii')
        query=scope.get('query_string',b'').decode('ascii')
        url=self.upstream+path+('?' + query if query else '')
        headers=[(k,v) for k,v in scope.get('headers',[]) if k.lower() not in HOP and k.lower()!=b'host']
        headers.append((b'host',b'theta-plus-jw4jpkebxa-el.a.run.app'))
        async def disconnected():
            while True:
                event=await receive()
                if event['type']=='http.disconnect':return
                await asyncio.sleep(0)
        producer=asyncio.create_task(self.forward(scope,url,headers,body,send))
        monitor=asyncio.create_task(disconnected())
        try:
            done,_=await asyncio.wait((producer,monitor),return_when=asyncio.FIRST_COMPLETED)
            for task in done:task.result()
        finally:
            producer.cancel();monitor.cancel()
            await asyncio.gather(producer,monitor,return_exceptions=True)

    async def forward(self,scope,url,headers,body,send):
        started=False
        try:
            async with self.client_factory(timeout=httpx.Timeout(300,connect=10),follow_redirects=False) as client:
                request=client.build_request(scope['method'],url,headers=headers,content=bytes(body))
                response=await client.send(request,stream=True)
                try:
                    await send({'type':'http.response.start','status':response.status_code,
                        'headers':[(k,v) for k,v in response.headers.raw if k.lower() not in HOP]})
                    started=True
                    async for part in response.aiter_raw():
                        await send({'type':'http.response.body','body':part,'more_body':True})
                    await send({'type':'http.response.body','body':b''})
                finally: await response.aclose()
        except asyncio.CancelledError: raise
        except Exception:
            # No fallback to an old application with a different data owner.
            if not started:
                await send({'type':'http.response.start','status':503,
                    'headers':[(b'content-type',b'application/json'),(b'retry-after',b'10')]})
                await send({'type':'http.response.body','body':b'{"detail":"Unified backend temporarily unavailable; retry shortly"}'})
            else:
                raise

    async def websocket(self,scope,receive,send):
        headers=dict(scope.get('headers',[]))
        origin=headers.get(b'origin',b'').decode()
        host=headers.get(b'host',b'').decode()
        if scope['path']!='/live/ws' or (origin and urlsplit(origin).netloc!=host):
            await send({'type':'websocket.close','code':1008});return
        event=await receive()
        if event['type']!='websocket.connect': return
        url=self.upstream.replace('http://','ws://',1)+'/live/ws'
        try:
            async with self.connect(url,origin=self.upstream,open_timeout=10,close_timeout=5,
                    max_size=1048576,max_queue=16,logger=WIRE) as upstream:
                await send({'type':'websocket.accept'})
                async def inbound():
                    while True:
                        event=await receive()
                        if event['type']=='websocket.disconnect': return
                        value=event.get('text') if event.get('text') is not None else event.get('bytes')
                        if value is not None: await upstream.send(value)
                async def outbound():
                    async for value in upstream:
                        await send({'type':'websocket.send',**({'text':value} if isinstance(value,str) else {'bytes':value})})
                tasks=[asyncio.create_task(inbound()),asyncio.create_task(outbound())]
                try:
                    done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for task in done: task.result()
                finally:
                    for task in tasks: task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except asyncio.CancelledError: raise
        except Exception:
            await send({'type':'websocket.close','code':1011})
