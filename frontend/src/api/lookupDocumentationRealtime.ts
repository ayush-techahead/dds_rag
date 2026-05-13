import { apiUrl } from '../lib/apiBase';

export type LookupDocumentationResponse = {
  result: string;
};

export async function lookupDocumentationRealtime(
  accessToken: string,
  sessionId: string,
  query: string
): Promise<string> {
  const url = apiUrl(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/realtime/tools/lookup_documentation`
  );
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
      'ngrok-skip-browser-warning': 'true',
    },
    body: JSON.stringify({ query }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(
      `lookup_documentation failed: ${response.status} ${response.statusText}${text ? ` — ${text.slice(0, 200)}` : ''}`
    );
  }
  const data = (await response.json()) as LookupDocumentationResponse;
  return data.result;
}
