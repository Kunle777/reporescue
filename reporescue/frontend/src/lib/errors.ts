import { ApiError } from "./api";

export type FriendlyError = {
  title: string;
  message: string;
  retryable: boolean;
};

const CODE_TITLES: Record<string, { title: string; retryable: boolean }> = {
  DEBUG_PYTHON_ONLY: { title: "Debug is Python-only in beta", retryable: false },
  BETA_DAILY_AI_LIMIT_REACHED: { title: "Daily AI limit reached", retryable: false },
  BETA_DAILY_DEBUG_LIMIT_REACHED: { title: "Daily Debug limit reached", retryable: false },
  GLOBAL_AI_LIMIT_REACHED: { title: "RepoRescue is at capacity", retryable: false },
  PROVIDER_RATE_LIMITED: { title: "AI provider rate limit", retryable: true },
  PROVIDER_UNAVAILABLE: { title: "AI provider unavailable", retryable: true },
  CONVERSATION_AI_UNAVAILABLE: { title: "Conversation unavailable", retryable: false },
  IDEMPOTENCY_KEY_REUSED: { title: "Request key already used", retryable: false },
  IDEMPOTENCY_CONFLICT: { title: "Duplicate request", retryable: false },
  REQUEST_ALREADY_PROCESSING: { title: "Request already running", retryable: false },
  NOT_AUTHENTICATED: { title: "Session expired", retryable: false },
  AUTH_FAILED: { title: "Sign-in problem", retryable: false },
  AUTH_SERVICE_UNAVAILABLE: { title: "Sign-in unavailable", retryable: true },
};

/** Map an API failure to a safe user-facing title, message, and retry flag. */
export function friendlyError(
  error: unknown,
  fallbackTitle: string,
  fallbackMessage: string
): FriendlyError {
  if (error instanceof ApiError) {
    if (error.status === 0) {
      return {
        title: "Connection problem",
        message: "We couldn't reach RepoRescue. Check your connection and try again.",
        retryable: true,
      };
    }
    const known = error.code ? CODE_TITLES[error.code] : undefined;
    if (known) {
      // Backend messages for known codes are already user-safe.
      return { title: known.title, message: error.message, retryable: known.retryable };
    }
    const retryable =
      error.status === 408 || error.status === 429 || [502, 503, 504].includes(error.status);
    return { title: fallbackTitle, message: error.message, retryable };
  }
  return { title: fallbackTitle, message: fallbackMessage, retryable: false };
}

/** Friendly titles for persisted backend failure reasons (job banner). */
const REASON_TITLES: Record<string, string> = {
  model_not_found: "AI model unavailable",
  provider_auth: "AI provider access problem",
  api_rejected: "AI request rejected",
  api_error: "AI request failed",
  rate_limit: "AI provider rate limit",
  config_error: "AI not configured",
  invalid_output: "AI response problem",
  model_refused: "AI declined the request",
  needs_clarification: "Objective needs clarification",
};

export function reasonTitle(reason: string): string {
  return REASON_TITLES[reason] ?? reason;
}
