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
    <div className="view-stack projects-view">
      <header className="page-intro">
        <div>
          <p className="eyebrow">PROJECT CONTEXT</p>
          <h2>Repositories</h2>
          <p>Register public GitHub repositories and keep their canonical identity in one place.</p>
        </div>
        {!loading && !error && <span className="count-chip"><FolderGit2 size={15} /> {projects.length} registered</span>}
      </header>

      <div className="projects-layout">
        <section className="surface-section add-repository-panel" aria-labelledby="add-repository-title">
          <div className="section-title-row">
            <div><p className="eyebrow">ADD TO WORKSPACE</p><h3 id="add-repository-title">Connect a repository</h3></div>
            <span className="section-icon-wrap"><FolderGit2 size={18} /></span>
          </div>
          <p className="section-description">Registering stores the public repository reference. It does not inspect or index its contents.</p>
          <AddRepositoryForm onAdded={onChanged} onUnauthorized={onUnauthorized} />
        </section>

        <section className="surface-section repository-directory" aria-labelledby="repository-directory-title">
          <div className="section-title-row">
            <div><p className="eyebrow">YOUR WORKSPACE</p><h3 id="repository-directory-title">Registered repositories</h3></div>
            {!loading && !error && <span className="count-chip count-chip-small">{projects.length}</span>}
          </div>
          {loading ? (
            <div className="loading-block" role="status"><LoaderCircle size={17} className="spin" /> Loading repositories…</div>
          ) : error ? (
            <div className="banner banner-error" role="alert"><AlertTriangle size={17} /><div><strong>Could not load repositories</strong><p>{error}</p></div><button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>Retry</button></div>
          ) : projects.length === 0 ? (
            <div className="repository-empty"><span className="empty-mark"><FolderGit2 size={20} /></span><div><strong>Your repository list is empty</strong><p>Once registered, repositories will appear here with their owner, default branch, and management actions.</p></div></div>
          ) : (
            <ProjectList projects={projects} onChanged={onChanged} onUnauthorized={onUnauthorized} />
          )}
        </section>
      </div>
    </div>
  );
}
