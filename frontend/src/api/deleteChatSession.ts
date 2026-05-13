import { apiUrl } from '../lib/apiBase';

/**
 * DELETE /api/v1/chat/sessions/{session_id} — soft-delete; 204 empty body (idempotent).
 */
export async function deleteChatSession(accessToken: string, sessionId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`), {
    method: 'DELETE',
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });
  if (!response.ok) {
    throw new Error(`Delete session failed: ${response.status} ${response.statusText}`);
  }
}
