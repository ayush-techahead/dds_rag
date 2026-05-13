import { apiUrl } from '../lib/apiBase';

export type ChatMessageResponse = {
  role: string;
  content: string;
  id?: string;
  created_at?: string;
  session_id?: string;
  user_id?: string;
  source?: 'text' | 'voice' | string;
};

export type VoiceCommitOptions = {
  /** Idempotent retries: same id + session returns the original stored pair */
  clientTurnId?: string;
  /** Debug breadcrumb only (≤128 chars server-side) */
  openaiResponseId?: string;
};

const COMMIT_MAX_ATTEMPTS = 4;
const COMMIT_BACKOFF_MS = [0, 400, 1200, 2800];

export class VoiceCommitSessionNotFoundError extends Error {
  constructor(message = 'Chat session no longer exists.') {
    super(message);
    this.name = 'VoiceCommitSessionNotFoundError';
  }
}

export class VoiceCommitValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'VoiceCommitValidationError';
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function parseJsonDetail(response: Response): Promise<string> {
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
  return text ? text.slice(0, 300) : response.statusText;
}

function isVoiceCommitRetryable(status: number, err: unknown): boolean {
  if (status === 503) return true;
  if (typeof navigator !== 'undefined' && !navigator.onLine) return true;
  return err instanceof TypeError;
}

export async function commitVoiceTurn(
  accessToken: string,
  sessionId: string,
  userTranscript: string,
  assistantTranscript: string,
  options?: VoiceCommitOptions
): Promise<{ messages: ChatMessageResponse[]; sessionTitle: string | null }> {
  const url = apiUrl(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/voice/commit`
  );
  const body: Record<string, unknown> = {
    user_transcript: userTranscript,
    assistant_transcript: assistantTranscript,
  };
  if (options?.clientTurnId) {
    body.client_turn_id = options.clientTurnId;
  }
  if (options?.openaiResponseId) {
    body.openai_response_id = options.openaiResponseId;
  }

  let lastError: Error | null = null;

  for (let attempt = 0; attempt < COMMIT_MAX_ATTEMPTS; attempt++) {
    const delay = COMMIT_BACKOFF_MS[attempt] ?? COMMIT_BACKOFF_MS[COMMIT_BACKOFF_MS.length - 1];
    if (delay > 0) {
      await sleep(delay);
    }

    let response: Response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          'Content-Type': 'application/json',
          'ngrok-skip-browser-warning': 'true',
        },
        body: JSON.stringify(body),
      });
    } catch (e) {
      lastError = e instanceof Error ? e : new Error(String(e));
      if (attempt < COMMIT_MAX_ATTEMPTS - 1 && isVoiceCommitRetryable(0, e)) {
        continue;
      }
      throw lastError;
    }

    if (response.status === 422) {
      const detail = await parseJsonDetail(response);
      console.error('voice/commit rejected:', detail);
      throw new VoiceCommitValidationError(detail || 'Validation error');
    }

    if (response.status === 404) {
      throw new VoiceCommitSessionNotFoundError();
    }

    if (response.status >= 500) {
      lastError = new Error(
        `Voice commit failed: ${response.status} ${await parseJsonDetail(response)}`
      );
      if (attempt < COMMIT_MAX_ATTEMPTS - 1) {
        continue;
      }
      throw lastError;
    }

    if (!response.ok) {
      const detail = await parseJsonDetail(response);
      throw new Error(`Voice commit failed: ${response.status} ${detail}`);
    }

    const raw = (await response.json()) as ChatMessageResponse[];
    const list = Array.isArray(raw) ? raw : [];
    const sessionTitle =
      response.headers.get('X-Chat-Session-Title')?.trim() || null;
    return {
      messages: list,
      sessionTitle,
    };
  }

  throw lastError ?? new Error('Voice commit failed after retries');
}
