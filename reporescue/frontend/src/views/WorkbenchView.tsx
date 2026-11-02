import { useEffect, useRef, useState } from "react";
import {
  Activity, AlertTriangle, Check, CheckCircle2, ChevronDown,
  Circle, Code2, FileCode2, FolderGit2, GitPullRequest, LoaderCircle,
  Play, Shield, ShieldAlert, ShieldCheck, Terminal, XCircle, Zap
} from "lucide-react";
import type { Session } from "@supabase/supabase-js";
import { ApiError, apiJson } from "../lib/api";
import { cancelJob, createJob, getJob, listJobs } from "../lib/jobs";
import type {
  IngestResult, InvestigationDetail, InvestigationMode, JobCard,
  RealReport, Report,
} from "../lib/types";
import { isTerminalStatus } from "../lib/types";

type StaticResult = { mode: InvestigationMode; summary: string; overall?: string };

const POLL_MS = 3000;

/** Demo investigation plus persistent background investigations. */
export default function WorkbenchView({
  session,
  onUnauthorized,
  restoredJobId = null,
  onJobAdopted = () => {},
}: {
  session: Session | null;
  onUnauthorized: () => void;
  /** Backend-owned active job to restore (set once by App on refresh). */
  restoredJobId?: string | null;
  /** Called after the restored job is adopted so App clears it. */
  onJobAdopted?: () => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showLogs, setShowLogs] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const [repoRef, setRepoRef] = useState("");
  const [ingestLoading, setIngestLoading] = useState(false);
  const [ingestError, setIngestError] = useState("");
  const [ingestResult, setIngestResult] = useState<IngestResult | null>(null);
  const [investLoading, setInvestLoading] = useState(false);
  const [investError, setInvestError] = useState("");
  const [investReport, setInvestReport] = useState<RealReport | null>(null);
  const [showRealLogs, setShowRealLogs] = useState(false);
  const [mode, setMode] = useState<InvestigationMode>("debug");
  const [userMessage, setUserMessage] = useState("");
  const [jobDetail, setJobDetail] = useState<InvestigationDetail | null>(null);
  const [jobError, setJobError] = useState("");
  const [staticResult, setStaticResult] = useState<StaticResult | null>(null);
  const [recentJobs, setRecentJobs] = useState<JobCard[]>([]);
  const [cancelling, setCancelling] = useState(false);
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
    if (detail.status === "completed" && detail.final_report) {
      if (detail.mode === "debug") {
        setInvestReport(detail.final_report as unknown as RealReport);
      } else {
        const summary = detail.final_report["summary"];
        const overall = detail.final_report["overall"];
        setStaticResult({
          mode: detail.mode,
          summary: typeof summary === "string" ? summary : "Assessment complete.",
          overall: typeof overall === "string" ? overall : undefined,
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

  async function runInvestigation() {
    setLoading(true);
    setError("");
    setReport(null);
    setShowLogs(false);
    try {
      const data = await apiJson<Report & { message?: string }>(
        "/api/demo/investigate",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scenario: "negative-discount" }),
        },
        { auth: false }
      );
      if (data.status !== "complete") throw new Error(data.message || "Investigation failed.");
      setReport(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
    } finally {
      setLoading(false);
    }
  }

  async function connectRepository() {
    setIngestLoading(true);
    setIngestError("");
    setIngestResult(null);
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
    setJobError("");
    setInvestError("");
    setInvestReport(null);
    setStaticResult(null);
    setShowRealLogs(false);
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
    setInvestError("");
    setInvestReport(null);
    setStaticResult(null);
    setShowRealLogs(false);
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

  const verified = report?.verdict === "TESTS_PASSED";
  const realVerified = investReport?.verdict === "TESTS_PASSED";

  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Workbench</h2>
          <p>Run the bundled demo or inspect a real repository.</p>
        </div>
        <button className="btn btn-primary" onClick={() => void runInvestigation()} disabled={loading}>
          {loading ? <LoaderCircle className="spin" size={16} /> : <Play size={15} />}
          {loading ? "Running checks…" : "Run demo investigation"}
        </button>
      </div>

      {error && <div className="banner banner-error"><AlertTriangle size={17} /><div><strong>Could not complete investigation</strong><p>{error} Make sure the FastAPI backend is running on port 8000.</p></div></div>}

      <section className="card">
        <div className="repo-head">
          <span className="row-icon"><FolderGit2 size={18} /></span>
          <div className="row-main">
            <strong>checkout-service-demo</strong>
            <small><FileCode2 size={13} /> Python <Circle size={5} fill="currentColor" /> 2 source modules <Terminal size={13} /> pytest</small>
          </div>
          <span className="pill">{verified ? "Tests passed" : report ? "Inconclusive" : "Not run yet"}</span>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h3><Activity size={16} /> Investigation timeline</h3>
          <span className="caption">{loading ? "IN PROGRESS" : report ? "COMPLETED" : "READY TO RUN"}</span>
        </div>
        <div className="timeline">
          <TimelineStep number="01" title="Map repository" detail="Inspect the prepared project structure" state={report || loading ? "done" : "idle"} />
          <TimelineStep number="02" title="Reproduce baseline failure" detail={report ? (report.baseline.passed ? "Baseline unexpectedly passed" : "Test failure captured from pytest") : "Run the original test suite"} state={report ? (report.baseline.passed ? "warn" : "done") : loading ? "active" : "idle"} />
          <TimelineStep number="03" title="Propose targeted patch" detail="Apply the controlled local demo patch" state={report ? "done" : loading ? "active" : "idle"} />
          <TimelineStep number="04" title="Verify independently" detail={report ? (report.verification.passed ? "Post-patch tests passed" : "Post-patch tests did not pass") : "Rerun tests against the patched copy"} state={report ? (report.verification.passed ? "done" : "warn") : loading ? "active" : "idle"} last />
        </div>
        {loading && <div className="loading-note"><LoaderCircle size={14} className="spin" /> Running the bundled pytest suite in a temporary workspace…</div>}
      </section>

      {session ? (
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
            <div className="file-tab"><FileCode2 size={14} /><span>{ingestResult.profile.name}</span><span className="change-count">{ingestResult.profile.commit_sha.slice(0, 7)}</span></div>
            <p className="hint">{ingestResult.profile.structure_summary}</p>
            <div className="test-row"><div className={`test-icon ${ingestResult.profile.supported ? "pass" : "fail"}`}>{ingestResult.profile.supported ? <Check size={15} /> : <XCircle size={15} />}</div><div className="test-label"><strong>{ingestResult.profile.supported ? "Supported Python project" : "Not a supported Python project"}</strong><small>Language: {ingestResult.profile.primary_language} · Dependencies: {ingestResult.profile.dependency_format} · Tests: {ingestResult.profile.test_framework} ({ingestResult.profile.tests.count} files){ingestResult.profile.frameworks.length > 0 ? ` · ${ingestResult.profile.frameworks.join(", ")}` : ""}</small></div></div>
            {ingestResult.warnings.length > 0 && <pre className="output">{ingestResult.warnings.join("\n")}</pre>}
            <div className="form-row">
              <select
                className="input input-ref"
                value={mode}
                onChange={(event) => setMode(event.target.value as InvestigationMode)}
                aria-label="Investigation mode"
              >
                <option value="debug">Debug</option>
                <option value="explain">Explain</option>
                <option value="health">Health</option>
              </select>
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
          {investReport && <>
            <div className={`verdict-banner ${realVerified ? "verdict-banner-good" : "verdict-banner-warn"}`}>
              {realVerified ? <ShieldCheck size={21} /> : <ShieldAlert size={21} />}
              <div><strong>{realVerified ? "Test-backed fix" : "Inconclusive result"}</strong><p>{investReport.summary}</p></div>
            </div>
            <div className="test-row"><div className="test-icon fail"><XCircle size={15} /></div><div className="test-label"><strong>Baseline failures</strong><small>{investReport.baseline.failures.length > 0 ? investReport.baseline.failures.map((failure) => failure.test).join(", ") : "No parsed failures"}</small></div><span className="result-label result-fail">{investReport.baseline.status.toUpperCase()}</span></div>
            {investReport.diagnosis && <div className="patch-explanation"><span className="explanation-icon"><Zap size={14} /></span><div><strong>AI diagnosis</strong><p>{investReport.diagnosis}</p></div></div>}
            {investReport.patch.edits.map((edit) => <div key={edit.file}>
              <div className="file-tab"><FileCode2 size={14} /><span>{edit.file}</span></div>
              <div className="diff">
                <div className="diff-line removed"><span>−</span><code>{edit.before}</code></div>
                <div className="diff-line added"><span>+</span><code>{edit.after}</code></div>
              </div>
            </div>)}
            {investReport.patch.reason && <div className="patch-explanation"><span className="explanation-icon"><Zap size={14} /></span><div><strong>Why this change?</strong><p>{investReport.patch.reason}</p></div></div>}
            <div className="test-row"><div className={`test-icon ${investReport.guard.validated ? "pass" : "fail"}`}>{investReport.guard.validated ? <Check size={15} /> : <XCircle size={15} />}</div><div className="test-label"><strong>Patch guard</strong><small>{investReport.guard.message}</small></div></div>
            <div className="test-row"><div className={`test-icon ${investReport.verification.passed ? "pass" : "fail"}`}>{investReport.verification.passed ? <Check size={15} /> : <XCircle size={15} />}</div><div className="test-label"><strong>Post-patch suite</strong><small>{investReport.verification.passed ? "Verification run passed" : "Verification run did not pass"} · {investReport.execution.backend} ({investReport.execution.isolation}) · {investReport.attempts} attempt(s)</small></div><span className={`result-label ${investReport.verification.passed ? "result-pass" : "result-fail"}`}>{investReport.verification.passed ? "PASSED" : "FAILED"}</span></div>
            <button className="btn btn-secondary btn-sm" onClick={() => setShowRealLogs(!showRealLogs)}><Terminal size={14} /> {showRealLogs ? "Hide test output" : "View raw test output"} <ChevronDown size={14} className={showRealLogs ? "rotate" : ""} /></button>
            {showRealLogs && <pre className="output">{`BASELINE\n${investReport.baseline.output}\n\nPOST-PATCH\n${investReport.verification.output}`}</pre>}
          </>}
          {staticResult && <>
            <div className="verdict-banner verdict-banner-good">
              <ShieldCheck size={21} />
              <div><strong>{staticResult.mode === "explain" ? "Explanation" : "Health assessment"}{staticResult.overall ? ` — ${staticResult.overall}` : ""}</strong><p>{staticResult.summary}</p></div>
            </div>
          </>}
        </section>
      ) : (
        <section className="card">
          <div className="card-head">
            <h3><FolderGit2 size={16} /> Inspect a GitHub repository</h3>
            <span className="caption">SIGN IN REQUIRED</span>
          </div>
          <p className="hint">Sign in to inspect repositories and run real investigations. Your projects stay private to your account.</p>
        </section>
      )}

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <h3><Code2 size={16} /> Patch review</h3>
            <span className="pill">1 FILE</span>
          </div>
          {report ? <>
            <div className="file-tab"><FileCode2 size={14} /><span>{report.patch.file}</span><span className="change-count">1 change</span></div>
            <div className="diff">
              <div className="diff-line context"><span>1</span><code>def final_price(price: float, discount: float) -&gt; float:</code></div>
              <div className="diff-line context"><span>2</span><code>    """Return the amount charged after applying a discount."""</code></div>
              <div className="diff-line removed"><span>3 −</span><code>{report.patch.before.trim()}</code></div>
              <div className="diff-line added"><span>3 +</span><code>{report.patch.after.trim()}</code></div>
            </div>
            <div className="patch-explanation"><span className="explanation-icon"><Zap size={14} /></span><div><strong>Why this change?</strong><p>{report.patch.reason}</p></div></div>
            <div className="demo-disclaimer"><AlertTriangle size={13} /> Deterministic demo patch — not model-generated.</div>
          </> : <div className="empty-state"><Code2 size={24} /><strong>Your patch will appear here</strong><p>Run the investigation to inspect the changed line and its rationale.</p></div>}
        </section>

        <section className="card">
          <div className="card-head">
            <h3><ShieldCheck size={16} /> Verification report</h3>
            {report && <span className={`verdict-tag ${verified ? "verdict-good" : "verdict-warn"}`}>{verified ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}{verified ? "PASS" : "CHECK"}</span>}
          </div>
          {report ? <>
            <div className={`verdict-banner ${verified ? "verdict-banner-good" : "verdict-banner-warn"}`}>
              {verified ? <ShieldCheck size={21} /> : <ShieldAlert size={21} />}
              <div><strong>{verified ? "Test-backed fix" : "Inconclusive result"}</strong><p>{report.summary}</p></div>
            </div>
            <div className="test-row"><div className="test-icon fail"><XCircle size={15} /></div><div className="test-label"><strong>Baseline suite</strong><small>{report.baseline.passed ? "Unexpectedly passed" : "Expected failure reproduced"}</small></div><span className={`result-label ${report.baseline.passed ? "result-warn" : "result-fail"}`}>{report.baseline.passed ? "REVIEW" : "FAILED"}</span></div>
            <div className="test-row"><div className={`test-icon ${report.verification.passed ? "pass" : "fail"}`}>{report.verification.passed ? <Check size={15} /> : <XCircle size={15} />}</div><div className="test-label"><strong>Post-patch suite</strong><small>{report.verification.passed ? "All bundled tests passed" : "One or more tests failed"}</small></div><span className={`result-label ${report.verification.passed ? "result-pass" : "result-fail"}`}>{report.verification.passed ? "PASSED" : "FAILED"}</span></div>
            <button className="btn btn-secondary btn-sm" onClick={() => setShowLogs(!showLogs)}><Terminal size={14} /> {showLogs ? "Hide test output" : "View raw test output"} <ChevronDown size={14} className={showLogs ? "rotate" : ""} /></button>
            {showLogs && <pre className="output">{`BASELINE\n${report.baseline.output}\n\nPOST-PATCH\n${report.verification.output}`}</pre>}
            <div className="integrity-note"><Shield size={14} /><span>Tests ran on a temporary copy. The original bundled fixture was not modified.</span></div>
          </> : <div className="empty-state"><Shield size={24} /><strong>Evidence, not just an AI answer</strong><p>The report will show actual test output and whether the baseline-to-patch transition succeeded.</p></div>}
        </section>
      </div>
    </div>
  );
}

function TimelineStep({ number, title, detail, state, last = false }: { number: string; title: string; detail: string; state: "idle" | "active" | "done" | "warn"; last?: boolean }) {
  return <div className={`timeline-step ${last ? "last" : ""}`}>
    <div className={`timeline-node node-${state}`}>{state === "done" ? <Check size={13} /> : state === "warn" ? <AlertTriangle size={13} /> : state === "active" ? <LoaderCircle size={13} className="spin" /> : <span>{number}</span>}</div>
    <div className="timeline-copy"><strong>{title}</strong><span>{detail}</span></div>
    <span className={`step-state state-${state}`}>{state === "done" ? "COMPLETE" : state === "active" ? "RUNNING" : state === "warn" ? "REVIEW" : "PENDING"}</span>
  </div>;
}
