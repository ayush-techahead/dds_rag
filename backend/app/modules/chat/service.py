import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from beanie import PydanticObjectId
from pymongo.errors import BulkWriteError, DuplicateKeyError

from app.core.config import settings
from app.core.exceptions import AppException, NotFoundException
from app.core.logging import get_logger
from app.modules.chat.intent import classify_route
from app.modules.chat.llm_client import ChatCompletionClient
from app.modules.chat.model import ChatMessage, ChatSession, MessageRole, MessageSource
from app.modules.chat.repository import ChatRepository
from app.modules.chat.schemas import (
    ChatMessageCreate,
    ChatMessageResponse,
    ChatSessionCreate,
    ChatSessionDetailResponse,
    ChatSessionResponse,
    VoiceCommitRequest,
)
from app.modules.chat.session_title import suggest_session_title
from app.modules.chat.website_context import WEBSITE_DESCRIPTION
from app.modules.embeddings.openai_embeddings import OpenAIEmbeddingProvider
from app.modules.embeddings.query_cache import QueryEmbeddingCache, get_query_embedding_cache
from app.modules.vector_store.qdrant import QdrantVectorStore

logger = get_logger(__name__)


def _is_dds_website_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    host = parsed.netloc.lower()
    return parsed.scheme in {"http", "https"} and (
        host == "dds.ca.gov" or host.endswith(".dds.ca.gov")
    )


def _looks_like_markdown_filename(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.endswith((".md", ".markdown"))


def _preview(text: str, max_len: int = 160) -> str:
    """Single-line truncation for log lines (avoids huge or multi-line dumps)."""
    one = " ".join((text or "").split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1] + "…"


def _format_indexed_passage(
    idx: int,
    payload: dict[str, Any],
    *,
    excerpt_chars: int = 12000,
) -> str:
    """Build a source card for the responder: provenance + excerpt (Qdrant payload).

    ``excerpt_chars`` caps the excerpt body. The text-chat responder uses the default
    (generous) budget; the voice path passes a much smaller value because the Realtime
    model's working context is more expensive and prone to mid-response truncation.
    """
    lines: list[str] = [f"[{idx}]"]
    url = payload.get("url")
    has_dds_url = _is_dds_website_url(url)
    if has_dds_url:
        lines.append(f"DDS website URL: {url.strip()}")
        title = payload.get("title")
        if isinstance(title, str) and title.strip() and not _looks_like_markdown_filename(title):
            lines.append(f"Page title: {title.strip()}")
    sec = payload.get("section_title")
    if isinstance(sec, str) and sec.strip() and not _looks_like_markdown_filename(sec):
        lines.append(f"Section: {sec.strip()}")
    path = payload.get("section_path")
    if isinstance(path, list) and path:
        path_s = " > ".join(
            str(p) for p in path if str(p).strip() and not _looks_like_markdown_filename(str(p))
        )
        if path_s:
            lines.append(f"Section path: {path_s}")
    excerpt = (payload.get("text") or "").strip()
    if excerpt_chars > 0 and len(excerpt) > excerpt_chars:
        excerpt = excerpt[:excerpt_chars] + "…"
    lines.append(f"Excerpt:\n{excerpt}")
    return "\n".join(lines)


# Strong references for background tasks: ``asyncio.create_task`` only holds a weak
# reference internally, so without this set the task can be garbage-collected before
# it finishes. Tasks remove themselves on completion via ``add_done_callback``.
_background_tasks: set[asyncio.Task] = set()


def _is_duplicate_key_error(exc: BaseException) -> bool:
    """Detect the unique-index conflict from either ``insert_many`` or ``insert``."""
    if isinstance(exc, DuplicateKeyError):
        return True
    if isinstance(exc, BulkWriteError):
        details = getattr(exc, "details", None) or {}
        for err in details.get("writeErrors", []) or []:
            if isinstance(err, dict) and err.get("code") == 11000:
                return True
    return False


_GREETING_RESPONDER_SYSTEM = (
    "You are a candid, supportive DDS chat assistant. Reply warmly and briefly to "
    "greetings or light small talk, then offer a concrete way you can help. "
    "Do not invent specific DDS facts. Never mention markdown files, uploaded files, "
    "or the behind-the-scenes retrieval process. Ask at most one focused "
    "follow-up question when it would help the user take the next step."
)

_OUT_OF_SCOPE_SYSTEM = (
    "You are a candid, supportive DDS chat assistant. The user's message is outside "
    "DDS-related help. Briefly say what you can help with instead, without scolding. "
    "Never mention markdown files, uploaded files, or the behind-the-scenes retrieval "
    "process. Ask at most one focused DDS-related follow-up question if useful."
)

_NO_KNOWLEDGE_HIT_SYSTEM = (
    "You are a candid, supportive DDS chat assistant. The information is not available "
    "for this specific question. Say that plainly and directly. Do not say you checked, "
    "searched, reviewed, or looked anything up. If possible, give a safe next step "
    "such as contacting a regional center, DDS, or asking a more specific "
    "question. Never mention markdown files, uploaded files, filenames, archives, "
    "document IDs, or retrieval. Ask at most one focused follow-up question."
)

_MERGED_KNOWLEDGE_SYSTEM_TEMPLATE = (
    "You are a candid, supportive DDS chat assistant. Answer the user's latest message "
    "directly in plain language using the context below.\n\n"
    "Context available:\n"
    "(1) WEBSITE OVERVIEW — mission, contacts, structure, and general DDS facts.\n"
    "(2) CLASSIFIER DRAFT — short factual draft from routing when only the overview applied; "
    'may read "(none)".\n'
    "(3) INDEXED PASSAGES — numbered cards with DDS website URL/page/section metadata "
    "when available and excerpts; "
    "prefer these for procedures, eligibility detail, and anything case-specific.\n\n"
    "Rules:\n"
    "- Start with the useful answer, not a preface. Do not say you are checking, "
    "reviewing, searching, looking anything up, or using retrieval details. Do not "
    "include process or citation-style phrases in the answer.\n"
    "- Use a supportive, practical tone. Be candid about limits, but do not over-apologize.\n"
    "- If indexed passages conflict with the overview on specifics, trust passages for "
    "operational detail.\n"
    "- Do not invent facts. State supported claims directly; use "
    '"may apply" or "I do not have enough detail to confirm that" when appropriate.\n'
    "- Never mention markdown files, uploaded filenames, archive filenames, or document IDs "
    "in the answer.\n"
    "- Use only actual DDS website URLs (dds.ca.gov) for links; never cite a markdown "
    "file or uploaded document as a source.\n"
    "- For age-, eligibility-, or individualized service questions: only name programs or "
    "next steps if the overview or passages explicitly support them; otherwise say "
    '"I do not have that information available" and what detail would help.\n'
    "- Include specific DDS links only when they are relevant to the user's question or "
    "the user asks for links. Put them at the end under `Helpful links` with no more "
    "than three bullets. Use only DDS website URLs from the context you used, or "
    "https://www.dds.ca.gov for broad overview answers. Do not add a link section when "
    "no useful DDS URL is available.\n"
    "- End with one purposeful follow-up question or next step only when it would help "
    "the user move forward; otherwise stop after the answer.\n\n"
    "WEBSITE OVERVIEW:\n{website}\n\n"
    "CLASSIFIER DRAFT:\n{classifier}\n\n"
    "INDEXED PASSAGES:\n{indexed}"
)


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


class ChatService:
    def __init__(
        self,
        repository: ChatRepository | None = None,
        llm: ChatCompletionClient | None = None,
        embeddings: OpenAIEmbeddingProvider | None = None,
        vector_store: QdrantVectorStore | None = None,
        embedding_cache: QueryEmbeddingCache | None = None,
    ) -> None:
        self.repository = repository or ChatRepository()
        self.llm = llm or ChatCompletionClient()
        self.embeddings = embeddings or OpenAIEmbeddingProvider()
        self.vector_store = vector_store or QdrantVectorStore()
        self.embedding_cache = embedding_cache or get_query_embedding_cache()

    async def require_chat_session(self, user_id: str, session_id: str) -> ChatSession:
        session = await self.repository.get_session_for_user(session_id, user_id)
        if session is None:
            raise NotFoundException("Chat session not found")
        return session

    async def lookup_indexed_passages(
        self,
        user_id: str,
        query: str,
        *,
        session_id: str | None = None,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Embed ``query``, search Qdrant, return passages scoring >= min threshold.

        ``top_k`` overrides ``CHAT_RAG_TOP_K`` for callers (e.g. the voice path) that
        need a smaller retrieval window to keep the model's working context tight.
        """
        log_extra: dict[str, object] = {
            "user_id": user_id,
            "query_preview": _preview(query),
        }
        if session_id:
            log_extra["session_id"] = session_id

        logger.info(
            "chat.knowledge.query",
            extra={**log_extra, "embedding_model": settings.OPENAI_EMBEDDING_MODEL},
        )

        embed_t0 = time.perf_counter()
        embedding_model = settings.OPENAI_EMBEDDING_MODEL

        async def _compute_embedding() -> list[float]:
            vectors = await self.embeddings.embed_documents([query])
            if not vectors or not vectors[0]:
                raise AppException("Embedding provider returned no vector")
            return vectors[0]

        query_vector, cache_hit = await self.embedding_cache.get_or_compute(
            embedding_model,
            query,
            _compute_embedding,
        )
        limit = top_k if isinstance(top_k, int) and top_k > 0 else settings.CHAT_RAG_TOP_K
        hits = await self.vector_store.search_user_chunks(
            user_id=user_id,
            query_vector=query_vector,
            limit=limit,
        )
        min_score = settings.CHAT_RAG_MIN_SCORE
        good_hits = [(s, p) for s, p in hits if s >= min_score]
        passage_payloads = [p if isinstance(p, dict) else {} for _, p in good_hits]
        scores_raw = [round(s, 4) for s, _ in hits]
        scores_good = [round(s, 4) for s, _ in good_hits]
        logger.info(
            "chat.knowledge.qdrant.done",
            extra={
                **log_extra,
                "retrieve_elapsed_ms": round((time.perf_counter() - embed_t0) * 1000, 1),
                "embedding_cache_hit": cache_hit,
                "collection": settings.QDRANT_COLLECTION_NAME,
                "top_k_requested": limit,
                "min_score": min_score,
                "hits_returned": len(hits),
                "hits_above_threshold": len(good_hits),
                "scores_top_k": scores_raw,
                "scores_above_threshold": scores_good,
            },
        )
        return passage_payloads

    async def lookup_documentation_tool_result(
        self,
        user_id: str,
        query: str,
        *,
        session_id: str | None = None,
    ) -> str:
        """Plain-text tool output for OpenAI Realtime ``lookup_documentation``.

        Uses the voice-specific RAG budget (``CHAT_RAG_VOICE_TOP_K`` /
        ``CHAT_RAG_VOICE_EXCERPT_CHARS``). Smaller defaults than the text path keep
        each Realtime turn's working context within token limits so the model is
        less likely to cut audio off mid-response.
        """
        t0 = time.perf_counter()
        q = query.strip()
        if not q:
            return (
                "NO_RELEVANT_INFO: The information is not available because the request "
                "was not specific enough. Ask one clarifying question."
            )
        top_k = settings.CHAT_RAG_VOICE_TOP_K
        excerpt_chars = settings.CHAT_RAG_VOICE_EXCERPT_CHARS
        passage_payloads = await self.lookup_indexed_passages(
            user_id,
            q,
            session_id=session_id,
            top_k=top_k,
        )
        has_excerpt = any((p.get("text") or "").strip() for p in passage_payloads)
        if not passage_payloads or not has_excerpt:
            logger.info(
                "chat.realtime.tool.lookup.done",
                extra={
                    "user_id": user_id,
                    "session_id": session_id,
                    "query_preview": _preview(q),
                    "result": "no_sources",
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                },
            )
            return (
                "NO_RELEVANT_INFO: The information is not available for this specific "
                "query. Tell the user directly that the information is not available. "
                "Do not mention internal material, documents, or lookup."
            )
        parts: list[str] = []
        for i, payload in enumerate(passage_payloads, start=1):
            parts.append(
                _format_indexed_passage(i, payload, excerpt_chars=excerpt_chars),
            )
        result = "\n\n".join(parts)
        logger.info(
            "chat.realtime.tool.lookup.done",
            extra={
                "user_id": user_id,
                "session_id": session_id,
                "query_preview": _preview(q),
                "result": "ok",
                "passages": len(passage_payloads),
                "result_chars": len(result),
                "voice_top_k": top_k,
                "voice_excerpt_chars": excerpt_chars,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        )
        return result

    async def commit_voice_turn(
        self,
        session: ChatSession,
        user_id: str,
        payload: VoiceCommitRequest,
    ) -> tuple[list[ChatMessageResponse], str | None]:
        """Persist user + assistant text from a completed Realtime voice turn.

        Stability contract:
        - **Idempotent retries** (``client_turn_id``): a complete pair already stored
          for this session+id is returned verbatim; a partial orphan from a previous
          crashed attempt is deleted before re-inserting.
        - **Concurrent retries:** a partial unique index on
          ``(session_id, client_turn_id, role)`` guarantees that two simultaneous
          retries can't both insert — the loser raises ``DuplicateKeyError`` /
          ``BulkWriteError`` which we reconcile by re-reading the winner's pair.
        - **Tighter write window:** both messages are inserted via a single
          ``insert_many`` call instead of two sequential ``insert``s.
        - **Source tagging:** rows are marked ``source=voice`` with the optional
          ``client_turn_id`` / ``openai_response_id`` breadcrumbs.
        """
        t0 = time.perf_counter()
        session_id = str(session.id)
        user_text = payload.user_transcript.strip()
        assistant_text = payload.assistant_transcript.strip()
        client_turn_id = (payload.client_turn_id or "").strip() or None
        openai_response_id = (payload.openai_response_id or "").strip() or None
        log_extra: dict[str, object] = {
            "session_id": session_id,
            "user_id": user_id,
            "client_turn_id": client_turn_id,
            "user_chars": len(user_text),
            "assistant_chars": len(assistant_text),
        }

        if client_turn_id:
            idempotent = await self._reconcile_voice_turn_by_client_id(
                session_id,
                client_turn_id,
            )
            if idempotent is not None:
                logger.info(
                    "chat.voice.commit.idempotent_hit",
                    extra={
                        **log_extra,
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    },
                )
                return idempotent, None

        user_message = ChatMessage(
            session_id=PydanticObjectId(session_id),
            user_id=PydanticObjectId(user_id),
            role=MessageRole.USER,
            content=user_text,
            source=MessageSource.VOICE,
            client_turn_id=client_turn_id,
            openai_response_id=openai_response_id,
        )
        assistant_message = ChatMessage(
            session_id=PydanticObjectId(session_id),
            user_id=PydanticObjectId(user_id),
            role=MessageRole.ASSISTANT,
            content=assistant_text,
            source=MessageSource.VOICE,
            client_turn_id=client_turn_id,
            openai_response_id=openai_response_id,
        )

        try:
            created_user, created_assistant = await self.repository.create_voice_turn(
                user_message,
                assistant_message,
            )
        except (DuplicateKeyError, BulkWriteError) as exc:
            if not _is_duplicate_key_error(exc) or not client_turn_id:
                raise
            # Race: a concurrent retry already inserted this turn. Read the winner.
            reconciled = await self._reconcile_voice_turn_by_client_id(
                session_id,
                client_turn_id,
            )
            if reconciled is None:
                # Conflict with no observable pair — surface the original error.
                raise
            logger.info(
                "chat.voice.commit.race_resolved",
                extra={
                    **log_extra,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                },
            )
            return reconciled, None

        # Title generation involves an LLM call; doing it inline would delay the
        # commit response by 1-3 s and block the SPA from rendering the assistant
        # bubble. For voice we touch the session synchronously, then schedule the
        # title generation as a background task so the client response returns now.
        await self.repository.touch_session(session)
        title_task = self._schedule_title_generation_if_first_assistant(
            session,
            session_id,
            user_text,
            assistant_text,
        )
        logger.info(
            "chat.voice.commit.done",
            extra={
                **log_extra,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "title_scheduled": title_task is not None,
            },
        )
        return (
            [
                ChatMessageResponse.from_document(created_user),
                ChatMessageResponse.from_document(created_assistant),
            ],
            None,
        )

    def _schedule_title_generation_if_first_assistant(
        self,
        session: ChatSession,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> asyncio.Task | None:
        """Kick off async title generation for the voice path; never awaited by callers."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(
            self._maybe_generate_session_title(
                session,
                session_id,
                user_text,
                assistant_text,
            ),
            name=f"voice-title:{session_id}",
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return task

    async def _maybe_generate_session_title(
        self,
        session: ChatSession,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Background task: generate a title for the first assistant message, then save it.

        Errors are logged but never raised; the user response has already been sent.
        On the next ``GET /sessions/{id}`` the SPA will see the freshly stored title.
        """
        try:
            count = await self.repository.count_assistant_messages(session_id)
            if count != 1:
                return
            title = await suggest_session_title(
                self.llm,
                user_text=user_text,
                assistant_text=assistant_text,
            )
            if title:
                await self.repository.set_session_title(session, title)
                logger.info(
                    "chat.voice.title.set",
                    extra={"session_id": session_id, "title": title},
                )
        except Exception:
            logger.exception(
                "chat.voice.title.generation_failed",
                extra={"session_id": session_id},
            )

    async def _reconcile_voice_turn_by_client_id(
        self,
        session_id: str,
        client_turn_id: str,
    ) -> list[ChatMessageResponse] | None:
        """Return the persisted pair for ``client_turn_id``, or ``None`` if none found.

        Side effect: a single orphan row from a previous crashed attempt is deleted
        so the next insert can succeed under the unique index. This is the lazy GC
        for the "process killed between user-insert and assistant-insert" case.
        """
        existing = await self.repository.find_voice_turn_by_client_id(
            session_id,
            client_turn_id,
        )
        if len(existing) >= 2:
            user_msg = next(
                (m for m in existing if m.role == MessageRole.USER),
                existing[0],
            )
            assistant_msg = next(
                (m for m in existing if m.role == MessageRole.ASSISTANT),
                existing[-1],
            )
            return [
                ChatMessageResponse.from_document(user_msg),
                ChatMessageResponse.from_document(assistant_msg),
            ]
        if len(existing) == 1:
            logger.warning(
                "chat.voice.commit.orphan_cleanup",
                extra={
                    "session_id": session_id,
                    "client_turn_id": client_turn_id,
                    "orphan_role": existing[0].role,
                },
            )
            await self.repository.delete_message(existing[0])
        return None

    async def _finalize_session_after_assistant(
        self,
        session: ChatSession,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> str | None:
        """Touch session; on first assistant message, overwrite title when generation succeeds."""
        count = await self.repository.count_assistant_messages(session_id)
        if count != 1:
            await self.repository.touch_session(session)
            return None
        title = await suggest_session_title(
            self.llm,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        if title:
            await self.repository.set_session_title(session, title)
            return title
        await self.repository.touch_session(session)
        return None

    @staticmethod
    def _router_transcript(history: list[ChatMessage]) -> str:
        chronological = sorted(history, key=lambda m: m.created_at)
        window = chronological[-settings.CHAT_ROUTER_MAX_MESSAGES :]
        lines: list[str] = []
        for m in window:
            if not m.content.strip():
                continue
            label = "user" if m.role == MessageRole.USER else "assistant"
            lines.append(f"{label}: {m.content.strip()}")
        return "\n".join(lines)

    @staticmethod
    def _responder_user_turn(transcript: str, latest_user: str) -> str:
        return (
            f"Recent dialogue:\n{transcript}\n\n"
            f"Respond to the latest user message:\n{latest_user}"
        )

    def _messages_greeting(self, transcript: str, latest_user: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": _GREETING_RESPONDER_SYSTEM},
            {
                "role": "user",
                "content": self._responder_user_turn(transcript, latest_user),
            },
        ]

    def _messages_out_of_scope(self, transcript: str, latest_user: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": _OUT_OF_SCOPE_SYSTEM},
            {
                "role": "user",
                "content": self._responder_user_turn(transcript, latest_user),
            },
        ]

    def _messages_no_knowledge_hit(self, latest_user: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": _NO_KNOWLEDGE_HIT_SYSTEM},
            {"role": "user", "content": latest_user},
        ]

    def _messages_merged_knowledge(
        self,
        classifier_note: str | None,
        passage_payloads: list[dict[str, Any]],
        latest_user: str,
    ) -> list[dict[str, str]]:
        classifier_block = (classifier_note or "").strip() or "(none)"
        parts: list[str] = []
        for i, payload in enumerate(passage_payloads, start=1):
            if not isinstance(payload, dict):
                payload = {}
            parts.append(_format_indexed_passage(i, payload))
        indexed = "\n\n".join(parts) if parts else "(none)"
        website = WEBSITE_DESCRIPTION.strip()
        if len(website) > 32000:
            website = website[:32000] + "…"
        return [
            {
                "role": "system",
                "content": _MERGED_KNOWLEDGE_SYSTEM_TEMPLATE.format(
                    website=website,
                    classifier=classifier_block,
                    indexed=indexed,
                ),
            },
            {"role": "user", "content": latest_user},
        ]

    async def _build_responder_messages(
        self,
        user_id: str,
        history: list[ChatMessage],
        latest_user: str,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, str]]:
        """Prepare responder prompts after history load.

        Step 1 (partial): transcript = last CHAT_ROUTER_MAX_MESSAGES turns (same window as classifier context cap).
        Step 2: classify_route — single small LLM with transcript + website description (JSON route).
        Step 3: greeting | out_of_scope | answered_from_overview | knowledge (embed/search → merge).
        Step 4 (prep): messages for CHAT_RESPONDER_MODEL; streaming is done by caller.
        """
        pipeline_t0 = time.perf_counter()
        log_extra: dict[str, object] = {
            "user_id": user_id,
            "history_messages": len(history),
        }
        if session_id:
            log_extra["session_id"] = session_id

        # Step 1 — bounded transcript for router/RAG context (limit = CHAT_ROUTER_MAX_MESSAGES).
        transcript = self._router_transcript(history)
        log_extra["transcript_chars"] = len(transcript)
        logger.info(
            "chat.pipeline.start",
            extra={**log_extra, "latest_preview": _preview(latest_user)},
        )

        # Step 2 — classify (transcript + latest user); JSON from CHAT_ROUTER_MODEL.
        route = await classify_route(
            self.llm,
            transcript,
            latest_user_text=latest_user,
        )
        logger.info(
            "chat.router.done",
            extra={
                **log_extra,
                "intent": route.intent,
                "search_query_preview": _preview(route.search_query or ""),
                "overview_answer_preview": _preview(route.overview_answer or ""),
                "router_model": settings.CHAT_ROUTER_MODEL,
            },
        )

        # Step 3 — route decision and assemble context for the responder (no streaming yet).
        if route.intent == "greeting":
            logger.info(
                "chat.branch",
                extra={**log_extra, "branch": "greeting", "responder_model": settings.CHAT_RESPONDER_MODEL},
            )
            return self._messages_greeting(transcript, latest_user)
        if route.intent == "out_of_scope":
            logger.info(
                "chat.branch",
                extra={**log_extra, "branch": "out_of_scope", "responder_model": settings.CHAT_RESPONDER_MODEL},
            )
            return self._messages_out_of_scope(transcript, latest_user)

        if route.intent == "answered_from_overview":
            draft = (route.overview_answer or "").strip()
            logger.info(
                "chat.branch",
                extra={
                    **log_extra,
                    "branch": "answered_from_overview",
                    "classifier_draft_chars": len(draft),
                    "classifier_draft_preview": _preview(draft),
                    "responder_model": settings.CHAT_RESPONDER_MODEL,
                    "pipeline_elapsed_ms": round((time.perf_counter() - pipeline_t0) * 1000, 1),
                },
            )
            return self._messages_merged_knowledge(draft, [], latest_user)

        query = (route.search_query or latest_user).strip()

        if not query:
            logger.info(
                "chat.knowledge.empty_query",
                extra={**log_extra, "branch": "no_knowledge_hit_empty_query"},
            )
            return self._messages_no_knowledge_hit(latest_user)

        passage_payloads = await self.lookup_indexed_passages(
            user_id,
            query,
            session_id=session_id,
        )
        has_excerpt = any((p.get("text") or "").strip() for p in passage_payloads)

        if not passage_payloads or not has_excerpt:
            logger.info(
                "chat.knowledge.no_sources",
                extra={
                    **log_extra,
                    "branch": "no_knowledge_hit",
                    "reason": "no_passages_above_threshold",
                    "pipeline_elapsed_ms": round((time.perf_counter() - pipeline_t0) * 1000, 1),
                },
            )
            return self._messages_no_knowledge_hit(latest_user)

        logger.info(
            "chat.knowledge.merge",
            extra={
                **log_extra,
                "branch": "merged_knowledge",
                "classifier_draft": False,
                "indexed_chunks": len(passage_payloads),
                "responder_model": settings.CHAT_RESPONDER_MODEL,
                "pipeline_elapsed_ms": round((time.perf_counter() - pipeline_t0) * 1000, 1),
            },
        )
        return self._messages_merged_knowledge(None, passage_payloads, latest_user)

    async def create_session(
        self,
        user_id: str,
        payload: ChatSessionCreate,
    ) -> ChatSessionResponse:
        session = ChatSession(user_id=PydanticObjectId(user_id), title=payload.title)
        created_session = await self.repository.create_session(session)
        return ChatSessionResponse.from_document(created_session)

    async def list_sessions(self, user_id: str) -> list[ChatSessionResponse]:
        sessions = await self.repository.list_sessions_by_user(user_id)
        return [ChatSessionResponse.from_document(session) for session in sessions]

    async def get_session(self, user_id: str, session_id: str) -> ChatSessionDetailResponse:
        session = await self.repository.get_session_for_user(session_id, user_id)
        if session is None:
            raise NotFoundException("Chat session not found")

        messages = await self.repository.list_messages_for_session(session_id)
        session_response = ChatSessionResponse.from_document(session)
        return ChatSessionDetailResponse(
            **session_response.model_dump(),
            messages=[ChatMessageResponse.from_document(message) for message in messages],
        )

    async def delete_session(self, user_id: str, session_id: str) -> None:
        if not await self.repository.soft_delete_session_for_user(session_id, user_id):
            raise NotFoundException("Chat session not found")

    async def create_message(
        self,
        user_id: str,
        session_id: str,
        payload: ChatMessageCreate,
    ) -> tuple[list[ChatMessageResponse], str | None]:
        session = await self.require_chat_session(user_id, session_id)

        user_message = ChatMessage(
            session_id=PydanticObjectId(session_id),
            user_id=PydanticObjectId(user_id),
            role=MessageRole.USER,
            content=payload.content,
            source=MessageSource.TEXT,
        )
        created_user_message = await self.repository.create_message(user_message)

        history = await self.repository.list_messages_for_session(
            session_id,
            limit=settings.CHAT_HISTORY_FETCH_LIMIT,
        )
        latest = payload.content.strip()
        responder_messages = await self._build_responder_messages(
            user_id,
            history,
            latest,
            session_id=session_id,
        )
        respond_t0 = time.perf_counter()
        assistant_text = await self.llm.complete(
            responder_messages,
            model=settings.CHAT_RESPONDER_MODEL,
            temperature=settings.CHAT_RESPONDER_TEMPERATURE,
        )
        logger.info(
            "chat.responder.complete",
            extra={
                "session_id": session_id,
                "user_id": user_id,
                "mode": "sync",
                "responder_model": settings.CHAT_RESPONDER_MODEL,
                "assistant_chars": len(assistant_text),
                "responder_elapsed_ms": round((time.perf_counter() - respond_t0) * 1000, 1),
            },
        )

        assistant_message = ChatMessage(
            session_id=PydanticObjectId(session_id),
            user_id=PydanticObjectId(user_id),
            role=MessageRole.ASSISTANT,
            content=assistant_text,
            source=MessageSource.TEXT,
        )
        created_assistant_message = await self.repository.create_message(assistant_message)
        new_title = await self._finalize_session_after_assistant(
            session,
            session_id,
            latest,
            assistant_text,
        )

        return [
            ChatMessageResponse.from_document(created_user_message),
            ChatMessageResponse.from_document(created_assistant_message),
        ], new_title

    async def stream_message_events(
        self,
        session: ChatSession,
        user_id: str,
        payload: ChatMessageCreate,
    ) -> AsyncIterator[str]:
        """SSE: save user message, load history, classify/RAG prep, stream responder only.

        Step 1: Load last CHAT_HISTORY_FETCH_LIMIT messages; transcript uses last
        CHAT_ROUTER_MAX_MESSAGES of those.
        Step 2–3: _build_responder_messages → classify_route (website + transcript), branch, optional Qdrant.
        Step 4: stream_complete(..., CHAT_RESPONDER_MODEL) only.
        """
        session_id = str(session.id)

        user_message = ChatMessage(
            session_id=PydanticObjectId(session_id),
            user_id=PydanticObjectId(user_id),
            role=MessageRole.USER,
            content=payload.content,
            source=MessageSource.TEXT,
        )
        await self.repository.create_message(user_message)

        yield _sse_event(
            {
                "event": "loading",
                "message": "Thinking...",
            }
        )

        # Step 1 — Last CHAT_HISTORY_FETCH_LIMIT messages (includes the message just saved).
        # Router transcript uses up to CHAT_ROUTER_MAX_MESSAGES from this window.
        history = await self.repository.list_messages_for_session(
            session_id,
            limit=settings.CHAT_HISTORY_FETCH_LIMIT,
        )
        latest = payload.content.strip()

        logger.info(
            "chat.sse.request",
            extra={
                "session_id": session_id,
                "user_id": user_id,
                "latest_preview": _preview(latest),
            },
        )

        # Steps 2–3–4 (prep): classify, branch, optionally retrieve chunks → responder_messages.
        responder_messages = await self._build_responder_messages(
            user_id,
            history,
            latest,
            session_id=session_id,
        )

        # Step 4 — Stream the responder only (router/RAG work is already done above).
        stream_t0 = time.perf_counter()
        parts: list[str] = []
        try:
            async for delta in self.llm.stream_complete(
                responder_messages,
                model=settings.CHAT_RESPONDER_MODEL,
                temperature=settings.CHAT_RESPONDER_TEMPERATURE,
            ):
                parts.append(delta)
                yield _sse_event({"event": "delta", "text": delta})
        except AppException as exc:
            yield _sse_event({"event": "error", "detail": exc.message})
            return
        except Exception:
            logger.exception("Streaming chat LLM failure", extra={"session_id": session_id})
            yield _sse_event({"event": "error", "detail": "Unexpected language model failure"})
            return

        full_text = "".join(parts).strip()
        if not full_text:
            yield _sse_event(
                {"event": "error", "detail": "Language model returned an empty response"},
            )
            return

        logger.info(
            "chat.responder.stream_complete",
            extra={
                "session_id": session_id,
                "user_id": user_id,
                "mode": "sse",
                "responder_model": settings.CHAT_RESPONDER_MODEL,
                "assistant_chars": len(full_text),
                "stream_elapsed_ms": round((time.perf_counter() - stream_t0) * 1000, 1),
            },
        )

        assistant_message = ChatMessage(
            session_id=PydanticObjectId(session_id),
            user_id=PydanticObjectId(user_id),
            role=MessageRole.ASSISTANT,
            content=full_text,
            source=MessageSource.TEXT,
        )
        await self.repository.create_message(assistant_message)
        new_title = await self._finalize_session_after_assistant(
            session,
            session_id,
            latest,
            full_text,
        )

        done_payload: dict[str, object] = {
            "event": "done",
            "message": full_text,
        }
        if new_title:
            done_payload["session_title"] = new_title
        yield _sse_event(done_payload)
