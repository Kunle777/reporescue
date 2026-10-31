import { useState } from "react";
import {
  Activity, AlertTriangle, Check, CheckCircle2, ChevronDown,
  Circle, Code2, FileCode2, FolderGit2, GitPullRequest, LoaderCircle,
  Play, Shield, ShieldAlert, ShieldCheck, Terminal, XCircle, Zap
} from "lucide-react";

type TestRun = {
  passed: boolean;
  exit_code: number | null;
  output: string;
  duration_ms: number;
};
type Report = {
  status: string;
  scenario: string;
  repository: string;
  mode: string;
  summary: string;
  verdict: string;
  baseline: TestRun;
  verification: TestRun;
  patch: { file: string; before: string; after: string; reason: string };
  limitations: string[];
};
type RepoProfile = {
  url: string;
  name: string;
  commit_sha: string;
  ref: string | null;
  primary_language: string;
  supported: boolean;
  python_requires: string | null;
  dependency_format: string;
  dependency_files: string[];
  frameworks: string[];
  test_framework: string;
  tests: { dirs: string[]; files: string[]; count: number };
  python_file_count: number;
  source_dirs: string[];
  structure_summary: string;
  warnings: string[];
};
type IngestResult = {
  status: string;
  ingestion_id: string;
  profile: RepoProfile;
  warnings: string[];
};

export default function App() {
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showLogs, setShowLogs] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const [repoRef, setRepoRef] = useState("");
  const [ingestLoading, setIngestLoading] = useState(false);
  const [ingestError, setIngestError] = useState("");
  const [ingestResult, setIngestResult] = useState<IngestResult | null>(null);

  async function runInvestigation() {
    setLoading(true);
    setError("");
    setReport(null);
    setShowLogs(false);
    try {
      const response = await fetch("/api/demo/investigate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario: "negative-discount" }),
      });
      if (!response.ok) throw new Error(`API returned HTTP ${response.status}`);
      const data = await response.json();
      if (data.status !== "complete") throw new Error(data.message || "Investigation failed.");
      setReport(data as Report);
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
    try {
      const response = await fetch("/api/repos/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: repoUrl.trim(), ref: repoRef.trim() || null }),
      });
      if (!response.ok) throw new Error(`API returned HTTP ${response.status}`);
      const data = await response.json();
      if (data.status !== "complete") throw new Error(data.message || "Repository ingestion failed.");
      setIngestResult(data as IngestResult);
    } catch (err) {
      setIngestError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
    } finally {
      setIngestLoading(false);
    }
  }

  const verified = report?.verdict === "TESTS_PASSED";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" aria-label="RepoRescue home">
          <span className="brand-mark"><ShieldCheck size={21} strokeWidth={2.2} /></span>
          <span className="brand-name">repo<span>rescue</span><small>DEVELOPER WORKBENCH</small></span>
        </a>
        <div className="workspace-label">WORKSPACE <ChevronDown size={13} /></div>
        <button className="repo-switcher">
          <span className="repo-avatar"><FolderGit2 size={17} /></span>
          <span className="repo-switcher-text"><strong>checkout-service</strong><small>Local demo repository</small></span>
          <ChevronDown size={15} className="muted-icon" />
        </button>
        <div className="nav-label">INVESTIGATE</div>
        <button className="nav-item active"><Activity size={17} /> Investigation <span className="nav-count">01</span></button>
        <button className="nav-item" onClick={() => document.getElementById("patch-panel")?.scrollIntoView({ behavior: "smooth" })}><GitPullRequest size={17} /> Patch review</button>
        <button className="nav-item" onClick={() => document.getElementById("verification-panel")?.scrollIntoView({ behavior: "smooth" })}><ShieldCheck size={17} /> Verification report</button>
        <div className="sidebar-bottom">
          <div className="local-status"><span className="status-dot" /><span><strong>Local environment</strong><small>No paid API calls</small></span></div>
          <div className="sidebar-foot"><Shield size={14} /> Model proposes. Tests verify.</div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><strong>Investigation #001</strong></div>
          <div className="topbar-right"><span className="local-pill"><span className="status-dot" /> LOCAL DEMO</span><div className="avatar">R</div></div>
        </header>

        <div className="content">
          <section className="page-heading">
            <div>
              <div className="eyebrow"><span className="eyebrow-line" /> AI-ASSISTED DEBUGGING</div>
              <h1>Find the bug.<br /><span>Prove the fix.</span></h1>
              <p className="page-subtitle">Investigate a reproducible failure, inspect the proposed patch, and verify the result with real tests.</p>
            </div>
            <div className="heading-art" aria-hidden="true"><div className="art-ring ring-one" /><div className="art-ring ring-two" /><div className="art-core"><ShieldCheck size={34} /></div><span className="art-spark spark-one">✳</span><span className="art-spark spark-two">·</span></div>
          </section>

          <section className="repo-card">
            <div className="repo-card-main">
              <div className="repo-icon"><FolderGit2 size={21} /></div>
              <div className="repo-info"><div className="repo-title">checkout-service-demo <span className="branch-tag"><GitPullRequest size={11} /> main</span></div><div className="repo-meta"><span><FileCode2 size={13} /> Python</span><span><Circle size={5} fill="currentColor" /> 2 source modules</span><span><Terminal size={13} /> pytest</span></div></div>
            </div>
            <div className="repo-card-divider" />
            <div className="repo-card-stat"><span>SCENARIO</span><strong>Negative discount</strong><small>Reproducible bug fixture</small></div>
            <div className="repo-card-stat"><span>VERIFICATION</span><strong className={verified ? "success-text" : ""}>{verified ? "Tests passed" : report ? "Inconclusive" : "Not run yet"}</strong><small>{report ? `${report.verification.duration_ms} ms test run` : "Awaiting investigation"}</small></div>
          </section>

          <section className="panel">
            <div className="panel-topline"><div><span className="panel-icon"><FolderGit2 size={16} /></span><strong>Connect a GitHub repository</strong></div><span className="panel-caption">INSPECTION ONLY</span></div>
            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <input
                style={{ flex: "2 1 16rem", padding: "0.55rem 0.7rem" }}
                placeholder="https://github.com/owner/repo"
                value={repoUrl}
                onChange={(event) => setRepoUrl(event.target.value)}
                aria-label="GitHub repository URL"
              />
              <input
                style={{ flex: "1 1 8rem", padding: "0.55rem 0.7rem" }}
                placeholder="branch, tag, or commit (optional)"
                value={repoRef}
                onChange={(event) => setRepoRef(event.target.value)}
                aria-label="Branch, tag, or commit"
              />
              <button className="run-button" onClick={connectRepository} disabled={ingestLoading || !repoUrl.trim()}>
                {ingestLoading ? <LoaderCircle className="spin" size={16} /> : <GitPullRequest size={15} />}
                {ingestLoading ? "Cloning…" : "Inspect repository"}
              </button>
            </div>
            {ingestError && <div className="error-banner"><AlertTriangle size={17} /><div><strong>Could not inspect repository</strong><p>{ingestError}</p></div></div>}
            {ingestResult && <>
              <div className="file-tab"><FileCode2 size={14} /><span>{ingestResult.profile.name}</span><span className="change-count">{ingestResult.profile.commit_sha.slice(0, 7)}</span></div>
              <p style={{ margin: "0.75rem 0" }}>{ingestResult.profile.structure_summary}</p>
              <div className="test-row"><div className={`test-icon ${ingestResult.profile.supported ? "pass" : "fail"}`}>{ingestResult.profile.supported ? <Check size={15} /> : <XCircle size={15} />}</div><div className="test-label"><strong>{ingestResult.profile.supported ? "Supported Python project" : "Not a supported Python project"}</strong><small>Language: {ingestResult.profile.primary_language} · Dependencies: {ingestResult.profile.dependency_format} · Tests: {ingestResult.profile.test_framework} ({ingestResult.profile.tests.count} files){ingestResult.profile.frameworks.length > 0 ? ` · ${ingestResult.profile.frameworks.join(", ")}` : ""}</small></div></div>
              {ingestResult.warnings.length > 0 && <pre className="test-output">{ingestResult.warnings.join("\n")}</pre>}
            </>}
          </section>

          <div className="section-heading">
            <div><div className="section-kicker">INVESTIGATION FLOW</div><h2>Debugging session</h2></div>
            <button className="run-button" onClick={runInvestigation} disabled={loading}>
              {loading ? <LoaderCircle className="spin" size={16} /> : <Play size={15} fill="currentColor" />}
              {loading ? "Running checks…" : "Run investigation"}
            </button>
          </div>

          {error && <div className="error-banner"><AlertTriangle size={17} /><div><strong>Could not complete investigation</strong><p>{error} Make sure the FastAPI backend is running on port 8000.</p></div></div>}

          <section className="timeline-card">
            <div className="panel-topline"><div><span className="panel-icon"><Activity size={16} /></span><strong>Investigation timeline</strong></div><span className="panel-caption">{loading ? "IN PROGRESS" : report ? "COMPLETED" : "READY TO RUN"}</span></div>
            <div className="timeline">
              <TimelineStep number="01" title="Map repository" detail="Inspect the prepared project structure" state={report || loading ? "done" : "idle"} />
              <TimelineStep number="02" title="Reproduce baseline failure" detail={report ? (report.baseline.passed ? "Baseline unexpectedly passed" : "Test failure captured from pytest") : "Run the original test suite"} state={report ? (report.baseline.passed ? "warn" : "done") : loading ? "active" : "idle"} />
              <TimelineStep number="03" title="Propose targeted patch" detail="Apply the controlled local demo patch" state={report ? "done" : loading ? "active" : "idle"} />
              <TimelineStep number="04" title="Verify independently" detail={report ? (report.verification.passed ? "Post-patch tests passed" : "Post-patch tests did not pass") : "Rerun tests against the patched copy"} state={report ? (report.verification.passed ? "done" : "warn") : loading ? "active" : "idle"} last />
            </div>
            {loading && <div className="loading-note"><LoaderCircle size={14} className="spin" /> Running the bundled pytest suite in a temporary workspace…</div>}
          </section>

          <div className="detail-grid">
            <section className="panel" id="patch-panel">
              <div className="panel-topline"><div><span className="panel-icon"><Code2 size={16} /></span><strong>Patch review</strong></div><span className="file-count">1 FILE</span></div>
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

            <section className="panel" id="verification-panel">
              <div className="panel-topline"><div><span className="panel-icon"><ShieldCheck size={16} /></span><strong>Verification report</strong></div>{report && <span className={`verdict-tag ${verified ? "verdict-good" : "verdict-warn"}`}>{verified ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}{verified ? "PASS" : "CHECK"}</span>}</div>
              {report ? <>
                <div className={`verdict-banner ${verified ? "verdict-banner-good" : "verdict-banner-warn"}`}>
                  {verified ? <ShieldCheck size={21} /> : <ShieldAlert size={21} />}
                  <div><strong>{verified ? "Test-backed fix" : "Inconclusive result"}</strong><p>{report.summary}</p></div>
                </div>
                <div className="test-row"><div className="test-icon fail"><XCircle size={15} /></div><div className="test-label"><strong>Baseline suite</strong><small>{report.baseline.passed ? "Unexpectedly passed" : "Expected failure reproduced"}</small></div><span className={`result-label ${report.baseline.passed ? "result-warn" : "result-fail"}`}>{report.baseline.passed ? "REVIEW" : "FAILED"}</span></div>
                <div className="test-row"><div className={`test-icon ${report.verification.passed ? "pass" : "fail"}`}>{report.verification.passed ? <Check size={15} /> : <XCircle size={15} />}</div><div className="test-label"><strong>Post-patch suite</strong><small>{report.verification.passed ? "All bundled tests passed" : "One or more tests failed"}</small></div><span className={`result-label ${report.verification.passed ? "result-pass" : "result-fail"}`}>{report.verification.passed ? "PASSED" : "FAILED"}</span></div>
                <button className="logs-toggle" onClick={() => setShowLogs(!showLogs)}><Terminal size={14} /> {showLogs ? "Hide test output" : "View raw test output"} <ChevronDown size={14} className={showLogs ? "rotate" : ""} /></button>
                {showLogs && <pre className="test-output">{`BASELINE\n${report.baseline.output}\n\nPOST-PATCH\n${report.verification.output}`}</pre>}
                <div className="integrity-note"><Shield size={14} /><span>Tests ran on a temporary copy. The original bundled fixture was not modified.</span></div>
              </> : <div className="empty-state"><Shield size={24} /><strong>Evidence, not just an AI answer</strong><p>The report will show actual test output and whether the baseline-to-patch transition succeeded.</p></div>}
            </section>
          </div>

          <footer className="page-footer"><span>REPORESCUE <span className="footer-dot">·</span> LOCAL PROTOTYPE</span><span><ShieldCheck size={13} /> Model proposes. Tests verify.</span></footer>
        </div>
      </main>
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
