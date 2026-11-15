import base64
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
import app.main as main
from app import zai_session


def token():
    payload = base64.urlsafe_b64encode(json.dumps({'id': 'test-user'}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


class CarbonGatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, headers={'Origin': 'http://testserver'})
        self.env = patch.dict(os.environ, {'ZAI_SESSION_TOKEN': token()})
        self.env.start()

    def tearDown(self):
        self.client.close()
        self.env.stop()

    def request(self, handler, payload):
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch('app.main.current_user', return_value={'id': 'carbon-test'}), patch('app.main.require_genisi_csrf', AsyncMock()), patch('app.main.httpx.AsyncClient', side_effect=lambda **kw: real_client(transport=transport, **kw)):
            return self.client.post('/api/models/carbon/stream', json=payload)

    def test_session_and_csrf_required(self):
        self.assertIn(self.client.post('/api/models/carbon/stream', json={'text': 'hi'}).status_code, (401, 403))
        with patch('app.main.require_genisi_csrf', AsyncMock()):
            self.assertEqual(self.client.post('/api/models/carbon/stream', json={'text': 'hi'}).status_code, 401)

    def test_web_stream_and_image_upload(self):
        captured = []
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                text = ('data: {"data":{"delta_content":"<details>تفكير</details>مرحب"}}\r\n\r\n'
                        'data: {"data":{"delta_content":"ا"}}\r\n\r\n'
                        'data: {"data":{"phase":"done"}}\r\n\r\n').encode()
                for i in range(0, len(text), 3):
                    yield text[i:i+3]
        def handler(req):
            captured.append(req)
            if req.url.path == '/api/v1/files/':
                return httpx.Response(200, json={'id': 'file-123', 'filename': 'image.png'})
            return httpx.Response(200, stream=Chunks(), headers={'content-type': 'text/event-stream'})
        image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode()
        response = self.request(handler, {'text': 'صف الصورة', 'system': 'system', 'history': [{'role': 'user', 'content': 'قبل'}], 'image': image, 'model': 'bad', 'thinking': False, 'url': 'https://evil.test'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('تفكير', response.text)
        self.assertIn('مرحبا', response.text)
        self.assertIn('"type":"done"', response.text)
        self.assertNotIn(token(), response.text)
        self.assertEqual(len(captured), 2)
        req = captured[1]
        self.assertEqual(req.url.host, 'chat.z.ai')
        self.assertEqual(req.url.path, '/api/v2/chat/completions')
        self.assertEqual(req.headers['authorization'], 'Bearer ' + token())
        self.assertEqual(req.url.params['token'], token())
        self.assertTrue(req.headers['x-signature'])
        body = json.loads(req.content)
        self.assertEqual(body['model'], 'x-preview-l')
        self.assertIs(body['features']['enable_thinking'], True)
        self.assertEqual(body['signature_prompt'], 'صف الصورة')
        self.assertEqual(body['messages'][-1]['content'][0]['image_url']['url'], 'file-123')
        self.assertEqual(body['files'][0]['id'], 'file-123')
        self.assertTrue(body['messages'][0]['content'].startswith('[System Instructions]'))

    def test_provider_failures_no_leaks_or_fake_completion(self):
        cases = [httpx.Response(401, text=token()), httpx.Response(429),
                 httpx.Response(200, text='verification', headers={'content-type':'text/html'}),
                 httpx.Response(200, text='data: {"error":{"message":"captcha"}}\n\n', headers={'content-type':'text/event-stream'}),
                 httpx.Response(200, text='data: {"data":{"delta_content":"partial"}}\n\n', headers={'content-type':'text/event-stream'})]
        for upstream in cases:
            response = self.request(lambda req: upstream, {'text':'hello'})
            self.assertIn('"type":"error"', response.text)
            self.assertNotIn('"type":"done"', response.text)
            self.assertNotIn(token(), response.text)

    def test_nested_errors_are_not_reported_as_empty_answer_or_leaked(self):
        for err, expected in [
            ({'code': 'CAPTCHA_REQUIRED', 'message': 'captcha ' + token()}, 'التحقق'),
            ({'code': 'MODEL_CONCURRENCY_LIMIT', 'detail': 'Model is currently at capacity'}, 'حد الطلبات'),
            ({'code': 'AUTHENTICATION_FAILED', 'message': 'authentication failed'}, 'جلسة الحساب'),
            ({'code': 'OTHER', 'message': token()}, 'خطأ داخل البث'),
        ]:
            for nested in (False, True):
                data = {'data': {'error': err}} if nested else {'error': err}
                text = 'data: ' + json.dumps({'data': data}) + '\n\ndata: [DONE]\n\n'
                response = self.request(lambda req: httpx.Response(200, text=text, headers={'content-type':'text/event-stream'}), {'text':'hello'})
                self.assertIn(expected, response.text)
                self.assertNotIn(token(), response.text)
                self.assertNotIn('"type":"done"', response.text)

    def test_empty_content_on_final_event_does_not_erase_answer(self):
        text = 'data: {"data":{"delta_content":"hello"}}\n\ndata: {"data":{"phase":"done","content":""}}\n\n'
        response = self.request(lambda req: httpx.Response(200, text=text, headers={'content-type':'text/event-stream'}), {'text':'hello'})
        self.assertIn('hello', response.text)
        self.assertIn('"type":"done"', response.text)
        self.assertNotIn('"type":"error"', response.text)
        self.assertEqual(zai_session.update_text('hello', {'edit_index': 2, 'edit_content': ''}), 'he')

    def test_config_and_multimodal_validation(self):
        with patch('app.main.current_user', return_value={'id': 'carbon-config'}), patch('app.main.require_genisi_csrf', AsyncMock()):
            for value in ('', 'bad-token'):
                with patch.dict(os.environ, {'ZAI_SESSION_TOKEN': value, 'ZAI_API_KEY': 'old-key'}):
                    self.assertEqual(self.client.post('/api/models/carbon/stream', json={'text':'hi'}).status_code, 503)
            for payload in ({'text':'hi', 'image':'https://evil.test/a.png'}, {'text':'hi', 'image':'data:image/png;base64,SGVsbG8='}, {'text':'hi', 'history':[{'role':'system','content':'bad'}]}, {'text':''}):
                self.assertEqual(self.client.post('/api/models/carbon/stream', json=payload).status_code, 422)

    def test_followup_and_snapshot_edits(self):
        seen = []
        def handler(req):
            seen.append(req)
            return httpx.Response(200, text='data: {"data":{"content":"😀first"}}\n\ndata: {"data":{"edit_index":2,"edit_content":"correct"}}\n\ndata: [DONE]\n\n', headers={'content-type':'text/event-stream'})
        response = self.request(handler, {'text':'', 'history':[{'role':'user','content':'tool result'}]})
        self.assertIn('😀correct', response.text)
        self.assertIn('"type":"done"', response.text)
        self.assertEqual(json.loads(seen[0].content)['signature_prompt'], 'tool result')
        self.assertEqual(zai_session.split_text('<det'), ('', ''))
        self.assertEqual(zai_session.split_text('<details>thinking</det'), ('thinking', ''))

    def test_index_disables_caching_of_the_app_shell(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers.get('cache-control', ''))
        self.assertIn("small:'Z.AI · تفكير دائم وبث مباشر'", response.text)

    def test_frontend_routes(self):
        html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text()
        for value in ('/api/models/carbon/stream', '/api/models/qwen/stream', "state.routeMode==='carbon'", "event.type==='snapshot'", 'انقطع بث Carbon قبل اكتمال الرد'):
            self.assertIn(value, html)
        self.assertNotIn(token(), html)
