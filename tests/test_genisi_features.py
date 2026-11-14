import hashlib
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]


def _solve_pow(prefix: str, bits: int) -> str:
    for nonce in range(5_000_000):
        digest = hashlib.sha256(f"{prefix}:{nonce}".encode("utf-8")).digest()
        zeros = 0
        for byte in digest:
            if byte == 0:
                zeros += 8
                continue
            zeros += 8 - byte.bit_length()
            break
        if zeros >= bits:
            return str(nonce)
    raise AssertionError("Failed to solve PoW in test")


def _solve_math_question(question: str) -> str:
    m_mul = re.search(r"\((\d+)\s*×\s*(\d+)\)\s*\+\s*(\d+)", question)
    if m_mul:
        return str(int(m_mul.group(1)) * int(m_mul.group(2)) + int(m_mul.group(3)))
    m_add = re.search(r"(\d+)\s*\+\s*(\d+)\s*([+−-])\s*(\d+)", question)
    if m_add:
        a, b, op, c = int(m_add.group(1)), int(m_add.group(2)), m_add.group(3), int(m_add.group(4))
        return str(a + b + c if op == "+" else a + b - c)
    raise AssertionError(f"Unrecognized challenge question: {question}")


class GenisiMediaVoiceMusicTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "GENISI_LOCAL_DATA_DIR": self._tmpdir.name,
                "SESSION_SECRET": "test-session-secret-32-chars-minimum-123456",
                "DATASET_ENCRYPTION_KEY": "test-dataset-encryption-key-32-chars-min-7890",
                "COOKIE_SECURE": "0",
                "COOKIE_SAMESITE": "lax",
            },
            clear=False,
        )
        self._env.start()

        import app.genisi_auth as ga
        import app.main as main_mod

        ga._hub = None
        ga._challenges.clear()
        ga._rate_events.clear()
        ga._login_failures.clear()
        self._orig_cookie = (ga.COOKIE_SECURE, ga.COOKIE_SAMESITE, ga.PARTITIONED_COOKIES)
        ga.COOKIE_SECURE = False
        ga.COOKIE_SAMESITE = "lax"
        ga.PARTITIONED_COOKIES = False
        main_mod._voxcpm_meta_cache.update({"ts": 0.0, "prefix": None})

        self.ga = ga
        self.main_mod = main_mod
        self.client = TestClient(main_mod.app, headers={"Origin": "http://testserver"})
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.ga.COOKIE_SECURE, self.ga.COOKIE_SAMESITE, self.ga.PARTITIONED_COOKIES = self._orig_cookie
        self.ga._hub = None
        self._env.stop()
        self._tmpdir.cleanup()

    def _register_user(self, client: TestClient, username: str, email: str, password: str = "StrongPass#2026!") -> dict:
        ch_res = client.post("/api/auth/challenge", json={"mode": "register"})
        self.assertEqual(ch_res.status_code, 200, ch_res.text)
        ch = ch_res.json()
        answer = _solve_math_question(ch["question"])
        nonce = _solve_pow(ch["pow_prefix"], int(ch["difficulty"]))
        reg_res = client.post(
            "/api/auth/register",
            json={
                "username": username,
                "email": email,
                "password": password,
                "challenge_id": ch["challenge_id"],
                "answer": answer,
                "pow_nonce": nonce,
            },
        )
        self.assertEqual(reg_res.status_code, 200, reg_res.text)
        return reg_res.json()["user"]

    def _csrf_headers(self, client: TestClient, extra: dict | None = None) -> dict:
        csrf = client.cookies.get("genisi_csrf", "")
        headers = {"X-CSRF-Token": csrf}
        if extra:
            headers.update(extra)
        return headers

    def test_encrypted_media_persistence_and_15_day_inactivity_cleanup(self) -> None:
        user = self._register_user(self.client, "anes_dev", "anes@example.com")
        uid = user["id"]

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR-genisi-image-payload"
        mp3_bytes = b"ID3\x04\x00\x00\x00\x00\x00\x10genisi-stepaudio-track"
        svg_bytes = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"

        # 1) Upload image, music audio, and SVG attachment
        r_img = self.client.post(
            "/api/media/upload?id=m_img01&kind=image&name=hero.png&mime=image/png",
            content=png_bytes,
            headers=self._csrf_headers(self.client, {"Content-Type": "image/png"}),
        )
        self.assertEqual(r_img.status_code, 200, r_img.text)
        self.assertEqual(r_img.json()["asset"]["ttl_days"], 15)

        r_aud = self.client.post(
            "/api/media/upload?id=m_aud01&kind=audio&name=song.mp3&mime=audio/mpeg",
            content=mp3_bytes,
            headers=self._csrf_headers(self.client, {"Content-Type": "audio/mpeg"}),
        )
        self.assertEqual(r_aud.status_code, 200, r_aud.text)

        r_svg = self.client.post(
            "/api/media/upload?id=a_svg01&kind=image&name=diagram.svg&mime=image/svg%2Bxml",
            content=svg_bytes,
            headers=self._csrf_headers(self.client, {"Content-Type": "image/svg+xml"}),
        )
        self.assertEqual(r_svg.status_code, 200, r_svg.text)

        # 2) Verify on-disk binary files are AES-256-GCM encrypted (never plaintext)
        enc_img_path = Path(self._tmpdir.name) / "media" / uid / "m_img01.bin"
        self.assertTrue(enc_img_path.is_file())
        raw_disk = enc_img_path.read_bytes()
        self.assertTrue(raw_disk.startswith(b"GMED1"))
        self.assertNotIn(b"IHDR-genisi-image-payload", raw_disk)

        # 3) Verify retrieval & safe Content-Disposition headers
        get_img = self.client.get("/api/media/m_img01")
        self.assertEqual(get_img.status_code, 200)
        self.assertEqual(get_img.content, png_bytes)
        self.assertIn("inline;", get_img.headers.get("content-disposition", ""))

        get_svg = self.client.get("/api/media/a_svg01")
        self.assertEqual(get_svg.status_code, 200)
        self.assertEqual(get_svg.content, svg_bytes)
        self.assertIn("attachment;", get_svg.headers.get("content-disposition", ""))
        self.assertIn("sandbox", get_svg.headers.get("content-security-policy", ""))

        # 4) Verify conversation history preserves both user attachments and assistant media
        sessions_payload = [
            {
                "title": "اختبار الوسائط والمرفقات",
                "ts": int(time.time() * 1000),
                "msgs": [
                    {
                        "role": "user",
                        "content": "حلل هذا الملف وأنشئ صورة وموسيقى",
                        "atts": [
                            {
                                "id": "a_svg01",
                                "kind": "image",
                                "name": "diagram.svg",
                                "mime": "image/svg+xml",
                                "size": len(svg_bytes),
                                "url": "/api/media/a_svg01",
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "تم الإنشاء بنجاح",
                        "media": [
                            {"id": "m_img01", "kind": "image", "url": "/api/media/m_img01"},
                            {"id": "m_aud01", "kind": "audio", "url": "/api/media/m_aud01", "title": "StepAudio Track"},
                        ],
                    },
                ],
            }
        ]
        save_res = self.client.post(
            "/api/history/save",
            json={"sessions": sessions_payload},
            headers=self._csrf_headers(self.client),
        )
        self.assertEqual(save_res.status_code, 200, save_res.text)
        hist_res = self.client.get("/api/history")
        self.assertEqual(hist_res.status_code, 200)
        hist_json = hist_res.json()
        self.assertEqual(hist_json["media_ids"], ["a_svg01", "m_aud01", "m_img01"])
        loaded_msgs = hist_json["sessions"][0]["msgs"]
        self.assertEqual(loaded_msgs[0]["atts"][0]["id"], "a_svg01")
        self.assertEqual(loaded_msgs[0]["atts"][0]["url"], "/api/media/a_svg01")
        self.assertEqual(loaded_msgs[1]["media"][0]["id"], "m_img01")
        self.assertEqual(loaded_msgs[1]["media"][1]["id"], "m_aud01")

        # 5) Cross-user isolation check: second user cannot access User 1's media
        other_client = TestClient(self.main_mod.app, headers={"Origin": "http://testserver"})
        with other_client:
            self._register_user(other_client, "other_user", "other@example.com")
            forbidden = other_client.get("/api/media/m_img01")
            self.assertEqual(forbidden.status_code, 404)

        # 6) Simulate 15+ days of non-use on m_img01 first (lazy 410 Gone on GET),
        #    while m_aud01 was created 20d ago and used 14d ago (refreshed via /api/media/touch)
        now = int(time.time())
        expired_ts = now - (15 * 86400 + 60)
        still_within_window_ts = now - (14 * 86400)

        def _age_first(rec: dict) -> None:
            rec["media_assets"]["m_img01"]["created_at"] = expired_ts - 3600
            rec["media_assets"]["m_img01"]["last_used_at"] = expired_ts
            rec["media_assets"]["m_aud01"]["created_at"] = now - (20 * 86400)
            rec["media_assets"]["m_aud01"]["last_used_at"] = still_within_window_ts

        self.ga._store().update_user(uid, _age_first)

        # Direct GET on expired m_img01 immediately returns 410 Gone and deletes its ciphertext blob
        gone_res = self.client.get("/api/media/m_img01")
        self.assertEqual(gone_res.status_code, 410)
        self.assertFalse(enc_img_path.exists())

        # Touch m_aud01 so it remains active despite being 20 days since creation!
        touch_res = self.client.post(
            "/api/media/touch",
            json={"ids": ["m_aud01"]},
            headers=self._csrf_headers(self.client),
        )
        self.assertEqual(touch_res.status_code, 200)
        self.assertEqual(touch_res.json()["touched"], 1)

        # Now age a_svg01 past 15 days and verify periodic background sweep purges it while keeping m_aud01
        def _age_svg(rec: dict) -> None:
            rec["media_assets"]["a_svg01"]["created_at"] = expired_ts - 3600
            rec["media_assets"]["a_svg01"]["last_used_at"] = expired_ts

        self.ga._store().update_user(uid, _age_svg)
        sweep = self.ga.cleanup_all_expired_media()
        self.assertEqual(sweep["assets_deleted"], 1)
        self.assertEqual(self.client.get("/api/media/a_svg01").status_code, 404)
        still_alive = self.client.get("/api/media/m_aud01")
        self.assertEqual(still_alive.status_code, 200)
        self.assertEqual(still_alive.content, mp3_bytes)

        # Explicit delete removes m_aud01 immediately
        del_res = self.client.post(
            "/api/media/delete",
            json={"ids": ["m_aud01"]},
            headers=self._csrf_headers(self.client),
        )
        self.assertEqual(del_res.status_code, 200)
        self.assertEqual(del_res.json()["deleted"], 1)
        self.assertEqual(self.client.get("/api/media/m_aud01").status_code, 404)

    def test_store_url_blocks_ssrf_targets(self) -> None:
        self._register_user(self.client, "sec_user", "sec@example.com")
        res = self.client.post(
            "/api/media/store-url",
            json={"url": "http://127.0.0.1:8000/api/health", "kind": "video"},
            headers=self._csrf_headers(self.client),
        )
        self.assertEqual(res.status_code, 400)

    def test_stepaudio3_music_submit_and_query_proxy(self) -> None:
        real_async_client = httpx.AsyncClient
        submitted_payloads: list[dict] = []

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if url.endswith("/v1/audio/music/submit"):
                body = json.loads(req.content.decode("utf-8"))
                submitted_payloads.append(body)
                return httpx.Response(200, json={"task_id": "step_task_99"})
            if url.endswith("/v1/audio/music/query"):
                body = json.loads(req.content.decode("utf-8"))
                return httpx.Response(
                    200,
                    json={
                        "task_id": body["task_id"],
                        "status": "SUCCESS",
                        "response_format": "mp3",
                        "audio": "//uQxAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAACcQCA",
                        "rewritten_lyrics": "[Verse 1]\nنسيم الفجر يغني",
                    },
                )
            raise AssertionError(f"Unexpected upstream URL: {url}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            self.main_mod.httpx,
            "AsyncClient",
            side_effect=lambda **kw: real_async_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        ):
            # Case A: Instrumental track -> must set instrumental=True, omit lyrics, and strip unknown fields (duration, vocal_language)
            sub = self.client.post(
                "/api/music/submit",
                json={
                    "task": "text_to_music",
                    "caption": "Warm acoustic Arabic oud and gentle lo-fi hip hop beat, 88 BPM",
                    "instrumental": True,
                    "lyrics": "[Instrumental]",
                    "duration": 45,
                    "vocal_language": "ar",
                },
            )
            self.assertEqual(sub.status_code, 200, sub.text)
            self.assertEqual(sub.json()["task_id"], "step_task_99")
            self.assertEqual(
                submitted_payloads[-1],
                {
                    "model_id": "step-music",
                    "response_format": "mp3",
                    "sample_rate": 44100,
                    "bit_rate": 192,
                    "task": "text_to_music",
                    "caption": "Warm acoustic Arabic oud and gentle lo-fi hip hop beat, 88 BPM",
                    "instrumental": True,
                },
            )

            # Case B: Vocal track -> must omit `instrumental` key completely and pass `lyrics`
            sub_vocal = self.client.post(
                "/api/music/submit",
                json={
                    "task": "text_to_music",
                    "caption": "Modern Arabic pop ballad with strings and piano",
                    "instrumental": False,
                    "lyrics": "[Verse]\nيا ليل الساهرين",
                },
            )
            self.assertEqual(sub_vocal.status_code, 200, sub_vocal.text)
            self.assertEqual(
                submitted_payloads[-1],
                {
                    "model_id": "step-music",
                    "response_format": "mp3",
                    "sample_rate": 44100,
                    "bit_rate": 192,
                    "task": "text_to_music",
                    "caption": "Modern Arabic pop ballad with strings and piano",
                    "lyrics": "[Verse]\nيا ليل الساهرين",
                },
            )

            # Query returns raw base64 audio untouched
            qry = self.client.post("/api/music/query", json={"task_id": "step_task_99"})
            self.assertEqual(qry.status_code, 200, qry.text)
            data = qry.json()
            self.assertEqual(data["status"], "SUCCESS")
            self.assertEqual(data["audio"], "//uQxAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAACcQCA")
            self.assertEqual(data["rewritten_lyrics"], "[Verse 1]\nنسيم الفجر يغني")

    def test_workspace_and_agent_state_do_not_503_when_sandbox_unconfigured(self) -> None:
        self._register_user(self.client, "regular_user", "regular@example.com")
        ws = self.client.get("/api/workspace")
        self.assertEqual(ws.status_code, 200, ws.text)
        self.assertEqual(ws.json(), {"success": True, "entries": [], "truncated": False, "sandbox_enabled": False})

        ag = self.client.get("/api/agent/state")
        self.assertEqual(ag.status_code, 200, ag.text)
        self.assertEqual(ag.json(), {"plan": [], "notes": [], "sessions": 0, "updated": None, "sandbox_enabled": False})

        # Direct workspace file read remains strictly protected when sandbox is unconfigured
        wf = self.client.get("/api/workspace/file?path=README.md")
        self.assertEqual(wf.status_code, 503)

    def test_voxcpm_voice_tts_disables_normalizer_for_arabic_and_streams_audio(self) -> None:
        captured_call_payload = {}
        fake_wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
        real_async_client = httpx.AsyncClient

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if url.endswith("/gradio_api/info"):
                return httpx.Response(200, json={"named_endpoints": {"/generate": {"parameters": []}}})
            if url.endswith("/gradio_api/call/generate") and req.method == "POST":
                body = json.loads(req.content.decode("utf-8"))
                captured_call_payload["data"] = body["data"]
                return httpx.Response(200, json={"event_id": "evt_vox_1"})
            if url.endswith("/gradio_api/call/generate/evt_vox_1"):
                sse = (
                    "event: generating\ndata: null\n\n"
                    'event: complete\ndata: [{"path":"/tmp/gradio/out.wav","url":"https://openbmb-voxcpm-demo.hf.space/gradio_api/file=/tmp/gradio/out.wav"}]\n\n'
                ).encode("utf-8")
                return httpx.Response(200, content=sse, headers={"Content-Type": "text/event-stream"})
            if "file=/tmp/gradio/out.wav" in url:
                return httpx.Response(200, content=fake_wav, headers={"Content-Type": "audio/wav"})
            raise AssertionError(f"Unexpected VoxCPM URL: {url}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            self.main_mod.httpx,
            "AsyncClient",
            side_effect=lambda **kw: real_async_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        ):
            res = self.client.post(
                "/api/voice/tts",
                json={
                    "text": "أهلاً بك في مكالمة جينيسي الصوتية المباشرة، كيف أستطيع مساعدتك اليوم؟",
                    "control_instruction": "Warm, articulate young adult male voice, natural conversational rhythm",
                    "do_normalize": True,  # Backend must override to False for Arabic script!
                },
            )
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.content, fake_wav)
            self.assertIn("audio/wav", res.headers.get("content-type", ""))
            sent_data = captured_call_payload["data"]
            self.assertEqual(len(sent_data), 8)
            self.assertEqual(sent_data[1], "Warm, articulate young adult male voice, natural conversational rhythm")
            self.assertFalse(sent_data[6], "do_normalize must be False when Arabic text is present")

    def test_frontend_ui_specs_image_square_and_voice_pill_bar(self) -> None:
        html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")

        # 1) Image Generation minimalist rounded square (image-2.png: 371x364, radius 28px)
        self.assertIn(".waitcard.img-square", html)
        self.assertRegex(html, r"aspect-ratio:\s*371\s*/\s*364")
        self.assertRegex(html, r"border-radius:\s*28px")

        # 2) Speaking Mode input bar only (image-1.png: 660x64 pill bar, radius 999px), normal composer untouched
        self.assertIn('id="viewVoice"', html)
        self.assertRegex(html, r"\.voice-pill-bar\s*\{[^}]*width:\s*min\(100%,\s*660px\)[^}]*height:\s*64px[^}]*border-radius:\s*999px")
        self.assertRegex(html, r"\.composer\s*\{[^}]*border-radius:\s*var\(--r-lg\)")

        # 3) StepAudio-3-Music & VoxCPM-Demo & Qwen/Image/Video wired strictly via Python /api/* (zero browser proxies)
        self.assertIn("/api/music/submit", html)
        self.assertIn("/api/music/query", html)
        self.assertIn("/api/voice/tts", html)
        self.assertIn("/api/models/qwen/stream", html)
        self.assertIn("/api/models/image/generate", html)
        self.assertIn("/api/models/image/edit", html)
        self.assertIn("/api/models/video/submit", html)
        self.assertIn("music_gen", html)
        self.assertIn("liveCallBtn", html)

    def test_zero_proxies_mobile_responsiveness_and_speaking_mode_addbubble_fix(self) -> None:
        html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")

        # 1) Zero external proxies anywhere in static/index.html (especially AnesKAM's proxy)
        for banned in (
            "aneskam",
            "allorigins",
            "codetabs",
            "thingproxy",
            "r.jina.ai",
            "viaProxy",
            "VIDEO_PROXY",
            "fetchCors",
            "hf.space",
        ):
            self.assertNotIn(banned.lower(), html.lower(), f"Banned proxy/direct-space reference remained: {banned}")

        # 2) Speaking Mode addBubble ReferenceError fixed (uses addAiStatic + global addBubble fallback)
        self.assertIn("function addBubble(role, text, media)", html)
        self.assertIn("const b = addAiStatic(finalSpoken, [], false);", html)
        self.assertNotIn("addBubble('assistant'", html)

        # 3) Universal phone screen compatibility (320px -> 760px, safe areas, 100dvh, landscape, 16px mobile inputs)
        #    AND desktop zoom safety (100dvh/100vw on .view MUST stay strictly inside @media (max-width:760px))
        self.assertIn("100dvh", html)
        self.assertIn("env(safe-area-inset-top", html)
        self.assertIn("env(safe-area-inset-bottom", html)
        self.assertIn("@media (max-width:430px)", html)
        self.assertIn("@media (max-width:360px)", html)
        self.assertIn("@media (max-height:560px) and (max-width:760px)", html)
        self.assertIn("font-size:16px !important", html)
        style_only = html.split("<style>", 1)[1].split("</style>", 1)[0]
        before_mobile_block = style_only.split("@media (max-width:760px){", 1)[0]
        self.assertNotIn("height:100dvh", before_mobile_block)

    def test_python_model_gateways_and_cors_headers(self) -> None:
        fake_png = b"\x89PNG\r\n\x1a\nfakepngbytes" * 80
        fake_mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"videodata" * 120)

        class _FakeModelsAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aclose(self):
                return None

            async def get(self, url, **kwargs):
                params = kwargs.get("params") or {}
                if url.endswith("/info"):
                    return httpx.Response(
                        200,
                        json={
                            "named_endpoints": {
                                "/chat_predict": {"parameters": []},
                                "/text_to_video": {
                                    "parameters": [
                                        {"parameter_name": "prompt", "parameter_default": ""},
                                        {"parameter_name": "negative_prompt", "parameter_default": ""},
                                        {"parameter_name": "height_ui", "parameter_default": 512},
                                        {"parameter_name": "width_ui", "parameter_default": 896},
                                        {"parameter_name": "mode", "parameter_default": "text-to-video"},
                                        {"parameter_name": "duration_ui", "parameter_default": 3.5},
                                    ]
                                },
                            }
                        },
                    )
                if "/file=" in url or url.endswith(".mp4"):
                    return httpx.Response(200, content=fake_mp4, headers={"content-type": "video/mp4"})
                if "/ai/aiappgen" in url or "/ai/nano-banana" in url:
                    return httpx.Response(200, content=fake_png, headers={"content-type": "image/png"})
                if params.get("action") == "status":
                    return httpx.Response(200, json={"status": "SUCCEEDED", "video_url": "https://cdn.example.com/out.mp4"})
                if "html.duckduckgo.com" in url:
                    return httpx.Response(
                        200,
                        text='<a class="result__a" href="https://example.com/doc">Example Doc</a><a class="result__snippet">Snippet body</a>',
                        headers={"content-type": "text/html"},
                    )
                return httpx.Response(404, text="not found")

            async def post(self, url, **kwargs):
                if "/call/chat_predict" in url:
                    return httpx.Response(200, json={"event_id": "evt-qwen-1"})
                if "/call/text_to_video" in url:
                    return httpx.Response(200, json={"event_id": "evt-vid-1"})
                return httpx.Response(404, text="not found")

            def stream(self, method, url, **kwargs):
                is_vid = "/call/text_to_video/" in url

                class _FakeStream:
                    status_code = 200

                    async def __aenter__(self_inner):
                        return self_inner

                    async def __aexit__(self_inner, exc_type, exc, tb):
                        return False

                    async def aiter_lines(self_inner):
                        if is_vid:
                            yield "event: complete"
                            yield 'data: [{"video": {"path": "/tmp/gradio/out.mp4"}}, 42]'
                            yield ""
                        else:
                            yield "event: complete"
                            yield 'data: [null, null, null, null, [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "مرحباً من بايثون مباشرة"}]]'
                            yield ""

                    async def aiter_bytes(self_inner):
                        yield (
                            b"event: complete\n"
                            b'data: [null, null, null, null, [{"role": "assistant", "content": "streamed via python"}]]\n\n'
                        )

                return _FakeStream()

        cross_origin_headers = {
            "Origin": "https://mobile-client.example.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        }
        with patch("app.main.httpx.AsyncClient", _FakeModelsAsyncClient):
            # 1) Preflight OPTIONS returns CORS headers
            opt = self.client.options("/api/models/qwen/once", headers=cross_origin_headers)
            self.assertEqual(opt.status_code, 200)
            self.assertEqual(
                opt.headers.get("access-control-allow-origin"),
                "https://mobile-client.example.org",
            )

            # 2) Cross-origin POST on Qwen once works without 403 CSRF block and carries CORS header
            r_once = self.client.post(
                "/api/models/qwen/once",
                headers={"Origin": "https://mobile-client.example.org"},
                json={"data": ["hello", None, None, None, [], "sys", 0.4, 0.8, 20], "endpoint": "chat_predict"},
            )
            self.assertEqual(r_once.status_code, 200, r_once.text)
            self.assertEqual(r_once.json()["text"], "مرحباً من بايثون مباشرة")
            self.assertEqual(
                r_once.headers.get("access-control-allow-origin"),
                "https://mobile-client.example.org",
            )

            # 3) Qwen SSE stream via Python
            r_stream = self.client.post(
                "/api/models/qwen/stream",
                headers={"Origin": "https://mobile-client.example.org"},
                json={"data": ["hi", None, None, None, [], "sys", 0.4, 0.8, 20], "endpoint": "chat_predict"},
            )
            self.assertEqual(r_stream.status_code, 200)
            self.assertIn("streamed via python", r_stream.text)

            # 4) Image generate & edit via Python
            r_img = self.client.post(
                "/api/models/image/generate",
                headers={"Origin": "https://mobile-client.example.org"},
                json={"prompt": "minimalist oasis at dusk", "width": 1024, "height": 1024, "seed": 42},
            )
            self.assertEqual(r_img.status_code, 200)
            self.assertEqual(r_img.content, fake_png)

            # 5) Async Video submit, status polling, and Range-capable MP4 streaming via Python (zero 502 timeouts)
            r_vid_sub = self.client.post(
                "/api/models/video/submit",
                headers={"Origin": "https://mobile-client.example.org"},
                json={"prompt": "brave rabbit on mossy oak branch", "ratio": "16:9", "sound": True},
            )
            self.assertEqual(r_vid_sub.status_code, 200)
            vid_tid = r_vid_sub.json()["task_id"]
            self.assertTrue(vid_tid.startswith("vid_"))

            vid_done = None
            for _ in range(30):
                r_vid_st = self.client.post(
                    "/api/models/video/status",
                    headers={"Origin": "https://mobile-client.example.org"},
                    json={"task_id": vid_tid},
                )
                self.assertEqual(r_vid_st.status_code, 200)
                if r_vid_st.json().get("status") == "SUCCEEDED":
                    vid_done = r_vid_st.json()
                    break
                time.sleep(0.05)
            self.assertIsNotNone(vid_done, "Background video task did not reach SUCCEEDED")
            self.assertEqual(vid_done["video_url"], f"/api/models/video/file/{vid_tid}")

            r_mp4_full = self.client.get(vid_done["video_url"])
            self.assertEqual(r_mp4_full.status_code, 200)
            self.assertEqual(r_mp4_full.content, fake_mp4)
            self.assertIn("video/mp4", r_mp4_full.headers.get("content-type", ""))

            r_mp4_range = self.client.get(vid_done["video_url"], headers={"Range": "bytes=0-31"})
            self.assertEqual(r_mp4_range.status_code, 206)
            self.assertEqual(r_mp4_range.content, fake_mp4[:32])

            # 6) Web search & SSRF-guarded read-url via Python
            r_ws = self.client.post(
                "/api/tools/web-search",
                headers={"Origin": "https://mobile-client.example.org"},
                json={"query": "Genisi Atlas", "count": 3},
            )
            self.assertEqual(r_ws.status_code, 200)
            self.assertTrue(r_ws.json()["results"])

            r_ssrf = self.client.post(
                "/api/tools/read-url",
                headers={"Origin": "https://mobile-client.example.org"},
                json={"url": "https://127.0.0.1/admin"},
            )
            self.assertEqual(r_ssrf.status_code, 400)


if __name__ == "__main__":
    unittest.main()
