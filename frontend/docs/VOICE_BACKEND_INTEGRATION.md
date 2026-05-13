# Voice chat: backend checklist for a complete integration

The browser client now **streams** user and assistant transcripts into the main chat UI (OpenAI Realtime events) and **persists** each completed turn via your existing **`POST /api/v1/chat/sessions/{session_id}/voice/commit`** endpoint. This document describes what the FastAPI/Mongo side should guarantee and what optional enhancements would make a “full” product.

## Already assumed by the frontend (verify on the server)

1. **`POST .../realtime/session`**  
   Mints an OpenAI Realtime session and returns `client_secret`, `model`, etc. The SPA uses the returned `model` when opening the WebRTC SDP URL (`/v1/realtime?model=...`).

2. **`POST .../realtime/tools/lookup_documentation`**  
   Returns `{ "result": "..." }`; the client forwards that string into the Realtime tool output channel.

3. **`POST .../voice/commit`**  
   - **Body:** `{ "user_transcript": string, "assistant_transcript": string }` within the documented length limits.  
   - **Auth:** Same JWT as other chat routes; session must belong to the user.  
   - **Response:** JSON array of **two** persisted messages (user, then assistant), same conceptual shape as text chat (`ChatMessageResponse[]`).  
   - **Optional header:** `X-Chat-Session-Title` on the first auto-title, mirroring text `POST .../messages`.

4. **`GET .../chat/sessions/{id}`**  
   After a successful commit, reloading the session (or opening it on another device) must return the new messages in order so history matches what the user saw during voice.

## Required for correctness (if not already done)

- **Atomic write:** Persist both user and assistant messages in one transaction (or equivalent) so a half-written turn cannot appear after a partial failure.
- **Idempotency / duplicate turns:** The client may retry a commit on network failure. Consider a **client-generated turn id** in the commit body (future API change) or dedupe by `(session_id, user_hash, assistant_hash, created_at window)` to avoid duplicate rows.
- **Validation:** Enforce max lengths server-side (same as `VoiceCommitRequest`) and return **4xx** with a clear JSON error so the UI can surface it.
- **Role and content:** Store assistant as `assistant` / user as `user` in Mongo so `GET` mapping stays consistent with text chat.

## Optional “full implementation” enhancements

| Enhancement | Why |
|-------------|-----|
| **Message metadata** | Add `source: "voice" \| "text"` (and optionally `openai_response_id`) on stored messages for analytics, moderation, and debugging. |
| **Incremental server persistence** | If you need **other tabs or admins** to see partial transcripts without waiting for `voice/commit`, add something like `POST .../voice/transcript_chunk` (append-only) or a small **WebSocket/SSE** fan-out from the server. The current SPA does **not** require this for the primary user. |
| **Streaming assistant from your model** | Voice assistant text still comes from OpenAI Realtime. If the product must show **only** post-processed text (PII strip, citations), add a backend pipeline that rewrites or replaces content before commit, or commit a **canonical** assistant string different from the live Realtime transcript (document the policy). |
| **Rate limits & quotas** | Per-user limits on `voice/commit` and mint to control cost and abuse. |
| **OpenAI GA migration** | When you move from `POST /v1/realtime/sessions` to GA **`client_secrets`**, document the new mint response and set **`VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE=calls`** in the SPA if the browser must use **`POST /v1/realtime/calls`**. |

## Contract summary for `voice/commit`

The frontend, after each assistant audio transcript `done` event (with a non-empty user transcript), calls **`voice/commit`** and then **replaces** trailing rows marked as optimistic voice bubbles with the **exact** `[user, assistant]` array returned by the API. If the array length or order ever differs, update the client’s `mergeVoiceCommitMessages` logic and this contract in lockstep.
