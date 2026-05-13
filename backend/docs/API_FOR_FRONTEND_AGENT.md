# API contract for front-end / codegen agents

Machine-readable schema: **`openapi.json`** at the repository root (OpenAPI 3.1). Regenerate after route or model changes:

```bash
source .venv/bin/activate
python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2))" > openapi.json
```

Interactive docs when the server runs: `http://localhost:8000/docs` and `http://localhost:8000/openapi.json`.

## Base URL and version prefix

- All application routes are under **`/api/v1`** (configurable via `API_V1_PREFIX`; default is `/api/v1`).
- Example: `GET https://<host>/api/v1/health`

## Authentication

- **Bearer JWT** for protected routes. Header: `Authorization: Bearer <access_token>`.
- Obtain a token from **`POST /api/v1/auth/login`**.
- OpenAPI security scheme name: **`OAuth2PasswordBearer`** (token URL in schema points at `/api/v1/auth/login` for OAuth2-password-style clients).

### Login request shapes

`POST /api/v1/auth/login` inspects **`Content-Type`**:

1. **`application/json`** — body object with `password` and **`email`** or **`username`** (same value as email). Example: `{"email":"a@b.com","password":"..."}`.
2. **`application/x-www-form-urlencoded`** — fields: **`password`** plus **`username`** (OAuth2 convention, value is the email) or **`email`**. Example: `username=a@b.com&password=...` (as used in integration tests).

Wrong or missing fields → **400** with `{"detail": "<message>"}` (not always 422, because this handler parses manually).

### Register

`POST /api/v1/auth/register` — JSON only: `UserCreate` (`email`, `password` min 8 chars, optional `full_name`). Response **201** `UserResponse`.

## Error responses (global handlers)

| Status | Body shape | When |
|--------|------------|------|
| 400 | `{"detail": "..."}` | Bad request / domain errors (`BadRequestException`) |
| 401 | `{"detail": "..."}` | Missing/invalid JWT, inactive user |
| 403 | `{"detail": "..."}` | Forbidden |
| 404 | `{"detail": "..."}` | Not found |
| 422 | `{"detail": "Validation error", "errors": [...]}` | Pydantic / FastAPI validation (`RequestValidationError`) |
| 500 | `{"detail": "Internal server error"}` | Unhandled errors |
| 503 | `{"detail": "..."}` | Service unavailable (e.g. LLM/embeddings) |

Some endpoints return **422** with FastAPI’s `HTTPValidationError` shape where a route still uses default validation.

## Multipart uploads (documents)

Use **`multipart/form-data`** with part name **`file`** (FastAPI `File(...)` default):

- **`POST /api/v1/documents/upload`** — single file. Allowed: **`.pdf`** (content-type `application/pdf` or `application/octet-stream`), **`.md` / `.markdown`** (`text/markdown`, `text/plain`, or `application/octet-stream`).
- **`POST /api/v1/documents/zip-sessions`** — ZIP archive (phase 1 manifest; see OpenAPI `Body_create_zip_markdown_session_...`).

Max size is controlled by **`MAX_UPLOAD_SIZE_MB`** (see `.env.example`).

## ZIP ingest JSON body

**`POST /api/v1/documents/zip-sessions/{session_id}/ingest`**

- **`Content-Type: application/json`**
- Body may be **omitted** or **`{}`** — server applies defaults (`ZipIngestBatchRequest` empty instance).
- Otherwise use **`ZipIngestBatchRequest`**: either `markdown_skip` + `markdown_path_limit`, **or** non-empty `path_indices`, not both (see schema `exclusive_slice_or_indices` in code).

## Chat streaming (SSE)

**`POST /api/v1/chat/sessions/{session_id}/messages/stream`**

- Same JSON body as non-streaming messages: `{"content":"..."}` (see `ChatMessageCreate`).
- Response **`text/event-stream`**. Parse blocks separated by blank lines; each line starting with `data: ` is JSON.
- Event types (see `app/modules/chat/service.py`): `loading`, `delta` (accumulate `text`), `done` (`message` is full assistant reply), `error` (`detail`).

## Voice chat (OpenAI Realtime)

> **For the full SPA implementation spec** (lifecycle, WebRTC setup, event handlers, TypeScript types, error handling, UX checklist, and a working single-file React reference) see [`FRONTEND_VOICE_INTEGRATION.md`](./FRONTEND_VOICE_INTEGRATION.md). The section below summarises the HTTP contract; everything WebRTC- and event-handler-related lives in the companion doc.

Duplex voice (mic in, spoken answer out, server-side VAD) uses **OpenAI’s Realtime API** from the browser. This backend never proxies audio; it mints **short-lived credentials**, runs **documentation search** when the model calls a tool, and can **persist transcripts** to the same Mongo chat session as text.

### Environment

- **`OPENAI_API_KEY`** — required for minting Realtime sessions (server-side only).
- **`OPENAI_REALTIME_API_BASE`** — default `https://api.openai.com/v1`. Realtime client-secret creation uses `POST {OPENAI_REALTIME_API_BASE}/realtime/client_secrets`. This is separate from **`OPENAI_BASE_URL`**, which is used for chat completions and embeddings; many OpenAI-compatible hosts do not implement Realtime.
- **`OPENAI_REALTIME_MODEL`**, **`OPENAI_REALTIME_VOICE`**, **`OPENAI_REALTIME_REQUEST_TIMEOUT_SECONDS`** — see `.env.example`.

### Mint ephemeral session

**`POST /api/v1/chat/sessions/{session_id}/realtime/session`**

- **Auth:** Bearer JWT; session must belong to the user.
- **Response:** `RealtimeSessionMintResponse` — `client_secret.value` (ephemeral token), `client_secret.expires_at`, `openai_session_id`, `model`, `chat_session_id`.

Use `client_secret.value` to authenticate the browser’s WebRTC or WebSocket connection to OpenAI (see [OpenAI Realtime guide](https://platform.openai.com/docs/guides/realtime)). The client secret is bound to a GA Realtime session with **audio** output, **server VAD**, input transcription (**whisper-1**), and one function tool named **`lookup_documentation`**.

### RAG tool (HTTP bridge)

When the Realtime model invokes **`lookup_documentation`**, the client should call:

**`POST /api/v1/chat/sessions/{session_id}/realtime/tools/lookup_documentation`**

- **Body:** `{"query":"<concise search string>"}` (`LookupDocumentationRequest`).
- **Response:** `{"result":"<plain text>"}` (`LookupDocumentationResponse`). Pass `result` back to OpenAI as the tool output (same user JWT). The text is either numbered source cards (URLs, excerpts) or a line starting with **`NO_SOURCES:`** when nothing matched Qdrant.

### Persist voice turn to Mongo (optional)

**`POST /api/v1/chat/sessions/{session_id}/voice/commit`**

- **Body:** `VoiceCommitRequest`
  - `user_transcript` (required, 1–8000 chars)
  - `assistant_transcript` (required, 1–16000 chars)
  - `client_turn_id` (optional, ≤128 chars) — stable id per voice turn; retries with the same id return the originally stored pair instead of duplicating rows.
  - `openai_response_id` (optional, ≤128 chars) — stored as a debug breadcrumb only.
- **Response:** exactly two `ChatMessageResponse` objects in order `[user, assistant]`, each with `source = "voice"`. The frontend's `mergeVoiceCommitMessages` should replace its trailing optimistic voice bubbles with this array verbatim.
- **Header:** the voice endpoint does **not** set `X-Chat-Session-Title` on the first turn. Title generation runs as a background task so the commit response returns immediately (the SPA can render the assistant bubble without a 1-3 s LLM-blocked delay). The title becomes visible on the next `GET /chat/sessions` or `GET /chat/sessions/{id}` call. The text endpoint still returns the header synchronously.
- **Atomicity:** the two inserts are committed together; if the second fails the first is rolled back, so a half-written turn never surfaces from `GET .../sessions/{id}`.
- **Errors:** `404` when the session is not owned by the caller; `422` from the validator when transcripts exceed the length limits.

`ChatMessageResponse.source` is `"text"` for typed messages and `"voice"` for committed voice turns, on every chat history endpoint.

### Client checklist

1. Log in → JWT.
2. Create or select a chat session → `session_id`.
3. `POST .../realtime/session` → read `client_secret.value` and `model`.
4. Open Realtime per OpenAI docs (WebRTC preferred; WebSocket supported).
5. **Subscribe to data-channel events** (see next subsection). Without these, the SPA hears audio but never sees text or saves it.
6. On **`function_call`** / tool invocation for **`lookup_documentation`**, `POST` the tool route with `{ "query": ... }`, then submit the returned `result` to the Realtime session per OpenAI's tool-output event sequence.
7. After the turn completes, `POST .../voice/commit` with final transcripts so history matches the UI.

### Required Realtime data-channel events

WebRTC carries audio over the media track, but the assistant's **text transcript** and **tool calls** are JSON events on the data channel. The SPA **must** handle these — missing one is the typical cause of "I hear the answer but it doesn't show up in chat and nothing is saved":

| OpenAI Realtime event | What the SPA must do |
|---|---|
| `conversation.item.input_audio_transcription.completed` | Render the user bubble (this seems to already work). |
| `response.audio_transcript.delta` | Append `.delta` chunks to a streaming assistant bubble in the UI. |
| `response.audio_transcript.done` | Use the final `.transcript` as the canonical assistant text. **Then call `POST .../voice/commit`** with `{user_transcript, assistant_transcript, client_turn_id, openai_response_id?}` and replace the streaming bubble with the returned `[user, assistant]` array. |
| `response.function_call_arguments.delta` / `response.function_call_arguments.done` | Buffer the arguments. On `done`, parse `arguments.query`, `POST` to the `lookup_documentation` tool route, then send `conversation.item.create` (`function_call_output` with the `call_id` and the `result` string) followed by `response.create` so the model continues speaking. **If this loop is broken, the assistant will go silent or cut off mid-sentence after the tool call.** |
| `error` | Log and surface — do not silently kill the peer connection. |

`response.audio.delta` events also exist and contain raw audio — you do **not** need them when using WebRTC (audio comes via the media track). When using WebSocket transport you must decode them yourself.

### Why audio sometimes cuts off mid-response

- **Server-side token cap.** Client secrets minted by this backend now set the session `max_output_tokens` from `OPENAI_REALTIME_MAX_OUTPUT_TOKENS` (default `"inf"`); without it OpenAI silently caps Realtime responses and the audio ends abruptly. Bump or tune via `.env`.
- **VAD interruption.** Background noise (or the assistant's own audio if echo cancellation is off in the browser) can trip server-VAD into thinking the user started speaking, which cancels the in-flight response. The backend now pins minted Realtime sessions to non-interrupting server VAD: `interrupt_response=false`, `threshold=0.78`, `prefix_padding_ms=350`, and `silence_duration_ms=650`. On the SPA side, request the mic with `echoCancellation: true, noiseSuppression: true, autoGainControl: true`, disable the local mic while assistant audio is audible, and ignore VAD events until playback finishes.
- **Missing tool reply.** If the SPA receives `response.function_call_arguments.done` but never returns `function_call_output` + `response.create`, the model stops talking and the response truncates. See the table above.

## Automated validation

Integration tests under `app/tests/` exercise registration, login (JSON + form), chat (including SSE and voice mint/tool/commit), documents, websites, and ZIP flows. They require **MongoDB** reachable at `MONGODB_URI` (default `mongodb://localhost:27017`) and use database **`rag_chatbot_backend_test`**. Optional: set **`TEST_MONGODB_URI`** to override the URI for tests.

```bash
pytest
```

If MongoDB is not running, tests will hang or error on the `clean_test_database` fixture.

## CORS

Configured via **`BACKEND_CORS_ORIGINS`**. In `development` and `test`, loopback origins are also matched with a regex (see `app/main.py`).
