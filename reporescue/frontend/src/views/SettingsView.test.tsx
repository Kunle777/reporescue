import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { User } from "@supabase/supabase-js";
import SettingsView from "./SettingsView";
import { ThemeProvider } from "../theme/ThemeContext";
import { ApiError } from "../lib/api";

const mockProfileApi = vi.hoisted(() => ({
  getProfile: vi.fn(),
  updateUsername: vi.fn(),
  syncGithubIdentity: vi.fn(),
  disconnectGithubIdentity: vi.fn(),
}));

vi.mock("../lib/profile", () => mockProfileApi);

const mockAuth = vi.hoisted(() => ({
  linkGitHubIdentity: vi.fn(),
  unlinkGitHubIdentity: vi.fn(),
  githubSyncConflict: null as string | null,
  clearGithubSyncConflict: vi.fn(),
  githubSyncTick: 0,
}));

vi.mock("../auth/AuthContext", () => ({ useAuth: () => mockAuth }));

function userWith(identities: string[]): User {
  return {
    id: "user-1",
    email: "ada@example.com",
    app_metadata: { provider: identities[0] ?? "email" },
    identities: identities.map((provider) => ({ provider })),
  } as unknown as User;
}

function profile(overrides: Record<string, unknown> = {}) {
  return {
    id: "user-1",
    username: null,
    display_name: null,
    avatar_url: null,
    github_username: null,
    github_user_id: null,
    github_avatar_url: null,
    github_display_name: null,
    github_connected_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const GITHUB = {
  github_username: "Kunle777",
  github_user_id: "12345678",
  github_avatar_url: "https://avatars.example/u/12345678",
  github_display_name: "Abdulrazaq Olanipekun",
  github_connected_at: "2026-01-02T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.githubSyncConflict = null;
  mockAuth.githubSyncTick = 0;
});

describe("SettingsView username", () => {
  it("shows the current username and saves a change", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile({ username: "ada_debugs" }));
    mockProfileApi.updateUsername.mockResolvedValue(profile({ username: "ada_builds" }));
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    const input = (await screen.findByLabelText(
      "Your username"
    )) as HTMLInputElement;
    expect(input.value).toBe("ada_debugs");
    fireEvent.change(input, { target: { value: "ada_builds" } });
    fireEvent.click(screen.getByRole("button", { name: "Save username" }));

    expect(mockProfileApi.updateUsername).toHaveBeenCalledWith("ada_builds");
    expect(await screen.findByText("Saved")).toBeDefined();
  });

  it("reports a taken username", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile());
    mockProfileApi.updateUsername.mockRejectedValue(
      new ApiError(409, "Username is already taken.")
    );
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    const input = await screen.findByLabelText("Pick a username");
    fireEvent.change(input, { target: { value: "taken_name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save username" }));
    expect(
      await screen.findByText("That username is taken. Pick another one.")
    ).toBeDefined();
  });

  it("validates before calling the API", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile());
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    const input = await screen.findByLabelText("Pick a username");
    fireEvent.change(input, { target: { value: "ab" } });
    fireEvent.click(screen.getByRole("button", { name: "Save username" }));
    expect(
      await screen.findByText("Username must be at least 3 characters.")
    ).toBeDefined();
    expect(mockProfileApi.updateUsername).not.toHaveBeenCalled();
  });
});

describe("SettingsView GitHub", () => {
  it("shows the verified identity when connected", async () => {
    mockProfileApi.getProfile.mockResolvedValue(
      profile({ username: "ada_debugs", ...GITHUB })
    );
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email", "github"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    // Rendered twice: Account row plus the GitHub card.
    expect(await screen.findAllByText("@Kunle777")).toHaveLength(2);
    expect(screen.getByText(/Abdulrazaq Olanipekun/)).toBeDefined();
    // RepoRescue username untouched by the GitHub identity.
    expect((screen.getByLabelText("Your username") as HTMLInputElement).value).toBe(
      "ada_debugs"
    );
    expect(
      screen.getByRole("button", { name: "Disconnect GitHub" })
    ).toBeDefined();
  });

  it("connects from the disconnected state", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile());
    mockAuth.linkGitHubIdentity.mockResolvedValue({ error: null });
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub" })
    );
    expect(mockAuth.linkGitHubIdentity).toHaveBeenCalled();
  });

  it("reports connect failures", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile());
    mockAuth.linkGitHubIdentity.mockResolvedValue({
      error: "OAuth provider is not enabled",
    });
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub" })
    );
    expect(
      await screen.findByText("OAuth provider is not enabled")
    ).toBeDefined();
  });

  it("disconnects and keeps the account", async () => {
    mockProfileApi.getProfile.mockResolvedValue(
      profile({ username: "ada_debugs", ...GITHUB })
    );
    mockProfileApi.disconnectGithubIdentity.mockResolvedValue(
      profile({ username: "ada_debugs" })
    );
    mockAuth.unlinkGitHubIdentity.mockResolvedValue({ error: null });
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email", "github"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Disconnect GitHub" })
    );
    expect(
      await screen.findByText(/Your account and projects are untouched/)
    ).toBeDefined();
    expect(mockProfileApi.disconnectGithubIdentity).toHaveBeenCalled();
    expect(mockAuth.unlinkGitHubIdentity).toHaveBeenCalled();
    expect(screen.getByText("Not connected")).toBeDefined();
  });

  it("hides disconnect for GitHub-only sign-in", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile({ ...GITHUB }));
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["github"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    expect(await screen.findAllByText("@Kunle777")).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: "Disconnect GitHub" })
    ).toBeNull();
    expect(screen.getByText(/only sign-in method/)).toBeDefined();
  });

  it("offers confirmed replacement on conflict", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile({ ...GITHUB }));
    mockProfileApi.syncGithubIdentity.mockResolvedValue(
      profile({ ...GITHUB, github_username: "someone", github_user_id: "999" })
    );
    mockAuth.githubSyncConflict =
      "@Kunle777 is already connected to this profile.";
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email", "github"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );

    expect(
      await screen.findByText("Different GitHub account detected")
    ).toBeDefined();
    fireEvent.click(
      screen.getByRole("button", { name: "Replace with the signed-in account" })
    );
    expect(mockProfileApi.syncGithubIdentity).toHaveBeenCalledWith(true);
    expect(await screen.findByText("GitHub account replaced.")).toBeDefined();
    expect(mockAuth.clearGithubSyncConflict).toHaveBeenCalled();
  });
});

describe("SettingsView theme", () => {
  it("switches the persisted theme from settings", async () => {
    window.localStorage.clear();
    mockProfileApi.getProfile.mockResolvedValue(profile());
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email"])}
          session={null}
          onSignOut={() => {}}
        />
      </ThemeProvider>
    );
    fireEvent.click(await screen.findByRole("radio", { name: /Dark/ }));
    expect(window.localStorage.getItem("reporescue-theme")).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("notifies the app after a username save", async () => {
    mockProfileApi.getProfile.mockResolvedValue(profile({ username: "ada_debugs" }));
    mockProfileApi.updateUsername.mockResolvedValue(profile({ username: "ada_builds" }));
    const onProfileChanged = vi.fn();
    render(
      <ThemeProvider>
        <SettingsView
          user={userWith(["email"])}
          session={null}
          onSignOut={() => {}}
          onProfileChanged={onProfileChanged}
        />
      </ThemeProvider>
    );
    const input = (await screen.findByLabelText(
      "Your username"
    )) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "ada_builds" } });
    fireEvent.click(screen.getByRole("button", { name: "Save username" }));
    expect(await screen.findByText("Saved")).toBeDefined();
    expect(onProfileChanged).toHaveBeenCalledTimes(1);
  });
});
