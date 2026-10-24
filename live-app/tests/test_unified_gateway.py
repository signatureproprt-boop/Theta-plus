import asyncio
import pytest
import httpx
from unified_gateway import UnifiedGateway


@pytest.mark.parametrize('upstream',['https://evil.com','http://127.0.0.1:8000','http://169.254.169.254:8000','http://10.1.1.1:8000/x','http://u:p@10.1.1.1:8000'])
def test_gateway_target_is_fixed_private_backend(upstream):
    with pytest.raises(ValueError): UnifiedGateway(None,upstream)


def test_same_https_host_forwards_to_new_backend_without_changing_body():
    async def exercise():
        requests=[]
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self): yield b'{"system":"Theta Plus"}'
        async def handler(request):
            requests.append(request)
            return httpx.Response(200,stream=Stream(),headers={'content-type':'application/json'})
        factory=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler),**kw)
        gateway=UnifiedGateway(None,'http://10.160.0.2:8000',client_factory=factory)
        output=[]
        async def receive():return {'type':'http.request','body':b'{"selection":1}','more_body':False}
        async def send(event):output.append(event)
        await gateway({'type':'http','method':'POST','path':'/api/test','raw_path':b'/api/test',
            'query_string':b'a=1','headers':[(b'host',b'theta-plus-jw4jpkebxa-el.a.run.app'),
            (b'content-type',b'application/json')]},receive,send)
        assert output[0]['status']==200
        assert requests[0].url.host=='10.160.0.2'
        assert requests[0].headers['host']=='theta-plus-jw4jpkebxa-el.a.run.app'
        assert requests[0].content==b'{"selection":1}'
    asyncio.run(exercise())


def test_fence_and_health_stay_local_and_failure_does_not_switch_backends():
    async def exercise():
        local=[]
        async def app(scope,receive,send):local.append(scope['path'])
        gateway=UnifiedGateway(app,'http://10.160.0.2:8000')
        await gateway({'type':'http','path':'/api/migration/status'},None,None)
        assert local==['/api/migration/status']
        async def handler(request):raise httpx.ConnectError('PRIVATE_NETWORK_DOWN')
        gateway.client_factory=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler),**kw)
        output=[]
        async def receive():return {'type':'http.request','body':b''}
        async def send(event):output.append(event)
        await gateway({'type':'http','method':'GET','path':'/','headers':[]},receive,send)
        assert output[0]['status']==503 and local==['/api/migration/status']
    asyncio.run(exercise())


def test_browser_disconnect_closes_upstream_stream_promptly():
    async def exercise():
        begun,closed=asyncio.Event(),asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                begun.set()
                await asyncio.Future()
                yield b''
            async def aclose(self):closed.set()
        async def handler(request):return httpx.Response(200,stream=Stream())
        factory=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler),**kw)
        gateway=UnifiedGateway(None,'http://10.160.0.2:8000',client_factory=factory)
        received=False
        async def receive():
            nonlocal received
            if not received:
                received=True
                return {'type':'http.request','body':b''}
            await begun.wait()
            return {'type':'http.disconnect'}
        async def send(event):pass
        await asyncio.wait_for(gateway({'type':'http','method':'GET','path':'/events','headers':[]},receive,send),2)
        assert closed.is_set()
    asyncio.run(exercise())
