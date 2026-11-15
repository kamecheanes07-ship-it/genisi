---
title: AnesNT Genisi 4 Atlas
emoji: 🧠
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 8000
pinned: false
---

# Genisi 4 Atlas — Portable Secure Release 58.65.6 — Windows 12 Embedded System Prompt

Portable Docker/FastAPI application. Hugging Face Spaces is optional as a host. The application uses Hugging Face Hub Dataset `AnesKAM/Genisi-users` for persistent encrypted user data, uses the public `Qwen/Qwen3.5-Omni-Offline-Demo` Space for Atlas, and optionally uses an experimental Z.AI chat web session for Carbon.

Carbon uses the unofficial, experimental `chat.z.ai` web protocol, not the developer API. Set `ZAI_SESSION_TOKEN` to a fresh chat-session JWT in server-side hosting secrets (never commit it, send it in chat, or expose it to the browser). `ZAI_WEB_VERSION` optionally overrides the default web client version `prod-fe-1.1.123`. The model is the web identifier `x-preview-l` (reported by an unofficial bridge to correspond to GLM-5.3-Flash); its availability is not guaranteed. Requests enable thinking and stream reasoning and answers. One image is uploaded to Z.AI, and supported documents are extracted to text locally; audio/video are not supported. All Carbon users share the configured Z.AI account and its quota. The web endpoint may change, require CAPTCHA or reject requests; complete any verification on Z.AI itself, and errors are reported rather than bypassed. No unlimited use is promised. This integration has only mock tests, not a live session test. The web protocol sends the session token in the upstream URL; keep proxy/access logs and HTTP debug logging disabled or redacted. Revoke an exposed session and replace it in hosting secrets.

See [README-AR.md](README-AR.md) for deployment on Render, Railway, VPS, or any Docker host.

**Never commit `HF_TOKEN`, `SESSION_SECRET`, `WORKSPACE_HASH_SECRET`, provider keys, or `.env` files.** Configure them in the hosting provider's server-side secrets/environment settings. GitHub Pages alone cannot run this backend.


## Built-in Windows 12 System Prompt

The complete self-contained Windows 12 concept-desktop specification is embedded in `app/windows12_prompt.py`. The complete prompt is embedded directly into `static/index.html` for the active Atlas browser router and into the backend agent module. On a Windows 12 request or contextual follow-up, the router adds it to every stateless model call in that task. No prompt-fetch request is needed. The backend agent also appends the same prompt directly to its system messages. No HTML reference, Dataset record, screenshot, or user attachment is required. Unrelated requests do not receive the specialized prompt.

The prompt defines the visual system, wallpaper and desktop shell, lock screen, taskbar, Start menu, Quick Settings, complete window manager, eleven desktop applications, accessibility and theme behavior, implementation rules, build sequence, acceptance checklist, and delivery requirements.

Optional status and prompt endpoints are available at `GET /api/windows12/prompt/status` and `GET /api/windows12/prompt`; neither endpoint is needed by the browser router, and neither reads a reference file or Dataset record.

The concept is a web-based Windows 12-inspired simulation, not an official Microsoft operating system.

Release 58.65.6: Self-contained Windows 12 design System Prompt embedded in the Genisi application; no external reference file is needed.
