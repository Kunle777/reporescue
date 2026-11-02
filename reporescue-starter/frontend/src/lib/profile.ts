import { apiJson } from "./api";

/** Mirrors backend ProfileResponse. */
export type Profile = {
  id: string;
  username: string | null;
  display_name: string | null;
  avatar_url: string | null;
  github_username: string | null;
  github_user_id: string | null;
  github_avatar_url: string | null;
  github_display_name: string | null;
  github_connected_at: string | null;
  created_at: string;
  updated_at: string;
};

export function getProfile(): Promise<Profile> {
  return apiJson<Profile>("/api/profile");
}

export function updateUsername(username: string): Promise<Profile> {
  return apiJson<Profile>("/api/profile", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
  });
}

/**
 * Store the session's verified GitHub identity. The body carries only the
 * replace-confirmation flag: identity itself always comes from the verified
 * JWT, never from the caller.
 */
export function syncGithubIdentity(confirm = false): Promise<Profile> {
  return apiJson<Profile>("/api/profile/github", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm }),
  });
}

/** Clear the GitHub link. Account, username, and projects survive. */
export function disconnectGithubIdentity(): Promise<Profile> {
  return apiJson<Profile>("/api/profile/github", { method: "DELETE" });
}
