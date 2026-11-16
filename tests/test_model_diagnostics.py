import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
import app.main as main


class GatewayDiagnostics(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, headers={'Origin': 'http://testserver'})
        main._qwen_meta_cache.clear()

    def tearDown(self):
        self.client.close()

    def request(self, model, handler):
        real = httpx.AsyncClient
        calls = []
        def record(req):
            calls.append(req)
            return handler(req)
        with patch('app.main.current_user', return_value={'id': 'diagnostic-test'}), patch('app.main.require_genisi_csrf', AsyncMock()), patch('app.main._get_hf_token', return_value=''), patch('app.main.httpx.AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(record), **kw)):
            payload = {'text': 'hi'} if model == 'carbon' else {'data': ['hi', None, None, None, [], '', .7, .8, 20]}
            res = self.client.post(f'/api/models/{"carbon" if model == "carbon" else "qwen"}/stream', json=payload)
        return res, calls

    def test_rate_limit_reports_retry_without_resending_or_leaking_body(self):
        for model in ('atlas', 'carbon'):
            with self.subTest(model=model), self.assertLogs('app.main', level='WARNING') as logs:
                res, calls = self.request(model, lambda req: httpx.Response(429, text='private-upstream-body', headers={'Retry-After': '27'}))
            self.assertEqual(len(calls), 1)
            self.assertIn('HTTP 429', res.text)
            self.assertIn('27 ثانية', res.text)
            self.assertNotIn('private-upstream-body', res.text + str(logs.output))
            self.assertIn('stage=start status=429', str(logs.output))

    def test_upstream_failures_do_not_resend_start_and_are_categorized(self):
        for model in ('atlas', 'carbon'):
            with self.subTest(model=model):
                res, calls = self.request(model, lambda req: httpx.Response(503, text='private'))
                self.assertEqual(len(calls), 1)
                self.assertIn('HTTP 503', res.text)
                self.assertNotIn('private', res.text)

    def test_retry_header_is_not_reflected_unchecked(self):
        res, _ = self.request('carbon', lambda req: httpx.Response(429, headers={'Retry-After': 'secret'}))
        self.assertNotIn('secret', res.text)
        self.assertIn('أعد المحاولة لاحقاً', res.text)

    def test_invalid_event_ids_and_json_are_protocol_errors(self):
        for model in ('atlas', 'carbon'):
            for body in ('not json', json.dumps({'event_id': '../secret'})):
                with self.subTest(model=model, body=body):
                    res, calls = self.request(model, lambda req: httpx.Response(200, text=body))
                    self.assertEqual(len(calls), 1)
                    self.assertIn('غير صالحة أو غير مكتملة', res.text)
                    self.assertNotIn('secret', res.text)

    def test_connection_timeout_and_interrupted_stream(self):
        for model in ('atlas', 'carbon'):
            def timeout(req):
                raise httpx.ConnectTimeout('private network details', request=req)
            res, _ = self.request(model, timeout)
            self.assertIn('مهلة', res.text)
            self.assertNotIn('private', res.text)
            def partial(req):
                return (httpx.Response(200, json={'event_id': 'ok'}) if req.method == 'POST' else
                        httpx.Response(200, text='', headers={'content-type': 'text/event-stream'}))
            res, _ = self.request(model, partial)
            self.assertIn('event: error' if model == 'atlas' else '"type":"error"', res.text)
            self.assertNotIn('event: complete', res.text)
            self.assertNotIn('"type":"done"', res.text)

if __name__ == '__main__':
    unittest.main()
