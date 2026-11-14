from __future__ import annotations

import asyncio
import base64
import codecs
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
from urllib.parse import quote, unquote, urljoin, urlsplit

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
QWEN_MODELSCOPE_MIRROR_URL = os.getenv("QWEN_MODELSCOPE_MIRROR_URL", "https://qwen-qwen3-5-omni-offline-demo.ms.show").rstrip("/")
QWEN3_OMNI_FALLBACK_URL = os.getenv("QWEN3_OMNI_FALLBACK_URL", "https://qwen-qwen3-omni-demo.hf.space").rstrip("/")
QWEN25_OMNI_FALLBACK_URL = os.getenv("QWEN25_OMNI_FALLBACK_URL", "https://qwen-qwen2-5-omni-7b-demo.hf.space").rstrip("/")
OMEGATECH_AI_BASE_URL = os.getenv("OMEGATECH_AI_BASE_URL", "https://api.omegatech.app/api/ai").rstrip("/")
STEPAUDIO_MUSIC_SPACE_URL = os.getenv("STEPAUDIO_MUSIC_SPACE_URL", "https://stepfun-ai-stepaudio-3-music.hf.space").rstrip("/")
VOXCPM_SPACE_URL = os.getenv("VOXCPM_SPACE_URL", "https://openbmb-voxcpm-demo.hf.space").rstrip("/")
QWEN3_TTS_VD_SPACE_URL = os.getenv("QWEN3_TTS_VD_SPACE_URL", "https://qwen-qwen3-tts-voice-design.hf.space").rstrip("/")
QWEN3_TTS_VD_MS_URL = os.getenv("QWEN3_TTS_VD_MS_URL", "https://qwen-qwen3-tts-voice-design.ms.show").rstrip("/")
QWEN3_TTS_DEMO_SPACE_URL = os.getenv("QWEN3_TTS_DEMO_SPACE_URL", "https://qwen-qwen3-tts-demo.hf.space").rstrip("/")
QWEN3_TTS_DEMO_MS_URL = os.getenv("QWEN3_TTS_DEMO_MS_URL", "https://qwen-qwen3-tts-demo.ms.show").rstrip("/")
IMAGE_API_BASE_URL = os.getenv("IMAGE_API_BASE_URL", "https://prexzyapis.com").rstrip("/")
VIDEO_API_URL = os.getenv("VIDEO_API_URL", "https://api.omegatech.app/api/ai/Txt2video").strip()
_qwen_meta_cache: dict[str, Any] = {"ts": 0.0, "prefix": None, "endpoints": None}
_recent_qwen_uploads: dict[str, dict[str, Any]] = {}
_music_schema_cache: dict[str, Any] = {"ts": 0.0, "submit_props": None}
_voxcpm_meta_cache: dict[str, Any] = {"ts": 0.0, "prefix": None}
_voxcpm_sem = asyncio.Semaphore(2)
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
        "qwen_omni_space": QWEN_OMNI_SPACE_URL,
        "qwen_modelscope_mirror": QWEN_MODELSCOPE_MIRROR_URL,
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


_DEFAULT_OUTBOUND_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Accept": "*/*",
}


def _get_hf_token() -> str:
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACEHUB_API_TOKEN")
        or os.getenv("HF_API_TOKEN")
        or ""
    ).strip()


def _hf_space_headers(
    space_url: str,
    *,
    include_auth: bool = True,
    accept: str = "application/json",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    base = (space_url or "").rstrip("/")
    host = (urlsplit(base).hostname or "").lower()
    hdrs: dict[str, str] = {
        "User-Agent": "gradio_client/1.8.0 (Python/3.11; Genisi-Atlas)",
        "Accept": accept,
    }
    if host.endswith(".ms.show"):
        hdrs["Referer"] = "https://www.modelscope.cn/"
    if include_auth:
        tok = _get_hf_token()
        if tok and (host.endswith(".hf.space") or host.endswith("huggingface.co")):
            bad_ts = float(_qwen_meta_cache.get(f"bad_tok:{tok[:12]}") or 0.0)
            if time.time() - bad_ts > 900.0:
                hdrs["Authorization"] = f"Bearer {tok}"
    if extra:
        hdrs.update(extra)
    return hdrs


async def _ensure_modelscope_session(client: httpx.AsyncClient, space_url: str) -> None:
    """Warm up ModelScope Studio (.ms.show) cookies via GET so POST /gradio_api/call never hits a 302->GET redirect."""
    base = (space_url or "").rstrip("/")
    host = (urlsplit(base).hostname or "").lower()
    if not host.endswith(".ms.show"):
        return
    cache_key = f"ms_cookies:{base}"
    cached = _qwen_meta_cache.get(cache_key)
    now = time.time()
    if isinstance(cached, dict) and now - float(cached.get("ts") or 0.0) < 900.0:
        for ck_name, ck_val in (cached.get("cookies") or {}).items():
            client.cookies.set(ck_name, ck_val)
        return
    try:
        r_warm = await client.get(
            f"{base}/gradio_api/info",
            headers=_hf_space_headers(base, include_auth=False, accept="application/json"),
            timeout=10.0,
        )
        if r_warm.status_code < 400:
            _qwen_meta_cache[cache_key] = {
                "ts": now,
                "cookies": {k: v for k, v in client.cookies.items()},
            }
            _qwen_meta_cache[f"prefix:{base}"] = "/gradio_api"
    except Exception:
        pass


async def _hf_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    space_url: str,
    extra_headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send an HTTP request to a Gradio Space (HF or ModelScope) with auth & instant 429 mirror failover."""
    base = (space_url or "").rstrip("/")
    host = (urlsplit(base).hostname or "").lower()
    verb = method.upper()
    if host.endswith(".ms.show") and verb == "POST":
        await _ensure_modelscope_session(client, base)

    tok = _get_hf_token()
    can_auth = bool(
        tok
        and (host.endswith(".hf.space") or host.endswith("huggingface.co"))
        and (time.time() - float(_qwen_meta_cache.get(f"bad_tok:{tok[:12]}") or 0.0) > 900.0)
    )
    auth_modes = [True, False] if can_auth else [False]
    last_res: httpx.Response | None = None

    for use_auth in auth_modes:
        for retry_5xx in range(2):
            hdrs = _hf_space_headers(base, include_auth=use_auth, extra=extra_headers)
            if verb == "POST":
                res = await client.post(url, headers=hdrs, **kwargs)
            else:
                res = await client.get(url, headers=hdrs, **kwargs)
            last_res = res
            if res.status_code < 400:
                _qwen_meta_cache.pop(f"throttled:{base}", None)
                return res
            if res.status_code in {401, 403} and use_auth:
                if tok:
                    _qwen_meta_cache[f"bad_tok:{tok[:12]}"] = time.time()
                break
            if res.status_code == 429:
                # Immediately mark this mirror throttled so caller fails over to the next mirror without delay
                _qwen_meta_cache[f"throttled:{base}"] = time.time() + 75.0
                return res
            if res.status_code in {502, 503, 504} and retry_5xx == 0:
                await asyncio.sleep(0.28 + random.uniform(0.02, 0.14))
                continue
            return res
    assert last_res is not None
    return last_res


@app.post("/api/music/submit")
async def music_submit(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Direct Python gateway for stepfun-ai/StepAudio-3-Music using its official step-music contract."""
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
            upstream = await _hf_request_with_retry(
                client,
                "POST",
                f"{STEPAUDIO_MUSIC_SPACE_URL}/v1/audio/music/submit",
                space_url=STEPAUDIO_MUSIC_SPACE_URL,
                json=send_body,
            )
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
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip()[:160]
    if not task_id:
        raise HTTPException(422, "معرّف المهمة task_id مطلوب")
    try:
        timeout = httpx.Timeout(45.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            upstream = await _hf_request_with_retry(
                client,
                "POST",
                f"{STEPAUDIO_MUSIC_SPACE_URL}/v1/audio/music/query",
                space_url=STEPAUDIO_MUSIC_SPACE_URL,
                json={"task_id": task_id},
            )
            data = upstream.json() if upstream.content else {}
            if upstream.status_code >= 400:
                detail = _extract_stepaudio_error(data, upstream.status_code)
                raise HTTPException(upstream.status_code if upstream.status_code < 500 else 502, detail[:320])
            return _normalize_space_media_urls(data, STEPAUDIO_MUSIC_SPACE_URL)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر الاستعلام عن حالة الموسيقى: {str(exc)[:240]}") from exc


def _sniff_audio_mime_and_ext(raw: bytes, fallback_mime: str = "") -> tuple[str, str]:
    """Detect real audio container format from magic bytes so VoxCPM/Gradio never receives mislabeled extensions."""
    head = raw[:16] if raw else b""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "audio/mpeg", "mp3"
    if head.startswith(b"OggS"):
        return "audio/ogg", "ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac", "flac"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm", "webm"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    clean_fb = (fallback_mime or "").split(";")[0].strip().lower()
    ext_map = {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/wave": "wav",
        "audio/ogg": "ogg",
        "audio/opus": "opus",
        "audio/flac": "flac",
        "audio/webm": "webm",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
    }
    if clean_fb in ext_map:
        return ("audio/mpeg" if clean_fb == "audio/mp3" else clean_fb), ext_map[clean_fb]
    return "audio/wav", "wav"


def _clean_tts_text(raw: str, max_chars: int = 900) -> str:
    """Strip markdown blocks, URLs, and control characters while staying safely inside VoxCPM2's token budget."""
    txt = str(raw or "")
    txt = re.sub(r"```[\s\S]*?```", " ", txt)
    txt = re.sub(r"`([^`]+)`", r"\1", txt)
    txt = re.sub(r"https?://\S+", " ", txt)
    txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_chars].strip()


def _select_qwen3_tts_voice(control: str) -> str:
    """Map natural-language voice persona description to an expressive Qwen3-TTS preset."""
    low = (control or "").lower()
    if any(w in low for w in ("female", "woman", "girl", "sopran", "alto", "أنث", "امرأ", "فتاة", "نسائ")):
        if any(w in low for w in ("warm", "calm", "soft", "gentle", "serene", "هادئ", "دافئ")):
            return "Serena / 苏瑶"
        return "Cherry / 芊悦"
    if any(w in low for w in ("male", "man", "boy", "baritone", "tenor", "bass", "ذكر", "رجل", "شاب", "رجول")):
        return "Ethan / 晨煦"
    return "Cherry / 芊悦"


async def _run_gradio_tts_call(
    client: httpx.AsyncClient,
    *,
    space_url: str,
    endpoint: str,
    gradio_data: list[Any],
    call_timeout: float = 24.0,
    sse_timeout: float = 75.0,
) -> tuple[bytes, str]:
    """Execute a Gradio TTS call + SSE wait + audio download with HF_TOKEN 401 fallback and MIME sniffing."""
    base = space_url.rstrip("/")
    prefix_key = f"tts_prefix:{base}"
    prefix = str(_voxcpm_meta_cache.get(prefix_key) if prefix_key in _voxcpm_meta_cache else "/gradio_api")

    r_call = await _hf_request_with_retry(
        client,
        "POST",
        f"{base}{prefix}/call/{endpoint}",
        space_url=base,
        json={"data": gradio_data},
        timeout=call_timeout,
    )
    if r_call.status_code == 404 and prefix == "/gradio_api":
        prefix = ""
        r_call = await _hf_request_with_retry(
            client,
            "POST",
            f"{base}/call/{endpoint}",
            space_url=base,
            json={"data": gradio_data},
            timeout=call_timeout,
        )
    _voxcpm_meta_cache[prefix_key] = prefix
    if base == VOXCPM_SPACE_URL:
        _voxcpm_meta_cache["prefix"] = prefix
        _voxcpm_meta_cache["ts"] = time.time()

    if r_call.status_code == 429:
        _voxcpm_meta_cache[f"throttled:{base}"] = time.time() + 45.0
        raise RuntimeError(f"{base} HTTP 429")
    if r_call.status_code >= 400:
        raise RuntimeError(f"{base} HTTP {r_call.status_code}: {r_call.text[:160]}")

    call_json = r_call.json() if r_call.content else {}
    event_id = str(call_json.get("event_id") or "").strip()
    if not event_id:
        raise RuntimeError(f"{base}: missing event_id")

    sse_url = f"{base}{prefix}/call/{endpoint}/{event_id}"
    host = (urlsplit(base).hostname or "").lower()
    tok = _get_hf_token()
    can_auth = bool(
        tok
        and (host.endswith(".hf.space") or host.endswith("huggingface.co"))
        and (time.time() - float(_qwen_meta_cache.get(f"bad_tok:{tok[:12]}") or 0.0) > 900.0)
    )
    sse_auth_modes = [True, False] if can_auth else [False]

    audio_url = ""
    for use_auth in sse_auth_modes:
        cur_event = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        retried_unauth = False
        async with client.stream(
            "GET",
            sse_url,
            headers=_hf_space_headers(base, include_auth=use_auth, accept="text/event-stream"),
            timeout=sse_timeout,
        ) as sse:
            if sse.status_code in {401, 403} and use_auth:
                if tok:
                    _qwen_meta_cache[f"bad_tok:{tok[:12]}"] = time.time()
                retried_unauth = True
            elif sse.status_code >= 400:
                if sse.status_code == 429:
                    _voxcpm_meta_cache[f"throttled:{base}"] = time.time() + 45.0
                err_body = (await sse.aread()).decode("utf-8", errors="replace")[:180]
                raise RuntimeError(f"{base} SSE HTTP {sse.status_code}: {err_body}")
            else:
                async for raw_chunk in sse.aiter_bytes():
                    buf += decoder.decode(raw_chunk, final=False).replace("\r\n", "\n")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip("\r")
                        if line.startswith("event:"):
                            cur_event = line[6:].strip()
                        elif line.startswith("data:"):
                            raw_data = line[5:].strip()
                            if cur_event == "error":
                                low_err = raw_data.lower()
                                if any(k in low_err for k in ("busy", "queue", "concurrent", "429", "unstable")):
                                    _voxcpm_meta_cache[f"throttled:{base}"] = time.time() + 35.0
                                raise RuntimeError(f"{base} stream error: {raw_data[:220]}")
                            if cur_event == "complete" and raw_data and raw_data != "null":
                                arr = json.loads(raw_data)
                                first = arr[0] if isinstance(arr, list) and arr else arr
                                if isinstance(first, dict):
                                    u = str(first.get("url") or "").strip()
                                    p = str(first.get("path") or "").strip()
                                    if u:
                                        audio_url = u if u.startswith("http") else urljoin(base + "/", u.lstrip("/"))
                                    elif p:
                                        audio_url = p if p.startswith("http") else f"{base}{prefix}/file={p}"
                                elif isinstance(first, str) and first.strip():
                                    s_val = first.strip()
                                    audio_url = s_val if s_val.startswith("http") else f"{base}{prefix}/file={s_val}"
                                break
                    if audio_url:
                        break
        if retried_unauth:
            continue
        break

    if not audio_url:
        raise RuntimeError(f"{base}: لم يُرجع المحرك ملفاً صوتياً")

    r_audio = await _hf_request_with_retry(
        client,
        "GET",
        audio_url,
        space_url=base,
        timeout=45.0,
    )
    if r_audio.status_code >= 400 or len(r_audio.content or b"") <= 200:
        raise RuntimeError(f"{base}: تعذر تنزيل المقطع الصوتي ({r_audio.status_code})")

    raw_hdr_mime = (r_audio.headers.get("content-type") or "").split(";")[0].strip().lower()
    sniffed_mime, _ = _sniff_audio_mime_and_ext(r_audio.content, raw_hdr_mime)
    media_type = sniffed_mime if sniffed_mime.startswith("audio/") else (raw_hdr_mime if raw_hdr_mime.startswith("audio/") else "audio/wav")
    return r_audio.content, media_type


@app.post("/api/voice/tts")
async def voice_tts_proxy(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Resilient Python TTS gateway: primary VoxCPM2 + automatic Voice-Design/Qwen3-TTS multi-Space failover."""
    text = _clean_tts_text(str(payload.get("text") or ""), max_chars=900)
    if not text:
        raise HTTPException(422, "النص الصوتي فارغ")
    control = str(payload.get("control_instruction") or "").strip()[:400]
    default_control = control or "Warm, clear, natural conversational voice, steady moderate pace."
    try:
        cfg_value = max(1.0, min(3.0, float(payload.get("cfg_value", 2.0))))
    except (TypeError, ValueError):
        cfg_value = 2.0
    raw_norm = payload.get("do_normalize") if "do_normalize" in payload else payload.get("normalize", False)
    do_normalize = bool(raw_norm)
    if re.search(r"[\u0600-\u06FF\u0400-\u04FF\u3040-\u30FF\uAC00-\uD7AF]", text):
        do_normalize = False

    ref_b64 = _strip_data_uri_prefix(payload.get("reference_audio_base64") or "")
    ref_mime_hint = str(payload.get("reference_audio_mime") or "").strip()

    errors: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=95.0, follow_redirects=True) as client:
            now = time.time()
            voxcpm_throttled = now < float(_voxcpm_meta_cache.get(f"throttled:{VOXCPM_SPACE_URL}") or 0.0)

            # 1) Primary engine: openbmb/VoxCPM-Demo (concurrency-guarded per IP + automatic no-ref retry)
            if VOXCPM_SPACE_URL and not voxcpm_throttled:
                async with _voxcpm_sem:
                    prefix = str(_voxcpm_meta_cache.get("prefix") or "/gradio_api")
                    ref_file_data = None
                    if ref_b64 and len(ref_b64) < 8_000_000:
                        try:
                            ref_bytes = base64.b64decode(ref_b64)
                            if len(ref_bytes) > 512:
                                ref_mime, ref_ext = _sniff_audio_mime_and_ext(ref_bytes, ref_mime_hint)
                                ref_filename = f"voice-anchor.{ref_ext}"
                                up_res = await _hf_request_with_retry(
                                    client,
                                    "POST",
                                    f"{VOXCPM_SPACE_URL}{prefix}/upload",
                                    space_url=VOXCPM_SPACE_URL,
                                    files={"files": (ref_filename, ref_bytes, ref_mime)},
                                    timeout=22.0,
                                )
                                if up_res.status_code == 404 and prefix == "/gradio_api":
                                    prefix = ""
                                    up_res = await _hf_request_with_retry(
                                        client,
                                        "POST",
                                        f"{VOXCPM_SPACE_URL}/upload",
                                        space_url=VOXCPM_SPACE_URL,
                                        files={"files": (ref_filename, ref_bytes, ref_mime)},
                                        timeout=22.0,
                                    )
                                if up_res.status_code == 200:
                                    _voxcpm_meta_cache["prefix"] = prefix
                                    _voxcpm_meta_cache[f"tts_prefix:{VOXCPM_SPACE_URL}"] = prefix
                                    up_arr = up_res.json()
                                    if isinstance(up_arr, list) and up_arr:
                                        up_path = up_arr[0] if isinstance(up_arr[0], str) else (up_arr[0] or {}).get("path", "")
                                        if up_path:
                                            ref_file_data = {
                                                "path": up_path,
                                                "orig_name": ref_filename,
                                                "mime_type": ref_mime,
                                                "meta": {"_type": "gradio.FileData"},
                                            }
                        except Exception:
                            ref_file_data = None

                    vox_attempts: list[Any] = [ref_file_data] if ref_file_data else [None]
                    if ref_file_data is not None:
                        # If reference cloning fails (e.g. clip <1s / >30s / token budget), retry pure Voice Design mode
                        vox_attempts.append(None)

                    for attempt_ref in vox_attempts:
                        gradio_data = [
                            text,
                            "" if attempt_ref else control,
                            attempt_ref,
                            False,
                            "",
                            cfg_value,
                            do_normalize,
                            False,
                        ]
                        try:
                            audio_bytes, media_type = await _run_gradio_tts_call(
                                client,
                                space_url=VOXCPM_SPACE_URL,
                                endpoint="generate",
                                gradio_data=gradio_data,
                            )
                            return Response(content=audio_bytes, media_type=media_type, headers={"Cache-Control": "no-store"})
                        except Exception as exc:
                            errors.append(str(exc)[:180])
                            if time.time() < float(_voxcpm_meta_cache.get(f"throttled:{VOXCPM_SPACE_URL}") or 0.0):
                                break

            # 2) Official Qwen3-TTS failover mirrors (Voice-Design HF + MS, then Qwen3-TTS-Demo HF + MS)
            qwen_voice = _select_qwen3_tts_voice(default_control)
            tts_fallbacks: list[tuple[str, str, list[Any]]] = [
                (QWEN3_TTS_VD_SPACE_URL, "tts_interface", [text[:600], default_control]),
                (QWEN3_TTS_VD_MS_URL, "tts_interface", [text[:600], default_control]),
                (QWEN3_TTS_DEMO_SPACE_URL, "tts_interface", [text[:600], qwen_voice, "Auto / 自动"]),
                (QWEN3_TTS_DEMO_MS_URL, "tts_interface", [text[:600], qwen_voice, "Auto / 自动"]),
            ]
            healthy_fb = [
                item for item in tts_fallbacks
                if item[0] and time.time() >= float(_voxcpm_meta_cache.get(f"throttled:{item[0]}") or 0.0)
            ]
            throttled_fb = [item for item in tts_fallbacks if item[0] and item not in healthy_fb]

            for fb_url, fb_ep, fb_args in healthy_fb + throttled_fb:
                try:
                    audio_bytes, media_type = await _run_gradio_tts_call(
                        client,
                        space_url=fb_url,
                        endpoint=fb_ep,
                        gradio_data=fb_args,
                        call_timeout=20.0,
                        sse_timeout=55.0,
                    )
                    return Response(content=audio_bytes, media_type=media_type, headers={"Cache-Control": "no-store"})
                except Exception as exc:
                    errors.append(str(exc)[:180])
                    continue

            err_tail = errors[-1] if errors else "لم يُرجع محرك الصوت مقطعاً صالحاً"
            raise HTTPException(502, f"تعذر توليد الصوت حالياً: {err_tail[:220]}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر توليد الصوت عبر الخادم: {str(exc)[:240]}") from exc


# ═══════════════════════════════════════════════════════════════════════════════
# Direct Python Model & Tool Gateways (Zero Third-Party Proxies, Native CORS)
# ═══════════════════════════════════════════════════════════════════════════════

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


def _qwen_gradio_candidates() -> list[str]:
    base_list: list[str] = []
    for raw in [
        QWEN_OMNI_SPACE_URL,
        QWEN_MODELSCOPE_MIRROR_URL,
        QWEN3_OMNI_FALLBACK_URL,
        QWEN25_OMNI_FALLBACK_URL,
        *[u.strip() for u in os.getenv("QWEN_EXTRA_SPACE_URLS", "").split(",") if u.strip()],
    ]:
        cleaned = (raw or "").strip().rstrip("/")
        if cleaned.startswith(("http://", "https://")) and cleaned not in base_list:
            base_list.append(cleaned)

    now = time.time()
    healthy = [u for u in base_list if now >= float(_qwen_meta_cache.get(f"throttled:{u}") or 0.0)]
    throttled = [u for u in base_list if u not in healthy]
    pref = str(_qwen_meta_cache.get("preferred_space") or "")
    pref_ts = float(_qwen_meta_cache.get("preferred_ts") or 0.0)
    if pref and pref in healthy and (now - pref_ts) < 240.0:
        healthy = [pref] + [u for u in healthy if u != pref]
    return healthy + throttled


def _prune_qwen_uploads() -> None:
    now = time.time()
    expired = [k for k, v in _recent_qwen_uploads.items() if now - float(v.get("ts") or 0) > 900]
    for k in expired:
        _recent_qwen_uploads.pop(k, None)
    items = sorted(_recent_qwen_uploads.items(), key=lambda kv: float(kv[1].get("ts") or 0), reverse=True)
    total_bytes = 0
    for idx, (k, rec) in enumerate(items):
        sz = len(rec.get("bytes") or b"")
        if idx >= 12 or total_bytes + sz > 30 * 1024 * 1024:
            _recent_qwen_uploads.pop(k, None)
        else:
            total_bytes += sz


def _cache_qwen_upload(path_key: str, space_url: str, name: str, mime: str, raw: bytes, public_url: str) -> None:
    _prune_qwen_uploads()
    if len(raw) <= 25 * 1024 * 1024:
        _recent_qwen_uploads[path_key] = {
            "ts": time.time(),
            "space": space_url,
            "name": name,
            "mime": mime,
            "bytes": raw,
            "public_url": public_url,
        }


async def _upload_to_gradio_space(
    client: httpx.AsyncClient,
    space_url: str,
    raw: bytes,
    safe_name: str,
    safe_mime: str,
) -> tuple[str, str] | None:
    prefix = str(_qwen_meta_cache.get(f"prefix:{space_url}") or "/gradio_api")
    up_res = await _hf_request_with_retry(
        client,
        "POST",
        f"{space_url}{prefix}/upload",
        space_url=space_url,
        files={"files": (safe_name, raw, safe_mime)},
        timeout=45.0,
    )
    if up_res.status_code == 404 and prefix == "/gradio_api":
        prefix = ""
        up_res = await _hf_request_with_retry(
            client,
            "POST",
            f"{space_url}/upload",
            space_url=space_url,
            files={"files": (safe_name, raw, safe_mime)},
            timeout=45.0,
        )
    if up_res.status_code >= 400:
        return None
    arr = up_res.json() if up_res.content else []
    if not isinstance(arr, list) or not arr:
        return None
    remote_path = arr[0] if isinstance(arr[0], str) else str((arr[0] or {}).get("path") or "")
    if not remote_path:
        return None
    _qwen_meta_cache[f"prefix:{space_url}"] = prefix
    public_url = f"{space_url}{prefix}/file={remote_path}"
    return remote_path, public_url


async def _upload_to_public_temp_host(
    client: httpx.AsyncClient,
    raw: bytes,
    safe_name: str,
    safe_mime: str,
) -> str:
    # 1) Try authenticated Gradio Space uploads first
    for space_url in _qwen_gradio_candidates():
        try:
            got = await _upload_to_gradio_space(client, space_url, raw, safe_name, safe_mime)
            if got:
                return got[1]
        except Exception:
            continue

    # 2) Fallback temporary public file hosts (for image edit or cross-space FileData URLs)
    try:
        r_tmp = await client.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": (safe_name, raw, safe_mime)},
            headers=_DEFAULT_OUTBOUND_HEADERS,
            timeout=35.0,
        )
        if r_tmp.status_code < 400:
            j_tmp = r_tmp.json()
            u_tmp = str(((j_tmp or {}).get("data") or {}).get("url") or "").strip()
            if u_tmp.startswith("http"):
                return re.sub(r"^http://", "https://", u_tmp.replace("tmpfiles.org/", "tmpfiles.org/dl/"), flags=re.I)
    except Exception:
        pass

    try:
        r_uguu = await client.post(
            "https://uguu.se/upload.php",
            files={"files[]": (safe_name, raw, safe_mime)},
            headers=_DEFAULT_OUTBOUND_HEADERS,
            timeout=35.0,
        )
        if r_uguu.status_code < 400:
            j_uguu = r_uguu.json()
            files_arr = (j_uguu or {}).get("files") or []
            if files_arr and isinstance(files_arr[0], dict):
                u_uguu = str(files_arr[0].get("url") or "").strip()
                if u_uguu.startswith("https://"):
                    return u_uguu
    except Exception:
        pass

    try:
        r_cat = await client.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": (safe_name, raw, safe_mime)},
            headers=_DEFAULT_OUTBOUND_HEADERS,
            timeout=35.0,
        )
        txt_cat = (r_cat.text or "").strip()
        if r_cat.status_code < 400 and txt_cat.startswith("https://"):
            return txt_cat
    except Exception:
        pass

    return ""


async def _remap_file_data_for_space(
    client: httpx.AsyncClient,
    fd: Any,
    target_space: str,
) -> Any:
    if not isinstance(fd, dict):
        return fd
    if "video" in fd and isinstance(fd.get("video"), dict):
        remapped_inner = await _remap_file_data_for_space(client, fd["video"], target_space)
        return {"video": remapped_inner, "subtitles": fd.get("subtitles")}
    orig_path = str(fd.get("path") or "").strip()
    if not orig_path:
        return fd
    rec = _recent_qwen_uploads.get(orig_path)
    if not rec or rec.get("space") == target_space:
        return fd
    try:
        reup = await _upload_to_gradio_space(
            client,
            target_space,
            rec["bytes"],
            str(rec.get("name") or "upload.bin"),
            str(rec.get("mime") or "application/octet-stream"),
        )
        if reup:
            return {"path": reup[0], "meta": {"_type": "gradio.FileData"}}
    except Exception:
        pass
    pub_url = str(rec.get("public_url") or "")
    if pub_url.startswith("https://"):
        return {"path": pub_url, "url": pub_url, "meta": {"_type": "gradio.FileData"}}
    return fd


def _normalize_qwen_chat_predict_args(data: list[Any]) -> list[Any]:
    """Normalize the 9-arg Qwen3.5-Omni-Offline-Demo chat_predict payload to match app.py."""
    if len(data) != 9:
        return list(data)
    text = str(data[0]) if data[0] is not None else ""
    audio_fd = data[1] if isinstance(data[1], dict) else None
    image_fd = data[2] if isinstance(data[2], dict) else None
    video_fd: dict[str, Any] | None = None
    if isinstance(data[3], dict):
        if isinstance(data[3].get("video"), dict):
            video_fd = {"video": data[3]["video"], "subtitles": data[3].get("subtitles")}
        elif data[3].get("path"):
            video_fd = {"video": data[3], "subtitles": None}

    clean_hist: list[dict[str, str]] = []
    if isinstance(data[4], list):
        for item in data[4]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            cnt = item.get("content")
            if role in {"user", "assistant"} and isinstance(cnt, str) and cnt != "":
                clean_hist.append({"role": role, "content": cnt})

    sys_prompt = str(data[5]) if data[5] is not None else ""
    try:
        temp = round(max(0.1, min(2.0, float(data[6] if data[6] is not None else 0.7))), 3)
    except Exception:
        temp = 0.7
    try:
        top_p = round(max(0.05, min(1.0, float(data[7] if data[7] is not None else 0.8))), 3)
    except Exception:
        top_p = 0.8
    try:
        top_k = int(max(1, min(100, round(float(data[8] if data[8] is not None else 20)))))
    except Exception:
        top_k = 20

    return [text, audio_fd, image_fd, video_fd, clean_hist, sys_prompt, temp, top_p, top_k]


async def _adapt_qwen_data_for_space(
    client: httpx.AsyncClient,
    data: list[Any],
    space_url: str,
) -> list[Any]:
    norm = _normalize_qwen_chat_predict_args(data)
    if len(norm) < 9:
        return norm
    audio_fd = await _remap_file_data_for_space(client, norm[1], space_url)
    image_fd = await _remap_file_data_for_space(client, norm[2], space_url)
    video_fd = await _remap_file_data_for_space(client, norm[3], space_url)
    if (
        space_url == QWEN3_OMNI_FALLBACK_URL
        and space_url not in {QWEN_OMNI_SPACE_URL, QWEN_MODELSCOPE_MIRROR_URL}
        and len(norm) == 9
    ):
        # Qwen/Qwen3-Omni-Demo /chat_predict signature (12 positional args):
        # [text, audio, image, video, history, system_prompt, voice_choice, temperature, top_p, top_k, return_audio, enable_thinking]
        return [
            norm[0],
            audio_fd,
            image_fd,
            video_fd,
            norm[4],
            norm[5],
            "Cherry / 芊悦",
            norm[6],
            norm[7],
            norm[8],
            False,
            False,
        ]
    if (
        space_url == QWEN25_OMNI_FALLBACK_URL
        and space_url not in {QWEN_OMNI_SPACE_URL, QWEN_MODELSCOPE_MIRROR_URL, QWEN3_OMNI_FALLBACK_URL}
        and len(norm) == 9
    ):
        # Qwen/Qwen2.5-Omni-7B-Demo /chat_predict signature (7 positional args):
        # [text, audio, image, video, history, system_prompt, voice_choice]
        return [
            norm[0],
            audio_fd,
            image_fd,
            video_fd,
            norm[4],
            norm[5],
            "Cherry",
        ]
    return [
        norm[0],
        audio_fd,
        image_fd,
        video_fd,
        *norm[4:],
    ]


async def _start_qwen_gradio_call(
    client: httpx.AsyncClient,
    data: list[Any],
    endpoint: str,
    space_url: str,
    *,
    call_timeout: float = 28.0,
) -> tuple[str, str]:
    chosen = endpoint.strip().strip("/") or "chat_predict"
    adapted_data = await _adapt_qwen_data_for_space(client, data, space_url)
    prefix = str(_qwen_meta_cache.get(f"prefix:{space_url}") or "/gradio_api")

    r_call = await _hf_request_with_retry(
        client,
        "POST",
        f"{space_url}{prefix}/call/{chosen}",
        space_url=space_url,
        json={"data": adapted_data},
        timeout=call_timeout,
    )
    if r_call.status_code == 404 and prefix == "/gradio_api":
        prefix = ""
        r_call = await _hf_request_with_retry(
            client,
            "POST",
            f"{space_url}/call/{chosen}",
            space_url=space_url,
            json={"data": adapted_data},
            timeout=call_timeout,
        )
    if r_call.status_code >= 400:
        raise RuntimeError(f"{space_url} HTTP {r_call.status_code}: {r_call.text[:160]}")
    started = r_call.json() if r_call.content else {}
    event_id = str(started.get("event_id") or "").strip()
    if not event_id:
        raise RuntimeError(f"{space_url}: missing event_id")
    _qwen_meta_cache[f"prefix:{space_url}"] = prefix
    return f"{space_url}{prefix}/call/{chosen}", event_id


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


def _extract_openai_chat_reply(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str) and msg["content"].strip():
            return msg["content"].strip()
    return ""


async def _dashscope_qwen35_direct(
    client: httpx.AsyncClient,
    data: list[Any],
) -> str | None:
    """Optional zero-Gradio direct DashScope qwen3.5-omni-plus path when DASHSCOPE_API_KEY is configured."""
    api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    if not api_key or any(bool(data[idx]) for idx in (1, 2, 3) if len(data) > idx):
        return None
    norm = _normalize_qwen_chat_predict_args(data)
    user_text = str(norm[0] or "")
    history = norm[4] if len(norm) > 4 and isinstance(norm[4], list) else []
    sys_prompt = str(norm[5] or "")
    temp = float(norm[6]) if len(norm) > 6 else 0.7
    top_p = float(norm[7]) if len(norm) > 7 else 0.8
    top_k = int(norm[8]) if len(norm) > 8 else 20

    messages: list[dict[str, Any]] = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    for h in history:
        messages.append({"role": h["role"], "content": [{"type": "text", "text": h["content"]}]})
    if user_text:
        messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
    if not messages:
        return None

    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    res = await client.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "model": os.getenv("DASHSCOPE_OMNI_MODEL", "qwen3.5-omni-plus"),
            "messages": messages,
            "modalities": ["text"],
            "temperature": temp,
            "top_p": top_p,
            "extra_body": {"top_k": top_k},
        },
        timeout=90.0,
    )
    if res.status_code >= 400:
        return None
    return _extract_openai_chat_reply(res.json()) or None


@app.get("/api/models/qwen/info")
async def qwen_model_info(request: Request, endpoint: str = "chat_predict") -> dict[str, Any]:
    """Return Qwen3.5-Omni gateway readiness immediately without burning upstream HF rate limits."""
    chosen = endpoint.strip().strip("/") or "chat_predict"
    active_space = str(_qwen_meta_cache.get("preferred_space") or QWEN_OMNI_SPACE_URL)
    prefix = str(_qwen_meta_cache.get(f"prefix:{active_space}") or "/gradio_api")
    return {
        "ok": True,
        "space": active_space,
        "prefix": prefix,
        "endpoint": chosen,
        "schema": {},
    }


@app.post("/api/models/qwen/upload")
async def qwen_model_upload(
    request: Request,
    name: str = "upload.bin",
    mime: str = "application/octet-stream",
) -> dict[str, Any]:
    """Upload a multimodal attachment directly from Python with multi-Space & public host failover."""
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "الملف المرفق فارغ")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(413, "حجم المرفق يتجاوز الحد المسموح (25MB)")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "upload.bin").name)[:96] or "upload.bin"
    safe_mime = (mime or request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()[:120]

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            for space_url in _qwen_gradio_candidates():
                try:
                    got = await _upload_to_gradio_space(client, space_url, raw, safe_name, safe_mime)
                    if got:
                        remote_path, public_url = got
                        _cache_qwen_upload(remote_path, space_url, safe_name, safe_mime, raw, public_url)
                        return {
                            "ok": True,
                            "path": remote_path,
                            "url": public_url,
                            "file_data": {"path": remote_path, "meta": {"_type": "gradio.FileData"}},
                        }
                except Exception:
                    continue

            public_url = await _upload_to_public_temp_host(client, raw, safe_name, safe_mime)
            if public_url:
                _cache_qwen_upload(public_url, "", safe_name, safe_mime, raw, public_url)
                return {
                    "ok": True,
                    "path": public_url,
                    "url": public_url,
                    "file_data": {"path": public_url, "url": public_url, "meta": {"_type": "gradio.FileData"}},
                }
            raise HTTPException(502, "تعذر رفع المرفق إلى جميع خوادم الوسائط حالياً")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"تعذر رفع المرفق عبر الخادم: {str(exc)[:220]}") from exc


@app.post("/api/models/qwen/stream")
async def qwen_model_stream(request: Request, payload: dict[str, Any] = Body(...)) -> StreamingResponse:
    """Stream Qwen3.5-Omni-Offline-Demo SSE via Python across HF + ModelScope mirrors with UTF-8 safety & failover."""
    data = payload.get("data")
    if not isinstance(data, list):
        raise HTTPException(422, "الحقل data يجب أن يكون مصفوفة")
    endpoint = str(payload.get("endpoint") or "chat_predict").strip().strip("/") or "chat_predict"

    async def _sse_generator():
        # Immediately flush an SSE comment so reverse proxies (HF Spaces) see 200 OK + active bytes in <5ms
        yield b": connected\n\n"
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0), follow_redirects=True) as client:
            # Optional direct DashScope qwen3.5-omni-plus when DASHSCOPE_API_KEY is configured
            if os.getenv("DASHSCOPE_API_KEY", "").strip():
                try:
                    direct_reply = await _dashscope_qwen35_direct(client, data)
                    if direct_reply:
                        norm = _normalize_qwen_chat_predict_args(data)
                        in_hist = list(norm[4]) if len(norm) > 4 and isinstance(norm[4], list) else []
                        if norm[0]:
                            in_hist.append({"role": "user", "content": str(norm[0])})
                        in_hist.append({"role": "assistant", "content": direct_reply})
                        frame = json.dumps([None, None, None, None, in_hist], ensure_ascii=False)
                        yield f"event: generating\ndata: {frame}\n\nevent: complete\ndata: {frame}\n\n".encode("utf-8")
                        return
                except Exception as exc:
                    errors.append(f"dashscope: {str(exc)[:140]}")

            for space_url in _qwen_gradio_candidates():
                try:
                    call_url, event_id = await _start_qwen_gradio_call(client, data, endpoint, space_url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors.append(str(exc)[:180])
                    continue

                emitted_text = False
                completed_ok = False
                try:
                    async with client.stream(
                        "GET",
                        f"{call_url}/{event_id}",
                        headers=_hf_space_headers(space_url, accept="text/event-stream"),
                        timeout=180.0,
                    ) as upstream:
                        if upstream.status_code >= 400:
                            if upstream.status_code == 429:
                                _qwen_meta_cache[f"throttled:{space_url}"] = time.time() + 75.0
                            err_txt = (await upstream.aread()).decode("utf-8", errors="replace")[:180]
                            errors.append(f"{space_url} SSE HTTP {upstream.status_code}: {err_txt}")
                            continue

                        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                        buf = ""
                        aborted_upstream = False
                        chunk_iter = upstream.aiter_bytes().__aiter__()
                        idle_ticks = 0

                        while True:
                            try:
                                raw_chunk = await asyncio.wait_for(chunk_iter.__anext__(), timeout=15.0)
                                idle_ticks = 0
                                eof = False
                            except asyncio.TimeoutError:
                                idle_ticks += 1
                                if idle_ticks >= 11:
                                    errors.append(f"{space_url}: SSE stream timed out")
                                    aborted_upstream = True
                                    break
                                yield b": keepalive\n\n"
                                continue
                            except StopAsyncIteration:
                                raw_chunk = b""
                                eof = True

                            decoded = decoder.decode(raw_chunk, final=eof)
                            if decoded:
                                buf += decoded.replace("\r\n", "\n")
                            if eof and buf.strip() and not buf.endswith("\n\n"):
                                buf += "\n\n"

                            while "\n\n" in buf:
                                block, buf = buf.split("\n\n", 1)
                                stripped = block.strip()
                                if not stripped:
                                    continue
                                ev_name = ""
                                data_lines: list[str] = []
                                for ln in stripped.split("\n"):
                                    if ln.startswith("event:"):
                                        ev_name = ln[6:].strip()
                                    elif ln.startswith("data:"):
                                        data_lines.append(ln[5:].strip())
                                raw_ev_data = "\n".join(data_lines)
                                if ev_name == "error":
                                    if not emitted_text:
                                        errors.append(f"{space_url} stream error: {raw_ev_data[:160]}")
                                        aborted_upstream = True
                                        break
                                    yield (stripped + "\n\n").encode("utf-8")
                                    return
                                if ev_name in {"generating", "complete"}:
                                    extracted: str | None = None
                                    if raw_ev_data and raw_ev_data != "null":
                                        try:
                                            extracted = _extract_qwen_assistant_text(json.loads(raw_ev_data))
                                        except Exception:
                                            extracted = None
                                    if extracted:
                                        emitted_text = True
                                    if ev_name == "generating":
                                        if emitted_text:
                                            yield (stripped + "\n\n").encode("utf-8")
                                        continue
                                    # ev_name == "complete": never forward an empty completion before emitted_text
                                    if not emitted_text:
                                        errors.append(f"{space_url}: empty assistant completion")
                                        aborted_upstream = True
                                        break
                                    completed_ok = True
                                    _qwen_meta_cache["preferred_space"] = space_url
                                    _qwen_meta_cache["preferred_ts"] = time.time()
                                    yield (stripped + "\n\n").encode("utf-8")
                                    return
                                # Forward heartbeat/process_starts comments harmlessly, never premature completion
                                if ev_name == "heartbeat":
                                    yield b": heartbeat\n\n"

                            if aborted_upstream or eof:
                                break

                        if emitted_text or completed_ok:
                            _qwen_meta_cache["preferred_space"] = space_url
                            _qwen_meta_cache["preferred_ts"] = time.time()
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if emitted_text:
                        yield f"event: error\ndata: {json.dumps({'message': str(exc)[:220]}, ensure_ascii=False)}\n\n".encode("utf-8")
                        return
                    errors.append(f"{space_url}: {str(exc)[:160]}")
                    continue

        err_summary = errors[-1] if errors else "تعذر الاتصال بمحركات Qwen"
        yield f"event: error\ndata: {json.dumps({'message': err_summary}, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/models/qwen/once")
async def qwen_model_once(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Single-turn non-streaming Qwen3.5-Omni call via Python across HF + ModelScope mirrors with failover."""
    data = payload.get("data")
    if not isinstance(data, list):
        raise HTTPException(422, "الحقل data يجب أن يكون مصفوفة")
    endpoint = str(payload.get("endpoint") or "chat_predict").strip().strip("/") or "chat_predict"

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=80.0, follow_redirects=True) as client:
        if os.getenv("DASHSCOPE_API_KEY", "").strip():
            try:
                direct_reply = await _dashscope_qwen35_direct(client, data)
                if direct_reply:
                    return {"ok": True, "text": direct_reply}
            except Exception as exc:
                errors.append(f"dashscope: {str(exc)[:140]}")

        for space_url in _qwen_gradio_candidates():
            try:
                call_url, event_id = await _start_qwen_gradio_call(
                    client, data, endpoint, space_url, call_timeout=22.0
                )
                latest = ""
                cur_event = ""
                async with client.stream(
                    "GET",
                    f"{call_url}/{event_id}",
                    headers=_hf_space_headers(space_url, accept="text/event-stream"),
                    timeout=65.0,
                ) as sse:
                    if sse.status_code >= 400:
                        if sse.status_code == 429:
                            _qwen_meta_cache[f"throttled:{space_url}"] = time.time() + 75.0
                        errors.append(f"{space_url} HTTP {sse.status_code}")
                        continue
                    failed_sse = False
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    buf = ""
                    async for raw_chunk in sse.aiter_bytes():
                        buf += decoder.decode(raw_chunk, final=False).replace("\r\n", "\n")
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            if line.startswith("event:"):
                                cur_event = line[6:].strip()
                            elif line.startswith("data:"):
                                raw_data = line[5:].strip()
                                if cur_event == "error":
                                    errors.append(f"{space_url}: {raw_data[:160]}")
                                    failed_sse = True
                                    break
                                if cur_event in {"generating", "complete"} and raw_data and raw_data != "null":
                                    try:
                                        extracted = _extract_qwen_assistant_text(json.loads(raw_data))
                                        if extracted is not None:
                                            latest = extracted
                                    except Exception:
                                        pass
                        if failed_sse or (cur_event == "complete" and latest):
                            break
                    if not failed_sse and latest:
                        _qwen_meta_cache["preferred_space"] = space_url
                        _qwen_meta_cache["preferred_ts"] = time.time()
                        return {"ok": True, "text": latest}
                    if not failed_sse and not latest:
                        errors.append(f"{space_url}: empty assistant completion")
            except Exception as exc:
                errors.append(str(exc)[:160])
                continue

    raise HTTPException(502, f"تعذر إتمام الطلب عبر محركات Qwen: {(errors[-1] if errors else 'unknown')[:220]}")


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

    # Handle JSON wrapping a data URI or HTTPS image URL (including nested data.imageUrl / data.image)
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
            if isinstance(val, dict):
                for sub_k in ("imageUrl", "image_url", "image", "url", "output", "result"):
                    sub_v = val.get(sub_k)
                    if isinstance(sub_v, str) and sub_v.strip():
                        cand_str = sub_v.strip()
                        break
                if cand_str:
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
    """Generate an image directly from Python with multi-provider resilience (zero proxies)."""
    prompt = str(payload.get("prompt") or "").strip()[:1800]
    if not prompt:
        raise HTTPException(422, "وصف الصورة مطلوب")
    model_id = str(payload.get("model") or "DALL-E").strip()[:60] or "DALL-E"

    attempts = [
        (f"{IMAGE_API_BASE_URL}/ai/aiappgen", {"prompt": prompt, "modelId": model_id}),
        (f"{OMEGATECH_AI_BASE_URL}/flux", {"action": "generate", "prompt": prompt}),
        (f"{OMEGATECH_AI_BASE_URL}/Argen", {"action": "image", "prompt": prompt}),
    ]
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        for url, params in attempts:
            try:
                r = await client.get(url, params=params, headers=_DEFAULT_OUTBOUND_HEADERS)
                img_bytes, mime = await _decode_upstream_image_response(client, r)
                return Response(
                    content=img_bytes,
                    media_type=mime,
                    headers={"Cache-Control": "no-store", "X-Image-Mime": mime},
                )
            except Exception as exc:
                last_err = exc
                continue
    if isinstance(last_err, HTTPException):
        raise last_err
    raise HTTPException(502, f"تعذر توليد الصورة عبر الخادم: {str(last_err or 'unknown')[:240]}")


@app.post("/api/models/image/edit")
async def model_image_edit(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Edit an image directly from Python with multi-host upload & multi-provider failover."""
    prompt = str(payload.get("prompt") or "").strip()[:1200]
    if not prompt:
        raise HTTPException(422, "تعليمات تعديل الصورة مطلوبة")

    image_url = str(payload.get("image_url") or "").strip()
    image_b64 = _strip_data_uri_prefix(payload.get("image_base64") or "")
    in_mime = str(payload.get("mime") or "image/png").split(";")[0].strip().lower()
    if not in_mime.startswith("image/"):
        in_mime = "image/png"

    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        if image_url.startswith("https://"):
            public_img_url = _validate_public_https_url(image_url)
        elif image_b64:
            raw_src = base64.b64decode(image_b64)
            if len(raw_src) > 20 * 1024 * 1024:
                raise HTTPException(413, "حجم الصورة يتجاوز الحد المسموح")
            ext = "jpg" if "jpeg" in in_mime or "jpg" in in_mime else "webp" if "webp" in in_mime else "png"
            public_img_url = await _upload_to_public_temp_host(client, raw_src, f"edit-src.{ext}", in_mime)
            if not public_img_url:
                raise HTTPException(502, "تعذر تجهيز الصورة المصدر للتعديل")
        else:
            raise HTTPException(422, "يرجى إرفاق صورة أو تمرير رابط صورة عام")

        edit_attempts = [
            (f"{IMAGE_API_BASE_URL}/ai/imageedit", {"prompt": prompt, "image": public_img_url}),
            (f"{OMEGATECH_AI_BASE_URL}/Nanobana-extra", {"action": "edit", "image": public_img_url, "prompt": prompt}),
            (f"{OMEGATECH_AI_BASE_URL}/flux", {"action": "edit", "prompt": prompt, "imageUrl": public_img_url}),
        ]
        last_err: Exception | None = None
        for url, params in edit_attempts:
            try:
                r = await client.get(url, params=params, headers=_DEFAULT_OUTBOUND_HEADERS)
                img_bytes, mime = await _decode_upstream_image_response(client, r)
                return Response(
                    content=img_bytes,
                    media_type=mime,
                    headers={"Cache-Control": "no-store", "X-Image-Mime": mime},
                )
            except Exception as exc:
                last_err = exc
                continue
        if isinstance(last_err, HTTPException):
            raise last_err
        raise HTTPException(502, f"تعذر تعديل الصورة عبر الخادم: {str(last_err or 'unknown')[:240]}")



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


def _trim_prompt_cleanly(prompt: str, max_len: int = 950) -> str:
    s = re.sub(r"\s+", " ", (prompt or "").strip())
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    for sep in (". ", ", ", "; ", " "):
        idx = cut.rfind(sep)
        if idx >= int(max_len * 0.65):
            return cut[:idx].rstrip(" ,;.") + ("." if sep == ". " else "")
    return cut.rstrip()


def _extract_video_url_from_json(obj: Any) -> str:
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith(("http://", "https://")) and (
            re.search(r"\.(mp4|webm|mov)(\?|$)", s, re.I) or "video" in s.lower() or "generated/" in s.lower()
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
        # Prioritize data.videoUrl (exact Omegatech Txt2video contract) and standard video keys
        for key in ("videoUrl", "video_url", "video", "output_url", "mp4", "url", "data", "result", "output"):
            if key in obj:
                val = obj[key]
                if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                    return re.sub(r"^http://", "https://", val.strip(), flags=re.I)
                got = _extract_video_url_from_json(val)
                if got:
                    return got
    return ""


async def _generate_video_via_rest_api(prompt: str, ratio: str, sound: bool) -> tuple[bytes | None, str]:
    """Call free unlimited Txt2video API directly from Python with generous timeout and clean retry."""
    if not VIDEO_API_URL:
        raise RuntimeError("VIDEO_API_URL unconfigured")
    clean_ratio = ratio if ratio in {"auto", "1:1", "16:9", "9:16", "4:3", "3:4"} else "auto"
    sound_str = "true" if sound else "false"
    prompt_variants = [_trim_prompt_cleanly(prompt, 950)]
    if len(prompt_variants[0]) > 520:
        prompt_variants.append(_trim_prompt_cleanly(prompt, 500))

    last_err = "تعذر الاتصال بخدمة الفيديو"
    req_timeout = httpx.Timeout(connect=25.0, read=300.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=req_timeout, follow_redirects=True) as client:
        for attempt_idx, cur_prompt in enumerate(prompt_variants):
            try:
                r = await client.get(
                    VIDEO_API_URL,
                    params={
                        "action": "generate",
                        "prompt": cur_prompt,
                        "ratio": clean_ratio,
                        "sound": sound_str,
                    },
                    headers=_DEFAULT_OUTBOUND_HEADERS,
                )
                if r.status_code < 400 and _sniff_video_mp4(r.content, r.headers.get("content-type", "")):
                    return r.content, ""
                text = r.text or ""
                try:
                    data = r.json()
                except Exception:
                    data = None

                if r.status_code >= 400 or (
                    isinstance(data, dict)
                    and (
                        data.get("status") is False
                        or data.get("success") is False
                        or (data.get("statusCode") and int(data["statusCode"]) != 200)
                    )
                ):
                    msg = str((data or {}).get("message") or (data or {}).get("error") or f"HTTP {r.status_code}")
                    last_err = msg[:220]
                    if attempt_idx + 1 < len(prompt_variants):
                        await asyncio.sleep(1.5)
                        continue
                    raise RuntimeError(last_err)

                direct_url = _extract_video_url_from_json(data) if data is not None else ""
                if not direct_url:
                    m = re.search(r"https?://[^\s\"'<>]+", text)
                    if m:
                        direct_url = re.sub(r"^http://", "https://", m.group(0), flags=re.I)
                if direct_url:
                    # Try caching MP4 bytes in Python for same-origin Range playback & encrypted DB sync;
                    # if upstream CDN prefers direct browser access, fall back seamlessly to direct_url.
                    try:
                        dl = await client.get(direct_url, headers=_DEFAULT_OUTBOUND_HEADERS, timeout=60.0)
                        if dl.status_code < 400 and _sniff_video_mp4(dl.content, dl.headers.get("content-type", "")):
                            return dl.content, direct_url
                    except Exception:
                        pass
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
                    last_err = "لم تُرجع خدمة الفيديو رابط videoUrl صالحاً"
                    if attempt_idx + 1 < len(prompt_variants):
                        await asyncio.sleep(1.5)
                        continue
                    raise RuntimeError(last_err)

                base_root = VIDEO_API_URL.rsplit("/", 1)[0]
                deadline = time.time() + 240.0
                while time.time() < deadline:
                    await asyncio.sleep(4.0)
                    for poll_url, poll_params in (
                        (f"{base_root}/txt2video-status", {"task_key": task_key}),
                        (VIDEO_API_URL, {"action": "status", "task_id": task_key, "id": task_key}),
                    ):
                        try:
                            sr = await client.get(poll_url, params=poll_params, headers=_DEFAULT_OUTBOUND_HEADERS, timeout=30.0)
                            if sr.status_code >= 400:
                                continue
                            if _sniff_video_mp4(sr.content, sr.headers.get("content-type", "")):
                                return sr.content, ""
                            sj = sr.json()
                            st = str(sj.get("status") or (sj.get("data") or {}).get("status") or "").lower()
                            if st in {"failed", "error", "cancelled", "canceled"}:
                                raise RuntimeError(str(sj.get("message") or sj.get("error") or "فشل توليد الفيديو"))
                            found = _extract_video_url_from_json(sj)
                            if found:
                                try:
                                    dl = await client.get(found, headers=_DEFAULT_OUTBOUND_HEADERS, timeout=60.0)
                                    if dl.status_code < 400 and _sniff_video_mp4(dl.content, dl.headers.get("content-type", "")):
                                        return dl.content, found
                                except Exception:
                                    pass
                                return None, found
                        except RuntimeError:
                            raise
                        except Exception:
                            continue
                raise RuntimeError("انتهت مهلة انتظار توليد الفيديو")
            except Exception as exc:
                last_err = str(exc)[:220]
                if attempt_idx + 1 < len(prompt_variants):
                    await asyncio.sleep(1.5)
                    continue
                raise RuntimeError(last_err) from exc
    raise RuntimeError(last_err)


async def _execute_video_job(task_id: str, prompt: str, ratio: str, sound: bool) -> None:
    job = _video_jobs.get(task_id)
    if not job:
        return
    try:
        job["step"] = "generating"
        mp4_bytes, remote_url = await _generate_video_via_rest_api(prompt, ratio, sound)
        if mp4_bytes:
            job.update(
                {
                    "status": "succeeded",
                    "bytes": mp4_bytes,
                    "mime": "video/mp4",
                    "video_url": f"/api/models/video/file/{task_id}",
                    "remote_url": remote_url or None,
                    "ts": time.time(),
                }
            )
            return
        if remote_url.startswith("https://"):
            job.update(
                {
                    "status": "succeeded",
                    "video_url": remote_url,
                    "remote_url": remote_url,
                    "ts": time.time(),
                }
            )
            return
        raise RuntimeError("لم يتم استلام رابط فيديو صالح")
    except Exception as exc:
        job.update(
            {
                "status": "failed",
                "error": f"تعذر توليد الفيديو حالياً ({str(exc)[:200]}) — يرجى إعادة المحاولة.",
                "ts": time.time(),
            }
        )


@app.post("/api/models/video/submit")
async def model_video_submit(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Queue an unlimited free Txt2video job asynchronously in Python (returns in <5ms so HF Space never hits 502)."""
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
    """Poll asynchronous video generation task status from Python (unlimited, zero proxy)."""
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
                "remote_url": job.get("remote_url"),
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
