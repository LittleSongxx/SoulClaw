export type TokenProvider = () => string;
export type UnauthorizedHandler = () => void;

export function createApiClient(getToken: TokenProvider, onUnauthorized: UnauthorizedHandler) {
  return async function api(path: string, init: RequestInit = {}) {
    const headers = new Headers(init.headers || {});
    headers.set("Content-Type", "application/json");
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(path, { ...init, headers });
    if (response.status === 401) {
      onUnauthorized();
      throw new Error("Authentication expired");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || response.statusText);
    return payload;
  };
}
