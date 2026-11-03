import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  AlertTriangle, Check, ChevronDown,
  FlaskConical, FolderGit2, GitPullRequest, LoaderCircle,
  Play, ShieldAlert, ShieldCheck, Terminal, XCircle, Zap
} from "lucide-react";
import type { Session } from "@supabase/supabase-js";
import { ApiError, apiJson } from "../lib/api";
import { cancelJob, createJob, getJob, listJobs } from "../lib/jobs";
import CapabilityPanel from "../components/CapabilityPanel";
import ModeCards from "../components/ModeCards";
import ConversationPanel from "../components/ConversationPanel";
import type {
  ExecFailure, IngestResult, InvestigationDetail, InvestigationMode, JobCard,
  RealReport, RoutingInfo,
} from "../lib/types";
import { isTerminalStatus } from "../lib/types";

type StaticResult = {
  mode: InvestigationMode;
  summary: string;
  overall?: string;
  checks?: { name: string; status: string; detail?: string }[];
  limitations?: string[];
};

const POLL_MS = 3000;

type WorkbenchTab = "overview" | "findings" | "evidence" | "execution" | "changes" | "conversation";

const TABS: { id: WorkbenchTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "findings", label: "Findings" },
  { id: "evidence", label: "Evidence" },
  { id: "execution", label: "Execution" },
  { id: "changes", label: "Changes" },
  { id: "conversation", label: "Conversation" },
];

/** Job-status pill: active is blue, completed green, failed red, rest neutral. */
function jobStatusPill(status: string): string {
  if (status === "completed") return "pill green";
  if (status === "failed") return "pill red";
  if (status === "cancelled") return "pill";
  return "pill blue";
}

function runOutcomePill(status: string | null, passed: boolean | null): string {
  if (status === "timeout" || status === "cancelled" || status === "install_error" || status === "error") return "pill amber";
  if (status === "test_failure") return "pill red";
  if (status === "ok") return passed === true ? "pill green" : "pill amber";
  if (passed === true) return "pill green";
  if (passed === false) return "pill red";
  return "pill";
}

function runOutcomeLabel(status: string | null, passed: boolean | null): string {
  if (status === "timeout") return "timed out";
  if (status === "cancelled") return "cancelled";
  if (status === "test_failure") return "test failure";
  if (status === "install_error") return "setup failed";
  if (status === "error") return "execution error";
  if (status === "unsupported" || status === "not_available" || status === "unavailable") return "not available";
  if (status === "not_run") return "not run";
  if (status === "static_only" || status === "unverified") return "unverified · static-only";
  if (status === "ok") return passed === true ? "passed" : "outcome unclear";
  if (passed === true) return "passed";
  if (passed === false) return "failed";
  return status ?? "not recorded";
}

/** Finding-kind pill: facts green, hypotheses amber, failed checks red. */
function findingKindPill(kind: string): string {
  if (kind === "fact" || kind === "confirmed") return "pill green";
  if (kind === "hypothesis") return "pill amber";
  if (kind === "failed check" || kind === "failed") return "pill red";
  return "pill";
}

/** One execution run: kind, outcome, backend details, failures, output toggle. */
function RunCard({
  storageKey,
  kind,
  backend,
  isolation,
  status,
  passed,
  exitCode,
  output,
  durationMs,
  failures,
  installOk,
  installLog,
  outputTruncated = false,
  open,
  onToggle,
}: {
  storageKey: string;
  kind: string;
  backend: string | null;
  isolation?: string | null;
  status: string | null;
  passed: boolean | null;
  exitCode: number | null;
  output: string | null;
  durationMs: number | null;
  failures: ExecFailure[];
  installOk?: boolean | null;
  installLog?: string | null;
  outputTruncated?: boolean;
  open: boolean;
  onToggle: (key: string) => void;
}) {
  const details = [
    backend,
    isolation ?? null,
    exitCode === null ? null : `exit ${exitCode}`,
    durationMs === null ? null : `${durationMs}ms`,
  ].filter((part): part is string => part !== null);
  return (
    <div>
      <div className="card-head">
        <h3>{kind}</h3>
        <span className={runOutcomePill(status, passed)}>{runOutcomeLabel(status, passed)}</span>
      </div>
      {details.length > 0 && <p className="hint mono">{details.join(" · ")}</p>}
      {failures.length > 0 && (
        <p className="mono">
          {failures.map((failure) => `${failure.test} — ${failure.message}`).join("; ")}
        </p>
      )}
      {installOk === false && (
        <div>
          <p className="hint">Dependency install failed.</p>
          {installLog ? <pre className="output">{installLog}</pre> : null}
        </div>
      )}
      {outputTruncated && <p className="hint" role="status">Output was truncated while it was collected; the retained tail is shown.</p>}
      {output !== null && output !== "" && (
        <div>
          <button className="btn btn-secondary btn-sm" onClick={() => onToggle(storageKey)}>
            <Terminal size={14} /> {open ? "Hide output" : "Show output"}{" "}
            <ChevronDown size={14} className={open ? "rotate" : ""} />
          </button>
          {open && <pre className="output">{output}</pre>}
        </div>
      )}
    </div>
  );
}

/** Real repository investigations: inspect, start, follow, and reopen. */
export default function WorkbenchView({
  session,
  onUnauthorized,
  restoredJobId = null,
  onJobAdopted = () => {},
  onNewInvestigation,
}: {
  session: Session | null;
  onUnauthorized: () => void;
  /** Backend-owned active job to restore (set once by App on refresh). */
  restoredJobId?: string | null;
  /** Called after the restored job is adopted so App clears it. */
  onJobAdopted?: () => void;
  /** Open the guided New Investigation flow (provided by App). */
  onNewInvestigation?: () => void;
}) {
  const [repoUrl, setRepoUrl] = useState("");
  const [repoRef, setRepoRef] = useState("");
  const [ingestLoading, setIngestLoading] = useState(false);
  const [ingestError, setIngestError] = useState("");
  const [ingestResult, setIngestResult] = useState<IngestResult | null>(null);
  const [investLoading, setInvestLoading] = useState(false);
  const [investError, setInvestError] = useState("");
  const [investReport, setInvestReport] = useState<RealReport | null>(null);

  const [mode, setMode] = useState<InvestigationMode>("general");
  const [userMessage, setUserMessage] = useState("");
  const [jobDetail, setJobDetail] = useState<InvestigationDetail | null>(null);
  const [jobError, setJobError] = useState("");
  const [staticResult, setStaticResult] = useState<StaticResult | null>(null);
  const [routing, setRouting] = useState<RoutingInfo | null>(null);
  const [recentJobs, setRecentJobs] = useState<JobCard[]>([]);
  const [cancelling, setCancelling] = useState(false);
  const [tab, setTab] = useState<WorkbenchTab>("overview");
  const [settledDetail, setSettledDetail] = useState<InvestigationDetail | null>(null);
  const [openOutput, setOpenOutput] = useState<string | null>(null);
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const adoptedRef = useRef(false);
  const settledRef = useRef<string | null>(null);

  const jobId = jobDetail?.job?.job_id ?? null;
  const jobStatus = jobDetail?.status ?? null;

  function handleApiError(err: unknown, fallback: string): string {
    if (err instanceof ApiError && err.status === 401) {
      onUnauthorized();
      return "Session expired — you have been signed out. Please sign in again.";
    }
    return err instanceof Error ? err.message : fallback;
  }

  /** Terminal handoff: render the persisted outcome without restarting. */
  function settle(detail: InvestigationDetail) {
    const id = detail.job?.job_id;
    if (!id || settledRef.current === id) return;
    settledRef.current = id;
    setJobDetail(null);
    setSettledDetail(detail);
    const routed = (detail.final_report as { routing?: RoutingInfo } | null)?.routing;
    setRouting(routed && typeof routed.explanation === "string" ? routed : null);
    if (detail.status === "completed" && detail.final_report) {
      if (detail.mode === "debug") {
        setInvestReport(detail.final_report as unknown as RealReport);
      } else {
        const summary = detail.final_report["summary"];
        const overall = detail.final_report["overall"];
        const checks = detail.final_report["checks"];
        const limitations = detail.final_report["limitations"];
        setStaticResult({
          mode: detail.mode,
          summary: typeof summary === "string" ? summary : "Assessment complete.",
          overall: typeof overall === "string" ? overall : undefined,
          checks: Array.isArray(checks) ? checks.filter((item): item is { name: string; status: string; detail?: string } => Boolean(item && typeof item.name === "string" && typeof item.status === "string")) : undefined,
          limitations: Array.isArray(limitations) ? limitations.filter((item): item is string => typeof item === "string") : undefined,
        });
      }
    } else if (detail.status === "failed") {
      setInvestError(detail.error?.message || "Investigation failed.");
    } else if (detail.status === "cancelled") {
      setInvestError("Investigation was cancelled.");
    }
    void refreshRecent();
  }

  async function refreshRecent() {
    if (!session) return;
    try {
      setRecentJobs(await listJobs(10));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized();
    }
  }

  // Recent investigations for this account (read-only; never creates).
  useEffect(() => {
    void refreshRecent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  // Adopt the backend-owned active job once after a refresh/restart.
  useEffect(() => {
    if (!session || !restoredJobId || adoptedRef.current) return;
    adoptedRef.current = true;
    onJobAdopted();
    void (async () => {
      try {
        const detail = await getJob(restoredJobId);
        if (isTerminalStatus(detail.status)) settle(detail);
        else setJobDetail(detail);
      } catch (err) {
        setJobError(handleApiError(err, "Could not restore the investigation."));
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, restoredJobId]);

  // Follow the active job until it settles.
  useEffect(() => {
    if (!jobId || !jobStatus || isTerminalStatus(jobStatus)) return;
    const timer = setInterval(() => {
      void (async () => {
        try {
          const next = await getJob(jobId);
          if (isTerminalStatus(next.status)) settle(next);
          else setJobDetail(next);
        } catch (err) {
          setJobError(handleApiError(err, "Lost contact with the job."));
        }
      })();
    }, POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, jobStatus]);

  async function connectRepository() {
    setIngestLoading(true);
    setIngestError("");
    setIngestResult(null);
    setStaticResult(null);
    setRouting(null);
    setSettledDetail(null);
    setInvestError("");
    setInvestReport(null);
    try {
      const data = await apiJson<IngestResult & { message?: string }>("/api/repos/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: repoUrl.trim(), ref: repoRef.trim() || null }),
      });
      if (data.status !== "complete") throw new Error(data.message || "Repository ingestion failed.");
      setIngestResult(data);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        setIngestError("Session expired — you have been signed out. Please sign in again.");
      } else {
        setIngestError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
      }
    } finally {
      setIngestLoading(false);
    }
  }

  /** Follow one job: poll while active, hand off when terminal. */
  async function followJob(id: string) {
    settledRef.current = null;
    setSettledDetail(null);
    setJobError("");
    setInvestError("");
    setInvestReport(null);
    setStaticResult(null);
    setRouting(null);
    try {
      const detail = await getJob(id);
      if (isTerminalStatus(detail.status)) settle(detail);
      else setJobDetail(detail);
    } catch (err) {
      setJobError(handleApiError(err, "Could not reach the RepoRescue API."));
    }
  }

  async function startRealInvestigation() {
    if (!ingestResult) return;
    setInvestLoading(true);
    setSettledDetail(null);
    setInvestError("");
    setInvestReport(null);
    setStaticResult(null);
    setRouting(null);
    setJobError("");
    try {
      const created = await createJob({
        ingestion_id: ingestResult.ingestion_id,
        mode,
        message: userMessage,
      });
      const detail = await getJob(created.job_id);
      if (isTerminalStatus(detail.status)) settle(detail);
      else setJobDetail(detail);
      void refreshRecent();
    } catch (err) {
      setInvestError(handleApiError(err, "Could not reach the RepoRescue API."));
    } finally {
      setInvestLoading(false);
    }
  }

  async function cancelActiveJob() {
    if (!jobId) return;
    setCancelling(true);
    try {
      settle(await cancelJob(jobId));
    } catch (err) {
      setJobError(handleApiError(err, "Could not reach the RepoRescue API."));
    } finally {
      setCancelling(false);
    }
  }

  const realVerified = investReport?.verdict === "TESTS_PASSED";

  const subject = jobDetail ?? settledDetail;
  const subjectActive = subject ? !isTerminalStatus(subject.status) : false;
  const showResults = subject !== null;
  const runFailures = subject ? subject.runs.flatMap((run) => run.failures) : [];
  const latestAttempt =
    subject && subject.attempts.length > 0
      ? subject.attempts[subject.attempts.length - 1]
      : null;

  function selectTab(next: WorkbenchTab) {
    setTab(next);
    setOpenOutput(null);
  }

  function onTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const order = TABS.map((entry) => entry.id);
    const current = order.indexOf(tab);
    let next: number | null = null;
    if (event.key === "ArrowRight") next = (current + 1) % order.length;
    else if (event.key === "ArrowLeft") next = (current - 1 + order.length) % order.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = order.length - 1;
    if (next === null) return;
    event.preventDefault();
    setTab(order[next]);
    tabRefs.current[next]?.focus();
  }

  function toggleOutput(key: string) {
    setOpenOutput((current) => (current === key ? null : key));
  }

  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Workbench</h2>
          <p>Inspect a real repository, then run a background investigation.</p>
        </div>
        {onNewInvestigation && (
          <button className="btn btn-primary" onClick={onNewInvestigation}>
            <FlaskConical size={15} /> New investigation
          </button>
        )}
      </div>

      {session ? (
        <>
        <section className="card">
          <div className="card-head">
            <h3><FolderGit2 size={16} /> Inspect a GitHub repository</h3>
            <span className="caption">BACKGROUND JOBS</span>
          </div>
          {jobError && <div className="banner banner-error"><AlertTriangle size={17} /><div><strong>Job update failed</strong><p>{jobError}</p></div></div>}
          {jobDetail && jobDetail.job && <>
            <div className="job-card">
              <div className="job-head">
                <span className="row-icon"><LoaderCircle size={16} className={isTerminalStatus(jobDetail.status) ? "" : "spin"} /></span>
                <div className="row-main">
                  <strong>{jobDetail.repo_name}</strong>
                  <small>{jobDetail.mode} · {jobDetail.stage ? `stage: ${jobDetail.stage}` : jobDetail.status}{jobDetail.stages_completed.length > 0 ? ` · done: ${jobDetail.stages_completed.join(", ")}` : ""}</small>
                </div>
                <span className="pill">{jobDetail.status.toUpperCase()} · {jobDetail.progress_pct}%</span>
              </div>
              <div className="progress-track"><div className="progress-fill" style={{ width: `${jobDetail.progress_pct}%` }} /></div>
              <div>
                <button className="btn btn-secondary btn-sm" onClick={() => void cancelActiveJob()} disabled={cancelling}>
                  {cancelling ? "Cancelling…" : "Cancel investigation"}
                </button>
                <p className="hint">Cancellation is cooperative. An active install, test, or provider request may finish or reach its timeout before cancellation settles.</p>
              </div>
            </div>
          </>}
          {recentJobs.length > 0 && <>
            <div className="recent-head"><span className="caption">RECENT INVESTIGATIONS</span></div>
            <div className="recent-list">
              {recentJobs.map((job) => (
                <button
                  key={job.job_id}
                  type="button"
                  className={`recent-item${job.job_id === jobId ? " recent-active" : ""}`}
                  onClick={() => void followJob(job.job_id)}
                >
                  <span className="recent-repo">{job.repo_name}</span>
                  <span className="recent-meta">{job.mode} · {job.status.toUpperCase()} · {job.progress_pct}%</span>
                </button>
              ))}
            </div>
          </>}
          <div className="form-row">
            <input
              className="input input-grow"
              placeholder="https://github.com/owner/repo"
              value={repoUrl}
              onChange={(event) => setRepoUrl(event.target.value)}
              aria-label="GitHub repository URL"
            />
            <input
              className="input input-ref"
              placeholder="branch, tag, or commit (optional)"
              value={repoRef}
              onChange={(event) => setRepoRef(event.target.value)}
              aria-label="Branch, tag, or commit"
            />
            <button className="btn btn-primary" onClick={() => void connectRepository()} disabled={ingestLoading || !repoUrl.trim()}>
              {ingestLoading ? <LoaderCircle className="spin" size={16} /> : <GitPullRequest size={15} />}
              {ingestLoading ? "Cloning…" : "Inspect repository"}
            </button>
          </div>
          {ingestError && <div className="banner banner-error"><AlertTriangle size={17} /><div><strong>Could not inspect repository</strong><p>{ingestError}</p></div></div>}
          {ingestResult && <>
            <CapabilityPanel profile={ingestResult.profile} warnings={ingestResult.warnings} />
            <span className="caption">INVESTIGATION MODE</span>
            <ModeCards value={mode} onChange={setMode} />
            <div className="form-row">
              <input
                className="input input-grow"
                placeholder="What should we look into? (optional)"
                value={userMessage}
                onChange={(event) => setUserMessage(event.target.value)}
                aria-label="Problem statement"
              />
            </div>
            <div>
              <button className="btn btn-primary" onClick={() => void startRealInvestigation()} disabled={investLoading}>
                {investLoading ? <LoaderCircle className="spin" size={16} /> : <Play size={15} />}
                {investLoading ? "Starting…" : "Start background investigation"}
              </button>
            </div>
            {investLoading && <div className="loading-note"><LoaderCircle size={14} className="spin" /> Queueing the investigation — it keeps running if you refresh or leave…</div>}
          </>}
          {investError && <div className="banner banner-error"><AlertTriangle size={17} /><div><strong>Could not complete real investigation</strong><p>{investError}</p></div></div>}



        </section>
      {session && showResults && subject && (
        <section className="card" aria-label="Investigation results">
          <div
            className="tabs"
            role="tablist"
            aria-label="Investigation sections"
            onKeyDown={onTabKeyDown}
          >
            {TABS.map((entry, index) => (
              <button
                key={entry.id}
                ref={(node) => {
                  tabRefs.current[index] = node;
                }}
                type="button"
                role="tab"
                id={`tab-${entry.id}`}
                aria-selected={tab === entry.id}
                aria-controls={`panel-${entry.id}`}
                tabIndex={tab === entry.id ? 0 : -1}
                className={`tab${tab === entry.id ? " active" : ""}`}
                onClick={() => selectTab(entry.id)}
              >
                {entry.label}
              </button>
            ))}
          </div>
          {tab === "overview" && subject && (
            <div role="tabpanel" id="panel-overview" aria-labelledby="tab-overview">
              <div className="card-head">
                <h3>
                  {subject.repo_name}{" "}
                  <span className="mono muted">{subject.commit_sha.slice(0, 7)}</span>
                </h3>
                <span className={jobStatusPill(subject.status)}>
                  {subject.status} · {subject.progress_pct}%
                </span>
              </div>
              <div className="progress-track">
                <div className="progress-fill" style={{ width: `${subject.progress_pct}%` }} />
              </div>
              {subject.user_message && (
                <div className="banner banner-info">
                  <div>
                    <strong>Investigating: “{subject.user_message}”</strong>
                  </div>
                </div>
              )}
              {routing && (
                <div className="banner banner-info">
                  <Zap size={17} />
                  <div>
                    <strong>General routing{routing.mode ? ` → ${routing.mode}` : ""}</strong>
                    <p>
                      {routing.explanation}
                      {routing.confidence ? ` (confidence: ${routing.confidence})` : ""}
                    </p>
                  </div>
                </div>
              )}
              {investReport && (
                <div
                  className={`verdict-banner ${realVerified ? "verdict-banner-good" : "verdict-banner-warn"}`}
                >
                  {realVerified ? <ShieldCheck size={21} /> : <ShieldAlert size={21} />}
                  <div>
                    <strong>{realVerified ? "Test-backed fix" : "Inconclusive result"}</strong>
                    <p>{investReport.summary}</p>
                  </div>
                </div>
              )}
              {staticResult && (
                <div className="verdict-banner verdict-banner-warn">
                  <ShieldAlert size={21} />
                  <div>
                    <strong>
                      {staticResult.mode === "explain" ? "AI interpretation · static context" : "Health summary · AI interpretation"}
                      {staticResult.overall ? ` — ${staticResult.overall}` : ""}
                    </strong>
                    <p>{staticResult.summary}</p>
                    {staticResult.mode === "explain" && <p className="hint">Repository reading only; behavior was not executed or proven.</p>}
                    {staticResult.mode === "health" && staticResult.checks && (
                      <div aria-label="Deterministic health checks">
                        <strong>Deterministic observations</strong>
                        {staticResult.checks.map((check, index) => <p key={`${check.name}-${index}`}><span className="pill">{check.status}</span> {check.name}{check.detail ? ` — ${check.detail}` : ""}</p>)}
                      </div>
                    )}
                    {staticResult.limitations?.map((limitation) => <p className="hint" key={limitation}>{limitation}</p>)}
                  </div>
                </div>
              )}
              <dl className="kv-list">
                <div className="kv-row">
                  <dt>Repository</dt>
                  <dd className="mono">{subject.repo_url}</dd>
                </div>
                <div className="kv-row">
                  <dt>Mode</dt>
                  <dd>{subject.mode}</dd>
                </div>
                <div className="kv-row">
                  <dt>Status</dt>
                  <dd>{subject.status}</dd>
                </div>
                <div className="kv-row">
                  <dt>Stage</dt>
                  <dd>
                    {subject.stage ?? subject.status}
                    {subject.stages_completed.length > 0
                      ? ` · done: ${subject.stages_completed.join(", ")}`
                      : ""}
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>Problem statement</dt>
                  <dd>{subject.user_message ?? "—"}</dd>
                </div>
                {subject.error && (
                  <div className="kv-row">
                    <dt>Error</dt>
                    <dd>
                      {subject.error.reason} — {subject.error.message}
                    </dd>
                  </div>
                )}
                <div className="kv-row">
                  <dt>Verification</dt>
                  <dd>{subject.verification_status}</dd>
                </div>
                <div className="kv-row">
                  <dt>Limitations</dt>
                  <dd>
                    {subject.limitations.length > 0
                      ? subject.limitations.join("; ")
                      : "None recorded"}
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>Updated</dt>
                  <dd>{subject.updated_at ?? subject.created_at ?? "—"}</dd>
                </div>
                {isTerminalStatus(subject.status) && (
                  <div className="kv-row">
                    <dt>Completed</dt>
                    <dd>{subject.completed_at ?? "—"}</dd>
                  </div>
                )}
              </dl>
              {subjectActive && (
                <div>
                  <h3>So far</h3>
                  <dl className="kv-list">
                    <div className="kv-row">
                      <dt>Failing tests</dt>
                      <dd className="mono">
                        {runFailures.length > 0
                          ? runFailures.map((failure) => failure.test).join(", ")
                          : "None recorded yet"}
                      </dd>
                    </div>
                    <div className="kv-row">
                      <dt>Diagnosis</dt>
                      <dd>
                        {latestAttempt?.diagnosis ?? "Not yet available"}{" "}
                        {latestAttempt?.diagnosis && (
                          <span className="muted">({latestAttempt.confidence})</span>
                        )}
                      </dd>
                    </div>
                    <div className="kv-row">
                      <dt>Verification</dt>
                      <dd>
                        {subject.verification_status}
                        {subject.patches.length === 0 ? " · no patches proposed yet" : ""}
                      </dd>
                    </div>
                  </dl>
                </div>
              )}
            </div>
          )}
          {tab === "findings" && subject && (
            <div role="tabpanel" id="panel-findings" aria-labelledby="tab-findings">
              {investReport?.diagnosis && (
                <div className="finding">
                  <div className="frow">
                    <span className="pill">diagnosis</span>
                  </div>
                  <strong>AI diagnosis</strong>
                  <p>{investReport.diagnosis}</p>
                </div>
              )}
              {staticResult && (
                <div className="finding">
                  <div className="frow">
                    <span className="pill">{staticResult.mode}</span>
                    {staticResult.overall && (
                      <span className="pill amber">{staticResult.overall}</span>
                    )}
                  </div>
                  <strong>
                    {staticResult.mode === "explain" ? "AI interpretation (not runtime verified)" : "AI health interpretation"}
                  </strong>
                  <p>{staticResult.summary}</p>
                </div>
              )}
              {subject.findings.map((finding, index) => (
                <div key={`${finding.kind}-${finding.title}-${index}`} className="finding">
                  <div className="frow">
                    <span className={findingKindPill(finding.kind)}>{finding.kind}</span>
                    <span className="pill">{finding.confidence} confidence</span>
                    {finding.category && <span className="muted">{finding.category}</span>}
                  </div>
                  <strong>{finding.title}</strong>
                  {finding.detail && <p>{finding.detail}</p>}
                </div>
              ))}
              {!investReport?.diagnosis && !staticResult && subject.findings.length === 0 && (
                <div className="empty-state">
                  <strong>No findings yet</strong>
                  <p>
                    {subjectActive
                      ? "Findings will appear as the investigation progresses."
                      : "The backend recorded no findings."}
                  </p>
                </div>
              )}
            </div>
          )}
          {tab === "evidence" && subject && (
            <div role="tabpanel" id="panel-evidence" aria-labelledby="tab-evidence">
              <p className="hint">
                Every reference below comes from the persisted investigation result. Line
                numbers appear only where the backend recorded them.
              </p>
              {subject.runs.map((run, runIndex) =>
                run.failures.map((failure, failureIndex) => (
                  <div key={`run-${runIndex}-${failureIndex}`}>
                    <div>
                      <span className="file-tab">{failure.test}</span>{" "}
                      <span className="pill">{failure.kind || run.kind}</span>
                    </div>
                    {failure.message && <p>{failure.message}</p>}
                    {failure.traceback && <pre className="output">{failure.traceback}</pre>}
                  </div>
                ))
              )}
              {investReport &&
                investReport.baseline.failures.map((failure, index) => (
                <div key={`baseline-${index}`}>
                  <div>
                    <span className="file-tab">{failure.test}</span>{" "}
                    <span className="pill">baseline · {failure.kind}</span>
                  </div>
                  {failure.message && <p>{failure.message}</p>}
                  {failure.traceback && <pre className="output">{failure.traceback}</pre>}
                </div>
              ))}
              {investReport && investReport.verification.failures.length > 0 && (
                <div>
                  {investReport.verification.failures.map((failure, index) => (
                    <div key={`verification-${index}`}>
                      <div>
                        <span className="file-tab">{failure.test}</span>{" "}
                        <span className="pill">post-patch · {failure.kind}</span>
                      </div>
                      {failure.message && <p>{failure.message}</p>}
                      {failure.traceback && <pre className="output">{failure.traceback}</pre>}
                    </div>
                  ))}
                </div>
              )}
              {runFailures.length === 0 &&
                (investReport?.baseline.failures.length ?? 0) === 0 &&
                (investReport?.verification.failures.length ?? 0) === 0 && (
                  <div className="empty-state">
                    <strong>No evidence recorded yet</strong>
                    <p>
                      {subjectActive
                        ? "Test output and tracebacks will appear here once runs complete."
                        : "The backend recorded no test output or tracebacks."}
                    </p>
                  </div>
                )}
            </div>
          )}
          {tab === "execution" && subject && (
            <div role="tabpanel" id="panel-execution" aria-labelledby="tab-execution">
              {subject.attempts.map((attempt) => (
                <div key={attempt.attempt_no}>
                  <div className="card-head">
                    <h3>Attempt {attempt.attempt_no}</h3>
                    <span className="muted">confidence: {attempt.confidence}</span>
                  </div>
                  {attempt.diagnosis && <p>{attempt.diagnosis}</p>}
                  <p className="hint">
                    {attempt.patch ? "patch recorded" : "no patch recorded"} ·{" "}
                    {attempt.guard ? "guard recorded" : "no guard recorded"} ·{" "}
                    {attempt.verification ? "verification recorded" : "no verification recorded"}
                  </p>
                </div>
              ))}
              {subject.runs.map((run, index) => (
                <RunCard
                  key={`run-${index}`}
                  storageKey={`run-${index}`}
                  kind={run.kind}
                  backend={run.backend}
                  status={run.status}
                  passed={run.passed}
                  exitCode={run.exit_code}
                  output={run.output}
                  outputTruncated={run.output_truncated ?? Boolean(run.output?.includes("RepoRescue retained at most"))}
                  durationMs={run.duration_ms}
                  failures={run.failures}
                  open={openOutput === `run-${index}`}
                  onToggle={toggleOutput}
                />
              ))}
              {investReport && (
                <RunCard
                  storageKey="baseline"
                  kind="Baseline"
                  backend={investReport.baseline.backend}
                  isolation={investReport.baseline.isolation}
                  status={investReport.baseline.status}
                  passed={investReport.baseline.passed}
                  exitCode={investReport.baseline.exit_code}
                  output={investReport.baseline.output}
                  durationMs={investReport.baseline.duration_ms}
                  failures={investReport.baseline.failures}
                  installOk={investReport.baseline.install_ok}
                  installLog={investReport.baseline.install_log}
                  outputTruncated={Boolean(investReport.baseline.output?.includes("RepoRescue retained at most"))}
                  open={openOutput === "baseline"}
                  onToggle={toggleOutput}
                />
              )}
              {investReport && (
                <RunCard
                  storageKey="verification"
                  kind="Verification"
                  backend={investReport.verification.backend}
                  isolation={investReport.verification.isolation}
                  status={investReport.verification.status}
                  passed={investReport.verification.passed}
                  exitCode={investReport.verification.exit_code}
                  output={investReport.verification.output}
                  durationMs={investReport.verification.duration_ms}
                  failures={investReport.verification.failures}
                  installOk={investReport.verification.install_ok}
                  installLog={investReport.verification.install_log}
                  outputTruncated={Boolean(investReport.verification.output?.includes("RepoRescue retained at most"))}
                  open={openOutput === "verification"}
                  onToggle={toggleOutput}
                />
              )}
              {subject.timings && (
                <dl className="kv-list">
                  {Object.entries(subject.timings).map(([key, value]) =>
                    typeof value === "string" ||
                    typeof value === "number" ||
                    typeof value === "boolean" ? (
                      <div key={key} className="kv-row">
                        <dt>{key}</dt>
                        <dd>{String(value)}</dd>
                      </div>
                    ) : null
                  )}
                </dl>
              )}
              {!subjectActive &&
                subject.status === "completed" &&
                subject.runs.length === 0 &&
                subject.attempts.length === 0 &&
                !investReport && (
                  <div className="banner banner-info">
                    <div>
                      <strong>Execution was unavailable — not failed.</strong>
                      <p>This investigation recorded no runs, so nothing was executed.</p>
                    </div>
                  </div>
                )}
              {!subjectActive &&
                subject.status !== "completed" &&
                subject.runs.length === 0 &&
                subject.attempts.length === 0 &&
                !investReport && <p className="hint">No runs were recorded.</p>}
              {subjectActive && subject.runs.length === 0 && subject.attempts.length === 0 && (
                <p className="hint">Execution has not started yet.</p>
              )}
            </div>
          )}
          {tab === "changes" && subject && (
            <div role="tabpanel" id="panel-changes" aria-labelledby="tab-changes">
              {subject.patches.map((patch, patchIndex) => (
                <div key={`patch-${patchIndex}`}>
                  <div className="card-head">
                    <h3>Proposal {patchIndex + 1}</h3>
                    <span className="pill">{patch.status}</span>
                  </div>
                  {patch.reason && <p>{patch.reason}</p>}
                  {patch.files.map((file) => (
                    <div key={file.file}>
                      <div>
                        <span className="file-tab">{file.file}</span>
                      </div>
                      <div className="diff">
                        <div className="diff-line removed">
                          <span>−</span>
                          <code>{file.before ?? "not recorded"}</code>
                        </div>
                        <div className="diff-line added">
                          <span>+</span>
                          <code>{file.after ?? "not recorded"}</code>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ))}
              {investReport &&
                investReport.patch.edits.map((edit) => (
                <div key={edit.file}>
                  <div>
                    <span className="file-tab">{edit.file}</span>
                  </div>
                  <div className="diff">
                    <div className="diff-line removed">
                      <span>−</span>
                      <code>{edit.before}</code>
                    </div>
                    <div className="diff-line added">
                      <span>+</span>
                      <code>{edit.after}</code>
                    </div>
                  </div>
                </div>
              ))}
              {investReport?.patch.reason && (
                <div className="patch-explanation">
                  <span className="explanation-icon">
                    <Zap size={14} />
                  </span>
                  <div>
                    <strong>Why this change?</strong>
                    <p>{investReport.patch.reason}</p>
                  </div>
                </div>
              )}
              {investReport && (
                <div className="test-row">
                  <div className={`test-icon ${investReport.guard.validated ? "pass" : "fail"}`}>
                    {investReport.guard.validated ? <Check size={15} /> : <XCircle size={15} />}
                  </div>
                  <div className="test-label">
                    <strong>Patch guard</strong>
                    <small>{investReport.guard.message}</small>
                  </div>
                  <span className={investReport.guard.validated ? "pill green" : "pill amber"}>
                    {investReport.guard.validated ? "validated" : "not validated"}
                  </span>
                </div>
              )}
              <dl className="kv-list">
                <div className="kv-row">
                  <dt>Verification status</dt>
                  <dd>{subject.verification_status}</dd>
                </div>
                {investReport && (
                  <div className="kv-row">
                    <dt>Independent run</dt>
                    <dd>
                      <span
                        className={investReport.verification.passed ? "pill green" : "pill red"}
                      >
                        {investReport.verification.passed
                          ? "independent run passed"
                          : "independent run did not pass"}
                      </span>{" "}
                      <span className="muted">
                        {investReport.execution.backend} ({investReport.execution.isolation})
                      </span>
                    </dd>
                  </div>
                )}
              </dl>
              {subject.patches.length === 0 && (investReport?.patch.edits.length ?? 0) === 0 && (
                <div className="empty-state">
                  <strong>No changes proposed yet</strong>
                  <p>
                    {subjectActive
                      ? "Proposed patches will appear here for review."
                      : "The backend recorded no patches."}
                  </p>
                </div>
              )}
            </div>
          )}
          {tab === "conversation" && subject && (
            <div role="tabpanel" id="panel-conversation" aria-labelledby="tab-conversation">
              <ConversationPanel
                investigationId={subject.investigation_id}
                repoName={subject.repo_name}
                mode={subject.mode}
                active={subjectActive}
                onUnauthorized={onUnauthorized}
              />
            </div>
          )}
        </section>
      )}
        </>
      ) : null}

    </div>
  );
}
