import { useEffect, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  LogOut,
  Unlink,
} from "lucide-react";
import type { Session, User } from "@supabase/supabase-js";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../lib/api";
import { normalizeUsername, usernameError } from "../lib/auth-validation";
import {
  disconnectGithubIdentity,
  getProfile,
  syncGithubIdentity,
  updateUsername,
  type Profile,
} from "../lib/profile";
import githubMark from "../assets/github-mark.svg";

type GithubLink = {
  userId: string;
  username: string | null;
  displayName: string | null;
  avatarUrl: string | null;
  connectedAt: string | null;
};

function githubLinkOf(profile: Profile): GithubLink | null {
  if (!profile.github_user_id) return null;
  return {
    userId: profile.github_user_id,
    username: profile.github_username,
    displayName: profile.github_display_name,
    avatarUrl: profile.github_avatar_url,
    connectedAt: profile.github_connected_at,
  };
}

/** False when GitHub is the only sign-in method: disconnect would lock out. */
function hasOtherSignIn(user: User | null): boolean {
  const identities = user?.identities ?? [];
  return identities.some((identity) => identity.provider !== "github");
}

function providerOf(user: User): string | null {
  const metadata = user.app_metadata as unknown as Record<string, unknown>;
  const provider = metadata.provider;
  return typeof provider === "string" && provider ? provider : null;
}

function expiryOf(session: Session | null): string {
  if (!session?.expires_at) return "—";
  return new Date(session.expires_at * 1000).toLocaleString();
}

export default function SettingsView({
  user,
  session,
  onSignOut,
}: {
  user: User | null;
  session: Session | null;
  onSignOut: () => void;
}) {
  const provider = user ? providerOf(user) : null;
  const {
    linkGitHubIdentity,
    unlinkGitHubIdentity,
    githubSyncConflict,
    clearGithubSyncConflict,
    githubSyncTick,
  } = useAuth();
  const [username, setUsername] = useState<string | null>(null);
  const [profileLoaded, setProfileLoaded] = useState(false);
  const [profileError, setProfileError] = useState("");
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [github, setGithub] = useState<GithubLink | null>(null);
  const [githubBusy, setGithubBusy] = useState<
    "connect" | "disconnect" | "replace" | null
  >(null);
  const [githubError, setGithubError] = useState("");
  const [githubNotice, setGithubNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setProfileLoaded(false);
      setProfileError("");
      setUsername(null);
      setGithub(null);
      if (!user) {
        setProfileLoaded(true);
        return;
      }
      try {
        const profile = await getProfile();
        if (cancelled) return;
        setUsername(profile.username);
        setDraft(profile.username ?? "");
        setGithub(githubLinkOf(profile));
      } catch (error) {
        if (!cancelled) {
          setUsername(null);
          setGithub(null);
          setProfileError(error instanceof Error ? error.message : "Could not load your account profile.");
        }
      } finally {
        if (!cancelled) setProfileLoaded(true);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [user, githubSyncTick]);

  async function connectGithub() {
    setGithubBusy("connect");
    setGithubError("");
    setGithubNotice("");
    try {
      const result = await linkGitHubIdentity();
      if (result.error) {
        setGithubError(result.error);
        setGithubBusy(null);
      }
      // Success navigates to GitHub; the busy state stays until then.
    } catch (err) {
      setGithubError(err instanceof Error ? err.message : "Could not connect GitHub.");
      setGithubBusy(null);
    }
  }

  async function disconnectGithub() {
    setGithubBusy("disconnect");
    setGithubError("");
    setGithubNotice("");
    try {
      // Backend first (idempotent): if detaching at Supabase then fails,
      // the next login re-syncs instead of stranding a stale link.
      const cleared = await disconnectGithubIdentity();
      setGithub(githubLinkOf(cleared));
      const result = await unlinkGitHubIdentity();
      if (result.error) {
        setGithubError(
          `${result.error} The local link is cleared, but GitHub may ` +
            "reconnect on your next sign-in. Try again."
        );
        return;
      }
      setGithubNotice("GitHub disconnected. Your account and projects are untouched.");
    } catch (err) {
      setGithubError(
        err instanceof Error ? err.message : "Could not disconnect GitHub."
      );
    } finally {
      setGithubBusy(null);
    }
  }

  async function replaceGithub() {
    setGithubBusy("replace");
    setGithubError("");
    setGithubNotice("");
    try {
      const profile = await syncGithubIdentity(true);
      setGithub(githubLinkOf(profile));
      clearGithubSyncConflict();
      setGithubNotice("GitHub account replaced.");
    } catch (err) {
      setGithubError(
        err instanceof Error ? err.message : "Could not replace the GitHub account."
      );
    } finally {
      setGithubBusy(null);
    }
  }

  async function saveUsername(event: FormEvent) {
    event.preventDefault();
    setSaved(false);
    const invalid = usernameError(draft);
    if (invalid) {
      setDraftError(invalid);
      return;
    }
    setDraftError("");
    setSaving(true);
    try {
      const profile = await updateUsername(normalizeUsername(draft));
      setUsername(profile.username);
      setSaved(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setDraftError("That username is taken. Pick another one.");
      } else if (err instanceof ApiError && err.status === 422) {
        setDraftError(err.message);
      } else {
        setDraftError(
          err instanceof Error ? err.message : "Could not save the username."
        );
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Settings</h2>
          <p>Your account and current session.</p>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <h3>Account</h3>
        </div>
        {profileError && <div className="banner banner-error" role="alert"><AlertTriangle size={17} /><div><strong>Profile could not be loaded</strong><p>{profileError}</p></div></div>}
        <dl className="kv-list">
          <div className="kv-row">
            <dt>Email</dt>
            <dd>{user?.email ?? "—"}</dd>
          </div>
<div className="kv-row">
            <dt>Signed in with</dt>
            <dd>{provider ?? "—"}</dd>
          </div>
          <div className="kv-row">
            <dt>GitHub</dt>
            <dd>
              {!profileLoaded
                ? "…"
                : github?.username
                  ? `@${github.username}`
                  : "Not connected"}
            </dd>
          </div>
        </dl>
      </section>

      <section className="card">
        <div className="card-head">
          <h3>Username</h3>
        </div>
        {!profileLoaded ? (
          <p className="hint">Loading username…</p>
        ) : (
          <form onSubmit={(event) => void saveUsername(event)} className="form-stack">
            <div className="field">
              <label className="label" htmlFor="settings-username">
                {username ? "Your username" : "Pick a username"}
              </label>
              <input
                id="settings-username"
                className={`input${draftError ? " input-invalid" : ""}`}
                type="text"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                autoComplete="username"
                maxLength={30}
                placeholder="e.g. ada_debugs"
              />
              {draftError ? (
                <span className="field-error">{draftError}</span>
              ) : (
                <span className="hint">
                  3–30 characters: a letter first, then letters, digits or _ .
                  Usernames are unique.
                </span>
              )}
            </div>
            <div className="form-row">
              <button className="btn btn-secondary" type="submit" disabled={saving}>
                {saving && <LoaderCircle className="spin" size={15} />}
                Save username
              </button>
              {saved && (
                <span className="save-note">
                  <CheckCircle2 size={15} /> Saved
                </span>
              )}
            </div>
          </form>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h3>GitHub</h3>
        </div>
        {!profileLoaded ? (
          <p className="hint">Loading GitHub status…</p>
        ) : github ? (
          <div className="form-stack">
            <div className="form-row github-id-row">
              {github.avatarUrl && (
                <img
                  className="avatar-xs"
                  src={github.avatarUrl}
                  alt=""
                  width={22}
                  height={22}
                />
              )}
              <span>
                <strong>
                  {github.username
                    ? `@${github.username}`
                    : "Connected account"}
                </strong>
                {github.displayName && (
                  <span className="hint"> · {github.displayName}</span>
                )}
              </span>
            </div>
            <p className="hint">
              Verified GitHub identity, synced on sign-in. RepoRescue never
              asks for repository access.
            </p>
            {hasOtherSignIn(user) ? (
              <div className="form-row">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={githubBusy !== null}
                  onClick={() => void disconnectGithub()}
                >
                  {githubBusy === "disconnect" ? (
                    <LoaderCircle className="spin" size={14} />
                  ) : (
                    <Unlink size={14} />
                  )}
                  Disconnect GitHub
                </button>
              </div>
            ) : (
              <p className="hint">
                GitHub is your only sign-in method, so it cannot be
                disconnected here.
              </p>
            )}
          </div>
        ) : (
          <div className="form-stack">
            <p className="hint">
              Connect GitHub to show your verified developer identity. Sign-in
              only — RepoRescue never requests repository permissions.
            </p>
            <div className="form-row">
              <button
                type="button"
                className="btn btn-github btn-sm"
                disabled={githubBusy !== null}
                onClick={() => void connectGithub()}
              >
                {githubBusy === "connect" ? (
                  <LoaderCircle className="spin" size={14} />
                ) : (
                  <img src={githubMark} alt="" width={16} height={16} />
                )}
                {githubBusy === "connect"
                  ? "Redirecting to GitHub…"
                  : "Connect GitHub"}
              </button>
            </div>
          </div>
        )}
        {githubSyncConflict && (
          <div className="banner banner-error">
            <AlertTriangle size={17} />
            <div>
              <strong>Different GitHub account detected</strong>
              <p>{githubSyncConflict}</p>
              <div className="resend-row">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={githubBusy !== null}
                  onClick={() => void replaceGithub()}
                >
                  {githubBusy === "replace" && (
                    <LoaderCircle className="spin" size={14} />
                  )}
                  Replace with the signed-in account
                </button>
              </div>
            </div>
          </div>
        )}
        {githubError && (
          <div className="banner banner-error">
            <AlertTriangle size={17} />
            <div>
              <strong>GitHub action failed</strong>
              <p>{githubError}</p>
            </div>
          </div>
        )}
        {githubNotice && (
          <div className="banner banner-success">
            <CheckCircle2 size={17} />
            <div>
              <strong>Done</strong>
              <p>{githubNotice}</p>
            </div>
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h3>Session</h3>
        </div>
        <dl className="kv-list">
          <div className="kv-row">
            <dt>Access token expires</dt>
            <dd>{expiryOf(session)}</dd>
          </div>
        </dl>
        <p className="hint">
          Sessions persist across reloads. Signing out clears this browser session.
        </p>
        <button type="button" className="btn btn-secondary" onClick={onSignOut}>
          <LogOut size={15} /> Sign out
        </button>
      </section>
    </div>
  );
}
