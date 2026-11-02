/**
 * Client-side auth form validation.
 *
 * These rules mirror the backend username policy in
 * backend/app/profiles.py exactly, so a name accepted here is accepted by
 * POST/PATCH profile endpoints (modulo uniqueness races, which the server
 * reports as HTTP 409). The backend always re-validates; this module only
 * gives instant field feedback.
 */

export const USERNAME_MIN_LENGTH = 3;
export const USERNAME_MAX_LENGTH = 30;
export const PASSWORD_MIN_LENGTH = 8;

/** Same rule as the backend: lowercase letter first, then letters/digits/_ . */
const USERNAME_PATTERN = /^[a-z][a-z0-9_]*$/;

/** Same reserved list as backend/app/profiles.py. */
const RESERVED_USERNAMES = new Set([
  "admin",
  "administrator",
  "api",
  "help",
  "moderator",
  "null",
  "official",
  "reporescue",
  "root",
  "security",
  "staff",
  "support",
  "system",
  "undefined",
]);

/** Trimmed + lowercased candidate, or "" for non-text input. */
export function normalizeUsername(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.trim().toLowerCase();
}

/** Field error for the signup username, or null when valid. */
export function usernameError(value: string): string | null {
  const username = normalizeUsername(value);
  if (!username) return "Choose a username.";
  if (username.length < USERNAME_MIN_LENGTH) {
    return `Username must be at least ${USERNAME_MIN_LENGTH} characters.`;
  }
  if (username.length > USERNAME_MAX_LENGTH) {
    return `Username must be at most ${USERNAME_MAX_LENGTH} characters.`;
  }
  if (!USERNAME_PATTERN.test(username)) {
    return "Use a letter first, then lowercase letters, digits or underscores.";
  }
  if (RESERVED_USERNAMES.has(username)) {
    return "That username is reserved. Pick another one.";
  }
  return null;
}

/** Field error for an email address, or null when valid. */
export function emailError(value: string): string | null {
  const email = value.trim();
  if (!email) return "Enter your email address.";
  // Practical format check; Supabase owns deliverability.
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return "Enter a valid email address.";
  }
  return null;
}

/** Field error for a signup/new password, or null when strong enough. */
export function passwordError(value: string): string | null {
  if (!value) return "Choose a password.";
  if (value.length < PASSWORD_MIN_LENGTH) {
    return `Password must be at least ${PASSWORD_MIN_LENGTH} characters.`;
  }
  if (!/[A-Za-z]/.test(value) || !/[0-9]/.test(value)) {
    return "Password needs at least one letter and one number.";
  }
  return null;
}

/** Field error for password confirmation, or null when it matches. */
export function confirmPasswordError(
  password: string,
  confirm: string
): string | null {
  if (!confirm) return "Repeat your password.";
  if (confirm !== password) return "Passwords do not match.";
  return null;
}
