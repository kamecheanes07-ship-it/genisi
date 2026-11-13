from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .windows12_prompt import WINDOWS12_SYSTEM_PROMPT, WINDOWS12_PROMPT_VERSION, windows12_prompt_metadata
from .genisi_auth import (
    MEDIA_TTL_DAYS,
    _origin_guard,
    _rate_limit,
    _sha,
    _client_ip,
    cleanup_all_expired_media,
    get_genisi_user_record,
    initialize_genisi_auth,
    require_genisi_csrf,
    router as genisi_auth_router,
)
from .worker_client import worker as WORKER

APP_NAME = "AnesNT Genisi"
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production").lower()
STEPAUDIO_MUSIC_SPACE_URL = os.getenv("STEPAUDIO_MUSIC_SPACE_URL", "https://stepfun-ai-stepaudio-3-music.hf.space").rstrip("/")
VOXCPM_SPACE_URL = os.getenv("VOXCPM_SPACE_URL", "https://openbmb-voxcpm-demo.hf.space").rstrip("/")
_music_schema_cache: dict[str, Any] = {"ts": 0.0, "submit_props": None}
_voxcpm_meta_cache: dict[str, Any] = {"ts": 0.0, "prefix": None}
_cleanup_task: asyncio.Task[Any] | None = None
AGENT = Agent()
app = FastAPI(title=APP_NAME, version="0.4.4-windows12-embedded-system-prompt", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")

class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=12000)


class ChatBody(BaseModel):
    project_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    message: str = Field(min_length=1, max_length=12000)
    history: list[HistoryItem] = Field(default_factory=list, max_length=20)


DEFAULT_PROJECT_ID = "00000000-0000-4000-8000-000000000001"


class TerminalBody(BaseModel):
    project_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    command: str = Field(min_length=1, max_length=8000)
    cwd: str = Field(default=".", max_length=300)


def current_user(request: Request) -> dict[str, Any]:
    """Resolve the same encrypted dataset-backed account used by the secure auth router."""
    return get_genisi_user_record(request)


def workspace_key(user_id: str, project_id: str) -> str:
    secret = os.getenv("WORKSPACE_HASH_SECRET", SESSION_SECRET).encode()
    return hmac.new(secret, f"{user_id}:{project_id}".encode(), hashlib.sha256).hexdigest()[:32]


def default_workspace_key(user_id: str) -> str:
    return workspace_key(user_id, DEFAULT_PROJECT_ID)


def sse_event(event: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


def get_windows12_system_context() -> dict[str, Any]:
    """Return the design specification embedded in the application source."""
    return {
        **windows12_prompt_metadata(),
        "system_prompt": WINDOWS12_SYSTEM_PROMPT,
    }


async def _periodic_media_cleanup_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            result = await asyncio.to_thread(cleanup_all_expired_media)
            if result.get("assets_deleted"):
                print(f"[Genisi] Purged {result['assets_deleted']} expired (>= {MEDIA_TTL_DAYS}d) media asset(s).")
        except Exception as exc:
            print(f"[Genisi] Media cleanup sweep skipped: {type(exc).__name__}")
        await asyncio.sleep(6 * 3600)


@app.on_event("startup")
async def startup() -> None:
    global _cleanup_task
    # Prevent same-UID child project processes from inspecting this API process's /proc environ.
    try:
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except Exception:
        pass

    env_mode = os.getenv("ENVIRONMENT", ENVIRONMENT).lower()
    sess_secret = os.getenv("SESSION_SECRET", SESSION_SECRET)
    if env_mode == "production" and (len(sess_secret) < 32 or sess_secret == "INSECURE-LOCAL-ONLY-CHANGE-ME"):
        raise RuntimeError("Set SESSION_SECRET to a stable random value of at least 32 characters in your hosting provider environment/secrets.")
    await asyncio.to_thread(initialize_genisi_auth)
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_periodic_media_cleanup_loop())
    # The Windows 12 design prompt is part of the application code. No Dataset,
    # HTML file, or user attachment is read to activate this design mode.
    prompt_info = windows12_prompt_metadata()
    print(
        "[Genisi] Built-in Windows 12 design prompt ready: "
        f"version={WINDOWS12_PROMPT_VERSION} chars={prompt_info['chars']} "
        f"sha256={prompt_info['sha256']}"
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "app": APP_NAME,
        "qwen_configured": bool(AGENT.base_url and AGENT.api_key),
        "sandbox_configured": WORKER.enabled and bool(os.getenv("SANDBOX_OWNER_EMAIL", "").strip()),
        "sandbox_mode": "local",
        "sandbox_note": "Local workspace runner; not a per-user Docker container.",
        "hosting_mode": "self-hosted-backend",
        "huggingface_dataset": os.getenv("GENISI_DATASET_ID", "AnesKAM/Genisi-users"),
        "media_ttl_days": MEDIA_TTL_DAYS,
        "voxcpm_space": VOXCPM_SPACE_URL,
        "stepaudio_music_space": STEPAUDIO_MUSIC_SPACE_URL,
        "windows12_prompt": windows12_prompt_metadata(),
    }


@app.get("/api/workspace")
async def get_workspace(request: Request) -> dict[str, Any]:
    user = current_user(request)
    ensure_sandbox_owner(user)
    if not WORKER.enabled:
        raise HTTPException(503, "Workspace runner is disabled. Set SANDBOX_MODE=local.")
    workspace_id = default_workspace_key(user["id"])
    try:
        result = await WORKER.request("GET", f"/v1/files/{workspace_id}", params={"path": "."})
        entries = []
        for item in result.get("items", []):
            if not isinstance(item, dict):
                continue
            rel = str(item.get("path", ""))
            entries.append({
                "type": item.get("type", "file"),
                "name": Path(rel).name,
                "path": rel,
                "size": item.get("size"),
            })
        return {"success": True, "entries": entries, "truncated": bool(result.get("truncated"))}
    except Exception as exc:
        raise HTTPException(502, f"تعذر قراءة مساحة العمل: {str(exc)[:300]}") from exc


@app.get("/api/workspace/file")
async def get_workspace_file(path: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    ensure_sandbox_owner(user)
    if not WORKER.enabled:
        raise HTTPException(503, "Workspace runner is disabled.")
    try:
        result = await WORKER.request("POST", "/v1/files/read", json={
            "workspace_id": default_workspace_key(user["id"]),
            "path": path,
        })
        return {"success": True, "path": result.get("path", path), "content": result.get("content", "")}
    except FileNotFoundError as exc:
        raise HTTPException(404, "الملف غير موجود") from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc)[:300]) from exc
    except Exception as exc:
        raise HTTPException(502, f"تعذر قراءة الملف: {str(exc)[:300]}") from exc


@app.get("/api/workspace/download")
async def download_workspace_file(path: str, request: Request) -> FileResponse:
    user = current_user(request)
    ensure_sandbox_owner(user)
    if not WORKER.enabled:
        raise HTTPException(503, "Workspace runner is disabled.")
    try:
        workspace = WORKER._workspace(default_workspace_key(user["id"]))
        target = WORKER._safe_target(workspace, path)
        if target.is_symlink() or not target.is_file():
            raise HTTPException(404, "الملف غير موجود")
        return FileResponse(
            path=str(target),
            filename=target.name,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    except HTTPException:
        raise
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc)[:300]) from exc
    except Exception as exc:
        raise HTTPException(404, "الملف غير موجود") from exc


@app.get("/api/agent/state")
async def agent_state(request: Request) -> dict[str, Any]:
    user = current_user(request)
    ensure_sandbox_owner(user)
    return {"plan": [], "notes": [], "sessions": 0, "updated": None}


@app.get("/api/windows12/prompt/status")
@app.get("/api/reference/windows12/status")
async def windows12_prompt_status() -> dict[str, Any]:
    """Report the built-in prompt status; no login or external file is needed."""
    return {"ok": True, **windows12_prompt_metadata()}


@app.get("/api/windows12/prompt")
@app.get("/api/reference/windows12/prompt")  # Backwards-compatible alias.
async def windows12_system_prompt(response: Response) -> dict[str, Any]:
    """Serve only the non-sensitive, application-embedded Windows 12 design prompt."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return {"ok": True, **windows12_prompt_metadata(), "system_prompt": WINDOWS12_SYSTEM_PROMPT}


@app.post("/api/chat/stream")
async def chat_stream(request: Request) -> StreamingResponse:
    # This endpoint matches the Genisi 58.65 browser SSE contract.
    await require_genisi_csrf(request)
    user = current_user(request)
    ensure_sandbox_owner(user)
    if not WORKER.enabled:
        raise HTTPException(503, "Local Genisi Sandbox is disabled. Set SANDBOX_MODE=local.")
    if not AGENT.base_url or not AGENT.api_key or not AGENT.model:
        raise HTTPException(503, "لم يتم إعداد مزود النموذج. اضبط HF_INFERENCE_MODEL أو QWEN_BASE_URL وQWEN_API_KEY.")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON request") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid request body")

    raw_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    clean_messages: list[dict[str, str]] = []
    for item in raw_messages[-20:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            clean_messages.append({"role": item["role"], "content": content[:12000]})

    message = str(payload.get("message") or "").strip()[:12000]
    if not message:
        for item in reversed(clean_messages):
            if item["role"] == "user":
                message = item["content"]
                break
    if not message:
        raise HTTPException(422, "اكتب رسالة قبل الإرسال")

    history = clean_messages[:]
    if history and history[-1]["role"] == "user" and history[-1]["content"].strip() == message:
        history.pop()

    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    if attachments:
        names = [str(a.get("name", "ملف"))[:100] for a in attachments[:8] if isinstance(a, dict)]
        if names:
            message += "\n\n[مرفقات من المستخدم: " + ", ".join(names) + ". قدّم توضيحاً إذا لم تكن محتوياتها متاحة للنموذج.]"

    workspace_id = default_workspace_key(user["id"])

    async def event_stream():
        try:
            async for event in AGENT.stream_events(
                user_id=user["id"],
                workspace_id=workspace_id,
                project_id=DEFAULT_PROJECT_ID,
                message=message,
                history=history,
            ):
                yield sse_event(event)
        except Exception as exc:
            safe_message = str(exc).replace("\n", " ")[:500]
            yield sse_event({"type": "error", "content": safe_message or "تعذر إكمال الطلب."})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    """Compatibility alias; the frontend uses /api/auth/me."""
    return {"user": current_user(request)}


@app.post("/api/chat")
async def chat(
    body: ChatBody,
    request: Request,
    csrf: None = Depends(require_genisi_csrf),
) -> dict[str, Any]:
    user = current_user(request)
    ensure_sandbox_owner(user)
    if not WORKER.enabled:
        raise HTTPException(503, "Local Genisi Sandbox is disabled. Set SANDBOX_MODE=local.")
    project = workspace_key(user["id"], body.project_id)
    history = [item.model_dump() for item in body.history]
    try:
        answer = await AGENT.run(
            user["id"], project, body.project_id, body.message, history,
        )
        return {"answer": answer}
    except Exception as exc:
        raise HTTPException(502, f"تعذر إكمال طلب الوكيل: {str(exc)[:500]}") from exc


@app.post("/api/terminal")
async def terminal(
    body: TerminalBody,
    request: Request,
    csrf: None = Depends(require_genisi_csrf),
) -> dict[str, Any]:
    user = current_user(request)
    ensure_sandbox_owner(user)
    if not WORKER.enabled:
        raise HTTPException(503, "Local Genisi Sandbox is disabled. Set SANDBOX_MODE=local.")
    project = workspace_key(user["id"], body.project_id)
    try:
        return await WORKER.request("POST", "/v1/exec", json={
            "workspace_id": project, "command": body.command, "cwd": body.cwd
        })
    except Exception as exc:
        raise HTTPException(502, f"Terminal error: {str(exc)[:400]}") from exc


def ensure_sandbox_owner(user: dict[str, Any]) -> None:
    """The built-in runner is deliberately owner-only, not public multi-tenant isolation."""
    owner_email = os.getenv("SANDBOX_OWNER_EMAIL", "").strip().lower()
    if not owner_email:
        raise HTTPException(
            503,
            "Set SANDBOX_OWNER_EMAIL to the email of the trusted application owner. "
            "The built-in sandbox is owner-only because it runs inside the app container.",
        )
    if user.get("email", "").strip().lower() != owner_email:
        raise HTTPException(403, "The local coding sandbox is currently restricted to the application owner.")


@app.get("/api/preview/{project_id}/{path:path}")
async def preview(project_id: str, path: str, request: Request) -> Response:
    user = current_user(request)
    ensure_sandbox_owner(user)
    if not WORKER.enabled:
        raise HTTPException(503, "Local Genisi Sandbox is disabled. Set SANDBOX_MODE=local.")
    project = workspace_key(user["id"], project_id)
    try:
        upstream = await WORKER.preview(project, path or "", request.query_params)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/plain"),
            headers={k: v for k, v in upstream.headers.items() if k.lower() in {"cache-control", "content-security-policy", "etag"}},
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(502, f"تعذر الوصول إلى معاينة Next.js: {str(exc)[:300]}") from exc


def _normalize_space_media_urls(obj: Any, base_url: str) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("/") and (
                "url" in k.lower() or "audio" in k.lower() or v.endswith((".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"))
            ):
                out[k] = urljoin(base_url + "/", v.lstrip("/"))
            else:
                out[k] = _normalize_space_media_urls(v, base_url)
        return out
    if isinstance(obj, list):
        return [_normalize_space_media_urls(x, base_url) for x in obj]
    return obj


async def _get_stepaudio_submit_props(client: httpx.AsyncClient) -> set[str] | None:
    now = time.time()
    if _music_schema_cache["submit_props"] is not None and now - float(_music_schema_cache["ts"]) < 3600:
        return _music_schema_cache["submit_props"]
    try:
        r = await client.get(f"{STEPAUDIO_MUSIC_SPACE_URL}/openapi.json", timeout=8.0)
        if r.status_code == 200:
            spec = r.json()
            post_op = (((spec.get("paths") or {}).get("/v1/audio/music/submit") or {}).get("post") or {})
            schema = ((((post_op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {})
            ref = schema.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                sname = ref.split("/")[-1]
                schema = ((spec.get("components") or {}).get("schemas") or {}).get(sname) or {}
            props = schema.get("properties")
            if isinstance(props, dict) and props:
                prop_set = set(props.keys())
                _music_schema_cache["submit_props"] = prop_set
                _music_schema_cache["ts"] = now
                return prop_set
    except Exception:
        pass
    return None


@app.post("/api/music/submit")
async def music_submit(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Proxy music generation submissions to stepfun-ai/StepAudio-3-Music."""
    _origin_guard(request)
    _rate_limit("music-submit:" + _sha(_client_ip(request)), 20, 300)
    task = str(payload.get("task") or "text_to_music").strip()
    if task not in {"text_to_music", "vocal_to_music", "music_cover"}:
        task = "text_to_music"
    caption = str(payload.get("caption") or payload.get("prompt") or "").strip()[:2000]
    if not caption:
        raise HTTPException(422, "يرجى إدخال وصف الموسيقى (caption)")
    body: dict[str, Any] = {"task": task, "caption": caption}
    for opt_key in ("lyrics", "instrumental", "duration", "vocal_language", "audio_base64", "src_audio_base64", "reference_audio", "audio_url", "audio", "seed"):
        if opt_key in payload and payload[opt_key] is not None and payload[opt_key] != "":
            body[opt_key] = payload[opt_key]

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            allowed_props = await _get_stepaudio_submit_props(client)
            send_body = {k: v for k, v in body.items() if (allowed_props is None or k in allowed_props)} if allowed_props else dict(body)
            if "task" not in send_body and (allowed_props is None or "task" in allowed_props):
                send_body["task"] = task
            if "caption" not in send_body and (allowed_props is None or "caption" in allowed_props):
                send_body["caption"] = caption

            upstream = await client.post(f"{STEPAUDIO_MUSIC_SPACE_URL}/v1/audio/music/submit", json=send_body)
            if upstream.status_code == 422 and len(send_body) > 2:
                minimal: dict[str, Any] = {"task": task, "caption": caption}
                if body.get("lyrics"):
                    minimal["lyrics"] = body["lyrics"]
                upstream = await client.post(f"{STEPAUDIO_MUSIC_SPACE_URL}/v1/audio/music/submit", json=minimal)
            data = upstream.json() if upstream.content else {}
            if upstream.status_code >= 400:
                detail = data.get("detail") or data.get("error") or data.get("message") or f"StepAudio HTTP {upstream.status_code}"
                raise HTTPException(upstream.status_code if upstream.status_code < 500 else 502, str(detail)[:320])
            return _normalize_space_media_urls(data, STEPAUDIO_MUSIC_SPACE_URL)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر الاتصال بنموذج الموسيقى StepAudio-3-Music: {str(exc)[:240]}") from exc


@app.post("/api/music/query")
async def music_query(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Poll music generation status from stepfun-ai/StepAudio-3-Music."""
    _origin_guard(request)
    _rate_limit("music-query:" + _sha(_client_ip(request)), 180, 300)
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip()[:160]
    if not task_id:
        raise HTTPException(422, "معرّف المهمة task_id مطلوب")
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            upstream = await client.post(f"{STEPAUDIO_MUSIC_SPACE_URL}/v1/audio/music/query", json={"task_id": task_id})
            data = upstream.json() if upstream.content else {}
            if upstream.status_code >= 400:
                detail = data.get("detail") or data.get("error") or data.get("message") or f"StepAudio HTTP {upstream.status_code}"
                raise HTTPException(upstream.status_code if upstream.status_code < 500 else 502, str(detail)[:320])
            return _normalize_space_media_urls(data, STEPAUDIO_MUSIC_SPACE_URL)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر الاستعلام عن حالة الموسيقى: {str(exc)[:240]}") from exc


@app.post("/api/voice/tts")
async def voice_tts_proxy(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Server-side proxy for openbmb/VoxCPM-Demo Gradio speech generation."""
    _origin_guard(request)
    _rate_limit("voice-tts:" + _sha(_client_ip(request)), 45, 300)
    text = str(payload.get("text") or "").strip()[:1200]
    if not text:
        raise HTTPException(422, "النص الصوتي فارغ")
    control = str(payload.get("control_instruction") or "").strip()[:400]
    try:
        cfg_value = max(1.0, min(3.0, float(payload.get("cfg_value", 2.0))))
    except (TypeError, ValueError):
        cfg_value = 2.0
    raw_norm = payload.get("do_normalize") if "do_normalize" in payload else payload.get("normalize", False)
    do_normalize = bool(raw_norm)
    if re.search(r"[\u0600-\u06FF\u0400-\u04FF\u3040-\u30FF\uAC00-\uD7AF]", text):
        do_normalize = False

    try:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            prefix = _voxcpm_meta_cache.get("prefix")
            if prefix is None or time.time() - float(_voxcpm_meta_cache.get("ts") or 0) > 3600:
                prefix = "/gradio_api"
                for cand in ("/gradio_api", ""):
                    r_info = await client.get(f"{VOXCPM_SPACE_URL}{cand}/info", timeout=12.0)
                    if r_info.status_code == 200:
                        prefix = cand
                        _voxcpm_meta_cache["prefix"] = prefix
                        _voxcpm_meta_cache["ts"] = time.time()
                        break

            gradio_data = [text, control, None, False, "", cfg_value, do_normalize, False]
            r_call = await client.post(
                f"{VOXCPM_SPACE_URL}{prefix}/call/generate",
                json={"data": gradio_data},
                timeout=25.0,
            )
            call_json = r_call.json() if r_call.content else {}
            event_id = call_json.get("event_id")
            if r_call.status_code >= 400 or not event_id:
                raise HTTPException(502, f"فشل إرسال طلب الصوت إلى VoxCPM ({r_call.status_code})")

            audio_url = ""
            cur_event = ""
            async with client.stream("GET", f"{VOXCPM_SPACE_URL}{prefix}/call/generate/{event_id}", timeout=90.0) as sse:
                async for line in sse.aiter_lines():
                    if line.startswith("event:"):
                        cur_event = line[6:].strip()
                    elif line.startswith("data:"):
                        raw_data = line[5:].strip()
                        if cur_event == "error":
                            raise HTTPException(502, f"خطأ من VoxCPM: {raw_data[:240]}")
                        if cur_event == "complete":
                            arr = json.loads(raw_data)
                            first = arr[0] if isinstance(arr, list) and arr else arr
                            if isinstance(first, dict):
                                u = str(first.get("url") or "")
                                p = str(first.get("path") or "")
                                if u:
                                    audio_url = u if u.startswith("http") else urljoin(VOXCPM_SPACE_URL + "/", u.lstrip("/"))
                                elif p:
                                    audio_url = f"{VOXCPM_SPACE_URL}{prefix}/file={p}"
                            elif isinstance(first, str):
                                audio_url = first if first.startswith("http") else f"{VOXCPM_SPACE_URL}{prefix}/file={first}"
                            break
            if not audio_url:
                raise HTTPException(502, "لم يُرجع نموذج VoxCPM ملفاً صوتياً")
            r_audio = await client.get(audio_url, timeout=45.0)
            if r_audio.status_code >= 400 or not r_audio.content:
                raise HTTPException(502, "تعذر تنزيل المقطع الصوتي من VoxCPM")
            media_type = (r_audio.headers.get("content-type") or "audio/wav").split(";")[0].strip()
            if not media_type.startswith("audio/"):
                media_type = "audio/wav"
            return Response(content=r_audio.content, media_type=media_type, headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر توليد الصوت عبر VoxCPM: {str(exc)[:240]}") from exc


# The secure router owns registration, login, logout, preferences, history and account health.
app.include_router(genisi_auth_router)
