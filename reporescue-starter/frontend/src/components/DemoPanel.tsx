import { useState } from "react";
import {
  Activity, AlertTriangle, Check, CheckCircle2, ChevronDown,
  Circle, Code2, FileCode2, FolderGit2, LoaderCircle,
  Play, Shield, ShieldAlert, ShieldCheck, Terminal, XCircle, Zap
} from "lucide-react";
import { apiJson } from "../lib/api";
import type { Report } from "../lib/types";

/**
 * Public demo investigation. Signed-out only: it calls the unauthenticated
 * demo endpoint with a fixed scenario and never touches user repositories.
 */
export default function DemoPanel() {
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showLogs, setShowLogs] = useState(false);

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

  const verified = report?.verdict === "TESTS_PASSED";

  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Public demo</h2>
          <p>A fixed sample investigation. Nothing here touches your repositories.</p>
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
