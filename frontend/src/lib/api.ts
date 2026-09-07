/** Thin fetch wrapper. Errors carry the server's detail so the UI can show it. */

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    // Development identity. With AQM_AUTH_REQUIRED=true the backend ignores
    // this and requires an Entra ID bearer token instead.
    "X-User-Email": localStorage.getItem("aqm.user") ?? "estimator@localhost",
    ...((init?.headers as Record<string, string>) ?? {}),
  };

  // File uploads must NOT carry a Content-Type of ours: the browser sets it
  // itself, including the multipart boundary the server needs to split the
  // request. Sending our own — even an empty one — produces a body the
  // server cannot parse.
  if (!(init?.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(`${BASE}${path}`, { ...init, headers });

  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = await response.text();
    }
    throw new ApiError(describe(detail) ?? response.statusText, response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function describe(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "detail" in detail) {
    return String((detail as { detail: unknown }).detail);
  }
  return detail ? JSON.stringify(detail) : null;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  /**
   * Send files. FormData sets its own Content-Type with the multipart
   * boundary in it, so the JSON header the other calls use has to be left
   * off — setting it by hand produces a request the server cannot split.
   */
  upload: <T,>(path: string, body: FormData) =>
    request<T>(path, { method: "POST", body }),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
};
