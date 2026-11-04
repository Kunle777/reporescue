import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { Session, User } from "@supabase/supabase-js";
import { apiFetch } from "../lib/api";
import { getSupabase, isSupabaseConfigured } from "../lib/supabase";
import { normalizeUsername } from "../lib/auth-validation";
import {
  accessTokenIndicatesRecovery,
  callbackErrorMessage,
  cleanCallbackUrl,
  parseAuthCallback,
} from "./recovery";

export type AuthResult = {
  error: string | null;
  needsConfirmation: boolean;
};

export type SimpleResult = {
  error: string | null;
};

type AuthState = {
  configured: boolean;
  session: Session | null;
  user: User | null;
  loading: boolean;
  /** True when the session came from a reset link: only set a password. */
  recoveryPending: boolean;
  /** Friendly error from an expired/invalid email link, if any. */
  callbackError: string | null;
  clearCallbackError: () => void;
  /**
   * Set when the last GitHub auto-sync hit a different connected account
   * (HTTP 409): replacing it needs explicit confirmation in Settings.
   */
  githubSyncConflict: string | null;
  clearGithubSyncConflict: () => void;
  /** Bumped after every GitHub auto-sync attempt so views can reload. */
  githubSyncTick: number;
  signUp: (
    email: string,
    password: string,
    username: string
  ) => Promise<AuthResult>;
  signIn: (email: string, password: string) => Promise<AuthResult>;
  signInWithGitHub: () => Promise<AuthResult>;
  linkGitHubIdentity: () => Promise<SimpleResult>;
  unlinkGitHubIdentity: () => Promise<SimpleResult>;
  signOut: () => Promise<void>;
  resendConfirmation: (email: string) => Promise<SimpleResult>;
  requestPasswordReset: (email: string) => Promise<SimpleResult>;
  updatePassword: (password: string) => Promise<SimpleResult>;
};

const AuthContext = createContext<AuthState | null>(null);

function configError(): AuthResult {
  return {
    error:
      "Supabase is not configured. Copy frontend/.env.example to .env.local " +
      "and fill in VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.",
    needsConfirmation: false,
  };
}

function configSimpleError(): SimpleResult {
  return { error: configError().error };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(isSupabaseConfigured);
  const [recoveryEvent, setRecoveryEvent] = useState(false);
  const [callbackError, setCallbackError] = useState<string | null>(null);
  const [githubSyncConflict, setGithubSyncConflict] = useState<string | null>(
    null
  );
  const [githubSyncTick, setGithubSyncTick] = useState(0);

  useEffect(() => {
    if (!isSupabaseConfigured) {
      setLoading(false);
      return;
    }
    const supabase = getSupabase();
    let cancelled = false;

    async function consumeCallback(): Promise<void> {
      const parsed = parseAuthCallback(window.location.href);
      if (parsed.kind === "none") return;
      if (parsed.kind === "code") {
        const { error } = await supabase.auth.exchangeCodeForSession(
          parsed.code
        );
        if (!cancelled) {
          if (!error) {
            window.history.replaceState(
              null,
              "",
              cleanCallbackUrl(window.location.href)
            );
          } else {
            // A session may still have been established (e.g. double
            // consume); only surface the error when no session exists.
            const { data } = await supabase.auth.getSession();
            if (!data.session) {
              setCallbackError(callbackErrorMessage(error.message));
            }
            window.history.replaceState(
              null,
              "",
              cleanCallbackUrl(window.location.href)
            );
          }
        }
        return;
      }
      const { error } = await supabase.auth.verifyOtp({
        token_hash: parsed.tokenHash,
        type: parsed.type as
          | "signup"
          | "recovery"
          | "email"
          | "invite"
          | "magiclink"
          | "email_change",
      });
      if (cancelled) return;
      window.history.replaceState(
        null,
        "",
        cleanCallbackUrl(window.location.href)
      );
      if (error) {
        const { data } = await supabase.auth.getSession();
        if (!data.session) {
          setCallbackError(callbackErrorMessage(error.message));
        }
      } else if (parsed.type === "recovery") {
        setRecoveryEvent(true);
      }
    }

    async function init() {
      await consumeCallback();
      if (cancelled) return;
      const { data } = await supabase.auth.getSession();
      if (cancelled) return;
      setSession(data.session);
      setLoading(false);
    }
    void init();

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event, nextSession) => {
      if (cancelled) return;
      if (event === "PASSWORD_RECOVERY") setRecoveryEvent(true);
      if (event === "SIGNED_OUT") {
        setRecoveryEvent(false);
        setCallbackError(null);
        setGithubSyncConflict(null);
      }
      setSession(nextSession);
      setLoading(false);
    });
    return () => {
      cancelled = true;
      subscription.unsubscribe();
    };
  }, []);

  // After every GitHub-backed sign-in (signup, login, or link return),
  // store the session's verified GitHub identity. Same-account syncs are
  // idempotent; a 409 surfaces as a conflict for Settings to resolve.
  // Other failures stay silent: views show the last known profile state.
  useEffect(() => {
    if (!isSupabaseConfigured || !session) return;
    const providers = session.user.app_metadata.providers;
    const linked =
      (Array.isArray(providers) && providers.includes("github")) ||
      session.user.app_metadata.provider === "github";
    if (!linked) return;
    let cancelled = false;
    async function sync() {
      try {
        const response = await apiFetch("/api/profile/github", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: false }),
        });
        if (cancelled) return;
        if (response.status === 409) {
          const body = (await response
            .json()
            .catch(() => null)) as { detail?: unknown } | null;
          setGithubSyncConflict(
            typeof body?.detail === "string"
              ? body.detail
              : "A different GitHub account is already connected."
          );
        } else if (response.ok) {
          setGithubSyncConflict(null);
        }
      } catch {
        /* offline/backend down: keep last known state */
      } finally {
        if (!cancelled) setGithubSyncTick((tick) => tick + 1);
      }
    }
    void sync();
    return () => {
      cancelled = true;
    };
  }, [session]);

  async function signUp(
    email: string,
    password: string,
    username: string
  ): Promise<AuthResult> {
    if (!isSupabaseConfigured) return configError();
    const { data, error } = await getSupabase().auth.signUp({
      email,
      password,
      options: { data: { username: normalizeUsername(username) } },
    });
    if (error) return { error: error.message, needsConfirmation: false };
    return { error: null, needsConfirmation: data.session === null };
  }

  async function signIn(email: string, password: string): Promise<AuthResult> {
    if (!isSupabaseConfigured) return configError();
    const { error } = await getSupabase().auth.signInWithPassword({
      email,
      password,
    });
    if (error) return { error: error.message, needsConfirmation: false };
    return { error: null, needsConfirmation: false };
  }

  async function signInWithGitHub(): Promise<AuthResult> {
    if (!isSupabaseConfigured) return configError();
    // No scopes requested: GitHub sign-in is identity only, never repo access.
    const { error } = await getSupabase().auth.signInWithOAuth({
      provider: "github",
      options: { redirectTo: window.location.origin },
    });
    if (error) return { error: error.message, needsConfirmation: false };
    return { error: null, needsConfirmation: false };
  }

  async function linkGitHubIdentity(): Promise<SimpleResult> {
    if (!isSupabaseConfigured) return configSimpleError();
    // Attaches GitHub to the CURRENT user (no new account). No scopes:
    // identity only, never repo access.
    const { error } = await getSupabase().auth.linkIdentity({
      provider: "github",
    });
    if (error) return { error: error.message };
    return { error: null };
  }

  async function unlinkGitHubIdentity(): Promise<SimpleResult> {
    if (!isSupabaseConfigured) return configSimpleError();
    const identities = session?.user?.identities ?? [];
    const github = identities.find(
      (identity) => identity.provider === "github"
    );
    if (!github) {
      return { error: "No linked GitHub identity found on this session." };
    }
    const { error } = await getSupabase().auth.unlinkIdentity(github);
    if (error) return { error: error.message };
    return { error: null };
  }

  async function signOut(): Promise<void> {
    if (!isSupabaseConfigured) {
      setSession(null);
      return;
    }
    await getSupabase().auth.signOut();
    setRecoveryEvent(false);
    setCallbackError(null);
    setGithubSyncConflict(null);
    setSession(null);
  }

  async function resendConfirmation(email: string): Promise<SimpleResult> {
    if (!isSupabaseConfigured) return configSimpleError();
    const { error } = await getSupabase().auth.resend({
      type: "signup",
      email,
    });
    if (error) return { error: error.message };
    return { error: null };
  }

  async function requestPasswordReset(email: string): Promise<SimpleResult> {
    if (!isSupabaseConfigured) return configSimpleError();
    const { error } = await getSupabase().auth.resetPasswordForEmail(email, {
      redirectTo: window.location.origin,
    });
    if (error) return { error: error.message };
    return { error: null };
  }

  async function updatePassword(password: string): Promise<SimpleResult> {
    if (!isSupabaseConfigured) return configSimpleError();
    const { error } = await getSupabase().auth.updateUser({ password });
    if (error) return { error: error.message };
    setRecoveryEvent(false);
    return { error: null };
  }

  const recoveryPending =
    session !== null &&
    (recoveryEvent || accessTokenIndicatesRecovery(session.access_token));

  const state: AuthState = {
    configured: isSupabaseConfigured,
    session,
    user: session?.user ?? null,
    loading,
    recoveryPending,
    callbackError,
    clearCallbackError: () => setCallbackError(null),
    githubSyncConflict,
    clearGithubSyncConflict: () => setGithubSyncConflict(null),
    githubSyncTick,
    signUp,
    signIn,
    signInWithGitHub,
    linkGitHubIdentity,
    unlinkGitHubIdentity,
    signOut,
    resendConfirmation,
    requestPasswordReset,
    updatePassword,
  };
  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const state = useContext(AuthContext);
  if (!state) throw new Error("useAuth must be used inside <AuthProvider>.");
  return state;
}
