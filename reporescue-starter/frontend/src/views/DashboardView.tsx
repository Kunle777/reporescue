import { AlertTriangle, FolderGit2, LoaderCircle, Plus } from "lucide-react";
import ProjectList from "../components/ProjectList";
import type { Project } from "../lib/types";

export default function DashboardView({
  userEmail,
  projects,
  loading,
  error,
  onRetry,
  onGoProjects,
  onChanged,
  onUnauthorized,
}: {
  userEmail: string | null;
  projects: Project[];
  loading: boolean;
  error: string;
  onRetry: () => void;
  onGoProjects: () => void;
  onChanged: () => void;
  onUnauthorized: () => void;
}) {
  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Welcome{userEmail ? `, ${userEmail}` : ""}</h2>
          <p>Register a public repository to start investigating it.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={onGoProjects}>
          <Plus size={16} /> Add repository
        </button>
      </div>

      <div className="stat-grid">
        <div className="card stat-card">
          <span className="stat-icon">
            <FolderGit2 size={18} />
          </span>
          <div>
            <strong>{loading ? "…" : projects.length}</strong>
            <small>Registered {projects.length === 1 ? "repository" : "repositories"}</small>
          </div>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <h3>Repositories</h3>
          {projects.length > 5 && (
            <button type="button" className="link-button" onClick={onGoProjects}>
              View all
            </button>
          )}
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
            <strong>No repositories yet</strong>
            <p>Add a public GitHub repository to see it here.</p>
            <button type="button" className="btn btn-primary" onClick={onGoProjects}>
              <Plus size={16} /> Add repository
            </button>
          </div>
        ) : (
          <>
            <ProjectList
              projects={projects.slice(0, 5)}
              compact
              onChanged={onChanged}
              onUnauthorized={onUnauthorized}
            />
            {projects.length > 5 && (
              <p className="hint">
                Showing 5 of {projects.length}. Manage the full list in Projects.
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
