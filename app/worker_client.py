from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx


WORKSPACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_TEXT_FILE = 2_000_000
MAX_COMMAND_OUTPUT = 16_000
MAX_LIST_ITEMS = 300
IGNORED_DIRS = {"node_modules", ".git", ".next", ".venv", "__pycache__", ".npm-cache", ".genisi-home", ".tmp"}

# This is a lightweight workspace runner inside the application container.
# It is NOT a per-user Docker security boundary. The application must restrict
# command execution to the application owner / trusted collaborators.
ALLOWED_COMMANDS = {
    "npm", "npx", "node", "python", "python3", "pip", "pytest", "git",
    "ls", "pwd", "cat", "head", "tail", "find", "grep", "wc", "mkdir",
    "touch", "cp", "mv", "rm", "echo", "tree", "which", "du", "df",
}


class WorkerClient:
    """Local Genisi workspace runner; no Docker daemon and no external VPS."""

    def __init__(self) -> None:
        self.mode = os.getenv("SANDBOX_MODE", "local").strip().lower()
        self.root = Path(
            os.getenv("GENISI_WORKSPACES_DIR", "/tmp/genisi-workspaces")
        ).expanduser().resolve()
        self.timeout = max(5, min(int(os.getenv("SANDBOX_COMMAND_TIMEOUT", "90")), 180))
        self._next_processes: dict[str, subprocess.Popen[bytes]] = {}
        self._next_ports: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(2)

    @property
    def enabled(self) -> bool:
        return self.mode == "local"

    def _workspace(self, workspace_id: str) -> Path:
        if not WORKSPACE_ID_RE.fullmatch(workspace_id or ""):
            raise ValueError("Invalid workspace ID")
        self.root.mkdir(parents=True, exist_ok=True)
        workspace = (self.root / workspace_id).resolve()
        if workspace.parent != self.root:
            raise ValueError("Invalid workspace path")
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            workspace.chmod(0o700)
        except OSError:
            pass
        return workspace

    @staticmethod
    def _relative_path(raw: str) -> str:
        raw = str(raw or ".").replace("\\", "/").strip()
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part == ".." for part in path.parts):
            raise ValueError("Use a relative path inside the project; '..' is not allowed")
        return "." if str(path) in ("", ".") else str(path)

    def _safe_target(self, workspace: Path, raw: str) -> Path:
        rel = self._relative_path(raw)
        target = (workspace / rel).resolve()
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("Path escapes the project workspace") from exc
        return target

    def _clean_env(self, workspace: Path) -> dict[str, str]:
        """Pass only non-secret environment variables to project commands."""
        home = workspace / ".genisi-home"
        npm_cache = workspace / ".npm-cache"
        tmp = workspace / ".tmp"
        home.mkdir(exist_ok=True)
        npm_cache.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(workspace / ".tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CI": "1",
            "npm_config_cache": str(npm_cache),
            "npm_config_update_notifier": "false",
            "NEXT_TELEMETRY_DISABLED": "1",
            "PORT": "3000",
        }

    def _run_sync(self, workspace_id: str, command: str, cwd: str = ".") -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        relative_cwd = self._relative_path(cwd)
        workdir = self._safe_target(workspace, relative_cwd)
        if not workdir.exists() or not workdir.is_dir():
            return {"ok": False, "exit_code": 2, "stdout": "", "stderr": "Working directory does not exist."}

        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            return {"ok": False, "exit_code": 2, "stdout": "", "stderr": f"Invalid command syntax: {exc}"}

        if not argv:
            return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

        executable = Path(argv[0]).name
        if executable not in ALLOWED_COMMANDS:
            return {
                "ok": False,
                "exit_code": 126,
                "stdout": "",
                "stderr": f"Command '{executable}' is not in the Genisi command allowlist.",
            }

        # Avoid shell interpreters and shell evaluation flags. Commands are
        # launched with shell=False; use one command per tool call.
        if executable in {"sh", "bash", "dash", "zsh", "sudo", "su", "env", "printenv"}:
            return {"ok": False, "exit_code": 126, "stdout": "", "stderr": "Shell/system commands are disabled."}

        if executable in {"node", "python", "python3"} and any(
            arg in {"-c", "--eval", "-e"} for arg in argv[1:]
        ):
            return {"ok": False, "exit_code": 126, "stdout": "", "stderr": "Inline code execution flags are disabled in terminal mode."}

        shell_tokens = {";", "&&", "||", "|", ">", ">>", "<", "&"}
        for arg in argv[1:]:
            if arg in shell_tokens or "`" in arg or "$(" in arg:
                return {"ok": False, "exit_code": 126, "stdout": "", "stderr": "Shell operators are disabled; run one command at a time."}
            value = arg.split("=", 1)[-1]
            if value.startswith("/"):
                return {"ok": False, "exit_code": 126, "stdout": "", "stderr": "Absolute paths are disabled; use paths inside the project."}
            if ".." in PurePosixPath(value).parts:
                return {"ok": False, "exit_code": 126, "stdout": "", "stderr": "Parent-directory paths are disabled."}

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                env=self._clean_env(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
                    proc.wait()
                stdout, stderr = proc.communicate()
                return {
                    "ok": False,
                    "exit_code": 124,
                    "stdout": stdout[-MAX_COMMAND_OUTPUT:],
                    "stderr": (stderr + f"\nCommand timed out after {self.timeout} seconds.")[-8_000:],
                }
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": stdout[-MAX_COMMAND_OUTPUT:],
                "stderr": stderr[-8_000:],
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "exit_code": 127,
                "stdout": "",
                "stderr": f"'{executable}' is not installed in this deployment image.",
            }

    async def _run(self, workspace_id: str, command: str, cwd: str = ".") -> dict[str, Any]:
        async with self._semaphore:
            return await asyncio.to_thread(self._run_sync, workspace_id, command, cwd)

    def _list_files(self, workspace_id: str, raw_path: str = ".") -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        base = self._safe_target(workspace, raw_path)
        if not base.exists() or not base.is_dir():
            raise FileNotFoundError("Directory not found")
        items: list[dict[str, Any]] = []
        for current, dirs, files in os.walk(base, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.relative_to(base).parts)
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not (current_path / d).is_symlink())
            if depth >= 4:
                dirs[:] = []
            for dirname in dirs:
                p = current_path / dirname
                items.append({"type": "dir", "path": p.relative_to(workspace).as_posix()})
                if len(items) >= MAX_LIST_ITEMS:
                    break
            if len(items) >= MAX_LIST_ITEMS:
                break
            for filename in sorted(files):
                p = current_path / filename
                if p.is_symlink():
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = None
                items.append({"type": "file", "path": p.relative_to(workspace).as_posix(), "size": size})
                if len(items) >= MAX_LIST_ITEMS:
                    break
            if len(items) >= MAX_LIST_ITEMS:
                break
        return {"ok": True, "path": self._relative_path(raw_path), "items": items, "truncated": len(items) >= MAX_LIST_ITEMS}

    def _read_file(self, workspace_id: str, raw_path: str) -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        target = self._safe_target(workspace, raw_path)
        if not target.is_file():
            raise FileNotFoundError("File not found")
        if target.stat().st_size > MAX_TEXT_FILE:
            raise ValueError(f"File exceeds {MAX_TEXT_FILE} bytes")
        try:
            content = target.read_text("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("File is not UTF-8 text") from exc
        return {"ok": True, "path": self._relative_path(raw_path), "content": content}

    def _write_file(self, workspace_id: str, raw_path: str, content: str) -> dict[str, Any]:
        if len(content.encode("utf-8")) > MAX_TEXT_FILE:
            raise ValueError(f"File exceeds {MAX_TEXT_FILE} bytes")
        workspace = self._workspace(workspace_id)
        target = self._safe_target(workspace, raw_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to follow symlinks even if they point back inside workspace.
        if target.is_symlink():
            raise ValueError("Writing through symlinks is not allowed")
        temp = target.with_name(target.name + f".genisi-{os.getpid()}.tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(target)
        return {"ok": True, "path": self._relative_path(raw_path), "bytes": len(content.encode("utf-8"))}

    async def _start_next(self, workspace_id: str, project_id: str = "") -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        package_json = workspace / "package.json"
        if not package_json.is_file():
            return {"ok": False, "error": "No package.json found. Create a Next.js project first."}
        if shutil.which("npm") is None:
            return {"ok": False, "error": "Node.js/npm are not installed in the deployment image."}

        async with self._lock:
            old = self._next_processes.get(workspace_id)
            if old and old.poll() is None:
                port = self._next_ports[workspace_id]
                return {"ok": True, "message": "Next.js is already running", "port": port,
                        "preview_path": f"/api/preview/{project_id}/" if project_id else None}

            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
            sock.close()
            log_path = workspace / ".genisi-next.log"
            log_file = log_path.open("ab")
            env = self._clean_env(workspace)
            env["PORT"] = str(port)
            try:
                proc = subprocess.Popen(
                    ["npm", "run", "dev", "--", "--hostname", "127.0.0.1", "--port", str(port)],
                    cwd=str(workspace),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    close_fds=True,
                    start_new_session=True,
                )
            except FileNotFoundError:
                log_file.close()
                return {"ok": False, "error": "npm is not installed."}
            log_file.close()
            self._next_processes[workspace_id] = proc
            self._next_ports[workspace_id] = port

        await asyncio.sleep(1.2)
        if proc.poll() is not None:
            try:
                tail = log_path.read_text("utf-8", errors="replace")[-5000:]
            except OSError:
                tail = ""
            return {"ok": False, "error": "Next.js process exited during startup.", "log": tail}

        return {
            "ok": True,
            "message": "Next.js development server started",
            "port": port,
            "preview_path": f"/api/preview/{project_id}/" if project_id else None,
        }

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.enabled:
            raise RuntimeError("Set SANDBOX_MODE=local to enable the built-in application sandbox.")
        method = method.upper()
        payload = kwargs.get("json") or {}
        params = kwargs.get("params") or {}

        if method == "POST" and path == "/v1/exec":
            return await self._run(
                str(payload.get("workspace_id", "")),
                str(payload.get("command", ""))[:8000],
                str(payload.get("cwd", "."))[:300],
            )

        if method == "GET" and path.startswith("/v1/files/"):
            workspace_id = path.rsplit("/", 1)[-1]
            return self._list_files(workspace_id, str(params.get("path", ".")))

        if method == "POST" and path == "/v1/files/read":
            return self._read_file(str(payload.get("workspace_id", "")), str(payload.get("path", "")))

        if method == "POST" and path == "/v1/files/write":
            return self._write_file(
                str(payload.get("workspace_id", "")),
                str(payload.get("path", "")),
                str(payload.get("content", "")),
            )

        if method == "POST" and path == "/v1/next/start":
            return await self._start_next(
                str(payload.get("workspace_id", "")),
                str(payload.get("project_id", "")),
            )

        raise ValueError(f"Unknown local sandbox operation: {method} {path}")

    async def preview(self, workspace_id: str, path: str = "", params: Any = None) -> httpx.Response:
        if not self.enabled:
            raise RuntimeError("Local sandbox is disabled")
        self._workspace(workspace_id)
        proc = self._next_processes.get(workspace_id)
        port = self._next_ports.get(workspace_id)
        if not proc or proc.poll() is not None or not port:
            raise RuntimeError("Next.js is not running for this project. Start it from the agent first.")
        clean_path = self._relative_path(path) if path else "."
        route = "" if clean_path == "." else clean_path
        url = f"http://127.0.0.1:{port}/{route}"
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            return await client.get(url, params=params or {})


# Shared instance used by app/main.py and app/agent.py.
worker = WorkerClient()
