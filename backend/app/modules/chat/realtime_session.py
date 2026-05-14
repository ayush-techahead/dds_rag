"""Mint OpenAI Realtime client secrets for browser voice chat."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import BadRequestException, ServiceUnavailableException
from app.core.logging import get_logger
from app.modules.chat.schemas import RealtimeClientSecret, RealtimeSessionMintResponse
from app.modules.chat.website_context import WEBSITE_DESCRIPTION

logger = get_logger(__name__)

_TRANSIENT_STATUS_CODES = frozenset({500, 502, 503, 504})

LOOKUP_DOCUMENTATION_TOOL_NAME = "lookup_documentation"

LOOKUP_DOCUMENTATION_TOOL: dict[str, Any] = {
    "type": "function",
    "name": LOOKUP_DOCUMENTATION_TOOL_NAME,
    "description": (
        "Retrieve relevant DDS context from indexed content. "
        "Call before answering specific procedural, eligibility, or program-detail questions. "
        "Use a concise English search query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Concise DDS lookup query.",
            }
        },
        "required": ["query"],
    },
}

def _voice_instructions(chat_session_id: str) -> str:
    return (
        "You are a candid, supportive voice assistant for California DDS information. "
        "Answer directly in natural spoken language and help the user take the next "
        "practical step. "
        f"This chat session id is {chat_session_id} (for logging only).\n\n"
        "WEBSITE OVERVIEW — use for high-level DDS facts (mission, contacts, structure):\n"
        f"{WEBSITE_DESCRIPTION.strip()}\n\n"
        "For procedures, eligibility, programs, or case-specific detail: call the "
        f"{LOOKUP_DOCUMENTATION_TOOL_NAME} tool with a short search query before answering. "
        "Do not narrate the tool call, mention source material, or say that you are "
        "checking anything. If the tool returns text starting with NO_RELEVANT_INFO, "
        "say plainly that the information is not available for that specific question "
        "and ask for one clarifying detail only if it would help.\n\n"
        "Tool policy:\n"
        "- For DDS factual, procedural, eligibility, services, intake, regional center, "
        "program, or contact questions, call lookup_documentation before answering unless "
        "the answer is fully covered by the website overview.\n"
        "- Do not call lookup_documentation repeatedly with the same query after a "
        "NO_RELEVANT_INFO result or tool failure; answer that the information is not "
        "available and ask the user to rephrase or narrow the request only if useful.\n"
        "- After lookup_documentation returns passages, ground the answer only in those "
        "passages plus the overview.\n\n"
        "Rules:\n"
        "- Start with the answer, not a process update. Do not say you are checking, "
        "reviewing, searching, or looking anything up. Do not include process or "
        "citation-style phrases in the answer.\n"
        "- Be empathetic and candid. Keep wording concrete and useful; avoid filler.\n"
        "- Do not invent facts beyond the overview and tool results.\n"
        "- If tool excerpts conflict with the overview on specifics, trust the excerpts.\n"
        "- Never mention markdown files, uploaded filenames, archive filenames, or document IDs "
        "in the answer.\n"
        "- Use only actual DDS website URLs (dds.ca.gov) for links; never cite a markdown "
        "file or uploaded document as a source.\n"
        "- Do not use Markdown section headings in voice. If a link is truly helpful, "
        "mention at most one or two DDS page names and URLs near the end. If no DDS link "
        "is available, skip links instead of discussing missing sources.\n"
        "- Ask one focused follow-up question only when it would help the user move "
        "forward; otherwise end cleanly after the answer.\n"
        "- Keep answers focused; long unbroken monologues can be truncated by the audio "
        "pipeline. If the topic is broad, summarize first and offer to expand on parts.\n"
    )


def _format_api_error(body: dict[str, Any], status_code: int) -> str:
    err = body.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return f"OpenAI Realtime API error ({status_code}): {err['message']}"
    return f"OpenAI Realtime API error ({status_code})"


def _coerce_max_output_tokens(raw: str) -> str | int:
    """Realtime accepts ``"inf"`` or an int 1–4096; pass strings through, coerce numerics."""
    value = (raw or "").strip()
    if not value or value.lower() == "inf":
        return "inf"
    try:
        n = int(value)
    except ValueError:
        return "inf"
    return max(100, min(n, 4096))


def _realtime_turn_detection() -> dict[str, Any]:
    vad_type = (settings.OPENAI_REALTIME_VAD_TYPE or "semantic_vad").strip() or "semantic_vad"
    turn_detection: dict[str, Any] = {
        "type": vad_type,
        "interrupt_response": bool(settings.OPENAI_REALTIME_VAD_INTERRUPT_RESPONSE),
        "create_response": bool(settings.OPENAI_REALTIME_VAD_CREATE_RESPONSE),
    }
    if vad_type == "server_vad":
        turn_detection.update(
            {
                "threshold": settings.OPENAI_REALTIME_VAD_THRESHOLD,
                "prefix_padding_ms": settings.OPENAI_REALTIME_VAD_PREFIX_PADDING_MS,
                "silence_duration_ms": settings.OPENAI_REALTIME_VAD_SILENCE_MS,
            },
        )
    return turn_detection


def _realtime_noise_reduction() -> dict[str, str] | None:
    value = (settings.OPENAI_REALTIME_NOISE_REDUCTION or "").strip().lower()
    if not value or value in {"none", "off", "disabled", "false"}:
        return None
    return {"type": value}


def _realtime_reasoning() -> dict[str, str] | None:
    effort = (settings.OPENAI_REALTIME_REASONING_EFFORT or "").strip().lower()
    if not effort or effort in {"none", "off", "disabled", "false"}:
        return None
    return {"effort": effort}


def _safety_identifier_for_user(user_id: str | None) -> str | None:
    if not user_id:
        return None
    secret = settings.JWT_SECRET_KEY.strip().encode("utf-8")
    return hmac.new(secret, user_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _realtime_session_config_for_log(session: dict[str, Any]) -> dict[str, Any]:
    """Log enough returned config to detect VAD normalization without dumping prompts."""
    audio = session.get("audio")
    audio_input = audio.get("input") if isinstance(audio, dict) else None
    return {
        "id": session.get("id"),
        "model": session.get("model"),
        "output_modalities": session.get("output_modalities"),
        "max_output_tokens": session.get("max_output_tokens"),
        "audio": {
            "input": {
                "turn_detection": (
                    audio_input.get("turn_detection")
                    if isinstance(audio_input, dict)
                    else None
                ),
            },
            "output": audio.get("output") if isinstance(audio, dict) else None,
        },
        "tool_choice": session.get("tool_choice"),
        "reasoning": session.get("reasoning"),
    }


def _returned_turn_detection(body: dict[str, Any]) -> dict[str, Any] | None:
    session = body.get("session")
    if not isinstance(session, dict):
        return None
    audio = session.get("audio")
    if not isinstance(audio, dict):
        return None
    audio_input = audio.get("input")
    if not isinstance(audio_input, dict):
        return None
    turn_detection = audio_input.get("turn_detection")
    return turn_detection if isinstance(turn_detection, dict) else None


def parse_mint_response(
    body: dict[str, Any],
    *,
    chat_session_id: str,
) -> RealtimeSessionMintResponse:
    session = body.get("session")
    if not isinstance(session, dict):
        raise ServiceUnavailableException(
            "Invalid Realtime client secret response (missing session)",
        )
    sid = session.get("id")
    if not isinstance(sid, str):
        raise ServiceUnavailableException(
            "Invalid Realtime client secret response (missing session id)",
        )
    model = session.get("model")
    use_model = model if isinstance(model, str) else settings.OPENAI_REALTIME_MODEL
    val = body.get("value")
    exp = body.get("expires_at")
    if not isinstance(val, str) or not isinstance(exp, int | float):
        raise ServiceUnavailableException("Invalid Realtime client secret shape")
    return RealtimeSessionMintResponse(
        chat_session_id=chat_session_id,
        openai_session_id=sid,
        client_secret=RealtimeClientSecret(value=val, expires_at=int(exp)),
        model=use_model,
        voice_instructions=_voice_instructions(chat_session_id),
    )


def build_session_payload(chat_session_id: str) -> dict[str, Any]:
    """Build the JSON body sent to ``POST /v1/realtime/client_secrets``.

    The GA Realtime session schema nests audio and VAD options below ``session``.
    ``output_modalities=["audio"]`` still emits an audio transcript on the data channel.
    """
    audio_input: dict[str, Any] = {
        "format": {"type": "audio/pcm", "rate": 24000},
        "transcription": {"model": settings.OPENAI_REALTIME_TRANSCRIPTION_MODEL},
        "turn_detection": _realtime_turn_detection(),
    }
    noise_reduction = _realtime_noise_reduction()
    if noise_reduction is not None:
        audio_input["noise_reduction"] = noise_reduction

    session: dict[str, Any] = {
        "type": "realtime",
        "model": settings.OPENAI_REALTIME_MODEL,
        "output_modalities": ["audio"],
        "instructions": _voice_instructions(chat_session_id),
        "audio": {
            "input": audio_input,
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": settings.OPENAI_REALTIME_VOICE,
            },
        },
        "tools": [LOOKUP_DOCUMENTATION_TOOL],
        "tool_choice": "auto",
        "max_output_tokens": _coerce_max_output_tokens(
            settings.OPENAI_REALTIME_MAX_OUTPUT_TOKENS,
        ),
    }
    reasoning = _realtime_reasoning()
    if reasoning is not None:
        session["reasoning"] = reasoning

    return {
        "session": session,
    }


async def mint_openai_realtime_session(
    *,
    chat_session_id: str,
    user_id: str | None = None,
) -> RealtimeSessionMintResponse:
    """Create an ephemeral Realtime client secret via ``POST /v1/realtime/client_secrets``.

    Transient OpenAI errors (502/503/504) and network failures are retried with
    exponential backoff up to ``OPENAI_REALTIME_MINT_MAX_RETRIES`` additional
    attempts. Authentication errors and other 4xx responses fail fast — retrying
    them would only waste latency.
    """
    api_key = settings.OPENAI_API_KEY.strip()
    if not api_key:
        raise ServiceUnavailableException(
            "Realtime is not configured: set OPENAI_API_KEY.",
        )
    base = settings.OPENAI_REALTIME_API_BASE.rstrip("/")
    url = f"{base}/realtime/client_secrets"
    payload = build_session_payload(chat_session_id)
    session = payload["session"]
    audio = session["audio"]
    turn_detection = audio["input"]["turn_detection"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    safety_identifier = _safety_identifier_for_user(user_id)
    if safety_identifier is not None:
        headers["OpenAI-Safety-Identifier"] = safety_identifier
    timeout = httpx.Timeout(settings.OPENAI_REALTIME_REQUEST_TIMEOUT_SECONDS)
    logger.info(
        "chat.realtime.session.mint_request",
        extra={
            "chat_session_id": chat_session_id,
            "model": session["model"],
            "voice": audio["output"]["voice"],
            "output_modalities": session["output_modalities"],
            "max_output_tokens": session["max_output_tokens"],
            "vad_type": turn_detection["type"],
            "vad_interrupt_response": turn_detection.get("interrupt_response"),
            "vad_create_response": turn_detection.get("create_response"),
            "vad_threshold": turn_detection.get("threshold"),
            "vad_prefix_padding_ms": turn_detection.get("prefix_padding_ms"),
            "vad_silence_ms": turn_detection.get("silence_duration_ms"),
            "reasoning_effort": session.get("reasoning", {}).get("effort"),
            "transcription_model": audio["input"].get("transcription", {}).get("model"),
            "noise_reduction": audio["input"].get("noise_reduction"),
            "has_safety_identifier": safety_identifier is not None,
        },
    )

    max_retries = max(0, int(settings.OPENAI_REALTIME_MINT_MAX_RETRIES))
    backoff_base = max(0.0, float(settings.OPENAI_REALTIME_MINT_BACKOFF_BASE_SECONDS))
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            last_error = ServiceUnavailableException(
                "Could not reach the OpenAI Realtime API",
            )
            logger.warning(
                "chat.realtime.session.mint_network_error",
                extra={
                    "chat_session_id": chat_session_id,
                    "attempt": attempt + 1,
                    "of_attempts": max_retries + 1,
                    "error": str(exc),
                },
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff_base * (2**attempt))
                continue
            raise last_error from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ServiceUnavailableException("Invalid JSON from OpenAI Realtime API") from exc
        if not isinstance(body, dict):
            raise ServiceUnavailableException("Invalid response from OpenAI Realtime API")

        if response.is_success:
            parsed = parse_mint_response(body, chat_session_id=chat_session_id)
            returned_turn_detection = _returned_turn_detection(body)
            expected_turn_detection = _realtime_turn_detection()
            if returned_turn_detection != expected_turn_detection:
                logger.warning(
                    "chat.realtime.session.minted_vad_mismatch",
                    extra={
                        "chat_session_id": chat_session_id,
                        "openai_session_id": parsed.openai_session_id,
                        "requested_turn_detection": expected_turn_detection,
                        "returned_turn_detection": returned_turn_detection,
                        "returned_session_config": _realtime_session_config_for_log(
                            body.get("session", {}),
                        ),
                    },
                )
            logger.info(
                "chat.realtime.session.minted",
                extra={
                    "chat_session_id": chat_session_id,
                    "openai_session_id": parsed.openai_session_id,
                    "model": parsed.model,
                    "client_secret_expires_at": parsed.client_secret.expires_at,
                    "attempts_used": attempt + 1,
                    "requested_turn_detection": expected_turn_detection,
                    "returned_turn_detection": returned_turn_detection,
                    "returned_interrupt_response": (
                        returned_turn_detection.get("interrupt_response")
                        if returned_turn_detection is not None
                        else None
                    ),
                },
            )
            return parsed

        message = _format_api_error(body, response.status_code)
        is_transient = response.status_code in _TRANSIENT_STATUS_CODES
        logger.warning(
            "chat.realtime.session.mint_failed",
            extra={
                "chat_session_id": chat_session_id,
                "status_code": response.status_code,
                "error_message": message,
                "attempt": attempt + 1,
                "of_attempts": max_retries + 1,
                "transient": is_transient,
                "requested_turn_detection": _realtime_turn_detection(),
                "returned_session_config": (
                    _realtime_session_config_for_log(body["session"])
                    if isinstance(body.get("session"), dict)
                    else None
                ),
            },
        )

        if is_transient and attempt < max_retries:
            last_error = ServiceUnavailableException(message)
            await asyncio.sleep(backoff_base * (2**attempt))
            continue

        if is_transient:
            raise ServiceUnavailableException(message)
        raise BadRequestException(message)

    # Defensive fallback: control should always exit via return/raise above.
    raise last_error or ServiceUnavailableException("Realtime session mint failed")
