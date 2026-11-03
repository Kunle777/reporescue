import { useEffect, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, GitPullRequest, LoaderCircle, Play } from "lucide-react";
import { ApiError, apiJson } from "../lib/api";
import { createJob } from "../lib/jobs";
import CapabilityPanel from "../components/CapabilityPanel";
import ModeCards from "../components/ModeCards";
import type { IngestResult, InvestigationMode, Project, View } from "../lib/types";

const URL_CHOICE = "__url__";

function validateRepoUrl(raw: string): string | null {
  const value = raw.trim();
  if (!value) return "Enter a repository URL.";
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "Enter a complete URL, e.g. https://github.com/owner/repo.";
  }
  if (parsed.protocol !== "https:") return "Use an https:// GitHub URL.";
  if (parsed.hostname.toLowerCase() !== "github.com") {
    return "Only public github.com repositories are supported.";
  }
  if (parsed.pathname.split("/").filter(Boolean).length < 2) {
    return "Include the owner and repository, e.g. https://github.com/owner/repo.";
  }
  return null;
}

/**
 * Guided New Investigation flow: choose a registered repository or inspect a
 * URL, pick one of the four modes (General is the default), optionally
 * describe the problem, then submit through the jobs API. Submission hands
 * the created job to the workbench; nothing is inspected twice.
 */
export default function NewInvestigationView({
  projects,
  initialIngest = null,
  onIngestConsumed = () => {},
  onJobCreated,
  onNavigate,
  onUnauthorized,
}: {
  projects: Project[];
  /** Adopted once: an inspection carried over from the Repositories view. */
  initialIngest?: IngestResult | null;
  /** Called after the carried-over inspection is adopted so App clears it. */
  onIngestConsumed?: () => void;
  /** Created job id; App restores it in the workbench. */
  onJobCreated: (jobId: string) => void;
  onNavigate: (view: View) => void;
  onUnauthorized: () => void;
}) {
  const [choice, setChoice] = useState<string>(() =>
    projects.length > 0 ? projects[0].id : URL_CHOICE
  );
  const [repoUrl, setRepoUrl] = useState("");
  const [repoRef, setRepoRef] = useState("");
  const [ingestLoading, setIngestLoading] = useState(false);
  const [ingestError, setIngestError] = useState("");
  const [ingestResult, setIngestResult] = useState<IngestResult | null>(null);
  const [mode, setMode] = useState<InvestigationMode>("general");
  const [userMessage, setUserMessage] = useState("");
  const [submitLoading, setSubmitLoading] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const adoptedRef = useRef(false);

  // Adopt a carried-over inspection once; never re-inspect it here.
  useEffect(() => {
    if (!initialIngest || adoptedRef.current) return;
    adoptedRef.current = true;
    setIngestResult(initialIngest);
    setRepoUrl(initialIngest.profile.url);
    setChoice(URL_CHOICE);
    onIngestConsumed();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialIngest]);

  function selectChoice(id: string) {
    setChoice(id);
    setIngestResult(null);
    setIngestError("");
    setSubmitError("");
  }

  function editUrl(value: string) {
    setRepoUrl(value);
    if (ingestResult) setIngestResult(null);
  }

  function editRef(value: string) {
    setRepoRef(value);
    if (ingestResult) setIngestResult(null);
  }

  async function inspectRepository() {
    const project =
      choice === URL_CHOICE ? null : projects.find((item) => item.id === choice) ?? null;
    const url = project ? project.github_url : repoUrl.trim();
    const ref = project ? project.default_branch ?? null : repoRef.trim() || null;
    if (!project) {
      const problem = validateRepoUrl(url);
      if (problem) {
        setIngestError(problem);
        return;
      }
    }
    setIngestLoading(true);
    setIngestError("");
    setIngestResult(null);
    setSubmitError("");
    try {
      const data = await apiJson<IngestResult & { message?: string }>("/api/repos/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, ref }),
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

  async function startInvestigation() {
    if (!ingestResult) {
      setSubmitError("Inspect a repository first.");
      return;
    }
    setSubmitLoading(true);
    setSubmitError("");
    try {
      const created = await createJob({
        ingestion_id: ingestResult.ingestion_id,
        mode,
        message: userMessage,
      });
      onJobCreated(created.job_id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        setSubmitError("Session expired — you have been signed out. Please sign in again.");
      } else {
        setSubmitError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
      }
    } finally {
      setSubmitLoading(false);
    }
  }

  const urlSelected = choice === URL_CHOICE;

  return (
    <div>
      <div className="view-head">
        <div>
          <h2>New investigation</h2>
          <p>Choose a repository, pick a mode, and start a background job.</p>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <h3>
            <span className="stepnum">1</span> Repository
          </h3>
          {ingestResult && (
            <span className="pill green">inspected · {ingestResult.profile.commit_sha.slice(0, 7)}</span>
          )}
        </div>
        {projects.length > 0 ? (
          <div role="radiogroup" aria-label="Repository choice" className="form-stack">
            {projects.map((project) => (
              <label key={project.id} className="radio-row">
                <input
                  type="radio"
                  name="repo-choice"
                  checked={choice === project.id}
                  onChange={() => selectChoice(project.id)}
                />
                <span>
                  <strong>{project.display_name}</strong>{" "}
                  <small className="muted">
                    {project.github_owner}/{project.repository_name}
                    {project.default_branch ? ` · ${project.default_branch}` : ""}
                  </small>
                </span>
              </label>
            ))}
            <label className="radio-row">
              <input
                type="radio"
                name="repo-choice"
                checked={urlSelected}
                onChange={() => selectChoice(URL_CHOICE)}
              />
              <span>Use a different repository URL</span>
            </label>
          </div>
        ) : (
          <p className="hint">No registered repositories — inspect a URL below, or add one in Projects.</p>
        )}
        <div className="form-row" style={{ marginTop: "10px" }}>
          <input
            className="input input-grow"
            placeholder="https://github.com/owner/repo"
            value={repoUrl}
            onChange={(event) => editUrl(event.target.value)}
            aria-label="GitHub repository URL"
            disabled={!urlSelected}
          />
          <input
            className="input input-ref"
            placeholder="branch, tag, or commit (optional)"
            value={repoRef}
            onChange={(event) => editRef(event.target.value)}
            aria-label="Branch, tag, or commit"
            disabled={!urlSelected}
          />
          <button
            className="btn btn-primary"
            onClick={() => void inspectRepository()}
            disabled={ingestLoading}
          >
            {ingestLoading ? <LoaderCircle className="spin" size={16} /> : <GitPullRequest size={15} />}
            {ingestLoading ? "Inspecting…" : "Inspect repository"}
          </button>
        </div>
        {ingestLoading && (
          <div className="loading-note">
            <LoaderCircle size={14} className="spin" /> Inspecting repository — cloning and profiling…
          </div>
        )}
        {ingestError && (
          <div className="banner banner-error">
            <AlertTriangle size={17} />
            <div>
              <strong>Could not inspect repository</strong>
              <p>{ingestError}</p>
            </div>
          </div>
        )}
        {ingestResult && (
          <CapabilityPanel profile={ingestResult.profile} warnings={ingestResult.warnings} />
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h3>
            <span className="stepnum">2</span> Investigation mode
          </h3>
        </div>
        <ModeCards value={mode} onChange={setMode} />
      </section>

      <section className="card">
        <div className="card-head">
          <h3>
            <span className="stepnum">3</span> What should we look into?{" "}
            <span className="muted">(optional)</span>
          </h3>
        </div>
        <textarea
          className="input"
          style={{ width: "100%" }}
          rows={3}
          placeholder="Describe the bug, question, or concern…"
          value={userMessage}
          onChange={(event) => setUserMessage(event.target.value)}
          aria-label="Problem statement"
        />
        <p className="hint" style={{ marginTop: "8px" }}>
          Runs as a background job. You can leave; progress is saved and restored.
        </p>
        {submitError && (
          <div className="banner banner-error">
            <AlertTriangle size={17} />
            <div>
              <strong>Could not start investigation</strong>
              <p>{submitError}</p>
            </div>
          </div>
        )}
        <div style={{ marginTop: "12px", display: "flex", gap: "8px" }}>
          <button
            className="btn btn-primary"
            onClick={() => void startInvestigation()}
            disabled={submitLoading || !ingestResult}
          >
            {submitLoading ? <LoaderCircle className="spin" size={16} /> : <Play size={15} />}
            {submitLoading ? "Starting…" : "Start investigation"}
          </button>
          <button className="btn btn-secondary" onClick={() => onNavigate("dashboard")}>
            <ArrowLeft size={14} /> Back
          </button>
        </div>
      </section>
    </div>
  );
}
