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
};

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
  return fetch(path, { ...init, headers });
}

/** Throw ApiError with the server's detail message unless response.ok. */
export async function throwForStatus(response: Response): Promise<void> {
  if (response.ok) return;
  let detail = `API returned HTTP ${response.status}`;
  try {
    const body = (await response.json()) as {
      detail?: unknown;
      message?: unknown;
    };
    if (typeof body.detail === "string") detail = body.detail;
    else if (typeof body.message === "string") detail = body.message;
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
  return (await response.json()) as T;
}
