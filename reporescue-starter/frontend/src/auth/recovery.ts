/**
 * Pure helpers for Supabase email-link callbacks and recovery sessions.
 *
 * Kept free of the Supabase client so the parsing and session-shape logic
 * is unit-testable. AuthContext wires these to the real client.
 */

/** otp types accepted in `?token_hash=…&type=…` callback links. */
const VERIFY_TYPES = new Set([
  "signup",
  "recovery",
  "email",
  "invite",
  "magiclink",
  "email_change",
]);

export type AuthCallback =
  | { kind: "code"; code: string }
  | { kind: "token_hash"; tokenHash: string; type: string }
  | { kind: "none" };

/** Parse a callback URL for PKCE `code` or legacy `token_hash` params. */
export function parseAuthCallback(href: string): AuthCallback {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return { kind: "none" };
  }
  const code = url.searchParams.get("code");
  if (code) return { kind: "code", code };
  const tokenHash = url.searchParams.get("token_hash");
  const type = url.searchParams.get("type") ?? "";
  if (tokenHash && VERIFY_TYPES.has(type)) {
    return { kind: "token_hash", tokenHash, type };
  }
  return { kind: "none" };
}

/** Strip callback query params after consumption so reloads stay clean. */
export function cleanCallbackUrl(href: string): string {
  try {
    const url = new URL(href);
    url.searchParams.delete("code");
    url.searchParams.delete("token_hash");
    url.searchParams.delete("type");
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}

type JwtPayload = {
  amr?: unknown;
};

/**
 * True when the access token carries a `recovery` amr method, i.e. the
 * session came from a password-reset link and must only set a new password.
 * UI hint only — the backend verifies the real JWT signature.
 */
export function accessTokenIndicatesRecovery(accessToken: string): boolean {
  try {
    const payloadSegment = accessToken.split(".")[1] ?? "";
    const normalized = payloadSegment.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(
      normalized.length + ((4 - (normalized.length % 4)) % 4),
      "="
    );
    const payload = JSON.parse(atob(padded)) as JwtPayload;
    if (!Array.isArray(payload.amr)) return false;
    return payload.amr.some(
      (entry) =>
        typeof entry === "object" &&
        entry !== null &&
        (entry as { method?: unknown }).method === "recovery"
    );
  } catch {
    return false;
  }
}

/** Friendly message for email-link callback failures. Never invents delivery. */
export function callbackErrorMessage(raw: string): string {
  const lowered = raw.toLowerCase();
  if (lowered.includes("expired")) {
    return (
      "This link has expired. Request a fresh one below " +
      "and try again."
    );
  }
  if (
    lowered.includes("invalid") ||
    lowered.includes("not found") ||
    lowered.includes("already")
  ) {
    return (
      "This link is invalid or was already used. " +
      "Request a fresh one below and try again."
    );
  }
  return raw;
}

/** Friendly mapping for email sign-in failures. */
export function signInErrorMessage(raw: string): {
  message: string;
  emailUnconfirmed: boolean;
} {
  const lowered = raw.toLowerCase();
  if (lowered.includes("email not confirmed")) {
    return {
      message:
        "This email address is not confirmed yet. " +
        "Check your inbox for the confirmation link, or resend it below.",
      emailUnconfirmed: true,
    };
  }
  if (lowered.includes("invalid login credentials")) {
    return {
      message: "Incorrect email or password. Try again.",
      emailUnconfirmed: false,
    };
  }
  return { message: raw, emailUnconfirmed: false };
}
