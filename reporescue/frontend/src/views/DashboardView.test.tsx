import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import DashboardView from "./DashboardView";

const mockJobs = vi.hoisted(() => ({ listJobs: vi.fn() }));

vi.mock("../lib/jobs", () => mockJobs);

function renderDashboard(username: string | null) {
  return render(
    <DashboardView
      username={username}
      projects={[]}
      loading={false}
      error=""
      onRetry={() => {}}
      onGoProjects={() => {}}
      onGoWorkbench={() => {}}
      onChanged={() => {}}
      onUnauthorized={() => {}}
      onOpenInvestigation={() => {}}
    />
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockJobs.listJobs.mockResolvedValue([]);
});

describe("DashboardView greeting", () => {
  it("greets with the persisted username", async () => {
    renderDashboard("ada_debugs");
    expect(await screen.findByText("Welcome back, ada_debugs")).toBeDefined();
  });

  it("falls back to a neutral greeting without inventing a name", async () => {
    renderDashboard(null);
    expect(await screen.findByText("Welcome back, there")).toBeDefined();
  });
});
