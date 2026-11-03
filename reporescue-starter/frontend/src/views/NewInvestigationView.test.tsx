import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ApiError } from "../lib/api";
import NewInvestigationView from "./NewInvestigationView";
import type { IngestResult, Project } from "../lib/types";

const mockJobs = vi.hoisted(() => ({ createJob: vi.fn() }));
vi.mock("../lib/jobs", () => mockJobs);

const mockApi = vi.hoisted(() => ({ apiJson: vi.fn() }));
vi.mock("../lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/api")>();
  return { ...original, apiJson: mockApi.apiJson };
});

function project(overrides: Record<string, unknown> = {}): Project {
  return {
    id: "p1",
    display_name: "calc",
    github_owner: "acme",
    repository_name: "calc",
    github_url: "https://github.com/acme/calc",
    default_branch: "main",
    visibility: "public",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    ...overrides,
  } as Project;
}

function ingestPayload(): IngestResult {
  return {
    status: "complete",
    ingestion_id: "ing1",
    profile: {
      url: "https://github.com/acme/calc",
      name: "acme/calc",
      commit_sha: "a".repeat(40),
      ref: "main",
      primary_language: "python",
      supported: true,
      python_requires: ">=3.11",
      dependency_format: "pyproject",
      dependency_files: ["pyproject.toml"],
      frameworks: ["django"],
      test_framework: "pytest",
      tests: { dirs: ["tests"], files: ["tests/test_calc.py"], count: 12 },
      python_file_count: 48,
      source_dirs: ["pkg"],
      structure_summary: "Python project with 48 source files.",
      warnings: [],
    },
    warnings: [],
  };
}

function renderView(props: Record<string, unknown> = {}) {
  const handlers = {
    onJobCreated: vi.fn(),
    onNavigate: vi.fn(),
    onUnauthorized: vi.fn(),
    onIngestConsumed: vi.fn(),
  };
  render(
    <NewInvestigationView
      projects={[
        project(),
        project({
          id: "p2",
          display_name: "web",
          repository_name: "web",
          github_url: "https://github.com/acme/web",
          default_branch: "dev",
        }),
      ]}
      onJobCreated={handlers.onJobCreated}
      onNavigate={handlers.onNavigate}
      onUnauthorized={handlers.onUnauthorized}
      onIngestConsumed={handlers.onIngestConsumed}
      {...props}
    />
  );
  return handlers;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("NewInvestigationView modes", () => {
  it("defaults to General mode", () => {
    renderView({ projects: [] });
    expect(screen.getByRole("radio", { name: /General/ }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("radio", { name: /Debug/ }).getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("radio", { name: /Explain/ }).getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("radio", { name: /Health/ }).getAttribute("aria-checked")).toBe("false");
  });

  it.each([["General"], ["Debug"], ["Explain"], ["Health"]])("selects the %s mode", (label) => {
    renderView({ projects: [] });
    fireEvent.click(screen.getByRole("radio", { name: new RegExp(label) }));
    expect(
      screen.getByRole("radio", { name: new RegExp(label) }).getAttribute("aria-checked")
    ).toBe("true");
  });
});

describe("NewInvestigationView repository", () => {
  it.each([
    ["", "Enter a repository URL."],
    ["notaurl", "Enter a complete URL, e.g. https://github.com/owner/repo."],
    ["http://github.com/acme/calc", "Use an https:// GitHub URL."],
    ["https://gitlab.com/acme/calc", "Only public github.com repositories are supported."],
    ["https://github.com/acme", "Include the owner and repository, e.g. https://github.com/owner/repo."],
  ])("validates the repository URL before inspecting (%s)", async (value, message) => {
    renderView({ projects: [] });
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), { target: { value } });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText(message as string)).toBeDefined();
    expect(mockApi.apiJson).not.toHaveBeenCalled();
  });

  it("inspects a URL repository and shows its capabilities", async () => {
    mockApi.apiJson.mockResolvedValueOnce(ingestPayload());
    renderView({ projects: [] });
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.change(screen.getByLabelText("Branch, tag, or commit"), {
      target: { value: "main" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(mockApi.apiJson).toHaveBeenCalledWith("/api/repos/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: "https://github.com/acme/calc", ref: "main" }),
    });
    expect(await screen.findByText(/inspected ·/)).toBeDefined();
    expect(screen.getByText("acme/calc")).toBeDefined();
    expect(screen.getByText("runnable")).toBeDefined();
  });

  it("inspects a selected registered project", async () => {
    mockApi.apiJson.mockResolvedValueOnce(ingestPayload());
    renderView();
    fireEvent.click(screen.getByRole("radio", { name: /web/ }));
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(mockApi.apiJson).toHaveBeenCalledWith("/api/repos/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: "https://github.com/acme/web", ref: "dev" }),
    });
    expect(await screen.findByText(/inspected ·/)).toBeDefined();
  });

  it("adopts a carried-over inspection without re-inspecting", async () => {
    const handlers = renderView({ projects: [], initialIngest: ingestPayload() });
    expect(await screen.findByText(/inspected ·/)).toBeDefined();
    expect(screen.getByText("acme/calc")).toBeDefined();
    expect(mockApi.apiJson).not.toHaveBeenCalled();
    expect(handlers.onIngestConsumed).toHaveBeenCalledTimes(1);
  });

  it("clears a stale inspection when the choice changes", async () => {
    mockApi.apiJson.mockResolvedValueOnce(ingestPayload());
    renderView();
    fireEvent.click(screen.getByRole("radio", { name: /different repository URL/ }));
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText(/inspected ·/)).toBeDefined();
    fireEvent.click(screen.getByRole("radio", { name: /web/ }));
    expect(screen.queryByText(/inspected ·/)).toBeNull();
    expect(
      (screen.getByText("Start investigation") as HTMLButtonElement).disabled
    ).toBe(true);
  });

  it("clears a stale inspection when the URL is edited", async () => {
    mockApi.apiJson.mockResolvedValueOnce(ingestPayload());
    renderView({ projects: [] });
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText(/inspected ·/)).toBeDefined();
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/other" },
    });
    expect(screen.queryByText(/inspected ·/)).toBeNull();
  });

  it("shows a hint when no repositories are registered", () => {
    renderView({ projects: [] });
    expect(screen.getByText(/No registered repositories/)).toBeDefined();
    expect(screen.queryByRole("radiogroup", { name: "Repository choice" })).toBeNull();
  });

  it("surfaces ingest API errors and keeps submit disabled", async () => {
    mockApi.apiJson.mockResolvedValueOnce({ status: "error", message: "clone failed" });
    renderView({ projects: [] });
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText("Could not inspect repository")).toBeDefined();
    expect(screen.getByText("clone failed")).toBeDefined();
    expect(
      (screen.getByText("Start investigation") as HTMLButtonElement).disabled
    ).toBe(true);
  });

  it("maps a 401 on inspect to sign-out", async () => {
    mockApi.apiJson.mockRejectedValueOnce(new ApiError(401, "expired"));
    const handlers = renderView({ projects: [] });
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText(/Session expired/)).toBeDefined();
    expect(handlers.onUnauthorized).toHaveBeenCalledTimes(1);
  });
});

describe("NewInvestigationView submission", () => {
  async function inspectFirst() {
    mockApi.apiJson.mockResolvedValueOnce(ingestPayload());
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
    await screen.findByText(/inspected ·/);
  }

  it("disables Start until a repository is inspected", async () => {
    renderView({ projects: [] });
    expect((screen.getByText("Start investigation") as HTMLButtonElement).disabled).toBe(true);
    await inspectFirst();
    expect((screen.getByText("Start investigation") as HTMLButtonElement).disabled).toBe(false);
  });

  it("submits through the jobs API and hands the job to the workbench", async () => {
    const handlers = renderView({ projects: [] });
    await inspectFirst();
    fireEvent.click(screen.getByRole("radio", { name: /Explain/ }));
    fireEvent.change(screen.getByLabelText("Problem statement"), {
      target: { value: "Why is this slow?" },
    });
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j9", investigation_id: "i9", status: "queued" });
    fireEvent.click(screen.getByText("Start investigation"));
    await screen.findByText("Starting…");
    expect(mockJobs.createJob).toHaveBeenCalledWith({
      ingestion_id: "ing1",
      mode: "explain",
      message: "Why is this slow?",
    });
    expect(handlers.onJobCreated).toHaveBeenCalledWith("j9");
  });

  it("passes a blank message through for the client to normalize", async () => {
    renderView({ projects: [] });
    await inspectFirst();
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j9", investigation_id: "i9", status: "queued" });
    fireEvent.click(screen.getByText("Start investigation"));
    expect(mockJobs.createJob).toHaveBeenCalledWith({
      ingestion_id: "ing1",
      mode: "general",
      message: "",
    });
  });

  it("shows submit errors without transitioning", async () => {
    const handlers = renderView({ projects: [] });
    await inspectFirst();
    mockJobs.createJob.mockRejectedValueOnce(new Error("queue full"));
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText("Could not start investigation")).toBeDefined();
    expect(screen.getByText("queue full")).toBeDefined();
    expect(handlers.onJobCreated).not.toHaveBeenCalled();
  });

  it("maps a 401 on submit to sign-out", async () => {
    const handlers = renderView({ projects: [] });
    await inspectFirst();
    mockJobs.createJob.mockRejectedValueOnce(new ApiError(401, "expired"));
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText(/Session expired/)).toBeDefined();
    expect(handlers.onUnauthorized).toHaveBeenCalledTimes(1);
    expect(handlers.onJobCreated).not.toHaveBeenCalled();
  });

  it("navigates back to the dashboard", () => {
    const handlers = renderView({ projects: [] });
    fireEvent.click(screen.getByText("Back"));
    expect(handlers.onNavigate).toHaveBeenCalledWith("dashboard");
  });
});
