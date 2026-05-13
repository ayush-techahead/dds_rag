"""First-reply session title: small LLM call → JSON {title}; mirrors router JSON parsing."""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.chat.llm_client import ChatCompletionClient

logger = get_logger(__name__)

_SESSION_TITLE_MAX_LEN = 120

_SYSTEM = """
You name a chat session for a sidebar list. Given the first user message and the assistant's
first reply, output ONLY valid JSON (no markdown fences):
{"title":"..."}

Rules:
- title: short phrase (under 80 characters ideally), no quotes inside, no trailing punctuation
  clusters, describe the topic (not "Chat" or "Conversation").
- Use the same language as the user's message when practical.
""".strip()


def _parse_title_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    return json.loads(text)


def _clip(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _sanitize_title(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    one = " ".join(raw.split())
    if not one:
        return None
    if len(one) > _SESSION_TITLE_MAX_LEN:
        one = one[: _SESSION_TITLE_MAX_LEN - 1] + "…"
    return one


async def suggest_session_title(
    llm: ChatCompletionClient,
    *,
    user_text: str,
    assistant_text: str,
) -> str | None:
    cap = settings.CHAT_SESSION_TITLE_PROMPT_MAX_CHARS
    user_blob = _clip(user_text, cap)
    assistant_blob = _clip(assistant_text, cap)
    user_content = (
        f"First user message:\n{user_blob}\n\n"
        f"First assistant reply:\n{assistant_blob}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_content},
    ]
    try:
        raw = await llm.complete(
            messages,
            model=settings.CHAT_SESSION_TITLE_MODEL,
            max_tokens=settings.CHAT_SESSION_TITLE_MAX_TOKENS,
            temperature=settings.CHAT_SESSION_TITLE_TEMPERATURE,
        )
        data = _parse_title_json(raw)
        title = data.get("title")
        if not isinstance(title, str):
            logger.warning("Session title JSON missing string title", extra={"preview": raw[:200]})
            return None
        return _sanitize_title(title.strip())
    except Exception as exc:
        logger.warning(
            "Session title generation failed",
            extra={"error": str(exc)},
        )
        return None
