from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unittest
from pathlib import Path
from fastapi import Response

from app.agent import SYSTEM_PROMPT, _build_messages, needs_windows12_prompt
from app.main import app, get_windows12_system_context, windows12_system_prompt, windows12_prompt_status
from app.windows12_prompt import (
    WINDOWS12_PROMPT_SHA256,
    WINDOWS12_PROMPT_VERSION,
    WINDOWS12_SYSTEM_PROMPT,
    windows12_prompt_metadata,
)


ROOT = Path(__file__).resolve().parents[1]


class Windows12EmbeddedPromptTests(unittest.TestCase):
    def test_detects_arabic_and_english_windows12_requests(self) -> None:
        self.assertTrue(needs_windows12_prompt("صمم واجهة ويندوز 12"))
        self.assertTrue(needs_windows12_prompt("Build a Windows12 desktop"))
        self.assertTrue(needs_windows12_prompt("Design a Win-12 concept"))
        self.assertFalse(needs_windows12_prompt("صمم صفحة تسجيل دخول"))

    def test_detects_followups_in_a_windows12_conversation(self) -> None:
        history = [{"role": "user", "content": "صمم واجهة Windows 12"}]
        self.assertTrue(needs_windows12_prompt("غيّر شريط المهام", history))
        self.assertTrue(needs_windows12_prompt("Add dark mode", history))
        self.assertTrue(needs_windows12_prompt("صغّر الشريط", history))
        chained_history = history + [
            {"role": "assistant", "content": "تم تعديل الشريط."},
            {"role": "user", "content": "صغّر الشريط"},
        ]
        self.assertTrue(needs_windows12_prompt("اجعل الخلفية زرقاء", chained_history))
        self.assertFalse(needs_windows12_prompt("ما حالة الطقس اليوم؟", history))

    def test_backend_agent_injects_full_prompt_only_for_windows12(self) -> None:
        windows_messages = _build_messages("صمم واجهة ويندوز 12", [])
        self.assertEqual(windows_messages[0]["role"], "system")
        self.assertEqual(windows_messages[1]["role"], "system")
        self.assertEqual(windows_messages[1]["content"], WINDOWS12_SYSTEM_PROMPT)
        self.assertEqual(windows_messages[-1]["content"], "صمم واجهة ويندوز 12")

        ordinary_messages = _build_messages("اشرح لي الفرق بين HTTP وHTTPS", [])
        self.assertEqual(len(ordinary_messages), 2)
        self.assertNotIn("WINDOWS 12 CONCEPT DESKTOP SYSTEM PROMPT", ordinary_messages[0]["content"])

    def test_backend_followup_reinjects_prompt(self) -> None:
        history = [
            {"role": "user", "content": "Build a Windows 12 desktop"},
            {"role": "assistant", "content": "I created the desktop shell."},
        ]
        messages = _build_messages("Add dark mode", history)
        self.assertEqual(messages[1]["content"], WINDOWS12_SYSTEM_PROMPT)

    def test_prompt_is_self_contained_and_contains_the_required_design_spec(self) -> None:
        self.assertIn("Do not require, request, search for, or wait for a separate reference HTML file", WINDOWS12_SYSTEM_PROMPT)
        self.assertIn("## 4. LOCK SCREEN", WINDOWS12_SYSTEM_PROMPT)
        self.assertIn("## 5. TASKBAR AND SYSTEM TRAY", WINDOWS12_SYSTEM_PROMPT)
        self.assertIn("## 6. START MENU", WINDOWS12_SYSTEM_PROMPT)
        self.assertIn("## 7. QUICK SETTINGS FLYOUT", WINDOWS12_SYSTEM_PROMPT)
        self.assertIn("## 8. WINDOW MANAGER", WINDOWS12_SYSTEM_PROMPT)
        self.assertIn("## 9. APPLICATION SPECIFICATIONS", WINDOWS12_SYSTEM_PROMPT)
        for app_name in ("File Explorer", "Settings", "Office 365", "Paint Studio", "Calculator", "Media Player"):
            self.assertIn(app_name, WINDOWS12_SYSTEM_PROMPT)
        self.assertIn("## 13. ACCEPTANCE CHECKLIST", WINDOWS12_SYSTEM_PROMPT)

    def test_prompt_metadata_and_sha256_are_consistent(self) -> None:
        meta = windows12_prompt_metadata()
        digest = hashlib.sha256(WINDOWS12_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
        self.assertEqual(digest, WINDOWS12_PROMPT_SHA256)
        self.assertEqual(meta["sha256"], digest)
        self.assertEqual(meta["version"], WINDOWS12_PROMPT_VERSION)
        self.assertTrue(meta["embedded"])
        self.assertFalse(meta["attachment_required"])
        self.assertEqual(meta["chars"], len(WINDOWS12_SYSTEM_PROMPT))

    def test_api_exposes_embedded_prompt_routes(self) -> None:
        paths = {route.path for route in app.routes}
        self.assertIn("/api/windows12/prompt", paths)
        self.assertIn("/api/windows12/prompt/status", paths)
        self.assertIn("/api/reference/windows12/prompt", paths)  # compatibility alias
        context = get_windows12_system_context()
        self.assertTrue(context["embedded"])
        self.assertFalse(context["attachment_required"])
        self.assertEqual(context["system_prompt"], WINDOWS12_SYSTEM_PROMPT)

    def test_active_browser_router_embeds_and_activates_prompt_automatically(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("currentTurnNeedsWindows12Prompt(text)", html)
        self.assertIn("activateWindows12SystemPrompt(text);", html)
        self.assertIn("const WINDOWS12_ATLAS_SYSTEM_PROMPT =", html)
        self.assertIn("WINDOWS 12 BUILT-IN DESIGN MODE", html)
        self.assertIn("chat_predict is stateless: resend the full spec on every tool-loop call", html)
        self.assertIn("systemPrompt += '\\n\\n' + ai.windows12SystemPrompt", html)
        match = re.search(r"const WINDOWS12_ATLAS_SYSTEM_PROMPT = (\".*?\");\n", html, re.DOTALL)
        self.assertIsNotNone(match, "Full prompt must be compiled into the active Atlas frontend.")
        frontend_prompt = json.loads(match.group(1))
        self.assertEqual(frontend_prompt, WINDOWS12_SYSTEM_PROMPT)
        self.assertIn(f'const WINDOWS12_ATLAS_SYSTEM_PROMPT_SHA256 = "{WINDOWS12_PROMPT_SHA256}";', html)
        self.assertIn("windows12SystemPrompt: WINDOWS12_ATLAS_SYSTEM_PROMPT", html)
        self.assertNotIn("/api/windows12/prompt'", html)

    def test_no_runtime_dependency_on_windows12_html_or_dataset_reader(self) -> None:
        for name in ("app/main.py", "app/agent.py", "app/genisi_auth.py", "static/index.html", "Dockerfile"):
            content = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("codes/windows12.html", content, name)
            self.assertNotIn("read_windows12_reference", content, name)
        self.assertFalse((ROOT / "codes" / "windows12.html").exists())
        docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY codes/", docker)


    def test_prompt_api_returns_embedded_prompt_without_auth_or_file_io(self) -> None:
        response = Response()
        payload = asyncio.run(windows12_system_prompt(response))
        status = asyncio.run(windows12_prompt_status())
        self.assertEqual(payload["system_prompt"], WINDOWS12_SYSTEM_PROMPT)
        self.assertTrue(payload["embedded"])
        self.assertFalse(payload["attachment_required"])
        self.assertEqual(status["sha256"], payload["sha256"])
        self.assertEqual(response.headers["cache-control"], "public, max-age=3600")

    def test_readable_docs_match_embedded_prompt(self) -> None:
        docs = (ROOT / "docs" / "WINDOWS12_ATLAS_SYSTEM_PROMPT.md").read_text(encoding="utf-8").strip()
        self.assertEqual(docs, WINDOWS12_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
