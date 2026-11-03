import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApiError } from "../lib/api";
import ProjectsView from "./ProjectsView";
import type { IngestResult, Project } from "../lib/types";

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
      url: "https://github.com/acme/shop",
      name: "acme/shop",
      commit_sha: "c".repeat(40),
      ref: "main",
      primary_language: "python",
      supported: true,
      python_requires: ">=3.11",
      dependency_format: "pyproject",
      dependency_files: ["pyproject.toml"],
      frameworks: [],
      test_framework: "pytest",
      tests: { dirs: ["tests"], files: [], count: 3 },
      python_file_count: 20,
      source_dirs: ["pkg"],
      structure_summary: "Python project with 20 source files.",
      warnings: [],
    },
    warnings: [],
  };
}

function renderView(props: Record<string, unknown> = {}) {
  const handlers = {
    onRetry: vi.fn(),
    onChanged: vi.fn(),
    onStartInvestigation: vi.fn(),
    onUnauthorized: vi.fn(),
  };
  render(
    <ProjectsView
      projects={[project()]}
      loading={false}
      error=""
      onRetry={handlers.onRetry}
      onChanged={handlers.onChanged}
      onStartInvestigation={handlers.onStartInvestigation}
      onUnauthorized={handlers.onUnauthorized}
      {...props}
    />
  );
  return handlers;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ProjectsView repository inspection", () => {
  it("requires a URL before inspecting", () => {
    renderView();
    fireEvent.click(screen.getByText("Inspect"));
    expect(screen.getByText("Enter a repository URL first.")).toBeDefined();
    expect(mockApi.apiJson).not.toHaveBeenCalled();
  });

  it("inspects a repository and hands the result to the new flow", async () => {
    mockApi.apiJson.mockResolvedValueOnce(ingestPayload());
    const handlers = renderView();
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/shop" },
    });
    fireEvent.change(screen.getByLabelText("Branch, tag, or commit"), {
      target: { value: "main" },
    });
    fireEvent.click(screen.getByText("Inspect"));
    expect(mockApi.apiJson).toHaveBeenCalledWith("/api/repos/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: "https://github.com/acme/shop", ref: "main" }),
    });
    expect(await screen.findByText("acme/shop")).toBeDefined();
    fireEvent.click(screen.getByText("Start investigation →"));
    expect(handlers.onStartInvestigation).toHaveBeenCalledWith(ingestPayload());
  });

  it("surfaces inspect errors", async () => {
    mockApi.apiJson.mockRejectedValueOnce(new Error("clone failed"));
    renderView();
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/shop" },
    });
    fireEvent.click(screen.getByText("Inspect"));
    expect(await screen.findByText("Could not inspect repository")).toBeDefined();
    expect(screen.getByText("clone failed")).toBeDefined();
    expect(screen.queryByText("Start investigation →")).toBeNull();
  });

  it("maps a 401 on inspect to sign-out", async () => {
    mockApi.apiJson.mockRejectedValueOnce(new ApiError(401, "expired"));
    const handlers = renderView();
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/shop" },
    });
    fireEvent.click(screen.getByText("Inspect"));
    await waitFor(() => expect(handlers.onUnauthorized).toHaveBeenCalledTimes(1));
  });
});

describe("ProjectsView registered list", () => {
  it("renders loading, empty, and error states with retry", () => {
    const { rerender } = render(
      <ProjectsView
        projects={[]}
        loading={true}
        error=""
        onRetry={() => {}}
        onChanged={() => {}}
        onStartInvestigation={() => {}}
        onUnauthorized={() => {}}
      />
    );
    expect(screen.getByText("Loading repositories…")).toBeDefined();

    rerender(
      <ProjectsView
        projects={[]}
        loading={false}
        error=""
        onRetry={() => {}}
        onChanged={() => {}}
        onStartInvestigation={() => {}}
        onUnauthorized={() => {}}
      />
    );
    expect(screen.getByText("Nothing registered yet")).toBeDefined();

    const onRetry = vi.fn();
    rerender(
      <ProjectsView
        projects={[]}
        loading={false}
        error="boom"
        onRetry={onRetry}
        onChanged={() => {}}
        onStartInvestigation={() => {}}
        onUnauthorized={() => {}}
      />
    );
    expect(screen.getByText("Could not load repositories")).toBeDefined();
    fireEvent.click(screen.getByText("Retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("lists registered repositories with a count", () => {
    renderView({ projects: [project(), project({ id: "p2", display_name: "web" })] });
    expect(screen.getByText("calc")).toBeDefined();
    expect(screen.getByText("web")).toBeDefined();
    expect(screen.getByText("2")).toBeDefined();
  });
});
