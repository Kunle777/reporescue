import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import type { Session, User } from "@supabase/supabase-js";
import { AuthProvider, useAuth } from "./AuthContext";

const mockClient = vi.hoisted(() => ({
  auth: {
    getSession: vi.fn(),
    onAuthStateChange: vi.fn(),
    linkIdentity: vi.fn(),
    unlinkIdentity: vi.fn(),
  },
}));

vi.mock("../lib/supabase", () => ({
  isSupabaseConfigured: true,
  getSupabase: () => mockClient,
}));

type AuthSnapshot = ReturnType<typeof useAuth>;

let snapshot: AuthSnapshot | null = null;
let authListener: ((event: string, session: Session | null) => void) | null =
  null;

function Probe() {
  snapshot = useAuth();
  return (
    <div>
      <span data-testid="conflict">{snapshot.githubSyncConflict ?? "none"}</span>
      <span data-testid="tick">{snapshot.githubSyncTick}</span>
    </div>
  );
}

function githubSession(): Session {
  return {
    access_token: "token",
    user: {
      id: "user-1",
      app_metadata: { provider: "github", providers: ["github"] },
      user_metadata: { user_name: "Kunle777" },
      identities: [{ provider: "github" }],
    } as unknown as User,
  } as unknown as Session;
}

function emailSession(): Session {
  return {
    access_token: "token",
    user: {
      id: "user-1",
      app_metadata: { provider: "email", providers: ["email"] },
      user_metadata: {},
      identities: [{ provider: "email" }],
    } as unknown as User,
  } as unknown as Session;
}

function emit(event: string, session: Session | null) {
  act(() => {
    authListener?.(event, session);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  snapshot = null;
  authListener = null;
  mockClient.auth.getSession.mockResolvedValue({ data: { session: null } });
  mockClient.auth.onAuthStateChange.mockImplementation(
    (callback: (event: string, session: Session | null) => void) => {
      authListener = callback;
      return { data: { subscription: { unsubscribe: vi.fn() } } };
    }
  );
});

async function renderProvider() {
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>
  );
  await screen.findByTestId("tick");
}

describe("AuthContext GitHub", () => {
  it("links GitHub to the current user without scopes", async () => {
    mockClient.auth.linkIdentity.mockResolvedValue({ error: null });
    await renderProvider();

    let result;
    await act(async () => {
      result = await snapshot?.linkGitHubIdentity();
    });
    expect(mockClient.auth.linkIdentity).toHaveBeenCalledWith({
      provider: "github",
    });
    expect(result).toEqual({ error: null });
  });

  it("reports link failures", async () => {
    mockClient.auth.linkIdentity.mockResolvedValue({
      error: { message: "provider disabled" },
    });
    await renderProvider();

    let result;
    await act(async () => {
      result = await snapshot?.linkGitHubIdentity();
    });
    expect(result).toEqual({ error: "provider disabled" });
  });

  it("auto-syncs after a GitHub sign-in", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await renderProvider();
    emit("SIGNED_IN", githubSession());

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/profile/github");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ confirm: false }));
    expect(screen.getByTestId("conflict").textContent).toBe("none");
    await waitFor(() => {
      expect(screen.getByTestId("tick").textContent).toBe("1");
    });
  });

  it("surfaces a 409 as a replace conflict", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "@old is already connected." }), {
        status: 409,
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    await renderProvider();
    emit("SIGNED_IN", githubSession());

    expect(await screen.findByText("@old is already connected.")).toBeDefined();
    await waitFor(() => {
      expect(screen.getByTestId("tick").textContent).toBe("1");
    });
  });

  it("skips auto-sync for email-only sessions", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await renderProvider();
    emit("SIGNED_IN", emailSession());

    await waitFor(() => {
      expect(screen.getByTestId("tick").textContent).toBe("0");
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stays silent when the backend is unreachable", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("down"));
    vi.stubGlobal("fetch", fetchMock);
    await renderProvider();
    emit("SIGNED_IN", githubSession());

    await waitFor(() => {
      expect(screen.getByTestId("tick").textContent).toBe("1");
    });
    expect(screen.getByTestId("conflict").textContent).toBe("none");
  });

  it("unlinks the GitHub identity", async () => {
    mockClient.auth.unlinkIdentity.mockResolvedValue({ error: null });
    await renderProvider();
    emit("SIGNED_IN", githubSession());

    let result;
    await act(async () => {
      result = await snapshot?.unlinkGitHubIdentity();
    });
    expect(mockClient.auth.unlinkIdentity).toHaveBeenCalledWith({
      provider: "github",
    });
    expect(result).toEqual({ error: null });
  });

  it("reports unlink when no GitHub identity is linked", async () => {
    await renderProvider();
    emit("SIGNED_IN", emailSession());

    let result: { error: string | null } | undefined;
    await act(async () => {
      result = await snapshot?.unlinkGitHubIdentity();
    });
    expect(result?.error).toContain("No linked GitHub identity");
    expect(mockClient.auth.unlinkIdentity).not.toHaveBeenCalled();
  });
});
