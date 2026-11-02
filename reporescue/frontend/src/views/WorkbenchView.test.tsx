import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { Session } from "@supabase/supabase-js";
import WorkbenchView from "./WorkbenchView";

const mockJobs = vi.hoisted(() => ({
  createJob: vi.fn(),
  getActiveJob: vi.fn(),
  getJob: vi.fn(),
  listJobs: vi.fn(),
  cancelJob: vi.fn(),
}));

vi.mock("../lib/jobs", () => mockJobs);

const mockApi = vi.hoisted(() => ({ apiJson: vi.fn() }));
vi.mock("../lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/api")>();
  return { ...original, apiJson: mockApi.apiJson };
});

const session = { access_token: "token" } as unknown as Session;

function detail(overrides: Record<string, unknown> = {}) {
  return {
    investigation_id: "i1",
    repo_url: "https://github.com/acme/calc",
    repo_name: "acme/calc",
    commit_sha: "b".repeat(40),
    ref: null,
    mode: "debug",
    user_message: null,
    status: "running",
    stage: "baseline",
    stages_completed: ["materialize"],
    progress_pct: 40,
    partial: null,
    verification_status: "unverified",
    error: null,
    limitations: [],
    final_report: null,
    timings: null,
    created_at: null,
    updated_at: null,
    completed_at: null,
    job: { job_id: "j1", investigation_id: "i1", status: "running", attempts: 1, max_attempts: 3, error: null, created_at: null, updated_at: null },
    attempts: [],
    runs: [],
    findings: [],
    patches: [],
    messages: [],
    ...overrides,
  };
}

function debugReport() {
  return {
    status: "complete",
    repository: "acme/calc",
    commit_sha: "b".repeat(40),
    mode: "real-repo-meta",
    summary: "Reproduced 1 failing test(s); the guarded patch passed.",
    verdict: "TESTS_PASSED",
    diagnosis: "add subtracts.",
    diagnosis_confidence: "confirmed",
    baseline: { backend: "docker", isolation: "x", status: "test_failure", passed: false, exit_code: 1, output: "FAILED", duration_ms: 1, failures: [{ test: "t", kind: "failed", message: "m", traceback: "tb" }], install_ok: true, install_log: "" },
    verification: { backend: "docker", isolation: "x", status: "ok", passed: true, exit_code: 0, output: "1 passed", duration_ms: 1, failures: [], install_ok: null, install_log: "" },
    patch: { edits: [{ file: "pkg/calc.py", before: "a", after: "b" }], reason: "Fix." },
    guard: { validated: true, message: "Guard approved 1 edit(s)." },
    execution: { backend: "docker", isolation: "x" },
    attempts: 1,
    limitations: [],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockJobs.listJobs.mockResolvedValue([]);
});

describe("WorkbenchView job restoration", () => {
  it("loads nothing without a restored job id", () => {
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    expect(mockJobs.getJob).not.toHaveBeenCalled();
    expect(mockJobs.createJob).not.toHaveBeenCalled();
  });

  it("restores the backend-owned job once without creating a duplicate", async () => {
    const onAdopted = vi.fn();
    mockJobs.getJob.mockResolvedValueOnce(detail());
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" onJobAdopted={onAdopted} />
    );
    expect(await screen.findByText("acme/calc")).toBeDefined();
    expect(screen.getByText(/RUNNING/)).toBeDefined();
    expect(mockJobs.getJob).toHaveBeenCalledTimes(1);
    expect(mockJobs.getJob).toHaveBeenCalledWith("j1");
    expect(mockJobs.createJob).not.toHaveBeenCalled();
    expect(onAdopted).toHaveBeenCalledTimes(1);
  });

  it("renders the persisted report when polling observes completion", async () => {
    mockJobs.getJob
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce(detail({ status: "completed", progress_pct: 100, final_report: debugReport() }));
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    expect(await screen.findByText("acme/calc")).toBeDefined();
    // One 3s poll later the completed report renders without a restart.
    expect(await screen.findByText("Test-backed fix", {}, { timeout: 6000 })).toBeDefined();
    expect(mockJobs.createJob).not.toHaveBeenCalled();
  }, 10000);

  it("starts a background investigation from an inspected repo", async () => {
    mockApi.apiJson.mockResolvedValueOnce({
      status: "complete",
      ingestion_id: "ing1",
      profile: {
        name: "acme/calc", commit_sha: "b".repeat(40), structure_summary: "Python project.",
        supported: true, primary_language: "python", dependency_format: "pyproject",
        test_framework: "pytest", tests: { count: 1 }, frameworks: [],
      },
      warnings: [],
    });
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j9", investigation_id: "i9", status: "queued" });
    mockJobs.getJob.mockResolvedValueOnce(detail({ job: { job_id: "j9", investigation_id: "i9", status: "queued", attempts: 0, max_attempts: 3, error: null, created_at: null, updated_at: null }, status: "queued", progress_pct: 0 }));

    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText("Start background investigation")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Problem statement"), {
      target: { value: "fix the bug" },
    });
    fireEvent.click(screen.getByText("Start background investigation"));

    expect(await screen.findByText(/QUEUED/)).toBeDefined();
    expect(mockJobs.createJob).toHaveBeenCalledTimes(1);
    expect(mockJobs.createJob).toHaveBeenCalledWith({
      ingestion_id: "ing1",
      mode: "debug",
      message: "fix the bug",
    });
  });

  it("cancels the followed job", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    mockJobs.cancelJob.mockResolvedValueOnce(detail({ status: "cancelled" }));
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    expect(await screen.findByText("acme/calc")).toBeDefined();
    fireEvent.click(screen.getByText("Cancel investigation"));
    expect(await screen.findByText("Investigation was cancelled.")).toBeDefined();
    expect(mockJobs.cancelJob).toHaveBeenCalledWith("j1");
  });
});
