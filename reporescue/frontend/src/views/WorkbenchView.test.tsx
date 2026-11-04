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
    expect(screen.getAllByText(/RUNNING/).length).toBeGreaterThan(0);
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
    expect(await screen.findByText("Reproduced 1 failing test(s); the guarded patch passed.", {}, { timeout: 6000 })).toBeDefined();
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
    expect(await screen.findByText("What should RepoRescue do?")).toBeDefined();
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), {
      target: { value: "fix the bug" },
    });
    fireEvent.click(screen.getByText("Start investigation"));

    expect((await screen.findAllByText(/QUEUED/)).length).toBeGreaterThan(0);
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
    expect((await screen.findAllByText("CANCELLED")).length).toBeGreaterThan(0);
    expect(mockJobs.cancelJob).toHaveBeenCalledWith("j1");
  });

  it("keeps General selected by default and reports the canonical API limitation without submitting", async () => {
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    mockApi.apiJson.mockResolvedValueOnce({ status: "complete", ingestion_id: "ing1", profile: { name: "acme/calc", commit_sha: "b".repeat(40), supported: true, tests: { count: 0 }, warnings: [] }, warnings: [] });
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), { target: { value: "https://github.com/acme/calc" } });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText("What should RepoRescue do?")).toBeDefined();
    expect(screen.getByRole("radio", { name: /General/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText(/current \/api\/jobs contract rejects it/)).toBeDefined();
    expect(mockJobs.createJob).not.toHaveBeenCalled();
  });

  it("renders persisted conversation history read-only", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({ messages: [{ role: "user", content: "Where is the parser?" }, { role: "assistant", content: "The API persisted this response." }] }));
    render(<WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />);
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Conversation" }));
    expect(screen.getByText("Where is the parser?")).toBeDefined();
    expect(screen.getByText("The API persisted this response.")).toBeDefined();
    expect(screen.getByText(/Sending follow-up messages is not available/)).toBeDefined();
  });

  it("renders backend findings, execution evidence, and proposed patches without conflating verification", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({
      findings: [{ kind: "hypothesis", category: "logic", title: "Possible boundary error", detail: "Review the upper bound.", confidence: "medium", evidence: { file: "src/range.py", line: 12 } }],
      runs: [{ kind: "baseline", backend: "modal", isolation: "cloud sandbox", status: "test_failure", passed: false, exit_code: 1, output: "FAILED test_range.py", failures: [{ test: "test_range", kind: "assertion", message: "Expected 3", traceback: "trace" }], duration_ms: 450, install_ok: true, install_log: "dependencies ready" }],
      patches: [{ status: "proposed", reason: "Adjust the boundary.", files: [{ file: "src/range.py", before: "range(3)", after: "range(4)" }] }],
      verification_status: "unverified",
    }));
    render(<WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />);
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: /Findings/ }));
    expect(screen.getByText("Possible boundary error")).toBeDefined();
    expect(screen.getByText("hypothesis")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Execution" }));
    expect(screen.getByText("modal")).toBeDefined();
    expect(screen.getByText("tests failed")).toBeDefined();
    expect(screen.getByText("FAILED test_range.py")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(screen.getByText("proposed")).toBeDefined();
    expect(screen.getByText(/this patch has not been confirmed as a fix/)).toBeDefined();
  });
});
