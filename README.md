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

Portable Docker/FastAPI application. Hugging Face Spaces is optional as a host. The application uses Hugging Face Hub Dataset `AnesKAM/Genisi-users` for persistent encrypted user data, uses the public `Qwen/Qwen3.5-Omni-Offline-Demo` Space for Atlas, and uses the public Ornith Space for Carbon.

Carbon uses the public Ornith-1.5-122B Hugging Face Space for chat, not Z.AI or an API key in this app. Availability, quotas and API costs are controlled by the Space owner, who configures their own `ORNITH_API_KEY`. Genisi requests task-sensitive reasoning using the same text-based prompting as Atlas; this is best-effort, not a guaranteed native reasoning mode. The Space streams answer text but its published handler discards native reasoning fields and exposes no thinking switch. Text and locally extracted document content are sent to the Ornith Space. For a single image, Genisi sends it to the Qwen Omni Space for a question-focused description and visible-text transcription, then sends that description (not the image) to Ornith. The description is saved with chat history for follow-up questions (and synced if history sync is enabled). This is indirect vision and can miss details or misread text; it fails explicitly if Qwen is unavailable. Audio/video attachments remain unsupported in Carbon. Atlas stays on Qwen. Both Spaces are external services and may change, queue, rate-limit or be unavailable.

See [README-AR.md](README-AR.md) for deployment on Render, Railway, VPS, or any Docker host.

**Never commit `HF_TOKEN`, `SESSION_SECRET`, `WORKSPACE_HASH_SECRET`, provider keys, or `.env` files.** Configure them in the hosting provider's server-side secrets/environment settings. GitHub Pages alone cannot run this backend.


## Built-in Windows 12 System Prompt

The complete self-contained Windows 12 concept-desktop specification is embedded in `app/windows12_prompt.py`. The complete prompt is embedded directly into `static/index.html` for the active Atlas browser router and into the backend agent module. On a Windows 12 request or contextual follow-up, the router adds it to every stateless model call in that task. No prompt-fetch request is needed. The backend agent also appends the same prompt directly to its system messages. No HTML reference, Dataset record, screenshot, or user attachment is required. Unrelated requests do not receive the specialized prompt.

The prompt defines the visual system, wallpaper and desktop shell, lock screen, taskbar, Start menu, Quick Settings, complete window manager, eleven desktop applications, accessibility and theme behavior, implementation rules, build sequence, acceptance checklist, and delivery requirements.

Optional status and prompt endpoints are available at `GET /api/windows12/prompt/status` and `GET /api/windows12/prompt`; neither endpoint is needed by the browser router, and neither reads a reference file or Dataset record.

The concept is a web-based Windows 12-inspired simulation, not an official Microsoft operating system.

Release 58.65.6: Self-contained Windows 12 design System Prompt embedded in the Genisi application; no external reference file is needed.

Multiple images (up to the six attachment limit) are handled individually: the first is used directly by Atlas, while additional images are separately described and supplied as text for comparison; Carbon uses descriptions for all images. This is not joint image perception and may miss details, adds latency, and stops with an error if an additional image cannot be processed. Descriptions are stored in chat history for follow-up questions. The first Atlas image must be reattached for follow-ups requiring its original pixels.

### 3D Studio

Signed-in users can open **3D** from the composer and enter a short English object description. A third-party service generates Three.js code; Genisi renders it in a sandboxed, opaque-origin iframe with network connections blocked. The preview can be rotated and exported to GLB. It is a separate studio, not an automatic chat tool, and results are not saved to chat: download before closing. The experimental model has a limited vocabulary and may return invalid or inaccurate objects; the external service may be unavailable. Generated code can still consume browser resources despite the sandbox.

The offline viewer bundles Three.js 0.161.0 (license: `static/three-license.txt`). To rebuild `static/three-viewer.html` from `static/three-viewer-source.js`, install development-only `three@0.161.0` and `esbuild@0.25.5`, then run `node scripts/build-three-viewer.mjs`. No npm dependency is needed at runtime.
