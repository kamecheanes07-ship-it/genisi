import json
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
import app.main as main

class ThreeGatewayTests(unittest.TestCase):
    def setUp(self): self.client=TestClient(main.app,headers={'Origin':'http://testserver'})
    def tearDown(self): self.client.close()
    def run_request(self, handler, body):
        real=httpx.AsyncClient
        requests=[]
        def record(req): requests.append(req); return handler(req)
        with patch('app.main.current_user',return_value={'id':'3d-test'}), patch('app.main.require_genisi_csrf',AsyncMock()), patch('app.main._rate_limit'), patch('app.main.httpx.AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(record),**kw)):
            return self.client.post('/api/models/3d/generate',json=body),requests
    def test_auth_and_csrf(self):
        self.assertIn(self.client.post('/api/models/3d/generate',json={'prompt':'cube'}).status_code,(401,403))
    def test_success_fixed_upstream_and_utf8_chunks(self):
        frames=['data: '+json.dumps({'t':piece},ensure_ascii=False)+'\n\n' for piece in ['// مجسم\n','return new THREE.Group();']]
        frames.append('data: '+json.dumps({'done':True})+'\n\n')
        class Split(httpx.AsyncByteStream):
            async def __aiter__(self):
                b=''.join(frames).encode()
                for i in range(0,len(b),3): yield b[i:i+3]
        def handler(req):
            self.assertEqual(str(req.url),'https://starpowertechnology-wvy-3d-23m.hf.space/api/generate')
            self.assertEqual(json.loads(req.content),{'prompt':'a cube','format':'threejs','temperature':0.45})
            self.assertNotIn('authorization',req.headers)
            return httpx.Response(200,stream=Split(),headers={'content-type':'text/event-stream'})
        res,calls=self.run_request(handler,{'prompt':'a cube','url':'https://evil.invalid'})
        self.assertEqual(res.status_code,200,res.text)
        self.assertEqual(res.json()['code'],'// مجسم\nreturn new THREE.Group();')
        self.assertEqual(len(calls),1)
    def test_errors_no_partial_or_provider_details(self):
        for content in ['data: {"t":"partial"}\n\n','data: {"error":"private"}\n\n','data: {oops}\n\n','data: {"done":true}\n\n','data: '+json.dumps({'t':'a'*260000})+'\n\n']:
            res,_=self.run_request(lambda req:httpx.Response(200,text=content,headers={'content-type':'text/event-stream'}),{'prompt':'cube'})
            self.assertEqual(res.status_code,502)
            self.assertNotIn('private',res.text);self.assertNotIn('partial',res.text)
        res,calls=self.run_request(lambda req:httpx.Response(429,headers={'retry-after':'12'}),{'prompt':'cube'})
        self.assertEqual(res.status_code,502);self.assertIn('429',res.text);self.assertEqual(len(calls),1)
        res,_=self.run_request(lambda req:httpx.Response(200,text='oops',headers={'content-type':'text/html'}),{'prompt':'cube'})
        self.assertEqual(res.status_code,502)
    def test_prompt_validation(self):
        for body in ({'prompt':''},{'prompt':' '*5},{'prompt':'a'*301}):
            res,calls=self.run_request(lambda req:self.fail('no request'),body)
            self.assertEqual(res.status_code,422);self.assertEqual(calls,[])

if __name__=='__main__': unittest.main()
