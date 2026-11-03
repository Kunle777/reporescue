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

const mockConversation = vi.hoisted(() => ({ getMessages: vi.fn(), postMessage: vi.fn() }));
vi.mock("../lib/conversation", () => mockConversation);

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
    expect((await screen.findAllByText("acme/calc")).length).toBeGreaterThanOrEqual(1);
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
    expect((await screen.findAllByText("acme/calc")).length).toBeGreaterThanOrEqual(1);
    // One 3s poll later the completed report renders without a restart.
    expect(await screen.findByText("Test-backed fix", {}, { timeout: 6000 })).toBeDefined();
    expect(mockJobs.createJob).not.toHaveBeenCalled();
  }, 10000);

  it("separates deterministic Health observations from its AI summary", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({
      mode: "health",
      status: "completed",
      progress_pct: 100,
      final_report: {
        summary: "The repository has a small test surface.",
        overall: "needs_attention",
        checks: [{ name: "test coverage", status: "warn", detail: "No test script was detected." }],
        limitations: ["Static-only: dependencies were not installed and code was not executed."],
      },
    }));
    render(<WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />);
    expect(await screen.findByText(/Health summary · AI interpretation/)).toBeDefined();
    expect(screen.getByText("Deterministic observations")).toBeDefined();
    expect(screen.getByText(/test coverage/)).toBeDefined();
    expect(screen.getByText(/Static-only: dependencies were not installed/)).toBeDefined();
  });

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
      mode: "general",
      message: "fix the bug",
    });
  });

  it("shows the General routing explanation from the persisted report", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        status: "completed",
        progress_pct: 100,
        final_report: {
          ...debugReport(),
          routing: {
            requested_mode: "general",
            intent: "debug",
            confidence: "high",
            reasons: ["bug report"],
            mode: "debug",
            plan_id: "debug",
            conservative: false,
            explanation: "Interpreted as a bug report; running Debug.",
          },
        },
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    expect(await screen.findByText(/General routing/)).toBeDefined();
    expect(screen.getByText(/Interpreted as a bug report/)).toBeDefined();
  });

  it("cancels the followed job", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    mockJobs.cancelJob.mockResolvedValueOnce(detail({ status: "cancelled" }));
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    expect((await screen.findAllByText("acme/calc")).length).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByText("Cancel investigation"));
    expect(await screen.findByText("Investigation was cancelled.")).toBeDefined();
    expect(mockJobs.cancelJob).toHaveBeenCalledWith("j1");
  });
});

describe("WorkbenchView tabs", () => {
  it("shows the Overview tab by default with status, mode, repository and progress", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({ user_message: "fix it" }));
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tablist", { name: "Investigation sections" });
    expect(screen.getByRole("tab", { name: "Overview" }).getAttribute("aria-selected")).toBe(
      "true"
    );
    expect((await screen.findAllByText("acme/calc")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("https://github.com/acme/calc")).toBeDefined();
    expect(screen.getByText("debug")).toBeDefined();
    expect(screen.getByText("running · 40%")).toBeDefined();
    expect(screen.getByText(/Investigating:/)).toBeDefined();
    expect(screen.getByText("So far")).toBeDefined();
    expect(screen.getByText("None recorded yet")).toBeDefined();
  });

  it("switches between tabs with buttons and arrow keys", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Overview" });
    fireEvent.click(screen.getByRole("tab", { name: "Findings" }));
    expect(screen.getByRole("tab", { name: "Findings" }).getAttribute("aria-selected")).toBe(
      "true"
    );
    expect(screen.getByText("No findings yet")).toBeDefined();
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Evidence" }).getAttribute("aria-selected")).toBe(
      "true"
    );
    expect(screen.getByText("No evidence recorded yet")).toBeDefined();
  });

  it("renders findings from the persisted detail", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        status: "completed",
        progress_pct: 100,
        findings: [
          {
            kind: "fact",
            category: "dependencies",
            title: "Pinned dependencies",
            detail: "24 resolved.",
            confidence: "confirmed",
          },
        ],
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Findings" });
    fireEvent.click(screen.getByRole("tab", { name: "Findings" }));
    expect(await screen.findByText("Pinned dependencies")).toBeDefined();
    expect(screen.getByText("24 resolved.")).toBeDefined();
    expect(screen.getByText("dependencies")).toBeDefined();
    expect(screen.getByText("fact").className).toContain("pill green");
  });

  it("renders evidence from run failures with tracebacks", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        runs: [
          {
            kind: "baseline",
            backend: "docker",
            status: "test_failure",
            passed: false,
            exit_code: 1,
            output: "FAILED",
            failures: [
              {
                test: "tests/test_calc.py::test_add",
                kind: "failed",
                message: "assert 1 == 2",
                traceback: "line 1\nline 2",
              },
            ],
            duration_ms: 41,
          },
        ],
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Evidence" });
    fireEvent.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(await screen.findByText("tests/test_calc.py::test_add")).toBeDefined();
    expect(screen.getByText("assert 1 == 2")).toBeDefined();
    expect(screen.getByText(/line 1/)).toBeDefined();
  });

  it("distinguishes run outcomes without calling unavailable runs failed", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        runs: [
          {
            kind: "baseline",
            backend: "docker",
            status: "test_failure",
            passed: false,
            exit_code: 1,
            output: "FAILED",
            failures: [],
            duration_ms: 41,
          },
          {
            kind: "verification",
            backend: "docker",
            status: "ok",
            passed: true,
            exit_code: 0,
            output: "ok",
            failures: [],
            duration_ms: 38,
          },
          {
            kind: "install",
            backend: null,
            status: "not_run",
            passed: null,
            exit_code: null,
            output: null,
            failures: [],
            duration_ms: null,
          },
          {
            kind: "lint",
            backend: null,
            status: "not_available",
            passed: null,
            exit_code: null,
            output: null,
            failures: [],
            duration_ms: null,
          },
          { kind: "timeout", backend: "docker", status: "timeout", passed: false, exit_code: null, output: "tail", output_truncated: true, failures: [], duration_ms: 100 },
          { kind: "cancel", backend: "docker", status: "cancelled", passed: false, exit_code: null, output: null, failures: [], duration_ms: 0 },
          { kind: "execution-error", backend: "docker", status: "error", passed: null, exit_code: null, output: null, failures: [], duration_ms: 0 },
          { kind: "setup-error", backend: "docker", status: "install_error", passed: null, exit_code: 2, output: null, failures: [], duration_ms: 0 },
          { kind: "unavailable", backend: "none", status: "unavailable", passed: null, exit_code: null, output: null, failures: [], duration_ms: 0 },
          { kind: "unsupported", backend: "none", status: "unsupported", passed: null, exit_code: null, output: null, failures: [], duration_ms: 0 },
          { kind: "static", backend: "static-only", status: "static_only", passed: null, exit_code: null, output: null, failures: [], duration_ms: 0 },
        ],
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Execution" });
    fireEvent.click(screen.getByRole("tab", { name: "Execution" }));
    expect(await screen.findByText("baseline")).toBeDefined();
    expect(screen.getByText("test failure").className).toContain("pill red");
    expect(screen.getByText("passed").className).toContain("pill green");
    expect(screen.getByText("not run").className).toBe("pill");
    expect(screen.getAllByText("not available")).toHaveLength(3);
    expect(screen.getAllByText("not available").every((node) => node.className === "pill")).toBe(true);
    expect(screen.getByText("timed out").className).toContain("pill amber");
    expect(screen.getByText("cancelled").className).toContain("pill amber");
    expect(screen.getByText("execution error").className).toContain("pill amber");
    expect(screen.getByText("setup failed").className).toContain("pill amber");
    expect(screen.getByText("unverified · static-only").className).toBe("pill");
    expect(screen.getByText(/Output was truncated while it was collected/)).toBeDefined();
  });

  it("renders attempts, timings and output toggles", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        attempts: [
          {
            attempt_no: 1,
            diagnosis: "off by one",
            confidence: "hypothesis",
            patch: null,
            guard: null,
            verification: null,
          },
        ],
        timings: { baseline_ms: 41000 },
        runs: [
          {
            kind: "baseline",
            backend: "docker",
            status: "test_failure",
            passed: false,
            exit_code: 1,
            output: "OUTPUT-HERE",
            failures: [],
            duration_ms: 41,
          },
        ],
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Execution" });
    fireEvent.click(screen.getByRole("tab", { name: "Execution" }));
    expect(await screen.findByText("Attempt 1")).toBeDefined();
    expect(screen.getByText("off by one")).toBeDefined();
    expect(screen.getByText("confidence: hypothesis")).toBeDefined();
    expect(screen.getByText("baseline_ms")).toBeDefined();
    expect(screen.getByText("41000")).toBeDefined();
    fireEvent.click(screen.getByText("Show output"));
    expect(screen.getByText("OUTPUT-HERE")).toBeDefined();
    fireEvent.click(screen.getByText("Hide output"));
    expect(screen.queryByText("OUTPUT-HERE")).toBeNull();
  });

  it("renders changes with patch validation and verification status", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        status: "completed",
        progress_pct: 100,
        verification_status: "verified",
        patches: [
          {
            status: "proposed",
            reason: "Fix it.",
            files: [{ file: "pkg/calc.py", before: "BEFORE-LINE", after: "AFTER-LINE" }],
          },
        ],
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Changes" });
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(await screen.findByText("Proposal 1")).toBeDefined();
    expect(screen.getByText("proposed")).toBeDefined();
    expect(screen.getByText("Fix it.")).toBeDefined();
    expect(screen.getByText("pkg/calc.py")).toBeDefined();
    expect(screen.getByText("BEFORE-LINE")).toBeDefined();
    expect(screen.getByText("AFTER-LINE")).toBeDefined();
    expect(screen.getByText("verified")).toBeDefined();
    expect(screen.queryByText("Patch guard")).toBeNull();
    expect(screen.queryByText("No changes proposed yet")).toBeNull();
  });

  it("shows the settled debug report across Overview, Findings, Evidence and Changes", async () => {
    const report = {
      ...debugReport(),
      summary: "SUMMARY-HERE",
      diagnosis: "DIAGNOSIS-HERE",
      baseline: {
        ...debugReport().baseline,
        failures: [
          { test: "tests/test_x.py::test_y", kind: "failed", message: "MSG-HERE", traceback: "TRACE-HERE" },
        ],
      },
      patch: {
        edits: [{ file: "pkg/changed.py", before: "OLD-LINE", after: "NEW-LINE" }],
        reason: "REASON-HERE",
      },
      guard: { validated: true, message: "GUARD-OK" },
    };
    mockJobs.getJob.mockResolvedValueOnce(
      detail({ status: "completed", progress_pct: 100, final_report: report })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    expect(await screen.findByText("Test-backed fix")).toBeDefined();
    expect(screen.getByText("SUMMARY-HERE")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Findings" }));
    expect(await screen.findByText("AI diagnosis")).toBeDefined();
    expect(screen.getByText("DIAGNOSIS-HERE")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(await screen.findByText("tests/test_x.py::test_y")).toBeDefined();
    expect(screen.getByText("MSG-HERE")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(await screen.findByText("pkg/changed.py")).toBeDefined();
    expect(screen.getByText("OLD-LINE")).toBeDefined();
    expect(screen.getByText("NEW-LINE")).toBeDefined();
    expect(screen.getByText("Patch guard")).toBeDefined();
    expect(screen.getByText("GUARD-OK")).toBeDefined();
    expect(screen.getByText("validated")).toBeDefined();
    expect(screen.getByText("independent run passed")).toBeDefined();
  });

  it("loads the investigation conversation in its tab, locked while running", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    mockConversation.getMessages.mockResolvedValueOnce({
      session_id: "s1",
      messages: [{ id: "m1", role: "user", content: "Q?", created_at: null }],
    });
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Conversation" });
    fireEvent.click(screen.getByRole("tab", { name: "Conversation" }));
    expect(await screen.findByText("Q?")).toBeDefined();
    expect(mockConversation.getMessages).toHaveBeenCalledWith("i1");
    expect(screen.getByText(/Conversation unlocks/)).toBeDefined();
    expect(screen.queryByLabelText("Ask about this investigation")).toBeNull();
  });

  it("enables the composer for terminal investigations", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({ status: "completed", progress_pct: 100 })
    );
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Conversation" });
    fireEvent.click(screen.getByRole("tab", { name: "Conversation" }));
    expect(await screen.findByLabelText("Ask about this investigation")).toBeDefined();
    expect(screen.queryByText(/Conversation unlocks/)).toBeNull();
  });

  it("shows honest empty states for a fresh job", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    await screen.findByRole("tab", { name: "Findings" });
    fireEvent.click(screen.getByRole("tab", { name: "Findings" }));
    expect(await screen.findByText("No findings yet")).toBeDefined();
    expect(screen.getByText("Findings will appear as the investigation progresses.")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(await screen.findByText("No evidence recorded yet")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Execution" }));
    expect(await screen.findByText("Execution has not started yet.")).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(await screen.findByText("No changes proposed yet")).toBeDefined();
  });

  it("reports failed jobs with their backend error", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        status: "failed",
        error: { reason: "dependency_error", message: "FAIL-MSG" },
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} restoredJobId="j1" />
    );
    expect(await screen.findByText("FAIL-MSG")).toBeDefined();
    expect(screen.getByText(/dependency_error/)).toBeDefined();
    fireEvent.click(screen.getByRole("tab", { name: "Execution" }));
    expect(await screen.findByText("No runs were recorded.")).toBeDefined();
  });

  it("opens the guided flow from the workbench header", () => {
    const onNewInvestigation = vi.fn();
    render(
      <WorkbenchView
        session={session}
        onUnauthorized={() => {}}
        onNewInvestigation={onNewInvestigation}
      />
    );
    fireEvent.click(screen.getByText("New investigation"));
    expect(onNewInvestigation).toHaveBeenCalledTimes(1);
  });
});
