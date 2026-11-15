"""Helpers for the public Ornith Gradio chat Space."""
from typing import Any

SPACE = 'https://ornith-ai-ornith-1-5-122b.hf.space'


def _text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get('text') for part in content if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str)]
        return ''.join(parts) if parts else None
    return None


def assistant_snapshot(payload: Any, sent_count: int) -> str | None:
    # /bot_message returns one Chatbot component: [history]. Gradio 6
    # represents text as [{"type":"text","text":"..."}], but older
    # deployments can use plain strings.
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        return None
    history = payload[0]
    if len(history) <= sent_count:
        return None
    last = history[-1]
    if not isinstance(last, dict) or last.get('role') != 'assistant':
        return None
    return _text(last.get('content'))


def provider_error(text: str) -> bool:
    # The Space's bot_message catches upstream errors and yields them as an
    # assistant message, not a Gradio error event. Do not pass through details.
    s = text.lstrip().lower()
    return s.startswith(('⚠️ ornith api error:', '⚠ ornith api error:', '⚠️ no api key configured', '⚠ no api key configured'))


def chat_history(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{'role': m['role'], 'content': [{'type': 'text', 'text': m['content']}]} for m in messages]
