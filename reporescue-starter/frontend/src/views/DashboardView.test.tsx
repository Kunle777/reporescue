import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { Session } from "@supabase/supabase-js";
import DashboardView from "./DashboardView";
import type { JobCard, Project } from "../lib/types";

const mockJobs = vi.hoisted(() => ({ listJobs: vi.fn() }));
vi.mock("../lib/jobs", () => mockJobs);

const session = {
  access_token: "token",
  user: { email: "sam@example.com" },
} as unknown as Session;

function card(overrides: Record<string, unknown> = {}): JobCard {
  return {
    job_id: "j1",
    investigation_id: "i1",
    status: "running",
    attempts: 1,
    max_attempts: 3,
    error: null,
    created_at: null,
    updated_at: null,
    repo_name: "acme/calc",
    mode: "debug",
    progress_pct: 40,
    ...overrides,
  } as JobCard;
}

function project(overrides: Record<string, unknown> = {}): Project {
  return {
    id: "p1",
    display_name: "calc",
    github_url: "https://github.com/acme/calc",
    ...overrides,
  } as Project;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("DashboardView", () => {
  it("renders counts, continue card, and recent jobs", async () => {
    mockJobs.listJobs.mockResolvedValue([
      card({}),
      card({ job_id: "j2", status: "completed", progress_pct: 100 }),
    ]);
    const onResume = vi.fn();
    render(
      <DashboardView
        session={session}
        projects={[project({}), project({ id: "p2", display_name: "web" })]}
        onNavigate={() => {}}
        onResume={onResume}
        onUnauthorized={() => {}}
      />
    );
    await screen.findByText("Continue investigating");
    fireEvent.click(screen.getByText("Resume"));
    expect(onResume).toHaveBeenCalledWith("j1");
  });

  it("shows empty states without jobs or projects", async () => {
    mockJobs.listJobs.mockResolvedValue([]);
    render(
      <DashboardView
        session={session}
        projects={[]}
        onNavigate={() => {}}
        onResume={() => {}}
        onUnauthorized={() => {}}
      />
    );
    await screen.findByText("No investigations yet");
    screen.getByText("No repositories registered");
  });

  it("navigates to the new investigation flow and workbench history", async () => {
    mockJobs.listJobs.mockResolvedValue([]);
    const onNavigate = vi.fn();
    render(
      <DashboardView
        session={session}
        projects={[]}
        onNavigate={onNavigate}
        onResume={() => {}}
        onUnauthorized={() => {}}
      />
    );
    await screen.findByText("No investigations yet");
    fireEvent.click(screen.getByText("New investigation"));
    expect(onNavigate).toHaveBeenCalledWith("new-investigation");
    fireEvent.click(screen.getByText("View history"));
    expect(onNavigate).toHaveBeenCalledWith("workbench");
    fireEvent.click(screen.getByText("View all"));
    expect(onNavigate).toHaveBeenCalledWith("projects");
  });
});
