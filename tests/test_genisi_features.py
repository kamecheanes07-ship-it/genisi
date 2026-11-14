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
        fake_wav = b"RIFF$\x01\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x01\x00\x00" + (b"\x00\x10" * 160)
        real_async_client = httpx.AsyncClient
        self.main_mod._voxcpm_meta_cache.clear()

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

    def test_voice_tts_mp3_anchor_sse_401_retry_and_qwen3_tts_failover(self) -> None:
        """Ensure /api/voice/tts never 502s on MP3 reference anchors, dataset-only HF_TOKEN 401s, or VoxCPM busy state."""
        import base64 as b64_mod

        real_async_client = httpx.AsyncClient
        fake_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x10" + (b"\xff\xfb\x90\x64" * 280)
        fake_wav = b"RIFF$\x01\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x01\x00\x00" + (b"\x02\x08" * 160)
        uploaded_raw_parts: list[bytes] = []
        vox_calls: list[list] = []
        qwen_vd_calls: list[list] = []
        mode = {"stage": "ref_reject_then_design_ok"}

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            auth_hdr = req.headers.get("authorization", "")

            if "openbmb-voxcpm-demo.hf.space" in url:
                if mode["stage"] == "vox_busy":
                    return httpx.Response(429, text="BackendBusyError")
                # Simulate dataset-scoped HF_TOKEN rejected with 401 on public Space endpoints
                if auth_hdr:
                    return httpx.Response(401, json={"error": "Invalid credentials in Authorization header"})
                if url.endswith("/gradio_api/upload") and req.method == "POST":
                    uploaded_raw_parts.append(req.read())
                    return httpx.Response(200, json=["/tmp/gradio/voice-anchor.mp3"])
                if url.endswith("/gradio_api/call/generate") and req.method == "POST":
                    body = json.loads(req.content.decode("utf-8"))
                    vox_calls.append(body["data"])
                    ev_id = "evt_ref_bad" if body["data"][2] is not None else "evt_design_ok"
                    return httpx.Response(200, json={"event_id": ev_id})
                if url.endswith("/gradio_api/call/generate/evt_ref_bad"):
                    sse_err = b'event: error\ndata: "Reference audio must be between 1 and 30 seconds"\n\n'
                    return httpx.Response(200, content=sse_err, headers={"Content-Type": "text/event-stream"})
                if url.endswith("/gradio_api/call/generate/evt_design_ok"):
                    sse_ok = (
                        'event: complete\ndata: [{"path":"/tmp/gradio/out.mp3","url":"https://openbmb-voxcpm-demo.hf.space/gradio_api/file=/tmp/gradio/out.mp3"}]\n\n'
                    ).encode("utf-8")
                    return httpx.Response(200, content=sse_ok, headers={"Content-Type": "text/event-stream"})
                if "file=/tmp/gradio/out.mp3" in url:
                    return httpx.Response(200, content=fake_mp3, headers={"Content-Type": "application/octet-stream"})

            if "qwen-qwen3-tts-voice-design.hf.space" in url:
                if auth_hdr:
                    return httpx.Response(401, json={"error": "Invalid token"})
                if url.endswith("/gradio_api/call/tts_interface") and req.method == "POST":
                    body = json.loads(req.content.decode("utf-8"))
                    qwen_vd_calls.append(body["data"])
                    return httpx.Response(200, json={"event_id": "evt_qwen_vd_1"})
                if url.endswith("/gradio_api/call/tts_interface/evt_qwen_vd_1"):
                    sse_ok = (
                        'event: complete\ndata: [{"path":"/tmp/gradio/qwen_vd.wav","url":"https://qwen-qwen3-tts-voice-design.hf.space/gradio_api/file=/tmp/gradio/qwen_vd.wav"}]\n\n'
                    ).encode("utf-8")
                    return httpx.Response(200, content=sse_ok, headers={"Content-Type": "text/event-stream"})
                if "file=/tmp/gradio/qwen_vd.wav" in url:
                    return httpx.Response(200, content=fake_wav, headers={"Content-Type": "audio/wav"})

            raise AssertionError(f"Unexpected TTS URL: {url}")

        transport = httpx.MockTransport(handler)
        self.main_mod._voxcpm_meta_cache.clear()
        self.main_mod._qwen_meta_cache.clear()
        with (
            patch.dict(os.environ, {"HF_TOKEN": "hf_dataset_only_token_xyz"}, clear=False),
            patch.object(
                self.main_mod.httpx,
                "AsyncClient",
                side_effect=lambda **kw: real_async_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
            ),
        ):
            # Case A: MP3 anchor uploaded as voice-anchor.mp3 (never mislabeled .wav), 401 retried, ref error retried in Voice Design mode
            res1 = self.client.post(
                "/api/voice/tts",
                json={
                    "text": "مرحباً بك في جينيسي، الصوت يعمل الآن بدون أي خطأ 502.",
                    "control_instruction": "Warm young male voice, calm natural pace",
                    "reference_audio_base64": b64_mod.b64encode(fake_mp3).decode("ascii"),
                },
            )
            self.assertEqual(res1.status_code, 200, res1.text)
            self.assertEqual(res1.content, fake_mp3)
            self.assertEqual(res1.headers.get("content-type"), "audio/mpeg")
            self.assertTrue(uploaded_raw_parts and b"voice-anchor.mp3" in uploaded_raw_parts[0])
            self.assertNotIn(b"voice-anchor.wav", uploaded_raw_parts[0])
            self.assertEqual(len(vox_calls), 2, "Must retry without reference_audio when cloning clip is rejected")
            self.assertIsNone(vox_calls[1][2])

            # Case B: VoxCPM busy (429) -> seamless failover to official Qwen3-TTS-Voice-Design
            mode["stage"] = "vox_busy"
            self.main_mod._voxcpm_meta_cache.clear()
            res2 = self.client.post(
                "/api/voice/tts",
                json={
                    "text": "هذا رد صوتي احتياطي فوري عبر محرك Qwen3 TTS الرسمي.",
                    "control_instruction": "Clear warm female conversational voice",
                },
            )
            self.assertEqual(res2.status_code, 200, res2.text)
            self.assertEqual(res2.content, fake_wav)
            self.assertIn("audio/wav", res2.headers.get("content-type", ""))
            self.assertEqual(len(qwen_vd_calls), 1)
            self.assertEqual(qwen_vd_calls[0][1], "Clear warm female conversational voice")

    def test_frontend_ui_specs_image_square_and_voice_pill_bar(self) -> None:
        html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")

        # 1) Animated moving dot-matrix waiting cards (image-4.png) for image, video, and music
        self.assertIn(".waitcard.img-square", html)
        self.assertIn(".waitcard.dm-card", html)
        self.assertIn("function startDotMatrixCanvas(card, kind)", html)
        self.assertIn("dm-canvas", html)
        self.assertIn("vid-wait", html)
        self.assertIn("music-wait", html)
        self.assertRegex(html, r"aspect-ratio:\s*371\s*/\s*364")
        self.assertRegex(html, r"border-radius:\s*28px")

        # 2) Enlarged main capsule composer (wider 880px + taller 82px pill style) + Speaking Mode pill bar (660x64px)
        self.assertIn('id="viewVoice"', html)
        self.assertRegex(html, r"\.voice-pill-bar\s*\{[^}]*width:\s*min\(100%,\s*660px\)[^}]*height:\s*64px[^}]*border-radius:\s*999px")
        self.assertRegex(html, r"\.composer\s*\{[^}]*width:\s*min\(100%,\s*880px\)[^}]*min-height:\s*82px[^}]*border-radius:\s*999px")
        self.assertIn("comp-pill-row", html)

        # 3) Live Voice Call UI (image-5.png: bottom glowing blue aurora canvas + Hold || & red End X buttons)
        self.assertIn('id="voiceAuroraCanvas"', html)
        self.assertIn("function startVoiceAuroraCanvas()", html)
        self.assertIn('id="voiceHoldBtn"', html)
        self.assertIn("function toggleVoiceHold()", html)
        self.assertIn('id="voiceCloseBtn"', html)

        # 4) Redesigned Studio Audio/Music player (.aud-studio) + Branded Word / Excel / PowerPoint cards (.doc-studio-card)
        self.assertIn("function buildStudioAudioCard(", html)
        self.assertIn("aud-studio", html)
        self.assertIn("aud-wave", html)
        self.assertIn("function buildStudioDocCard(", html)
        self.assertIn("doc-studio-card", html)
        self.assertIn('data-doc-kind="word"', html)
        self.assertIn('data-doc-kind="excel"', html)
        self.assertIn('data-doc-kind="ppt"', html)

        # 5) StepAudio-3-Music & VoxCPM-Demo & Qwen/Image/Video wired strictly via Python /api/* (zero browser proxies)
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
                    return httpx.Response(200, json={"named_endpoints": {"/chat_predict": {"parameters": []}}})
                if "Txt2video" in url and params.get("action") == "generate":
                    return httpx.Response(
                        200,
                        json={
                            "statusCode": 200,
                            "success": True,
                            "source": "Omegatech",
                            "data": {
                                "prompt": params.get("prompt"),
                                "ratio": params.get("ratio"),
                                "sound": params.get("sound") == "true",
                                "videoUrl": "https://sora.aritek.app/generated/t2vStS51220f3jsmazg11rmt0cv76mreem7fc-ft3.1.0.mp4",
                            },
                            "timestamp": "2026-11-14T17:55:53.645Z",
                            "attribution": "@Omegatech-01",
                        },
                    )
                if "sora.aritek.app/generated/" in url or url.endswith(".mp4"):
                    return httpx.Response(200, content=fake_mp4, headers={"content-type": "video/mp4"})
                if "/ai/aiappgen" in url or "/ai/nano-banana" in url:
                    return httpx.Response(200, content=fake_png, headers={"content-type": "image/png"})
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
                return httpx.Response(404, text="not found")

            def stream(self, method, url, **kwargs):
                class _FakeStream:
                    status_code = 200

                    async def __aenter__(self_inner):
                        return self_inner

                    async def __aexit__(self_inner, exc_type, exc, tb):
                        return False

                    async def aiter_lines(self_inner):
                        yield "event: complete"
                        yield 'data: [null, null, null, null, [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "مرحباً من بايثون مباشرة"}]]'
                        yield ""

                    async def aiter_bytes(self_inner):
                        yield (
                            "event: complete\n"
                            'data: [null, null, null, null, [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "مرحباً من بايثون مباشرة"}]]\n\n'
                        ).encode("utf-8")

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
            self.assertIn("مرحباً من بايثون مباشرة", r_stream.text)

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

    def test_qwen_single_space_success_and_no_failover(self) -> None:
        real_client = httpx.AsyncClient
        requests = []
        mode = {"fail": False}
        self.main_mod._qwen_meta_cache.clear()

        def handler(req):
            requests.append(req)
            self.assertEqual(req.url.host, "qwen-qwen3-5-omni-offline-demo.hf.space")
            if mode["fail"]:
                return httpx.Response(429, text="busy")
            if req.method == "POST" and req.url.path.endswith("/upload"):
                return httpx.Response(200, json=["/tmp/gradio/image.png"])
            if req.method == "POST":
                args = json.loads(req.content)["data"]
                self.assertEqual(len(args), 9)
                self.assertEqual(args[0], "مرحبا")
                return httpx.Response(200, json={"event_id": "evt1"})
            frame = json.dumps([None, None, None, None, [{"role": "user", "content": "مرحبا"}, {"role": "assistant", "content": "أهلاً"}]], ensure_ascii=False)
            return httpx.Response(200, text=f"event: generating\ndata: {frame}\n\nevent: complete\ndata: {frame}\n\n")

        transport = httpx.MockTransport(handler)
        with patch.dict(os.environ, {"HF_TOKEN": "hf_test_token_123456", "DASHSCOPE_API_KEY": "ignored", "QWEN_EXTRA_SPACE_URLS": "https://example.com"}), patch.object(self.main_mod.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=transport, **kw)):
            payload = {"data": ["مرحبا", None, None, None, [], "system", 0.5, 0.8, 20]}
            info = self.client.get("/api/models/qwen/info").json()
            self.assertEqual(info["space"], "https://qwen-qwen3-5-omni-offline-demo.hf.space")
            self.assertEqual(self.client.post("/api/models/qwen/once", json=payload).json()["text"], "أهلاً")
            self.assertIn("أهلاً", self.client.post("/api/models/qwen/stream", json=payload).text)
            upload = self.client.post("/api/models/qwen/upload?name=image.png&mime=image/png", content=b"image")
            self.assertEqual(upload.status_code, 200)
            self.assertEqual(upload.json()["path"], "/tmp/gradio/image.png")
            self.assertTrue(any(r.headers.get("authorization") == "Bearer hf_test_token_123456" for r in requests))
            mode["fail"] = True
            requests.clear()
            self.assertEqual(self.client.post("/api/models/qwen/once", json=payload).status_code, 502)
            self.assertIn("event: error", self.client.post("/api/models/qwen/stream", json=payload).text)
            self.assertEqual(self.client.post("/api/models/qwen/upload?name=image.png", content=b"image").status_code, 502)
            self.assertTrue(requests)
            self.assertEqual(self.client.post("/api/models/qwen/once", json={**payload, "endpoint": "other"}).status_code, 422)

    def test_qwen_partial_stream_is_reported_as_failure(self) -> None:
        real_client = httpx.AsyncClient
        frame = json.dumps([None, None, None, None, [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "رد غير مكتمل"}
        ]], ensure_ascii=False)

        def handler(req):
            self.assertEqual(req.url.host, "qwen-qwen3-5-omni-offline-demo.hf.space")
            if req.method == "POST":
                return httpx.Response(200, json={"event_id": "partial"})
            return httpx.Response(200, text=f"event: generating\ndata: {frame}\n\n")

        transport = httpx.MockTransport(handler)
        with patch.object(self.main_mod.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=transport, **kw)):
            payload = {"data": ["hi", None, None, None, [], "", 0.7, 0.8, 20]}
            stream = self.client.post("/api/models/qwen/stream", json=payload)
            self.assertIn("رد غير مكتمل", stream.text)
            self.assertIn("event: error", stream.text)
            self.assertNotIn("event: complete", stream.text)
            self.assertEqual(self.client.post("/api/models/qwen/once", json=payload).status_code, 502)
            for path in ("/api/models/qwen/once", "/api/models/qwen/stream"):
                self.assertEqual(self.client.post(path, json={"data": ["too short"]}).status_code, 422)

    def test_qwen_empty_complete_uses_latest_generating_frame(self) -> None:
        real_client = httpx.AsyncClient
        frame = json.dumps([None, None, None, None, [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "جواب كامل"}
        ]], ensure_ascii=False)

        def handler(req):
            if req.method == "POST":
                return httpx.Response(200, json={"event_id": "empty-complete"})
            return httpx.Response(200, text=f"event: generating\ndata: {frame}\n\nevent: complete\ndata: null\n\n")

        transport = httpx.MockTransport(handler)
        with patch.object(self.main_mod.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=transport, **kw)):
            payload = {"data": ["hi", None, None, None, [], "", 0.7, 0.8, 20]}
            stream = self.client.post("/api/models/qwen/stream", json=payload)
            self.assertIn("event: complete\ndata: [", stream.text)
            self.assertIn("جواب كامل", stream.text)
            self.assertEqual(self.client.post("/api/models/qwen/once", json=payload).json()["text"], "جواب كامل")

    def test_qwen_empty_complete_suppression_and_split_arabic_utf8_chunks(self) -> None:
        """Ensure empty upstream `event: complete` never aborts browser stream and split 2-byte Arabic UTF-8 never corrupts."""
        self.main_mod._qwen_meta_cache.clear()
        arabic_reply = "أهلاً وسهلاً بك! محرك Qwen3.5-Omni-Offline-Demo يعمل عبر البايثون بكفاءة تامة."
        full_sse_bytes = (
            "event: generating\n"
            + "data: "
            + json.dumps(
                [None, None, None, None, [{"role": "user", "content": "اختبار"}, {"role": "assistant", "content": arabic_reply}]],
                ensure_ascii=False,
            )
            + "\n\nevent: complete\n"
            + "data: "
            + json.dumps(
                [None, None, None, None, [{"role": "user", "content": "اختبار"}, {"role": "assistant", "content": arabic_reply}]],
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode("utf-8")

        # Find an exact index inside full_sse_bytes where a 2-byte UTF-8 Arabic lead byte (0xD8/0xD9) is split from its continuation byte
        split_idx = next(
            i for i in range(1, len(full_sse_bytes))
            if full_sse_bytes[i - 1] in (0xD8, 0xD9) and (0x80 <= full_sse_bytes[i] <= 0xBF)
        )
        chunk_a, chunk_b = full_sse_bytes[:split_idx], full_sse_bytes[split_idx:]

        mode = {"empty": True}

        class _SplitAndEmptyCompleteClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url, **kwargs):
                return httpx.Response(200, json={"named_endpoints": {"/chat_predict": {"parameters": []}}})

            async def post(self, url, **kwargs):
                if "qwen-qwen3-5-omni-offline-demo.hf.space" in url:
                    return httpx.Response(200, json={"event_id": "evt_empty_1" if mode["empty"] else "evt_split_2"})
                return httpx.Response(404, text="not found")

            def stream(self, method, url, **kwargs):
                is_empty_mirror = "evt_empty_1" in url

                class _StreamCtx:
                    status_code = 200

                    async def __aenter__(self_inner):
                        return self_inner

                    async def __aexit__(self_inner, exc_type, exc, tb):
                        return False

                    async def aiter_bytes(self_inner):
                        if is_empty_mirror:
                            # Exact bug from Qwen3.5-Omni-Offline-Demo app.py when predict() yields 0 chunks:
                            # initial user-only frame followed by `event: complete` with empty assistant string ""
                            yield (
                                b'event: generating\ndata: [null, null, null, null, [{"role": "user", "content": "hi"}]]\n\n'
                                b'event: complete\ndata: [null, null, null, null, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}]]\n\n'
                            )
                        else:
                            yield chunk_a
                            yield chunk_b

                return _StreamCtx()

        with patch("app.main.httpx.AsyncClient", _SplitAndEmptyCompleteClient):
            res = self.client.post(
                "/api/models/qwen/stream",
                json={"data": ["اختبار", None, None, None, [], "sys", 0.6, 0.8, 20], "endpoint": "chat_predict"},
            )
            self.assertEqual(res.status_code, 200)
            self.assertIn("event: error", res.text)
            self.assertNotIn("event: complete", res.text)
            mode["empty"] = False
            res = self.client.post(
                "/api/models/qwen/stream",
                json={"data": ["اختبار", None, None, None, [], "sys", 0.6, 0.8, 20], "endpoint": "chat_predict"},
            )
            body_text = res.text
            # Must NOT contain corrupted replacement character U+FFFD
            self.assertNotIn("\ufffd", body_text)
            # Must contain the intact Arabic text from the Space
            self.assertIn(arabic_reply, body_text)
            # Must contain ONLY ONE `event: complete` (the empty one from mirror 1 was suppressed!)
            self.assertEqual(body_text.count("event: complete"), 1)


if __name__ == "__main__":
    unittest.main()
