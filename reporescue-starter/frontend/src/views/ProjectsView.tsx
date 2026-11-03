import { useState } from "react";
import { AlertTriangle, FolderGit2, LoaderCircle } from "lucide-react";
import AddRepositoryForm from "../components/AddRepositoryForm";
import CapabilityPanel from "../components/CapabilityPanel";
import ProjectList from "../components/ProjectList";
import { ApiError, apiJson } from "../lib/api";
import type { IngestResult, Project } from "../lib/types";

export default function ProjectsView({
  projects,
  loading,
  error,
  onRetry,
  onChanged,
  onStartInvestigation,
  onUnauthorized,
}: {
  projects: Project[];
  loading: boolean;
  error: string;
  onRetry: () => void;
  onChanged: () => void;
  onStartInvestigation: (ingest: IngestResult) => void;
  onUnauthorized: () => void;
}) {
  const [repoUrl, setRepoUrl] = useState("");
  const [ref, setRef] = useState("");
  const [inspecting, setInspecting] = useState(false);
  const [inspectError, setInspectError] = useState("");
  const [inspected, setInspected] = useState<IngestResult | null>(null);

  async function inspect() {
    const url = repoUrl.trim();
    if (!url) {
      setInspectError("Enter a repository URL first.");
      return;
    }
    setInspecting(true);
    setInspectError("");
    setInspected(null);
    try {
      const data = await apiJson<IngestResult & { message?: string }>("/api/repos/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, ref: ref.trim() || null }),
      });
      if (data.status !== "complete") throw new Error(data.message || "Repository ingestion failed.");
      setInspected(data);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
      } else {
        setInspectError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
      }
    } finally {
      setInspecting(false);
    }
  }

  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Repositories</h2>
          <p>Public GitHub repositories registered to your account.</p>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <h3>Add repository</h3>
        </div>
        <AddRepositoryForm onAdded={onChanged} onUnauthorized={onUnauthorized} />
      </section>

      <section className="card">
        <div className="card-head">
          <h3>Inspect repository</h3>
        </div>
        <div className="form-row">
          <input
            className="input input-grow"
            placeholder="https://github.com/org/repo"
            aria-label="GitHub repository URL"
            value={repoUrl}
            onChange={(event) => setRepoUrl(event.target.value)}
          />
          <input
            className="input"
            style={{ width: "140px" }}
            placeholder="main (optional)"
            aria-label="Branch, tag, or commit"
            value={ref}
            onChange={(event) => setRef(event.target.value)}
          />
          <button className="btn btn-secondary" onClick={() => void inspect()} disabled={inspecting}>
            {inspecting ? <LoaderCircle size={14} className="spin" /> : null}
            {inspecting ? "Inspecting…" : "Inspect"}
          </button>
        </div>
        {inspectError && (
          <div className="banner banner-error" role="alert" style={{ marginTop: "10px" }}>
            <AlertTriangle size={17} />
            <div>
              <strong>Could not inspect repository</strong>
              <p>{inspectError}</p>
            </div>
          </div>
        )}
      </section>

      {inspected && (
        <>
          <CapabilityPanel profile={inspected.profile} />
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              className="btn btn-primary"
              onClick={() => {
                if (inspected) onStartInvestigation(inspected);
              }}
            >
              Start investigation →
            </button>
          </div>
        </>
      )}

      <section className="card">
        <div className="card-head">
          <h3>
            Registered repositories{" "}
            {!loading && !error && <span className="pill">{projects.length}</span>}
          </h3>
        </div>
        {loading ? (
          <div className="loading-note">
            <LoaderCircle size={14} className="spin" /> Loading repositories…
          </div>
        ) : error ? (
          <div className="banner banner-error">
            <AlertTriangle size={17} />
            <div>
              <strong>Could not load repositories</strong>
              <p>{error}</p>
            </div>
            <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
              Retry
            </button>
          </div>
        ) : projects.length === 0 ? (
          <div className="empty-state">
            <FolderGit2 size={26} />
            <strong>Nothing registered yet</strong>
            <p>Use the form above to add your first public repository.</p>
          </div>
        ) : (
          <ProjectList
            projects={projects}
            onChanged={onChanged}
            onUnauthorized={onUnauthorized}
          />
        )}
      </section>
    </div>
  );
}
