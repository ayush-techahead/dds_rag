"""Mint OpenAI Realtime client secrets for browser voice chat."""

from __future__ import annotations

import asyncio
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

_EXPECTED_REALTIME_TURN_DETECTION: dict[str, Any] = {
    "type": "server_vad",
    "interrupt_response": False,
    "threshold": 0.78,
    "prefix_padding_ms": 350,
    "silence_duration_ms": 650,
}

LOOKUP_DOCUMENTATION_TOOL: dict[str, Any] = {
    "type": "function",
    "name": LOOKUP_DOCUMENTATION_TOOL_NAME,
    "description": (
        "Search indexed DDS documentation and website-derived chunks. "
        "Call before answering specific procedural, eligibility, or program-detail questions. "
        "Use a concise English search query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for documentation retrieval.",
            }
        },
        "required": ["query"],
    },
}


def _voice_instructions(chat_session_id: str) -> str:
    return (
        "You are a helpful voice assistant for California DDS documentation and indexed "
        f"product knowledge. This chat session id is {chat_session_id} (for logging only).\n\n"
        "WEBSITE OVERVIEW — use for high-level DDS facts (mission, contacts, structure):\n"
        f"{WEBSITE_DESCRIPTION.strip()}\n\n"
        "For procedures, eligibility, programs, or case-specific detail: call the "
        f"{LOOKUP_DOCUMENTATION_TOOL_NAME} tool with a short search query before answering. "
        "If the tool returns text starting with NO_SOURCES, say you cannot find it in the "
        "indexed documentation and suggest rephrasing.\n\n"
        "Rules:\n"
        "- Do not invent facts beyond the overview and tool results.\n"
        "- If tool excerpts conflict with the overview on specifics, trust the excerpts.\n"
        "- Never mention markdown files, uploaded filenames, archive filenames, or document IDs "
        "in the answer.\n"
        "- Use only actual DDS website URLs (dds.ca.gov) for reference links and citations; "
        "never cite a markdown file or uploaded document as a source.\n"
        "- Use the same answer structure as text chat:\n"
        "  (direct response)\n"
        "  ## Where to learn more\n"
        "  (bullet list: include only DDS website URLs from tool passages you used; include "
        "passage numbers [n] when the link came from a tool passage. If only the overview "
        "applied, link or name https://www.dds.ca.gov from the overview text only. If no DDS "
        "website link is available, do not list file-based citations; instead ask whether the "
        "user would like links to DDS resources or any other information.)\n"
        "- Keep reference links, URLs, source titles, and passage citations out of the direct "
        "response; put them only in the final ## Where to learn more section.\n"
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
    return max(1, min(n, 4096))


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
    )


def build_session_payload(chat_session_id: str) -> dict[str, Any]:
    """Build the JSON body sent to ``POST /v1/realtime/client_secrets``.

    The GA Realtime session schema nests audio and VAD options below ``session``.
    ``output_modalities=["audio"]`` still emits an audio transcript on the data channel.
    """
    return {
        "session": {
            "type": "realtime",
            "model": settings.OPENAI_REALTIME_MODEL,
            "output_modalities": ["audio"],
            "instructions": _voice_instructions(chat_session_id),
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "whisper-1"},
                    "turn_detection": dict(_EXPECTED_REALTIME_TURN_DETECTION),
                },
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
        },
    }


async def mint_openai_realtime_session(*, chat_session_id: str) -> RealtimeSessionMintResponse:
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
            "vad_interrupt_response": turn_detection["interrupt_response"],
            "vad_threshold": turn_detection["threshold"],
            "vad_prefix_padding_ms": turn_detection["prefix_padding_ms"],
            "vad_silence_ms": turn_detection["silence_duration_ms"],
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
            if returned_turn_detection != _EXPECTED_REALTIME_TURN_DETECTION:
                logger.warning(
                    "chat.realtime.session.minted_vad_mismatch",
                    extra={
                        "chat_session_id": chat_session_id,
                        "openai_session_id": parsed.openai_session_id,
                        "requested_turn_detection": _EXPECTED_REALTIME_TURN_DETECTION,
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
                    "requested_turn_detection": _EXPECTED_REALTIME_TURN_DETECTION,
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
                "requested_turn_detection": _EXPECTED_REALTIME_TURN_DETECTION,
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
