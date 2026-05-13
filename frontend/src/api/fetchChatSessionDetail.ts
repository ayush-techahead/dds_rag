import { apiUrl } from '../lib/apiBase';

export type ChatSessionMessageRow = {
  id?: string;
  role: string;
  content: string;
  source?: string;
};

export type ChatSessionDetailJson = {
  id?: string;
  title?: string | null;
  messages?: ChatSessionMessageRow[];
};

export async function fetchChatSessionDetail(
  accessToken: string,
  sessionId: string
): Promise<ChatSessionDetailJson> {
  const response = await fetch(
    apiUrl(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`),
    {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'ngrok-skip-browser-warning': 'true',
      },
    }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(
      `GET session failed: ${response.status} ${response.statusText}${text ? ` — ${text.slice(0, 200)}` : ''}`
    );
  }
  return (await response.json()) as ChatSessionDetailJson;
}
