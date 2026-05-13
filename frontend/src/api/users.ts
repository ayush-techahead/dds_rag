import { apiUrl } from '../lib/apiBase';

/** GET /api/v1/users/me */
export interface UserResponse {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_superuser: boolean;
  created_at: string;
}

export async function fetchCurrentUser(accessToken: string): Promise<UserResponse> {
  const res = await fetch(apiUrl('/api/v1/users/me'), {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`GET /users/me failed: ${res.status} ${text.slice(0, 200)}`);
  }
  return (await res.json()) as UserResponse;
}
