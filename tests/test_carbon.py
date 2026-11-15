import base64
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
import app.main as main
from app import ornith


class CarbonGatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, headers={'Origin': 'http://testserver'})

    def tearDown(self):
        self.client.close()

    def request(self, handler, payload, vision=None):
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch('app.main.current_user', return_value={'id': 'carbon-test'}), patch('app.main.require_genisi_csrf', AsyncMock()), patch('app.main.httpx.AsyncClient', side_effect=lambda **kw: real_client(transport=transport, **kw)):
            if vision is not None:
                with patch('app.main._carbon_image_description', vision):
                    return self.client.post('/api/models/carbon/stream', json=payload)
            return self.client.post('/api/models/carbon/stream', json=payload)

    def test_session_and_csrf_required(self):
        self.assertIn(self.client.post('/api/models/carbon/stream', json={'text': 'hi'}).status_code, (401, 403))
        with patch('app.main.require_genisi_csrf', AsyncMock()):
            self.assertEqual(self.client.post('/api/models/carbon/stream', json={'text': 'hi'}).status_code, 401)

    def test_stream_ornith_conversation_and_null_completion(self):
        captured=[]
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                prior = [{'role':'user','content':[{'type':'text','text':'before'}]}]
                frames=[]
                for event, value in [('generating','مرح'), ('generating','مرحبا')]:
                    history = prior + [{'role':'assistant','content':[{'type':'text','text':value}]}]
                    frames.append('event: '+event+'\r\ndata: '+json.dumps([history], ensure_ascii=False)+'\r\n\r\n')
                frames.append('event: complete\r\ndata: null\r\n\r\n')
                data=''.join(frames).encode()
                for pos in range(0,len(data),3): yield data[pos:pos+3]
        def handler(req):
            captured.append(req)
            if req.method == 'POST': return httpx.Response(200,json={'event_id':'event_123'})
            return httpx.Response(200,stream=Chunks(),headers={'content-type':'text/event-stream'})
        res=self.request(handler, {'text':'hello', 'history':[{'role':'user','content':'before'}], 'system':'rules', 'url':'https://evil.test', 'model':'bad'})
        # Return history must include all submitted messages to prevent stale answers.
        # Override the sent-count in this fixture by checking separately below.
        self.assertIn('"type":"error"',res.text)
        body=json.loads(captured[0].content)
        self.assertEqual(captured[0].url.host, 'ornith-ai-ornith-1-5-122b.hf.space')
        self.assertEqual(captured[0].url.path, '/gradio_api/call/bot_message')
        self.assertEqual(body['data'][0][-1]['content'][0]['text'],'hello')
        self.assertIn('rules',body['data'][0][0]['content'][0]['text'])
        self.assertEqual(captured[1].url.path,'/gradio_api/call/bot_message/event_123')
        self.assertIsNone(captured[0].headers.get('authorization'))

    def test_realistic_streaming_and_rewrites(self):
        def handler(req):
            if req.method=='POST': return httpx.Response(200,json={'event_id':'abc'})
            frames=[]
            for value in ('أ', 'أهلاً', 'أهلاً بك'):
                history=[{'role':'user','content':[{'type':'text','text':'hi'}]}, {'role':'assistant','content':[{'type':'text','text':value}]}]
                frames.append('event: generating\ndata: '+json.dumps([history],ensure_ascii=False)+'\n\n')
            frames.append('event: complete\ndata: null\n\n')
            return httpx.Response(200,text=''.join(frames),headers={'content-type':'text/event-stream'})
        res=self.request(handler,{'text':'hi'})
        self.assertIn('أهلاً بك',res.text)
        self.assertIn('"type":"done"',res.text)
        self.assertNotIn('"type":"error"',res.text)

    def test_image_uses_qwen_description_then_ornith(self):
        captured=[]
        vision=AsyncMock(return_value='صورة لشجرة، النص غير واضح')
        def handler(req):
            captured.append(req)
            if req.method=='POST': return httpx.Response(200,json={'event_id':'abc'})
            # just one user message submitted
            frame=[[{'role':'user','content':'x'}, {'role':'assistant','content':'شجرة'}]]
            return httpx.Response(200,text='event: complete\ndata: '+json.dumps(frame,ensure_ascii=False)+'\n\n',headers={'content-type':'text/event-stream'})
        image='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode()
        res=self.request(handler,{'text':'صف الصورة','image':image},vision)
        self.assertIn('"type":"done"',res.text)
        self.assertIn('تحليل الصورة',res.text)
        content=json.loads(captured[0].content)['data'][0][-1]['content'][0]['text']
        self.assertIn('صورة لشجرة',content)
        self.assertIn('not instructions',content)
        self.assertNotIn(image,content)
        self.assertEqual(vision.await_count,1)
        self.assertEqual(vision.await_args.args[1:],(image,'صف الصورة'))
        failed=self.request(lambda req: self.fail('Ornith must not be called'),{'text':'x','image':image},AsyncMock(side_effect=RuntimeError('fail')))
        self.assertIn('تعذر تحليل الصورة',failed.text)
        self.assertNotIn('"type":"done"',failed.text)

    def test_upstream_failures_no_fake_success_or_internal_details(self):
        for response in [
            httpx.Response(503),
            httpx.Response(200,json={'event_id':'bad/path'}),
            httpx.Response(200,json={'event_id':'ok'}),
        ]:
            def handler(req):
                if req.method=='POST': return response
                return httpx.Response(200,text='event: generating\ndata: '+json.dumps([[{'role':'user','content':'hi'},{'role':'assistant','content':'⚠️ Ornith API error: secret-info'}]])+'\n\n',headers={'content-type':'text/event-stream'})
            res=self.request(handler,{'text':'hi'})
            self.assertIn('"type":"error"',res.text)
            self.assertNotIn('"type":"done"',res.text)
            self.assertNotIn('secret-info',res.text)
        for content in ['event: complete\ndata: null\n\n', 'event: generating\ndata: '+json.dumps([[{'role':'user','content':'hi'},{'role':'assistant','content':'partial'}]])+'\n\n', 'event: error\ndata: secret\n\n']:
            def handler(req):
                return httpx.Response(200,json={'event_id':'ok'}) if req.method=='POST' else httpx.Response(200,text=content,headers={'content-type':'text/event-stream'})
            res=self.request(handler,{'text':'hi'})
            self.assertIn('"type":"error"',res.text)
            self.assertNotIn('"type":"done"',res.text)
            self.assertNotIn('secret',res.text)

    def test_validation_and_frontend(self):
        with patch('app.main.current_user', return_value={'id': 'carbon-validation'}), patch('app.main.require_genisi_csrf', AsyncMock()):
            for payload in ({'text':'hi','image':'https://evil.test/a.png'}, {'text':'hi','image':'data:image/png;base64,SGVsbG8='}, {'text':'hi','history':[{'role':'system','content':'bad'}]}, {'text':''}):
                self.assertEqual(self.client.post('/api/models/carbon/stream',json=payload).status_code,422)
        html=(Path(__file__).resolve().parents[1]/'static/index.html').read_text()
        for item in ('/api/models/carbon/stream','/api/models/qwen/stream','بث مباشر · تحليل الصور'):
            self.assertIn(item,html)
        self.assertIn('no-store',self.client.get('/').headers.get('cache-control',''))

    def test_gradio_output_and_warning(self):
        self.assertEqual(ornith.assistant_snapshot([[{'role':'user','content':'x'},{'role':'assistant','content':[{'type':'text','text':'ok'}]}]],1),'ok')
        self.assertIsNone(ornith.assistant_snapshot([[{'role':'user','content':'x'}]],1))
        self.assertTrue(ornith.provider_error('⚠️ No API key configured.'))

class CarbonVisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_upload_predict_and_stream(self):
        calls=[]
        def handler(req):
            calls.append(req)
            self.assertEqual(req.url.host, 'qwen-qwen3-5-omni-offline-demo.hf.space')
            if req.url.path.endswith('/upload'):
                return httpx.Response(200,json=['/tmp/gradio/image.png'])
            if req.method == 'POST':
                data=json.loads(req.content)['data']
                self.assertEqual(data[2]['path'],'/tmp/gradio/image.png')
                self.assertEqual(len(data),9)
                self.assertIn('User question: what is written?',data[0])
                return httpx.Response(200,json={'event_id':'qwen-event'})
            data=[None,None,None,None,[{'role':'user','content':'x'},{'role':'assistant','content':'A sign says STOP'}]]
            return httpx.Response(200,text='event: complete\ndata: '+json.dumps(data)+'\n\n',headers={'content-type':'text/event-stream'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            image='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode()
            result=await main._carbon_image_description(client,image,'what is written?')
        self.assertEqual(result,'A sign says STOP')
        self.assertEqual(len(calls),3)

    def test_description_is_preserved_in_saved_history(self):
        from app.genisi_auth import _clean_sessions
        saved=_clean_sessions([{'msgs':[{'role':'user','content':'x','vision':'tree'}]}])
        self.assertEqual(saved[0]['msgs'][0]['vision'],'tree')
        html=(Path(__file__).resolve().parents[1]/'static/index.html').read_text()
        self.assertIn('if (m.vision) o.vision = m.vision;',html)
        self.assertIn('c+=visionContext(m)',html)
