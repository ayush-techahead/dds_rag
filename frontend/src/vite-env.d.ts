/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BACKEND_BASE_URL?: string;
  /** Override OpenAI Realtime HTTP origin (default https://api.openai.com/v1). */
  readonly VITE_OPENAI_REALTIME_HTTP_BASE?: string;
  /**
   * WebRTC SDP POST path: omit or `calls` (default) → POST /v1/realtime/calls.
   * `realtime` opts into the legacy POST /v1/realtime?model=… path.
   */
  readonly VITE_OPENAI_REALTIME_WEBRTC_HANDSHAKE?: 'calls' | 'realtime' | string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
