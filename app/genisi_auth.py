"""Genisi secure account, challenge, preferences, and private-dataset API.

Mount in the existing FastAPI app:
    from genisi_auth import router as genisi_auth_router
    app.include_router(genisi_auth_router)

Required server-side environment secrets: HF_TOKEN (dataset write access), SESSION_SECRET (>=32 chars).
Optional environment variable: GENISI_DATASET_ID=AnesKAM/Genisi-users.
Hugging Face is the data service; the FastAPI app can run on any Docker/Python host.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError, VerificationError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Body, HTTPException, Request, Response
from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi, hf_hub_download
from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()
DATASET_ID = os.getenv("GENISI_DATASET_ID", os.getenv("HF_USERS_DATASET", "AnesKAM/Genisi-users")).strip()
SESSION_COOKIE = "genisi_session"
CSRF_COOKIE = "genisi_csrf"
SESSION_TTL = 14 * 24 * 60 * 60
CHALLENGE_TTL = 180
POW_DIFFICULTY = max(16, min(22, int(os.getenv("GENISI_POW_BITS", "17"))))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1" if os.getenv("ENVIRONMENT", "production").lower() == "production" else "0") == "1"
# Hugging Face Spaces may run inside a cross-site iframe. Lax cookies are
# withheld in that context, so production defaults to None; Secure is required.
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none" if os.getenv("ENVIRONMENT", "production").lower() == "production" else "lax").strip().lower()
if COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    COOKIE_SAMESITE = "none" if os.getenv("ENVIRONMENT", "production").lower() == "production" else "lax"
# CHIPS (Partitioned cookies) keeps authentication working in embedded Spaces
# even when the browser blocks ordinary third-party cookies.
PARTITIONED_COOKIES = COOKIE_SAMESITE == "none" and COOKIE_SECURE

_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16, type=Type.ID)
_DUMMY_PASSWORD_HASH = _password_hasher.hash(secrets.token_urlsafe(32))
_store_lock = threading.RLock()
_challenge_lock = threading.Lock()
_challenges: dict[str, dict[str, Any]] = {}
_rate_lock = threading.Lock()
_rate_events: dict[str, deque[float]] = defaultdict(deque)
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_hub: "HubStore | None" = None
DEFAULT_PREFERENCES: dict[str, Any] = {
    "memory_enabled": False,
    "memory_summary": "",
    "warmth": 55,
    "enthusiasm": 50,
    "emoji_mode": "auto",
    "visual_answers": True,
    "headers_lists": True,
}


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["login", "register"]


class AuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    challenge_id: str = Field(min_length=20, max_length=80)
    answer: str = Field(min_length=1, max_length=12)
    pow_nonce: str = Field(min_length=1, max_length=16)
    username: str | None = Field(default=None, max_length=32)


class PreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferences: dict[str, Any]


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessions: list[Any] = Field(max_length=40)


def _now() -> float:
    return time.time()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_error(exc: Exception) -> None:
    # Never log request passwords, access tokens, or record contents.
    print(f"[Genisi auth] {type(exc).__name__}: {str(exc)[:240]}")


def _dataset_key() -> bytes:
    secret = os.getenv("SESSION_SECRET", "").strip()
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    return hashlib.sha256(b"Genisi-private-dataset-v1\x00" + secret.encode("utf-8")).digest()


def _encrypt_user_doc(path: str, value: dict[str, Any]) -> dict[str, Any]:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_dataset_key()).encrypt(nonce, _json_bytes(value), path.encode("utf-8"))
    return {
        "_genisi_encrypted": 1,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def _decrypt_user_doc(path: str, value: dict[str, Any]) -> dict[str, Any]:
    if value.get("_genisi_encrypted") != 1:
        # Read legacy/plain records for one-way migration on the next update.
        return value
    try:
        nonce = base64.b64decode(value["nonce"], validate=True)
        ciphertext = base64.b64decode(value["ciphertext"], validate=True)
        plaintext = AESGCM(_dataset_key()).decrypt(nonce, ciphertext, path.encode("utf-8"))
        result = json.loads(plaintext.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Invalid encrypted account record")
        return result
    except Exception as exc:
        raise RuntimeError("Unable to decrypt a Genisi account record; check that SESSION_SECRET has not changed") from exc


class HubStore:
    """Small document store backed by one JSON file per account in a private dataset."""

    def __init__(self) -> None:
        token = os.getenv("HF_TOKEN", "").strip()
        if not token:
            raise RuntimeError("HF_TOKEN is not configured")
        if not DATASET_ID or "/" not in DATASET_ID:
            raise RuntimeError("GENISI_DATASET_ID must be namespace/repository")
        self.api = HfApi(token=token, user_agent={"app": "Genisi", "feature": "secure-auth"})
        self.token = token
        self.ready = False

    def ensure(self) -> None:
        if self.ready:
            return
        with _store_lock:
            if self.ready:
                return
            self.api.create_repo(repo_id=DATASET_ID, repo_type="dataset", private=True, exist_ok=True)
            info = self.api.repo_info(repo_id=DATASET_ID, repo_type="dataset")
            if not bool(getattr(info, "private", False)):
                self.api.update_repo_settings(repo_id=DATASET_ID, repo_type="dataset", private=True)
            self.ready = True

    def read_json(self, path: str) -> dict[str, Any] | None:
        self.ensure()
        try:
            filename = hf_hub_download(
                repo_id=DATASET_ID,
                filename=path,
                repo_type="dataset",
                token=self.token,
            )
            with open(filename, "r", encoding="utf-8") as f:
                value = json.load(f)
            if not isinstance(value, dict):
                return None
            if path.startswith("users/"):
                return _decrypt_user_doc(path, value)
            return value
        except EntryNotFoundError:
            return None
        except HfHubHTTPError as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return None
            raise


    def commit_json(self, items: dict[str, Any], message: str) -> None:
        """Commit several JSON records together, retrying against a fresh Hub revision."""
        self.ensure()
        operations = []
        for path, value in items.items():
            stored = _encrypt_user_doc(path, value) if path.startswith("users/") else value
            operations.append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=_json_bytes(stored)))
        last_error: Exception | None = None
        for attempt in range(4):
            info = self.api.repo_info(repo_id=DATASET_ID, repo_type="dataset")
            parent = getattr(info, "sha", None)
            try:
                self.api.create_commit(
                    repo_id=DATASET_ID,
                    repo_type="dataset",
                    operations=operations,
                    commit_message=message[:120],
                    parent_commit=parent,
                    token=self.token,
                )
                return
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                is_conflict = status in (409, 412) or "parent commit" in str(exc).lower() or "conflict" in str(exc).lower()
                if not is_conflict or attempt == 3:
                    raise
                time.sleep(0.15 * (attempt + 1))
        if last_error:
            raise last_error

    def get_user(self, uid: str) -> dict[str, Any] | None:
        return self.read_json(f"users/{uid}.json")

    def _legacy_user(self, field: str, value: str) -> dict[str, Any] | None:
        """Read and securely migrate an account created by the previous Genisi UserStore."""
        legacy = self.read_json("auth/users.json")
        users = legacy.get("users", {}) if isinstance(legacy, dict) else {}
        if not isinstance(users, dict):
            return None
        target = value.casefold() if field == "username" else value.lower()
        for old in users.values():
            if not isinstance(old, dict):
                continue
            candidate = str(old.get(field, "")).strip()
            candidate = candidate.casefold() if field == "username" else candidate.lower()
            if not candidate or candidate != target:
                continue
            email = str(old.get("email", "")).strip().lower()
            username = str(old.get("username", "")).strip()
            password_hash = str(old.get("password_hash", ""))
            if not email or not username or not password_hash:
                return None
            try:
                uid = str(uuid.UUID(str(old.get("id", ""))))
            except (ValueError, AttributeError):
                uid = str(uuid.uuid4())
            record = {
                "schema": 2,
                "id": uid,
                "name": str(old.get("name", username))[:60],
                "username": username,
                "email": email,
                "password_hash": password_hash,
                "role": str(old.get("role", "user"))[:24],
                "status": str(old.get("status", "active"))[:24],
                "created_at": old.get("created_at") or int(_now()),
                "updated_at": int(_now()),
                "session_version": 1,
                "preferences": dict(DEFAULT_PREFERENCES),
                "sessions": [],
            }
            email_path = f"indexes/email_{_index_digest('email', email)}.json"
            name_path = f"indexes/name_{_index_digest('name', username.casefold())}.json"
            user_path = f"users/{uid}.json"
            with _store_lock:
                existing = self.read_json(email_path) or self.read_json(name_path)
                if existing:
                    return existing
                self.commit_json({
                    user_path: record,
                    email_path: {"user_id": uid},
                    name_path: {"user_id": uid},
                }, "Genisi: migrate legacy account")
            return {"user_id": uid}
        return None

    def migrate_legacy_accounts(self) -> int:
        """Move the old plain auth/users.json store into encrypted per-user records."""
        legacy = self.read_json("auth/users.json")
        old_users = legacy.get("users", {}) if isinstance(legacy, dict) else {}
        if not isinstance(old_users, dict) or not old_users:
            return 0
        additions: dict[str, dict[str, Any]] = {}
        seen_emails: set[str] = set()
        seen_names: set[str] = set()
        for old in old_users.values():
            if not isinstance(old, dict):
                return 0  # Keep the legacy file intact if any row needs manual repair.
            email = str(old.get("email", "")).strip().lower()
            username = str(old.get("username", "")).strip()
            password_hash = str(old.get("password_hash", ""))
            if not email or not username or not password_hash:
                return 0
            if email in seen_emails or username.casefold() in seen_names:
                return 0
            seen_emails.add(email)
            seen_names.add(username.casefold())
            try:
                uid = str(uuid.UUID(str(old.get("id", ""))))
            except (ValueError, AttributeError):
                uid = str(uuid.uuid4())
            email_path = f"indexes/email_{_index_digest('email', email)}.json"
            name_path = f"indexes/name_{_index_digest('name', username.casefold())}.json"
            user_path = f"users/{uid}.json"
            email_existing = self.read_json(email_path)
            name_existing = self.read_json(name_path)
            existing = email_existing or name_existing
            if existing:
                existing_uid = str(existing.get("user_id", ""))
                existing_record = self.get_user(existing_uid) if existing_uid else None
                if not existing_record or str(existing_record.get("email", "")).lower() != email or str(existing_record.get("username", "")).casefold() != username.casefold():
                    return 0  # Conflicting account data: keep the source file for manual repair.
                additions[email_path] = {"user_id": existing_uid}
                additions[name_path] = {"user_id": existing_uid}
                continue
            additions[user_path] = {
                "schema": 2, "id": uid,
                "name": str(old.get("name", username))[:60],
                "username": username, "email": email,
                "password_hash": password_hash,
                "role": str(old.get("role", "user"))[:24],
                "status": str(old.get("status", "active"))[:24],
                "created_at": old.get("created_at") or int(_now()),
                "updated_at": int(_now()), "session_version": 1,
                "preferences": dict(DEFAULT_PREFERENCES), "sessions": [],
            }
            additions[email_path] = {"user_id": uid}
            additions[name_path] = {"user_id": uid}
        # Do not delete the legacy file unless every source row was successfully mapped.
        operations = [
            CommitOperationAdd(
                path_in_repo=path,
                path_or_fileobj=_json_bytes(_encrypt_user_doc(path, value) if path.startswith("users/") else value),
            )
            for path, value in additions.items()
        ]
        operations.append(CommitOperationDelete(path_in_repo="auth/users.json"))
        last_error: Exception | None = None
        for attempt in range(4):
            parent = getattr(self.api.repo_info(repo_id=DATASET_ID, repo_type="dataset"), "sha", None)
            try:
                self.api.create_commit(
                    repo_id=DATASET_ID, repo_type="dataset", operations=operations,
                    commit_message="Genisi: encrypt and migrate legacy user records",
                    parent_commit=parent, token=self.token,
                )
                return sum(1 for path in additions if path.startswith("users/"))
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                conflict = status in (409, 412) or "parent commit" in str(exc).lower() or "conflict" in str(exc).lower()
                if not conflict or attempt == 3:
                    raise
                time.sleep(0.15 * (attempt + 1))
        if last_error:
            raise last_error
        return 0

    def find_email(self, email: str) -> dict[str, Any] | None:
        found = self.read_json(f"indexes/email_{_index_digest('email', email)}.json")
        return found or self._legacy_user("email", email)

    def find_username(self, username: str) -> dict[str, Any] | None:
        found = self.read_json(f"indexes/name_{_index_digest('name', username.casefold())}.json")
        return found or self._legacy_user("username", username)

    def create_user(self, record: dict[str, Any]) -> None:
        email = record["email"]
        username = record["username"]
        with _store_lock:
            if self.find_email(email):
                raise ValueError("EMAIL_EXISTS")
            if self.find_username(username):
                raise ValueError("USERNAME_EXISTS")
            uid = record["id"]
            items = {
                f"users/{uid}.json": record,
                f"indexes/email_{_index_digest('email', email)}.json": {"user_id": uid},
                f"indexes/name_{_index_digest('name', username.casefold())}.json": {"user_id": uid},
            }
            try:
                self.commit_json(items, "Genisi: register account")
            except Exception:
                # Recheck indexes after a concurrent commit before reporting a generic conflict.
                if self.find_email(email):
                    raise ValueError("EMAIL_EXISTS")
                if self.find_username(username):
                    raise ValueError("USERNAME_EXISTS")
                raise

    def update_user(self, uid: str, updater) -> dict[str, Any]:
        with _store_lock:
            record = self.get_user(uid)
            if not record:
                raise KeyError("USER_NOT_FOUND")
            updater(record)
            record["updated_at"] = int(_now())
            self.commit_json({f"users/{uid}.json": record}, "Genisi: update user data")
            return record


def _index_digest(namespace: str, value: str) -> str:
    return hmac.new(_dataset_key(), (namespace + ":" + value).encode("utf-8"), hashlib.sha256).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _store() -> HubStore:
    global _hub
    if _hub is None:
        _hub = HubStore()
    return _hub


def initialize_genisi_auth() -> None:
    """Fail fast at app startup when the private dataset or persistent secrets are unavailable."""
    if COOKIE_SAMESITE == "none" and not COOKIE_SECURE:
        raise RuntimeError("COOKIE_SAMESITE=none requires COOKIE_SECURE=1 (HTTPS).")
    _serializer()
    _dataset_key()
    store = _store()
    store.ensure()
    migrate = getattr(store, "migrate_legacy_accounts", None)
    if callable(migrate):
        migrate()


def get_genisi_user_record(request: Request) -> dict[str, Any]:
    """Synchronous, sanitized account accessor for legacy FastAPI business routes."""
    return _public_user(_current_user(request))


def _serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("SESSION_SECRET", "").strip()
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    return URLSafeTimedSerializer(secret, salt="genisi-session-v1")


def _client_ip(request: Request) -> str:
    # Trusted reverse proxies may provide one of these headers.
    for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        raw = request.headers.get(header, "").strip()
        if raw:
            return (raw.split(",")[-1] if header == "x-forwarded-for" else raw.split(",", 1)[0]).strip()[:64]
    return request.client.host if request.client else "unknown"


def _rate_limit(key: str, limit: int, window: int) -> None:
    now = _now()
    with _rate_lock:
        q = _rate_events[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "محاولات كثيرة. يرجى الانتظار ثم المحاولة مجدداً.")
        q.append(now)


def _origin_guard(request: Request) -> None:
    """Validate same-origin mutations behind HTTPS-terminating reverse proxies.

    Some hosts terminate TLS before forwarding HTTP to Uvicorn. In that case
    request.url.scheme is "http" while the browser Origin is "https". We
    compare the external host first and enforce the forwarded scheme whenever
    the proxy supplies it, without weakening the double-submit CSRF check.
    """
    raw_origin = request.headers.get("origin", "").strip()
    if not raw_origin or raw_origin.lower() == "null":
        raise HTTPException(403, "طلب غير موثوق")
    try:
        origin = urlsplit(raw_origin)
        if origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password:
            raise ValueError("invalid origin")
    except Exception:
        raise HTTPException(403, "مصدر الطلب غير مسموح")

    def normalized_host(value: str, scheme_hint: str = "") -> str:
        value = value.strip().split(",", 1)[0].strip()
        if not value:
            return ""
        parsed = urlsplit(value if "://" in value else "//" + value)
        scheme = (parsed.scheme or scheme_hint).lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            return ""
        try:
            port = parsed.port
        except ValueError:
            return ""
        if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
            return f"{host}:{port}"
        return host

    site_header = request.headers.get("sec-fetch-site", "").strip().lower()
    if site_header and site_header not in ("same-origin", "none"):
        raise HTTPException(403, "طلب عابر للمواقع مرفوض")
    browser_confirms_same_origin = site_header == "same-origin"

    origin_host = normalized_host(origin.netloc, origin.scheme)
    host_candidates = [
        request.headers.get("x-forwarded-host", ""),
        request.headers.get("host", ""),
        request.url.netloc,
    ]
    scheme_hint = request.headers.get("x-forwarded-proto", origin.scheme).split(",", 1)[0].strip().lower() or origin.scheme
    expected_hosts = {normalized_host(value, scheme_hint) for value in host_candidates if value.strip()}
    expected_hosts.discard("")
    # Chromium/Firefox set Sec-Fetch-Site themselves. When it confirms
    # same-origin, allow the public host to differ from the internal proxy
    # Host header; double-submit CSRF validation below remains mandatory.
    if not origin_host or (origin_host not in expected_hosts and not browser_confirms_same_origin):
        raise HTTPException(403, "مصدر الطلب غير مسموح")

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if forwarded_proto and forwarded_proto in {"http", "https"} and origin.scheme != forwarded_proto and not browser_confirms_same_origin:
        raise HTTPException(403, "مصدر الطلب غير مسموح")
    # Without a forwarded scheme, do not compare against the internal HTTP
    # hop: the external proxy may have already terminated HTTPS.
    if not forwarded_proto and request.url.scheme == "https" and origin.scheme != "https" and not browser_confirms_same_origin:
        raise HTTPException(403, "مصدر الطلب غير مسموح")


def _csrf_guard(request: Request) -> None:
    _origin_guard(request)
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(403, "تعذر التحقق من أمان الطلب. حدّث الصفحة ثم أعد المحاولة.")


def _public_user(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id", ""),
        "name": record.get("name", record.get("username", "")),
        "username": record.get("username", ""),
        "email": record.get("email", ""),
        "plan": "free",
        "created_at": record.get("created_at"),
    }


def _set_session(response: Response, record: dict[str, Any]) -> None:
    token = _serializer().dumps({"uid": record["id"], "ver": int(record.get("session_version", 1))})
    csrf = secrets.token_urlsafe(32)
    common = {"max_age": SESSION_TTL, "path": "/", "secure": COOKIE_SECURE, "samesite": COOKIE_SAMESITE}
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)
    if PARTITIONED_COOKIES:
        # Append the CHIPS attribute manually. Starlette's partitioned=True
        # argument is gated on Python 3.14, while the raw cookie attribute is
        # valid on the Python 3.11 runtime used by this deployment.
        indices = [i for i, (key, _) in enumerate(response.raw_headers) if key.lower() == b"set-cookie"]
        for i in indices[-2:]:
            key, value = response.raw_headers[i]
            if b"; partitioned" not in value.lower():
                response.raw_headers[i] = (key, value + b"; Partitioned")


def _clear_session(response: Response) -> None:
    # Clear the partitioned cookies (CHIPS) as well as any legacy unpartitioned
    # cookies created by older releases. The attributes must match on deletion.
    if PARTITIONED_COOKIES:
        expiry = "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/; SameSite=None; Secure; Partitioned"
        response.headers.append("set-cookie", f"{SESSION_COOKIE}=; {expiry}; HttpOnly")
        response.headers.append("set-cookie", f"{CSRF_COOKIE}=; {expiry}")
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite=COOKIE_SAMESITE)
    response.delete_cookie(CSRF_COOKIE, path="/", secure=COOKIE_SECURE, httponly=False, samesite=COOKIE_SAMESITE)


def _current_user(request: Request) -> dict[str, Any]:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        raise HTTPException(401, "يلزم تسجيل الدخول")
    try:
        payload = _serializer().loads(raw, max_age=SESSION_TTL)
        uid = str(payload.get("uid", ""))
        version = int(payload.get("ver", 0))
        if not uid or not re.fullmatch(r"[0-9a-f-]{36}", uid):
            raise ValueError("bad session")
        record = _store().get_user(uid)
        if not record or version != int(record.get("session_version", 1)):
            raise HTTPException(401, "انتهت الجلسة. سجّل الدخول مجدداً.")
        return record
    except SignatureExpired:
        raise HTTPException(401, "انتهت الجلسة. سجّل الدخول مجدداً.")
    except BadSignature:
        raise HTTPException(401, "جلسة غير صالحة")
    except HTTPException:
        raise
    except Exception as exc:
        _safe_error(exc)
        raise HTTPException(503, "تعذر الوصول إلى حسابك حالياً")


async def require_genisi_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency returning public account fields only (never the password hash)."""
    record = await asyncio.to_thread(_current_user, request)
    return _public_user(record)


async def require_genisi_csrf(request: Request) -> None:
    """FastAPI dependency for state-changing routes: Depends(require_genisi_csrf)."""
    await asyncio.to_thread(_csrf_guard, request)


def _make_challenge(mode: str, request: Request) -> dict[str, Any]:
    _rate_limit("challenge:" + _sha(_client_ip(request)), 40, 300)
    a = secrets.randbelow(11) + 3
    b = secrets.randbelow(8) + 2
    c = secrets.randbelow(9) + 1
    op = secrets.choice(("+", "−", "×"))
    answer = a * b + c if op == "×" else (a + b + c if op == "+" else a + b - c)
    expression = f"({a} × {b}) + {c}" if op == "×" else (f"{a} + {b} + {c}" if op == "+" else f"{a} + {b} − {c}")
    choices = {answer, answer + secrets.choice((1, 2, 3)), answer - secrets.choice((1, 2, 3)), answer + secrets.choice((5, 7, 9))}
    while len(choices) < 4:
        choices.add(answer + secrets.randbelow(15) - 7)
    options = list(choices)
    secrets.SystemRandom().shuffle(options)
    cid = secrets.token_urlsafe(24)
    prefix = secrets.token_hex(20)
    item = {
        "mode": mode,
        "answer": str(answer),
        "ip": _sha(_client_ip(request)),
        "ua": _sha(request.headers.get("user-agent", "")[:400]),
        "prefix": prefix,
        "difficulty": POW_DIFFICULTY,
        "expires": _now() + CHALLENGE_TTL,
        "attempts": 0,
    }
    with _challenge_lock:
        # Cheap cleanup; cap memory even under junk traffic.
        for old in [k for k, v in _challenges.items() if v["expires"] < _now()]:
            _challenges.pop(old, None)
        if len(_challenges) > 10000:
            _challenges.clear()
        _challenges[cid] = item
    return {
        "challenge_id": cid,
        "question": f"اختبار Genisi: ما ناتج {expression}؟",
        "options": [str(x) for x in options],
        "pow_prefix": prefix,
        "difficulty": POW_DIFFICULTY,
        "expires_in": CHALLENGE_TTL,
    }


def _leading_zero_bits(data: bytes) -> int:
    count = 0
    for byte in data:
        if byte == 0:
            count += 8
            continue
        count += 8 - byte.bit_length()
        break
    return count


def _consume_challenge(request: Request, payload: AuthRequest, mode: str) -> None:
    _origin_guard(request)
    with _challenge_lock:
        challenge = _challenges.pop(payload.challenge_id, None)
    if not challenge:
        raise HTTPException(400, "انتهت صلاحية اختبار Genisi. أنشئ اختباراً جديداً.")
    if challenge["mode"] != mode or challenge["expires"] < _now():
        raise HTTPException(400, "اختبار التحقق منتهي أو غير مطابق للعملية.")
    if challenge["ip"] != _sha(_client_ip(request)) or challenge["ua"] != _sha(request.headers.get("user-agent", "")[:400]):
        raise HTTPException(400, "تعذر مطابقة جلسة اختبار التحقق. أعد المحاولة.")
    if not hmac.compare_digest(str(payload.answer), challenge["answer"]):
        raise HTTPException(400, "إجابة اختبار Genisi غير صحيحة.")
    if not payload.pow_nonce.isdigit():
        raise HTTPException(400, "فشل اختبار الحماية الآلي.")
    digest = hashlib.sha256(f'{challenge["prefix"]}:{payload.pow_nonce}'.encode()).digest()
    if _leading_zero_bits(digest) < int(challenge["difficulty"]):
        raise HTTPException(400, "فشل اختبار الحماية الآلي. أعد المحاولة.")


def _validate_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", email):
        raise HTTPException(422, "البريد الإلكتروني غير صالح")
    return email


def _sanitize_preferences(raw: dict[str, Any]) -> dict[str, Any]:
    prefs = dict(DEFAULT_PREFERENCES)
    prefs["memory_enabled"] = bool(raw.get("memory_enabled", prefs["memory_enabled"]))
    prefs["memory_summary"] = str(raw.get("memory_summary", ""))[:6000]
    for key in ("warmth", "enthusiasm"):
        try:
            prefs[key] = max(0, min(100, int(raw.get(key, prefs[key]))))
        except (TypeError, ValueError):
            pass
    emoji = str(raw.get("emoji_mode", "auto"))
    prefs["emoji_mode"] = emoji if emoji in {"auto", "low", "high", "off"} else "auto"
    prefs["visual_answers"] = bool(raw.get("visual_answers", True))
    prefs["headers_lists"] = bool(raw.get("headers_lists", True))
    return prefs


def _clean_sessions(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for session in raw[-40:]:
        if not isinstance(session, dict):
            continue
        title = re.sub(r"\s+", " ", str(session.get("title", "محادثة جديدة"))).strip()[:60]
        try:
            ts = int(session.get("ts", 0))
        except (TypeError, ValueError):
            ts = 0
        messages = []
        for message in (session.get("msgs") or [])[-60:]:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            if role not in ("user", "assistant"):
                continue
            row: dict[str, Any] = {"role": role, "content": str(message.get("content", ""))[:60000]}
            if isinstance(message.get("media"), list):
                row["media"] = [m for m in message["media"][:12] if isinstance(m, dict)]
            if isinstance(message.get("atts"), list):
                row["atts"] = [
                    {"name": str(a.get("name", ""))[:120], "mime": str(a.get("mime", ""))[:100]}
                    for a in message["atts"][:6] if isinstance(a, dict)
                ]
            if message.get("docs"):
                row["docs"] = str(message["docs"])[:30000]
            messages.append(row)
        out.append({"title": title, "ts": ts, "msgs": messages})
    encoded = _json_bytes(out)
    if len(encoded) > 8 * 1024 * 1024:
        raise HTTPException(413, "حجم سجل المحادثات كبير جداً. احذف بعض المرفقات أو المحادثات القديمة.")
    return out


@router.get("/api/auth/health")
async def auth_health():
    try:
        _serializer()
        _dataset_key()
        await asyncio.to_thread(_store().ensure)
        return {"ok": True, "dataset": DATASET_ID, "private": True}
    except Exception as exc:
        _safe_error(exc)
        raise HTTPException(503, "خدمة الحسابات غير مهيأة. تحقق من HF_TOKEN وSESSION_SECRET وصلاحية الكتابة على Hugging Face Dataset.")


@router.post("/api/auth/challenge")
async def auth_challenge(payload: ChallengeRequest, request: Request):
    try:
        return _make_challenge(payload.mode, request)
    except HTTPException:
        raise
    except Exception as exc:
        _safe_error(exc)
        raise HTTPException(503, "تعذر إنشاء اختبار التحقق")


@router.post("/api/auth/register")
async def auth_register(payload: AuthRequest, request: Request, response: Response):
    email = _validate_email(payload.email)
    ipkey = _sha(_client_ip(request))
    _rate_limit("register:" + ipkey, 5, 3600)
    _consume_challenge(request, payload, "register")
    password = payload.password
    if len(password) < 12:
        raise HTTPException(422, "استخدم كلمة مرور من 12 حرفاً على الأقل")
    username = (payload.username or email.split("@", 1)[0]).strip()
    if not re.fullmatch(r"[\w.-]{3,32}", username, flags=re.UNICODE):
        raise HTTPException(422, "اسم المستخدم يجب أن يكون من 3 إلى 32 حرفاً أو رقماً، دون مسافات")
    uid = str(uuid.uuid4())
    now = int(_now())
    try:
        record = {
            "schema": 2,
            "id": uid,
            "email": email,
            "username": username,
            "password_hash": _password_hasher.hash(password),
            "created_at": now,
            "updated_at": now,
            "session_version": 1,
            "preferences": dict(DEFAULT_PREFERENCES),
            "sessions": [],
        }
        await asyncio.to_thread(_store().create_user, record)
        _set_session(response, record)
        return {"ok": True, "user": _public_user(record)}
    except ValueError as exc:
        if str(exc) == "EMAIL_EXISTS":
            raise HTTPException(409, "هذا البريد مسجّل بالفعل")
        if str(exc) == "USERNAME_EXISTS":
            raise HTTPException(409, "اسم المستخدم مستخدم بالفعل")
        raise HTTPException(409, "تعذر إنشاء الحساب بسبب تعارض. حاول مجدداً.")
    except HTTPException:
        raise
    except Exception as exc:
        _safe_error(exc)
        raise HTTPException(503, "تعذر إنشاء الحساب أو حفظه في Hugging Face Dataset. تحقق من HF_TOKEN وصلاحياته.")


@router.post("/api/auth/login")
async def auth_login(payload: AuthRequest, request: Request, response: Response):
    email = _validate_email(payload.email)
    ipkey = _sha(_client_ip(request))
    account_key = _sha(email)
    _rate_limit("login-ip:" + ipkey, 12, 900)
    _rate_limit("login-account:" + account_key, 8, 900)
    with _rate_lock:
        failed = _login_failures[account_key]
        while failed and _now() - failed[0] > 900:
            failed.popleft()
        if len(failed) >= 5:
            raise HTTPException(429, "تم إيقاف محاولات الدخول لهذا الحساب مؤقتاً. حاول بعد 15 دقيقة.")
    _consume_challenge(request, payload, "login")
    try:
        index = await asyncio.to_thread(_store().find_email, email)
        record = await asyncio.to_thread(_store().get_user, str(index.get("user_id"))) if index else None
        candidate_hash = record.get("password_hash", "") if record else _DUMMY_PASSWORD_HASH
        try:
            password_ok = _password_hasher.verify(candidate_hash, payload.password)
        except (VerifyMismatchError, VerificationError, ValueError):
            password_ok = False
        ok = bool(record) and password_ok and record.get("status", "active") == "active"
        if not ok:
            with _rate_lock:
                q = _login_failures[account_key]
                q.append(_now())
                while q and _now() - q[0] > 900:
                    q.popleft()
            raise HTTPException(401, "البريد الإلكتروني أو كلمة المرور غير صحيحين")
        with _rate_lock:
            _login_failures.pop(account_key, None)
        _set_session(response, record)
        return {"ok": True, "user": _public_user(record)}
    except HTTPException:
        raise
    except Exception as exc:
        _safe_error(exc)
        raise HTTPException(503, "تعذر الوصول إلى حسابك في الداتا سيت حالياً")


@router.get("/api/auth/me")
async def auth_me(request: Request):
    record = await asyncio.to_thread(_current_user, request)
    return {"ok": True, "user": _public_user(record)}


@router.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    _csrf_guard(request)
    _clear_session(response)
    return {"ok": True}


@router.post("/api/auth/logout-all")
async def auth_logout_all(request: Request, response: Response):
    _csrf_guard(request)
    record = await asyncio.to_thread(_current_user, request)
    record = await asyncio.to_thread(_store().update_user, record["id"], lambda r: r.update({"session_version": int(r.get("session_version", 1)) + 1}))
    _clear_session(response)
    return {"ok": True}


@router.get("/api/preferences")
async def get_preferences(request: Request):
    record = await asyncio.to_thread(_current_user, request)
    return {"preferences": _sanitize_preferences(record.get("preferences") or {})}


@router.post("/api/preferences")
async def save_preferences(payload: PreferencesRequest, request: Request):
    _csrf_guard(request)
    record = await asyncio.to_thread(_current_user, request)
    preferences = _sanitize_preferences(payload.preferences)
    await asyncio.to_thread(
        _store().update_user,
        record["id"],
        lambda r: r.update({"preferences": preferences}),
    )
    return {"ok": True, "preferences": preferences}


@router.get("/api/history")
async def get_history(request: Request):
    record = await asyncio.to_thread(_current_user, request)
    return {"sessions": record.get("sessions", []) if isinstance(record.get("sessions"), list) else []}


@router.post("/api/history/save")
async def save_history(payload: HistoryRequest, request: Request):
    _csrf_guard(request)
    record = await asyncio.to_thread(_current_user, request)
    sessions = _clean_sessions(payload.sessions)
    await asyncio.to_thread(
        _store().update_user,
        record["id"],
        lambda r: r.update({"sessions": sessions}),
    )
    return {"ok": True, "sync": "saved", "count": len(sessions)}


@router.post("/api/history/title")
async def save_title(request: Request, payload: dict[str, Any] = Body(...)):
    _csrf_guard(request)
    record = await asyncio.to_thread(_current_user, request)
    index = payload.get("index")
    title = re.sub(r"\s+", " ", str(payload.get("title", ""))).strip()[:60]
    if not title:
        raise HTTPException(422, "العنوان فارغ")
    def update(r: dict[str, Any]) -> None:
        sessions = r.get("sessions", [])
        if isinstance(index, int) and 0 <= index < len(sessions):
            sessions[index]["title"] = title
    await asyncio.to_thread(_store().update_user, record["id"], update)
    return {"ok": True, "title": title}
