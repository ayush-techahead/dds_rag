/**
 * Browser → OpenAI Realtime HTTP (SDP exchange). Must match where sessions are minted
 * (default https://api.openai.com/v1). Override if your deployment uses a compatible proxy.
 */
export function getOpenAiRealtimeHttpBase(): string {
  const raw = import.meta.env.VITE_OPENAI_REALTIME_HTTP_BASE;
  if (typeof raw === 'string' && raw.trim() !== '') {
    return raw.trim().replace(/\/$/, '');
  }
  return 'https://api.openai.com/v1';
}

/**
 * Ephemeral keys from `POST /v1/realtime/sessions` are the “beta” shape. OpenAI rejects them
 * against the GA WebRTC endpoint `POST /v1/realtime/calls` (400 API version mismatch).
 * Use `POST /v1/realtime?model=…` for that handshake instead.
 *
 * If you mint GA `client_secrets` and need the unified interface, set
 * `VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE=calls` to use `/v1/realtime/calls` (no model query).
 */
export function openAiRealtimeWebRtcSdpUrl(model: string): string {
  const base = getOpenAiRealtimeHttpBase();
  const handshake =
    typeof import.meta.env.VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE === 'string'
      ? import.meta.env.VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE.trim().toLowerCase()
      : '';
  if (handshake === 'calls') {
    return `${base}/realtime/calls`;
  }
  const m = model.trim() || 'gpt-4o-realtime-preview';
  return `${base}/realtime?model=${encodeURIComponent(m)}`;
}
