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
 * GA `client_secrets` connect through `/v1/realtime/calls`. The legacy
 * `/v1/realtime?model=...` SDP path is available only by explicit opt-in.
 */
export function openAiRealtimeWebRtcSdpUrl(model: string): string {
  const base = getOpenAiRealtimeHttpBase();
  const handshake =
    typeof import.meta.env.VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE === 'string'
      ? import.meta.env.VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE.trim().toLowerCase()
      : '';
  if (handshake !== 'realtime') {
    return `${base}/realtime/calls`;
  }
  const m = model.trim() || 'gpt-realtime-2';
  return `${base}/realtime?model=${encodeURIComponent(m)}`;
}
