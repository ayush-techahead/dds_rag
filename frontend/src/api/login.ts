import { apiUrl } from '../lib/apiBase';
import { loginRequestSchema } from './schemas';

/** Path under API_BASE (no trailing slash on origin). */
export const LOGIN_JSON_PATH = '/api/v1/auth/login';

export function validateLoginCredentials(
  emailTrimmed: string,
  password: string
): string | null {
  const parsed = loginRequestSchema.safeParse({
    email: emailTrimmed,
    password,
  });
  if (parsed.success) return null;
  const msg = parsed.error.issues[0]?.message;
  if (parsed.error.issues[0]?.code === 'invalid_string' && parsed.error.issues[0]?.path[0] === 'email') {
    return 'Enter a valid email address.';
  }
  return typeof msg === 'string' ? msg : 'Validation error.';
}

function formatLoginError(status: number, data: unknown): string {
  if (data && typeof data === 'object') {
    const o = data as Record<string, unknown>;
    if (typeof o.detail === 'string') {
      return o.detail;
    }
    if (Array.isArray(o.detail)) {
      const parts = o.detail.map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg: unknown }).msg);
        }
        return JSON.stringify(item);
      });
      if (parts.length > 0) return parts.join(' ');
    }
    if (status === 422 && Array.isArray(o.errors)) {
      return typeof o.detail === 'string' ? o.detail : 'Validation error.';
    }
  }
  if (status === 401) {
    return 'Incorrect email or password.';
  }
  if (status === 422) {
    return 'Validation error.';
  }
  if (status === 400) {
    return 'Invalid request.';
  }
  return 'Sign in failed. Please try again.';
}

export type LoginJsonResult =
  | { ok: true; access_token: string; token_type?: string }
  | { ok: false; message: string };

/**
 * JSON login (Content-Type: application/json; charset=utf-8).
 * Backend: POST {API_BASE}/api/v1/auth/login
 */
export async function loginWithJson(
  email: string,
  password: string
): Promise<LoginJsonResult> {
  const emailTrimmed = email.trim();
  const validationError = validateLoginCredentials(emailTrimmed, password);
  if (validationError) {
    return { ok: false, message: validationError };
  }

  const body = loginRequestSchema.parse({
    email: emailTrimmed,
    password,
  });

  const response = await fetch(apiUrl(LOGIN_JSON_PATH), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
    },
    body: JSON.stringify(body),
  });

  const text = await response.text();
  let data: unknown = {};
  if (text.trim()) {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      return {
        ok: false,
        message: text.slice(0, 200) || 'Invalid response from server.',
      };
    }
  }

  if (!response.ok) {
    return { ok: false, message: formatLoginError(response.status, data) };
  }

  if (typeof data === 'object' && data !== null && 'access_token' in data) {
    const token = (data as { access_token: unknown }).access_token;
    if (typeof token === 'string' && token.length > 0) {
      const tokenType = (data as { token_type?: unknown }).token_type;
      return {
        ok: true,
        access_token: token,
        token_type: typeof tokenType === 'string' ? tokenType : undefined,
      };
    }
  }

  return {
    ok: false,
    message: 'Login succeeded but no access token was returned.',
  };
}
