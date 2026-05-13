import { apiUrl } from '../lib/apiBase';
import { createSessionBodySchema } from './schemas';

export const CHAT_SESSION_ID_STORAGE_KEY = 'dds_demo_chat_session_id';

export interface ChatSession {
  id: string;
  user_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export async function createChatSession(accessToken: string): Promise<ChatSession> {
  const response = await fetch(apiUrl('/api/v1/chat/sessions'), {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(createSessionBodySchema.parse({})),
  });
  if (!response.ok) {
    throw new Error(
      `Create session failed: ${response.status} ${response.statusText}`
    );
  }
  const session = (await response.json()) as ChatSession;
  localStorage.setItem(CHAT_SESSION_ID_STORAGE_KEY, session.id);
  return session;
}
