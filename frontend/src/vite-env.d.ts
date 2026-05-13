/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BACKEND_BASE_URL?: string;
  /** Override OpenAI Realtime HTTP origin (default https://api.openai.com/v1). */
  readonly VITE_OPENAI_REALTIME_HTTP_BASE?: string;
  /**
   * WebRTC SDP POST path: omit or `realtime` (default) → POST /v1/realtime?model=…
   * Use `calls` only if ephemeral keys are GA `client_secrets` compatible with POST /v1/realtime/calls.
   */
  readonly VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE?: 'calls' | 'realtime' | string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
