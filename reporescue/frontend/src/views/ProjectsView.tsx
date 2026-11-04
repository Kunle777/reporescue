import { AlertTriangle, FolderGit2, LoaderCircle } from "lucide-react";
import AddRepositoryForm from "../components/AddRepositoryForm";
import ProjectList from "../components/ProjectList";
import type { Project } from "../lib/types";

export default function ProjectsView({
  projects,
  loading,
  error,
  onRetry,
  onChanged,
  onUnauthorized,
}: {
  projects: Project[];
  loading: boolean;
  error: string;
  onRetry: () => void;
  onChanged: () => void;
  onUnauthorized: () => void;
}) {
  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Projects</h2>
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
