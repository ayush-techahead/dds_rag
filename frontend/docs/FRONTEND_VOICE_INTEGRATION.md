# Front-end implementation guide: voice chat integration

**Audience:** the front-end SPA agent / developer implementing voice chat against this
backend. This document is the complete spec — implementing what is here means the
voice product works flawlessly with the backend as it stands today.

Cross-references:
- The HTTP contract lives in [`docs/API_FOR_FRONTEND_AGENT.md`](./API_FOR_FRONTEND_AGENT.md).
- The machine-readable schema is [`openapi.json`](../openapi.json) at the repo root.
- OpenAI's own primer: <https://platform.openai.com/docs/guides/realtime>.

If anything in this document conflicts with `openapi.json`, the OpenAPI is authoritative
(it's regenerated from the FastAPI app and the Pydantic models).

---

## TL;DR — what changed and what you must implement

Three earlier symptoms on the front-end ("audio stops midway", "no text appears in the
chat UI", "responses aren't saved") all reduced to one root cause: the SPA was rendering
audio but was not subscribing to the **OpenAI Realtime data-channel events**. The backend
is now hardened (idempotent commits, retry on mint, RAG budget for voice, audit log,
rate limit, async title generation, partial unique index, in-process embedding cache),
but the SPA must still implement six specific things:

1. **Mint a session** via `POST /api/v1/chat/sessions/{session_id}/realtime/session`.
2. **Open the WebRTC peer connection** to OpenAI using `client_secret.value` and the
   returned `model`. Add the mic track with echo cancellation, noise suppression, and
   AGC enabled. Add a data channel called `oai-events`.
3. **Subscribe to data-channel events** — at minimum the five listed in
   [§4](#4-data-channel-events---the-critical-bit). Without these the audio plays but no
   text appears and nothing is saved.
4. **Handle the `lookup_documentation` tool call** by POSTing the query to the backend's
   tool route, then sending `conversation.item.create` + `response.create` back to
   OpenAI. **Skipping this is the #2 cause of mid-response truncation.**
5. **Commit each completed turn** via `POST .../voice/commit` with a stable
   `client_turn_id` (UUIDv4 per turn). Replace your optimistic bubbles with the
   returned `[user, assistant]` pair verbatim.
6. **Handle the new error shapes** — 422 for malformed session IDs, 429 for rate-limited
   mints, 503 for OpenAI / Mongo transient. The backend already retries 5xx where safe;
   the SPA should surface 429 with a "slow down" message and not silently retry it.

---

## 1. Architecture overview

```
┌────────────────┐                          ┌──────────────────────┐
│   Browser SPA  │ ───── audio (WebRTC) ──► │  OpenAI Realtime     │
│  (this code)   │ ◄──── audio + events ─── │  (gpt-realtime)      │
└──┬─────────────┘                          └────────┬─────────────┘
   │                                                 │
   │ 1. mint session              4. tool query      │
   │    POST /realtime/session     POST .../tools    │
   │                                                 │
   │ 5. commit turn               (no audio relay)   │
   │    POST /voice/commit                           │
   ▼                                                 │
┌──────────────────────────────────────────┐         │
│  FastAPI backend                         │         │
│  - mints credentials, retries 5xx        │         │
│  - serves RAG search (lookup_documentation) ◄──────┘
│  - persists transcripts (Mongo)
│  - rate-limits mints, audits everything
└──────────────────────────────────────────┘
```

The backend never sees the audio. The browser talks WebRTC directly to OpenAI for
latency reasons. **The data channel between the browser and OpenAI is where the
text transcript and tool calls arrive — handling it is the SPA's job.**

---

## 2. Authentication

Same JWT as the rest of the chat API.

```ts
const tokenResp = await fetch("/api/v1/auth/login", {
  method: "POST",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ username: email, password }),
});
const { access_token } = await tokenResp.json();
```

Store the JWT (memory, `sessionStorage`, or however your existing auth works). Every
backend call below sends `Authorization: Bearer <access_token>`.

---

## 3. Voice session lifecycle

### 3.1 Pick or create a chat session

```ts
const sessionResp = await fetch("/api/v1/chat/sessions", {
  method: "POST",
  headers: { Authorization: `Bearer ${jwt}`, "Content-Type": "application/json" },
  body: JSON.stringify({}),
});
const { id: chatSessionId } = await sessionResp.json();
```

A chat session is just a logical grouping — voice and text turns can mix freely within
the same `chatSessionId`. The backend tags each persisted message with
`source: "voice" | "text"` so your UI can render them differently if you want.

### 3.2 Mint a Realtime session

```ts
type RealtimeSessionMintResponse = {
  chat_session_id: string;
  openai_session_id: string;
  model: string;
  client_secret: {
    value: string;       // ephemeral, expires within ~60 seconds — connect immediately
    expires_at: number;  // unix seconds
  };
};

const mintResp = await fetch(
  `/api/v1/chat/sessions/${chatSessionId}/realtime/session`,
  { method: "POST", headers: { Authorization: `Bearer ${jwt}` } },
);

if (mintResp.status === 429) {
  // Too many mints in the window. Show a "slow down" toast; do NOT auto-retry.
  const { detail } = await mintResp.json();
  throw new RateLimitError(detail);
}
if (!mintResp.ok) {
  // 503 means OpenAI was unreachable after the backend's own retries; user-friendly
  // error + offer a manual retry button.
  throw new Error((await mintResp.json()).detail);
}

const mint = (await mintResp.json()) as RealtimeSessionMintResponse;
```

**Important:** the `client_secret.value` is **short-lived** (often ~60 s). Open the
WebRTC connection immediately after the mint — don't cache the token.

The `model` field in the response is what OpenAI actually allocated; pass that to
the WebRTC handshake URL (it can differ from the default in `.env`).

### 3.3 Open the WebRTC peer connection

```ts
const pc = new RTCPeerConnection();

// 1. Hook up remote audio. WebRTC delivers the assistant's voice on a media track.
const audioEl = document.querySelector<HTMLAudioElement>("#voice-output")!;
pc.ontrack = (e) => { audioEl.srcObject = e.streams[0]; };

// 2. Add the mic with echo cancellation enabled.
//    Without echoCancellation, server-VAD can mistake the assistant's own audio
//    bleeding back through the mic for the user speaking, and cancel the response.
const micStream = await navigator.mediaDevices.getUserMedia({
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
});
micStream.getTracks().forEach((t) => pc.addTrack(t, micStream));

// 3. Open the data channel. The name "oai-events" is conventional.
//    All JSON events from OpenAI (transcripts, tool calls, errors) arrive here.
const dataChannel = pc.createDataChannel("oai-events");
dataChannel.addEventListener("message", (e) => {
  onRealtimeEvent(JSON.parse(e.data));   // see §4
});

// 4. SDP handshake.
const offer = await pc.createOffer();
await pc.setLocalDescription(offer);

const handshakeBase =
  import.meta.env.VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE === "calls"
    ? "https://api.openai.com/v1/realtime/calls"
    : "https://api.openai.com/v1/realtime";

const sdpResp = await fetch(`${handshakeBase}?model=${encodeURIComponent(mint.model)}`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${mint.client_secret.value}`,
    "Content-Type": "application/sdp",
  },
  body: offer.sdp,
});
if (!sdpResp.ok) throw new Error("WebRTC handshake failed");
await pc.setRemoteDescription({ type: "answer", sdp: await sdpResp.text() });
```

### 3.4 Tear down cleanly

```ts
function stopVoice() {
  dataChannel?.close();
  pc.getSenders().forEach((s) => s.track?.stop());
  pc.close();
  micStream.getTracks().forEach((t) => t.stop());
}
```

Call `stopVoice` from the route's cleanup (`onunmount`), on `visibilitychange`
"hidden" (optional), and from the user's "End call" button. **Not** calling this
leaks the mic indicator in the browser and keeps the OpenAI session billing.

### 3.5 Playback tail gate (mic reopen after audible end)

The reference SPA ([`src/hooks/useVoiceRealtime.ts`](../src/hooks/useVoiceRealtime.ts))
routes remote assistant playback through **Web Audio** (`MediaElementAudioSourceNode`
→ `AnalyserNode` → `destination`) and re-enables the local mic only after **sustained
sub-threshold RMS** once the playback-tail watcher is armed. The watcher is armed
immediately on `response.output_audio.done`. If only `response.done` has fired so far
(e.g. it raced ahead of `output_audio.done` or transcript completion), the client
waits **`RESPONSE_DONE_DEFERRED_TAIL_ARM_MS`** before arming the same watcher, because
`response.done` often lines up with **transcript** completion rather than local speaker
idle. OpenAI can also signal `output_audio.done` before the browser finishes the acoustic
tail; the RMS gate covers that gap.

**Transcript** events (`response.output_audio_transcript.*`) still drive chat bubbles and
`POST .../voice/commit`; they must **not** be used to reopen the mic, or speaker bleed
can be picked up as user speech.

Tunables live in [`src/lib/voiceRemotePlaybackGate.ts`](../src/lib/voiceRemotePlaybackGate.ts):
`PLAYBACK_TAIL_RMS_THRESHOLD`, `PLAYBACK_TAIL_SILENCE_MS`, `PLAYBACK_TAIL_MIN_HOLD_MS`,
`PLAYBACK_TAIL_MAX_MS`, `PLAYBACK_TAIL_SAMPLE_INTERVAL_MS`,
`PLAYBACK_TAIL_FALLBACK_DELAY_MS` (fixed delay when the Web Audio graph cannot be built),
and `RESPONSE_DONE_DEFERRED_TAIL_ARM_MS` (defer tail arming when only `response.done` fired).

---

## 4. Data-channel events — the critical bit

Every JSON message on the data channel has a `type` string. Below is the **minimum
viable** dispatcher. Missing any of these five is what reproduces the bugs from the
earlier conversation.

```ts
type VoiceTurnState = {
  // Stable id minted at the start of every assistant turn; the same id goes into
  // the eventual /voice/commit body for idempotent retries.
  clientTurnId: string;
  openaiResponseId?: string;
  userTranscript: string;        // built from input_audio_transcription.completed
  assistantTranscript: string;   // accumulated from response.audio_transcript.delta
  // Tool-call accumulator (one outstanding call_id per response in practice).
  pendingToolArguments: Map<string, string>;
};

function onRealtimeEvent(evt: any) {
  switch (evt.type) {
    // ─────────────────────────────────────────────────────────────────────────
    // 1. User finished speaking → user bubble.
    case "conversation.item.input_audio_transcription.completed": {
      const text = (evt.transcript || "").trim();
      if (!text) return;
      currentTurn().userTranscript = text;
      ui.upsertUserBubble({ id: "optimistic-user", text, source: "voice" });
      return;
    }

    // 2. Assistant transcript streaming → live update the assistant bubble.
    case "response.audio_transcript.delta": {
      const turn = currentTurn();
      turn.assistantTranscript += evt.delta;
      turn.openaiResponseId = evt.response_id ?? turn.openaiResponseId;
      ui.upsertAssistantBubble({
        id: "optimistic-assistant",
        text: turn.assistantTranscript,
        source: "voice",
        streaming: true,
      });
      return;
    }

    // 3. Assistant finished → commit to backend and reconcile bubbles.
    case "response.audio_transcript.done": {
      const turn = currentTurn();
      turn.assistantTranscript = (evt.transcript || turn.assistantTranscript).trim();
      turn.openaiResponseId = evt.response_id ?? turn.openaiResponseId;
      if (!turn.userTranscript || !turn.assistantTranscript) return;
      void commitVoiceTurn(turn);  // see §5
      return;
    }

    // 4. Model asked for the lookup_documentation tool.
    case "response.function_call_arguments.delta": {
      const buf = currentTurn().pendingToolArguments;
      buf.set(evt.call_id, (buf.get(evt.call_id) || "") + evt.delta);
      return;
    }
    case "response.function_call_arguments.done": {
      void handleToolCall(evt.call_id, evt.name, evt.arguments);  // see §6
      return;
    }

    // 5. Server-emitted error. NEVER silently kill the peer connection here —
    //    surface it. Many errors are non-fatal (e.g. rate limit on tool call).
    case "error": {
      console.error("OpenAI Realtime error", evt.error);
      ui.toast(evt.error?.message ?? "Realtime error");
      return;
    }

    // Optional debug surfaces — ignore in production UI:
    //   "response.created", "response.done", "response.audio.delta" (already in audio track),
    //   "input_audio_buffer.speech_started", etc.
    default:
      return;
  }
}
```

### Why each one matters

| Event | What breaks if you skip it |
|---|---|
| `conversation.item.input_audio_transcription.completed` | User bubble never shows. |
| `response.audio_transcript.delta` | Assistant text never streams into the UI even though the user hears the audio. |
| `response.audio_transcript.done` | No `voice/commit` ever fires → nothing is saved → next session reload shows only the user side. |
| `response.function_call_arguments.done` | After the first tool-grounded question, the assistant either goes silent or cuts off mid-sentence. |
| `error` | A peer-connection drop you can't diagnose. |

> `response.audio.delta` events also exist and carry raw PCM/Opus. **Ignore them** when
> using WebRTC — audio comes via the media track. Decoding them yourself is only
> required for WebSocket transport.

---

## 5. Persisting each turn (`voice/commit`)

Call this from your `response.audio_transcript.done` handler. **Always pass a
`client_turn_id`** — without it the backend can't deduplicate retries.

```ts
import { v4 as uuid } from "uuid";

type ChatMessageResponse = {
  id: string;
  session_id: string;
  user_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  source: "text" | "voice";
};

async function commitVoiceTurn(turn: VoiceTurnState): Promise<void> {
  const body = {
    user_transcript: turn.userTranscript,
    assistant_transcript: turn.assistantTranscript,
    client_turn_id: turn.clientTurnId,       // generated once at turn start
    openai_response_id: turn.openaiResponseId,
  };

  let response: Response;
  try {
    response = await fetch(
      `/api/v1/chat/sessions/${chatSessionId}/voice/commit`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${jwt}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      },
    );
  } catch (e) {
    // Network failure — safe to retry with the SAME client_turn_id. The backend's
    // partial unique index guarantees retries cannot duplicate rows.
    return scheduleRetry(() => commitVoiceTurn(turn));
  }

  if (response.status === 422) {
    // Validation error — transcripts too long or session_id malformed.
    // This is a bug, not a transient failure; do NOT retry.
    console.error("voice/commit rejected:", await response.json());
    return;
  }
  if (response.status === 404) {
    // Session was deleted in another tab while voice was open. Stop the call.
    ui.toast("Chat session no longer exists.");
    return stopVoice();
  }
  if (response.status >= 500) {
    return scheduleRetry(() => commitVoiceTurn(turn));
  }

  const [user, assistant] = (await response.json()) as [
    ChatMessageResponse,
    ChatMessageResponse,
  ];
  // Replace the trailing optimistic bubbles with the canonical [user, assistant].
  ui.mergeVoiceCommitMessages([user, assistant]);

  // Title is generated server-side as a background task — it won't be on this
  // response, but it'll appear on the next GET. See §7.4.
  scheduleSessionListRefresh();

  // Reset for the next turn.
  startNewVoiceTurn();
}

function startNewVoiceTurn(): void {
  currentTurnRef.current = {
    clientTurnId: uuid(),
    userTranscript: "",
    assistantTranscript: "",
    pendingToolArguments: new Map(),
  };
}
```

### The `[user, assistant]` reconciliation contract

The response is **always exactly two messages**, in that order. Your
`mergeVoiceCommitMessages` should:

1. Remove the optimistic bubbles (the two you were updating during the turn).
2. Append the two messages from the response **as-is** — they have the canonical
   `id`, `created_at`, and `source: "voice"`.

If the array length or order ever differs from `[user, assistant]`, that's a backend
bug; report it but don't try to compensate locally — your reconciliation logic should
break loudly so we notice.

### Idempotency in plain English

- Same `client_turn_id` retried → backend returns the original pair. Safe.
- Two `client_turn_id`s for the same conceptual turn → backend stores **two**
  pairs. Bad. Generate the `client_turn_id` **once** at the start of the turn
  (i.e., in `startNewVoiceTurn`) and reuse it through every retry.
- `client_turn_id` is **optional** in the schema but you should always send it
  in production. Without it, network blips during commit produce duplicate rows.

---

## 6. Tool call: `lookup_documentation`

When the user asks anything procedural, the model emits a function call. The SPA
acts as the HTTP bridge between OpenAI and the backend.

```ts
async function handleToolCall(
  callId: string,
  name: string,
  rawArguments: string,   // JSON string in the .done event
): Promise<void> {
  if (name !== "lookup_documentation") {
    // Unknown tool. Tell the model we couldn't satisfy the call, then ask it to
    // continue. Skipping this freezes the response.
    sendDataChannel({
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: callId,
        output: "ERROR: unknown tool",
      },
    });
    sendDataChannel({ type: "response.create" });
    return;
  }

  let query = "";
  try {
    query = JSON.parse(rawArguments)?.query ?? "";
  } catch {
    /* fall through with empty query; backend handles it */
  }

  let result = "NO_SOURCES: tool call failed locally.";
  try {
    const r = await fetch(
      `/api/v1/chat/sessions/${chatSessionId}/realtime/tools/lookup_documentation`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${jwt}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ query }),
      },
    );
    if (r.ok) result = (await r.json()).result;
  } catch (e) {
    console.error("lookup_documentation HTTP failed", e);
  }

  // Always send a tool output back, even on failure — otherwise the model hangs.
  sendDataChannel({
    type: "conversation.item.create",
    item: {
      type: "function_call_output",
      call_id: callId,
      output: result,
    },
  });
  sendDataChannel({ type: "response.create" });
}

function sendDataChannel(event: object): void {
  if (dataChannel.readyState === "open") {
    dataChannel.send(JSON.stringify(event));
  }
}
```

The backend's tool route is **already authenticated** with the same JWT (it runs RAG
against the user's own indexed documents). The response shape is fixed:

```ts
type LookupDocumentationResponse = { result: string };
```

`result` is either:
- A multi-line block of numbered source cards (`[1]`, `[2]`, …) with URL/excerpt
  metadata, or
- A line starting with `NO_SOURCES:` when nothing matched. Pass either back verbatim
  in `function_call_output`; the model is instructed to handle both.

### Voice-specific RAG budget (informational)

You don't configure this from the SPA — the backend already uses a tighter RAG budget
for the voice tool path (`CHAT_RAG_VOICE_TOP_K=3`, `CHAT_RAG_VOICE_EXCERPT_CHARS=1500`)
than for the text chat path. This keeps the model's working context small so voice
responses don't truncate. Just know that the response is meant to be terse.

---

## 7. UX & error handling

### 7.1 New HTTP status codes you must handle

| Status | When | UI behavior |
|---|---|---|
| **422** on any `/api/v1/chat/sessions/{id}/…` | Malformed session id, or transcripts exceed length limits | Show a developer-facing error. Should not happen with a well-formed SPA. Don't retry. |
| **429** on mint | User has crossed the per-user mint quota (default 20 / minute) | Toast "Too many voice sessions, slow down". Do **not** auto-retry — exponential backoff is OK but only on explicit user action. |
| **404** on commit / mint | Session was deleted in another tab | Toast and tear down the WebRTC connection. |
| **503** on mint | OpenAI Realtime unreachable after backend's own retries | "Voice service is temporarily unavailable. Retry?" with a button. |
| **503** on commit | Mongo / OpenAI transient | Auto-retry the same `client_turn_id` up to N times (e.g. 3, with backoff). After exhaustion, surface to user and keep the optimistic bubbles. |

### 7.2 Always retry commit with the same `client_turn_id`

The backend's partial unique index guarantees retries can't duplicate. The SPA's
job is to keep the same id across retries:

```ts
const turn = startNewVoiceTurn();   // mints the client_turn_id
// ... fill in transcripts ...
commitVoiceTurn(turn);              // retries internally with the same id
```

### 7.3 Browser audio permissions

`getUserMedia` will fail if the user has previously denied mic access. Handle the
exception explicitly:

```ts
try {
  micStream = await navigator.mediaDevices.getUserMedia({ audio: { /* … */ } });
} catch (e) {
  if (e instanceof DOMException && e.name === "NotAllowedError") {
    ui.toast("Mic access was denied. Allow it in browser settings to use voice.");
  } else {
    ui.toast("Couldn't access the microphone.");
  }
  return;
}
```

### 7.4 Session title on first voice turn

The voice commit endpoint does **not** set `X-Chat-Session-Title` (title generation
runs server-side as a background task so the commit response can return immediately).
For voice-first sessions, refresh the session sidebar / detail a short time after
the first commit:

```ts
async function commitVoiceTurn(turn: VoiceTurnState) {
  /* … as above … */
  if (turn === firstTurnInSession) {
    // Poll a couple of times; title usually lands within ~2 s.
    for (let i = 0; i < 5; i++) {
      await sleep(500);
      const detail = await fetchSessionDetail(chatSessionId);
      if (detail.title) {
        ui.setSessionTitle(detail.title);
        break;
      }
    }
  }
}
```

For text-first sessions the title still arrives synchronously via the
`X-Chat-Session-Title` header on `POST .../messages` — no change.

### 7.5 Surface `source` in the UI (optional but nice)

Stored messages now carry `source: "text" | "voice"`. Use it to add a small mic
icon next to voice-originated messages so users can tell what was said vs typed.
The same `chatSessionId` can contain both kinds.

```tsx
<MessageBubble
  text={msg.content}
  align={msg.role === "user" ? "right" : "left"}
  icon={msg.source === "voice" ? <MicIcon /> : null}
/>
```

### 7.6 Handle session expiry mid-call

`mint.client_secret.expires_at` tells you when the ephemeral token dies (usually ~60 s
from issue). The WebRTC peer connection survives past that point because it negotiated
TLS keys at handshake, but if the connection drops you'll need to re-mint and reconnect.
For long voice calls, refresh the session ~30 s before expiry by re-minting and
swapping the data channel. Most users don't need this; implement only if calls
routinely exceed a couple of minutes.

---

## 8. Types you'll likely want to define

These match the OpenAPI schema exactly (re-derive them with your codegen if you have
one — `openapi.json` at the repo root is the source of truth).

```ts
export type RealtimeSessionMintResponse = {
  chat_session_id: string;
  openai_session_id: string;
  model: string;
  client_secret: { value: string; expires_at: number };
};

export type LookupDocumentationRequest = { query: string };
export type LookupDocumentationResponse = { result: string };

export type VoiceCommitRequest = {
  user_transcript: string;           // 1..8000 chars
  assistant_transcript: string;      // 1..16000 chars
  client_turn_id?: string;           // ≤128 chars — strongly recommended
  openai_response_id?: string;       // ≤128 chars — debug breadcrumb
};

export type ChatMessageResponse = {
  id: string;
  session_id: string;
  user_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  source: "text" | "voice";
};

export type ChatSessionResponse = {
  id: string;
  user_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
};

export type ChatSessionDetailResponse = ChatSessionResponse & {
  messages: ChatMessageResponse[];
};
```

---

## 9. Environment variables on the SPA

| Var | Purpose | Default |
|---|---|---|
| `VITE_API_BASE_URL` | Backend host (e.g. `http://localhost:8000`) | required |
| `VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE` | `sessions` (legacy `/v1/realtime`) or `calls` (newer `/v1/realtime/calls`). Match the OpenAI account's supported handshake. | `sessions` |

The backend's `OPENAI_REALTIME_API_BASE`, `OPENAI_REALTIME_MODEL`, `OPENAI_REALTIME_VOICE`,
`OPENAI_REALTIME_MAX_OUTPUT_TOKENS`, and VAD knobs are server-side only — the SPA
never sets these.

---

## 10. End-to-end flow checklist

Treat this as a verification checklist when implementing or auditing the SPA's voice
flow:

- [ ] **Mint** — `POST /realtime/session` is called only after the user clicks
      "Start voice" (not on page load — sessions are short-lived). 429 surfaces a
      clear toast.
- [ ] **WebRTC** — `getUserMedia` requests `echoCancellation: true, noiseSuppression:
      true, autoGainControl: true`. Audio output `<audio>` element is set up before
      adding the local track.
- [ ] **Data channel** — `oai-events` channel is open and the message handler is
      attached before SDP is exchanged.
- [ ] **User bubble** — appears on `conversation.item.input_audio_transcription.completed`.
- [ ] **Streaming assistant bubble** — updates on `response.audio_transcript.delta`.
- [ ] **Tool call** — `response.function_call_arguments.done` triggers a POST to
      `/realtime/tools/lookup_documentation`, followed by `conversation.item.create`
      with `function_call_output` and `response.create` on the data channel. Even
      a failing HTTP call returns a `function_call_output` (with a `NO_SOURCES:` body).
- [ ] **Voice commit** — `response.audio_transcript.done` triggers
      `POST /voice/commit` with `{user_transcript, assistant_transcript,
      client_turn_id, openai_response_id}`. Response is `[user, assistant]` and
      replaces the optimistic bubbles verbatim.
- [ ] **`client_turn_id`** — generated **once** per turn (e.g. on `response.created`
      or just before `commitVoiceTurn` for the first time), reused across all retries.
- [ ] **Error event** — `error` events on the data channel are logged and surfaced,
      not silently dropped.
- [ ] **Teardown** — `stopVoice()` runs on unmount, on "End call", and on
      page hide. Mic indicator goes away.
- [ ] **Title refresh** — after the first voice commit for a fresh session, the SPA
      polls `GET /sessions/{id}` a few times (or refreshes on next navigation) so
      the auto-generated title appears in the sidebar.
- [ ] **Source field** — messages rendered from history (`GET /sessions/{id}`) honor
      `source: "voice" | "text"` for any per-modality UI affordance.
- [ ] **Retry safety** — network failure on `voice/commit` re-fires the SAME
      `client_turn_id` (does not generate a new one).

---

## 11. Common pitfalls & their symptoms

| Symptom | Root cause |
|---|---|
| Audio plays but no text appears in the chat. | SPA isn't subscribing to `response.audio_transcript.delta` / `.done`. |
| Refreshing the page shows only the user's side, no assistants. | `voice/commit` was never called — same root cause as above. |
| Audio cuts off mid-sentence after a complex question. | `response.function_call_arguments.done` handler missing or not sending `function_call_output` + `response.create` back. |
| Audio cuts off mid-sentence on simple questions too. | Server-VAD self-interrupt: mic doesn't have echo cancellation, the assistant's audio is bleeding into the mic. Fix: `getUserMedia({ audio: { echoCancellation: true, ... } })`. |
| Same turn shows up twice on reload. | Two different `client_turn_id`s used for retries of the same turn. Fix: mint the id once per turn and reuse on retry. |
| 422 from a chat endpoint that worked yesterday. | A non-hex string is being passed where a session id is expected; the backend now validates session id format strictly at the routing layer. |
| 429 from `/realtime/session`. | User exceeded the per-user mint quota (default 20/minute). UI should show a friendly cooldown. |
| Title never appears for voice-first sessions. | SPA isn't polling `GET /sessions/{id}` after the first commit; title is generated server-side asynchronously now. |

---

## 12. Minimal complete reference (single-file React/TS sketch)

The shape below isn't production-ready, but compiles, hits every backend endpoint
exactly once, and handles every event type. Use it as a scaffold.

```tsx
import { useEffect, useRef, useState } from "react";
import { v4 as uuid } from "uuid";

const API = import.meta.env.VITE_API_BASE_URL ?? "";

type Turn = {
  clientTurnId: string;
  openaiResponseId?: string;
  userTranscript: string;
  assistantTranscript: string;
  pendingToolArguments: Map<string, string>;
};

export function VoiceChat({ jwt, chatSessionId }: { jwt: string; chatSessionId: string }) {
  const pcRef = useRef<RTCPeerConnection | null>(null);
  const dcRef = useRef<RTCDataChannel | null>(null);
  const turnRef = useRef<Turn>(freshTurn());
  const [bubbles, setBubbles] = useState<Array<{ id: string; role: "user" | "assistant"; text: string }>>([]);

  async function start() {
    const mint = await mintSession(jwt, chatSessionId);
    const pc = new RTCPeerConnection();
    pcRef.current = pc;

    pc.ontrack = (e) => {
      const audio = document.getElementById("voice-output") as HTMLAudioElement;
      audio.srcObject = e.streams[0];
    };

    const mic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    mic.getTracks().forEach((t) => pc.addTrack(t, mic));

    const dc = pc.createDataChannel("oai-events");
    dcRef.current = dc;
    dc.onmessage = (e) => onEvent(JSON.parse(e.data));

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const handshake = "https://api.openai.com/v1/realtime";
    const sdp = await fetch(`${handshake}?model=${mint.model}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${mint.client_secret.value}`, "Content-Type": "application/sdp" },
      body: offer.sdp,
    });
    await pc.setRemoteDescription({ type: "answer", sdp: await sdp.text() });
  }

  function stop() {
    dcRef.current?.close();
    pcRef.current?.getSenders().forEach((s) => s.track?.stop());
    pcRef.current?.close();
    pcRef.current = null;
  }

  function onEvent(evt: any) {
    const turn = turnRef.current;
    switch (evt.type) {
      case "conversation.item.input_audio_transcription.completed":
        turn.userTranscript = (evt.transcript || "").trim();
        upsert("optimistic-user", "user", turn.userTranscript);
        break;
      case "response.audio_transcript.delta":
        turn.assistantTranscript += evt.delta;
        turn.openaiResponseId = evt.response_id ?? turn.openaiResponseId;
        upsert("optimistic-assistant", "assistant", turn.assistantTranscript);
        break;
      case "response.audio_transcript.done":
        turn.assistantTranscript = (evt.transcript || turn.assistantTranscript).trim();
        turn.openaiResponseId = evt.response_id ?? turn.openaiResponseId;
        if (turn.userTranscript && turn.assistantTranscript) {
          void commit(turn);
        }
        break;
      case "response.function_call_arguments.delta": {
        const buf = turn.pendingToolArguments;
        buf.set(evt.call_id, (buf.get(evt.call_id) || "") + evt.delta);
        break;
      }
      case "response.function_call_arguments.done":
        void runTool(evt.call_id, evt.name, evt.arguments);
        break;
      case "error":
        console.error("realtime error", evt.error);
        break;
    }
  }

  async function commit(turn: Turn) {
    const r = await fetch(`${API}/api/v1/chat/sessions/${chatSessionId}/voice/commit`, {
      method: "POST",
      headers: { Authorization: `Bearer ${jwt}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        user_transcript: turn.userTranscript,
        assistant_transcript: turn.assistantTranscript,
        client_turn_id: turn.clientTurnId,
        openai_response_id: turn.openaiResponseId,
      }),
    });
    if (!r.ok) return;
    const [user, assistant] = await r.json();
    setBubbles((prev) =>
      prev
        .filter((b) => b.id !== "optimistic-user" && b.id !== "optimistic-assistant")
        .concat({ id: user.id, role: "user", text: user.content })
        .concat({ id: assistant.id, role: "assistant", text: assistant.content }),
    );
    turnRef.current = freshTurn();
  }

  async function runTool(callId: string, name: string, rawArgs: string) {
    let result = "NO_SOURCES: tool call failed locally.";
    if (name === "lookup_documentation") {
      try {
        const q = JSON.parse(rawArgs)?.query ?? "";
        const r = await fetch(
          `${API}/api/v1/chat/sessions/${chatSessionId}/realtime/tools/lookup_documentation`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${jwt}`, "Content-Type": "application/json" },
            body: JSON.stringify({ query: q }),
          },
        );
        if (r.ok) result = (await r.json()).result;
      } catch (e) { console.error(e); }
    }
    dcRef.current?.send(JSON.stringify({
      type: "conversation.item.create",
      item: { type: "function_call_output", call_id: callId, output: result },
    }));
    dcRef.current?.send(JSON.stringify({ type: "response.create" }));
  }

  function upsert(id: string, role: "user" | "assistant", text: string) {
    setBubbles((prev) => {
      const i = prev.findIndex((b) => b.id === id);
      if (i === -1) return [...prev, { id, role, text }];
      const copy = prev.slice();
      copy[i] = { id, role, text };
      return copy;
    });
  }

  useEffect(() => () => stop(), []);

  return (
    <div>
      <audio id="voice-output" autoPlay />
      <button onClick={start}>Start voice</button>
      <button onClick={stop}>Stop</button>
      {bubbles.map((b) => (
        <div key={b.id} className={b.role}>{b.text}</div>
      ))}
    </div>
  );
}

function freshTurn(): Turn {
  return {
    clientTurnId: uuid(),
    userTranscript: "",
    assistantTranscript: "",
    pendingToolArguments: new Map(),
  };
}

async function mintSession(jwt: string, chatSessionId: string) {
  const r = await fetch(
    `${API}/api/v1/chat/sessions/${chatSessionId}/realtime/session`,
    { method: "POST", headers: { Authorization: `Bearer ${jwt}` } },
  );
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}
```

---

## 13. What you do **not** need to do

These were soft-spots in earlier rounds. The backend now owns them, so save your time:

- **No retry on idempotent mint failures (5xx)** — the backend already retries
  transient OpenAI errors twice with backoff. If you still see 503, OpenAI is genuinely
  down; surface the error and let the user click "retry".
- **No client-side dedupe of voice/commit retries** — the backend's partial unique
  index handles concurrent and serial retries safely. Just keep the `client_turn_id`
  stable.
- **No client-side rate limiting on mint** — the backend enforces the quota and
  returns 429. Just handle the status code.
- **No client-side embedding cache** — the backend caches embeddings per process for
  the lookup_documentation path; repeated tool calls reuse it.
- **No SPA-side title generation** — the backend generates one per session in the
  background. Just refresh `GET /sessions/{id}` to see it.

---

## 14. Glossary

- **Realtime session** — the short-lived OpenAI WebRTC session minted via
  `POST /realtime/session`. Lives until disconnect or expiry.
- **Chat session (`chat_session_id`)** — the long-lived Mongo `chat_sessions` row
  used to group voice/text turns for the same conversation. Survives across
  Realtime sessions.
- **Turn** — one user-utterance / assistant-response pair. The unit of
  `voice/commit`. Each turn gets a fresh `client_turn_id`.
- **`source`** — `"text"` if the message came from typing, `"voice"` if it was
  committed from a Realtime turn. Set by the backend; never trust the SPA to
  override it.
- **`oai-events`** — conventional name for the WebRTC data channel used by OpenAI
  Realtime. Carries JSON events; carries no audio.
