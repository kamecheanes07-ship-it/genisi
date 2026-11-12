from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .windows12_prompt import WINDOWS12_SYSTEM_PROMPT, WINDOWS12_PROMPT_VERSION, windows12_prompt_metadata
from .genisi_auth import (
    get_genisi_user_record,
    initialize_genisi_auth,
    require_genisi_csrf,
    router as genisi_auth_router,
)
from .worker_client import worker as WORKER

APP_NAME = "AnesNT Genisi"
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production").lower()
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


@app.on_event("startup")
def startup() -> None:
    # Prevent same-UID child project processes from inspecting this API process's /proc environ.
    try:
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except Exception:
        pass

    if ENVIRONMENT == "production" and (len(SESSION_SECRET) < 32 or SESSION_SECRET == "INSECURE-LOCAL-ONLY-CHANGE-ME"):
        raise RuntimeError("Set SESSION_SECRET to a stable random value of at least 32 characters in your hosting provider environment/secrets.")
    initialize_genisi_auth()
    # The Windows 12 design prompt is part of the application code. No Dataset,
    # HTML file, or user attachment is read to activate this design mode.
    prompt_info = windows12_prompt_metadata()
    print(
        "[Genisi] Built-in Windows 12 design prompt ready: "
        f"version={WINDOWS12_PROMPT_VERSION} chars={prompt_info['chars']} "
        f"sha256={prompt_info['sha256']}"
    )


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


# The secure router owns registration, login, logout, preferences, history and account health.
app.include_router(genisi_auth_router)
