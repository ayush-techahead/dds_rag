import { apiUrl } from '../lib/apiBase';
import { streamMessageBodySchema } from './schemas';

/** Response header when the first assistant reply triggered auto-titling (non-streaming path). */
export const X_CHAT_SESSION_TITLE = 'X-Chat-Session-Title';

export type ChatMessageResponseRole = 'user' | 'assistant';

export interface ChatMessageResponse {
  role: ChatMessageResponseRole;
  content: string;
}

/** Readable only if the API sends `Access-Control-Expose-Headers: X-Chat-Session-Title` on cross-origin responses. */
export function readSessionTitleFromMessageResponse(response: Response): string | undefined {
  const raw = response.headers.get(X_CHAT_SESSION_TITLE);
  return typeof raw === 'string' && raw.trim().length > 0 ? raw.trim() : undefined;
}

/**
 * POST /api/v1/chat/sessions/{session_id}/messages (non-streaming).
 * After success, applies the same rule as fetch: use `readSessionTitleFromMessageResponse` or `sessionTitle` here.
 */
export async function postChatSessionMessages(
  accessToken: string,
  sessionId: string,
  content: string
): Promise<{ messages: ChatMessageResponse[]; sessionTitle?: string }> {
  const url = apiUrl(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`);
  const body = streamMessageBodySchema.parse({ content });

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'ngrok-skip-browser-warning': 'true',
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(`POST messages failed: ${response.status} ${response.statusText}${text ? ` — ${text}` : ''}`);
  }

  const data: unknown = await response.json();
  const messages = Array.isArray(data) ? (data as ChatMessageResponse[]) : [];
  const sessionTitle = readSessionTitleFromMessageResponse(response);

  return { messages, sessionTitle };
}
