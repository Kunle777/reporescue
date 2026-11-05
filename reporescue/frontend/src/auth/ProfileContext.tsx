import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { useAuth } from "./AuthContext";
import { ApiError } from "../lib/api";
import { getProfile, type Profile } from "../lib/profile";

type ProfileState = {
  /** Persisted backend profile, or null when none exists yet. */
  profile: Profile | null;
  /** Persisted username, or null when the user never set one. */
  username: string | null;
  loading: boolean;
  /** Re-read the profile from the backend (after a username save). */
  refresh: () => Promise<void>;
};

const ProfileContext = createContext<ProfileState | null>(null);

/**
 * Backend profile state for the signed-in user. The profile row is the
 * authoritative username source; views must read it here instead of
 * deriving a display name from the email address.
 */
export function ProfileProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!session) {
      setProfile(null);
      return;
    }
    setLoading(true);
    try {
      setProfile(await getProfile());
    } catch (error) {
      // 404 means no profile row yet (lazily created on first use).
      // Anything else keeps the last known state; views surface
      // their own errors and the app signs out on 401 elsewhere.
      if (error instanceof ApiError && error.status === 404) {
        setProfile(null);
      }
    } finally {
      setLoading(false);
    }
  }, [session]);

  useEffect(() => {
    if (!session) {
      setProfile(null);
      setLoading(false);
      return;
    }
    void refresh();
  }, [session, refresh]);

  const state: ProfileState = {
    profile,
    username: profile?.username ?? null,
    loading,
    refresh,
  };
  return (
    <ProfileContext.Provider value={state}>
      {children}
    </ProfileContext.Provider>
  );
}

export function useProfile(): ProfileState {
  const state = useContext(ProfileContext);
  if (!state) throw new Error("useProfile must be used inside <ProfileProvider>.");
  return state;
}
