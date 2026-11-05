import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ProfileProvider, useProfile } from "./ProfileContext";

const mockProfileApi = vi.hoisted(() => ({ getProfile: vi.fn() }));

vi.mock("../lib/profile", () => mockProfileApi);

const mockAuth = vi.hoisted(() => ({ session: null as unknown }));

vi.mock("./AuthContext", () => ({
  useAuth: () => ({ session: mockAuth.session }),
}));

function Probe() {
  const { username, loading } = useProfile();
  return (
    <div>
      <span data-testid="username">{username ?? "none"}</span>
      <span data-testid="loading">{loading ? "yes" : "no"}</span>
    </div>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.session = null;
});

describe("ProfileProvider", () => {
  it("exposes the persisted username from the backend profile", async () => {
    mockAuth.session = { access_token: "token" };
    mockProfileApi.getProfile.mockResolvedValue({
      id: "u1",
      username: "ada_debugs",
    });
    render(
      <ProfileProvider>
        <Probe />
      </ProfileProvider>
    );
    await waitFor(() =>
      expect(screen.getByTestId("username").textContent).toBe("ada_debugs")
    );
    expect(mockProfileApi.getProfile).toHaveBeenCalledTimes(1);
  });

  it("reports no username when the profile has none", async () => {
    mockAuth.session = { access_token: "token" };
    mockProfileApi.getProfile.mockResolvedValue({ id: "u1", username: null });
    render(
      <ProfileProvider>
        <Probe />
      </ProfileProvider>
    );
    await waitFor(() =>
      expect(screen.getByTestId("username").textContent).toBe("none")
    );
  });

  it("fetches nothing without a session", () => {
    render(
      <ProfileProvider>
        <Probe />
      </ProfileProvider>
    );
    expect(mockProfileApi.getProfile).not.toHaveBeenCalled();
    expect(screen.getByTestId("username").textContent).toBe("none");
  });
});
