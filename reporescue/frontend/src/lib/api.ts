import { getSupabase, isSupabaseConfigured } from "./supabase";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type ApiOptions = {
  /** Attach the Supabase access token as a Bearer token (default true). */
  auth?: boolean;
  /** Request timeout in milliseconds. */
  timeoutMs?: number;
};

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

/** fetch() against the FastAPI backend with optional Bearer auth. */
export async function apiFetch(
  path: string,
  init: RequestInit = {},
  options: ApiOptions = {}
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (options.auth !== false && isSupabaseConfigured) {
    const { data } = await getSupabase().auth.getSession();
    const token = data.session?.access_token;
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
  }
  const url = /^https?:\/\//i.test(path) ? path : `${API_BASE_URL}${path}`;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? 30_000);
  const abortFromCaller = () => controller.abort(init.signal?.reason);
  if (init.signal?.aborted) abortFromCaller();
  else init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  try {
    return await fetch(url, { ...init, headers, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError(0, init.signal?.aborted ? "Request was cancelled." : "The request timed out. Please try again.");
    }
    throw new ApiError(0, error instanceof Error ? `Could not reach the RepoRescue API: ${error.message}` : "Could not reach the RepoRescue API.");
  } finally {
    window.clearTimeout(timer);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}

/** Throw ApiError with the server's detail message unless response.ok. */
export async function throwForStatus(response: Response): Promise<void> {
  if (response.ok) return;
  const labels: Record<number, string> = {
    401: "Your session is no longer valid. Sign in again.",
    403: "You do not have permission to perform this operation.",
    404: "The requested repository or investigation was not found.",
    409: "This operation conflicts with current account or repository data.",
    422: "The API rejected the submitted data. Check the fields and try again.",
    429: "The API is rate limiting requests. Wait a moment and retry.",
    500: "The API encountered an internal error.",
    503: "The API is temporarily unavailable.",
  };
  let detail = labels[response.status] ?? `API returned HTTP ${response.status}`;
  try {
    const body = (await response.json()) as {
      detail?: unknown;
      message?: unknown;
    };
    if (typeof body.detail === "string") detail = `${detail} ${body.detail}`;
    else if (Array.isArray(body.detail)) detail = `${detail} ${body.detail.map((item) => typeof item === "object" && item !== null && "msg" in item ? String(item.msg) : String(item)).join("; ")}`;
    else if (typeof body.message === "string") detail = `${detail} ${body.message}`;
  } catch {
    /* keep the default detail */
  }
  throw new ApiError(response.status, detail);
}

/** apiFetch() that throws ApiError on HTTP errors and returns parsed JSON. */
export async function apiJson<T>(
  path: string,
  init: RequestInit = {},
  options: ApiOptions = {}
): Promise<T> {
  const response = await apiFetch(path, init, options);
  await throwForStatus(response);
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  if (!text.trim()) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, "The API returned an invalid response body.");
  }
}
