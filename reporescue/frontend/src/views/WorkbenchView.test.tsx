import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Session } from "@supabase/supabase-js";
import { ApiError } from "../lib/api";
import WorkbenchView from "./WorkbenchView";

const mockJobs = vi.hoisted(() => ({
  createJob: vi.fn(),
  getActiveJob: vi.fn(),
  getJob: vi.fn(),
  listJobs: vi.fn(),
  cancelJob: vi.fn(),
  sendConversationMessage: vi.fn(),
}));

vi.mock("../lib/jobs", () => mockJobs);

const mockApi = vi.hoisted(() => ({ apiJson: vi.fn() }));
vi.mock("../lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/api")>();
  return { ...original, apiJson: mockApi.apiJson };
});

const mockGeneral = vi.hoisted(() => ({ classifyObjective: vi.fn() }));
vi.mock("../lib/general", () => mockGeneral);

const mockPatches = vi.hoisted(() => ({ downloadInvestigationPatch: vi.fn() }));
vi.mock("../lib/patches", () => mockPatches);

const session = { access_token: "token" } as unknown as Session;

function detail(overrides: Record<string, unknown> = {}) {
  return {
    investigation_id: "i1",
    repo_url: "https://github.com/acme/calc",
    repo_name: "acme/calc",
    commit_sha: "b".repeat(40),
    ref: null,
    mode: "debug",
    selected_mode: null,
    routing: null,
    preselected: null,
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

const DEFAULT_QUOTA = {
  timezone: "Africa/Lagos",
  resets_at: "2026-11-08T00:00:00+01:00",
  ai: { used: 0, limit: 10, remaining: 10 },
  debug: { used: 0, limit: 3, remaining: 3 },
};

beforeEach(() => {
  vi.resetAllMocks();
  mockJobs.listJobs.mockResolvedValue([]);
  mockApi.apiJson.mockResolvedValue(DEFAULT_QUOTA);
  // Mount fetches quota first; per-test action mocks queue after it.
  mockApi.apiJson.mockResolvedValueOnce(DEFAULT_QUOTA);
});

describe("WorkbenchView job restoration", () => {
  it("loads nothing without a selected job id", () => {
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    expect(mockJobs.getJob).not.toHaveBeenCalled();
    expect(mockJobs.createJob).not.toHaveBeenCalled();
  });

  it("loads the route-selected job once without creating a duplicate", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" onJobSelected={() => {}} />
    );
    expect(await screen.findByText("acme/calc")).toBeDefined();
    expect(screen.getAllByText(/RUNNING/).length).toBeGreaterThan(0);
    expect(mockJobs.getJob).toHaveBeenCalledTimes(1);
    expect(mockJobs.getJob).toHaveBeenCalledWith("j1");
    expect(mockJobs.createJob).not.toHaveBeenCalled();
  });

  it("reports saved-work selection to the route instead of fetching inline", async () => {
    mockJobs.listJobs.mockResolvedValue([
      { job_id: "j5", investigation_id: "i5", status: "completed", attempts: 1, max_attempts: 3, error: null, created_at: null, updated_at: null, repo_name: "acme/calc", mode: "debug", progress_pct: 100 },
    ]);
    const onJobSelected = vi.fn();
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId={null} onJobSelected={onJobSelected} />
    );
    fireEvent.click(await screen.findByText("acme/calc"));
    expect(onJobSelected).toHaveBeenCalledWith("j5");
    expect(mockJobs.getJob).not.toHaveBeenCalled();
  });

  it("drops a stale response when selection changes mid-flight", async () => {
    let resolveFirst!: (value: unknown) => void;
    const first = new Promise((resolve) => { resolveFirst = resolve; });
    const secondJob = { job_id: "j2", investigation_id: "i2", status: "running", attempts: 1, max_attempts: 3, error: null, created_at: null, updated_at: null };
    mockJobs.getJob
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce(detail({ repo_name: "acme/other", job: secondJob }));
    const { rerender } = render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" onJobSelected={() => {}} />
    );
    rerender(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j2" onJobSelected={() => {}} />
    );
    expect(await screen.findByText("acme/other")).toBeDefined();
    await act(async () => { resolveFirst(detail()); await first; });
    expect(screen.queryByText("acme/calc")).toBeNull();
    expect(mockJobs.getJob).toHaveBeenCalledWith("j2");
    if (mockJobs.getJob.mock.calls.length !== 2) throw new Error("CALLS=" + JSON.stringify(mockJobs.getJob.mock.calls));
  });

  it("shows the clarification question with a refine action", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({
      status: "failed",
      error: { reason: "needs_clarification", message: "What should be investigated?" },
      user_message: "debug it",
    }));
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    expect(await screen.findByText("Objective needs clarification")).toBeDefined();
    expect(screen.getByText("What should be investigated?")).toBeDefined();
    expect(screen.getByText("Refine objective")).toBeDefined();
  });

  it("renders the persisted report when polling observes completion", async () => {
    mockJobs.getJob
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce(detail({ status: "completed", progress_pct: 100, final_report: debugReport() }));
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
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
      idempotencyKey: expect.any(String),
    });
  });

  it("cancels the followed job", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    mockJobs.cancelJob.mockResolvedValueOnce(detail({ status: "cancelled" }));
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    expect(await screen.findByText("acme/calc")).toBeDefined();
    fireEvent.click(screen.getByText("Cancel investigation"));
    expect((await screen.findAllByText("CANCELLED")).length).toBeGreaterThan(0);
    expect(mockJobs.cancelJob).toHaveBeenCalledWith("j1");
  });

  it("classifies General objectives before queueing with the route", async () => {
    const onJobSelected = vi.fn();
    render(<WorkbenchView session={session} onUnauthorized={() => {}} onJobSelected={onJobSelected} />);
    mockApi.apiJson.mockResolvedValueOnce({ status: "complete", ingestion_id: "ing1", profile: { name: "acme/calc", commit_sha: "b".repeat(40), supported: true, tests: { count: 0 }, warnings: [] }, warnings: [] });
    mockGeneral.classifyObjective.mockResolvedValueOnce({
      selected_mode: "debug",
      rationale: "Failure signals matched.",
      confidence: "high",
      signals: { debug: ["fail*"] },
      source: "deterministic",
    });
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j7", investigation_id: "i7", status: "queued" });
    mockJobs.getJob.mockResolvedValueOnce(detail({ mode: "general", selected_mode: "debug", job: { job_id: "j7", investigation_id: "i7", status: "queued", attempts: 0, max_attempts: 3, error: null, created_at: null, updated_at: null }, status: "queued", progress_pct: 0 }));
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), { target: { value: "https://github.com/acme/calc" } });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText("What should RepoRescue do?")).toBeDefined();
    expect(screen.getByRole("radio", { name: /General/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), {
      target: { value: "fix the failing tests" },
    });
    fireEvent.click(screen.getByText("Start investigation"));
    await waitFor(() => expect(mockJobs.createJob).toHaveBeenCalledTimes(1));
    expect(mockGeneral.classifyObjective).toHaveBeenCalledWith("fix the failing tests", expect.any(String));
    expect(mockJobs.createJob).toHaveBeenCalledWith({
      ingestion_id: "ing1",
      mode: "general",
      message: "fix the failing tests",
      selected_mode: "debug",
      routing_rationale: "Failure signals matched.",
      routing_confidence: "high",
      routing_source: "deterministic",
      idempotencyKey: expect.any(String),
    });
    expect(onJobSelected).toHaveBeenCalledWith("j7");
    expect(await screen.findByText(/GENERAL → DEBUG INVESTIGATION/)).toBeDefined();
  });

  it("asks a follow-up question for vague General objectives without queueing", async () => {
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    mockApi.apiJson.mockResolvedValueOnce({ status: "complete", ingestion_id: "ing1", profile: { name: "acme/calc", commit_sha: "b".repeat(40), supported: true, tests: { count: 0 }, warnings: [] }, warnings: [] });
    mockGeneral.classifyObjective.mockResolvedValueOnce({
      selected_mode: null,
      reason: "needs_clarification",
      question: "What should be investigated?",
      rationale: "Too vague.",
      confidence: "none",
      signals: { debug: [], explain: [], health: [] },
      suggested_mode: "debug",
      source: "deterministic",
    });
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), { target: { value: "https://github.com/acme/calc" } });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText("What should RepoRescue do?")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), {
      target: { value: "debug it" },
    });
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText("What should be investigated?")).toBeDefined();
    expect(mockJobs.createJob).not.toHaveBeenCalled();
    // Repository selection and objective text are preserved.
    expect(screen.getByText("acme/calc")).toBeDefined();
    expect(
      (screen.getByLabelText("Problem statement or objective") as HTMLTextAreaElement).value
    ).toBe("debug it");
    // Refining clears the question; a routed retry queues exactly once.
    mockGeneral.classifyObjective.mockResolvedValueOnce({
      selected_mode: "debug",
      rationale: "Failure signals matched.",
      confidence: "medium",
      signals: { debug: ["fail*"] },
      source: "deterministic",
    });
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j8", investigation_id: "i8", status: "queued" });
    mockJobs.getJob.mockResolvedValueOnce(detail({ job: { job_id: "j8", investigation_id: "i8", status: "queued", attempts: 0, max_attempts: 3, error: null, created_at: null, updated_at: null }, status: "queued", progress_pct: 0 }));
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), {
      target: { value: "find why the login tests fail" },
    });
    expect(screen.queryByText("What should be investigated?")).toBeNull();
    fireEvent.click(screen.getByText("Start investigation"));
    expect((await screen.findAllByText(/QUEUED/)).length).toBeGreaterThan(0);
    expect(mockJobs.createJob).toHaveBeenCalledTimes(1);
  });

  it("renders persisted conversation history with a working composer", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({ messages: [{ role: "user", content: "Where is the parser?" }, { role: "assistant", content: "The API persisted this response." }] }));
    render(<WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />);
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Conversation" }));
    expect(screen.getByText("Where is the parser?")).toBeDefined();
    expect(screen.getByText("The API persisted this response.")).toBeDefined();
    expect(screen.getByPlaceholderText("Ask a follow-up about the findings or evidence…")).toBeDefined();
  });

  it("renders backend findings, execution evidence, and proposed patches without conflating verification", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({
      findings: [{ kind: "hypothesis", category: "logic", title: "Possible boundary error", detail: "Review the upper bound.", confidence: "medium", evidence: { file: "src/range.py", line: 12 } }],
      runs: [{ kind: "baseline", backend: "modal", isolation: "cloud sandbox", status: "test_failure", passed: false, exit_code: 1, output: "FAILED test_range.py", failures: [{ test: "test_range", kind: "assertion", message: "Expected 3", traceback: "trace" }], duration_ms: 450, install_ok: true, install_log: "dependencies ready" }],
      patches: [{ status: "proposed", reason: "Adjust the boundary.", files: [{ file: "src/range.py", before: "range(3)", after: "range(4)" }] }],
      verification_status: "unverified",
    }));
    render(<WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />);
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

  it("renders structured explanation sections with copyable commands", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({
      status: "completed",
      final_report: {
        summary: "## Overview\n\nAuth verifies users.\n\n## How to use it\n\n```bash\npytest -q\n```\n",
      },
    }));
    const { container } = render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    expect(await screen.findByText("Auth verifies users.")).toBeDefined();
    expect(container.querySelector(".md h3")?.textContent).toBe("Overview");
    expect(screen.getByText("pytest -q")).toBeDefined();
    expect(screen.getByText("Copy")).toBeDefined();
  });

  it("renders the active investigation above the setup form", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail());
    const { container } = render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    await screen.findByText("acme/calc");
    const results = container.querySelector(".workbench-results");
    const setup = container.querySelector(".setup-disclosure");
    expect(results).toBeDefined();
    expect(setup).toBeDefined();
    expect(
      results!.compareDocumentPosition(setup!) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it("collapses the setup form once an investigation starts", async () => {
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    mockApi.apiJson.mockResolvedValueOnce({ status: "complete", ingestion_id: "ing1", profile: { name: "acme/calc", commit_sha: "b".repeat(40), supported: true, tests: { count: 0 }, warnings: [] }, warnings: [] });
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j9", investigation_id: "i9", status: "queued" });
    mockJobs.getJob.mockResolvedValueOnce(detail({ job: { job_id: "j9", investigation_id: "i9", status: "queued", attempts: 0, max_attempts: 3, error: null, created_at: null, updated_at: null }, status: "queued", progress_pct: 0 }));
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), { target: { value: "https://github.com/acme/calc" } });
    fireEvent.click(screen.getByText("Inspect repository"));
    expect(await screen.findByText("What should RepoRescue do?")).toBeDefined();
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    fireEvent.click(screen.getByText("Start investigation"));
    expect((await screen.findAllByText(/QUEUED/)).length).toBeGreaterThan(0);
    expect(
      document.querySelector(".setup-disclosure")?.hasAttribute("open")
    ).toBe(false);
  });

  it("hides empty evidence objects but keeps real evidence inspectable", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({
      findings: [
        { kind: "fact", category: "t", title: "Empty evidence", detail: "d", confidence: "confirmed", evidence: {} },
        { kind: "fact", category: "t", title: "Real evidence", detail: "d", confidence: "confirmed", evidence: { file: "src/a.py", line: 3 } },
      ],
    }));
    render(<WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />);
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: /Findings/ }));
    expect(screen.getAllByText("Inspect finding evidence").length).toBe(1);
    fireEvent.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(screen.queryByText("Empty evidence")).toBeNull();
    expect(screen.getByText("Real evidence")).toBeDefined();
  });
});

describe("WorkbenchView repository inspection", () => {
  function inspect() {
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), {
      target: { value: "https://github.com/acme/calc" },
    });
    fireEvent.click(screen.getByText("Inspect repository"));
  }

  function completeIngest() {
    return {
      status: "complete",
      ingestion_id: "ing1",
      profile: {
        name: "acme/calc", commit_sha: "b".repeat(40), structure_summary: "Python project.",
        supported: true, primary_language: "python", test_framework: "pytest",
        tests: { count: 0 },
      },
      warnings: [],
    };
  }

  it("surfaces the backend failure reason instead of a generic error", async () => {
    mockApi.apiJson.mockResolvedValueOnce({
      status: "error", reason: "not_found", message: "Repository not found.",
    });
    inspect();
    expect(await screen.findByText(/repository was not found/i)).toBeDefined();
  });

  it("falls back when the backend gives no reason", async () => {
    mockApi.apiJson.mockResolvedValueOnce({ status: "error" });
    inspect();
    expect(
      await screen.findByText("The API did not confirm repository inspection.")
    ).toBeDefined();
  });

  it("redacts filesystem paths from backend detail", async () => {
    mockApi.apiJson.mockResolvedValueOnce({
      status: "error",
      reason: "workspace_error",
      message: "Failed at C:\\Temp\\x and /tmp/y.",
    });
    inspect();
    const alert = await screen.findByText(/backend could not prepare a workspace/i);
    expect(alert.textContent).not.toContain("C:\\Temp");
    expect(alert.textContent).not.toContain("/tmp/y");
  });

  it("inspects with the extended timeout without duplicating the request", async () => {
    mockApi.apiJson.mockResolvedValueOnce(completeIngest());
    inspect();
    await screen.findByText("What should RepoRescue do?");
    const ingestCalls = mockApi.apiJson.mock.calls.filter((call) => call[0] === "/api/repos/ingest");
    expect(ingestCalls).toHaveLength(1);
    expect(ingestCalls[0][1]).toEqual(expect.objectContaining({ method: "POST" }));
    expect(ingestCalls[0][2]).toEqual({ timeoutMs: 150000 });
  });
});

describe("WorkbenchView patch download", () => {
  function appliedDetail(overrides: Record<string, unknown> = {}) {
    return detail({
      status: "completed",
      patches: [
        {
          status: "applied",
          reason: "Fix.",
          files: [{ file: "pkg/calc.py", before: "a", after: "b" }],
        },
      ],
      verification_status: "passed",
      ...overrides,
    });
  }

  it("shows a verified download action for applied patches", async () => {
    mockJobs.getJob.mockResolvedValueOnce(appliedDetail());
    mockPatches.downloadInvestigationPatch.mockResolvedValueOnce({ filename: "x.patch" });
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(screen.getByText("Download .patch")).toBeDefined();
    expect(screen.getByText("verified")).toBeDefined();
    fireEvent.click(screen.getByText("Download .patch"));
    expect(mockPatches.downloadInvestigationPatch).toHaveBeenCalledWith("i1");
  });

  it("warns on unverified patches and surfaces download errors", async () => {
    mockJobs.getJob.mockResolvedValueOnce(appliedDetail({ verification_status: "failed" }));
    mockPatches.downloadInvestigationPatch.mockRejectedValueOnce(
      new ApiError(404, "No downloadable patch is available for this investigation.")
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(screen.getByText("unverified")).toBeDefined();
    expect(screen.getByText(/provided for inspection only/)).toBeDefined();
    fireEvent.click(screen.getByText("Download .patch"));
    expect(
      await screen.findByText(
        "No downloadable patch is available for this investigation."
      )
    ).toBeDefined();
  });

  it("hides the download action without an applied patch", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        status: "completed",
        patches: [{ status: "proposed", reason: "Maybe.", files: [] }],
        verification_status: "unverified",
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(screen.queryByText("Download .patch")).toBeNull();
  });

  it("shows affected files and how-to-apply instructions", async () => {
    mockJobs.getJob.mockResolvedValueOnce(appliedDetail());
    const { container } = render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(screen.getByText("AFFECTED FILES")).toBeDefined();
    expect(screen.getAllByText("pkg/calc.py").length).toBeGreaterThan(0);
    expect(screen.getByText("How to apply this patch")).toBeDefined();
    expect(container.textContent).toContain("git apply --check reporescue-fix.patch");
    expect(container.textContent).toContain("git apply -R reporescue-fix.patch");
    expect(container.textContent).toContain("Downloading never modifies your repositories.");
  });

  it("names the saved file and offers to download again", async () => {
    mockJobs.getJob.mockResolvedValueOnce(appliedDetail());
    mockPatches.downloadInvestigationPatch.mockResolvedValueOnce({ filename: "x.patch" });
    const { container } = render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    fireEvent.click(screen.getByText("Download .patch"));
    expect(await screen.findByText("Download again")).toBeDefined();
    expect(screen.getByText("x.patch")).toBeDefined();
    expect(container.textContent).toContain("browser’s download location");
    expect(container.textContent).toContain("git apply x.patch");
  });

  it("labels proposed patches as not applied anywhere", async () => {
    mockJobs.getJob.mockResolvedValueOnce(
      detail({
        status: "completed",
        patches: [{ status: "proposed", reason: "Maybe.", files: [] }],
        verification_status: "unverified",
      })
    );
    render(
      <WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />
    );
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(
      screen.getByText("Proposed: suggested by the model, not applied anywhere.")
    ).toBeDefined();
  });
});

describe("WorkbenchView beta restrictions", () => {
  function pythonIngest() {
    return { status: "complete", ingestion_id: "ing1", profile: { name: "acme/calc", commit_sha: "b".repeat(40), primary_language: "Python", supported: true, tests: { count: 0 }, warnings: [] }, warnings: [] as string[] };
  }
  function jsIngest() {
    return {
      status: "complete",
      ingestion_id: "ing-js",
      profile: {
        name: "acme/web",
        commit_sha: "j".repeat(40),
        ref: "main",
        primary_language: "JavaScript",
        supported: false,
        test_framework: "none",
        tests: { dirs: [], files: [], count: 0 },
        warnings: [],
      },
      warnings: [] as string[],
    };
  }

  async function inspectRepo(url: string, payload: unknown) {
    mockApi.apiJson.mockResolvedValueOnce(payload);
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Repository URL/i), { target: { value: url } });
    fireEvent.click(screen.getByText("Inspect repository"));
  }

  it("shows the public-only beta notice without inspecting", () => {
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    expect(screen.getByText(/Beta limitation/)).toBeDefined();
    expect(screen.getByText(/public GitHub repositories only/)).toBeDefined();
  });

  it("shows the Python-only note when Debug is selected", async () => {
    await inspectRepo("https://github.com/acme/calc", pythonIngest());
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    expect(screen.getByText(/Python-only in beta/)).toBeDefined();
  });

  it("blocks Debug on non-Python repos but allows switching modes", async () => {
    await inspectRepo("https://github.com/acme/web", jsIngest());
    await screen.findByText("acme/web");
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    expect(screen.getByText("Debug can't run on this repository")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), { target: { value: "fix it" } });
    fireEvent.click(screen.getByText("Start investigation"));
    expect(mockJobs.createJob).not.toHaveBeenCalled();
    expect(screen.getByText(/not a Python project/)).toBeDefined();
    expect(screen.queryByText("Retry with same request")).toBeNull();
    expect((screen.getByLabelText("Problem statement or objective") as HTMLTextAreaElement).value).toBe("fix it");
    fireEvent.click(screen.getByRole("radio", { name: /Explain/ }));
    expect(screen.queryByText("Debug can't run on this repository")).toBeNull();
    mockJobs.createJob.mockRejectedValueOnce(new ApiError(502, "bad gateway"));
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText("bad gateway")).toBeDefined();
    expect(mockJobs.createJob).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Retry with same request")).toBeDefined();
  });

  it("renders the private-repository beta label", async () => {
    await inspectRepo("https://github.com/acme/secret", {
      status: "error",
      reason: "private_repo_not_supported",
      message: "The repository was not found or is private.",
      url: "https://github.com/acme/secret",
      name: "acme/secret",
    });
    expect(await screen.findByText(/public GitHub repositories only/)).toBeDefined();
  });
});

describe("WorkbenchView request errors and retry", () => {
  function pythonIngest() {
    return { status: "complete", ingestion_id: "ing1", profile: { name: "acme/calc", commit_sha: "b".repeat(40), primary_language: "Python", supported: true, tests: { count: 0 }, warnings: [] }, warnings: [] as string[] };
  }
  async function startWithObjective() {
    mockApi.apiJson.mockResolvedValueOnce(pythonIngest());
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Repository URL/i), { target: { value: "https://github.com/acme/calc" } });
    fireEvent.click(screen.getByText("Inspect repository"));
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), { target: { value: "fix it" } });
  }

  it("shows coded backend errors without a retry action", async () => {
    await startWithObjective();
    mockJobs.createJob.mockRejectedValueOnce(
      new ApiError(422, "Debug mode currently supports Python repositories only.", "DEBUG_PYTHON_ONLY")
    );
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText("Debug mode currently supports Python repositories only.")).toBeDefined();
    expect(screen.queryByText("Retry with same request")).toBeNull();
    expect((screen.getByLabelText("Problem statement or objective") as HTMLTextAreaElement).value).toBe("fix it");
  });

  it("retries retryable failures with the same idempotency key", async () => {
    await startWithObjective();
    mockJobs.createJob.mockRejectedValueOnce(new ApiError(502, "bad gateway"));
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText("Retry with same request")).toBeDefined();
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j9", investigation_id: "i9", status: "queued" });
    fireEvent.click(screen.getByText("Retry with same request"));
    await waitFor(() => expect(mockJobs.createJob).toHaveBeenCalledTimes(2));
    const [first, second] = mockJobs.createJob.mock.calls;
    expect(typeof first[0].idempotencyKey).toBe("string");
    expect(second[0].idempotencyKey).toBe(first[0].idempotencyKey);
    expect(screen.queryByText("Retry with same request")).toBeNull();
  });

  it("clears the retry action when the objective changes", async () => {
    await startWithObjective();
    mockJobs.createJob.mockRejectedValueOnce(new ApiError(0, "timed out"));
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText("Retry with same request")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), { target: { value: "fix it harder" } });
    expect(screen.queryByText("Retry with same request")).toBeNull();
  });

  it("shows the limit message without retry and refreshes usage on quota 429", async () => {
    await startWithObjective();
    mockApi.apiJson.mockResolvedValue({
      ...DEFAULT_QUOTA,
      ai: { used: 10, limit: 10, remaining: 0 },
    });
    mockJobs.createJob.mockRejectedValueOnce(
      new ApiError(429, "The API is rate limiting requests.", "AI_DAILY_USER_LIMIT_REACHED")
    );
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText(/You've reached your 10 AI operations/)).toBeDefined();
    expect(screen.queryByText("Retry with same request")).toBeNull();
    expect(await screen.findByText(/used all 10 AI operations/)).toBeDefined();
  });
});

describe("WorkbenchView usage, skeleton and accessibility", () => {
  function pythonIngest() {
    return { status: "complete", ingestion_id: "ing1", profile: { name: "acme/calc", commit_sha: "b".repeat(40), primary_language: "Python", supported: true, tests: { count: 0 }, warnings: [] }, warnings: [] as string[] };
  }
  function quota(aiUsed: number, debugUsed: number) {
    return {
      timezone: "Africa/Lagos",
      resets_at: "2026-11-08T00:00:00+01:00",
      ai: { used: aiUsed, limit: 10, remaining: 10 - aiUsed },
      debug: { used: debugUsed, limit: 3, remaining: 3 - debugUsed },
    };
  }
  function useQuota(q: unknown) {
    mockApi.apiJson.mockReset();
    mockApi.apiJson.mockResolvedValue(q);
    mockApi.apiJson.mockResolvedValueOnce(q); // mount quota fetch
  }
  async function inspectPythonRepo() {
    mockApi.apiJson.mockResolvedValueOnce(pythonIngest());
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Repository URL/i), { target: { value: "https://github.com/acme/calc" } });
    fireEvent.click(screen.getByText("Inspect repository"));
    await screen.findByText("acme/calc");
  }
  function quotaCalls() {
    return mockApi.apiJson.mock.calls.filter((call) => call[0] === "/api/usage/quota");
  }

  it("shows shared and Debug usage counters with a status role", async () => {
    useQuota(quota(4, 2));
    await inspectPythonRepo();
    const indicator = await screen.findByText(/AI usage today: 4 \/ 10/);
    expect(indicator.closest("[role=\"status\"]")).not.toBeNull();
    expect(indicator.textContent).toContain("Debug: 2 / 3");
  });

  it("explains Debug exhaustion while other modes remain available", async () => {
    useQuota(quota(7, 3));
    await inspectPythonRepo();
    expect(await screen.findByText(/AI usage today: 7 \/ 10/)).toBeDefined();
    expect(screen.getByText(/used all 3 Debug operations/)).toBeDefined();
    expect(screen.getByText(/remain available/)).toBeDefined();
  });

  it("explains full shared exhaustion", async () => {
    useQuota(quota(10, 1));
    await inspectPythonRepo();
    expect(await screen.findByText(/used all 10 AI operations/)).toBeDefined();
    expect(screen.getByText(/allowance resets tomorrow/)).toBeDefined();
  });

  it("refreshes usage after a successful start but not after a failure", async () => {
    useQuota(quota(4, 2));
    await inspectPythonRepo();
    expect(quotaCalls()).toHaveLength(1);
    mockApi.apiJson.mockResolvedValue(quota(5, 2));
    mockJobs.getJob.mockResolvedValue(detail({ status: "completed" }));
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), { target: { value: "fix it" } });
    mockJobs.createJob.mockRejectedValueOnce(new ApiError(502, "bad gateway"));
    fireEvent.click(screen.getByText("Start investigation"));
    expect(await screen.findByText("Retry with same request")).toBeDefined();
    expect(quotaCalls()).toHaveLength(1);
    expect(screen.getByText(/AI usage today: 4 \/ 10/)).toBeDefined();
    mockJobs.createJob.mockResolvedValueOnce({ job_id: "j1", investigation_id: "i1", status: "queued" });
    fireEvent.click(screen.getByText("Retry with same request"));
    expect(await screen.findByText(/AI usage today: 5 \/ 10/)).toBeDefined();
    expect(quotaCalls().length).toBeGreaterThan(1);
  });

  it("shows an accessible skeleton while starting and clears it after", async () => {
    mockApi.apiJson.mockResolvedValueOnce(pythonIngest());
    mockJobs.getJob.mockResolvedValue(detail({ status: "completed" }));
    render(<WorkbenchView session={session} onUnauthorized={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Repository URL/i), { target: { value: "https://github.com/acme/calc" } });
    fireEvent.click(screen.getByText("Inspect repository"));
    await screen.findByText("acme/calc");
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    fireEvent.change(screen.getByLabelText("Problem statement or objective"), { target: { value: "fix it" } });
    let resolveStart!: (value: unknown) => void;
    mockJobs.createJob.mockReturnValueOnce(new Promise((resolve) => { resolveStart = resolve; }));
    fireEvent.click(screen.getByText("Start investigation"));
    const skeleton = await screen.findByRole("status", { name: "Loading investigation" });
    expect(skeleton.getAttribute("aria-busy")).toBe("true");
    await act(async () => { resolveStart({ job_id: "j1", investigation_id: "i1", status: "queued" }); });
    await waitFor(() => expect(screen.queryByRole("status", { name: "Loading investigation" })).toBeNull());
  });

  it("hides stale detail behind a skeleton when switching investigations", async () => {
    mockJobs.getJob.mockResolvedValueOnce(detail({ repo_name: "acme/old", status: "completed" }));
    const view = render(<WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j1" />);
    await screen.findByText("acme/old");
    let resolveSecond!: (value: unknown) => void;
    mockJobs.getJob.mockReturnValueOnce(new Promise((resolve) => { resolveSecond = resolve; }));
    view.rerender(<WorkbenchView session={session} onUnauthorized={() => {}} selectedJobId="j2" />);
    await screen.findByRole("status", { name: "Loading investigation" });
    expect(screen.queryByText("acme/old")).toBeNull();
    await act(async () => {
      resolveSecond(detail({ investigation_id: "i2", repo_name: "acme/new", status: "completed", job: { job_id: "j2", investigation_id: "i2", status: "completed", attempts: 1, max_attempts: 3, error: null, created_at: null, updated_at: null } }));
    });
    await screen.findByText("acme/new");
    await waitFor(() => expect(screen.queryByRole("status", { name: "Loading investigation" })).toBeNull());
  });
});