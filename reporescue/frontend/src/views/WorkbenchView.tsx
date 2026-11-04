import { useEffect, useRef, useState } from "react";
import {
  Activity, AlertTriangle, CircleHelp, Code2, Download, FileCode2, FolderGit2,
  LoaderCircle, Play, ShieldCheck,
} from "lucide-react";
import type { Session } from "@supabase/supabase-js";
import { ApiError, apiJson } from "../lib/api";
import { cancelJob, createJob, getJob, listJobs } from "../lib/jobs";
import { downloadInvestigationPatch } from "../lib/patches";
import type { IngestResult, InvestigationDetail, InvestigationMode, JobCard, Project } from "../lib/types";
import { isTerminalStatus } from "../lib/types";

const POLL_MS = 3000;
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

function DisplayValue({ value }: { value: unknown }) {
  if (value == null || value === "") return <span className="muted">Not recorded</span>;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return <>{String(value)}</>;
  return <pre className="output">{JSON.stringify(value, null, 2)}</pre>;
}

export default function WorkbenchView({
  session,
  onUnauthorized,
  restoredJobId = null,
  onJobAdopted = () => {},
  projects = [],
  projectsLoading = false,
  onNavigateProjects,
}: {
  session: Session | null;
  onUnauthorized: () => void;
  restoredJobId?: string | null;
  onJobAdopted?: () => void;
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
  const [jobDetail, setJobDetail] = useState<InvestigationDetail | null>(null);
  const [jobError, setJobError] = useState("");
  const [pendingJobId, setPendingJobId] = useState<string | null>(null);
  const [recentJobs, setRecentJobs] = useState<JobCard[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const [tab, setTab] = useState<Tab>("Overview");
  const [adopting, setAdopting] = useState(false);
  const submitting = useRef(false);
  const polling = useRef(false);
  const adopted = useRef<string | null>(null);

  const jobId = jobDetail?.job?.job_id ?? null;
  const active = Boolean(jobDetail && !isTerminalStatus(jobDetail.status));

  function authError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 401) {
      onUnauthorized();
      return "Your session expired. Sign in again to continue.";
    }
    return errorMessage(error, fallback);
  }

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

  useEffect(() => {
    if (!session || !restoredJobId || adopted.current === restoredJobId) return;
    adopted.current = restoredJobId;
    let live = true;
    setAdopting(true);
    void getJob(restoredJobId).then((detail) => {
      if (live) { setJobDetail(detail); setTab("Overview"); }
    }).catch((error: unknown) => {
      if (live) setJobError(authError(error, "Could not restore the active investigation."));
    }).finally(() => {
      if (live) { setAdopting(false); onJobAdopted(); }
    });
    return () => { live = false; };
  }, [session, restoredJobId]);

  useEffect(() => {
    if (!jobId || !active) return;
    let live = true;
    const timer = window.setInterval(() => {
      if (polling.current) return;
      polling.current = true;
      void getJob(jobId).then((next) => {
        if (live) { setJobDetail(next); setJobError(""); }
      }).catch((error: unknown) => {
        if (live) setJobError(authError(error, "Lost contact with the investigation."));
      }).finally(() => { polling.current = false; });
    }, POLL_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [jobId, active]);

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
      await downloadInvestigationPatch(jobDetail.investigation_id);
    } catch (error) {
      setDownloadError(authError(error, "Could not download the patch."));
    } finally { setDownloading(false); }
  }

  async function startInvestigation() {
    if (!ingestResult || submitting.current || investLoading) return;
    if (pendingJobId) {
      setJobError(`Job ${pendingJobId.slice(0, 8)} was accepted but its status has not loaded yet. Retry loading it before creating another investigation.`);
      return;
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
      });
      if (!created.job_id || !created.investigation_id) throw new Error("The API did not return an investigation ID.");
      setPendingJobId(created.job_id);
      void refreshRecent();
      try {
        const detail = await getJob(created.job_id);
        setJobDetail(detail);
        setPendingJobId(null);
        setJobError("");
        setTab("Overview");
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

  async function openJob(id: string) {
    setJobError("");
    setInvestError("");
    try { setJobDetail(await getJob(id)); setPendingJobId(null); setJobError(""); setTab("Overview"); }
    catch (error) { setJobError(authError(error, "Could not load this investigation.")); }
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

      <div className="workbench-start-grid">
      <div className="workbench-setup-main">
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
        <div className="mode-grid" role="radiogroup" aria-label="Investigation mode">{MODES.map(({ id, title, description, icon: Icon }, index) => <button key={id} type="button" role="radio" tabIndex={mode === id ? 0 : -1} aria-checked={mode === id} className={`mode-card${mode === id ? " selected" : ""}`} onKeyDown={(event) => { if (event.key === "ArrowRight" || event.key === "ArrowDown" || event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1; const next = MODES[(index + step + MODES.length) % MODES.length]; setMode(next.id); setInvestError(""); } }} onClick={() => { setMode(id); setInvestError(""); }}><Icon size={18} /><span><strong>{title}</strong><small>{description}</small></span>{id === "general" && <span className="mode-default">DEFAULT</span>}</button>)}</div>
        <div className="field objective-field"><label className="label" htmlFor="investigation-objective">Problem statement or objective</label><textarea id="investigation-objective" className="input objective-input" rows={3} maxLength={2000} placeholder={mode === "debug" ? "Describe the failure, expected behavior, and any useful context…" : mode === "explain" ? "What part of the repository should be explained?" : mode === "health" ? "Which health concerns should be assessed?" : "Describe what you want to understand or investigate…"} value={userMessage} onChange={(event) => setUserMessage(event.target.value)} /></div>
        <div className="setup-actions"><p className="hint">Inspection pins this repository snapshot. The queued job uses that snapshot and remains available after refresh.</p><button type="button" className="btn btn-primary" onClick={() => void startInvestigation()} disabled={investLoading}>{investLoading ? <LoaderCircle size={16} className="spin" /> : <Play size={16} />}{investLoading ? "Starting…" : "Start investigation"}</button></div>
        {!ingestResult.profile.supported && <p className="capability-note" role="status">The API marked this repository as outside the currently supported execution profile. The backend determines whether the selected analysis can proceed.</p>}
        {investError && <div role="alert" className="banner banner-error"><AlertTriangle size={17} /><div><strong>Investigation not started</strong><p>{investError}</p></div></div>}
      </section>}

      </div>
      <aside className="recent-section history-rail" aria-labelledby="history-title">
        <div className="section-heading"><div><p className="eyebrow">SAVED WORK</p><h3 id="history-title">Recent investigations</h3></div></div>
        {recentLoading ? <div className="loading-block" role="status"><LoaderCircle size={16} className="spin" /> Loading investigations…</div> : recentJobs.length > 0 ? <div className="recent-list">{recentJobs.map((job) => <button key={job.job_id} type="button" className={`recent-item${job.job_id === jobId ? " recent-active" : ""}`} onClick={() => void openJob(job.job_id)}><span className="recent-repo">{job.repo_name}</span><span className="recent-meta">{job.mode} · {job.status}</span><span className="recent-progress">{job.progress_pct}% reported</span></button>)}</div> : <div className="history-empty"><FolderGit2 size={19} /><p>Persisted investigations will appear here.</p></div>}
      </aside>
      </div>

      {(jobError || adopting) && <div className={jobError ? "banner banner-error" : "banner banner-info"} role={jobError ? "alert" : "status"}>{jobError ? <AlertTriangle size={17} /> : <LoaderCircle size={17} className="spin" />}<div>{jobError || "Restoring your active investigation…"}</div>{jobError && <button type="button" className="btn btn-secondary btn-sm" onClick={() => jobId ? void openJob(jobId) : pendingJobId ? void openJob(pendingJobId) : restoredJobId ? void openJob(restoredJobId) : void refreshRecent()}>Retry</button>}</div>}

      {jobDetail && <section className="card workbench-results" aria-label="Investigation results">
        <header className="investigation-header"><div><p className="eyebrow">{jobDetail.mode === "general" && jobDetail.selected_mode ? `GENERAL \u2192 ${jobDetail.selected_mode.toUpperCase()} INVESTIGATION` : `${jobDetail.mode.toUpperCase()} INVESTIGATION`}</p><h3>{jobDetail.repo_name}</h3><p className="investigation-objective">{jobDetail.user_message || "No objective was recorded for this investigation."}</p><div className="context-meta"><span>Revision <code>{jobDetail.commit_sha.slice(0, 7)}</code></span>{jobDetail.ref && <span>Ref {jobDetail.ref}</span>}<span>Job {jobDetail.job?.job_id.slice(0, 8)}</span></div></div><div className="investigation-actions"><span className={pillKind(jobDetail.status)}>{jobDetail.status.toUpperCase()}</span>{active && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void stopJob()} disabled={cancelling}>{cancelling ? "Cancelling…" : "Cancel investigation"}</button>}</div></header>
        {active && <div className="job-progress"><div className="progress-label"><span>{jobDetail.stage || jobDetail.status}</span><span>{jobDetail.progress_pct}% reported by API</span></div><progress value={jobDetail.progress_pct} max={100} aria-label={`Investigation progress ${jobDetail.progress_pct}%`} /></div>}
        {jobDetail.error && <div className="banner banner-error" role="status"><AlertTriangle size={17} /><div><strong>{jobDetail.error.reason}</strong><p>{jobDetail.error.message}</p></div></div>}
        <div className="workbench-tabs" role="tablist" aria-label="Investigation details" onKeyDown={(event) => { if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return; event.preventDefault(); const current = TABS.indexOf(tab); const next = event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length; setTab(TABS[next]); document.getElementById(`tab-${TABS[next].toLowerCase()}`)?.focus(); }}>{TABS.map((item) => <button key={item} id={`tab-${item.toLowerCase()}`} type="button" role="tab" tabIndex={tab === item ? 0 : -1} aria-selected={tab === item} aria-controls="workbench-panel" className={`workbench-tab${tab === item ? " active" : ""}`} onClick={() => setTab(item)}>{item}{item === "Findings" && <span>{jobDetail.findings.length}</span>}</button>)}</div>
        <div id="workbench-panel" role="tabpanel" aria-labelledby={`tab-${tab.toLowerCase()}`} className="workbench-panel">
          {tab === "Overview" && <div className="overview-grid"><div className="overview-summary"><p className="eyebrow">INVESTIGATION SUMMARY</p>{summary ? <p>{summary}</p> : diagnosis ? <p>{diagnosis}</p> : <p className="muted">The backend has not returned a final assessment yet.</p>}{jobDetail.limitations.length > 0 && <ul className="limitation-list">{jobDetail.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>}</div><div className="overview-facts"><div><span>Stage</span><strong>{jobDetail.stage || "Not reported"}</strong></div><div><span>Verification</span><strong className={pillKind(jobDetail.verification_status)}>{jobDetail.verification_status}</strong></div><div><span>Execution runs</span><strong>{jobDetail.runs.length}</strong></div><div><span>Proposed patches</span><strong>{jobDetail.patches.length}</strong></div></div>{jobDetail.routing && <div className="routing-note"><p className="eyebrow">GENERAL ROUTING</p><strong>Selected analysis: {jobDetail.routing.selected_mode}</strong><p>{jobDetail.routing.rationale}</p><span>Routing confidence: {jobDetail.routing.confidence}</span></div>}{diagnosis && summary && <div className="assessment-note"><ZapIcon /><div><strong>AI assessment</strong><p>{diagnosis}</p></div></div>}</div>}
          {tab === "Findings" && <div className="result-list">{jobDetail.findings.length ? jobDetail.findings.map((finding, index) => <article key={`${finding.title}-${index}`} className="result-item"><div className="result-item-heading"><span className={pillKind(finding.kind)}>{finding.kind}</span>{finding.category && <span className="muted">{finding.category}</span>}<span className="confidence">{finding.confidence} confidence</span></div><h4>{finding.title}</h4>{finding.detail && <p>{finding.detail}</p>}{finding.evidence != null && <details><summary>Inspect finding evidence</summary><DisplayValue value={finding.evidence} /></details>}</article>) : <EmptyResult title="No findings returned" text="Findings will appear here when they are present in the persisted investigation response." />}</div>}
          {tab === "Evidence" && <div className="result-list">{jobDetail.findings.some((finding) => finding.evidence != null) && jobDetail.findings.filter((finding) => finding.evidence != null).map((finding, index) => <article key={`evidence-${index}`} className="result-item"><h4>{finding.title}</h4><DisplayValue value={finding.evidence} /></article>)}{jobDetail.runs.map((run, index) => <article key={`run-evidence-${index}`} className="result-item"><h4>{run.kind} output</h4>{run.failures.map((failure, failureIndex) => <details key={failureIndex}><summary>{failure.test} · {failure.kind}</summary><pre className="output">{`${failure.message}\n${failure.traceback}`}</pre></details>)}{!run.failures.length && !run.output && <p className="muted">No execution evidence was recorded for this run.</p>}</article>)}{!jobDetail.findings.some((finding) => finding.evidence != null) && !jobDetail.runs.length && <EmptyResult title="No evidence recorded" text="Only repository and execution evidence returned by the backend is shown here." />}</div>}
          {tab === "Execution" && <div className="result-list">{jobDetail.runs.length ? jobDetail.runs.map((run, index) => <article key={`${run.kind}-${index}`} className="execution-card"><div className="result-item-heading"><h4>{run.kind}</h4><span className={pillKind(run.passed === true ? "passed" : run.passed === false ? "failed" : run.status || "not_run")}>{run.passed === true ? "tests passed" : run.passed === false ? "tests failed" : run.status || "not run"}</span></div><dl className="execution-meta"><div><dt>Provider</dt><dd>{run.backend || "Not reported"}</dd></div><div><dt>Isolation</dt><dd>{run.isolation || "Not reported"}</dd></div><div><dt>Exit code</dt><dd>{run.exit_code ?? "Not reported"}</dd></div><div><dt>Duration</dt><dd>{run.duration_ms == null ? "Not reported" : `${run.duration_ms} ms`}</dd></div></dl>{run.install_ok != null && <p className="hint">Dependency installation: {run.install_ok ? "completed" : "failed"}</p>}{run.install_log && <details><summary>Dependency installation output</summary><pre className="output">{run.install_log}</pre></details>}{run.output && <details open><summary>Captured execution output</summary><pre className="output">{run.output}</pre></details>}{run.truncated && <p className="truncation-note" role="status">The backend truncated this execution output.</p>}{run.failures.map((failure, i) => <details key={i}><summary>{failure.test} · {failure.kind}</summary><pre className="output">{`${failure.message}\n${failure.traceback}`}</pre></details>)}</article>) : <EmptyResult title="No execution run recorded" text="This investigation has no execution output. Static analysis does not imply that code was executed." />}</div>}
          {tab === "Changes" && <div className="result-list">{jobDetail.patches.some((patch) => patch.status === "applied") && <article className="result-item"><div className="result-item-heading"><h4>Guarded patch file</h4><span className={pillKind(jobDetail.verification_status === "passed" ? "verified" : "unverified")}>{jobDetail.verification_status === "passed" ? "verified" : "unverified"}</span></div><p className="hint">{jobDetail.verification_status === "passed" ? "Unified diff of the guarded patch for this investigation. Downloading does not apply it." : "Verification did not pass. This file is provided for inspection only; downloading does not apply it."}</p><button type="button" className="btn btn-secondary btn-sm" onClick={() => void downloadPatch()} disabled={downloading}>{downloading ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}{downloading ? "Preparing\u2026" : "Download .patch"}</button>{downloadError && <div role="alert" className="banner banner-error"><AlertTriangle size={17} /><div><p>{downloadError}</p></div></div>}</article>}{jobDetail.patches.length ? jobDetail.patches.map((patch, index) => <article key={index} className="result-item"><div className="result-item-heading"><h4>Patch proposal {index + 1}</h4><span className={pillKind(patch.status)}>{patch.status}</span></div>{patch.reason && <p>{patch.reason}</p>}{patch.files.map((file, fileIndex) => <div key={fileIndex} className="diff-block"><div className="file-tab"><FileCode2 size={14} /><code>{file.file}</code></div><pre className="diff removed">{file.before || "No before content returned"}</pre><pre className="diff added">{file.after || "No after content returned"}</pre></div>)}<p className="hint">{jobDetail.verification_status === "passed" ? "The backend reports passing verification for this investigation." : `Verification is ${jobDetail.verification_status}; this patch has not been confirmed as a fix.`}</p></article>) : <EmptyResult title="No patch returned" text="The backend has not recorded a proposed change for this investigation." />}</div>}
          {tab === "Conversation" && <div className="conversation-history"><p className="hint">Persisted conversation history from the investigation response. Sending follow-up messages is not available on the current backend contract.</p>{jobDetail.messages.length ? jobDetail.messages.map((message, index) => <article className={`message-entry ${message.role}`} key={`${index}-${message.role}`}><span>{message.role}</span><p>{message.content}</p></article>) : <EmptyResult title="No conversation messages" text="The investigation detail currently contains no persisted messages." />}</div>}
        </div>
      </section>}
    </div>
  );
}

function ZapIcon() { return <Activity size={18} aria-hidden="true" />; }

function EmptyResult({ title, text }: { title: string; text: string }) {
  return <div className="empty-result"><FolderGit2 size={21} /><strong>{title}</strong><p>{text}</p></div>;
}
