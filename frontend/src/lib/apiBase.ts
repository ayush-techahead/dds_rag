/**
 * Backend base URL from VITE_BACKEND_BASE_URL.
 * In production, default to the current origin so Nginx can proxy /api/* on an EC2 IP.
 * In dev, keep the existing localhost:8000 default.
 */
export function getApiBaseUrl(): string {
  const raw = import.meta.env.VITE_BACKEND_BASE_URL;
  if (typeof raw === 'string' && raw.trim() !== '') {
    return normalizeBase(raw.trim());
  }
  if (import.meta.env.PROD && typeof window !== 'undefined') {
    return window.location.origin;
  }
  return 'http://localhost:8000';
}

function normalizeBase(raw: string): string {
  const t = raw.replace(/\/$/, '');
  if (t.startsWith('http://') || t.startsWith('https://')) return t;
  if (t.startsWith('/')) return t;
  return `http://${t}`;
}

/** Path must start with / (e.g. `/auth/login`). Query string allowed. */
export function apiUrl(path: string): string {
  const base = getApiBaseUrl();
  const p = path.startsWith('/') ? path : `/${path}`;
  if (base.startsWith('http://') || base.startsWith('https://')) {
    return `${base.replace(/\/$/, '')}${p}`;
  }
  return `${base.replace(/\/$/, '')}${p}`;
}
