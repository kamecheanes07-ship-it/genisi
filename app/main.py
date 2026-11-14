from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html as html_mod
import ipaddress
import json
import os
import random
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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
    _validate_public_https_url,
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
QWEN_OMNI_SPACE_URL = os.getenv("QWEN_OMNI_SPACE_URL", "https://qwen-qwen3-5-omni-offline-demo.hf.space").rstrip("/")
STEPAUDIO_MUSIC_SPACE_URL = os.getenv("STEPAUDIO_MUSIC_SPACE_URL", "https://stepfun-ai-stepaudio-3-music.hf.space").rstrip("/")
VOXCPM_SPACE_URL = os.getenv("VOXCPM_SPACE_URL", "https://openbmb-voxcpm-demo.hf.space").rstrip("/")
IMAGE_API_BASE_URL = os.getenv("IMAGE_API_BASE_URL", "https://prexzyapis.com").rstrip("/")
VIDEO_API_URL = os.getenv("VIDEO_API_URL", "https://api.omegatech.app/api/ai/Txt2video").strip()
VIDEO_GRADIO_SPACES = [
    s.strip().rstrip("/")
    for s in os.getenv(
        "VIDEO_GRADIO_SPACES",
        "https://lightricks-ltx-video-distilled.hf.space,https://deeprat-ltx-video-zerogpu-optimized.hf.space",
    ).split(",")
    if s.strip()
]
_qwen_meta_cache: dict[str, Any] = {"ts": 0.0, "prefix": None, "endpoints": None}
_music_schema_cache: dict[str, Any] = {"ts": 0.0, "submit_props": None}
_voxcpm_meta_cache: dict[str, Any] = {"ts": 0.0, "prefix": None}
_video_schema_cache: dict[str, tuple[float, str, list[dict[str, Any]]]] = {}
_video_jobs: dict[str, dict[str, Any]] = {}
_video_tasks: dict[str, asyncio.Task[Any]] = {}
_cleanup_task: asyncio.Task[Any] | None = None
AGENT = Agent()
app = FastAPI(title=APP_NAME, version="0.4.4-windows12-embedded-system-prompt", docs_url=None, redoc_url=None, openapi_url=None)
_cors_origins_env = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_env,
    allow_origin_regex=None if _cors_origins_env else os.getenv("CORS_ALLOW_ORIGIN_REGEX", r"^https?://.*"),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Type", "Content-Disposition", "X-Image-Mime", "X-Public-Url"],
)
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


def is_sandbox_available_for(user: dict[str, Any]) -> bool:
    """Check whether the optional local owner-only workspace runner is active for this user."""
    if not WORKER.enabled:
        return False
    owner_email = os.getenv("SANDBOX_OWNER_EMAIL", "").strip().lower()
    if not owner_email:
        return False
    return user.get("email", "").strip().lower() == owner_email


@app.get("/api/workspace")
async def get_workspace(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if not is_sandbox_available_for(user):
        return {"success": True, "entries": [], "truncated": False, "sandbox_enabled": False}
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
        return {"success": True, "entries": entries, "truncated": bool(result.get("truncated")), "sandbox_enabled": True}
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
    if not is_sandbox_available_for(user):
        return {"plan": [], "notes": [], "sessions": 0, "updated": None, "sandbox_enabled": False}
    return {"plan": [], "notes": [], "sessions": 0, "updated": None, "sandbox_enabled": True}


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
            if (
                isinstance(v, str)
                and not v.startswith("//")
                and len(v) < 1024
                and (
                    v.startswith(("/v1/", "/outputs/", "/files/", "/audio/", "/gradio_api/", "/file="))
                    or (v.startswith("/") and v.endswith((".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus")))
                )
            ):
                out[k] = urljoin(base_url + "/", v.lstrip("/"))
            else:
                out[k] = _normalize_space_media_urls(v, base_url)
        return out
    if isinstance(obj, list):
        return [_normalize_space_media_urls(x, base_url) for x in obj]
    return obj


def _extract_stepaudio_error(data: Any, status_code: int) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err_type = str(err.get("type") or "").strip()
            err_msg = str(err.get("message") or "").strip()
            if err_type == "rate_limited" or status_code == 429:
                return "خدمة تأليف الموسيقى مشغولة حالياً (حد الطلبات بالدقيقة) — يرجى المحاولة بعد قليل."
            if err_type and err_msg:
                return f"{err_msg} ({err_type})"
            if err_msg or err_type:
                return err_msg or err_type
        for k in ("detail", "error", "message"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return f"StepAudio HTTP {status_code}"


def _strip_data_uri_prefix(raw: str) -> str:
    val = str(raw or "").strip()
    if val.startswith("data:") and "," in val:
        return val.split(",", 1)[1].strip()
    return val


@app.post("/api/music/submit")
async def music_submit(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Direct Python gateway for stepfun-ai/StepAudio-3-Music using its official step-music contract."""
    _rate_limit("music-submit:" + _sha(_client_ip(request)), 20, 300)
    task = str(payload.get("task") or "text_to_music").strip().lower()
    if task not in {"text_to_music", "vocal_to_music", "music_cover"}:
        task = "text_to_music"
    caption = str(payload.get("caption") or payload.get("prompt") or "").strip()[:500]
    if not caption:
        raise HTTPException(422, "يرجى إدخال وصف الموسيقى (caption)")

    resp_fmt = str(payload.get("response_format") or "mp3").strip().lower()
    if resp_fmt not in {"mp3", "wav", "flac", "opus"}:
        resp_fmt = "mp3"
    sample_rate = payload.get("sample_rate")
    sample_rate = sample_rate if sample_rate in {16000, 24000, 32000, 44100, 48000} else 44100

    send_body: dict[str, Any] = {
        "model_id": "step-music",
        "response_format": resp_fmt,
        "sample_rate": sample_rate,
        "task": task,
        "caption": caption,
    }
    if resp_fmt in {"mp3", "opus"}:
        bit_rate = payload.get("bit_rate")
        send_body["bit_rate"] = bit_rate if bit_rate in {64, 128, 192, 256, 320} else 192

    raw_lyrics = str(payload.get("lyrics") or "").strip()[:4000]
    if raw_lyrics.lower() in {"[instrumental]", "instrumental"}:
        raw_lyrics = ""

    vocal_b64 = _strip_data_uri_prefix(payload.get("vocal_audio") or payload.get("audio_base64") or "")
    song_b64 = _strip_data_uri_prefix(payload.get("song_audio") or payload.get("src_audio_base64") or payload.get("audio_base64") or "")

    if task == "vocal_to_music":
        if vocal_b64:
            send_body["vocal_audio"] = vocal_b64
            if raw_lyrics:
                send_body["lyrics"] = raw_lyrics
        else:
            send_body["task"] = "text_to_music"
            task = "text_to_music"
    elif task == "music_cover":
        if song_b64:
            send_body["song_audio"] = song_b64
            if raw_lyrics:
                send_body["lyrics"] = raw_lyrics
        else:
            send_body["task"] = "text_to_music"
            task = "text_to_music"

    if task == "text_to_music":
        if bool(payload.get("instrumental")):
            # Official StepAudio-3-Music contract: instrumental tracks set instrumental=True and omit lyrics.
            send_body["instrumental"] = True
        elif raw_lyrics:
            send_body["lyrics"] = raw_lyrics

    try:
        timeout = httpx.Timeout(230.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            upstream = await client.post(f"{STEPAUDIO_MUSIC_SPACE_URL}/v1/audio/music/submit", json=send_body)
            data = upstream.json() if upstream.content else {}
            if upstream.status_code >= 400:
                detail = _extract_stepaudio_error(data, upstream.status_code)
                raise HTTPException(upstream.status_code if upstream.status_code < 500 else 502, detail[:320])
            return _normalize_space_media_urls(data, STEPAUDIO_MUSIC_SPACE_URL)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر الاتصال بنموذج الموسيقى StepAudio-3-Music: {str(exc)[:240]}") from exc


@app.post("/api/music/query")
async def music_query(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Poll music generation status from stepfun-ai/StepAudio-3-Music."""
    _rate_limit("music-query:" + _sha(_client_ip(request)), 180, 300)
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip()[:160]
    if not task_id:
        raise HTTPException(422, "معرّف المهمة task_id مطلوب")
    try:
        timeout = httpx.Timeout(45.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            upstream = await client.post(f"{STEPAUDIO_MUSIC_SPACE_URL}/v1/audio/music/query", json={"task_id": task_id})
            data = upstream.json() if upstream.content else {}
            if upstream.status_code >= 400:
                detail = _extract_stepaudio_error(data, upstream.status_code)
                raise HTTPException(upstream.status_code if upstream.status_code < 500 else 502, detail[:320])
            return _normalize_space_media_urls(data, STEPAUDIO_MUSIC_SPACE_URL)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر الاستعلام عن حالة الموسيقى: {str(exc)[:240]}") from exc


@app.post("/api/voice/tts")
async def voice_tts_proxy(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Direct Python gateway for openbmb/VoxCPM-Demo Gradio speech synthesis."""
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

    ref_b64 = _strip_data_uri_prefix(payload.get("reference_audio_base64") or "")

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

            ref_file_data = None
            if ref_b64 and len(ref_b64) < 8_000_000:
                try:
                    ref_bytes = base64.b64decode(ref_b64)
                    up_res = await client.post(
                        f"{VOXCPM_SPACE_URL}{prefix}/upload",
                        files={"files": ("voice-anchor.wav", ref_bytes, "audio/wav")},
                        timeout=25.0,
                    )
                    if up_res.status_code == 200:
                        up_arr = up_res.json()
                        if isinstance(up_arr, list) and up_arr:
                            up_path = up_arr[0] if isinstance(up_arr[0], str) else (up_arr[0] or {}).get("path", "")
                            if up_path:
                                ref_file_data = {"path": up_path, "meta": {"_type": "gradio.FileData"}}
                except Exception:
                    ref_file_data = None

            gradio_data = [
                text,
                "" if ref_file_data else control,
                ref_file_data,
                False,
                "",
                cfg_value,
                do_normalize,
                False,
            ]
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


# ═══════════════════════════════════════════════════════════════════════════════
# Direct Python Model & Tool Gateways (Zero Third-Party Proxies, Native CORS)
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_OUTBOUND_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Accept": "*/*",
}


def _sniff_image_mime(raw: bytes) -> str:
    if len(raw) > 3 and raw[:3] == b"\x89PN":
        return "image/png"
    if len(raw) > 2 and raw[:2] == b"\xff\xd8":
        return "image/jpeg"
    if len(raw) > 11 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if len(raw) > 3 and raw[:3] == b"GIF":
        return "image/gif"
    return ""


async def _discover_qwen_gradio(client: httpx.AsyncClient, preferred_endpoint: str = "chat_predict") -> tuple[str, str, dict[str, Any]]:
    now = time.time()
    preferred = preferred_endpoint.strip().strip("/") or "chat_predict"
    cached_prefix = _qwen_meta_cache.get("prefix")
    cached_eps = _qwen_meta_cache.get("endpoints")
    if (
        cached_prefix is not None
        and isinstance(cached_eps, dict)
        and now - float(_qwen_meta_cache.get("ts") or 0) < 3600
    ):
        for k, val in cached_eps.items():
            if k.strip("/") == preferred:
                return str(cached_prefix), k.strip("/"), val if isinstance(val, dict) else {}

    last_err = "unknown"
    for cand in ("/gradio_api", ""):
        try:
            r = await client.get(f"{QWEN_OMNI_SPACE_URL}{cand}/info", timeout=15.0)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            info = r.json()
            named = info.get("named_endpoints") or {}
            if not isinstance(named, dict) or not named:
                last_err = "missing named_endpoints"
                continue
            _qwen_meta_cache.update({"ts": now, "prefix": cand, "endpoints": named})
            for k, val in named.items():
                if k.strip("/") == preferred:
                    return cand, k.strip("/"), val if isinstance(val, dict) else {}
            first_key = next(iter(named.keys())).strip("/")
            return cand, first_key, named.get(f"/{first_key}") or named.get(first_key) or {}
        except Exception as exc:
            last_err = str(exc)[:180]
    raise HTTPException(502, f"تعذر الاتصال بمحرك Genisi 4 Atlas: {last_err}")


def _extract_qwen_assistant_text(payload: Any) -> str | None:
    arr = payload
    if isinstance(arr, dict) and "data" in arr:
        arr = arr["data"]
    if isinstance(arr, str):
        try:
            arr = json.loads(arr)
        except Exception:
            return None
    if not isinstance(arr, list) or len(arr) <= 4 or not isinstance(arr[4], list):
        return None
    history = arr[4]
    last_user = -1
    for idx in range(len(history) - 1, -1, -1):
        item = history[idx]
        if isinstance(item, dict) and item.get("role") == "user":
            last_user = idx
            break
    for idx in range(len(history) - 1, last_user, -1):
        item = history[idx]
        if isinstance(item, dict) and item.get("role") == "assistant":
            content = item.get("content")
            if isinstance(content, str) and content != "":
                return content
    return None


@app.get("/api/models/qwen/info")
async def qwen_model_info(request: Request, endpoint: str = "chat_predict") -> dict[str, Any]:
    """Check Qwen3.5-Omni health and return Gradio endpoint metadata via Python."""
    _rate_limit("qwen-info:" + _sha(_client_ip(request)), 60, 300)
    async with httpx.AsyncClient(timeout=18.0, follow_redirects=True) as client:
        prefix, chosen, schema = await _discover_qwen_gradio(client, endpoint)
        return {
            "ok": True,
            "space": QWEN_OMNI_SPACE_URL,
            "prefix": prefix,
            "endpoint": chosen,
            "schema": schema,
        }


@app.post("/api/models/qwen/upload")
async def qwen_model_upload(
    request: Request,
    name: str = "upload.bin",
    mime: str = "application/octet-stream",
) -> dict[str, Any]:
    """Upload a multimodal attachment directly from Python to Qwen3.5-Omni (no browser CORS calls)."""
    _rate_limit("qwen-upload:" + _sha(_client_ip(request)), 45, 300)
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "الملف المرفق فارغ")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(413, "حجم المرفق يتجاوز الحد المسموح (25MB)")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "upload.bin").name)[:96] or "upload.bin"
    safe_mime = (mime or request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()[:120]

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            prefix, _, _ = await _discover_qwen_gradio(client)
            up_res = await client.post(
                f"{QWEN_OMNI_SPACE_URL}{prefix}/upload",
                files={"files": (safe_name, raw, safe_mime)},
            )
            if up_res.status_code >= 400:
                raise HTTPException(502, f"فشل رفع المرفق إلى المحرك ({up_res.status_code})")
            arr = up_res.json()
            if not isinstance(arr, list) or not arr:
                raise HTTPException(502, "استجابة رفع غير متوقعة من المحرك")
            remote_path = arr[0] if isinstance(arr[0], str) else str((arr[0] or {}).get("path") or "")
            if not remote_path:
                raise HTTPException(502, "لم يُرجع المحرك مسار المرفق")
            public_url = f"{QWEN_OMNI_SPACE_URL}{prefix}/file={remote_path}"
            return {
                "ok": True,
                "path": remote_path,
                "url": public_url,
                "file_data": {"path": remote_path, "meta": {"_type": "gradio.FileData"}},
            }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر رفع المرفق عبر الخادم: {str(exc)[:220]}") from exc


@app.post("/api/models/qwen/stream")
async def qwen_model_stream(request: Request, payload: dict[str, Any] = Body(...)) -> StreamingResponse:
    """Stream Qwen3.5-Omni Gradio SSE directly through Python with native CORS headers."""
    _rate_limit("qwen-stream:" + _sha(_client_ip(request)), 90, 300)
    data = payload.get("data")
    if not isinstance(data, list):
        raise HTTPException(422, "الحقل data يجب أن يكون مصفوفة")
    endpoint = str(payload.get("endpoint") or "chat_predict").strip().strip("/") or "chat_predict"

    client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0), follow_redirects=True)
    try:
        prefix, chosen, _ = await _discover_qwen_gradio(client, endpoint)
        call_url = f"{QWEN_OMNI_SPACE_URL}{prefix}/call/{chosen}"
        r_call = await client.post(call_url, json={"data": data}, timeout=35.0)
        if r_call.status_code >= 400:
            body_preview = r_call.text[:220]
            await client.aclose()
            raise HTTPException(502, f"رفض المحرك الطلب (HTTP {r_call.status_code}): {body_preview}")
        started = r_call.json() if r_call.content else {}
        event_id = started.get("event_id")
        if not event_id:
            await client.aclose()
            raise HTTPException(502, "لم يُرجع المحرك معرّف بث صالحاً")
    except HTTPException:
        raise
    except Exception as exc:
        await client.aclose()
        raise HTTPException(502, f"تعذر بدء البث عبر الخادم: {str(exc)[:240]}") from exc

    async def _sse_generator():
        try:
            async with client.stream("GET", f"{call_url}/{event_id}", timeout=180.0) as upstream:
                if upstream.status_code >= 400:
                    err_txt = (await upstream.aread()).decode("utf-8", errors="replace")[:200]
                    yield f"event: error\ndata: {json.dumps({'message': f'HTTP {upstream.status_code}: {err_txt}'})}\n\n".encode("utf-8")
                    return
                async for chunk in upstream.aiter_bytes():
                    if chunk:
                        yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'message': str(exc)[:220]})}\n\n".encode("utf-8")
        finally:
            await client.aclose()

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/models/qwen/once")
async def qwen_model_once(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Single-turn non-streaming Qwen3.5-Omni call via Python (used for prompt refinement & titles)."""
    _rate_limit("qwen-once:" + _sha(_client_ip(request)), 90, 300)
    data = payload.get("data")
    if not isinstance(data, list):
        raise HTTPException(422, "الحقل data يجب أن يكون مصفوفة")
    endpoint = str(payload.get("endpoint") or "chat_predict").strip().strip("/") or "chat_predict"

    try:
        async with httpx.AsyncClient(timeout=75.0, follow_redirects=True) as client:
            prefix, chosen, _ = await _discover_qwen_gradio(client, endpoint)
            call_url = f"{QWEN_OMNI_SPACE_URL}{prefix}/call/{chosen}"
            r_call = await client.post(call_url, json={"data": data}, timeout=25.0)
            started = r_call.json() if r_call.content else {}
            event_id = started.get("event_id")
            if r_call.status_code >= 400 or not event_id:
                raise HTTPException(502, f"تعذر تشغيل المحرك ({r_call.status_code})")

            latest = ""
            cur_event = ""
            async with client.stream("GET", f"{call_url}/{event_id}", timeout=65.0) as sse:
                async for line in sse.aiter_lines():
                    if line.startswith("event:"):
                        cur_event = line[6:].strip()
                    elif line.startswith("data:"):
                        raw_data = line[5:].strip()
                        if cur_event == "error":
                            raise HTTPException(502, f"خطأ من المحرك: {raw_data[:200]}")
                        if cur_event in {"generating", "complete"} and raw_data and raw_data != "null":
                            try:
                                extracted = _extract_qwen_assistant_text(json.loads(raw_data))
                                if extracted is not None:
                                    latest = extracted
                            except Exception:
                                pass
                            if cur_event == "complete":
                                break
            return {"ok": True, "text": latest}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر إتمام الطلب عبر الخادم: {str(exc)[:220]}") from exc


async def _decode_upstream_image_response(client: httpx.AsyncClient, r: httpx.Response) -> tuple[bytes, str]:
    raw = r.content or b""
    mime = _sniff_image_mime(raw)
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime and r.status_code < 400 and ctype.startswith("image/"):
        mime = ctype
    if mime and r.status_code < 400 and len(raw) > 128:
        return raw, mime

    txt = raw[:300_000].decode("utf-8", errors="replace")
    parsed: Any = None
    try:
        parsed = json.loads(txt)
    except Exception:
        parsed = None

    if r.status_code >= 400 or (
        isinstance(parsed, dict)
        and (parsed.get("status") is False or (parsed.get("statusCode") and int(parsed["statusCode"]) != 200))
    ):
        msg = ""
        if isinstance(parsed, dict):
            msg = str(parsed.get("message") or parsed.get("error") or "")
        raise HTTPException(502, (msg or f"Image service HTTP {r.status_code}")[:260])

    # Handle JSON wrapping a data URI or HTTPS image URL
    cand_str = ""
    if isinstance(parsed, dict):
        for k in ("image_url", "imageUrl", "image", "url", "output", "result", "link", "data"):
            val = parsed.get(k)
            if isinstance(val, str) and val.strip():
                cand_str = val.strip()
                break
            if isinstance(val, list) and val and isinstance(val[0], str):
                cand_str = val[0].strip()
                break
    if cand_str.startswith("data:image/") and "," in cand_str:
        head, b64_part = cand_str.split(",", 1)
        out_bytes = base64.b64decode(b64_part)
        out_mime = _sniff_image_mime(out_bytes) or head[5:].split(";")[0] or "image/png"
        return out_bytes, out_mime
    if cand_str.startswith("https://"):
        safe_u = _validate_public_https_url(cand_str)
        r2 = await client.get(safe_u, headers=_DEFAULT_OUTBOUND_HEADERS, timeout=60.0)
        m2 = _sniff_image_mime(r2.content) or "image/png"
        if r2.status_code < 400 and len(r2.content) > 128:
            return r2.content, m2

    raise HTTPException(502, "لم تُرجع خدمة الصور بيانات صورة صالحة")


@app.post("/api/models/image/generate")
async def model_image_generate(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Generate an image directly from Python without any third-party proxy."""
    _rate_limit("img-gen:" + _sha(_client_ip(request)), 30, 300)
    prompt = str(payload.get("prompt") or "").strip()[:1800]
    if not prompt:
        raise HTTPException(422, "وصف الصورة مطلوب")
    model_id = str(payload.get("model") or "DALL-E").strip()[:60] or "DALL-E"

    try:
        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
            r = await client.get(
                f"{IMAGE_API_BASE_URL}/ai/aiappgen",
                params={"prompt": prompt, "modelId": model_id},
                headers=_DEFAULT_OUTBOUND_HEADERS,
            )
            img_bytes, mime = await _decode_upstream_image_response(client, r)
            return Response(
                content=img_bytes,
                media_type=mime,
                headers={"Cache-Control": "no-store", "X-Image-Mime": mime},
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر توليد الصورة عبر الخادم: {str(exc)[:240]}") from exc


@app.post("/api/models/image/edit")
async def model_image_edit(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Edit an image directly from Python without any third-party proxy."""
    _rate_limit("img-edit:" + _sha(_client_ip(request)), 25, 300)
    prompt = str(payload.get("prompt") or "").strip()[:1200]
    if not prompt:
        raise HTTPException(422, "تعليمات تعديل الصورة مطلوبة")

    image_url = str(payload.get("image_url") or "").strip()
    image_b64 = _strip_data_uri_prefix(payload.get("image_base64") or "")
    in_mime = str(payload.get("mime") or "image/png").split(";")[0].strip().lower()
    if not in_mime.startswith("image/"):
        in_mime = "image/png"

    try:
        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
            if image_url.startswith("https://"):
                public_img_url = _validate_public_https_url(image_url)
            elif image_b64:
                raw_src = base64.b64decode(image_b64)
                if len(raw_src) > 20 * 1024 * 1024:
                    raise HTTPException(413, "حجم الصورة يتجاوز الحد المسموح")
                ext = "jpg" if "jpeg" in in_mime or "jpg" in in_mime else "webp" if "webp" in in_mime else "png"
                prefix, _, _ = await _discover_qwen_gradio(client)
                up_res = await client.post(
                    f"{QWEN_OMNI_SPACE_URL}{prefix}/upload",
                    files={"files": (f"edit-src.{ext}", raw_src, in_mime)},
                    timeout=45.0,
                )
                if up_res.status_code >= 400:
                    raise HTTPException(502, "تعذر تجهيز الصورة المصدر للتعديل")
                arr = up_res.json()
                remote_path = arr[0] if isinstance(arr, list) and arr and isinstance(arr[0], str) else (arr[0] or {}).get("path", "") if isinstance(arr, list) and arr else ""
                if not remote_path:
                    raise HTTPException(502, "تعذر الحصول على رابط الصورة المصدر")
                public_img_url = f"{QWEN_OMNI_SPACE_URL}{prefix}/file={remote_path}"
            else:
                raise HTTPException(422, "يرجى إرفاق صورة أو تمرير رابط صورة عام")

            r = await client.get(
                f"{IMAGE_API_BASE_URL}/ai/imageedit",
                params={"prompt": prompt, "image": public_img_url},
                headers=_DEFAULT_OUTBOUND_HEADERS,
            )
            img_bytes, mime = await _decode_upstream_image_response(client, r)
            return Response(
                content=img_bytes,
                media_type=mime,
                headers={"Cache-Control": "no-store", "X-Image-Mime": mime},
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر تعديل الصورة عبر الخادم: {str(exc)[:240]}") from exc


def _sniff_video_mp4(raw: bytes, content_type: str = "") -> bool:
    if len(raw) < 512:
        return False
    ct = (content_type or "").lower()
    if ct.startswith("video/"):
        return True
    # ISO BMFF / MP4 box header ('ftyp' at offset 4..8) or WebM EBML header (0x1A45DFA3)
    if raw[4:8] == b"ftyp" or raw[:4] == b"\x1a\x45\xdf\xa3":
        return True
    return False


def _prune_video_jobs() -> None:
    now = time.time()
    expired = [tid for tid, job in _video_jobs.items() if now - float(job.get("ts") or 0) > 1800]
    for tid in expired:
        _video_jobs.pop(tid, None)
        _video_tasks.pop(tid, None)
    # Cap stored MP4 payloads in memory to 16 recent videos
    with_bytes = sorted(
        [(float(j.get("ts") or 0), tid) for tid, j in _video_jobs.items() if j.get("bytes")],
        reverse=True,
    )
    for _, old_tid in with_bytes[16:]:
        if old_tid in _video_jobs:
            _video_jobs[old_tid].pop("bytes", None)


def _video_dims_for_ratio(ratio: str) -> tuple[int, int]:
    # Multiples of 32 within LTX-Video [256, 1280] slider bounds
    mapping = {
        "16:9": (512, 896),
        "auto": (512, 896),
        "9:16": (896, 512),
        "1:1": (640, 640),
        "4:3": (576, 768),
        "3:4": (768, 576),
    }
    return mapping.get(ratio, (512, 896))


async def _discover_video_space(client: httpx.AsyncClient, space_url: str) -> tuple[str, list[dict[str, Any]]]:
    now = time.time()
    cached = _video_schema_cache.get(space_url)
    if cached and now - cached[0] < 3600:
        return cached[1], cached[2]
    for prefix in ("/gradio_api", ""):
        r = await client.get(f"{space_url}{prefix}/info", timeout=15.0)
        if r.status_code != 200:
            continue
        info = r.json()
        named = info.get("named_endpoints") or {}
        ep = named.get("/text_to_video") or named.get("text_to_video")
        if isinstance(ep, dict):
            params = ep.get("parameters")
            if isinstance(params, list) and params:
                _video_schema_cache[space_url] = (now, prefix, params)
                return prefix, params
    raise RuntimeError(f"تعذر اكتشاف واجهة text_to_video في {space_url}")


async def _generate_video_via_gradio_space(
    space_url: str,
    prompt: str,
    ratio: str,
    auth_token: str | None = None,
) -> bytes:
    height_ui, width_ui = _video_dims_for_ratio(ratio)
    headers = dict(_DEFAULT_OUTBOUND_HEADERS)
    if auth_token and space_url.endswith(".hf.space"):
        headers["Authorization"] = f"Bearer {auth_token}"

    async with httpx.AsyncClient(timeout=240.0, follow_redirects=True) as client:
        prefix, params = await _discover_video_space(client, space_url)
        values_by_name: dict[str, Any] = {
            "prompt": prompt[:1000],
            "negative_prompt": "worst quality, inconsistent motion, blurry, jittery, distorted, watermark, text",
            "input_image_filepath": None,
            "input_video_filepath": None,
            "height_ui": height_ui,
            "width_ui": width_ui,
            "mode": "text-to-video",
            "duration_ui": 3.5,
            "ui_frames_to_use": 9,
            "seed_ui": random.randint(1, 2147483647),
            "randomize_seed": True,
            "ui_guidance_scale": 3.0,
            "improve_texture_flag": True,
            "slow_motion_flag": False,
        }
        call_data = [
            values_by_name.get(str(p.get("parameter_name") or ""), p.get("parameter_default"))
            for p in params
        ]
        r_call = await client.post(
            f"{space_url}{prefix}/call/text_to_video",
            headers={**headers, "Content-Type": "application/json"},
            json={"data": call_data},
            timeout=30.0,
        )
        if r_call.status_code >= 400:
            raise RuntimeError(f"{space_url} HTTP {r_call.status_code}: {(r_call.text or '')[:180]}")
        event_id = (r_call.json() or {}).get("event_id")
        if not event_id:
            raise RuntimeError(f"{space_url} missing event_id")

        video_url = ""
        async with client.stream(
            "GET",
            f"{space_url}{prefix}/call/text_to_video/{event_id}",
            headers=headers,
            timeout=220.0,
        ) as stream_res:
            if stream_res.status_code >= 400:
                raise RuntimeError(f"{space_url} stream HTTP {stream_res.status_code}")
            cur_event = ""
            async for line in stream_res.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    cur_event = line[6:].strip()
                elif line.startswith("data:"):
                    raw_data = line[5:].strip()
                    if cur_event == "error":
                        raise RuntimeError(f"{space_url} error: {raw_data[:200]}")
                    if cur_event == "complete":
                        parsed = json.loads(raw_data)
                        first = parsed[0] if isinstance(parsed, list) and parsed else parsed
                        if isinstance(first, dict):
                            vid_obj = first.get("video") if isinstance(first.get("video"), dict) else first
                            u = vid_obj.get("url") or ""
                            p = vid_obj.get("path") or ""
                            if u:
                                video_url = u if u.startswith("http") else f"{space_url}{u}"
                            elif p:
                                video_url = p if p.startswith("http") else f"{space_url}{prefix}/file={p}"
                        elif isinstance(first, str):
                            video_url = first if first.startswith("http") else f"{space_url}{prefix}/file={first}"
                        break
        if not video_url:
            raise RuntimeError(f"{space_url} returned no video file")
        dl = await client.get(video_url, headers=headers, timeout=90.0)
        if dl.status_code >= 400 or not _sniff_video_mp4(dl.content, dl.headers.get("content-type", "")):
            raise RuntimeError(f"تعذر تنزيل ملف الفيديو من {space_url}")
        return dl.content


def _extract_video_url_from_json(obj: Any) -> str:
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith(("http://", "https://")) and (
            re.search(r"\.(mp4|webm|mov)(\?|$)", s, re.I) or "video" in s.lower()
        ):
            return re.sub(r"^http://", "https://", s, flags=re.I)
        return ""
    if isinstance(obj, list):
        for item in obj:
            got = _extract_video_url_from_json(item)
            if got:
                return got
        return ""
    if isinstance(obj, dict):
        for key in ("video_url", "videoUrl", "video", "output_url", "mp4", "url", "result", "data", "output"):
            if key in obj:
                val = obj[key]
                if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                    return re.sub(r"^http://", "https://", val.strip(), flags=re.I)
                got = _extract_video_url_from_json(val)
                if got:
                    return got
    return ""


async def _generate_video_via_rest_api(prompt: str, ratio: str, sound: bool) -> tuple[bytes | None, str]:
    if not VIDEO_API_URL:
        raise RuntimeError("VIDEO_API_URL unconfigured")
    norm_ratio = "16:9" if ratio == "auto" else ratio
    async with httpx.AsyncClient(timeout=150.0, follow_redirects=True) as client:
        r = await client.get(
            VIDEO_API_URL,
            params={
                "action": "generate",
                "prompt": prompt[:1000],
                "ratio": norm_ratio,
                "sound": "true" if sound else "false",
            },
            headers=_DEFAULT_OUTBOUND_HEADERS,
        )
        if r.status_code < 400 and _sniff_video_mp4(r.content, r.headers.get("content-type", "")):
            return r.content, "video/mp4"
        text = r.text or ""
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code >= 400 or (
            isinstance(data, dict)
            and (data.get("status") is False or (data.get("statusCode") and int(data["statusCode"]) != 200))
        ):
            msg = str((data or {}).get("message") or (data or {}).get("error") or f"HTTP {r.status_code}")
            raise RuntimeError(msg[:220])

        direct_url = _extract_video_url_from_json(data) if data is not None else ""
        if not direct_url:
            m = re.search(r"https?://[^\s\"'<>]+", text)
            if m:
                direct_url = re.sub(r"^http://", "https://", m.group(0), flags=re.I)
        if direct_url:
            dl = await client.get(direct_url, headers=_DEFAULT_OUTBOUND_HEADERS, timeout=60.0)
            if dl.status_code < 400 and _sniff_video_mp4(dl.content, dl.headers.get("content-type", "")):
                return dl.content, "video/mp4"
            return None, direct_url

        nested = (data or {}).get("data") if isinstance(data, dict) else {}
        if not isinstance(nested, dict):
            nested = {}
        task_key = str(
            (data or {}).get("task_id")
            or (data or {}).get("taskId")
            or (data or {}).get("request_id")
            or (data or {}).get("id")
            or nested.get("taskKey")
            or nested.get("task_key")
            or nested.get("task_id")
            or nested.get("id")
            or ""
        ).strip()
        if not task_key:
            raise RuntimeError("لم تُرجع خدمة الفيديو رابطاً أو معرّف مهمة")

        base_root = VIDEO_API_URL.rsplit("/", 1)[0]
        deadline = time.time() + 180.0
        while time.time() < deadline:
            await asyncio.sleep(4.0)
            for poll_url, poll_params in (
                (f"{base_root}/txt2video-status", {"task_key": task_key}),
                (VIDEO_API_URL, {"action": "status", "task_id": task_key, "id": task_key}),
            ):
                try:
                    sr = await client.get(poll_url, params=poll_params, headers=_DEFAULT_OUTBOUND_HEADERS, timeout=25.0)
                    if sr.status_code >= 400:
                        continue
                    if _sniff_video_mp4(sr.content, sr.headers.get("content-type", "")):
                        return sr.content, "video/mp4"
                    sj = sr.json()
                    st = str(
                        sj.get("status")
                        or (sj.get("data") or {}).get("status")
                        or ""
                    ).lower()
                    if st in {"failed", "error", "cancelled", "canceled"}:
                        raise RuntimeError(str(sj.get("message") or sj.get("error") or "فشل توليد الفيديو"))
                    found = _extract_video_url_from_json(sj)
                    if found:
                        dl = await client.get(found, headers=_DEFAULT_OUTBOUND_HEADERS, timeout=60.0)
                        if dl.status_code < 400 and _sniff_video_mp4(dl.content, dl.headers.get("content-type", "")):
                            return dl.content, "video/mp4"
                        return None, found
                except RuntimeError:
                    raise
                except Exception:
                    continue
        raise RuntimeError("انتهت مهلة انتظار توليد الفيديو")


async def _execute_video_job(task_id: str, prompt: str, ratio: str, sound: bool) -> None:
    job = _video_jobs.get(task_id)
    if not job:
        return
    errors: list[str] = []
    hf_tok = (os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN") or "").strip() or None

    # 1) Primary & secondary ZeroGPU LTX-Video Spaces on Hugging Face (fast ~15-35s, real 30fps MP4)
    for space_url in VIDEO_GRADIO_SPACES:
        auth_attempts = [hf_tok, None] if hf_tok else [None]
        for tok in auth_attempts:
            try:
                job["step"] = "generating"
                mp4_bytes = await _generate_video_via_gradio_space(space_url, prompt, ratio, auth_token=tok)
                job.update(
                    {
                        "status": "succeeded",
                        "bytes": mp4_bytes,
                        "mime": "video/mp4",
                        "video_url": f"/api/models/video/file/{task_id}",
                        "ts": time.time(),
                    }
                )
                return
            except Exception as exc:
                err_txt = str(exc)[:200]
                errors.append(err_txt)
                # Only retry same space without token if token auth/quota caused failure
                if tok is not None and any(k in err_txt.lower() for k in ("401", "403", "quota", "token", "auth")):
                    continue
                break

    # 2) Fallback direct REST video provider (if online)
    try:
        mp4_bytes, url_or_mime = await _generate_video_via_rest_api(prompt, ratio, sound)
        if mp4_bytes:
            job.update(
                {
                    "status": "succeeded",
                    "bytes": mp4_bytes,
                    "mime": url_or_mime or "video/mp4",
                    "video_url": f"/api/models/video/file/{task_id}",
                    "ts": time.time(),
                }
            )
            return
        if url_or_mime.startswith("https://"):
            job.update({"status": "succeeded", "video_url": url_or_mime, "ts": time.time()})
            return
    except Exception as exc:
        errors.append(str(exc)[:200])

    summary = errors[-1] if errors else "الخدمة مشغولة حالياً"
    job.update(
        {
            "status": "failed",
            "error": f"تعذر توليد الفيديو حالياً ({summary}) — يرجى إعادة المحاولة بعد لحظات.",
            "ts": time.time(),
        }
    )


@app.post("/api/models/video/submit")
async def model_video_submit(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Queue a video generation job asynchronously in Python (returns immediately, zero 502 timeouts)."""
    _rate_limit("vid-submit:" + _sha(_client_ip(request)), 20, 300)
    prompt = str(payload.get("prompt") or "").strip()[:1200]
    if not prompt:
        raise HTTPException(422, "وصف الفيديو مطلوب")
    ratio = str(payload.get("ratio") or "auto").strip()
    if ratio not in {"auto", "1:1", "16:9", "9:16", "4:3", "3:4"}:
        ratio = "auto"
    sound = payload.get("sound") is not False

    _prune_video_jobs()
    task_id = f"vid_{uuid.uuid4().hex[:18]}"
    _video_jobs[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "step": "queued",
        "ts": time.time(),
    }
    bg_task = asyncio.create_task(_execute_video_job(task_id, prompt, ratio, sound))
    _video_tasks[task_id] = bg_task
    bg_task.add_done_callback(lambda _: _video_tasks.pop(task_id, None))
    return {"ok": True, "status": "processing", "task_id": task_id}


@app.post("/api/models/video/status")
async def model_video_status(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Poll asynchronous video generation task status from Python."""
    _rate_limit("vid-status:" + _sha(_client_ip(request)), 240, 300)
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip()[:160]
    if not task_id:
        raise HTTPException(422, "معرّف مهمة الفيديو مطلوب")

    job = _video_jobs.get(task_id)
    if job is not None:
        st = str(job.get("status") or "processing")
        if st == "succeeded":
            return {
                "ok": True,
                "status": "SUCCEEDED",
                "task_id": task_id,
                "video_url": job.get("video_url") or f"/api/models/video/file/{task_id}",
            }
        if st == "failed":
            return {
                "ok": False,
                "status": "failed",
                "task_id": task_id,
                "message": str(job.get("error") or "فشل توليد الفيديو"),
            }
        return {"ok": True, "status": "PROCESSING", "task_id": task_id, "step": job.get("step") or "generating"}

    # Fallback for external task IDs
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(
                VIDEO_API_URL,
                params={"action": "status", "task_id": task_id, "id": task_id},
                headers=_DEFAULT_OUTBOUND_HEADERS,
            )
            try:
                data = r.json()
            except Exception:
                data = {"raw": (r.text or "")[:300]}
            if r.status_code >= 400:
                msg = str((data or {}).get("message") or (data or {}).get("error") or f"HTTP {r.status_code}")
                raise HTTPException(502, msg[:240])
            return data if isinstance(data, dict) else {"data": data}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر الاستعلام عن حالة الفيديو: {str(exc)[:240]}") from exc


@app.get("/api/models/video/file/{task_id}")
async def model_video_file(task_id: str, request: Request) -> Response:
    """Stream generated MP4 video bytes from Python with HTTP Range support for mobile & desktop browsers."""
    clean_id = re.sub(r"[^A-Za-z0-9_-]", "", task_id or "")[:64]
    job = _video_jobs.get(clean_id)
    raw = job.get("bytes") if isinstance(job, dict) else None
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise HTTPException(404, "ملف الفيديو غير موجود أو انتهت صلاحيته المؤقتة")
    mime = str(job.get("mime") or "video/mp4")
    total = len(raw)
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=1800",
        "Content-Disposition": f'inline; filename="{clean_id}.mp4"',
    }
    range_hdr = request.headers.get("range") or request.headers.get("Range") or ""
    m = re.match(r"^bytes=(\d*)-(\d*)$", range_hdr.strip())
    if m:
        start_s, end_s = m.group(1), m.group(2)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else total - 1
        if start_s == "" and end_s:
            suffix = int(end_s)
            start = max(0, total - suffix)
            end = total - 1
        end = min(end, total - 1)
        if 0 <= start <= end < total:
            chunk = bytes(raw[start : end + 1])
            return Response(
                content=chunk,
                status_code=206,
                media_type=mime,
                headers={
                    **base_headers,
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                },
            )
    return Response(
        content=bytes(raw),
        status_code=200,
        media_type=mime,
        headers={**base_headers, "Content-Length": str(total)},
    )


def _validate_public_http_or_https_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "يُسمح فقط بروابط HTTP/HTTPS العامة")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
        raise HTTPException(400, "عنوان المصدر غير مسموح")
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, parsed.port or default_port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HTTPException(400, "تعذر التحقق من نطاق الرابط") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(400, "عنوان الشبكة الداخلية غير مسموح")
    return raw_url.strip()


def _html_to_clean_text(raw_html: str) -> str:
    cleaned = re.sub(r"<(script|style|noscript|svg|iframe)\b[^>]*>[\s\S]*?</\1>", " ", raw_html, flags=re.I)
    cleaned = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html_mod.unescape(cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _parse_ddg_html(raw_html: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    link_re = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I)
    snip_re = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>([\s\S]*?)</(?:a|div|span)>', re.I)
    links = link_re.findall(raw_html)
    snips = [ _html_to_clean_text(s) for s in snip_re.findall(raw_html) ]
    for idx, (href, raw_title) in enumerate(links):
        url = html_mod.unescape(href).strip()
        if "uddg=" in url:
            m_u = re.search(r"[?&]uddg=([^&]+)", url)
            if m_u:
                url = unquote(m_u.group(1))
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        title = _html_to_clean_text(raw_title)
        snippet = snips[idx] if idx < len(snips) else ""
        if title:
            results.append({"title": title[:200], "url": url[:500], "snippet": snippet[:360]})
        if len(results) >= 8:
            break
    return results


@app.post("/api/tools/web-search")
async def tool_web_search_python(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Run web search directly from Python without browser CORS proxies."""
    _rate_limit("web-search:" + _sha(_client_ip(request)), 45, 300)
    query = str(payload.get("query") or "").strip()[:400]
    if not query:
        raise HTTPException(422, "عبارة البحث مطلوبة")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=_DEFAULT_OUTBOUND_HEADERS,
            )
            if r.status_code >= 400:
                raise HTTPException(502, f"خدمة البحث أعادت HTTP {r.status_code}")
            results = _parse_ddg_html(r.text or "")
            return {"ok": True, "query": query, "results": results}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر تنفيذ البحث عبر الخادم: {str(exc)[:220]}") from exc


@app.post("/api/tools/read-url")
async def tool_read_url_python(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Fetch and extract readable text from a public URL directly via Python (SSRF-protected, zero proxies)."""
    _rate_limit("read-url:" + _sha(_client_ip(request)), 45, 300)
    raw_url = str(payload.get("url") or "").strip()[:1000]
    safe_url = _validate_public_http_or_https_url(raw_url)
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            cur_url = safe_url
            r: httpx.Response | None = None
            for _ in range(4):
                r = await client.get(cur_url, headers=_DEFAULT_OUTBOUND_HEADERS)
                if r.status_code in {301, 302, 303, 307, 308} and r.headers.get("location"):
                    nxt = urljoin(cur_url, r.headers["location"])
                    cur_url = _validate_public_http_or_https_url(nxt)
                    continue
                break
            if r is None or r.status_code >= 400:
                raise HTTPException(502, f"فشل قراءة الرابط (HTTP {r.status_code if r else 502})")
            raw_text = (r.content or b"")[:512_000].decode("utf-8", errors="replace")
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" in ctype or raw_text.lstrip().startswith("<"):
                raw_text = _html_to_clean_text(raw_text)
            if len(raw_text) > 8000:
                raw_text = raw_text[:8000] + " ...[مقتطع]"
            return {"ok": True, "url": cur_url, "text": raw_text}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر جلب الصفحة عبر الخادم: {str(exc)[:220]}") from exc


# The secure router owns registration, login, logout, preferences, history and account health.
app.include_router(genisi_auth_router)
