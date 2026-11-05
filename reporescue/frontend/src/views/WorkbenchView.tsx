import { useEffect, useRef, useState } from "react";
import {
  Activity, AlertTriangle, CircleHelp, Code2, Download, FileCode2, FolderGit2,
  LoaderCircle, Play, ShieldCheck,
} from "lucide-react";
import type { Session } from "@supabase/supabase-js";
import { ApiError, apiJson } from "../lib/api";
import { classifyObjective, type Classification } from "../lib/general";
import { cancelJob, createJob, getJob, listJobs, sendConversationMessage } from "../lib/jobs";
import { Markdown } from "../lib/markdown";
import { downloadInvestigationPatch } from "../lib/patches";
import type { IngestResult, InvestigationDetail, InvestigationMode, JobCard, Project } from "../lib/types";
import { isTerminalStatus } from "../lib/types";

const POLL_MS = 3000;

type UsageQuota = {
  timezone: string;
  resets_at: string;
  ai: { used: number; limit: number; remaining: number };
  debug: { used: number; limit: number; remaining: number };
};

const INSPECT_TIMEOUT_MS = 150_000;

const INGEST_REASON_LABELS: Record<string, string> = {
  invalid_url: "That repository URL is not supported. Use https://github.com/owner/repo.",
  unsupported_host: "Only github.com repositories are supported.",
  invalid_ref: "That branch, tag, or commit reference is invalid.",
  not_found: "The repository was not found. Check the URL and that the repository is public.",
  network_error: "The repository host could not be reached. Retry in a moment.",
  clone_failed: "The repository could not be cloned.",
  timeout: "Cloning the repository timed out. Retry, or try a smaller repository.",
  repo_too_large: "The repository exceeds the supported size limit.",
  workspace_error: "The backend could not prepare a workspace. Retry in a moment.",
  config_error: "The backend is misconfigured. Contact support.",
};

function sanitizeBackendDetail(text: string): string {
  return text
    .replace(/[A-Za-z]:\\[^\s"']*/g, "[path]")
    .replace(/\/(tmp|var|home|Users)[^\s"']*/g, "[path]")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 300);
}

function ingestFailureMessage(result: { reason?: string; message?: string }): string {
  const label =
    (result.reason && INGEST_REASON_LABELS[result.reason]) ||
    "The API did not confirm repository inspection.";
  const detail = result.message ? sanitizeBackendDetail(result.message) : "";
  if (!detail || label.includes(detail)) return label;
  return `${label} ${detail}`;
}
const TABS = ["Overview", "Findings", "Evidence", "Execution", "Changes", "Conversation"] as const;
type Tab = (typeof TABS)[number];
const MODES: { id: InvestigationMode; title: string; description: string; icon: typeof Activity }[] = [
  { id: "general", title: "General", description: "Route the request to a suitable investigation.", icon: CircleHelp },
  { id: "debug", title: "Debug", description: "Investigate a failure and verify a proposed repair.", icon: Code2 },
  { id: "explain", title: "Explain", description: "Understand repository structure and behavior.", icon: FileCode2 },
  { id: "health", title: "Health", description: "Review repository health using available analysis.", icon: ShieldCheck },
];

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function pillKind(status: string) {
  if (["passed", "verified", "fact", "confirmed"].includes(status.toLowerCase())) return "status-pill success";
  if (["failed", "test_failure", "error"].includes(status.toLowerCase())) return "status-pill danger";
  if (["timeout", "cancelled", "unverified", "not_run", "static_only", "hypothesis", "suspected"].includes(status.toLowerCase())) return "status-pill warning";
  return "status-pill";
}

function InvestigationSkeleton() {
  return (
    <section className="card workbench-results workbench-skeleton" aria-label="Loading investigation" aria-busy="true">
      <div className="skeleton-header">
        <div className="skeleton-stack">
          <span className="skeleton-line skeleton-eyebrow" />
          <span className="skeleton-line skeleton-title" />
          <span className="skeleton-line skeleton-description" />
          <span className="skeleton-line skeleton-meta" />
        </div>
        <span className="skeleton-pill" />
      </div>
      <div className="skeleton-progress"><span className="skeleton-line" /></div>
      <div className="skeleton-tabs">
        <span /><span /><span /><span /><span />
      </div>
      <div className="skeleton-content">
        <span className="skeleton-line skeleton-section-title" />
        <span className="skeleton-line skeleton-wide" />
        <span className="skeleton-line skeleton-wide" />
        <span className="skeleton-line skeleton-medium" />
        <div className="skeleton-panel"><span className="skeleton-line skeleton-medium" /><span className="skeleton-line skeleton-wide" /><span className="skeleton-line skeleton-short" /></div>
      </div>
    </section>
  );
}

function RecentInvestigationsSkeleton() {
  return <div className="recent-list" aria-label="Loading recent investigations" aria-busy="true">
    {[0, 1, 2].map((item) => <div className="recent-item recent-skeleton" key={item}>
      <span className="skeleton-line skeleton-title" />
      <span className="skeleton-line skeleton-meta" />
      <span className="skeleton-line skeleton-short" />
    </div>)}
  </div>;
}

function DisplayValue({ value }: { value: unknown }) {
  if (value == null || value === "") return <span className="muted">Not recorded</span>;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return <>{String(value)}</>;
  return <pre className="output">{JSON.stringify(value, null, 2)}</pre>;
}

export default function WorkbenchView({
  session,
  onUnauthorized,
  selectedJobId = null,
  onJobSelected = () => {},
  projects = [],
  projectsLoading = false,
  onNavigateProjects,
}: {
  session: Session | null;
  onUnauthorized: () => void;
  /** Job id selected by the route (deep link, saved work, restore). */
  selectedJobId?: string | null;
  /** Report a user-chosen job so the route (URL) follows the view. */
  onJobSelected?: (jobId: string | null) => void;
  projects?: Project[];
  projectsLoading?: boolean;
  onNavigateProjects?: () => void;
}) {
  const [repoUrl, setRepoUrl] = useState("");
  const [repoRef, setRepoRef] = useState("");
  const [projectId, setProjectId] = useState("");
  const [ingestResult, setIngestResult] = useState<IngestResult | null>(null);
  const [ingestLoading, setIngestLoading] = useState(false);
  const [ingestError, setIngestError] = useState("");
  const [mode, setMode] = useState<InvestigationMode>("general");
  const [userMessage, setUserMessage] = useState("");
  const [investLoading, setInvestLoading] = useState(false);
  const [investError, setInvestError] = useState("");
  const [classification, setClassification] = useState<Classification | null>(null);
  const [classifying, setClassifying] = useState(false);
  // The setup form stays open for a fresh visit and collapses once an
  // investigation takes focus, so the active work always leads the page.
  const [setupOpen, setSetupOpen] = useState(() => selectedJobId == null);
  const [jobDetail, setJobDetail] = useState<InvestigationDetail | null>(null);
  const [jobError, setJobError] = useState("");
  const [pendingJobId, setPendingJobId] = useState<string | null>(null);
  const [recentJobs, setRecentJobs] = useState<JobCard[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const [downloadedName, setDownloadedName] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [conversationDraft, setConversationDraft] = useState("");
  const [conversationSending, setConversationSending] = useState(false);
  const [conversationError, setConversationError] = useState("");
  const [adopting, setAdopting] = useState(false);
  const [usageQuota, setUsageQuota] = useState<UsageQuota | null>(null);
  const submitting = useRef(false);
  const polling = useRef(false);
  const loadSeq = useRef(0);

  const jobId = jobDetail?.job?.job_id ?? null;
  const active = Boolean(jobDetail && !isTerminalStatus(jobDetail.status));
  const appliedPatch = jobDetail?.patches.find((patch) => patch.status === "applied") ?? null;
  const patchVerified = jobDetail?.verification_status === "passed";

  function authError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 401) {
      onUnauthorized();
      return "Your session expired. Sign in again to continue.";
    }
    return errorMessage(error, fallback);
  }

  async function refreshQuota() {
    try {
      setUsageQuota(await apiJson<UsageQuota>("/api/usage/quota"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
    }
  }

  useEffect(() => {
    if (session) void refreshQuota();
    else setUsageQuota(null);
  }, [session]);

  async function refreshRecent() {
    if (!session) return;
    setRecentLoading(true);
    try {
      setRecentJobs(await listJobs(10));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setJobError(errorMessage(error, "Could not load recent investigations."));
    } finally { setRecentLoading(false); }
  }

  useEffect(() => {
    let live = true;
    if (session) void listJobs(10).then((jobs) => { if (live) setRecentJobs(jobs); }).catch((error: unknown) => {
      if (!live) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setJobError(errorMessage(error, "Could not load recent investigations."));
    }).finally(() => { if (live) setRecentLoading(false); });
    else setRecentLoading(false);
    return () => { live = false; };
  }, [session, onUnauthorized]);

  // Fetch the route-selected job (deep link, saved work, restore,
  // back/forward). Stale responses are dropped so one investigation can
  // never display another's data.
  useEffect(() => {
    if (!session || !selectedJobId || selectedJobId === jobId) return;
    const seq = ++loadSeq.current;
    let live = true;
    setAdopting(true);
    setJobError("");
    void getJob(selectedJobId).then((detail) => {
      if (!live || seq !== loadSeq.current) return;
      if (detail.job && detail.job.job_id !== selectedJobId) return;
      setJobDetail(detail);
      setPendingJobId(null);
      setTab("Overview");
    }).catch((error: unknown) => {
      if (live && seq === loadSeq.current) {
        setJobError(authError(error, "Could not load this investigation."));
      }
    }).finally(() => {
      if (live && seq === loadSeq.current) setAdopting(false);
    });
    return () => { live = false; };
  }, [session, selectedJobId, jobId]);

  useEffect(() => {
    if (!jobId || !active) return;
    let live = true;
    const timer = window.setInterval(() => {
      if (polling.current) return;
      polling.current = true;
      void getJob(jobId).then((next) => {
        if (!live) return;
        if (next.job && next.job.job_id !== jobId) return;
        setJobDetail(next);
        setJobError("");
      }).catch((error: unknown) => {
        if (live) setJobError(authError(error, "Lost contact with the investigation."));
      }).finally(() => { polling.current = false; });
    }, POLL_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [jobId, active]);

  // Download state belongs to one investigation; reset on switch.
  useEffect(() => {
    setDownloadedName(null);
    setDownloadError("");
  }, [jobId]);

  /** Reload one job directly (retry path), dropping stale responses. */
  async function reloadJob(id: string) {
    const seq = ++loadSeq.current;
    setJobError("");
    setInvestError("");
    setAdopting(true);
    try {
      const detail = await getJob(id);
      if (seq !== loadSeq.current) return;
      if (detail.job && detail.job.job_id !== id) return;
      setJobDetail(detail);
      setPendingJobId(null);
      setTab("Overview");
    } catch (error) {
      if (seq === loadSeq.current) {
        setJobError(authError(error, "Could not load this investigation."));
      }
    } finally {
      if (seq === loadSeq.current) setAdopting(false);
    }
  }

  async function sendConversation() {
    const question = conversationDraft.trim();
    if (!question || !jobDetail || !jobDetail.investigation_id || conversationSending) return;
    setConversationSending(true);
    setConversationError("");
    const key = typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `rr-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      const reply = await sendConversationMessage(jobDetail.investigation_id, question, key);
      setJobDetail((current) => {
        if (!current || current.investigation_id !== jobDetail.investigation_id) return current;
        return {
          ...current,
          messages: [
            ...current.messages,
            { role: "user", content: reply.user_message.content },
            {
              role: "assistant",
              content: reply.assistant_message.content,
              grounding_status: reply.assistant_message.grounding_status,
              evidence_ids: reply.assistant_message.evidence_ids,
            },
          ],
        };
      });
      setConversationDraft("");
      void refreshQuota();
    } catch (error) {
      setConversationError(authError(error, "Could not send your follow-up. Please try again."));
    } finally {
      setConversationSending(false);
    }
  }

  function selectProject(id: string) {
    setProjectId(id);
    const selected = projects.find((project) => project.id === id);
    setRepoUrl(selected?.github_url ?? "");
    setRepoRef(selected?.default_branch ?? "");
    setIngestResult(null);
    setIngestError("");
  }

  async function inspectRepository() {
    if (ingestLoading || !repoUrl.trim()) return;
    let parsed: URL;
    try { parsed = new URL(repoUrl.trim()); } catch { setIngestError("Enter a valid GitHub repository URL."); return; }
    if (parsed.protocol !== "https:" || parsed.hostname.toLowerCase() !== "github.com" || parsed.pathname.split("/").filter(Boolean).length < 2) {
      setIngestError("Enter a public repository URL in the form https://github.com/owner/repo.");
      return;
    }
    setIngestLoading(true);
    setIngestError("");
    setInvestError("");
    try {
      const result = await apiJson<IngestResult>("/api/repos/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: repoUrl.trim(), ref: repoRef.trim() || null }),
      }, { timeoutMs: INSPECT_TIMEOUT_MS });
      if (result.status !== "complete" || !result.ingestion_id) throw new Error(ingestFailureMessage(result));
      setIngestResult(result);
      setTab("Overview");
    } catch (error) {
      setIngestError(authError(error, "Could not inspect the repository."));
    } finally { setIngestLoading(false); }
  }

  async function downloadPatch() {
    if (!jobDetail || downloading) return;
    setDownloading(true);
    setDownloadError("");
    try {
      const { filename } = await downloadInvestigationPatch(jobDetail.investigation_id);
      setDownloadedName(filename);
    } catch (error) {
      setDownloadError(authError(error, "Could not download the patch."));
    } finally { setDownloading(false); }
  }

  async function startInvestigation() {
    if (!ingestResult || submitting.current || investLoading || classifying) return;
    if (pendingJobId) {
      setJobError(`Job ${pendingJobId.slice(0, 8)} was accepted but its status has not loaded yet. Retry loading it before creating another investigation.`);
      return;
    }
    // General classifies first: a vague objective stops here with a
    // follow-up question (repository selection and text are preserved)
    // instead of queueing an investigation that cannot answer it.
    let route: Extract<Classification, { selected_mode: InvestigationMode }> | null = null;
    if (mode === "general") {
      setClassifying(true);
      setInvestError("");
      setJobError("");
      try {
        const result = await classifyObjective(userMessage);
        void refreshQuota();
        setClassification(result);
        if (!result.selected_mode) {
          return;
        }
        route = result;
      } catch (error) {
        setInvestError(authError(error, "Could not classify the objective."));
        return;
      } finally {
        setClassifying(false);
      }
    } else {
      setClassification(null);
    }
    submitting.current = true;
    setInvestLoading(true);
    setInvestError("");
    setJobError("");
    setJobDetail(null);
    try {
      const created = await createJob({
        ingestion_id: ingestResult.ingestion_id,
        mode,
        message: userMessage,
        ...(projectId ? { project_id: projectId } : {}),
        ...(route
          ? {
              selected_mode: route.selected_mode,
              routing_rationale: route.rationale,
              routing_confidence: route.confidence,
              routing_source: route.source,
            }
          : {}),
      });
      if (!created.job_id || !created.investigation_id) throw new Error("The API did not return an investigation ID.");
      setPendingJobId(created.job_id);
      void refreshRecent();
      void refreshQuota();
      try {
        const detail = await getJob(created.job_id);
        setJobDetail(detail);
        setPendingJobId(null);
        setJobError("");
        setTab("Overview");
        setSetupOpen(false);
        onJobSelected(created.job_id);
      } catch (error) {
        setJobError(`The API accepted job ${created.job_id}, but its status could not be loaded. Retry to open that persisted job. ${authError(error, "")}`);
      }
    } catch (error) {
      setInvestError(authError(error, "Could not start the investigation."));
    } finally {
      submitting.current = false;
      setInvestLoading(false);
    }
  }

  function openJob(id: string) {
    // Route-driven: updating the selection fetches through the effect,
    // so the URL, saved work, and the detail view always agree.
    setJobError("");
    setInvestError("");
    setSetupOpen(false);
    onJobSelected(id);
  }

  async function stopJob() {
    if (!jobId || cancelling) return;
    setCancelling(true);
    try { setJobDetail(await cancelJob(jobId)); setJobError(""); void refreshRecent(); }
    catch (error) { setJobError(authError(error, "Could not cancel the investigation.")); }
    finally { setCancelling(false); }
  }

  const report = jobDetail?.final_report ?? {};
  const diagnosis = typeof report.diagnosis === "string" ? report.diagnosis : null;
  const summary = typeof report.summary === "string" ? report.summary : null;

  return (
    <div className="workbench-page view-stack">
      <header className="page-intro workbench-heading">
        <div><p className="eyebrow">REPOSITORY ENGINEERING</p><h2>Workbench</h2><p>Set an investigation in motion, then inspect what the repository and execution can support.</p></div>
        {jobDetail && <span className={pillKind(jobDetail.status)}>{jobDetail.status.toUpperCase()}</span>}
      </header>


      {jobError && <div className="banner banner-error" role="alert"><AlertTriangle size={17} /><div>{jobError}</div><button type="button" className="btn btn-secondary btn-sm" onClick={() => jobId ? void reloadJob(jobId) : pendingJobId ? void reloadJob(pendingJobId) : selectedJobId ? void reloadJob(selectedJobId) : void refreshRecent()}>Retry</button></div>}
      {adopting && !jobDetail && !jobError && <InvestigationSkeleton />}

      {jobDetail && <section className="card workbench-results" aria-label="Investigation results">
        <header className="investigation-header"><div><p className="eyebrow">{jobDetail.mode === "general" && jobDetail.selected_mode ? `GENERAL \u2192 ${jobDetail.selected_mode.toUpperCase()} INVESTIGATION` : `${jobDetail.mode.toUpperCase()} INVESTIGATION`}</p><h3>{jobDetail.repo_name}</h3><p className="investigation-objective">{jobDetail.user_message || "No objective was recorded for this investigation."}</p><div className="context-meta"><span>Revision <code>{jobDetail.commit_sha.slice(0, 7)}</code></span>{jobDetail.ref && <span>Ref {jobDetail.ref}</span>}<span>Job {jobDetail.job?.job_id.slice(0, 8)}</span></div></div><div className="investigation-actions"><span className={pillKind(jobDetail.status)}>{jobDetail.status.toUpperCase()}</span>{active && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void stopJob()} disabled={cancelling}>{cancelling ? "Cancelling…" : "Cancel investigation"}</button>}</div></header>
        {active && <div className="job-progress"><div className="progress-label"><span>{jobDetail.stage || jobDetail.status}</span><span>{jobDetail.progress_pct}% reported by API</span></div><progress value={jobDetail.progress_pct} max={100} aria-label={`Investigation progress ${jobDetail.progress_pct}%`} /></div>}
        {jobDetail.error && jobDetail.error.reason === "needs_clarification" && <div className="banner banner-warning" role="status"><CircleHelp size={17} /><div><strong>Objective needs clarification</strong><p>{jobDetail.error.message}</p><p className="hint">Nothing was started. Refine the objective and start a new investigation — the repository selection is preserved.</p></div><button type="button" className="btn btn-secondary btn-sm" onClick={() => { setUserMessage(jobDetail.user_message ?? ""); setSetupOpen(true); window.setTimeout(() => document.getElementById("investigation-objective")?.focus(), 0); }}>Refine objective</button></div>}
        {jobDetail.error && jobDetail.error.reason !== "needs_clarification" && <div className="banner banner-error" role="status"><AlertTriangle size={17} /><div><strong>{jobDetail.error.reason}</strong><p>{jobDetail.error.message}</p>{jobDetail.status === "failed" && <p className="hint">This is the persisted backend error. Adjust the repository or objective and start a new investigation to retry.</p>}</div></div>}
        <div className="workbench-tabs" role="tablist" aria-label="Investigation details" onKeyDown={(event) => { if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return; event.preventDefault(); const current = TABS.indexOf(tab); const next = event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length; setTab(TABS[next]); document.getElementById(`tab-${TABS[next].toLowerCase()}`)?.focus(); }}>{TABS.map((item) => <button key={item} id={`tab-${item.toLowerCase()}`} type="button" role="tab" tabIndex={tab === item ? 0 : -1} aria-selected={tab === item} aria-controls="workbench-panel" className={`workbench-tab${tab === item ? " active" : ""}`} onClick={() => setTab(item)}>{item}{item === "Findings" && <span>{jobDetail.findings.length}</span>}</button>)}</div>
        <div id="workbench-panel" role="tabpanel" aria-labelledby={`tab-${tab.toLowerCase()}`} className="workbench-panel">
          {tab === "Overview" && <div className="overview-grid"><div className="overview-summary"><p className="eyebrow">INVESTIGATION SUMMARY</p>{summary ? <Markdown text={summary} /> : diagnosis ? <Markdown text={diagnosis} /> : <p className="muted">The backend has not returned a final assessment yet.</p>}{jobDetail.limitations.length > 0 && <ul className="limitation-list">{jobDetail.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>}</div><div className="overview-facts"><div><span>Stage</span><strong>{jobDetail.stage || "Not reported"}</strong></div><div><span>Verification</span><strong className={pillKind(jobDetail.verification_status)}>{jobDetail.verification_status}</strong></div><div><span>Execution runs</span><strong>{jobDetail.runs.length}</strong></div><div><span>Proposed patches</span><strong>{jobDetail.patches.length}</strong></div></div>{jobDetail.routing && <div className="routing-note"><p className="eyebrow">GENERAL ROUTING</p><strong>Selected analysis: {jobDetail.routing.selected_mode}</strong><p>{jobDetail.routing.rationale}</p><span>Routing confidence: {jobDetail.routing.confidence}</span></div>}{diagnosis && summary && <div className="assessment-note"><ZapIcon /><div><strong>AI assessment</strong><Markdown text={diagnosis} /></div></div>}</div>}
          {tab === "Findings" && <div className="result-list">{jobDetail.findings.length ? jobDetail.findings.map((finding, index) => <article key={`${finding.title}-${index}`} className="result-item"><div className="result-item-heading"><span className={pillKind(finding.kind)}>{finding.kind}</span>{finding.category && <span className="muted">{finding.category}</span>}<span className="confidence">{finding.confidence} confidence</span></div><h4>{finding.title}</h4>{finding.detail && <div className="finding-detail"><Markdown text={finding.detail} /></div>}{hasEvidence(finding.evidence) && <details><summary>Inspect finding evidence</summary><DisplayValue value={finding.evidence} /></details>}</article>) : <EmptyResult title="No findings returned" text="Findings will appear here when they are present in the persisted investigation response." />}</div>}
          {tab === "Evidence" && <div className="result-list">{jobDetail.findings.some((finding) => hasEvidence(finding.evidence)) && jobDetail.findings.filter((finding) => hasEvidence(finding.evidence)).map((finding, index) => <article key={`evidence-${index}`} className="result-item"><h4>{finding.title}</h4><DisplayValue value={finding.evidence} /></article>)}{jobDetail.runs.map((run, index) => <article key={`run-evidence-${index}`} className="result-item"><h4>{run.kind} output</h4>{run.failures.map((failure, failureIndex) => <details key={failureIndex}><summary>{failure.test} · {failure.kind}</summary><pre className="output">{`${failure.message}\n${failure.traceback}`}</pre></details>)}{!run.failures.length && !run.output && <p className="muted">No execution evidence was recorded for this run.</p>}</article>)}{!jobDetail.findings.some((finding) => hasEvidence(finding.evidence)) && !jobDetail.runs.length && <EmptyResult title="No evidence recorded" text="Only repository and execution evidence returned by the backend is shown here." />}</div>}
          {tab === "Execution" && <div className="result-list">{jobDetail.runs.length ? jobDetail.runs.map((run, index) => <article key={`${run.kind}-${index}`} className="execution-card"><div className="result-item-heading"><h4>{run.kind}</h4><span className={pillKind(run.passed === true ? "passed" : run.passed === false ? "failed" : run.status || "not_run")}>{run.passed === true ? "tests passed" : run.passed === false ? "tests failed" : run.status || "not run"}</span></div><dl className="execution-meta"><div><dt>Provider</dt><dd>{run.backend || "Not reported"}</dd></div><div><dt>Isolation</dt><dd>{run.isolation || "Not reported"}</dd></div><div><dt>Exit code</dt><dd>{run.exit_code ?? "Not reported"}</dd></div><div><dt>Duration</dt><dd>{run.duration_ms == null ? "Not reported" : `${run.duration_ms} ms`}</dd></div>{run.command && <div><dt>Command</dt><dd><code>{run.command}</code></dd></div>}</dl>{run.install_ok != null && <p className="hint">Dependency installation: {run.install_ok ? "completed" : "failed"}</p>}{run.install_log && <details><summary>Dependency installation output</summary><pre className="output">{run.install_log}</pre></details>}{run.output && <details open><summary>Captured execution output</summary><pre className="output">{run.output}</pre></details>}{run.truncated && <p className="truncation-note" role="status">The backend truncated this execution output.</p>}{run.failures.map((failure, i) => <details key={i}><summary>{failure.test} · {failure.kind}</summary><pre className="output">{`${failure.message}\n${failure.traceback}`}</pre></details>)}</article>) : <EmptyResult title="No execution run recorded" text="This investigation has no execution output. Static analysis does not imply that code was executed." />}</div>}
          {tab === "Changes" && <div className="result-list">{appliedPatch && <article className="result-item patch-card"><div className="result-item-heading"><h4>Download patch</h4><span className={pillKind(patchVerified ? "verified" : "unverified")}>{patchVerified ? "verified" : "unverified"}</span></div><p className="hint">{patchVerified ? "Unified diff of the guarded patch for this investigation. Downloading saves the file and never modifies your repositories." : "Verification did not pass. This file is provided for inspection only; downloading saves the file and never modifies your repositories."}</p><p className="hint">{patchStatusNote(appliedPatch.status)}</p>{appliedPatch.files.length > 0 && <div><p className="eyebrow">AFFECTED FILES</p><ul className="affected-files">{appliedPatch.files.map((file, fileIndex) => <li key={fileIndex}><code>{file.file}</code></li>)}</ul></div>}<div className="patch-actions"><button type="button" className="btn btn-primary" onClick={() => void downloadPatch()} disabled={downloading}>{downloading ? <LoaderCircle size={15} className="spin" /> : <Download size={15} />}{downloading ? "Preparing\u2026" : downloadedName ? "Download again" : "Download .patch"}</button></div>{downloadedName && <p className="hint" role="status">Saved <code>{downloadedName}</code> to your browser’s download location.</p>}{downloadError && <div role="alert" className="banner banner-error"><AlertTriangle size={17} /><div><p>{downloadError}</p></div></div>}<details className="howto"><summary>How to apply this patch</summary><Markdown text={patchHowTo(downloadedName ?? "reporescue-fix.patch", patchVerified)} />{!downloadedName && <p className="hint">The exact filename is shown after you download.</p>}</details></article>}{jobDetail.patches.length ? jobDetail.patches.map((patch, index) => <article key={index} className="result-item"><div className="result-item-heading"><h4>Patch proposal {index + 1}</h4><span className={pillKind(patch.status)}>{patch.status}</span></div>{patch.reason && <p>{patch.reason}</p>}{patch.files.map((file, fileIndex) => <div key={fileIndex} className="diff-block"><div className="file-tab"><FileCode2 size={14} /><code>{file.file}</code></div><pre className="diff removed">{file.before || "No before content returned"}</pre><pre className="diff added">{file.after || "No after content returned"}</pre></div>)}<p className="hint">{patchStatusNote(patch.status)}</p><p className="hint">{jobDetail.verification_status === "passed" ? "The backend reports passing verification for this investigation." : `Verification is ${jobDetail.verification_status}; this patch has not been confirmed as a fix.`}</p></article>) : <EmptyResult title="No patch returned" text="The backend has not recorded a proposed change for this investigation." />}</div>}
          {tab === "Conversation" && <div className="conversation-panel">
            <div className="conversation-intro"><div><p className="eyebrow">INVESTIGATION ASSISTANT</p><h4>Ask about this investigation</h4><p>Follow up on findings, execution results and evidence. Replies are read-only and grounded in this investigation's saved record.</p></div><span className="conversation-scope">This investigation only</span></div>
            <div className="conversation-history" aria-live="polite">
              {jobDetail.messages.length ? jobDetail.messages.map((message, index) => <article className={`message-entry ${message.role}`} key={message.id ?? `${index}-${message.role}`}>
                <span>{message.role === "assistant" ? "RepoRescue" : message.role}</span>
                <div className="message-text"><Markdown text={message.content} /></div>
                {message.role === "assistant" && message.grounding_status && <div className="message-grounding"><span className={pillKind(message.grounding_status)}>{message.grounding_status.replaceAll("_", " ")}</span>{message.evidence_ids?.length ? <span>Evidence: {message.evidence_ids.join(", ")}</span> : <span>No direct evidence citations</span>}</div>}
              </article>) : <div className="conversation-empty"><CircleHelp size={20}/><strong>No messages yet</strong><p>Ask a question about this investigation's findings, logs, verification or proposed changes.</p></div>}
              {conversationSending && <article className="message-entry assistant conversation-thinking" role="status"><span>RepoRescue</span><div className="thinking-dots"><i/><i/><i/></div><small>Checking the saved investigation evidence…</small></article>}
            </div>
            {conversationError && <div className="banner banner-error" role="alert">{conversationError}</div>}
            <form className="conversation-composer" onSubmit={(event) => { event.preventDefault(); void sendConversation(); }}>
              <label className="sr-only" htmlFor="investigation-followup">Ask a follow-up about this investigation</label>
              <textarea id="investigation-followup" value={conversationDraft} onChange={(event) => setConversationDraft(event.target.value)} placeholder="Ask a follow-up about the findings or evidence…" rows={2} maxLength={2000} disabled={conversationSending}/>
              <div className="conversation-compose-footer"><small>Read-only · No code changes or commands are run</small><button className="btn btn-primary" type="submit" disabled={!conversationDraft.trim() || conversationSending}>{conversationSending ? <><LoaderCircle size={15} className="spin"/> Thinking…</> : <><Play size={14}/> Send</>}</button></div>
            </form>
          </div>}
        </div>
      </section>}

      <div className="workbench-start-grid">
      <details
        className="workbench-setup-main setup-disclosure"
        open={setupOpen}
        onToggle={(event) => setSetupOpen(event.currentTarget.open)}
      >
      <summary><strong>Start a new investigation</strong><small>Repository, mode, and objective</small></summary>
      <section className="surface-section investigation-setup" aria-labelledby="setup-title">
        <div className="section-heading"><div><p className="eyebrow">NEW INVESTIGATION</p><h3 id="setup-title">Choose a repository and objective</h3></div><FolderGit2 size={20} /></div>
        <div className="setup-fields">
          <div className="field"><label className="label" htmlFor="registered-repository">Registered repository <span className="optional">(optional)</span></label>
            <select id="registered-repository" className="input" value={projectId} onChange={(event) => selectProject(event.target.value)} disabled={projectsLoading}>
              <option value="">Enter a GitHub URL</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.github_owner}/{project.repository_name}</option>)}
            </select>
          </div>
          <div className="field"><label className="label" htmlFor="investigation-repo-url">GitHub repository URL</label><input id="investigation-repo-url" className="input" type="url" placeholder="https://github.com/owner/repo" value={repoUrl} onChange={(event) => { setRepoUrl(event.target.value); setProjectId(""); setIngestResult(null); }} /></div>
          <div className="field"><label className="label" htmlFor="investigation-ref">Branch, tag, or commit <span className="optional">(optional)</span></label><input id="investigation-ref" className="input" placeholder="Default branch" value={repoRef} onChange={(event) => { setRepoRef(event.target.value); setIngestResult(null); }} /></div>
          <button type="button" className="btn btn-secondary inspect-button" onClick={() => void inspectRepository()} disabled={ingestLoading || !repoUrl.trim()}>{ingestLoading ? <LoaderCircle size={16} className="spin" /> : <FolderGit2 size={16} />}{ingestLoading ? "Inspecting…" : "Inspect repository"}</button>
        </div>
        {projects.length === 0 && !projectsLoading && onNavigateProjects && <p className="hint">No registered repositories yet. You can inspect a public GitHub URL above or <button type="button" className="inline-link" onClick={onNavigateProjects}>register a repository</button> in Repositories first.</p>}
        {ingestError && <div role="alert" className="banner banner-error"><AlertTriangle size={17} /><div><strong>Repository inspection failed</strong><p>{ingestError}</p></div></div>}
        {ingestResult && <div className="repository-context"><div className="context-title"><FolderGit2 size={17} /><strong>{ingestResult.profile.name}</strong><span className="commit-chip">{ingestResult.profile.commit_sha.slice(0, 7)}</span></div><p>{ingestResult.profile.structure_summary}</p><div className="context-meta"><span>{ingestResult.profile.primary_language}</span><span>{ingestResult.profile.test_framework}</span><span>{ingestResult.profile.tests.count} test files</span><span className={pillKind(ingestResult.profile.supported ? "supported" : "unsupported")}>{ingestResult.profile.supported ? "Supported" : "Unsupported"}</span></div>{ingestResult.warnings.length > 0 && <ul className="warning-list">{ingestResult.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}</div>}
      </section>

      {ingestResult && <section className="card mode-panel" aria-labelledby="mode-title">
        <div className="section-heading"><div><p className="eyebrow">INVESTIGATION MODE</p><h3 id="mode-title">What should RepoRescue do?</h3></div></div>
        <div className="mode-grid" role="radiogroup" aria-label="Investigation mode">{MODES.map(({ id, title, description, icon: Icon }, index) => <button key={id} type="button" role="radio" tabIndex={mode === id ? 0 : -1} aria-checked={mode === id} className={`mode-card${mode === id ? " selected" : ""}`} onKeyDown={(event) => { if (event.key === "ArrowRight" || event.key === "ArrowDown" || event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1; const next = MODES[(index + step + MODES.length) % MODES.length]; setMode(next.id); setInvestError(""); setClassification(null); } }} onClick={() => { setMode(id); setInvestError(""); setClassification(null); }}><Icon size={18} /><span><strong>{title}</strong><small>{description}</small></span>{id === "general" && <span className="mode-default">DEFAULT</span>}</button>)}</div>
        <div className="field objective-field"><label className="label" htmlFor="investigation-objective">Problem statement or objective</label><textarea id="investigation-objective" className="input objective-input" rows={3} maxLength={2000} placeholder={mode === "debug" ? "Describe the failure, expected behavior, and any useful context…" : mode === "explain" ? "What part of the repository should be explained?" : mode === "health" ? "Which health concerns should be assessed?" : "Describe what you want to understand or investigate…"} value={userMessage} onChange={(event) => { setUserMessage(event.target.value); setClassification(null); }} /></div>
        {classifying && <div className="loading-block" role="status"><LoaderCircle size={16} className="spin" /> Classifying the objective…</div>}
        {classification && !classification.selected_mode && <div role="alert" className="banner banner-warning"><CircleHelp size={17} /><div><strong>Objective needs clarification</strong><p>{classification.question ?? classification.rationale}</p><p className="hint">Refine the objective above and press Start again. The repository selection is preserved.</p></div></div>}
        {classification && classification.selected_mode && <div className="routing-note" role="status"><p className="eyebrow">GENERAL ROUTING</p><strong>Will run: {classification.selected_mode}</strong><p>{classification.rationale}</p><span>Routing confidence: {classification.confidence} · {classification.source}</span>{classification.secondary_intents?.length ? <p className="hint">Also detected: {classification.secondary_intents.join(", ")}. This run focuses on {classification.selected_mode}; start a separate investigation for the additional assessment.</p> : null}</div>}
        <div className="setup-actions"><p className="hint">Inspection pins this repository snapshot. The queued job uses that snapshot and remains available after refresh.</p>{usageQuota && <p className="hint">Today: {usageQuota.ai.remaining}/{usageQuota.ai.limit} AI operations and {usageQuota.debug.remaining}/{usageQuota.debug.limit} Debug runs remaining · resets at midnight Africa/Lagos.</p>}<button type="button" className="btn btn-primary" onClick={() => void startInvestigation()} disabled={investLoading}>{investLoading ? <LoaderCircle size={16} className="spin" /> : <Play size={16} />}{investLoading ? "Starting…" : "Start investigation"}</button></div>
        {!ingestResult.profile.supported && <p className="capability-note" role="status">The API marked this repository as outside the currently supported execution profile. The backend determines whether the selected analysis can proceed.</p>}
        {investError && <div role="alert" className="banner banner-error"><AlertTriangle size={17} /><div><strong>Investigation not started</strong><p>{investError}</p></div></div>}
      </section>}

      </details>
      <aside className="recent-section history-rail" aria-labelledby="history-title">
        <div className="section-heading"><div><p className="eyebrow">SAVED WORK</p><h3 id="history-title">Recent investigations</h3></div></div>
        {recentLoading ? <RecentInvestigationsSkeleton /> : recentJobs.length > 0 ? <div className="recent-list">{recentJobs.map((job) => <button key={job.job_id} type="button" className={`recent-item${job.job_id === jobId ? " recent-active" : ""}`} onClick={() => void openJob(job.job_id)}><span className="recent-repo">{job.repo_name}</span><span className="recent-meta">{job.mode} · {job.status}</span><span className="recent-progress">{job.progress_pct}% reported</span></button>)}</div> : <div className="history-empty"><FolderGit2 size={19} /><p>Persisted investigations will appear here.</p></div>}
      </aside>
      </div>
    </div>
  );
}

/** True when backend evidence carries something worth inspecting ({}, null, and undefined hide the toggle). */
function hasEvidence(value: unknown): boolean {
  if (value == null) return false;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

/** Honest one-line meaning of a patch lifecycle status. */
function patchStatusNote(status: string): string {
  switch (status) {
    case "validated":
      return "Guarded: verified to apply cleanly to the pinned revision; not executed.";
    case "applied":
      return "Applied in the isolated verification workspace only — never to your machine.";
    default:
      return "Proposed: suggested by the model, not applied anywhere.";
  }
}

/** Markdown instructions for applying a unified diff by hand. */
function patchHowTo(filename: string, verified: boolean): string {
  return [
    "Open a terminal at your repository root — the same project and revision the investigation inspected.",
    "",
    "1. Make sure the working tree is clean:",
    "",
    "```bash",
    "git status",
    "```",
    "",
    "2. Confirm the patch applies cleanly:",
    "",
    "```bash",
    `git apply --check ${filename}`,
    "```",
    "",
    "3. Apply it:",
    "",
    "```bash",
    `git apply ${filename}`,
    "```",
    "",
    "4. Review every change before committing:",
    "",
    "```bash",
    "git diff",
    "```",
    "",
    "To reverse an applied patch:",
    "",
    "```bash",
    `git apply -R ${filename}`,
    "```",
    "",
    verified
      ? "This patch passed independent verification: the test suite was re-run with the patch applied in an isolated workspace. Review the diff before committing."
      : "This patch did NOT pass verification — treat it as a suggestion, inspect every hunk, and run your test suite before committing.",
    "",
    "Downloading never modifies your repositories.",
  ].join("\n");
}

function ZapIcon() { return <Activity size={18} aria-hidden="true" />; }

function EmptyResult({ title, text }: { title: string; text: string }) {
  return <div className="empty-result"><FolderGit2 size={21} /><strong>{title}</strong><p>{text}</p></div>;
}
