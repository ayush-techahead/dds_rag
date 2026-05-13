"""Classifier LLM: routes turns using transcript + website description (single small call)."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.chat.llm_client import ChatCompletionClient
from app.modules.chat.website_context import WEBSITE_DESCRIPTION

logger = get_logger(__name__)


class RouteDecision(BaseModel):
    intent: Literal["greeting", "knowledge", "out_of_scope", "answered_from_overview"]
    search_query: str | None = Field(
        default=None,
        description="Standalone query for embedding search when intent is knowledge.",
    )
    overview_answer: str | None = Field(
        default=None,
        description="Draft answer from WEBSITE DESCRIPTION only when intent is answered_from_overview.",
    )


_ROUTER_INSTRUCTIONS = """
You classify chat turns for an assistant that has (a) the WEBSITE DESCRIPTION below and
(b) separate uploaded/indexed documentation searched via embeddings.

Output ONLY valid JSON (no markdown fences):
{"intent":"greeting"|"out_of_scope"|"knowledge"|"answered_from_overview","search_query":string|null,"overview_answer":string|null}

Rules:
- greeting: hellos, thanks, small talk
  without needing facts from the site or documents.
- out_of_scope: unrelated, harmful, or clearly not about this organization/docs.
- answered_from_overview: ONLY for broad, non-case-specific questions whose answer is fully
  contained in the WEBSITE DESCRIPTION below (e.g. what DDS is, mission, general contact info,
  that DDS oversees regional centers, high-level list of program names, Lanterman Act mention,
  languages offered). Set overview_answer to a concise factual draft (plain text).
  Set search_query to null. Do not invent facts beyond the description.
  NOT answered_from_overview (use knowledge instead): eligibility, ages or age ranges,
  individualized recommendations (training, therapy, services for a specific child/person),
  forms, timelines, how to apply, complaints/appeals, or anything needing document detail.
- knowledge: the user needs specifics that require indexed/uploaded documents (forms,
  directives, detailed procedures), or the description is not enough, or the question is
  case-specific as above. Set search_query to
  one English sentence for semantic search. Set overview_answer to null.

If unsure between answered_from_overview and knowledge, prefer knowledge when detailed
policy or document-specific content may apply.

WEBSITE DESCRIPTION:
"""


def _message_requires_indexed_knowledge(user_text: str) -> bool:
    """When True, never use answered_from_overview — case-specific or needs indexed docs."""
    t = (user_text or "").lower()
    if re.search(r"\b\d{1,2}\s*(?:year|yr)s?\s*old\b", t):
        return True
    if re.search(r"\bage\s*:?\s*\d{1,2}\b", t):
        return True
    if re.search(r"\b(?:recommend|suggest|what should|best \w+ for|need help with)\b", t):
        return True
    if any(
        k in t
        for k in (
            "iep",
            "ifsp",
            "eligib",
            "therap",
            "aba",
            "intervention",
            "referral",
            "intake",
            "apply for",
            "how do i get",
            "how to get",
        )
    ):
        return True
    if "training" in t and any(
        w in t for w in ("autism", "child", "kid", "toddler", "baby", "preschool", "school")
    ):
        return True
    return False


def _router_system_content() -> str:
    return _ROUTER_INSTRUCTIONS.strip() + "\n\n" + WEBSITE_DESCRIPTION.strip()


def _parse_router_json(raw: str) -> dict:
    text = raw.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    return json.loads(text)


async def classify_route(
    llm: ChatCompletionClient,
    transcript: str,
    *,
    latest_user_text: str,
) -> RouteDecision:
    """Single CHAT_ROUTER_MODEL call with transcript + website description → structured route."""
    user_blob = (
        f"Recent conversation (newest at bottom):\n{transcript}\n\n"
        f'Latest user message (verbatim): "{latest_user_text}"'
    )
    messages = [
        {"role": "system", "content": _router_system_content()},
        {"role": "user", "content": user_blob},
    ]
    try:
        raw = await llm.complete(messages, model=settings.CHAT_ROUTER_MODEL)
        if settings.DEBUG:
            rp = raw.strip().replace("\n", " ")
            logger.debug(
                "router.raw_response",
                extra={"preview": rp[:400] + ("…" if len(rp) > 400 else "")},
            )
        data = _parse_router_json(raw)
        intent = data.get("intent")
        sq = data.get("search_query")
        oa = data.get("overview_answer")
        if intent not in ("greeting", "knowledge", "out_of_scope", "answered_from_overview"):
            raise ValueError("invalid intent")
        if sq is not None and not isinstance(sq, str):
            sq = None
        if oa is not None and not isinstance(oa, str):
            oa = None
        sq = sq.strip() if sq else None
        oa = oa.strip() if oa else None

        decision = RouteDecision(
            intent=intent,
            search_query=sq,
            overview_answer=oa,
        )

        if decision.intent == "knowledge":
            decision = RouteDecision(
                intent="knowledge",
                search_query=decision.search_query or latest_user_text.strip() or None,
                overview_answer=None,
            )
        elif decision.intent == "answered_from_overview":
            if _message_requires_indexed_knowledge(latest_user_text):
                decision = RouteDecision(
                    intent="knowledge",
                    search_query=latest_user_text.strip() or None,
                    overview_answer=None,
                )
            elif not decision.overview_answer:
                decision = RouteDecision(
                    intent="knowledge",
                    search_query=latest_user_text.strip() or None,
                    overview_answer=None,
                )
            else:
                decision = RouteDecision(
                    intent="answered_from_overview",
                    search_query=None,
                    overview_answer=decision.overview_answer,
                )
        if decision.intent == "greeting":
            return RouteDecision(intent="greeting", search_query=None, overview_answer=None)
        if decision.intent == "out_of_scope":
            return RouteDecision(intent="out_of_scope", search_query=None, overview_answer=None)
        return decision
    except Exception as exc:
        logger.warning(
            "Router JSON parse failed; defaulting to knowledge",
            extra={"error": str(exc)},
        )
        return RouteDecision(
            intent="knowledge",
            search_query=latest_user_text.strip() or None,
            overview_answer=None,
        )
