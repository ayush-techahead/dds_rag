import { apiUrl } from '../lib/apiBase';

export type RealtimeSessionMintResponse = {
  chat_session_id: string;
  openai_session_id: string;
  client_secret: { value: string; expires_at: number };
  model: string;
  voice_instructions?: string;
};

async function parseDetail(response: Response): Promise<string> {
  const text = await response.text().catch(() => '');
  try {
    const j = JSON.parse(text) as { detail?: unknown };
    const d = j.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return JSON.stringify(d);
    if (d != null && typeof d === 'object') return JSON.stringify(d);
  } catch {
    /* ignore */
  }
  return text ? text.slice(0, 400) : response.statusText;
}

export async function mintRealtimeSession(
  accessToken: string,
  sessionId: string
): Promise<RealtimeSessionMintResponse> {
  const url = apiUrl(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/realtime/session`
  );
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
      'ngrok-skip-browser-warning': 'true',
    },
  });

  if (response.status === 429) {
    const detail = await parseDetail(response);
    throw new Error(
      detail.trim().length > 0
        ? `Too many voice sessions — please slow down. ${detail}`
        : 'Too many voice sessions — please slow down and try again in a minute.'
    );
  }

  if (response.status === 503) {
    const detail = await parseDetail(response);
    throw new Error(
      detail.trim().length > 0
        ? `Voice service is temporarily unavailable. ${detail}`
        : 'Voice service is temporarily unavailable. Please try again.'
    );
  }

  if (response.status === 404) {
    throw new Error('Chat session was not found. Start a new chat or pick another session.');
  }

  if (!response.ok) {
    const detail = await parseDetail(response);
    throw new Error(
      `Mint realtime session failed (${response.status})${detail ? `: ${detail}` : ''}`
    );
  }

  return (await response.json()) as RealtimeSessionMintResponse;
}
