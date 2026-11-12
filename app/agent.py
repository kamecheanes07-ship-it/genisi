from __future__ import annotations

import json
import os
import re
from typing import Any

from .worker_client import worker
from .windows12_prompt import WINDOWS12_SYSTEM_PROMPT

SYSTEM_PROMPT = """You are Genisi, the AnesNT autonomous coding agent.
Work like a careful senior software engineer. Understand the user's goal, inspect the project before changing it, make focused edits, run relevant checks, and report what actually happened.
You have access to a per-account project folder through Genisi local workspace tools. This is a lightweight workspace runner inside the application container, NOT a separate Docker container or hardened multi-tenant sandbox. Use tools rather than pretending to read or modify files. Never claim a command, test, file write, or preview succeeded unless the tool confirms it. Before destructive operations (deleting many files, overwriting user work, database resets), explain the risk and ask the user for approval in chat. Do not request or expose secrets. Keep all file paths relative to the project workspace. Use one terminal command per tool call; do not use shell chaining, pipes, redirects, shell interpreters, or inline -c code. Prefer Next.js conventions when building a Next.js app. Start with a short plan for multi-step tasks.

WINDOWS 12 BUILT-IN DESIGN MODE (MANDATORY WHEN ACTIVATED):
A complete, self-contained Windows 12 concept-desktop design specification is embedded in the Genisi 4 Atlas application. The host automatically appends it as a separate system message whenever the current request or its contextual follow-up is about Windows 12. This design mode requires NO HTML reference, repository file, Hugging Face Dataset read, screenshot, or user attachment. Never ask for such a file, never claim you loaded one, and never stop because a reference file is missing. Build from the embedded specification. Do not describe the concept as an official Microsoft product or real operating system. Follow the full specification, preserve its listed application families and interactions, implement real working behavior rather than decorative placeholders, and obey its delivery and acceptance rules. If the user asks for a new full interface, create the complete concept from the specification. If the user asks for a change, preserve all existing features and visual identity while making only the requested change. Do not fabricate completed tests or previews.
"""

_WINDOWS12_PATTERN = re.compile(
    r"(?:windows\s*[-_]?\s*12|win\s*[-_]?\s*12|ويندوز\s*[-_]?\s*(?:12|١٢)|نوافذ\s*[-_]?\s*(?:12|١٢))",
    re.IGNORECASE,
)
_WINDOWS12_FOLLOWUP_PATTERN = re.compile(
    r"(?:أضف|اضف|زيد|زوّد|زود|غيّر|غير|بدّل|بدل|صلّح|صلح|أصلح|اصلح|احذف|حذف|اجعل|خلي|خليه|حسّن|حسن|عدّل|عدل|أكمل|اكمل|كمل|تابع|وسّع|وسع|طوّر|طور|استمر|اجعلها|اجعله|صغّر|صغر|كبّر|كبر|الشريط|القائمة|المستكشف|الخلفية|النوافذ|السطح المكتب|التطبيقات|الوضع الداكن|الشفافية|الزجاج|التأثيرات|taskbar|start menu|explorer|desktop|dark mode|wallpaper|mica|acrylic|icons|animations|add|change|fix|update|modify|remove|improve|continue|extend|implement|make it|keep the same|polish|refine)",
    re.IGNORECASE,
)


def needs_windows12_prompt(message: str, history: list[dict[str, str]] | None = None) -> bool:
    """Detect a Windows 12 request or a natural follow-up in the same topic."""
    if _WINDOWS12_PATTERN.search(message or ""):
        return True
    recent_user_text = [
        str(item.get("content", ""))
        for item in (history or [])[-8:]
        if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), str)
    ]
    recent_topic_messages = recent_user_text[-3:]
    return bool(any(_WINDOWS12_PATTERN.search(item) for item in recent_topic_messages)
                and _WINDOWS12_FOLLOWUP_PATTERN.search(message or ""))


def _build_messages(message: str, history: list[dict[str, str]] | None) -> list[dict[str, Any]]:
    safe_history: list[dict[str, str]] = []
    for item in (history or [])[-20:]:
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str):
            safe_history.append({"role": item["role"], "content": item["content"][:12000]})

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if needs_windows12_prompt(message, history):
        messages.append({"role": "system", "content": WINDOWS12_SYSTEM_PROMPT})
    messages.extend(safe_history)
    messages.append({"role": "user", "content": message[:12000]})
    return messages

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal_exec",
            "description": "Run one allowlisted command in the user's local Genisi workspace (not a Docker container). Use for npm, node, tests, and project commands. Shell chaining and shell interpreters are disabled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run inside /workspace."},
                    "cwd": {"type": "string", "description": "Working directory relative to /workspace. Default is '.'"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in the user's project workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to /workspace. Default '.'"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the user's project workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a UTF-8 text file in the user's project workspace. Parent folders are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_nextjs",
            "description": "Start the Next.js development server in the workspace and return its internal preview path.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


class Agent:
    def __init__(self) -> None:
        # Prefer an explicitly configured OpenAI-compatible provider. Optionally use
        # Hugging Face Inference Providers directly with the server-side HF token.
        hf_token = os.getenv("HF_TOKEN", "").strip()
        hf_model = os.getenv("HF_INFERENCE_MODEL", "").strip()
        self.base_url = os.getenv("QWEN_BASE_URL", "").strip() or ("https://router.huggingface.co/v1" if hf_token and hf_model else "")
        self.api_key = os.getenv("QWEN_API_KEY", "").strip() or (hf_token if hf_model else "")
        self.model = os.getenv("QWEN_MODEL", "").strip() or hf_model
        self.worker = worker

    async def run(
        self,
        user_id: str,
        workspace_id: str,
        project_id: str,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        if not self.base_url or not self.api_key:
            raise RuntimeError("Configure QWEN_BASE_URL + QWEN_API_KEY, or HF_INFERENCE_MODEL + an HF_TOKEN with Inference Providers access.")
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        disable_thinking = os.getenv("QWEN_DISABLE_THINKING", "false").lower() == "true"
        messages = _build_messages(message, history)
        for step in range(9):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.2,
            }
            if step < 8:
                kwargs["tools"] = TOOLS
                kwargs["tool_choice"] = "auto"
                kwargs["parallel_tool_calls"] = False
            if disable_thinking:
                kwargs["extra_body"] = {"enable_thinking": False}
            response = await client.chat.completions.create(**kwargs)
            assistant = response.choices[0].message
            assistant_message: dict[str, Any] = {"role": "assistant", "content": assistant.content or ""}
            if assistant.tool_calls:
                assistant_message["tool_calls"] = [call.model_dump(exclude_none=True) for call in assistant.tool_calls]
            messages.append(assistant_message)
            if not assistant.tool_calls:
                return assistant.content or "انتهى التنفيذ دون نص إضافي من النموذج."
            if step == 8:
                break
            for call in assistant.tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                    result = await self._execute(name, args, user_id, workspace_id, project_id)
                except Exception as exc:  # Return tool errors to the model so it can recover.
                    result = {"error": f"{type(exc).__name__}: {str(exc)[:1500]}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False)[:12000],
                })
        return "وصلت إلى حد خطوات التنفيذ لهذه الرسالة. راجع نتائج الأدوات أعلاه واطلب مني متابعة المهمة."

    async def stream_events(
        self,
        user_id: str,
        workspace_id: str,
        project_id: str,
        message: str,
        history: list[dict[str, str]] | None = None,
    ):
        """Stream answer/thinking deltas and execute requested workspace tools."""
        if not self.base_url or not self.api_key or not self.model:
            raise RuntimeError(
                "Configure QWEN_BASE_URL + QWEN_API_KEY, or HF_INFERENCE_MODEL + an HF_TOKEN with Inference Providers access."
            )
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        # Thinking is enabled by default for providers/models that support it.
        # Set QWEN_DISABLE_THINKING=true to force ordinary answer-only mode.
        disable_thinking = os.getenv("QWEN_DISABLE_THINKING", "false").lower() == "true"
        messages = _build_messages(message, history)
        try:
            for step in range(9):
                if step > 0:
                    yield {"type": "thinking_reset"}
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.2,
                    "stream": True,
                }
                if step < 8:
                    kwargs["tools"] = TOOLS
                    kwargs["tool_choice"] = "auto"
                    kwargs["parallel_tool_calls"] = False
                if disable_thinking:
                    kwargs["extra_body"] = {"enable_thinking": False}

                stream = await client.chat.completions.create(**kwargs)
                answer_parts: list[str] = []
                tool_calls: dict[int, dict[str, str]] = {}

                async for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                    if isinstance(reasoning, str) and reasoning:
                        yield {"type": "thinking_delta", "data": reasoning}
                    content = getattr(delta, "content", None)
                    if isinstance(content, str) and content:
                        answer_parts.append(content)
                        yield {"type": "answer_delta", "data": content}

                    for call in (getattr(delta, "tool_calls", None) or []):
                        index = int(getattr(call, "index", 0) or 0)
                        item = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        call_id = getattr(call, "id", None)
                        if call_id:
                            item["id"] = str(call_id)
                        function = getattr(call, "function", None)
                        if function is not None:
                            name = getattr(function, "name", None)
                            arguments = getattr(function, "arguments", None)
                            if name:
                                item["name"] += str(name)
                            if arguments:
                                item["arguments"] += str(arguments)

                answer_text = "".join(answer_parts)
                normalized_calls = []
                for index in sorted(tool_calls):
                    item = tool_calls[index]
                    if item["id"] and item["name"]:
                        normalized_calls.append({
                            "id": item["id"],
                            "type": "function",
                            "function": {
                                "name": item["name"],
                                "arguments": item["arguments"] or "{}",
                            },
                        })

                assistant_message: dict[str, Any] = {"role": "assistant", "content": answer_text or None}
                if normalized_calls:
                    assistant_message["tool_calls"] = normalized_calls
                messages.append(assistant_message)

                if not normalized_calls:
                    yield {"type": "final", "content": answer_text or "لم يُرجع النموذج نصاً."}
                    return

                if step == 8:
                    yield {"type": "final", "content": answer_text or "وصل الوكيل إلى الحد الأقصى لخطوات الأدوات."}
                    return

                for call in normalized_calls:
                    name = call["function"]["name"]
                    raw_args = call["function"]["arguments"]
                    yield {"type": "tool_start", "tool": name, "args": {}}
                    try:
                        args = json.loads(raw_args or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("Tool arguments must be a JSON object")
                        result = await self._execute(name, args, user_id, workspace_id, project_id)
                    except Exception as exc:
                        result = {"error": f"{type(exc).__name__}: {str(exc)[:1500]}"}
                    yield {"type": "tool", "tool": name, "result": result}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False)[:12000],
                    })

            yield {"type": "final", "content": "وصل الوكيل إلى الحد الأقصى لخطوات التنفيذ."}
        finally:
            await client.close()

    async def _execute(self, name: str, args: dict[str, Any], user_id: str, workspace_id: str, project_id: str) -> Any:
        if name == "terminal_exec":
            return await self.worker.request("POST", "/v1/exec", json={
                "workspace_id": workspace_id,
                "command": str(args.get("command", ""))[:8000],
                "cwd": str(args.get("cwd", "."))[:300],
            })
        if name == "list_files":
            return await self.worker.request("GET", f"/v1/files/{workspace_id}", params={"path": args.get("path", ".")})
        if name == "read_file":
            return await self.worker.request("POST", "/v1/files/read", json={
                "workspace_id": workspace_id, "path": args.get("path", "")
            })
        if name == "write_file":
            raw_path = str(args.get("path", "")).replace("\\", "/").strip("/")
            return await self.worker.request("POST", "/v1/files/write", json={
                "workspace_id": workspace_id,
                "path": raw_path,
                "content": args.get("content", ""),
            })
        if name == "start_nextjs":
            return await self.worker.request("POST", "/v1/next/start", json={"workspace_id": workspace_id, "project_id": project_id})
        return {"error": f"Unknown tool: {name}"}
