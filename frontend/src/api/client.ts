/**
 * The one HTTP client. Components never call `fetch` directly.
 *
 * Two things live here because they must be the same everywhere:
 *
 * 1. **401 handling.** The API answers an unauthenticated request with 401
 *    JSON, never a redirect, so the client is what decides to send the user to
 *    the login screen. Doing it per-call would mean one forgotten call site
 *    renders a permanently empty panel instead.
 * 2. **Refusal vs failure.** The backend distinguishes "I considered this and
 *    said no" from "something broke" (backend/src/api/errors.py). A refusal's
 *    message is meant for the user and is preserved verbatim — an order
 *    rejection that reaches the screen as "Request failed" is the exact thing
 *    the money rules forbid.
 */
import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  readonly kind: string;
  readonly status: number;
  readonly ref: string | null;

  constructor(message: string, kind: string, status: number, ref: string | null) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.ref = ref;
  }

  /** The backend considered the request and refused it. The message is the
   *  answer, and it is safe — and required — to show the user. */
  get isRefusal(): boolean {
    return this.kind === "refusal";
  }

  get isUnauthenticated(): boolean {
    return this.status === 401;
  }
}

type Listener = () => void;
const unauthenticatedListeners = new Set<Listener>();

/** Called by AuthContext. Any 401 anywhere sends the user to the login screen. */
export function onUnauthenticated(listener: Listener): () => void {
  unauthenticatedListeners.add(listener);
  return () => unauthenticatedListeners.delete(listener);
}

async function parseError(response: Response): Promise<ApiError> {
  let kind = "internal";
  let message = `${response.status} ${response.statusText}`;
  let ref: string | null = null;
  try {
    const body = (await response.json()) as ApiErrorBody;
    if (body?.error) {
      kind = body.error.kind ?? kind;
      message = body.error.message ?? message;
      ref = body.error.ref ?? null;
    }
  } catch {
    // A non-JSON error body is itself information: it usually means the
    // request reached the static fallback instead of a route. Keep the status
    // line rather than inventing a friendlier message that hides it.
  }
  return new ApiError(message, kind, response.status, ref);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });

  if (!response.ok) {
    const error = await parseError(response);
    if (error.isUnauthenticated) {
      unauthenticatedListeners.forEach((l) => l());
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string, init?: RequestInit) => request<T>(path, { ...init, method: "GET" }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
};
